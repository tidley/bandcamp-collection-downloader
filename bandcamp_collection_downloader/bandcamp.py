from __future__ import annotations

import html
import json
import re
import shutil
import time
import urllib.parse
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable

from .cache import LegacyCache, cache_id_from_sitem_id
from .filenames import sanitize_filename, unique_path
from .http_client import BrowserHttp, HttpResult, add_query


PURCHASES_URL = "https://bandcamp.com/your/purchases"
DEFAULT_FORMAT = "mp3-320"
FORMAT_ALIASES = {
    "aac": "aac-hi",
    "aiff": "aiff-lossless",
    "ogg": "vorbis",
}
FORMAT_EXTENSIONS = {
    "mp3-320": "mp3",
    "mp3-v0": "mp3",
    "flac": "flac",
    "aac-hi": "m4a",
    "vorbis": "ogg",
    "alac": "m4a",
    "wav": "wav",
    "aiff-lossless": "aiff",
}
COLLECTION_ITEMS_ENDPOINT = "/api/fancollection/1/collection_items"
AUDIO_EXTENSIONS = {f".{extension}" for extension in FORMAT_EXTENSIONS.values()}
COVER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass(frozen=True)
class TrackInfo:
    title: str
    number: int | None = None


@dataclass(frozen=True)
class CollectionState:
    item_count: int
    batch_size: int
    last_token: str | None
    initial_count: int


@dataclass(frozen=True)
class DownloadLink:
    url: str
    title: str
    source: str
    sitem_id: str | None = None
    year: str | None = None
    artist: str | None = None
    cache_ids: tuple[str, ...] = ()
    tracks: tuple[TrackInfo, ...] = ()
    cover_url: str | None = None
    expected_track_count: int | None = None
    is_preorder: bool = False
    release_date: str | None = None

    @property
    def cache_id(self) -> str | None:
        ids = self.matching_cache_ids
        return ids[0] if ids else None

    @property
    def matching_cache_ids(self) -> tuple[str, ...]:
        return _unique_cache_ids(*self.cache_ids, cache_id_from_sitem_id(self.sitem_id or _sitem_id_from_url(self.url)))

    @property
    def cache_description(self) -> str:
        return f'"{self.title}" ({self.year or str(time.gmtime().tm_year)}) by {self.artist or "Unknown Artist"}'


@dataclass
class DownloadSummary:
    collection_items: int = 0
    already_downloaded: int = 0
    new_downloads: int = 0
    succeeded: int = 0
    failed: int = 0
    duplicates: int = 0
    preorders_skipped: int = 0
    incomplete_skipped: int = 0


@dataclass
class DownloadPlan:
    pending: list[DownloadLink]
    skipped: list[DownloadLink]
    preorders: list[DownloadLink]
    incomplete: list[DownloadLink]
    summary: DownloadSummary


@dataclass(frozen=True)
class TargetLayout:
    folder: Path
    files: tuple[Path, ...]


@dataclass(frozen=True)
class ExtractedArchive:
    audio: tuple[Path, ...]
    covers: tuple[Path, ...]


class BandcampClient:
    def __init__(self, http: BrowserHttp, log, download_format: str = DEFAULT_FORMAT):
        self.http = http
        self.log = log
        self.download_format = normalize_format(download_format)

    def find_downloads(self, *, endpoints: list[str] | None = None, username: str | None = None) -> list[DownloadLink]:
        start_url = f"https://bandcamp.com/{username}" if username else PURCHASES_URL
        response = self._fetch_html(start_url)
        self._log_response("auth/page", response)

        if self._looks_logged_out(response):
            self.log("auth failed: response looks logged out")
            return []

        page_data = _pagedata_from_text(response.text)
        collection_state = _collection_state_from_pagedata(page_data)
        links = list(_download_links_from_text(response.text, response.final_url))
        fan_id = _extract_fan_id_from_pagedata(page_data) or _extract_fan_id(response.text)
        self.log(f"fan_id found: {fan_id or 'no; API requests will omit fan_id'}")
        if collection_state:
            self.log(
                "collection bootstrap: "
                f"initial={collection_state.initial_count} "
                f"total={collection_state.item_count} "
                f"batch={collection_state.batch_size} "
                f"last_token={collection_state.last_token or '-'}"
            )

        api_endpoints = list(dict.fromkeys([*_collection_api_endpoints_from_text(response.text, response.final_url), *(endpoints or [])]))
        if collection_state:
            endpoint = urllib.parse.urljoin(response.final_url, COLLECTION_ITEMS_ENDPOINT)
            if endpoint not in api_endpoints:
                api_endpoints.insert(0, endpoint)
                self.log(f"collection API endpoint inferred from fan page bootstrap: {endpoint}")
        if not api_endpoints:
            self.log("collection API endpoint found: no; pass --endpoint with the endpoint from browser devtools")
        for endpoint in api_endpoints:
            links.extend(
                self._fetch_collection_api(
                    endpoint,
                    fan_id,
                    response.final_url,
                    start_token=collection_state.last_token if collection_state else None,
                    batch_size=collection_state.batch_size if collection_state else 20,
                    expected_count=collection_state.item_count if collection_state else None,
                    initial_count=collection_state.initial_count if collection_state else 0,
                )
            )

        return _dedupe_links(links)

    def resolve_download(self, link: DownloadLink) -> DownloadLink | None:
        url = add_query(link.url, enc=self.download_format)
        response = self.http.request(
            url,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            referer=link.source,
            max_bytes=1024 * 1024,
        )
        self._log_response(f"download page {link.title}", response)

        content_type = response.headers.get("Content-Type", "")
        if response.status == 200 and ("audio/" in content_type or "application/octet-stream" in content_type or "application/zip" in content_type):
            return DownloadLink(response.final_url, link.title, link.source)

        resolved = _extract_encoded_download(response.text, response.final_url, self.download_format)
        if resolved:
            return DownloadLink(resolved, link.title, response.final_url)

        self.log(f"no {self.download_format} link found for {link.title}: {response.excerpt()}")
        return None

    def plan_downloads(self, links: Iterable[DownloadLink], cache: LegacyCache | None = None, *, include_preorders: bool = False) -> DownloadPlan:
        links = list(links)
        summary = DownloadSummary(collection_items=len(links))
        pending: list[DownloadLink] = []
        skipped: list[DownloadLink] = []
        preorders: list[DownloadLink] = []
        incomplete: list[DownloadLink] = []
        seen: set[str] = set()
        for link in links:
            cache_ids = link.matching_cache_ids
            duplicate_id = next((item for item in cache_ids if item in seen), None)
            if duplicate_id:
                summary.duplicates += 1
                self.log(f"Skipping duplicate: {duplicate_id}")
                continue
            seen.update(cache_ids)
            cache_hit = cache.find(cache_ids) if cache else None
            if cache_hit:
                self.log(f"Cache hit: {cache_hit}")
                self.log(f"Skipping: {link.title}")
                summary.already_downloaded += 1
                skipped.append(link)
            else:
                if not include_preorders and _is_preorder_link(link):
                    self.log(f"Preorder skipped: {link.cache_id or '-'} release_date={link.release_date or '-'}")
                    summary.preorders_skipped += 1
                    preorders.append(link)
                    continue
                if not include_preorders and _is_metadata_incomplete(link):
                    self.log(
                        f"Incomplete skipped: {link.cache_id or '-'} "
                        f"expected={link.expected_track_count or 0} available={len(link.tracks)}"
                    )
                    summary.incomplete_skipped += 1
                    incomplete.append(link)
                    continue
                pending.append(link)
        summary.new_downloads = len(pending)
        return DownloadPlan(pending=pending, skipped=skipped, preorders=preorders, incomplete=incomplete, summary=summary)

    def download_all(
        self,
        links: Iterable[DownloadLink],
        out_dir: Path,
        jobs: int,
        cache: LegacyCache | None = None,
        *,
        include_preorders: bool = False,
    ) -> DownloadSummary:
        return self.download_plan(self.plan_downloads(links, cache, include_preorders=include_preorders), out_dir, jobs, cache)

    def download_plan(self, plan: DownloadPlan, out_dir: Path, jobs: int, cache: LegacyCache | None = None) -> DownloadSummary:
        summary = plan.summary
        if not plan.pending:
            return summary

        done = 0
        failed = 0
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            futures = [pool.submit(self._download_one, link, out_dir, cache) for link in plan.pending]
            for future in as_completed(futures):
                result = future.result()
                if result == "success":
                    done += 1
                elif result == "incomplete":
                    summary.incomplete_skipped += 1
                elif result == "preorder":
                    summary.preorders_skipped += 1
                else:
                    failed += 1
        summary.succeeded = done
        summary.failed = failed
        return summary

    def target_layout(self, out_dir: Path, link: DownloadLink) -> TargetLayout:
        folder = _release_folder(out_dir, link)
        reserved: set[Path] = set()
        files = [unique_path(folder, filename, reserved) for filename in _expected_track_filenames(link, self.download_format)]
        if link.cover_url:
            files.append(unique_path(folder, "cover.jpg", reserved))
        return TargetLayout(folder=folder, files=tuple(files))

    def _download_one(self, link: DownloadLink, out_dir: Path, cache: LegacyCache | None) -> str:
        resolved = self.resolve_download(link)
        if not resolved:
            return "incomplete" if _is_metadata_incomplete(link) else "failed"

        target_dir = _release_folder(out_dir, link)
        target_dir.mkdir(parents=True, exist_ok=True)
        archive = target_dir / f".{sanitize_filename(link.cache_id or link.title, default='download')}.{self.download_format}.download"

        final_url, size, error = self.http.download(resolved.url, archive, referer=resolved.source)
        if error or not archive.exists() or archive.stat().st_size == 0:
            self.log(f"download failed: {resolved.title}: {error}")
            return "failed"

        if zipfile.is_zipfile(archive):
            stage = target_dir / f".{sanitize_filename(link.cache_id or link.title, default='download')}.extract"
            if stage.exists():
                shutil.rmtree(stage)
            stage.mkdir(parents=True)
            extracted = self._extract_archive(archive, stage)
            archive.unlink(missing_ok=True)
            if _is_download_incomplete(link, len(extracted.audio)):
                shutil.rmtree(stage, ignore_errors=True)
                self.log(
                    f"incomplete skipped: {link.cache_id or '-'} "
                    f"expected={link.expected_track_count or 0} downloaded={len(extracted.audio)}"
                )
                return "incomplete"
            targets = _move_extracted_archive(extracted, target_dir)
            shutil.rmtree(stage, ignore_errors=True)
        else:
            if _is_download_incomplete(link, 1):
                archive.unlink(missing_ok=True)
                self.log(f"incomplete skipped: {link.cache_id or '-'} expected={link.expected_track_count or 0} downloaded=1")
                return "incomplete"
            target = unique_path(target_dir, _expected_track_filenames(link, self.download_format)[0])
            archive.replace(target)
            targets = [target]

        if not targets:
            self.log(f"download failed: {resolved.title}: no audio files found")
            return "failed"

        self._download_cover_if_needed(link, target_dir)
        self.log(f"downloaded: {target_dir} ({size} bytes) from {final_url}")
        if _is_preorder_link(link):
            self.log(f"preorder not cached: {link.cache_id or '-'}")
            return "preorder"
        if cache:
            cache.append(link.cache_id, link.cache_description)
        return "success"

    def _extract_archive(self, archive: Path, target_dir: Path) -> ExtractedArchive:
        audio: list[Path] = []
        covers: list[Path] = []
        reserved: set[Path] = set()
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = Path(info.filename).name
                suffix = Path(name).suffix.lower()
                if suffix in AUDIO_EXTENSIONS:
                    target = unique_path(target_dir, name, reserved)
                    _write_zip_member(zf, info, target)
                    audio.append(target)
                elif suffix in COVER_EXTENSIONS and Path(name).stem.lower() in {"cover", "folder"}:
                    target = unique_path(target_dir, f"cover{suffix}", reserved)
                    _write_zip_member(zf, info, target)
                    covers.append(target)
        return ExtractedArchive(audio=tuple(audio), covers=tuple(covers))

    def _download_cover_if_needed(self, link: DownloadLink, target_dir: Path) -> None:
        if not link.cover_url or any((target_dir / f"cover{suffix}").exists() for suffix in COVER_EXTENSIONS):
            return
        target = target_dir / "cover.jpg"
        _, _, error = self.http.download(link.cover_url, target, referer=link.source)
        if error:
            self.log(f"cover download failed for {link.title}: {error}")

    def _fetch_html(self, url: str, *, referer: str | None = None) -> HttpResult:
        return self.http.request(url, headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}, referer=referer)

    def _fetch_collection_api(
        self,
        endpoint: str,
        fan_id: int | None,
        referer: str,
        *,
        start_token: str | None = None,
        batch_size: int = 20,
        expected_count: int | None = None,
        initial_count: int = 0,
    ) -> list[DownloadLink]:
        links: list[DownloadLink] = []
        older_than_token = start_token
        seen_tokens: set[str] = {older_than_token} if older_than_token else set()
        seen_items = initial_count
        stop_reason = "page limit reached"
        for page in range(1, 1000):
            payload = {"count": max(1, batch_size)}
            if fan_id:
                payload["fan_id"] = fan_id
            if older_than_token:
                payload["older_than_token"] = older_than_token
            offset = seen_items
            response = self.http.request(endpoint, method="POST", json_data=payload, referer=referer)
            self._log_response(f"api {endpoint} page {page}", response)
            if response.status != 200:
                stop_reason = f"HTTP {response.status}"
                break
            try:
                data = json.loads(response.text)
            except json.JSONDecodeError:
                self.log(f"api parse failed: {response.excerpt()}")
                stop_reason = "invalid JSON"
                break

            page_links = _download_links_from_json(data, endpoint, self.download_format)
            raw_items = _collection_api_item_count(data) or len(page_links)
            next_token = _find_value(data, "last_token") or _find_value(data, "older_than_token")
            more_available = data.get("more_available") if isinstance(data, dict) else None
            links.extend(page_links)
            seen_items += raw_items
            self.log(
                "collection api page="
                f"{page} offset={offset} token={older_than_token or '-'} "
                f"items={raw_items} downloads={len(page_links)} "
                f"next_token={next_token or '-'} more_available={more_available}"
            )

            if raw_items == 0:
                stop_reason = "empty page"
                break
            if more_available is False:
                stop_reason = "more_available=false"
                break
            if not next_token:
                stop_reason = "no next token"
                break
            if next_token in seen_tokens:
                stop_reason = f"repeated token {next_token}"
                break
            if expected_count is not None and seen_items >= expected_count and more_available is not True:
                stop_reason = f"expected count reached ({seen_items}/{expected_count})"
                break
            seen_tokens.add(next_token)
            older_than_token = next_token
        self.log(f"collection api stopped: {stop_reason}; items_seen={seen_items}; downloads_found={len(links)}")
        return links

    def _log_response(self, label: str, response: HttpResult) -> None:
        self.log(f"{label}: status={response.status} final_url={response.final_url}")
        if response.status >= 400 or response.status == 0:
            self.log(f"{label} body: {response.excerpt()}")

    @staticmethod
    def _looks_logged_out(response: HttpResult) -> bool:
        if response.status in {401, 403}:
            return True
        parsed = urllib.parse.urlsplit(response.final_url)
        return parsed.path.startswith("/login")


def _download_links_from_text(text: str, source: str) -> list[DownloadLink]:
    page_links = _download_links_from_pagedata(_pagedata_from_text(text), source)
    if page_links:
        return page_links

    text = html.unescape(text).replace("\\/", "/")
    links: list[DownloadLink] = []
    for block in _collection_item_blocks(text):
        url = _download_url_from_text(block, source)
        if not url:
            continue
        links.append(
            DownloadLink(
                url=url,
                title=_first_match(block, r"""data-title=["']([^"']+)["']""") or _html_text(block, "collection-item-title") or _title_from_url(url),
                source=source,
                sitem_id=_sitem_id_from_url(url),
                year=_year_from_block(block),
                artist=_clean_artist(_html_text(block, "collection-item-artist")),
                cache_ids=_unique_cache_ids(cache_id_from_sitem_id(_sitem_id_from_url(url))),
            )
        )
    if links:
        return links

    for raw_url in re.findall(r"""https?://[^"' <>)]+/download\?[^"' <>)]+|/download\?[^"' <>)]+""", text):
        url = urllib.parse.urljoin(source, raw_url)
        links.append(
            DownloadLink(
                url=url,
                title=_title_from_url(url),
                source=source,
                sitem_id=_sitem_id_from_url(url),
                cache_ids=_unique_cache_ids(cache_id_from_sitem_id(_sitem_id_from_url(url))),
            )
        )
    return links


def _collection_item_blocks(text: str) -> list[str]:
    starts = [match.start() for match in re.finditer(r"""<li\b[^>]*id=["']collection-item-container_[^"']+["']""", text)]
    return [text[start:end] for start, end in zip(starts, [*starts[1:], len(text)])]


def _collection_api_endpoints_from_text(text: str, source: str) -> list[str]:
    text = html.unescape(text).replace("\\/", "/")
    endpoints = []
    for raw_url in re.findall(r"""https?://[^"' <>)]+/api/fancollection/\d+/collection_items|/api/fancollection/\d+/collection_items""", text):
        endpoints.append(urllib.parse.urljoin(source, raw_url))
    return list(dict.fromkeys(endpoints))


def _download_links_from_json(data: object, source: str, download_format: str = DEFAULT_FORMAT) -> list[DownloadLink]:
    collection_links = _download_links_from_collection_json(data, source)
    if collection_links:
        return collection_links

    links: list[DownloadLink] = []
    for item in _walk(data):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("item_title") or item.get("album_title") or "bandcamp-download")
        artist = _first_present(item, "artist", "band_name", "band_title", "album_artist")
        year = _year_from_value(_first_present(item, "date", "release_date", "publish_date", "purchase_date", "token"))
        tracks = _tracks_from_item(item, {})
        for key, value in item.items():
            if isinstance(value, str) and "/download?" in value:
                url = urllib.parse.urljoin(source, value.replace("\\/", "/"))
                links.append(
                    DownloadLink(
                        url,
                        title,
                        source,
                        _sitem_id_from_url(url),
                        year,
                        artist,
                        _cache_ids_from_item(item, None, url),
                        tracks,
                        _cover_url_from_item(item),
                        _expected_track_count_from_item(item, tracks),
                        _is_preorder_item(item),
                        _release_date_from_item(item),
                    )
                )
            elif key in {"downloads", "download_urls"} and isinstance(value, dict):
                encoded = _find_download_url(value, download_format)
                if encoded:
                    url = urllib.parse.urljoin(source, encoded)
                    links.append(
                        DownloadLink(
                            url,
                            title,
                            source,
                            _sitem_id_from_url(url),
                            year,
                            artist,
                            _cache_ids_from_item(item, None, url),
                            tracks,
                            _cover_url_from_item(item),
                            _expected_track_count_from_item(item, tracks),
                            _is_preorder_item(item),
                            _release_date_from_item(item),
                        )
                    )
    return links


def _pagedata_from_text(text: str) -> dict:
    match = re.search(r"""<div\b[^>]*\bid=["']pagedata["'][^>]*\bdata-blob=(["'])(.*?)\1""", text, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(html.unescape(match.group(2)))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _collection_state_from_pagedata(data: dict) -> CollectionState | None:
    collection_data = data.get("collection_data")
    if not isinstance(collection_data, dict):
        return None
    sequence = collection_data.get("sequence")
    return CollectionState(
        item_count=_int_value(collection_data.get("item_count") or data.get("collection_count")),
        batch_size=_int_value(collection_data.get("batch_size")) or 20,
        last_token=collection_data.get("last_token") if isinstance(collection_data.get("last_token"), str) else None,
        initial_count=len(sequence) if isinstance(sequence, list) else 0,
    )


def _extract_fan_id_from_pagedata(data: dict) -> int | None:
    for section in (data.get("fan_data"), data.get("current_fan"), data):
        if isinstance(section, dict):
            fan_id = _int_value(section.get("fan_id"))
            if fan_id:
                return fan_id
    orderhistory = data.get("orderhistory")
    if isinstance(orderhistory, dict):
        fan_id = _int_value(orderhistory.get("fan_id"))
        if fan_id:
            return fan_id
    return None


def _download_links_from_pagedata(data: dict, source: str) -> list[DownloadLink]:
    links: list[DownloadLink] = []
    collection_data = data.get("collection_data")
    item_cache = data.get("item_cache")
    if isinstance(collection_data, dict) and isinstance(item_cache, dict):
        redownload_urls = collection_data.get("redownload_urls")
        collection_items = item_cache.get("collection")
        sequence = collection_data.get("sequence")
        tracklists = _collection_tracklists(data)
        if isinstance(redownload_urls, dict) and isinstance(collection_items, dict):
            ordered_items = []
            if isinstance(sequence, list):
                ordered_items = [collection_items[key] for key in sequence if key in collection_items]
            if not ordered_items:
                ordered_items = list(collection_items.values())
            links.extend(_links_from_collection_items(ordered_items, redownload_urls, source, tracklists))

    orderhistory = data.get("orderhistory")
    if isinstance(orderhistory, dict) and isinstance(orderhistory.get("items"), list):
        for item in orderhistory["items"]:
            if not isinstance(item, dict):
                continue
            raw_url = item.get("download_url")
            if not isinstance(raw_url, str) or "/download?" not in raw_url:
                continue
            links.append(_link_from_collection_item(item, raw_url, source, _item_sale_cache_id(item), {}))
    return links


def _download_links_from_collection_json(data: object, source: str) -> list[DownloadLink]:
    if not isinstance(data, dict):
        return []
    items = data.get("items")
    redownload_urls = data.get("redownload_urls")
    if not isinstance(items, list) or not isinstance(redownload_urls, dict):
        return []
    return _links_from_collection_items(items, redownload_urls, source, data.get("tracklists") if isinstance(data.get("tracklists"), dict) else {})


def _links_from_collection_items(items: Iterable[object], redownload_urls: dict, source: str, tracklists: dict | None = None) -> list[DownloadLink]:
    links: list[DownloadLink] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        cache_id = _item_sale_cache_id(item)
        raw_url = redownload_urls.get(cache_id) if cache_id else None
        if not isinstance(raw_url, str):
            raw_url = _first_download_url(redownload_urls, item)
        if not isinstance(raw_url, str):
            continue
        links.append(_link_from_collection_item(item, raw_url, source, cache_id, tracklists or {}))
    return links


def _extract_encoded_download(text: str, source: str, download_format: str) -> str | None:
    text = html.unescape(text).replace("\\/", "/")
    format_re = re.escape(download_format)
    for pattern in (
        rf'"{format_re}"\s*:\s*\{{[^{{}}]*"url"\s*:\s*"([^"]+)"',
        rf"""href=["']([^"']*(?:enc|format)={format_re}[^"']*)["']""",
        rf"""https?://[^"' <>)]+(?:enc|format)={format_re}[^"' <>)]+""",
    ):
        match = re.search(pattern, text)
        if match:
            return urllib.parse.urljoin(source, match.group(1))
    return None


def _extract_fan_id(text: str) -> int | None:
    text = html.unescape(text)
    for pattern in (r'"fan_id"\s*:\s*"?(\d+)"?', r"fan_id=(\d+)", r'"fanId"\s*:\s*"?(\d+)"?'):
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return None


def _find_value(data: object, key: str) -> str | None:
    for item in _walk(data):
        if isinstance(item, dict) and isinstance(item.get(key), str):
            return item[key]
    return None


def _find_download_url(data: dict, download_format: str) -> str | None:
    value = data.get(download_format) or data.get(DEFAULT_FORMAT) or data.get("mp3")
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("url"), str):
        return value["url"]
    return None


def _first_download_url(redownload_urls: dict, item: dict) -> str | None:
    sale_id = item.get("sale_item_id")
    if sale_id is not None:
        suffix = str(sale_id)
        for key, value in redownload_urls.items():
            if isinstance(key, str) and re.fullmatch(r"[a-z]" + re.escape(suffix), key) and isinstance(value, str):
                return value
    return None


def _item_sale_cache_id(item: dict) -> str | None:
    sale_type = item.get("sale_item_type")
    sale_id = item.get("sale_item_id")
    if isinstance(sale_type, str):
        return _legacy_cache_id(sale_type[:1], sale_id)
    return None


def _cache_ids_from_item(item: dict, primary: str | None, url: str) -> tuple[str, ...]:
    return _unique_cache_ids(primary, _item_sale_cache_id(item), cache_id_from_sitem_id(_sitem_id_from_url(url)))


def _link_from_collection_item(item: dict, raw_url: str, source: str, cache_id: str | None, tracklists: dict) -> DownloadLink:
    url = urllib.parse.urljoin(source, raw_url.replace("\\/", "/"))
    tracks = _tracks_from_item(item, tracklists)
    return DownloadLink(
        url=url,
        title=_title_from_item(item, url),
        source=source,
        sitem_id=_sitem_id_from_url(url),
        year=_year_from_item(item),
        artist=_artist_from_item(item),
        cache_ids=_cache_ids_from_item(item, cache_id, url),
        tracks=tracks,
        cover_url=_cover_url_from_item(item),
        expected_track_count=_expected_track_count_from_item(item, tracks),
        is_preorder=_is_preorder_item(item),
        release_date=_release_date_from_item(item),
    )


def _item_keys(item: dict) -> tuple[str, ...]:
    return _unique_cache_ids(
        _legacy_cache_id(str(item.get("item_type") or "")[:1], item.get("item_id")),
        _legacy_cache_id(item.get("tralbum_type"), item.get("tralbum_id")),
        _item_sale_cache_id(item),
    )


def _collection_tracklists(data: dict) -> dict:
    tracklists = data.get("tracklists")
    if not isinstance(tracklists, dict):
        return {}
    collection = tracklists.get("collection")
    return collection if isinstance(collection, dict) else tracklists


def _tracks_from_item(item: dict, tracklists: dict) -> tuple[TrackInfo, ...]:
    tracks = None
    for key in _item_keys(item):
        value = tracklists.get(key)
        if isinstance(value, list):
            tracks = value
            break
    if not tracks:
        return ()

    result: list[TrackInfo] = []
    for index, track in enumerate(tracks, 1):
        if not isinstance(track, dict):
            continue
        title = _first_present(track, "title", "track_title", "name")
        if not title:
            continue
        result.append(TrackInfo(title=title, number=_int_value(track.get("track_number") or track.get("track_num")) or index))
    return tuple(result)


def _expected_track_count_from_item(item: dict, tracks: tuple[TrackInfo, ...]) -> int | None:
    candidates = [
        _int_value(item.get("track_count")),
        _int_value(item.get("desc_track_count")),
        _int_value(item.get("num_streamable_tracks")),
        len(tracks),
    ]
    count = max(candidates)
    return count or None


def _is_preorder_item(item: dict) -> bool:
    for key in ("is_preorder", "preorder", "pre_order", "is_pre_order"):
        value = item.get(key)
        if value is True or str(value).strip().lower() in {"1", "true", "yes"}:
            return True
    return False


def _release_date_from_item(item: dict) -> str | None:
    for value in _walk(item):
        if isinstance(value, dict):
            found = _first_present(value, "release_date", "publish_date", "tralbum_release_date", "package_release_date", "sale_release_date")
            if found:
                return found
    return None


def _is_preorder_link(link: DownloadLink) -> bool:
    return link.is_preorder or _is_future_date(link.release_date)


def _is_metadata_incomplete(link: DownloadLink) -> bool:
    return bool(link.expected_track_count and link.tracks and len(link.tracks) < link.expected_track_count)


def _is_download_incomplete(link: DownloadLink, downloaded_tracks: int) -> bool:
    return bool(link.expected_track_count and downloaded_tracks < link.expected_track_count)


def _is_future_date(value: str | None) -> bool:
    parsed = _parse_date(value)
    return bool(parsed and parsed > date.today())


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        try:
            return date.fromisoformat(match.group(0))
        except ValueError:
            return None
    try:
        return parsedate_to_datetime(text).date()
    except (TypeError, ValueError, IndexError, AttributeError):
        return None


def _cover_url_from_item(item: dict) -> str | None:
    for key in ("item_art_url", "art_url", "image_url"):
        value = item.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    art_id = _int_value(item.get("art_id") or item.get("item_art_id"))
    return f"https://f4.bcbits.com/img/a{art_id}_10.jpg" if art_id else None


def _legacy_cache_id(prefix: str | None, value: object) -> str | None:
    if not prefix:
        return None
    digits = str(value).strip()
    prefix = prefix.strip().lower()
    return f"{prefix}{digits}" if re.fullmatch(r"[a-z]", prefix) and digits.isdigit() else None


def _unique_cache_ids(*cache_ids: str | None) -> tuple[str, ...]:
    result: list[str] = []
    for cache_id in cache_ids:
        if cache_id and cache_id not in result:
            result.append(cache_id)
    return tuple(result)


def _title_from_item(item: dict, url: str) -> str:
    return str(item.get("item_title") or item.get("title") or item.get("album_title") or item.get("item_title2") or _title_from_url(url))


def _artist_from_item(item: dict) -> str | None:
    return _first_present(item, "band_name", "artist", "artist_name", "band_title", "album_artist")


def _year_from_item(item: dict) -> str | None:
    return _year_from_value(
        _first_present(
            item,
            "purchased",
            "added",
            "date",
            "release_date",
            "publish_date",
            "purchase_date",
            "tralbum_release_date",
            "token",
        )
    )


def _collection_api_item_count(data: object) -> int:
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return len(data["items"])
    return 0


def _int_value(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _download_url_from_text(text: str, source: str) -> str | None:
    match = re.search(r"""href=["']([^"']*/download\?[^"']+)["']""", text)
    return urllib.parse.urljoin(source, match.group(1)) if match else None


def _sitem_id_from_url(url: str) -> str | None:
    parsed = urllib.parse.urlsplit(html.unescape(url))
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    sitem_id = query.get("sitem_id")
    return sitem_id if sitem_id and sitem_id.isdigit() else None


def _html_text(text: str, class_name: str) -> str | None:
    match = re.search(rf"""<div class=["'][^"']*\b{re.escape(class_name)}\b[^"']*["'][^>]*>([\s\S]*?)</div>""", text)
    if not match:
        return None
    return " ".join(re.sub(r"<[^>]+>", "", html.unescape(match.group(1))).split())


def _clean_artist(artist: str | None) -> str | None:
    if not artist:
        return None
    return artist[3:] if artist.lower().startswith("by ") else artist


def _year_from_block(text: str) -> str | None:
    token = _first_match(text, r"""data-token=["'](\d{10})""")
    return _year_from_value(token)


def _year_from_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    timestamp = re.match(r"\d{10}", text)
    if timestamp:
        return str(time.gmtime(int(timestamp.group(0))).tm_year)
    year = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    return year.group(1) if year else None


def _first_match(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text)
    return html.unescape(match.group(1)).strip() if match else None


def _first_present(data: dict, *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _walk(value: object):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _title_from_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    return query.get("title") or query.get("id") or "bandcamp-download"


def _release_folder(out_dir: Path, link: DownloadLink) -> Path:
    artist = sanitize_filename(link.artist or "Unknown Artist", default="Unknown Artist")
    title = sanitize_filename(link.title, default="Untitled")
    year = link.year if link.year and re.fullmatch(r"\d{4}", link.year) else str(time.gmtime().tm_year)
    return out_dir / artist / sanitize_filename(f"{year} - {title}", default=title)


def _expected_track_filenames(link: DownloadLink, download_format: str) -> list[str]:
    artist = link.artist or "Unknown Artist"
    album = link.title
    tracks = link.tracks or (TrackInfo(title=link.title, number=1),)
    numbers = [track.number or index for index, track in enumerate(tracks, 1)]
    width = max(2, len(str(max(numbers or [1]))))
    extension = extension_for_format(download_format)
    return [
        f"{artist} - {album} - {(track.number or index):0{width}d} {track.title}.{extension}"
        for index, track in enumerate(tracks, 1)
    ]


def _write_zip_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    with zf.open(info) as src, part.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    part.replace(target)


def _move_extracted_archive(extracted: ExtractedArchive, target_dir: Path) -> list[Path]:
    moved: list[Path] = []
    reserved: set[Path] = set()
    for path in (*extracted.audio, *extracted.covers):
        target = unique_path(target_dir, path.name, reserved)
        target.parent.mkdir(parents=True, exist_ok=True)
        path.replace(target)
        moved.append(target)
    return moved


def _dedupe_links(links: Iterable[DownloadLink]) -> list[DownloadLink]:
    seen: set[str] = set()
    result: list[DownloadLink] = []
    for link in links:
        keys = link.matching_cache_ids or (link.url,)
        if any(key in seen for key in keys):
            continue
        seen.update(keys)
        result.append(link)
    return result


def normalize_format(download_format: str) -> str:
    value = download_format.strip().lower()
    return FORMAT_ALIASES.get(value, value)


def extension_for_format(download_format: str) -> str:
    return FORMAT_EXTENSIONS.get(normalize_format(download_format), "zip")

from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .cache import LegacyCache, cache_id_from_sitem_id
from .filenames import unique_path
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


@dataclass(frozen=True)
class DownloadLink:
    url: str
    title: str
    source: str
    sitem_id: str | None = None
    year: str | None = None
    artist: str | None = None

    @property
    def cache_id(self) -> str | None:
        return cache_id_from_sitem_id(self.sitem_id or _sitem_id_from_url(self.url))

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


@dataclass
class DownloadPlan:
    pending: list[DownloadLink]
    skipped: list[DownloadLink]
    summary: DownloadSummary


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

        links = list(_download_links_from_text(response.text, response.final_url))
        fan_id = _extract_fan_id(response.text)
        self.log(f"fan_id found: {fan_id or 'no; API requests will omit fan_id'}")

        api_endpoints = list(dict.fromkeys([*_collection_api_endpoints_from_text(response.text, response.final_url), *(endpoints or [])]))
        if not api_endpoints:
            self.log("collection API endpoint found: no; pass --endpoint with the endpoint from browser devtools")
        for endpoint in api_endpoints:
            links.extend(self._fetch_collection_api(endpoint, fan_id, response.final_url))

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

    def plan_downloads(self, links: Iterable[DownloadLink], cache: LegacyCache | None = None) -> DownloadPlan:
        links = list(links)
        summary = DownloadSummary(collection_items=len(links))
        pending: list[DownloadLink] = []
        skipped: list[DownloadLink] = []
        seen: set[str] = set()
        for link in links:
            cache_id = link.cache_id
            if cache_id and cache_id in seen:
                summary.duplicates += 1
                self.log(f"Skipping duplicate: {cache_id}")
                continue
            if cache_id:
                seen.add(cache_id)
            if cache and cache.has(cache_id):
                self.log(f"Cache hit: {cache_id}")
                self.log(f"Skipping: {link.title}")
                summary.already_downloaded += 1
                skipped.append(link)
            else:
                pending.append(link)
        summary.new_downloads = len(pending)
        return DownloadPlan(pending=pending, skipped=skipped, summary=summary)

    def download_all(self, links: Iterable[DownloadLink], out_dir: Path, jobs: int, cache: LegacyCache | None = None) -> DownloadSummary:
        return self.download_plan(self.plan_downloads(links, cache), out_dir, jobs, cache)

    def download_plan(self, plan: DownloadPlan, out_dir: Path, jobs: int, cache: LegacyCache | None = None) -> DownloadSummary:
        summary = plan.summary
        if not plan.pending:
            return summary

        done = 0
        failed = 0
        reserved: set[Path] = set()
        tasks = [
            (link, unique_path(out_dir, f"{link.title}.{extension_for_format(self.download_format)}", reserved))
            for link in plan.pending
        ]
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            futures = [pool.submit(self._download_one, link, target, cache) for link, target in tasks]
            for future in as_completed(futures):
                if future.result():
                    done += 1
                else:
                    failed += 1
        summary.succeeded = done
        summary.failed = failed
        return summary

    def _download_one(self, link: DownloadLink, target: Path, cache: LegacyCache | None) -> bool:
        resolved = self.resolve_download(link)
        if not resolved:
            return False

        if target.exists() and target.stat().st_size > 0:
            self.log(f"skip existing: {target}")
            return True

        final_url, size, error = self.http.download(resolved.url, target, referer=resolved.source)
        if error or not target.exists() or target.stat().st_size == 0:
            self.log(f"download failed: {resolved.title}: {error}")
            return False
        self.log(f"downloaded: {target} ({size} bytes) from {final_url}")
        if cache:
            cache.append(link.cache_id, link.cache_description)
        return True

    def _fetch_html(self, url: str, *, referer: str | None = None) -> HttpResult:
        return self.http.request(url, headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}, referer=referer)

    def _fetch_collection_api(self, endpoint: str, fan_id: int | None, referer: str) -> list[DownloadLink]:
        links: list[DownloadLink] = []
        older_than_token: str | None = None
        seen_tokens: set[str] = set()
        for page in range(1, 50):
            payload = {"count": 100}
            if fan_id:
                payload["fan_id"] = fan_id
            if older_than_token:
                payload["older_than_token"] = older_than_token
            response = self.http.request(endpoint, method="POST", json_data=payload, referer=referer)
            self._log_response(f"api {endpoint} page {page}", response)
            if response.status != 200:
                break
            try:
                data = json.loads(response.text)
            except json.JSONDecodeError:
                self.log(f"api parse failed: {response.excerpt()}")
                break
            links.extend(_download_links_from_json(data, endpoint, self.download_format))
            older_than_token = _find_value(data, "last_token") or _find_value(data, "older_than_token")
            if not older_than_token or older_than_token in seen_tokens:
                break
            seen_tokens.add(older_than_token)
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
            )
        )
    if links:
        return links

    for raw_url in re.findall(r"""https?://[^"' <>)]+/download\?[^"' <>)]+|/download\?[^"' <>)]+""", text):
        url = urllib.parse.urljoin(source, raw_url)
        links.append(DownloadLink(url=url, title=_title_from_url(url), source=source, sitem_id=_sitem_id_from_url(url)))
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
    links: list[DownloadLink] = []
    for item in _walk(data):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("item_title") or item.get("album_title") or "bandcamp-download")
        artist = _first_present(item, "artist", "band_name", "band_title", "album_artist")
        year = _year_from_value(_first_present(item, "date", "release_date", "publish_date", "purchase_date", "token"))
        for key, value in item.items():
            if isinstance(value, str) and "/download?" in value:
                url = urllib.parse.urljoin(source, value.replace("\\/", "/"))
                links.append(DownloadLink(url, title, source, _sitem_id_from_url(url), year, artist))
            elif key in {"downloads", "download_urls"} and isinstance(value, dict):
                encoded = _find_download_url(value, download_format)
                if encoded:
                    url = urllib.parse.urljoin(source, encoded)
                    links.append(DownloadLink(url, title, source, _sitem_id_from_url(url), year, artist))
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
        return str(time.gmtime(int(text)).tm_year)
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


def _dedupe_links(links: Iterable[DownloadLink]) -> list[DownloadLink]:
    seen: set[str] = set()
    result: list[DownloadLink] = []
    for link in links:
        key = link.cache_id or link.url
        if key not in seen:
            seen.add(key)
            result.append(link)
    return result


def normalize_format(download_format: str) -> str:
    value = download_format.strip().lower()
    return FORMAT_ALIASES.get(value, value)


def extension_for_format(download_format: str) -> str:
    return FORMAT_EXTENSIONS.get(normalize_format(download_format), "zip")

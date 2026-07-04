from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .bandcamp import BandcampClient, DownloadSummary
from .cache import LegacyCache
from .cookies import discover_firefox_profiles, load_firefox_cookies
from .http_client import BrowserHttp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download Bandcamp purchases with Firefox cookies.")
    parser.add_argument("--profile", type=Path, help="Firefox profile directory, including Flatpak/portal profile paths.")
    parser.add_argument("--cookies-sqlite", type=Path, help="Path to cookies.sqlite.")
    parser.add_argument("--allow-weak-cookies", action="store_true", help="Allow cookies without identity/js_logged_in.")
    parser.add_argument("--list-profiles", action="store_true", help="List discovered Firefox profiles and exit.")
    parser.add_argument("fan", nargs="?", help="Optional fan username to probe instead of /your/purchases.")
    parser.add_argument("--username", help="Fan username to probe instead of /your/purchases.")
    parser.add_argument("--endpoint", action="append", default=[], help="Extra Bandcamp API endpoint to POST to.")
    parser.add_argument("--download-folder", "--out", dest="download_folder", type=Path, default=Path("Bandcamp"), help="Download directory.")
    parser.add_argument("--jobs", type=int, default=4, help="Parallel download jobs.")
    parser.add_argument("-f", "--format", default="mp3-320", help="Download format, e.g. mp3-320, flac, aac-hi, vorbis.")
    parser.add_argument("--include-preorders", action="store_true", help="Attempt preorders/incomplete releases instead of skipping them up front.")
    parser.add_argument("--dry-run", action="store_true", help="Authenticate and list discovered downloads without downloading.")
    parser.add_argument("--verbose", action="store_true", help="Print profile, cookie, HTTP, and API diagnostics.")
    args = parser.parse_args(argv)

    if args.list_profiles:
        return _list_profiles()

    def log(message: str) -> None:
        if args.verbose:
            print(message, file=sys.stderr)

    started = time.monotonic()
    args.download_folder.mkdir(parents=True, exist_ok=True)
    cache = LegacyCache(args.download_folder, log)
    cache.load()

    try:
        cookies = load_firefox_cookies(
            profile=args.profile,
            cookies_sqlite=args.cookies_sqlite,
            allow_weak_cookies=args.allow_weak_cookies,
            log=log,
        )
    except Exception as exc:
        print(f"cookie load failed: {exc}", file=sys.stderr)
        return 2

    log(f"cookie source: {cookies.source_db}")
    log(f"cookie temp copy: {cookies.copied_db}")
    log(f"cookies loaded: {cookies.loaded_count} names={','.join(cookies.loaded_names) or '(none)'}")
    log(f"identity cookie: {'yes' if cookies.has_identity else 'no'}")
    log(f"js_logged_in cookie: {'yes' if cookies.has_js_logged_in else 'no'}")
    if cookies.skipped:
        log("cookies skipped: " + ", ".join(f"{name}={count}" for name, count in sorted(cookies.skipped.items())))

    client = BandcampClient(BrowserHttp(cookies.jar), log, args.format)
    links = client.find_downloads(endpoints=args.endpoint, username=args.username or args.fan)
    if not links:
        print("no Bandcamp downloads found; rerun with --verbose and send the diagnostics from this machine", file=sys.stderr)
        return 1

    plan = client.plan_downloads(links, cache, include_preorders=args.include_preorders)
    for link in plan.preorders:
        _print_preorder(link)
    for link in plan.incomplete:
        _print_incomplete(link)
    if args.dry_run:
        for link in plan.skipped:
            _print_dry_run_item("SKIP", link, client, args.download_folder)
        for link in plan.pending:
            _print_dry_run_item("DOWNLOAD", link, client, args.download_folder)
        _print_summary(plan.summary, time.monotonic() - started)
        return 0

    summary = client.download_plan(plan, args.download_folder, args.jobs, cache)
    _print_summary(summary, time.monotonic() - started)
    return 0 if summary.failed == 0 and (
        summary.succeeded
        or summary.already_downloaded
        or summary.duplicates
        or summary.preorders_skipped
        or summary.incomplete_skipped
    ) else 1


def _list_profiles() -> int:
    profiles = discover_firefox_profiles()
    if not profiles:
        print("No Firefox profiles with cookies.sqlite found.")
        return 1
    for profile in profiles:
        marker = " default" if profile.is_default else ""
        print(f"{profile.kind}{marker}\t{profile.path}")
    return 0


def _print_summary(summary: DownloadSummary, elapsed: float) -> None:
    print(f"Collection items: {summary.collection_items}", file=sys.stderr)
    print(f"Already downloaded: {summary.already_downloaded}", file=sys.stderr)
    print(f"New downloads: {summary.new_downloads}", file=sys.stderr)
    print(f"Succeeded: {summary.succeeded}", file=sys.stderr)
    print(f"Failed: {summary.failed}", file=sys.stderr)
    print(f"Preorders skipped: {summary.preorders_skipped}", file=sys.stderr)
    print(f"Incomplete skipped: {summary.incomplete_skipped}", file=sys.stderr)
    print(f"Elapsed: {_format_elapsed(elapsed)}", file=sys.stderr)


def _release_name(link) -> str:
    return f"{link.artist or 'Unknown Artist'} - {link.title}"


def _print_preorder(link) -> None:
    print(f"PREORDER  {link.cache_id or '-'}  {_release_name(link)}  release_date={link.release_date or '-'}")


def _print_incomplete(link) -> None:
    print(
        f"INCOMPLETE  {link.cache_id or '-'}  {_release_name(link)}  "
        f"expected={link.expected_track_count or 0} available={len(link.tracks)}"
    )


def _print_dry_run_item(status: str, link, client: BandcampClient, download_folder: Path) -> None:
    layout = client.target_layout(download_folder, link)
    print(f"{status}  {link.cache_id or '-'}  {link.title}")
    print(f"  FOLDER  {layout.folder}")
    for path in layout.files:
        print(f"  FILE  {path}")


def _format_elapsed(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{secs:02}"

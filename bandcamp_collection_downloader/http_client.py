from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path


DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_DELAYS = (1, 2, 4)


@dataclass
class HttpResult:
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    body: bytes
    error: str | None = None

    @property
    def text(self) -> str:
        charset = "utf-8"
        content_type = self.headers.get("Content-Type") or self.headers.get("content-type") or ""
        match = re.search(r"charset=([^;\s]+)", content_type)
        if match:
            charset = match.group(1)
        return self.body.decode(charset, errors="replace")

    def excerpt(self, limit: int = 800) -> str:
        return " ".join(self.text[:limit].split())


class BrowserHttp:
    def __init__(self, cookiejar: CookieJar):
        self.cookiejar = cookiejar
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookiejar))

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        json_data: object | None = None,
        referer: str | None = None,
        max_bytes: int | None = None,
    ) -> HttpResult:
        request_headers = dict(DEFAULT_HEADERS)
        if headers:
            request_headers.update(headers)
        if referer:
            request_headers["Referer"] = referer
        if json_data is not None:
            data = json.dumps(json_data, separators=(",", ":")).encode()
            request_headers["Content-Type"] = "application/json"
            request_headers["Accept"] = "application/json, text/javascript, */*; q=0.01"

        for attempt in range(len(RETRY_DELAYS) + 1):
            req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
            try:
                with self.opener.open(req, timeout=60) as response:
                    return HttpResult(
                        url=url,
                        final_url=response.geturl(),
                        status=response.getcode(),
                        headers=dict(response.headers.items()),
                        body=response.read(max_bytes) if max_bytes is not None else response.read(),
                    )
            except urllib.error.HTTPError as exc:
                if exc.code in RETRY_STATUSES and attempt < len(RETRY_DELAYS):
                    _sleep(attempt)
                    continue
                return HttpResult(
                    url=url,
                    final_url=exc.geturl(),
                    status=exc.code,
                    headers=dict(exc.headers.items()),
                    body=exc.read(max_bytes) if max_bytes is not None else exc.read(),
                    error=str(exc),
                )
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                if attempt < len(RETRY_DELAYS):
                    _sleep(attempt)
                    continue
                return HttpResult(url=url, final_url=url, status=0, headers={}, body=b"", error=str(exc))
        return HttpResult(url=url, final_url=url, status=0, headers={}, body=b"", error="retry loop exhausted")

    def download(self, url: str, destination: Path, *, referer: str | None = None) -> tuple[str, int, str | None]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(len(RETRY_DELAYS) + 1):
            part = destination.with_suffix(destination.suffix + ".part")
            existing = part.stat().st_size if part.exists() else 0
            headers = dict(DEFAULT_HEADERS)
            headers["Accept"] = "*/*"
            if referer:
                headers["Referer"] = referer
            if existing:
                headers["Range"] = f"bytes={existing}-"

            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with self.opener.open(req, timeout=120) as response:
                    content_type = response.headers.get("Content-Type", "")
                    if "text/html" in content_type:
                        body = response.read(800).decode("utf-8", errors="replace")
                        return response.geturl(), 0, f"unexpected HTML response: {' '.join(body.split())}"
                    mode = "ab" if existing and response.getcode() == 206 else "wb"
                    total = existing if mode == "ab" else 0
                    with part.open(mode) as fh:
                        while True:
                            chunk = response.read(1024 * 256)
                            if not chunk:
                                break
                            fh.write(chunk)
                            total += len(chunk)
                    part.replace(destination)
                    return response.geturl(), total, None
            except urllib.error.HTTPError as exc:
                if exc.code in RETRY_STATUSES and attempt < len(RETRY_DELAYS):
                    _sleep(attempt)
                    continue
                body = exc.read(800).decode("utf-8", errors="replace")
                return url, 0, f"HTTP {exc.code}: {' '.join(body.split())}"
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                if attempt < len(RETRY_DELAYS):
                    _sleep(attempt)
                    continue
                return url, 0, str(exc)
        return url, 0, "retry loop exhausted"


def add_query(url: str, **params: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))


def _sleep(attempt: int) -> None:
    time.sleep(RETRY_DELAYS[attempt])

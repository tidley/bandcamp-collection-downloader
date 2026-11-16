from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Callable

from .http_client import BrowserHttp, HttpResult


class BrowserBackendUnavailable(RuntimeError):
    pass


class BrowserVerificationRequired(RuntimeError):
    pass


class BrowserSessionHttp:
    """HTTP transport backed by a disposable, interactive Playwright Firefox profile."""

    supports_parallel_downloads = False

    def __init__(
        self,
        cookiejar: CookieJar,
        *,
        headless: bool = False,
        notify: Callable[[str], None] | None = None,
        input_func: Callable[[str], str] = input,
    ):
        self._notify = notify or (lambda _message: None)
        self._input = input_func
        self._profile_dir = Path(tempfile.mkdtemp(prefix="bandcamp-playwright-firefox-"))
        try:
            self._playwright = _sync_playwright().start()
            self._context = self._playwright.firefox.launch_persistent_context(str(self._profile_dir), headless=headless)
            cookies = _playwright_cookies(cookiejar)
            if cookies:
                self._context.add_cookies(cookies)
            self._page = self._context.new_page()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        context = getattr(self, "_context", None)
        if context is not None:
            self._context = None
            context.close()
        playwright = getattr(self, "_playwright", None)
        if playwright is not None:
            self._playwright = None
            playwright.stop()
        profile_dir = getattr(self, "_profile_dir", None)
        if profile_dir:
            shutil.rmtree(profile_dir, ignore_errors=True)

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
        del data, referer, max_bytes
        if method == "GET" and json_data is None:
            response = self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            body = self._page.content().encode()
            result = HttpResult(
                url=url,
                final_url=self._page.url,
                status=response.status if response else 0,
                headers=response.all_headers() if response else {},
                body=body,
            )
            return self._verify_if_needed(result)

        request_headers = dict(headers or {})
        payload = json.dumps(json_data, separators=(",", ":")) if json_data is not None else None
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
            request_headers.setdefault("Accept", "application/json, text/javascript, */*; q=0.01")
        result = self._page.evaluate(
            """async ({url, method, headers, payload}) => {
                const response = await fetch(url, {method, headers, body: payload, credentials: 'include'});
                return {
                    status: response.status,
                    url: response.url,
                    headers: Object.fromEntries(response.headers.entries()),
                    body: await response.text(),
                };
            }""",
            {"url": url, "method": method, "headers": request_headers, "payload": payload},
        )
        return HttpResult(
            url=url,
            final_url=result["url"],
            status=result["status"],
            headers=result["headers"],
            body=result["body"].encode(),
        )

    def download(self, url: str, destination: Path, *, referer: str | None = None) -> tuple[str, int, str | None]:
        # Playwright's APIRequestContext buffers the entire response and applies
        # one timeout to the whole transfer. Stream signed file URLs instead,
        # preserving the browser's current cookies and resumable .part files.
        jar = CookieJar()
        for item in self._context.cookies():
            expires = item.get("expires", -1)
            jar.set_cookie(Cookie(
                version=0, name=item["name"], value=item["value"],
                port=None, port_specified=False,
                domain=item["domain"], domain_specified=item["domain"].startswith("."),
                domain_initial_dot=item["domain"].startswith("."),
                path=item.get("path") or "/", path_specified=True,
                secure=item.get("secure", False),
                expires=expires if expires > 0 else None, discard=expires <= 0,
                comment=None, comment_url=None,
                rest={"HttpOnly": None} if item.get("httpOnly") else {}, rfc2109=False,
            ))
        self._notify(f"Downloading: {destination.parent.name} / {destination.name}")

        def progress(received: int, total: int | None) -> None:
            amount = f"{received / (1024 * 1024):.1f} MiB"
            if total is not None:
                amount += f" / {total / (1024 * 1024):.1f} MiB"
            self._notify(f"  Transfer: {amount}")

        result = BrowserHttp(jar).download(url, destination, referer=referer, progress=progress)
        if result[2]:
            self._notify(f"Download failed: {destination.parent.name}: {result[2]}")
        else:
            self._notify(f"Saved: {destination}")
        return result

    def _verify_if_needed(self, result: HttpResult) -> HttpResult:
        if not _is_client_challenge(result.text):
            return result
        self._notify(
            "browser verification required: complete Bandcamp's Client Challenge in the opened Firefox window, "
            "then press Enter here to retry."
        )
        if not sys.stdin.isatty():
            raise BrowserVerificationRequired(
                "Bandcamp requires interactive browser verification, but standard input is not a terminal. "
                "Run --backend browser from an interactive terminal."
            )
        self._input("Press Enter after completing the verification in Firefox: ")
        response = self._page.reload(wait_until="domcontentloaded", timeout=60_000)
        retried = HttpResult(
            url=result.url,
            final_url=self._page.url,
            status=response.status if response else 0,
            headers=response.all_headers() if response else {},
            body=self._page.content().encode(),
        )
        if _is_client_challenge(retried.text):
            raise BrowserVerificationRequired(
                "Bandcamp still returned Client Challenge after the interactive retry; complete verification in Firefox and run again."
            )
        return retried


def _sync_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserBackendUnavailable(
            "browser backend requires Playwright; install it with `pip install '.[browser]'` and run `playwright install firefox`"
        ) from exc
    return sync_playwright()


def _playwright_cookies(cookiejar: CookieJar) -> list[dict]:
    cookies = []
    for cookie in cookiejar:
        item = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path or "/",
            "secure": cookie.secure,
            "httpOnly": cookie.has_nonstandard_attr("HttpOnly"),
            "expires": cookie.expires if cookie.expires is not None else -1,
        }
        cookies.append(item)
    return cookies


def _is_client_challenge(text: str) -> bool:
    return bool(re.search(r"<title[^>]*>\s*client\s+challenge\s*</title>", text, re.IGNORECASE))

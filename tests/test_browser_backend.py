import html
import json
import threading
import unittest
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from unittest import mock

from bandcamp_collection_downloader.bandcamp import BandcampClient, DownloadPlan, DownloadSummary
from bandcamp_collection_downloader.browser_backend import BrowserSessionHttp


class BrowserBackendIntegrationTests(unittest.TestCase):
    def test_file_transfer_streams_with_current_browser_cookies(self):
        browser = FakePlaywright(_collection_page())
        notify = mock.Mock()
        with mock.patch("bandcamp_collection_downloader.browser_backend._sync_playwright", return_value=browser):
            http = BrowserSessionHttp(CookieJar(), headless=True, notify=notify)
            # Include cookies created after browser startup, not just imported ones.
            browser.context.cookies = mock.Mock(return_value=[{
                "name": "identity", "value": "refreshed-session", "domain": ".bandcamp.com",
                "path": "/", "expires": -1, "secure": True, "httpOnly": True,
            }])
            try:
                with mock.patch("bandcamp_collection_downloader.browser_backend.BrowserHttp") as transport:
                    target = Path("release/archive.zip")
                    transport.return_value.download.return_value = ("https://files.test/archive", 123, None)
                    result = http.download("https://files.test/archive", target, referer="https://bandcamp.com/download")

                cookie = list(transport.call_args.args[0])[0]
                self.assertEqual(cookie.value, "refreshed-session")
                self.assertIsNone(cookie.expires)
                self.assertTrue(cookie.has_nonstandard_attr("HttpOnly"))
                call = transport.return_value.download.call_args
                self.assertEqual(call.args, ("https://files.test/archive", target))
                self.assertEqual(call.kwargs["referer"], "https://bandcamp.com/download")
                call.kwargs["progress"](1048576, 2097152)
                notify.assert_called_with("  Transfer: 1.0 MiB / 2.0 MiB")
                self.assertEqual(result[1:], (123, None))
            finally:
                http.close()

    def test_download_plan_keeps_browser_calls_on_owning_thread(self):
        browser = FakePlaywright(_collection_page())
        owner = threading.get_ident()
        with mock.patch("bandcamp_collection_downloader.browser_backend._sync_playwright", return_value=browser):
            http = BrowserSessionHttp(CookieJar(), headless=True)
            try:
                client = BandcampClient(http, lambda _: None)
                statuses = iter(["success", "failed", "incomplete", "preorder"])

                def download(link, out_dir, cache):
                    self.assertEqual(threading.get_ident(), owner)
                    http.request("https://bandcamp.com/example-user")
                    return next(statuses)

                plan = DownloadPlan([mock.sentinel.link] * 4, [], [], [], DownloadSummary(new_downloads=4))
                with mock.patch.object(client, "_download_one", side_effect=download):
                    summary = client.download_plan(plan, Path("unused"), jobs=4)
            finally:
                http.close()

        self.assertEqual(len(browser.context.page.gotos), 4)
        self.assertEqual(summary.succeeded, 1)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.incomplete_skipped, 1)
        self.assertEqual(summary.preorders_skipped, 1)

    def test_browser_session_discovers_bootstrap_collection_and_uses_browser_fetch(self):
        browser = FakePlaywright(_collection_page())
        jar = CookieJar()
        jar.set_cookie(_cookie("identity", "authenticated"))

        with mock.patch("bandcamp_collection_downloader.browser_backend._sync_playwright", return_value=browser):
            http = BrowserSessionHttp(jar, headless=True)
            try:
                links = BandcampClient(http, lambda _: None).find_downloads(username="example-user")
            finally:
                http.close()

        self.assertEqual([link.cache_id for link in links], ["p111"])
        self.assertEqual(browser.context.cookies[0]["name"], "identity")
        self.assertTrue(browser.context.cookies[0]["httpOnly"])
        self.assertEqual(browser.context.page.gotos, ["https://bandcamp.com/example-user"])
        self.assertEqual(browser.context.page.fetches[0]["method"], "POST")
        self.assertTrue(browser.context.closed)
        self.assertTrue(browser.stopped)


class FakePlaywright:
    def __init__(self, page_html):
        self.firefox = FakeFirefox(page_html)
        self.context = self.firefox.context
        self.stopped = False

    def start(self):
        return self

    def stop(self):
        self.stopped = True


class FakeFirefox:
    def __init__(self, page_html):
        self.context = FakeContext(page_html)

    def launch_persistent_context(self, _profile_dir, *, headless):
        self.headless = headless
        return self.context


class FakeContext:
    def __init__(self, page_html):
        self.page = FakePage(page_html)
        self.cookies = []
        self.closed = False

    def add_cookies(self, cookies):
        self.cookies.extend(cookies)

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


class FakePage:
    def __init__(self, page_html):
        self.page_html = page_html
        self.url = "about:blank"
        self.gotos = []
        self.fetches = []

    def goto(self, url, **_kwargs):
        self.url = url
        self.gotos.append(url)
        return FakeResponse(200, url)

    def content(self):
        return self.page_html

    def evaluate(self, _script, payload):
        self.fetches.append(payload)
        return {
            "status": 200,
            "url": payload["url"],
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"items": [], "redownload_urls": {}, "more_available": False}),
        }


class FakeResponse:
    def __init__(self, status, url):
        self.status = status
        self.url = url

    def all_headers(self):
        return {"content-type": "text/html"}


def _collection_page():
    blob = {
        "fan_data": {"fan_id": 123},
        "collection_data": {
            "item_count": 1,
            "batch_size": 20,
            "sequence": ["p10"],
            "redownload_urls": {"p111": "/download?sitem_id=111"},
        },
        "item_cache": {
            "collection": {
                "p10": {"item_title": "Browser Album", "sale_item_type": "p", "sale_item_id": 111}
            }
        },
    }
    return f'<div id="pagedata" data-blob="{html.escape(json.dumps(blob), quote=True)}"></div>'


def _cookie(name, value):
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=".bandcamp.com",
        domain_specified=True,
        domain_initial_dot=True,
        path="/",
        path_specified=True,
        secure=True,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": None},
        rfc2109=False,
    )

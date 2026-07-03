import urllib.error
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bandcamp_collection_downloader.http_client import BrowserHttp


class HttpClientTests(unittest.TestCase):
    def test_retries_retryable_http_status(self):
        client = BrowserHttp(None)
        client.opener = FakeOpener([_http_error(500), FakeResponse(200, b"ok")])

        with mock.patch("bandcamp_collection_downloader.http_client.time.sleep"):
            result = client.request("https://example.test")

        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, b"ok")
        self.assertEqual(client.opener.calls, 2)

    def test_download_resumes_part_file_with_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "file.mp3"
            target.with_suffix(".mp3.part").write_bytes(b"old")
            client = BrowserHttp(None)
            client.opener = FakeOpener([FakeResponse(206, b"new")])

            final_url, size, error = client.download("https://example.test/file", target)

            self.assertIsNone(error)
            self.assertEqual(size, 6)
            self.assertEqual(final_url, "https://example.test")
            self.assertEqual(target.read_bytes(), b"oldnew")
            self.assertEqual(client.opener.ranges, ["bytes=3-"])


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.ranges = []

    def open(self, request, timeout):
        self.calls += 1
        self.ranges.append(request.get_header("Range"))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body
        self.offset = 0
        self.headers = {"Content-Type": "application/octet-stream"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return "https://example.test"

    def getcode(self):
        return self.status

    def read(self, size=-1):
        if self.offset >= len(self.body):
            return b""
        if size is None or size < 0:
            size = len(self.body) - self.offset
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


def _http_error(status):
    return urllib.error.HTTPError("https://example.test", status, "error", {}, None)


if __name__ == "__main__":
    unittest.main()

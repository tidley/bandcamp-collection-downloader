import tempfile
import unittest
from pathlib import Path

from bandcamp_collection_downloader.bandcamp import BandcampClient, DownloadLink
from bandcamp_collection_downloader.cache import CACHE_FILENAME, LegacyCache, cache_id_from_sitem_id


class CacheTests(unittest.TestCase):
    def test_loads_and_appends_legacy_cache_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_file = root / CACHE_FILENAME
            cache_file.write_text('p375739204| "Tekno Sucks 170" (2026) by Myor\n', encoding="utf-8")
            cache = LegacyCache(root, lambda _: None)

            cache.load()
            cache.append("p382181582", '"Title" (2026) by Artist')

            self.assertTrue(cache.has("p375739204"))
            self.assertEqual(
                cache_file.read_text(encoding="utf-8"),
                'p375739204| "Tekno Sucks 170" (2026) by Myor\np382181582| "Title" (2026) by Artist\n',
            )

    def test_normalizes_sitem_id(self):
        self.assertEqual(cache_id_from_sitem_id("375739204"), "p375739204")
        self.assertIsNone(cache_id_from_sitem_id("x375739204"))

    def test_cache_hit_skips_before_resolving_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / CACHE_FILENAME).write_text('p375739204| "Tekno Sucks 170" (2026) by Myor\n', encoding="utf-8")
            cache = LegacyCache(root, lambda _: None)
            cache.load()
            client = NoResolveClient(None, lambda _: None)

            summary = client.download_all(
                [DownloadLink("https://bandcamp.com/download?sitem_id=375739204", "Tekno Sucks 170", "src")],
                root,
                1,
                cache,
            )

            self.assertEqual(summary.already_downloaded, 1)
            self.assertEqual(summary.new_downloads, 0)

    def test_cache_updates_after_successful_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = LegacyCache(root, lambda _: None)
            cache.load()
            client = ResolvedClient(FakeHttp(), lambda _: None)
            link = DownloadLink("https://bandcamp.com/download?sitem_id=382181582", "Title", "src", "382181582", "2026", "Artist")

            summary = client.download_all([link], root, 1, cache)

            self.assertEqual(summary.succeeded, 1)
            self.assertIn('p382181582| "Title" (2026) by Artist\n', (root / CACHE_FILENAME).read_text(encoding="utf-8"))
            self.assertTrue((root / "Title.mp3").exists())

    def test_duplicate_cache_ids_are_not_downloaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = LegacyCache(root, lambda _: None)
            cache.load()
            client = ResolvedClient(FakeHttp(), lambda _: None)

            summary = client.download_all(
                [
                    DownloadLink("https://bandcamp.com/download?sitem_id=382181582", "Title", "src"),
                    DownloadLink("https://bandcamp.com/download?sitem_id=382181582", "Title duplicate", "src"),
                ],
                root,
                1,
                cache,
            )

            self.assertEqual(summary.new_downloads, 1)
            self.assertEqual(summary.duplicates, 1)
            self.assertEqual(summary.succeeded, 1)


class NoResolveClient(BandcampClient):
    def resolve_download(self, link):
        raise AssertionError("cache hit should skip before resolving")


class ResolvedClient(BandcampClient):
    def resolve_download(self, link):
        return link


class FakeHttp:
    def download(self, url, destination, *, referer=None):
        destination.write_bytes(b"mp3")
        return url, 3, None


if __name__ == "__main__":
    unittest.main()

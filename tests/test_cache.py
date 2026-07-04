import tempfile
import unittest
import zipfile
from pathlib import Path

from bandcamp_collection_downloader.bandcamp import BandcampClient, DownloadLink, TrackInfo
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
            (root / CACHE_FILENAME).write_text('r375739204| "Tekno Sucks 170" (2026) by Myor\n', encoding="utf-8")
            cache = LegacyCache(root, lambda _: None)
            cache.load()
            client = NoResolveClient(None, lambda _: None)

            summary = client.download_all(
                [
                    DownloadLink(
                        "https://bandcamp.com/download?sitem_id=375739204",
                        "Tekno Sucks 170",
                        "src",
                        cache_ids=("r375739204", "p375739204"),
                    )
                ],
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
            self.assertFalse((root / "Title.mp3").exists())
            self.assertTrue((root / "Artist" / "2026 - Title" / "Artist - Title - 01 Title.mp3").exists())

    def test_zip_download_extracts_tracks_and_cover_to_release_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = LegacyCache(root, lambda _: None)
            cache.load()
            client = ResolvedClient(ZipHttp(), lambda _: None)
            link = DownloadLink("https://bandcamp.com/download?sitem_id=382181582", "Album", "src", "382181582", "2026", "Artist")

            summary = client.download_all([link], root, 1, cache)

            folder = root / "Artist" / "2026 - Album"
            self.assertEqual(summary.succeeded, 1)
            self.assertTrue((folder / "Artist - Album - 01 Track.mp3").exists())
            self.assertTrue((folder / "cover.jpg").exists())
            self.assertFalse(list(root.glob("*.mp3")))

    def test_incomplete_zip_does_not_update_cache_or_leave_tracks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = LegacyCache(root, lambda _: None)
            cache.load()
            client = ResolvedClient(ZipHttp(), lambda _: None)
            link = DownloadLink(
                "https://bandcamp.com/download?sitem_id=382181582",
                "Album",
                "src",
                "382181582",
                "2026",
                "Artist",
                tracks=(TrackInfo("Track 1", 1), TrackInfo("Track 2", 2)),
                expected_track_count=2,
            )

            summary = client.download_all([link], root, 1, cache, include_preorders=True)

            self.assertEqual(summary.succeeded, 0)
            self.assertEqual(summary.incomplete_skipped, 1)
            self.assertFalse((root / CACHE_FILENAME).exists())
            self.assertFalse(list((root / "Artist" / "2026 - Album").glob("*.mp3")))

    def test_multi_track_metadata_rejects_single_mp3_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = LegacyCache(root, lambda _: None)
            cache.load()
            client = ResolvedClient(FakeHttp(), lambda _: None)
            link = DownloadLink(
                "https://bandcamp.com/download?sitem_id=382181582",
                "Album",
                "src",
                "382181582",
                "2026",
                "Artist",
                tracks=(TrackInfo("Track 1", 1), TrackInfo("Track 2", 2)),
                expected_track_count=2,
            )

            summary = client.download_all([link], root, 1, cache, include_preorders=True)

            self.assertEqual(summary.succeeded, 0)
            self.assertEqual(summary.incomplete_skipped, 1)
            self.assertFalse((root / CACHE_FILENAME).exists())
            self.assertFalse(list((root / "Artist" / "2026 - Album").glob("*.mp3")))

    def test_include_preorders_downloads_but_does_not_cache_or_succeed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = LegacyCache(root, lambda _: None)
            cache.load()
            client = ResolvedClient(FakeHttp(), lambda _: None)
            link = DownloadLink(
                "https://bandcamp.com/download?sitem_id=382181582",
                "Preorder",
                "src",
                "382181582",
                "2999",
                "Artist",
                is_preorder=True,
            )

            summary = client.download_all([link], root, 1, cache, include_preorders=True)

            self.assertEqual(summary.succeeded, 0)
            self.assertEqual(summary.preorders_skipped, 1)
            self.assertFalse((root / CACHE_FILENAME).exists())

    def test_cache_updates_with_primary_legacy_r_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = LegacyCache(root, lambda _: None)
            cache.load()
            client = ResolvedClient(FakeHttp(), lambda _: None)
            link = DownloadLink(
                "https://bandcamp.com/download?sitem_id=356511620",
                "Legacy Title",
                "src",
                "356511620",
                "2026",
                "Artist",
                ("r356511620", "p356511620"),
            )

            summary = client.download_all([link], root, 1, cache)

            self.assertEqual(summary.succeeded, 1)
            self.assertIn('r356511620| "Legacy Title" (2026) by Artist\n', (root / CACHE_FILENAME).read_text(encoding="utf-8"))

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


class ZipHttp:
    def download(self, url, destination, *, referer=None):
        with zipfile.ZipFile(destination, "w") as zf:
            zf.writestr("Artist - Album - 01 Track.mp3", b"mp3")
            zf.writestr("cover.jpg", b"jpg")
        return url, destination.stat().st_size, None


if __name__ == "__main__":
    unittest.main()

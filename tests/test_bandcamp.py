import tempfile
import unittest
from pathlib import Path

from bandcamp_collection_downloader.bandcamp import (
    extension_for_format,
    normalize_format,
    _collection_api_endpoints_from_text,
    _download_links_from_text,
    _extract_fan_id,
)
from bandcamp_collection_downloader.filenames import sanitize_filename, unique_path


class BandcampTests(unittest.TestCase):
    def test_discovers_collection_api_endpoint_from_page(self):
        endpoints = _collection_api_endpoints_from_text(
            r'{"url":"\/api\/fancollection\/3\/collection_items"} https://bandcamp.com/api/fancollection/4/collection_items',
            "https://bandcamp.com/your/purchases",
        )

        self.assertEqual(
            endpoints,
            [
                "https://bandcamp.com/api/fancollection/3/collection_items",
                "https://bandcamp.com/api/fancollection/4/collection_items",
            ],
        )

    def test_extracts_optional_fan_id(self):
        self.assertEqual(_extract_fan_id('{"fanId":"12345"}'), 12345)
        self.assertEqual(_extract_fan_id('page-context="{&quot;fanId&quot;:12345}"'), 12345)
        self.assertIsNone(_extract_fan_id("{}"))

    def test_extracts_legacy_cache_metadata_from_collection_item(self):
        links = _download_links_from_text(
            """
            <li id="collection-item-container_2872214478" class="collection-item-container" data-title="Tekno Sucks 170" data-token="1773338149:2872214478:p::">
                <ul><li>nested gallery item</li></ul>
                <div class="collection-item-title">Tekno Sucks 170</div>
                <div class="collection-item-artist">by Myor</div>
                <a href="https://bandcamp.com/download?from=collection&amp;sitem_id=375739204">download</a>
            </li>
            """,
            "https://bandcamp.com/example-user",
        )

        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].cache_id, "p375739204")
        self.assertEqual(links[0].title, "Tekno Sucks 170")
        self.assertEqual(links[0].year, "2026")
        self.assertEqual(links[0].artist, "Myor")
        self.assertEqual(links[0].cache_description, '"Tekno Sucks 170" (2026) by Myor')

    def test_format_aliases_and_extensions(self):
        self.assertEqual(normalize_format("aac"), "aac-hi")
        self.assertEqual(extension_for_format("flac"), "flac")
        self.assertEqual(extension_for_format("vorbis"), "ogg")

    def test_sanitizes_and_uniquifies_filenames(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Bad_Name.mp3").write_text("x", encoding="utf-8")
            reserved = set()

            self.assertEqual(sanitize_filename('Bad/Name<>:"|?*.mp3'), "Bad_Name_______.mp3")
            self.assertEqual(sanitize_filename("CON"), "_CON")
            self.assertEqual(unique_path(root, "Bad_Name.mp3", reserved).name, "Bad_Name (2).mp3")


if __name__ == "__main__":
    unittest.main()

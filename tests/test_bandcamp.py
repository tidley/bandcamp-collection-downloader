import html
import json
import tempfile
import unittest
from pathlib import Path

from bandcamp_collection_downloader.bandcamp import (
    BandcampClient,
    extension_for_format,
    normalize_format,
    DownloadLink,
    TrackInfo,
    _collection_api_endpoints_from_text,
    _download_links_from_text,
    _extract_fan_id,
)
from bandcamp_collection_downloader.filenames import sanitize_filename, unique_path
from bandcamp_collection_downloader.http_client import HttpResult


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
        self.assertEqual(links[0].matching_cache_ids, ("p375739204",))
        self.assertEqual(links[0].title, "Tekno Sucks 170")
        self.assertEqual(links[0].year, "2026")
        self.assertEqual(links[0].artist, "Myor")
        self.assertEqual(links[0].cache_description, '"Tekno Sucks 170" (2026) by Myor')

    def test_extracts_pagedata_redownload_urls_with_r_and_p_cache_ids(self):
        links = _download_links_from_text(_pagedata_html(), "https://bandcamp.com/example-user")

        self.assertEqual([link.cache_id for link in links], ["p111", "r222"])
        self.assertEqual(links[1].matching_cache_ids, ("r222", "p222"))
        self.assertEqual(links[1].title, "Legacy Album")
        self.assertEqual(links[1].artist, "Artist B")

    def test_collection_api_paginates_from_bootstrap_token_until_exhausted(self):
        logs = []
        http = FakePagedHttp()
        client = BandcampClient(http, logs.append)

        links = client.find_downloads(username="example-user")

        self.assertEqual([link.cache_id for link in links], ["p111", "r222", "p333"])
        self.assertEqual(
            http.payloads,
            [
                {"count": 1, "fan_id": 123, "older_than_token": "tok1"},
                {"count": 1, "fan_id": 123, "older_than_token": "tok2"},
            ],
        )
        self.assertTrue(any("collection api page=1 offset=2 token=tok1 items=1 downloads=1" in message for message in logs))
        self.assertTrue(any("collection api stopped: more_available=false; items_seen=4; downloads_found=2" in message for message in logs))

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

    def test_target_layout_uses_artist_year_release_folder(self):
        client = BandcampClient(None, lambda _: None)
        link = DownloadLink(
            "https://bandcamp.com/download?sitem_id=1",
            "Spatial Saint",
            "src",
            "1",
            "2018",
            "Muxi",
            tracks=(TrackInfo("First", 1), TrackInfo("Second", 2)),
            cover_url="https://example.test/cover.jpg",
        )

        layout = client.target_layout(Path("/music/bandcamp"), link)

        self.assertEqual(layout.folder, Path("/music/bandcamp/Muxi/2018 - Spatial Saint"))
        self.assertEqual(
            list(layout.files),
            [
                Path("/music/bandcamp/Muxi/2018 - Spatial Saint/Muxi - Spatial Saint - 01 First.mp3"),
                Path("/music/bandcamp/Muxi/2018 - Spatial Saint/Muxi - Spatial Saint - 02 Second.mp3"),
                Path("/music/bandcamp/Muxi/2018 - Spatial Saint/cover.jpg"),
            ],
        )

    def test_plan_skips_preorders_by_default(self):
        client = BandcampClient(None, lambda _: None)
        plan = client.plan_downloads(
            [
                DownloadLink(
                    "https://bandcamp.com/download?sitem_id=1",
                    "Future Album",
                    "src",
                    "1",
                    "2999",
                    "Artist",
                    is_preorder=True,
                    release_date="2999-01-01",
                )
            ]
        )

        self.assertEqual(plan.summary.preorders_skipped, 1)
        self.assertEqual(plan.summary.new_downloads, 0)
        self.assertEqual(plan.preorders[0].cache_id, "p1")

    def test_include_preorders_allows_pending_plan(self):
        client = BandcampClient(None, lambda _: None)
        plan = client.plan_downloads(
            [DownloadLink("https://bandcamp.com/download?sitem_id=1", "Future Album", "src", "1", "2999", "Artist", is_preorder=True)],
            include_preorders=True,
        )

        self.assertEqual(plan.summary.preorders_skipped, 0)
        self.assertEqual(plan.summary.new_downloads, 1)

    def test_future_release_date_is_preorder(self):
        client = BandcampClient(None, lambda _: None)
        plan = client.plan_downloads(
            [DownloadLink("https://bandcamp.com/download?sitem_id=1", "Future Album", "src", "1", "2999", "Artist", release_date="2999-01-01")]
        )

        self.assertEqual(plan.summary.preorders_skipped, 1)

    def test_plan_skips_metadata_incomplete_release(self):
        client = BandcampClient(None, lambda _: None)
        plan = client.plan_downloads(
            [
                DownloadLink(
                    "https://bandcamp.com/download?sitem_id=1",
                    "Album",
                    "src",
                    "1",
                    "2020",
                    "Artist",
                    tracks=(TrackInfo("Preview", 1),),
                    expected_track_count=4,
                )
            ]
        )

        self.assertEqual(plan.summary.incomplete_skipped, 1)
        self.assertEqual(plan.summary.new_downloads, 0)


def _pagedata_html() -> str:
    blob = {
        "fan_data": {"fan_id": 123},
        "collection_data": {
            "item_count": 3,
            "batch_size": 1,
            "last_token": "tok1",
            "sequence": ["p10", "r20"],
            "redownload_urls": {
                "p111": "/download?from=collection&sitem_id=111",
                "r222": "/download?from=collection&sitem_id=222",
            },
        },
        "item_cache": {
            "collection": {
                "p10": {
                    "item_title": "Package Album",
                    "band_name": "Artist A",
                    "sale_item_type": "p",
                    "sale_item_id": 111,
                    "token": "1773338149:10:p::",
                },
                "r20": {
                    "item_title": "Legacy Album",
                    "band_name": "Artist B",
                    "sale_item_type": "r",
                    "sale_item_id": 222,
                    "token": "1773338149:20:a::",
                },
            }
        },
    }
    return f'<div id="pagedata" data-blob="{html.escape(json.dumps(blob), quote=True)}"></div>'


class FakePagedHttp:
    def __init__(self):
        self.payloads = []

    def request(self, url, *, method="GET", headers=None, data=None, json_data=None, referer=None, max_bytes=None):
        if json_data is None:
            return _json_response(url, _pagedata_html().encode())

        self.payloads.append(dict(json_data))
        if json_data["older_than_token"] == "tok1":
            return _json_response(
                url,
                {
                    "items": [
                        {
                            "item_title": "Second Page",
                            "band_name": "Artist C",
                            "sale_item_type": "p",
                            "sale_item_id": 333,
                            "token": "1773338149:30:p::",
                        }
                    ],
                    "redownload_urls": {"p333": "/download?from=collection&sitem_id=333"},
                    "last_token": "tok2",
                    "more_available": True,
                },
            )
        return _json_response(
            url,
            {
                "items": [
                    {
                        "item_title": "Duplicate Legacy Page",
                        "band_name": "Artist B",
                        "sale_item_type": "r",
                        "sale_item_id": 222,
                        "token": "1773338149:20:a::",
                    }
                ],
                "redownload_urls": {"r222": "/download?from=collection&sitem_id=222"},
                "last_token": "tok3",
                "more_available": False,
            },
        )


def _json_response(url, body):
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
    else:
        headers = {"Content-Type": "text/html"}
    return HttpResult(url=url, final_url=url, status=200, headers=headers, body=body)


if __name__ == "__main__":
    unittest.main()

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from bandcamp_collection_downloader.cookies import copy_cookie_db, discover_firefox_profiles, load_firefox_cookies, read_cookie_db


class CookieTests(unittest.TestCase):
    def test_filters_firefox_cookies_before_cookiejar(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "cookies.sqlite"
            self._make_cookie_db(db)

            jar, names, skipped = read_cookie_db(db)

            self.assertEqual(sorted(cookie.name for cookie in jar), ["identity", "js_logged_in"])
            self.assertEqual(names, ["identity", "js_logged_in"])
            self.assertEqual(skipped["irrelevant_name"], 1)
            self.assertEqual(skipped["blank_name"], 1)
            self.assertEqual(skipped["invalid_domain"], 1)
            self.assertEqual(skipped["invalid_name"], 1)
            self.assertEqual(skipped["control_chars"], 1)
            self.assertEqual(skipped["expired"], 1)

    def test_copy_cookie_db_copies_wal_and_shm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "cookies.sqlite"
            db.write_bytes(b"db")
            db.with_name("cookies.sqlite-wal").write_bytes(b"wal")
            db.with_name("cookies.sqlite-shm").write_bytes(b"shm")

            copied = copy_cookie_db(db, root / "copy")

            self.assertEqual(copied.read_bytes(), b"db")
            self.assertEqual(copied.with_name("cookies.sqlite-wal").read_bytes(), b"wal")
            self.assertEqual(copied.with_name("cookies.sqlite-shm").read_bytes(), b"shm")

    def test_discovers_native_and_flatpak_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            native_root = home / ".mozilla" / "firefox"
            flatpak_root = home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox"
            self._make_profile(native_root, "native.default", default=True)
            self._make_profile(flatpak_root, "flatpak.default", default=False)

            profiles = discover_firefox_profiles(home=home, uid=999999)

            self.assertEqual([profile.kind for profile in profiles], ["firefox", "flatpak"])
            self.assertTrue(profiles[0].is_default)

    def test_discovers_absolute_profile_path_from_profiles_ini(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            profile = Path(tmp) / "portal" / "example.default-release"
            profile.mkdir(parents=True)
            (profile / "cookies.sqlite").write_bytes(b"")
            root = home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox"
            root.mkdir(parents=True)
            (root / "profiles.ini").write_text(
                "[Profile0]\n"
                "Name=default-release\n"
                f"Path={profile}\n"
                "IsRelative=0\n",
                encoding="utf-8",
            )

            profiles = discover_firefox_profiles(home=home, uid=999999)

            self.assertEqual(profiles[0].path, profile)

    def test_auto_load_picks_highest_scoring_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            weak_root = home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox"
            strong_root = home / ".mozilla" / "firefox"
            weak = self._make_profile(weak_root, "weak.default", default=True)
            strong = self._make_profile(strong_root, "strong.default", default=False)
            self._make_cookie_db(weak / "cookies.sqlite", rows=[(".bandcamp.com", "identity", "abc")])
            self._make_cookie_db(
                strong / "cookies.sqlite",
                rows=[
                    (".bandcamp.com", "identity", "abc"),
                    (".bandcamp.com", "js_logged_in", "1"),
                    (".bandcamp.com", "logout", "x"),
                ],
            )
            logs = []

            result = load_firefox_cookies(home=home, log=logs.append)

            self.assertEqual(result.source_db, strong / "cookies.sqlite")
            self.assertEqual(result.loaded_names, ["identity", "js_logged_in", "logout"])
            self.assertTrue(any("profile considered:" in line for line in logs))
            self.assertTrue(any(f"chosen profile: {strong}" in line for line in logs))

    def test_requires_strong_cookies_unless_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            profile = self._make_profile(home / ".mozilla" / "firefox", "weak.default", default=True)
            self._make_cookie_db(profile / "cookies.sqlite", rows=[(".bandcamp.com", "identity", "abc")])

            with self.assertRaises(ValueError):
                load_firefox_cookies(home=home)

            result = load_firefox_cookies(home=home, allow_weak_cookies=True)
            self.assertEqual(result.loaded_names, ["identity"])

    def _make_cookie_db(self, db: Path, rows=None):
        now = int(time.time())
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                """
                CREATE TABLE moz_cookies (
                    host TEXT,
                    name TEXT,
                    value TEXT,
                    path TEXT,
                    expiry INTEGER,
                    isSecure INTEGER,
                    isHttpOnly INTEGER
                )
                """
            )
            rows = rows or [
                (".bandcamp.com", "identity", "abc"),
                ("bandcamp.com", "js_logged_in", "1"),
                (".bandcamp.com", "stripe_mid", "x"),
                (".bandcamp.com", "", "x"),
                (".stripe.com", "identity", "x"),
                (".bandcamp.com", "bad name", "x"),
                (".bandcamp.com", "logout", "x\n"),
                (".bandcamp.com", "download_encoding", "mp3-320", "/", now - 1, 1, 0),
            ]
            rows = [row if len(row) == 7 else (*row, "/", now + 3600, 1, 0) for row in rows]
            conn.executemany("INSERT INTO moz_cookies VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
            conn.commit()
        finally:
            conn.close()

    def _make_profile(self, root: Path, name: str, *, default: bool):
        profile = root / name
        profile.mkdir(parents=True)
        (profile / "cookies.sqlite").write_bytes(b"")
        (root / "profiles.ini").write_text(
            "[Profile0]\n"
            f"Name={name}\n"
            f"Path={name}\n"
            "IsRelative=1\n"
            f"Default={1 if default else 0}\n",
            encoding="utf-8",
        )
        return profile


if __name__ == "__main__":
    unittest.main()

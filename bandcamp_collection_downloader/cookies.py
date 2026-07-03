from __future__ import annotations

import configparser
import os
import re
import shutil
import sqlite3
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Iterable


ALLOWED_COOKIE_NAMES = {
    "identity",
    "js_logged_in",
    "logout",
    "download_encoding",
    "cart_client_id",
    "playlimit_client_id",
    "cookie_preferences",
}

ALLOWED_DOMAINS = {"bandcamp.com", ".bandcamp.com"}
COOKIE_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


@dataclass(frozen=True)
class FirefoxProfile:
    path: Path
    kind: str
    is_default: bool = False


@dataclass(frozen=True)
class ProfileScore:
    profile: FirefoxProfile
    score: int
    cookie_names: list[str]
    loaded_count: int
    error: str | None = None


@dataclass
class CookieLoadResult:
    jar: CookieJar
    source_db: Path
    copied_db: Path
    loaded_count: int
    loaded_names: list[str]
    skipped: Counter[str] = field(default_factory=Counter)
    profile_scores: list[ProfileScore] = field(default_factory=list)

    @property
    def has_identity(self) -> bool:
        return "identity" in self.loaded_names

    @property
    def has_js_logged_in(self) -> bool:
        return "js_logged_in" in self.loaded_names


def discover_firefox_profiles(home: Path | None = None, uid: int | None = None) -> list[FirefoxProfile]:
    home = Path.home() if home is None else home
    uid = os.getuid() if uid is None else uid
    profiles: list[FirefoxProfile] = []

    profiles.extend(_profiles_from_root(home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox", "flatpak"))
    profiles.extend(_profiles_from_root(home / ".mozilla" / "firefox", "firefox"))

    doc_root = Path(f"/run/user/{uid}/doc")
    if doc_root.exists():
        for cookie_db in sorted(doc_root.glob("*/*/cookies.sqlite")):
            profiles.append(FirefoxProfile(cookie_db.parent, "portal", False))
        for cookie_db in sorted(doc_root.glob("*/profile/cookies.sqlite")):
            profiles.append(FirefoxProfile(cookie_db.parent, "portal", False))

    return _dedupe_profiles(profiles)


def load_firefox_cookies(
    *,
    profile: Path | None = None,
    cookies_sqlite: Path | None = None,
    home: Path | None = None,
    allow_weak_cookies: bool = False,
    log=None,
) -> CookieLoadResult:
    log = log or (lambda _: None)
    profile_scores: list[ProfileScore] = []
    if cookies_sqlite:
        source_db = Path(cookies_sqlite).expanduser()
    else:
        profile_dir = Path(profile).expanduser() if profile else _choose_profile(discover_firefox_profiles(home), allow_weak_cookies, log, profile_scores).path
        source_db = profile_dir / "cookies.sqlite"

    if not source_db.exists():
        raise FileNotFoundError(f"Firefox cookie DB not found: {source_db}")

    with tempfile.TemporaryDirectory(prefix="bandcamp-cookies-") as tmp:
        copied = copy_cookie_db(source_db, Path(tmp))
        jar, names, skipped = read_cookie_db(copied)
        if not allow_weak_cookies and not _has_required_cookies(names):
            raise ValueError("Firefox cookies are missing identity/js_logged_in; pass --allow-weak-cookies to continue")
        return CookieLoadResult(
            jar=jar,
            source_db=source_db,
            copied_db=copied,
            loaded_count=len(jar),
            loaded_names=names,
            skipped=skipped,
            profile_scores=profile_scores,
        )


def copy_cookie_db(source_db: Path, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    copied = target_dir / "cookies.sqlite"
    shutil.copy2(source_db, copied)

    for suffix in ("-wal", "-shm"):
        sidecar = source_db.with_name(source_db.name + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, target_dir / sidecar.name)

    return copied


def read_cookie_db(db_path: Path) -> tuple[CookieJar, list[str], Counter[str]]:
    jar = CookieJar()
    skipped: Counter[str] = Counter()
    now = int(time.time())

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT host, name, value, path, expiry, isSecure, isHttpOnly
            FROM moz_cookies
            """
        )
        for row in rows:
            cookie = _cookie_from_row(row, now, skipped)
            if cookie is not None:
                jar.set_cookie(cookie)
    finally:
        conn.close()

    names = sorted({cookie.name for cookie in jar})
    return jar, names, skipped


def _profiles_from_root(root: Path, kind: str) -> list[FirefoxProfile]:
    if not root.exists():
        return []

    profiles: list[FirefoxProfile] = []
    ini = root / "profiles.ini"
    if ini.exists():
        parser = configparser.ConfigParser()
        parser.read(ini)
        for section in parser.sections():
            if not section.startswith("Profile"):
                continue
            raw_path = parser.get(section, "Path", fallback="")
            if not raw_path:
                continue
            path = Path(raw_path)
            if parser.getboolean(section, "IsRelative", fallback=True):
                path = root / path
            if (path / "cookies.sqlite").exists():
                profiles.append(
                    FirefoxProfile(path=path, kind=kind, is_default=parser.get(section, "Default", fallback="0") == "1")
                )

    for cookie_db in root.glob("*/cookies.sqlite"):
        profiles.append(FirefoxProfile(cookie_db.parent, kind, False))

    return profiles


def _dedupe_profiles(profiles: Iterable[FirefoxProfile]) -> list[FirefoxProfile]:
    seen: set[str] = set()
    result: list[FirefoxProfile] = []
    for profile in sorted(profiles, key=lambda p: (not p.is_default, p.kind, str(p.path))):
        key = str(profile.path)
        if key not in seen:
            seen.add(key)
            result.append(profile)
    return result


def _choose_profile(
    profiles: list[FirefoxProfile],
    allow_weak_cookies: bool,
    log,
    profile_scores: list[ProfileScore],
) -> FirefoxProfile:
    if not profiles:
        raise FileNotFoundError("No Firefox profile with cookies.sqlite found")
    best: ProfileScore | None = None
    for profile in profiles:
        score = _score_profile(profile)
        profile_scores.append(score)
        if score.error:
            log(f"profile considered: {profile.path} score=0 error={score.error}")
        else:
            log(f"profile considered: {profile.path} score={score.score} names={','.join(score.cookie_names) or '(none)'}")
        if score.error is None and (best is None or score.score > best.score):
            best = score

    if best is None:
        raise FileNotFoundError("No readable Firefox profile with cookies.sqlite found")
    if not allow_weak_cookies and not _has_required_cookies(best.cookie_names):
        raise ValueError("Best Firefox profile is missing identity/js_logged_in; pass --allow-weak-cookies to continue")
    log(f"chosen profile: {best.profile.path}")
    return best.profile


def _score_profile(profile: FirefoxProfile) -> ProfileScore:
    source_db = profile.path / "cookies.sqlite"
    try:
        with tempfile.TemporaryDirectory(prefix="bandcamp-cookie-score-") as tmp:
            copied = copy_cookie_db(source_db, Path(tmp))
            jar, names, _skipped = read_cookie_db(copied)
    except Exception as exc:
        return ProfileScore(profile=profile, score=0, cookie_names=[], loaded_count=0, error=str(exc))
    score = len(jar)
    if "identity" in names:
        score += 100
    if "js_logged_in" in names:
        score += 100
    if "logout" in names:
        score += 10
    return ProfileScore(profile=profile, score=score, cookie_names=names, loaded_count=len(jar))


def _has_required_cookies(names: list[str]) -> bool:
    return "identity" in names and "js_logged_in" in names


def _cookie_from_row(row: sqlite3.Row, now: int, skipped: Counter[str]) -> Cookie | None:
    name = _clean(row["name"])
    if not name:
        skipped["blank_name"] += 1
        return None
    if not COOKIE_TOKEN_RE.match(name):
        skipped["invalid_name"] += 1
        return None
    if name not in ALLOWED_COOKIE_NAMES:
        skipped["irrelevant_name"] += 1
        return None

    domain = _clean(row["host"]).lower()
    if domain not in ALLOWED_DOMAINS or _has_control_chars(domain):
        skipped["invalid_domain"] += 1
        return None

    value = "" if row["value"] is None else str(row["value"])
    if _has_control_chars(value):
        skipped["control_chars"] += 1
        return None

    path = _clean(row["path"]) or "/"
    if not path.startswith("/") or _has_control_chars(path):
        skipped["invalid_path"] += 1
        return None

    expires = int(row["expiry"] or 0)
    if expires and expires < now:
        skipped["expired"] += 1
        return None

    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=domain.startswith("."),
        domain_initial_dot=domain.startswith("."),
        path=path,
        path_specified=True,
        secure=bool(row["isSecure"]),
        expires=expires or None,
        discard=not expires,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": None} if row["isHttpOnly"] else {},
        rfc2109=False,
    )


def _clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def _has_control_chars(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)

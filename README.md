# Bandcamp Collection Downloader

A Python command-line tool for downloading purchased releases from a Bandcamp
fan collection.

This is intended as a drop-in, Python-based replacement for the original Java
`bandcamp-collection-downloader` CLI, with the same cache file format and a
command-line interface compatible with the original Java tool:

```bash
bandcamp_collection_downloader \
  --jobs=4 \
  -f=mp3-320 \
  --download-folder=/path/to/music \
  <bandcamp-user>
```

It downloads purchased items from a Bandcamp fan collection. Streaming-only
albums cannot be downloaded.

## Features

- No cookie export required for Firefox users.
- Firefox profiles are discovered automatically.
- Supports native Firefox and Firefox Flatpak.
- Supports xdg-document-portal profile paths such as `/run/user/.../doc/...`.
- Safe SQLite handling: copies `cookies.sqlite`, `cookies.sqlite-wal`, and
  `cookies.sqlite-shm` to a temporary directory before reading.
- Strict Bandcamp cookie filtering.
- Pure Python implementation with no external runtime dependencies.
- Java-compatible cache at:

  ```text
  <download-folder>/bandcamp-collection-downloader.cache
  ```

- Cache IDs preserve the original Java format, including `p...` and `r...`
  Bandcamp sale-item IDs.
- Downloads are written to the Java-style library layout:

  ```text
  <download-folder>/<Artist>/<YYYY - Album>/
  ```

- Atomic cache appends with locking, flush, and `fsync()`.
- `--jobs` bounded parallel downloads.
- Interrupted downloads resume automatically using `.part` files and HTTP
  `Range` requests when supported.
- Completed downloads are renamed from `.part` only after the transfer
  completes.
- Pre-orders and incomplete releases are skipped by default and are never added
  to the legacy cache.
- Retry policy for `429`, `500`, `502`, `503`, `504`, and network timeout-style
  failures.
- Dry-run output that shows cache behavior:

  ```text
  SKIP  p375739204  Example Album
    FOLDER  /music/bandcamp/Example Artist/2026 - Example Album
    FILE  /music/bandcamp/Example Artist/2026 - Example Album/Example Artist - Example Album - 01 Example Track.mp3
  PREORDER  p390100000  Example Artist - Future Album  release_date=2026-12-01
  DOWNLOAD  p390186728  Another Album
    FOLDER  /music/bandcamp/Another Artist/2026 - Another Album
    FILE  /music/bandcamp/Another Artist/2026 - Another Album/cover.jpg
  ```

## Requirements

- Python 3.10+
- Firefox or Firefox Flatpak with an active Bandcamp login

Currently supported browsers:

- Firefox
- Firefox Flatpak

Chrome-based browsers are not yet supported.

## Installation

### Option 1: virtual environment, recommended

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install .
```

### Option 2: pipx

```bash
pipx install .
```

### Option 3: run directly from the source tree

```bash
python3 -m bandcamp_collection_downloader --download-folder=/path/to/music <bandcamp-user>
```

## Usage

Most users can let the tool discover Firefox cookies automatically:

```bash
bandcamp_collection_downloader --download-folder=/path/to/music <bandcamp-user>
```

Choose a format:

```bash
bandcamp_collection_downloader -f=flac --download-folder=/path/to/music <bandcamp-user>
```

Pre-orders are skipped by default so preview-track downloads do not poison the
cache before the full release is available. To attempt them anyway without
adding them to the cache:

```bash
bandcamp_collection_downloader --include-preorders --download-folder=/path/to/music <bandcamp-user>
```

Run a dry-run first:

```bash
bandcamp_collection_downloader --dry-run --verbose --download-folder=/path/to/music <bandcamp-user>
```

Use a specific Firefox profile or cookie database:

```bash
bandcamp_collection_downloader --profile /path/to/firefox/profile <bandcamp-user>
bandcamp_collection_downloader --cookies-sqlite /path/to/cookies.sqlite <bandcamp-user>
```

## Migrating From The Java Downloader

No migration is normally required.

This implementation intentionally reuses:

- the existing download directory
- the existing `bandcamp-collection-downloader.cache`
- the same common command-line options: `--jobs`, `-f`, and `--download-folder`

Point this downloader at the same download directory and it will continue using
the existing cache. The cache tracks Bandcamp purchase IDs rather than
filenames, making it robust against album renames or filename changes.

## Cookie Discovery

When `--profile` and `--cookies-sqlite` are omitted, profiles are searched in
this order:

1. `~/.var/app/org.mozilla.firefox/.mozilla/firefox/profiles.ini`
2. `~/.mozilla/firefox/profiles.ini`

Both `IsRelative=1` and `IsRelative=0` entries are supported. Absolute profile
paths may point into xdg-document-portal locations.

Each candidate profile is scored:

- `+100` if `identity` exists.
- `+100` if `js_logged_in` exists.
- `+10` if `logout` exists.
- `+1` per relevant Bandcamp cookie.

By default, the chosen profile must contain both `identity` and `js_logged_in`.
Use `--allow-weak-cookies` to continue without them.

Verbose mode prints profiles considered, scores, chosen profile, and loaded
cookie names.

## Formats

Common Bandcamp formats are supported:

- `mp3-320`
- `mp3-v0`
- `flac`
- `aac-hi`
- `vorbis`
- `alac`
- `wav`
- `aiff-lossless`

Aliases:

- `aac` -> `aac-hi`
- `ogg` -> `vorbis`
- `aiff` -> `aiff-lossless`

## Cache Compatibility

The cache file is intentionally compatible with the original Java tool:

```text
p375739204| "Example Album" (2026) by Example Artist
r173055461| "Older Example Album" (2019) by Example Artist
```

The downloader uses the Bandcamp sale-item cache ID as the primary duplicate
check before filename matching. When Bandcamp only exposes a raw `sitem_id`, it
falls back to `p<sitem_id>` for compatibility. Cache entries are appended only
after a download completes successfully and the `.part` file has been renamed.

This lets an existing Bandcamp library switch between the Java downloader and
this Python implementation without losing cache history.

## Sample Output

Dry-run with `--verbose`:

```text
Loaded cache: 430 entries
PREORDER  p390100000  Example Artist - Future Album  release_date=2026-12-01
SKIP  p375739204  Example Album
  FOLDER  /music/bandcamp/Example Artist/2026 - Example Album
  FILE  /music/bandcamp/Example Artist/2026 - Example Album/Example Artist - Example Album - 01 Example Track.mp3
DOWNLOAD  p390186728  Another Album
  FOLDER  /music/bandcamp/Another Artist/2026 - Another Album
  FILE  /music/bandcamp/Another Artist/2026 - Another Album/Another Artist - Another Album - 01 First Track.mp3
  FILE  /music/bandcamp/Another Artist/2026 - Another Album/cover.jpg
DOWNLOAD  p390237414  Third Album
  FOLDER  /music/bandcamp/Third Artist/2026 - Third Album
  FILE  /music/bandcamp/Third Artist/2026 - Third Album/Third Artist - Third Album - 01 First Track.mp3
Collection items: 433
Already downloaded: 430
New downloads: 3
Succeeded: 0
Failed: 0
Preorders skipped: 1
Incomplete skipped: 0
Elapsed: 00:00:02
```

Download run with `--verbose`:

```text
Loaded cache: 430 entries
downloaded: /path/to/music/Another Album.mp3 (8542212 bytes) from https://...
downloaded: /path/to/music/Third Album.mp3 (9174820 bytes) from https://...
Cache updated: p390186728
Cache updated: p390237414
Collection items: 432
Already downloaded: 430
New downloads: 2
Succeeded: 2
Failed: 0
Elapsed: 00:00:09
```

## Advanced Usage

`--endpoint` adds an extra Bandcamp API endpoint to try when the logged-in page
does not expose enough download links. It is mainly a troubleshooting option
for Bandcamp page/API changes; normal users should not need it.

Example:

```bash
bandcamp_collection_downloader --endpoint https://bandcamp.com/api/... --dry-run --verbose <bandcamp-user>
```

## Privacy

Firefox cookies grant access to your Bandcamp account. Do not commit or share:

- `cookies.sqlite`
- `cookies.sqlite-wal`
- `cookies.sqlite-shm`
- exported cookie files
- verbose logs that include local profile paths or account details

This repository's `.gitignore` excludes common cookie, cache, and local test
data paths.

## Relationship To The Original Project

This project is not a fork and does not contain code from the original
implementation. It is a Python reimplementation built for compatibility with
the original Java/Kotlin CLI and cache format.

Original project:

- Official upstream: <https://framagit.org/Ezwen/bandcamp-collection-downloader>
- GitHub mirror: <https://github.com/Ezwen/bandcamp-collection-downloader>

## Development

Run tests:

```bash
python3 -m unittest
```

Run a compile check:

```bash
python3 -m compileall -q bandcamp_collection_downloader tests
```

## License

MIT

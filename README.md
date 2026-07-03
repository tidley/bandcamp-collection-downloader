# Bandcamp Collection Downloader

A small Python CLI for downloading purchased items from a Bandcamp fan
collection using Firefox cookies.

This is intended as a drop-in, Python-based replacement for the original Java
`bandcamp-collection-downloader` CLI, with the same cache file format and
compatible command-line shape:

```bash
bandcamp_collection_downloader \
  --jobs=4 \
  -f=mp3-320 \
  --download-folder=/path/to/music \
  <bandcamp-user>
```

## Features

- Automatic Firefox cookie discovery.
- Native Firefox and Flatpak Firefox support.
- xdg-document-portal profile support, including `/run/user/.../doc/...` paths.
- Safe SQLite handling: copies `cookies.sqlite`, `cookies.sqlite-wal`, and
  `cookies.sqlite-shm` to a temporary directory before reading.
- Strict Bandcamp cookie filtering.
- Java-compatible cache at:

  ```text
  <download-folder>/bandcamp-collection-downloader.cache
  ```

- Atomic cache appends with locking, flush, and `fsync()`.
- Cache IDs use the original `p<sitem_id>` format.
- `--jobs` bounded parallel downloads.
- `.part` resume using HTTP `Range` requests.
- Retry policy for `429`, `500`, `502`, `503`, `504`, and network timeout-style
  failures.
- Dry-run output that shows cache behavior:

  ```text
  SKIP  p375739204  Example Album
  DOWNLOAD  p390186728  Another Album
  ```

## Install

From a local checkout:

```bash
python3 -m pip install .
```

For editable development:

```bash
python3 -m pip install -e .
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

Run a dry-run first:

```bash
bandcamp_collection_downloader --dry-run --verbose --download-folder=/path/to/music <bandcamp-user>
```

Use a specific Firefox profile or cookie database:

```bash
bandcamp_collection_downloader --profile /path/to/firefox/profile <bandcamp-user>
bandcamp_collection_downloader --cookies-sqlite /path/to/cookies.sqlite <bandcamp-user>
```

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
```

The downloader uses `p<sitem_id>` as the primary duplicate check before
filename matching. Cache entries are appended only after a download completes
successfully and the `.part` file has been renamed.

This lets an existing Bandcamp library switch between the Java downloader and
this Python implementation without losing cache history.

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

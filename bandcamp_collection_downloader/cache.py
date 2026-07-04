from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Iterable


CACHE_FILENAME = "bandcamp-collection-downloader.cache"


class LegacyCache:
    def __init__(self, download_folder: Path, log):
        self.path = download_folder / CACHE_FILENAME
        self.log = log
        self.ids: set[str] = set()
        self._lock = threading.Lock()

    def load(self) -> None:
        self.ids = set()
        if self.path.exists():
            with self.path.open(encoding="utf-8") as fh:
                _lock(fh)
                try:
                    self.ids = {line.split("|", 1)[0].strip() for line in fh if "|" in line and line.strip()}
                finally:
                    _unlock(fh)
        self.log(f"Loaded cache: {len(self.ids)} entries")

    def has(self, cache_id: str | None) -> bool:
        return bool(cache_id and cache_id in self.ids)

    def find(self, cache_ids: Iterable[str | None]) -> str | None:
        for cache_id in cache_ids:
            if self.has(cache_id):
                return cache_id
        return None

    def append(self, cache_id: str | None, description: str) -> None:
        if not cache_id:
            return
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a+", encoding="utf-8") as fh:
                _lock(fh)
                try:
                    fh.seek(0)
                    self.ids = {line.split("|", 1)[0].strip() for line in fh if "|" in line and line.strip()}
                    if cache_id in self.ids:
                        return
                    fh.seek(0, os.SEEK_END)
                    fh.write(f"{cache_id}| {description}\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                finally:
                    _unlock(fh)
            self.ids.add(cache_id)
        self.log(f"Cache updated: {cache_id}")


def cache_id_from_sitem_id(sitem_id: str | int | None) -> str | None:
    if sitem_id is None:
        return None
    digits = str(sitem_id).strip()
    return f"p{digits}" if digits.isdigit() else None


def _lock(fh) -> None:
    try:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except ImportError:
        return


def _unlock(fh) -> None:
    try:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except ImportError:
        return

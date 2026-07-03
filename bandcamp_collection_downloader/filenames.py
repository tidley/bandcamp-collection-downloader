from __future__ import annotations

import re
from pathlib import Path


INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str, *, default: str = "bandcamp-download", max_len: int = 180) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("_", name)
    cleaned = " ".join(cleaned.split()).strip(" .")
    if not cleaned:
        cleaned = default
    stem = Path(cleaned).stem
    suffix = Path(cleaned).suffix
    if stem.upper() in RESERVED_WINDOWS_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:max_len].rstrip(" .") or default


def unique_path(directory: Path, filename: str, reserved: set[Path] | None = None) -> Path:
    reserved = reserved or set()
    filename = sanitize_filename(filename)
    path = directory / filename
    if path not in reserved and not path.exists():
        reserved.add(path)
        return path

    stem = path.stem
    suffix = path.suffix
    index = 2
    while True:
        candidate = directory / sanitize_filename(f"{stem} ({index}){suffix}")
        if candidate not in reserved and not candidate.exists():
            reserved.add(candidate)
            return candidate
        index += 1

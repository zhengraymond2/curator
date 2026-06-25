from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .checksums import sha256_file


MEDIA_EXTENSIONS = {
    ".3gp",
    ".arw",
    ".avi",
    ".cr2",
    ".cr3",
    ".dng",
    ".gif",
    ".heic",
    ".jpeg",
    ".jpg",
    ".m4v",
    ".mov",
    ".mp4",
    ".nef",
    ".orf",
    ".png",
    ".raf",
    ".rw2",
    ".tif",
    ".tiff",
}

SKIPPED_DIRS = {".curator", ".git", "Trash"}


@dataclass(frozen=True)
class MediaFile:
    path: Path
    name: str
    size: int
    sha256: str | None = None

    @property
    def duplicate_key(self) -> tuple[str, int, str | None]:
        return (self.name.casefold(), self.size, self.sha256)

    @property
    def name_size_key(self) -> tuple[str, int]:
        return (self.name.casefold(), self.size)


def is_media_file(path: Path) -> bool:
    return path.is_file() and path.suffix.casefold() in MEDIA_EXTENSIONS


def iter_media_files(root: Path) -> Iterable[Path]:
    root = root.expanduser()
    for path in root.rglob("*"):
        if any(part in SKIPPED_DIRS for part in path.parts):
            continue
        if is_media_file(path):
            yield path


def scan_media(root: Path, *, hash_files: bool = False) -> list[MediaFile]:
    files: list[MediaFile] = []
    for path in sorted(iter_media_files(root)):
        stat = path.stat()
        files.append(
            MediaFile(
                path=path,
                name=path.name,
                size=stat.st_size,
                sha256=sha256_file(path) if hash_files else None,
            )
        )
    return files


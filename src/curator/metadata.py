from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .progress import ProgressReporter


MDLS_DATE_FORMAT = "%Y-%m-%d %H:%M:%S %z"
EXIF_DATE_FORMATS = (
    "%Y:%m:%d %H:%M:%S%z",
    "%Y:%m:%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
)
EXIFTOOL_TIMESTAMP_TAGS = (
    "DateTimeOriginal",
    "CreateDate",
    "MediaCreateDate",
    "TrackCreateDate",
    "CreationDate",
)
IMAGE_METADATA_EXTENSIONS = {
    ".arw",
    ".cr2",
    ".cr3",
    ".dng",
    ".heic",
    ".jpeg",
    ".jpg",
    ".nef",
    ".orf",
    ".png",
    ".raf",
    ".rw2",
    ".tif",
    ".tiff",
}
COMMAND_CHUNK_SIZE = 100
METADATA_CACHE_FILENAME = "metadata-cache.json"
METADATA_CACHE_VERSION = 1


@dataclass(frozen=True)
class CaptureTimestamp:
    epoch: float
    source: str
    raw: str | None = None


class MetadataTimestampCache:
    def __init__(self, path: Path, entries: dict[str, object] | None = None) -> None:
        self.path = path
        self.entries = entries or {}
        self.dirty = False

    @classmethod
    def load(cls, path: Path) -> "MetadataTimestampCache":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls(path)

        if not isinstance(payload, dict) or payload.get("version") != METADATA_CACHE_VERSION:
            return cls(path)
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            return cls(path)
        return cls(path, entries)

    def get(self, path: Path) -> CaptureTimestamp | None:
        entry = self.entries.get(cache_key(path))
        if not isinstance(entry, dict):
            return None

        try:
            stat = path.stat()
            size = int(entry["size"])
            mtime_ns = int(entry["mtime_ns"])
            epoch = float(entry["epoch"])
            source = entry["source"]
        except (KeyError, OSError, TypeError, ValueError):
            return None

        if size != stat.st_size or mtime_ns != stat.st_mtime_ns or not isinstance(source, str):
            return None

        raw = entry.get("raw")
        return CaptureTimestamp(epoch=epoch, source=source, raw=raw if isinstance(raw, str) else None)

    def put(self, path: Path, timestamp: CaptureTimestamp) -> None:
        try:
            stat = path.stat()
        except OSError:
            return

        key = cache_key(path)
        entry = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "epoch": timestamp.epoch,
            "source": timestamp.source,
            "raw": timestamp.raw,
        }
        if self.entries.get(key) != entry:
            self.entries[key] = entry
            self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f"{self.path.name}.tmp")
        payload = {
            "version": METADATA_CACHE_VERSION,
            "entries": self.entries,
        }
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp_path.replace(self.path)


def metadata_cache_path(library_root: Path) -> Path:
    return library_root.expanduser().resolve() / ".curator" / METADATA_CACHE_FILENAME


def cache_key(path: Path) -> str:
    try:
        return str(path.expanduser().resolve())
    except OSError:
        return str(path.expanduser().absolute())


def capture_timestamp(path: Path) -> CaptureTimestamp:
    return capture_timestamps([path])[path]


def capture_timestamps(
    paths: list[Path],
    *,
    cache_path: Path | None = None,
    progress: ProgressReporter | None = None,
) -> dict[Path, CaptureTimestamp]:
    progress = progress or ProgressReporter.disabled()
    timestamps: dict[Path, CaptureTimestamp] = {}
    remaining = list(paths)
    cache = MetadataTimestampCache.load(cache_path) if cache_path is not None else None
    cached_paths: set[Path] = set()

    def finish() -> dict[Path, CaptureTimestamp]:
        if cache is not None:
            for cached_path, timestamp in timestamps.items():
                if cached_path in cached_paths:
                    continue
                cache.put(cached_path, timestamp)
            cache.save()
        return timestamps

    if cache is not None:
        for path in remaining:
            cached = cache.get(path)
            if cached is not None:
                timestamps[path] = cached
                cached_paths.add(path)
        remaining = [path for path in remaining if path not in timestamps]
        if not remaining:
            return finish()

    if remaining:
        before = len(timestamps)
        with progress.step(
            f"Reading metadata for {len(remaining)} file(s)",
            done=lambda: f"Exiftool matched {len(timestamps) - before} file(s)",
        ):
            for path, tag, value in exiftool_capture_dates(remaining):
                parsed = parse_exif_date(value)
                if parsed is not None:
                    timestamps[path] = CaptureTimestamp(epoch=parsed.timestamp(), source=f"exiftool:{tag}", raw=value)
    remaining = [path for path in remaining if path not in timestamps]
    if not remaining:
        return finish()

    image_paths = [path for path in remaining if path.suffix.casefold() in IMAGE_METADATA_EXTENSIONS]
    if image_paths:
        before = len(timestamps)
        with progress.step(
            f"Reading image metadata with sips for {len(image_paths)} file(s)",
            done=lambda: f"Sips matched {len(timestamps) - before} file(s)",
        ):
            for path, value in sips_creation_dates(image_paths).items():
                parsed = parse_exif_date(value)
                if parsed is not None:
                    timestamps[path] = CaptureTimestamp(epoch=parsed.timestamp(), source="sips:creation", raw=value)
    remaining = [path for path in remaining if path not in timestamps]
    if not remaining:
        return finish()

    if remaining:
        before = len(timestamps)
        with progress.step(
            f"Reading Spotlight metadata with mdls for {len(remaining)} file(s)",
            done=lambda: f"Mdls matched {len(timestamps) - before} file(s)",
        ):
            for path, value in mdls_content_creation_dates(remaining).items():
                parsed = parse_mdls_date(value)
                if parsed is not None:
                    timestamps[path] = CaptureTimestamp(
                        epoch=parsed.timestamp(), source="mdls:kMDItemContentCreationDate", raw=value
                    )
    remaining = [path for path in remaining if path not in timestamps]
    if not remaining:
        return finish()

    if remaining:
        with progress.step(
            f"Using filesystem modified times for {len(remaining)} file(s)",
            done=lambda: f"Used filesystem modified times for {len(remaining)} file(s)",
        ):
            for path in remaining:
                timestamps[path] = CaptureTimestamp(epoch=path.stat().st_mtime, source="filesystem_mtime")

    return finish()


def exiftool_capture_date(path: Path) -> tuple[str, str] | None:
    for result_path, tag, value in exiftool_capture_dates([path]):
        if result_path == path:
            return tag, value
    return None


def exiftool_capture_dates(paths: list[Path]) -> list[tuple[Path, str, str]]:
    if not paths or shutil.which("exiftool") is None:
        return []

    results: list[tuple[Path, str, str]] = []
    for chunk in chunks(paths, COMMAND_CHUNK_SIZE):
        results.extend(_exiftool_capture_dates_chunk(chunk))
    return results


def _exiftool_capture_dates_chunk(paths: list[Path]) -> list[tuple[Path, str, str]]:
    args = ["exiftool", "-j"]
    for tag in EXIFTOOL_TIMESTAMP_TAGS:
        args.append(f"-{tag}")
    args.extend(str(path) for path in paths)

    try:
        result = subprocess.run(
            args,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0:
        return []

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    results: list[tuple[Path, str, str]] = []
    for item in payload:
        source = item.get("SourceFile")
        if not isinstance(source, str):
            continue
        path = Path(source)
        for tag in EXIFTOOL_TIMESTAMP_TAGS:
            value = item.get(tag)
            if isinstance(value, str) and parse_exif_date(value) is not None:
                results.append((path, tag, value))
                break
    return results


def sips_creation_date(path: Path) -> str | None:
    return sips_creation_dates([path]).get(path)


def sips_creation_dates(paths: list[Path]) -> dict[Path, str]:
    if not paths or shutil.which("sips") is None:
        return {}

    results: dict[Path, str] = {}
    for chunk in chunks(paths, COMMAND_CHUNK_SIZE):
        results.update(_sips_creation_dates_chunk(chunk))
    return results


def _sips_creation_dates_chunk(paths: list[Path]) -> dict[Path, str]:
    try:
        result = subprocess.run(
            ["sips", "-g", "creation", *(str(path) for path in paths)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}

    if result.returncode != 0:
        return {}

    by_display_path = {str(path): path for path in paths}
    current: Path | None = None
    results: dict[Path, str] = {}
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped in by_display_path:
            current = by_display_path[stripped]
            continue
        if stripped.startswith("creation:"):
            value = stripped.split(":", 1)[1].strip()
            if current is not None and value:
                results[current] = value
    return results


def mdls_content_creation_date(path: Path) -> str | None:
    return mdls_content_creation_dates([path]).get(path)


def mdls_content_creation_dates(paths: list[Path]) -> dict[Path, str]:
    if not paths or shutil.which("mdls") is None:
        return {}

    results: dict[Path, str] = {}
    for chunk in chunks(paths, COMMAND_CHUNK_SIZE):
        results.update(_mdls_content_creation_dates_chunk(chunk))
    return results


def _mdls_content_creation_dates_chunk(paths: list[Path]) -> dict[Path, str]:
    try:
        result = subprocess.run(
            ["mdls", "-raw", "-name", "kMDItemContentCreationDate", *(str(path) for path in paths)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}

    if result.returncode != 0:
        return {}

    values = split_mdls_raw_output(result.stdout)
    results: dict[Path, str] = {}
    for path, value in zip(paths, values):
        if value and value != "(null)":
            results[path] = value
    return results


def split_mdls_raw_output(output: str) -> list[str]:
    stripped = output.strip("\n")
    if "\x00" in stripped:
        return [value.strip() for value in stripped.split("\x00")]
    return [line.strip() for line in stripped.splitlines() if line.strip()]


def parse_exif_date(value: str) -> datetime | None:
    cleaned = value.strip()
    if not cleaned or cleaned == "(null)":
        return None
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+0000"

    for date_format in EXIF_DATE_FORMATS:
        try:
            parsed = datetime.strptime(cleaned, date_format)
            if parsed.tzinfo is timezone.utc:
                return parsed
            return parsed
        except ValueError:
            continue
    return None


def parse_mdls_date(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, MDLS_DATE_FORMAT)
    except ValueError:
        return None


def chunks(paths: list[Path], size: int) -> list[list[Path]]:
    return [paths[index : index + size] for index in range(0, len(paths), size)]

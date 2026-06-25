from __future__ import annotations

import re
from pathlib import Path


SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_component(value: str, fallback: str = "Untitled") -> str:
    cleaned = SAFE_COMPONENT_RE.sub("-", value).strip(" .-_")
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned)
    return cleaned or fallback


def trash_name_for_path(path: Path) -> str:
    parts = [safe_component(part, "root") for part in path.resolve().parts if part not in ("/", "")]
    return "__".join(parts)


def ensure_unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


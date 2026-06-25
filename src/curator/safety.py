from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from .checksums import sha256_file
from .plan import Operation, Plan, utc_now_iso


class SafetyError(RuntimeError):
    """Raised when a planned operation violates Curator's safety rules."""


def _path(value: str | None, field_name: str) -> Path:
    if not value:
        raise SafetyError(f"operation missing {field_name}")
    return Path(value).expanduser()


def _refuse_symlink_source(path: Path) -> None:
    if path.is_symlink():
        raise SafetyError(f"refusing to operate on symlink source: {path}")


def _ensure_destination_available(path: Path) -> None:
    if path.exists():
        raise SafetyError(f"refusing to overwrite existing destination: {path}")


def apply_plan(plan: Plan, *, log_root: Path | None = None) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    log_handle = None
    if log_root is not None:
        log_root.mkdir(parents=True, exist_ok=True)
        log_path = log_root / f"{plan.run_id}.jsonl"
        _ensure_destination_available(log_path)
        log_handle = log_path.open("w", encoding="utf-8")

    try:
        for operation in plan.operations:
            result = apply_operation(operation)
            result["time"] = utc_now_iso()
            result["run_id"] = plan.run_id
            results.append(result)
            if log_handle is not None:
                log_handle.write(json.dumps(result, sort_keys=True) + "\n")
                log_handle.flush()
    finally:
        if log_handle is not None:
            log_handle.close()

    return results


def apply_operation(operation: Operation) -> dict[str, object]:
    if operation.type == "mkdir":
        dest = _path(operation.dest, "dest")
        dest.mkdir(parents=True, exist_ok=True)
        return {"op": "mkdir", "dest": str(dest)}

    if operation.type == "write_text":
        dest = _path(operation.dest, "dest")
        _ensure_destination_available(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(operation.text or "", encoding="utf-8")
        return {"op": "write_text", "dest": str(dest)}

    if operation.type == "copy":
        src = _path(operation.src, "src")
        dest = _path(operation.dest, "dest")
        if not src.is_file():
            raise SafetyError(f"copy source is not a file: {src}")
        _refuse_symlink_source(src)
        _ensure_destination_available(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.curator-tmp-{uuid.uuid4().hex}")
        try:
            shutil.copy2(src, tmp)
            if operation.expected_size is not None and tmp.stat().st_size != operation.expected_size:
                raise SafetyError(f"copy size mismatch for {src}")
            if operation.expected_sha256 is not None:
                copied_sha = sha256_file(tmp)
                if copied_sha != operation.expected_sha256:
                    raise SafetyError(f"copy checksum mismatch for {src}")
            tmp.rename(dest)
        finally:
            if tmp.exists():
                tmp.unlink()
        return {
            "op": "copy",
            "src": str(src),
            "dest": str(dest),
            "size": dest.stat().st_size,
            "sha256_verified": operation.expected_sha256 is not None,
        }

    if operation.type == "move":
        src = _path(operation.src, "src")
        dest = _path(operation.dest, "dest")
        if not src.exists():
            raise SafetyError(f"move source does not exist: {src}")
        _refuse_symlink_source(src)
        _ensure_destination_available(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        return {"op": "move", "src": str(src), "dest": str(dest), "reason": operation.reason}

    raise SafetyError(f"unsupported operation type: {operation.type}")


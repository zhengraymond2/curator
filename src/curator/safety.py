from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .checksums import sha256_file
from .paths import is_relative_to
from .plan import Operation, Plan, utc_now_iso
from .progress import ProgressReporter


class SafetyError(RuntimeError):
    """Raised when a planned operation violates Curator's safety rules."""


@dataclass(frozen=True)
class ApplyPolicy:
    source_root: Path
    dest_root: Path

    def __post_init__(self) -> None:
        source = self.source_root.expanduser().resolve()
        dest = self.dest_root.expanduser().resolve()
        object.__setattr__(self, "source_root", source)
        object.__setattr__(self, "dest_root", dest)
        if source == dest:
            raise SafetyError("source and dest roots must be different")
        if is_relative_to(source, dest):
            raise SafetyError("source root cannot be inside dest root")
        if is_relative_to(dest, source):
            raise SafetyError("dest root cannot be inside source root")

    def validate_log_root(self, path: Path) -> None:
        if not is_relative_to(path, self.dest_root):
            raise SafetyError(f"log root must be inside dest root: {path}")
        if is_relative_to(path, self.source_root):
            raise SafetyError(f"log root cannot be inside source root: {path}")

    def validate_operation(self, operation: Operation) -> None:
        if operation.type != "copy":
            raise SafetyError(f"restricted source-to-dest plans only support copy operations: {operation.type}")
        src = _path(operation.src, "src").resolve()
        dest = _path(operation.dest, "dest").resolve()
        if not is_relative_to(src, self.source_root):
            raise SafetyError(f"copy source is outside source root: {src}")
        if is_relative_to(dest, self.source_root):
            raise SafetyError(f"copy destination cannot be inside source root: {dest}")
        if not is_relative_to(dest, self.dest_root):
            raise SafetyError(f"copy destination is outside dest root: {dest}")


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


def apply_plan(
    plan: Plan,
    *,
    log_root: Path | None = None,
    progress: ProgressReporter | None = None,
) -> list[dict[str, object]]:
    progress = progress or ProgressReporter.disabled()
    policy = restricted_policy_for_plan(plan)
    results: list[dict[str, object]] = []
    log_handle = None
    if log_root is not None:
        if policy is not None:
            policy.validate_log_root(log_root)
        log_root.mkdir(parents=True, exist_ok=True)
        log_path = log_root / f"{plan.run_id}.jsonl"
        _ensure_destination_available(log_path)
        log_handle = log_path.open("x", encoding="utf-8")

    try:
        with progress.step(
            f"Applying {len(plan.operations)} operation(s)",
            done=lambda: f"Applied {len(results)} operation(s)",
        ):
            for operation in plan.operations:
                result = apply_operation(operation, policy=policy)
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


def restricted_policy_for_plan(plan: Plan) -> ApplyPolicy | None:
    if plan.metadata.get("kind") != "organize" or plan.metadata.get("transfer") != "copy":
        return None
    source = plan.metadata.get("source")
    library = plan.metadata.get("library")
    if not isinstance(source, str) or not isinstance(library, str):
        raise SafetyError("restricted organize copy plan requires source and library metadata")
    return ApplyPolicy(source_root=Path(source), dest_root=Path(library))


def apply_operation(operation: Operation, *, policy: ApplyPolicy | None = None) -> dict[str, object]:
    if policy is not None:
        policy.validate_operation(operation)

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
        copy_file_without_unlink(src, dest)
        if operation.expected_size is not None and dest.stat().st_size != operation.expected_size:
            raise SafetyError(f"copy size mismatch for {src}")
        if operation.expected_sha256 is not None:
            copied_sha = sha256_file(dest)
            if copied_sha != operation.expected_sha256:
                raise SafetyError(f"copy checksum mismatch for {src}")
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


def copy_file_without_unlink(src: Path, dest: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with src.open("rb") as source_handle:
        fd = os.open(dest, flags, 0o666)
        try:
            with os.fdopen(fd, "wb") as dest_handle:
                fd = -1
                shutil.copyfileobj(source_handle, dest_handle, length=1024 * 1024)
        finally:
            if fd != -1:
                os.close(fd)
    shutil.copystat(src, dest, follow_symlinks=False)

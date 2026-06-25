from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .checksums import sha256_file
from .paths import safe_component
from .plan import Operation, Plan, make_plan, new_run_id
from .progress import ProgressReporter


def build_ingest_plan(
    source: Path,
    dest_root: Path,
    *,
    name: str | None = None,
    progress: ProgressReporter | None = None,
) -> Plan:
    progress = progress or ProgressReporter.disabled()
    source = source.expanduser().resolve()
    dest_root = dest_root.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"ingest source must be a directory: {source}")

    run_id = new_run_id("ingest")
    folder_name = safe_component(name or f"{date.today().isoformat()}_{source.name}")
    ingest_root = dest_root / "Ingests" / folder_name
    operations: list[Operation] = []
    manifest_files: list[dict[str, object]] = []
    checksum_lines: list[str] = []

    with progress.step(
        f"Scanning source files under {source}",
        done=lambda: f"Found {len(source_files)} source file(s)",
    ):
        source_files = sorted(
            path for path in source.rglob("*") if path.is_file() and not any(part == ".curator" for part in path.parts)
        )

    with progress.step(
        f"Hashing {len(source_files)} source file(s)",
        done=lambda: f"Hashed {len(manifest_files)} source file(s)",
    ):
        for src in source_files:
            rel = src.relative_to(source)
            dest = ingest_root / rel
            digest = sha256_file(src)
            size = src.stat().st_size
            operations.append(
                Operation(
                    type="copy",
                    src=str(src),
                    dest=str(dest),
                    reason="ingest",
                    expected_size=size,
                    expected_sha256=digest,
                    metadata={"relative_path": str(rel)},
                )
            )
            manifest_files.append(
                {
                    "source": str(src),
                    "dest": str(dest),
                    "relative_path": str(rel),
                    "size": size,
                    "sha256": digest,
                }
            )
            checksum_lines.append(f"{digest}  {rel.as_posix()}")

    manifest = {
        "run_id": run_id,
        "source": str(source),
        "ingest_root": str(ingest_root),
        "file_count": len(manifest_files),
        "files": manifest_files,
        "checksum_algorithm": "sha256",
    }
    curator_dir = ingest_root / ".curator"
    operations.append(
        Operation(
            type="write_text",
            dest=str(curator_dir / "checksums.sha256"),
            reason="ingest-manifest",
            text="\n".join(checksum_lines) + ("\n" if checksum_lines else ""),
        )
    )
    operations.append(
        Operation(
            type="write_text",
            dest=str(curator_dir / "manifest.json"),
            reason="ingest-manifest",
            text=json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        )
    )

    return make_plan(
        run_id=run_id,
        description=f"ingest {source} to {ingest_root}",
        operations=operations,
        metadata={
            "kind": "ingest",
            "source": str(source),
            "dest_root": str(dest_root),
            "ingest_root": str(ingest_root),
            "file_count": len(manifest_files),
            "bytes": sum(int(item["size"]) for item in manifest_files),
        },
    )

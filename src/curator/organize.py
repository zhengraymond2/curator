from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .metadata import capture_timestamps
from .paths import is_relative_to, safe_component
from .plan import Operation, Plan, make_plan, new_run_id
from .scan import scan_media

SHOOT_GAP_SECONDS = 60 * 60


def build_organize_plan(source: Path, library: Path, *, mode: str, transfer: str = "copy") -> Plan:
    if mode not in {"ongoing", "migration"}:
        raise ValueError("mode must be 'ongoing' or 'migration'")
    if transfer not in {"copy", "move"}:
        raise ValueError("transfer must be 'copy' or 'move'")

    source = source.expanduser().resolve()
    library = library.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"organize source must be a directory: {source}")
    originals = library / "Originals"
    run_id = new_run_id(f"organize-{mode}")
    files = scan_media(source, hash_files=False)
    operations: list[Operation] = []

    by_parent = defaultdict(list)
    timestamped_files = []
    for media in files:
        if mode == "ongoing" and is_relative_to(media.path, originals):
            continue
        timestamped_files.append(media)
        by_parent[media.path.parent].append(media)
    timestamps = capture_timestamps([media.path for media in timestamped_files])

    for parent, group in sorted(by_parent.items(), key=lambda item: str(item[0])):
        sorted_group = sorted(group, key=lambda media: timestamps[media.path].epoch)
        shoot_index = 1
        previous_ts: float | None = None
        for media in sorted_group:
            captured = timestamps[media.path]
            if previous_ts is not None and captured.epoch - previous_ts > SHOOT_GAP_SECONDS:
                shoot_index += 1
            previous_ts = captured.epoch

            folder_name = safe_component(parent.name or "Shoot")
            if shoot_index > 1:
                folder_name = f"{folder_name}-{shoot_index:02d}"
            dest = originals / "Unsorted" / folder_name / media.path.name
            operations.append(
                Operation(
                    type=transfer,
                    src=str(media.path),
                    dest=str(dest),
                    reason=f"organize-{mode}",
                    expected_size=media.size if transfer == "copy" else None,
                    metadata={
                        "timestamp_source": captured.source,
                        "timestamp_raw": captured.raw,
                        "capture_epoch": captured.epoch,
                        "source_parent": str(parent),
                    },
                )
            )

    return make_plan(
        run_id=run_id,
        description=f"organize {source} into {originals} ({mode}, {transfer})",
        operations=operations,
        metadata={
            "kind": "organize",
            "mode": mode,
            "transfer": transfer,
            "source": str(source),
            "library": str(library),
            "files_planned": len(operations),
            "layout": "Originals/Country/Album",
            "unknown_country": "Unsorted",
            "timestamp_source": "exiftool_then_sips_then_mdls_then_filesystem_mtime",
        },
    )

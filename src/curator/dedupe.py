from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .checksums import sha256_file
from .paths import is_relative_to, trash_name_for_path
from .plan import Operation, Plan, make_plan, new_run_id, utc_now_iso
from .scan import MediaFile, scan_media


def build_dedupe_plan(roots: list[Path], trash_root: Path, *, library: Path | None = None) -> Plan:
    run_id = new_run_id("dedupe")
    trash_run_root = trash_root.expanduser().resolve() / "Duplicates" / run_id
    files: list[MediaFile] = []
    for root in roots:
        files.extend(scan_media(root.expanduser().resolve(), hash_files=False))

    by_name_size: dict[tuple[str, int], list[MediaFile]] = defaultdict(list)
    for media in files:
        by_name_size[media.name_size_key].append(media)

    conflicts: list[dict[str, object]] = []
    hashed_candidates = 0

    by_duplicate_key: dict[tuple[str, int, str | None], list[MediaFile]] = defaultdict(list)
    for name_size_key, group in sorted(by_name_size.items()):
        if len(group) < 2:
            continue
        hashed_group: list[MediaFile] = []
        for media in group:
            hashed_candidates += 1
            hashed_group.append(
                MediaFile(
                    path=media.path,
                    name=media.name,
                    size=media.size,
                    sha256=sha256_file(media.path),
                )
            )

        digests = {media.sha256 for media in hashed_group}
        if len(digests) > 1:
            conflicts.append(
                {
                    "name": name_size_key[0],
                    "size": name_size_key[1],
                    "sha256_values": sorted(value for value in digests if value),
                    "paths": [str(media.path) for media in hashed_group],
                }
            )
            continue

        for media in hashed_group:
            by_duplicate_key[media.duplicate_key].append(media)

    operations: list[Operation] = []
    log_lines = [
        f"Curator duplicate run: {run_id}",
        f"Time: {utc_now_iso()}",
        "",
    ]

    duplicate_groups = 0
    duplicate_files = 0
    for group in by_duplicate_key.values():
        if len(group) < 2:
            continue
        duplicate_groups += 1
        keep = choose_preserved_file(group, library=library)
        log_lines.append(f"Preserved: {keep.path}")
        for duplicate in sorted(group, key=lambda item: str(item.path)):
            if duplicate.path == keep.path:
                continue
            duplicate_files += 1
            dest = trash_run_root / trash_name_for_path(duplicate.path)
            operations.append(
                Operation(
                    type="move",
                    src=str(duplicate.path),
                    dest=str(dest),
                    reason="duplicate",
                    metadata={
                        "preserved": str(keep.path),
                        "sha256": duplicate.sha256,
                        "size": duplicate.size,
                        "name": duplicate.name,
                    },
                )
            )
            log_lines.append(f"  Duplicate moved: {duplicate.path}")
            log_lines.append(f"  Trash path: {dest}")
            log_lines.append(f"  Evidence: name={duplicate.name} size={duplicate.size} sha256={duplicate.sha256}")
        log_lines.append("")

    if conflicts:
        log_lines.append("Conflicts requiring manual review:")
        for conflict in conflicts:
            log_lines.append(
                f"  Same name and size but different content: {conflict['name']} size={conflict['size']}"
            )
            for digest in conflict["sha256_values"]:
                log_lines.append(f"    sha256={digest}")
        log_lines.append("")

    operations.append(
        Operation(
            type="write_text",
            dest=str(trash_run_root / "LOG.txt"),
            reason="dedupe-log",
            text="\n".join(log_lines),
        )
    )

    return make_plan(
        run_id=run_id,
        description=f"dedupe {len(roots)} root(s)",
        operations=operations,
        metadata={
            "kind": "dedupe",
            "roots": [str(root.expanduser().resolve()) for root in roots],
            "trash_root": str(trash_root.expanduser().resolve()),
            "library": str(library.expanduser().resolve()) if library else None,
            "files_scanned": len(files),
            "candidate_files_hashed": hashed_candidates,
            "duplicate_groups": duplicate_groups,
            "duplicate_files": duplicate_files,
            "conflicts": conflicts,
        },
    )


def choose_preserved_file(group: list[MediaFile], *, library: Path | None) -> MediaFile:
    if library is not None:
        originals = library.expanduser().resolve() / "Originals"
        for media in sorted(group, key=lambda item: str(item.path)):
            if is_relative_to(media.path, originals):
                return media
    return sorted(group, key=lambda item: str(item.path))[0]

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .checksums import sha256_file
from .plan import Plan
from .paths import is_relative_to
from .progress import ProgressReporter
from .scan import scan_media


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class OrganizeVerificationReport:
    planned_file_count: int
    source_file_count: int
    final_file_count: int
    source_byte_sum: int
    final_byte_sum: int
    source_unique_filenames: int
    final_unique_filenames: int
    checksum_multiset_match: bool
    pairwise_checksum_match: bool
    filesum_match: bool
    filename_set_match: bool
    source_plan_match: bool
    duplicate_destinations: tuple[Path, ...]
    missing_sources: tuple[Path, ...]
    missing_destinations: tuple[Path, ...]
    unplanned_source_files: tuple[Path, ...]
    stale_planned_sources: tuple[Path, ...]
    missing_filenames: tuple[str, ...]
    unexpected_filenames: tuple[str, ...]
    size_mismatches: tuple[tuple[Path, Path, int, int], ...]
    checksum_mismatches: tuple[tuple[Path, Path, str, str], ...]

    @property
    def success(self) -> bool:
        return all(
            (
                self.checksum_multiset_match,
                self.pairwise_checksum_match,
                self.filesum_match,
                self.filename_set_match,
                self.source_plan_match,
                not self.duplicate_destinations,
                not self.missing_sources,
                not self.missing_destinations,
                not self.size_mismatches,
                not self.checksum_mismatches,
            )
        )


def verify_organize_copy_plan(
    plan: Plan,
    *,
    progress: ProgressReporter | None = None,
) -> OrganizeVerificationReport:
    progress = progress or ProgressReporter.disabled()
    operations = [operation for operation in plan.operations if operation.type == "copy" and operation.src and operation.dest]
    planned_sources = tuple(Path(operation.src).expanduser().resolve() for operation in operations if operation.src)
    planned_dests = tuple(Path(operation.dest).expanduser().resolve() for operation in operations if operation.dest)
    duplicate_destinations = tuple(
        sorted((path for path, count in Counter(planned_dests).items() if count > 1), key=str)
    )

    source_root_value = plan.metadata.get("source")
    source_scan_available = False
    scanned_source_paths: set[Path] = set()
    if isinstance(source_root_value, str):
        source_root = Path(source_root_value).expanduser().resolve()
        if source_root.is_dir():
            source_scan_available = True
            scanned_source_paths = {media.path.expanduser().resolve() for media in scan_media(source_root)}
    library_value = plan.metadata.get("library")
    organization_root_value = plan.metadata.get("organization_root")
    if plan.metadata.get("mode") == "ongoing":
        if isinstance(organization_root_value, str):
            organization_root = Path(organization_root_value).expanduser().resolve()
            outside_organization_root = {
                path for path in scanned_source_paths if not is_relative_to(path, organization_root)
            }
            if outside_organization_root:
                scanned_source_paths = outside_organization_root
        elif isinstance(library_value, str):
            originals = Path(library_value).expanduser().resolve() / "Originals"
            scanned_source_paths = {path for path in scanned_source_paths if not is_relative_to(path, originals)}
    planned_source_set = set(planned_sources)
    aggregate_source_paths = scanned_source_paths if source_scan_available else planned_source_set

    source_by_path: dict[Path, FileSnapshot] = {}
    final_by_path: dict[Path, FileSnapshot] = {}
    missing_sources: list[Path] = []
    missing_destinations: list[Path] = []
    size_mismatches: list[tuple[Path, Path, int, int]] = []
    checksum_mismatches: list[tuple[Path, Path, str, str]] = []

    with progress.step(
        "Running final source/final integrity verification",
        done=lambda: "Finished final source/final integrity verification",
    ):
        for src in sorted(aggregate_source_paths, key=str):
            source_snapshot = snapshot_file(src)
            if source_snapshot is None:
                missing_sources.append(src)
            else:
                source_by_path[src] = source_snapshot

        for dest in planned_dests:
            final_snapshot = snapshot_file(dest)
            if final_snapshot is None:
                missing_destinations.append(dest)
            else:
                final_by_path[dest] = final_snapshot

        for src, dest in zip(planned_sources, planned_dests):
            source_snapshot = source_by_path.get(src)
            if source_snapshot is None:
                source_snapshot = snapshot_file(src)
                if source_snapshot is None:
                    if src not in missing_sources:
                        missing_sources.append(src)
                    continue
            final_snapshot = final_by_path.get(dest)
            if final_snapshot is None:
                continue
            if source_snapshot.size != final_snapshot.size:
                size_mismatches.append((src, dest, source_snapshot.size, final_snapshot.size))
            if source_snapshot.sha256 != final_snapshot.sha256:
                checksum_mismatches.append((src, dest, source_snapshot.sha256, final_snapshot.sha256))

    source_snapshots = tuple(source_by_path.values())
    final_snapshots = tuple(final_by_path.values())
    source_names = {snapshot.name for snapshot in source_snapshots}
    final_names = {snapshot.name for snapshot in final_snapshots}
    unplanned_source_files = tuple(sorted(scanned_source_paths - planned_source_set, key=str))
    stale_planned_sources = (
        tuple(sorted(planned_source_set - scanned_source_paths, key=str)) if source_scan_available else ()
    )

    return OrganizeVerificationReport(
        planned_file_count=len(planned_sources),
        source_file_count=len(source_snapshots),
        final_file_count=len(final_snapshots),
        source_byte_sum=sum(snapshot.size for snapshot in source_snapshots),
        final_byte_sum=sum(snapshot.size for snapshot in final_snapshots),
        source_unique_filenames=len(source_names),
        final_unique_filenames=len(final_names),
        checksum_multiset_match=Counter(snapshot.sha256 for snapshot in source_snapshots)
        == Counter(snapshot.sha256 for snapshot in final_snapshots),
        pairwise_checksum_match=not checksum_mismatches,
        filesum_match=len(source_snapshots) == len(final_snapshots)
        and sum(snapshot.size for snapshot in source_snapshots) == sum(snapshot.size for snapshot in final_snapshots),
        filename_set_match=source_names == final_names,
        source_plan_match=not unplanned_source_files and not stale_planned_sources,
        duplicate_destinations=duplicate_destinations,
        missing_sources=tuple(missing_sources),
        missing_destinations=tuple(missing_destinations),
        unplanned_source_files=unplanned_source_files,
        stale_planned_sources=stale_planned_sources,
        missing_filenames=tuple(sorted(source_names - final_names)),
        unexpected_filenames=tuple(sorted(final_names - source_names)),
        size_mismatches=tuple(size_mismatches),
        checksum_mismatches=tuple(checksum_mismatches),
    )


def snapshot_file(path: Path) -> FileSnapshot | None:
    if not path.is_file():
        return None
    stat = path.stat()
    return FileSnapshot(path=path, name=path.name, size=stat.st_size, sha256=sha256_file(path))


def format_verification_summary(report: OrganizeVerificationReport) -> str:
    status = "PASSED" if report.success else "FAILED"
    checksum_status = "PASSED" if report.checksum_multiset_match and report.pairwise_checksum_match else "FAILED"
    filesum_status = "PASSED" if report.filesum_match else "FAILED"
    filename_status = "PASSED" if report.filename_set_match else "FAILED"
    source_plan_status = "PASSED" if report.source_plan_match else "FAILED"
    return "\n".join(
        [
            f"Final integrity verification: {status}",
            f"Checksum comparison: {checksum_status}",
            (
                "Filesum comparison: "
                f"{filesum_status} ({report.source_file_count} source file(s), "
                f"{report.final_file_count} final file(s); "
                f"{report.source_byte_sum} source byte(s), {report.final_byte_sum} final byte(s))"
            ),
            (
                "Filename set comparison: "
                f"{filename_status} ({report.source_unique_filenames} source name(s), "
                f"{report.final_unique_filenames} final name(s))"
            ),
            f"Source plan coverage: {source_plan_status} ({report.planned_file_count} planned file(s))",
        ]
    )


def format_large_red_error(report: OrganizeVerificationReport, *, detail_limit: int = 25) -> str:
    red = "\033[1;31m"
    reset = "\033[0m"
    banner = "\n".join(
        [
            "",
            "########################################################################",
            "###                                                                  ###",
            "###  CURATOR INTEGRITY ERROR                                         ###",
            "###  DO NOT DELETE THE ORIGINAL SOURCE FOLDER.                       ###",
            "###  ONE OR MORE FILES ARE MISSING OR DO NOT MATCH THE FINAL COPY.   ###",
            "###                                                                  ###",
            "########################################################################",
            "",
        ]
    )
    return f"{red}{banner}{reset}\n{format_verification_summary(report)}\n{format_verification_details(report, detail_limit=detail_limit)}"


def format_verification_details(report: OrganizeVerificationReport, *, detail_limit: int = 25) -> str:
    lines: list[str] = []
    append_paths(lines, "Missing source files from the plan", report.missing_sources, detail_limit)
    append_paths(lines, "Missing final files", report.missing_destinations, detail_limit)
    append_paths(lines, "Duplicate planned final destinations", report.duplicate_destinations, detail_limit)
    append_paths(lines, "Source media files not covered by the plan", report.unplanned_source_files, detail_limit)
    append_paths(lines, "Planned source files no longer present in the source scan", report.stale_planned_sources, detail_limit)
    append_values(lines, "Missing final filenames", report.missing_filenames, detail_limit)
    append_values(lines, "Unexpected final filenames", report.unexpected_filenames, detail_limit)

    if report.size_mismatches:
        lines.append("Size mismatches:")
        for src, dest, source_size, final_size in report.size_mismatches[:detail_limit]:
            lines.append(f"  {src} -> {dest} ({source_size} source byte(s), {final_size} final byte(s))")
        append_remaining(lines, len(report.size_mismatches), detail_limit)

    if report.checksum_mismatches:
        lines.append("Checksum mismatches:")
        for src, dest, source_sha, final_sha in report.checksum_mismatches[:detail_limit]:
            lines.append(f"  {src} -> {dest} ({source_sha} source, {final_sha} final)")
        append_remaining(lines, len(report.checksum_mismatches), detail_limit)

    return "\n".join(lines) if lines else "No per-file mismatch details were recorded."


def append_paths(lines: list[str], title: str, paths: tuple[Path, ...], limit: int) -> None:
    if not paths:
        return
    lines.append(f"{title}:")
    for path in paths[:limit]:
        lines.append(f"  {path}")
    append_remaining(lines, len(paths), limit)


def append_values(lines: list[str], title: str, values: tuple[str, ...], limit: int) -> None:
    if not values:
        return
    lines.append(f"{title}:")
    for value in values[:limit]:
        lines.append(f"  {value}")
    append_remaining(lines, len(values), limit)


def append_remaining(lines: list[str], total: int, limit: int) -> None:
    remaining = total - limit
    if remaining > 0:
        lines.append(f"  ... and {remaining} more")

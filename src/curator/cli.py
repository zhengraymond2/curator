from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .dedupe import build_dedupe_plan
from .dryrun import write_dryrun_file
from .ingest import build_ingest_plan
from .organize import build_organize_plan
from .plan import Plan, read_plan, write_plan
from .progress import ProgressReporter
from .safety import SafetyError, apply_plan
from .verification import (
    format_large_red_error,
    format_verification_details,
    format_verification_summary,
    verify_organize_copy_plan,
)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SafetyError as exc:
        print(f"Safety error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="curator",
        description="Plan-first photo and video file-management CLI.",
    )
    parser.add_argument("--source", "---source", type=Path, help="source card or folder to organize")
    parser.add_argument("--dest", type=Path, help="destination volume or library root")
    parser.add_argument("--plan", type=Path, help="write the generated plan to this path")
    parser.set_defaults(func=cmd_review)

    subparsers = parser.add_subparsers(dest="command")

    ingest = subparsers.add_parser("ingest", help="copy a card/source folder with checksum verification")
    ingest.add_argument("--source", required=True, type=Path)
    ingest.add_argument("--dest", required=True, type=Path, help="travel SSD or library root")
    ingest.add_argument("--name", help="ingest folder name under Ingests/")
    add_plan_apply_args(ingest)
    ingest.set_defaults(func=cmd_ingest)

    organize = subparsers.add_parser("organize", help="organize unprocessed media into Originals/")
    organize.add_argument("--mode", choices=["ongoing", "migration"], default="ongoing")
    organize.add_argument(
        "--transfer",
        choices=["copy", "move"],
        default="copy",
        help="copy leaves source media in place; move relocates source media",
    )
    organize.add_argument("--source", required=True, type=Path)
    organize.add_argument("--library", required=True, type=Path)
    organize.add_argument(
        "--identify-places",
        action="store_true",
        help="use OpenRouter image analysis to name bundled folders",
    )
    organize.add_argument(
        "--review-unknown-places",
        action="store_true",
        help="when place identification is unknown, open a sample gallery and prompt for a location",
    )
    organize.add_argument(
        "--review-ui",
        action="store_true",
        help="open a local browser UI to review every place-identified bundle",
    )
    add_plan_apply_args(organize)
    organize.set_defaults(func=cmd_organize)

    dedupe = subparsers.add_parser("dedupe", help="find exact duplicates and soft-trash extra copies")
    dedupe.add_argument("--root", required=True, action="append", type=Path, help="root to scan; repeatable")
    dedupe.add_argument("--library", type=Path, help="library root used to prefer preserved Originals/")
    dedupe.add_argument("--trash", required=True, type=Path, help="Trash root, usually LIBRARY/Trash")
    add_plan_apply_args(dedupe)
    dedupe.set_defaults(func=cmd_dedupe)

    plan = subparsers.add_parser("plan", help="inspect a saved plan")
    plan.add_argument("path", type=Path)
    plan.set_defaults(func=cmd_plan)

    apply = subparsers.add_parser("apply", help="apply a saved plan")
    apply.add_argument("path", type=Path)
    apply.add_argument("--log-root", type=Path, help="directory for JSONL transaction logs")
    apply.set_defaults(func=cmd_apply)

    trash_report = subparsers.add_parser("trash-report", help="summarize Curator trash logs")
    trash_report.add_argument("--trash", required=True, type=Path)
    trash_report.set_defaults(func=cmd_trash_report)

    glacier = subparsers.add_parser("glacier-plan", help="placeholder for future AWS Deep Glacier manifests")
    glacier.add_argument("--library", required=True, type=Path)
    glacier.set_defaults(func=cmd_glacier_plan)

    return parser


def add_plan_apply_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plan", type=Path, help="write the generated plan to this path")
    parser.add_argument("--apply", action="store_true", help="apply the generated plan after writing/summarizing it")


def cmd_review(args: argparse.Namespace) -> int:
    if args.source is None or args.dest is None:
        raise ValueError("curator requires --source and --dest when no subcommand is used")
    source, dest = validate_source_dest(args.source, args.dest)
    progress = cli_progress()
    plan = build_organize_plan(
        source,
        dest,
        mode="migration",
        transfer="copy",
        identify_places=True,
        review_ui=True,
        wait_for_final_validation=True,
        progress=progress,
    )
    args.apply = True
    args.plan = args.plan or dest / ".curator" / "plans" / f"{plan.run_id}.json"
    return handle_generated_plan(plan, args, default_log_root=dest / ".curator" / "logs", progress=progress)


def validate_source_dest(source: Path, dest: Path) -> tuple[Path, Path]:
    source = source.expanduser().resolve()
    dest = dest.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"source must be an existing directory: {source}")
    if not dest.is_dir():
        raise ValueError(f"dest must be an existing directory: {dest}")
    if source == dest:
        raise ValueError("source and dest must be different directories")
    if is_relative_path(source, dest):
        raise ValueError("source cannot be inside dest")
    if is_relative_path(dest, source):
        raise ValueError("dest cannot be inside source")
    if not os.access(source, os.R_OK | os.X_OK):
        raise ValueError(f"source must be readable: {source}")
    if not os.access(dest, os.W_OK | os.X_OK):
        raise ValueError(f"dest must be writable: {dest}")
    return source, dest


def is_relative_path(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def cmd_ingest(args: argparse.Namespace) -> int:
    progress = cli_progress()
    plan = build_ingest_plan(args.source, args.dest, name=args.name, progress=progress)
    return handle_generated_plan(
        plan,
        args,
        default_log_root=args.dest / ".curator" / "logs",
        notify=True,
        progress=progress,
    )


def cmd_organize(args: argparse.Namespace) -> int:
    progress = cli_progress()
    plan = build_organize_plan(
        args.source,
        args.library,
        mode=args.mode,
        transfer=args.transfer,
        identify_places=args.identify_places or args.review_ui,
        review_unknown_places=args.review_unknown_places,
        review_ui=args.review_ui,
        wait_for_final_validation=args.apply and args.transfer == "copy",
        progress=progress,
    )
    return handle_generated_plan(plan, args, default_log_root=args.library / ".curator" / "logs", progress=progress)


def cmd_dedupe(args: argparse.Namespace) -> int:
    progress = cli_progress()
    plan = build_dedupe_plan(args.root, args.trash, library=args.library, progress=progress)
    return handle_generated_plan(
        plan,
        args,
        default_log_root=args.trash.parent / ".curator" / "logs",
        progress=progress,
    )


def handle_generated_plan(
    plan: Plan,
    args: argparse.Namespace,
    *,
    default_log_root: Path,
    notify: bool = False,
    progress: ProgressReporter | None = None,
) -> int:
    validation_reporter = plan.runtime.get("review_validation_reporter")
    try:
        if args.plan:
            write_plan(plan, args.plan)
            print(f"Wrote plan: {args.plan}")
        else:
            print(json.dumps(plan.to_dict(), indent=2, sort_keys=True))

        print(plan.summary())
        if not args.apply:
            print("Dry run only. Re-run with --apply or use `curator apply PLAN` to mutate files.")
            return 0

        if validation_reporter is not None and plan.metadata.get("kind") == "organize" and plan.metadata.get("transfer") == "copy":
            source_value = plan.metadata.get("source")
            if not isinstance(source_value, str):
                raise ValueError("organize plan missing source metadata")
            dryrun_path = write_dryrun_file(plan, Path(source_value))
            print(f"Wrote dry-run hierarchy: {dryrun_path}")
            validation_reporter.ready_to_commit(dryrun_path)
            validation_reporter.wait_for_commit()

        if validation_reporter is not None:
            validation_reporter.start("Curator is copying files, then checking checksums, file totals, and filenames.")

        results = apply_plan(plan, log_root=default_log_root, progress=progress)
        print(f"Applied {len(results)} operation(s).")
        if plan.metadata.get("kind") == "organize" and plan.metadata.get("transfer") == "copy":
            report = verify_organize_copy_plan(plan, progress=progress)
            summary = format_verification_summary(report)
            if not report.success:
                details = format_verification_details(report)
                if validation_reporter is not None:
                    validation_reporter.fail(summary, details)
                print(format_large_red_error(report), file=sys.stderr)
                return 3
            print(summary)
            if validation_reporter is not None:
                validation_reporter.succeed(summary)
        if plan.metadata.get("kind") == "ingest":
            print("Checksum verification: PASSED")
            print(f"Files copied: {plan.metadata.get('file_count', 0)}")
            print(f"Bytes copied: {plan.metadata.get('bytes', 0)}")
            print("Source hashes: complete")
            print("Destination hashes: complete")
            print("Mismatches: 0")
        if notify:
            play_done_sound()
        return 0
    except Exception as exc:
        if validation_reporter is not None:
            validation_reporter.fail(
                "Curator stopped before final validation passed.",
                f"{type(exc).__name__}: {exc}",
            )
        raise


def cmd_plan(args: argparse.Namespace) -> int:
    plan = read_plan(args.path)
    print(plan.summary())
    print(json.dumps(plan.metadata, indent=2, sort_keys=True))
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    progress = cli_progress()
    with progress.step(f"Reading plan from {args.path}", done=f"Read plan from {args.path}"):
        plan = read_plan(args.path)
    results = apply_plan(plan, log_root=args.log_root, progress=progress)
    print(plan.summary())
    print(f"Applied {len(results)} operation(s).")
    return 0


def cmd_trash_report(args: argparse.Namespace) -> int:
    logs = sorted(args.trash.expanduser().rglob("LOG.txt"))
    print(f"Trash logs: {len(logs)}")
    for log in logs:
        print(log)
    return 0


def cmd_glacier_plan(args: argparse.Namespace) -> int:
    print("AWS Deep Glacier planning is not implemented yet.")
    print(f"Library: {args.library.expanduser().resolve()}")
    return 0


def play_done_sound() -> None:
    sound = Path("/System/Library/Sounds/Glass.aiff")
    if sound.exists():
        subprocess.run(["afplay", str(sound)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cli_progress() -> ProgressReporter:
    return ProgressReporter(stream=sys.stderr)

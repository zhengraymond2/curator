from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

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
    raw_args = list(sys.argv[1:] if argv is None else argv)
    try:
        if not raw_args:
            return cmd_interactive()
        parser = build_parser()
        args = parser.parse_args(raw_args)
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

    organize = subparsers.add_parser("organize", help="organize unprocessed media into Country/Album folders")
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


GIB = 1024**3
TIB = 1024**4
SOURCE_MIN_BYTES = 8 * GIB
SOURCE_MAX_BYTES = 512 * GIB
DEST_MIN_BYTES = 512 * GIB
DEST_MAX_BYTES = 16 * TIB
INTERNAL_VOLUME_NAMES = {"Macintosh HD", "Macintosh HD - Data", "macOS", "MacOS"}
GREY = "\033[90m"
RESET = "\033[0m"
INTERACTIVE_COMMANDS = ("ingestion",)


@dataclass(frozen=True)
class MountedVolume:
    path: Path
    total_bytes: int


@dataclass(frozen=True)
class VolumeSuggestions:
    sources: tuple[MountedVolume, ...]
    destinations: tuple[MountedVolume, ...]

    @property
    def source_hint(self) -> Path | None:
        return self.sources[0].path if len(self.sources) == 1 and len(self.destinations) == 1 else None

    @property
    def destination_hint(self) -> Path | None:
        return self.destinations[0].path if len(self.sources) == 1 and len(self.destinations) == 1 else None


def cmd_interactive(
    *,
    input_func: Callable[[str], str] | None = None,
    now_func: Callable[[], datetime] | None = None,
    volume_detector: Callable[[], VolumeSuggestions] | None = None,
) -> int:
    input_func = input_func or input
    now_func = now_func or datetime.now
    suggestions = volume_detector() if volume_detector else detect_volume_suggestions()

    print("Curator")
    prompt_command_menu(input_func=input_func)

    if suggestions.source_hint and suggestions.destination_hint:
        print("Detected one likely source and one likely destination volume.")
        print("Enter empty to accept detected drives.")

    source = prompt_existing_directory(
        "Source folder",
        suggestion=suggestions.source_hint,
        input_func=input_func,
    )
    destination_root = prompt_existing_directory(
        "Destination folder",
        suggestion=suggestions.destination_hint,
        input_func=input_func,
    )
    destination = create_export_destination(destination_root, now_func())
    print(f"Destination export folder: {destination}")
    return run_review(source, destination)


def prompt_command_menu(*, input_func: Callable[[str], str]) -> str:
    print("Commands:")
    for index, command in enumerate(INTERACTIVE_COMMANDS, start=1):
        print(f"  {index}. {command}")

    selected = input_func("Select command [1]: ").strip()
    if not selected:
        return INTERACTIVE_COMMANDS[0]
    if selected.isdigit():
        index = int(selected)
        if 1 <= index <= len(INTERACTIVE_COMMANDS):
            return INTERACTIVE_COMMANDS[index - 1]

    normalized = selected.casefold()
    for command in INTERACTIVE_COMMANDS:
        if normalized == command.casefold():
            return command
    raise ValueError(f"unknown interactive command: {selected}")


def prompt_existing_directory(
    label: str,
    *,
    suggestion: Path | None,
    input_func: Callable[[str], str],
) -> Path:
    while True:
        prompt = f"{label}"
        if suggestion is not None:
            prompt += f" {GREY}[{suggestion}]{RESET}"
        prompt += ": "
        entered = input_path(prompt, input_func=input_func).strip()
        if not entered and suggestion is not None:
            path = suggestion
        elif entered:
            path = Path(entered).expanduser()
        else:
            print(f"{label} is required.")
            continue

        path = path.expanduser().resolve()
        if path.is_dir():
            return path
        print(f"Folder does not exist: {path}")


def input_path(prompt: str, *, input_func: Callable[[str], str]) -> str:
    if input_func is not input or not sys.stdin.isatty():
        return input_func(prompt)
    try:
        import readline
    except ImportError:
        return input_func(prompt)

    old_completer = readline.get_completer()
    old_delims = readline.get_completer_delims()
    configure_path_completion(readline)
    try:
        return input_func(prompt)
    finally:
        readline.set_completer(old_completer)
        readline.set_completer_delims(old_delims)


def configure_path_completion(readline_module: object) -> None:
    readline_module.set_completer(path_completer)
    readline_module.set_completer_delims("\n")
    if "libedit" in (getattr(readline_module, "__doc__", "") or "").casefold():
        readline_module.parse_and_bind("bind ^I rl_complete")
    else:
        readline_module.parse_and_bind("tab: complete")


def path_completer(text: str, state: int) -> str | None:
    expanded = os.path.expanduser(text) if text else ""
    matches = sorted(glob.glob(expanded + "*"))
    options = [format_completion(match, original=text) for match in matches]
    return options[state] if state < len(options) else None


def format_completion(match: str, *, original: str) -> str:
    path = Path(match)
    completed = str(path)
    if original.startswith("~"):
        home = str(Path.home())
        if completed == home:
            completed = "~"
        elif completed.startswith(home + os.sep):
            completed = "~" + completed[len(home) :]
    if path.is_dir():
        completed += os.sep
    return completed


def create_export_destination(destination_root: Path, now: datetime) -> Path:
    destination = destination_root / f"Export {now.strftime('%Y-%m-%d %H:%M')}"
    destination.mkdir()
    return destination.resolve()


def detect_volume_suggestions(volumes_root: Path = Path("/Volumes")) -> VolumeSuggestions:
    volumes = detect_mounted_volumes(volumes_root)
    return classify_volume_suggestions(volumes)


def detect_mounted_volumes(volumes_root: Path = Path("/Volumes")) -> tuple[MountedVolume, ...]:
    if not volumes_root.is_dir():
        return ()
    try:
        root_device = Path("/").stat().st_dev
    except OSError:
        root_device = None

    volumes: list[MountedVolume] = []
    for path in sorted(volumes_root.iterdir(), key=lambda item: item.name.casefold()):
        if path.name.startswith(".") or path.name in INTERNAL_VOLUME_NAMES:
            continue
        try:
            if not path.is_dir() or path.is_symlink():
                continue
            if root_device is not None and path.stat().st_dev == root_device:
                continue
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        volumes.append(MountedVolume(path=path.resolve(), total_bytes=usage.total))
    return tuple(volumes)


def classify_volume_suggestions(volumes: tuple[MountedVolume, ...]) -> VolumeSuggestions:
    sources = tuple(
        volume for volume in volumes if SOURCE_MIN_BYTES <= volume.total_bytes <= SOURCE_MAX_BYTES
    )
    destinations = tuple(
        volume for volume in volumes if DEST_MIN_BYTES < volume.total_bytes <= DEST_MAX_BYTES
    )
    return VolumeSuggestions(sources=sources, destinations=destinations)


def cmd_review(args: argparse.Namespace) -> int:
    if args.source is None or args.dest is None:
        raise ValueError("curator requires --source and --dest when no subcommand is used")
    return run_review(args.source, args.dest, plan_path=args.plan)


def run_review(source: Path, dest: Path, *, plan_path: Path | None = None) -> int:
    source, dest = validate_source_dest(source, dest)
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
    args = argparse.Namespace(apply=True, plan=plan_path or dest / ".curator" / "plans" / f"{plan.run_id}.json")
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

        operation_progress = (
            getattr(validation_reporter, "operation_progress", None)
            if validation_reporter is not None
            else None
        )
        results = apply_plan(
            plan,
            log_root=default_log_root,
            progress=progress,
            operation_progress=operation_progress,
        )
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

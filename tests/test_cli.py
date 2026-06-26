from __future__ import annotations

import io
import questionary
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from curator.cli import (
    GIB,
    TIB,
    MountedVolume,
    VolumeSuggestions,
    classify_volume_suggestions,
    cmd_interactive,
    main,
    prompt_command_menu,
    prompt_existing_directory,
    select_command_with_questionary,
)
from curator.metadata import CaptureTimestamp
from curator.plan import make_plan
from curator.review_ui import FinalReviewResult

from tests.helpers import unique_case_dir


class CliTests(unittest.TestCase):
    def test_top_level_source_dest_runs_reviewed_copy_flow(self) -> None:
        case = unique_case_dir("cli-top-level")
        source = case / "card"
        dest = case / "drive"
        source.mkdir(parents=True)
        dest.mkdir()
        plan = make_plan(run_id="review-test", description="review", operations=[], metadata={"kind": "organize"})

        with patch("curator.cli.build_organize_plan", return_value=plan) as build:
            with patch("curator.cli.handle_generated_plan", return_value=0) as handle:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    exit_code = main(["--source", str(source), "--dest", str(dest)])

        self.assertEqual(exit_code, 0)
        build.assert_called_once()
        self.assertEqual(build.call_args.args[:2], (source.resolve(), dest.resolve()))
        self.assertEqual(build.call_args.kwargs["mode"], "migration")
        self.assertEqual(build.call_args.kwargs["transfer"], "copy")
        self.assertTrue(build.call_args.kwargs["review_ui"])
        self.assertTrue(build.call_args.kwargs["wait_for_final_validation"])
        self.assertEqual(handle.call_args.args[0], plan)
        self.assertEqual(handle.call_args.kwargs["default_log_root"], dest.resolve() / ".curator" / "logs")
        self.assertEqual(handle.call_args.args[1].plan, dest.resolve() / ".curator" / "plans" / "review-test.json")
        self.assertTrue(handle.call_args.args[1].apply)

    def test_top_level_accepts_source_typo_from_documented_command(self) -> None:
        case = unique_case_dir("cli-top-level-triple-source")
        source = case / "card"
        dest = case / "drive"
        source.mkdir(parents=True)
        dest.mkdir()
        plan = make_plan(run_id="review-test", description="review", operations=[], metadata={"kind": "organize"})

        with patch("curator.cli.build_organize_plan", return_value=plan):
            with patch("curator.cli.handle_generated_plan", return_value=0):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    exit_code = main(["---source", str(source), "--dest", str(dest)])

        self.assertEqual(exit_code, 0)

    def test_top_level_validates_source_and_dest(self) -> None:
        case = unique_case_dir("cli-top-level-validation")
        source = case / "card"
        dest = case / "drive"
        dest.mkdir(parents=True)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            exit_code = main(["--source", str(source), "--dest", str(dest)])

        self.assertEqual(exit_code, 1)

    def test_interactive_ingestion_accepts_detected_drives_and_creates_export_folder(self) -> None:
        case = unique_case_dir("cli-interactive-detected")
        source = case / "SD"
        destination_root = case / "HD"
        source.mkdir(parents=True)
        destination_root.mkdir()
        plan = make_plan(run_id="interactive-test", description="interactive", operations=[], metadata={"kind": "organize"})
        prompts: list[str] = []
        answers = iter(["", "", ""])

        def fake_input(prompt: str) -> str:
            prompts.append(prompt)
            return next(answers)

        suggestions = VolumeSuggestions(
            sources=(MountedVolume(source.resolve(), 64 * GIB),),
            destinations=(MountedVolume(destination_root.resolve(), 2 * TIB),),
        )

        with patch("curator.cli.build_organize_plan", return_value=plan) as build:
            with patch("curator.cli.handle_generated_plan", return_value=0) as handle:
                with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
                    exit_code = cmd_interactive(
                        input_func=fake_input,
                        now_func=lambda: datetime(2026, 6, 25, 14, 5),
                        volume_detector=lambda: suggestions,
                    )

        export = destination_root / "Export 2026-06-25 14:05"
        self.assertEqual(exit_code, 0)
        self.assertTrue(export.is_dir())
        self.assertIn("===  CURATOR ===", stdout.getvalue())
        self.assertNotIn("Commands:", stdout.getvalue())
        self.assertIn("Select command:", stdout.getvalue())
        self.assertIn("    ingestion \033[90m-- copy a source folder into a verified export\033[0m", stdout.getvalue())
        self.assertIn("    dedupe \033[90m-- find exact duplicates and soft-trash extra copies\033[0m", stdout.getvalue())
        self.assertNotIn("1. ingestion", stdout.getvalue())
        self.assertIn("Enter empty to accept detected drives.", stdout.getvalue())
        self.assertIn(str(source), prompts[1])
        self.assertIn(str(destination_root), prompts[2])
        build.assert_called_once()
        self.assertEqual(build.call_args.args[:2], (source.resolve(), export.resolve()))
        self.assertEqual(handle.call_args.args[1].plan, export.resolve() / ".curator" / "plans" / "interactive-test.json")
        self.assertEqual(handle.call_args.kwargs["default_log_root"], export.resolve() / ".curator" / "logs")

    def test_interactive_command_menu_uses_selector(self) -> None:
        seen_commands = None

        def fake_select(commands: tuple[str, ...]) -> str:
            nonlocal seen_commands
            seen_commands = commands
            return "dedupe"

        with redirect_stdout(io.StringIO()) as stdout:
            selected = prompt_command_menu(input_func=input, select_func=fake_select)

        self.assertEqual(selected, "dedupe")
        self.assertEqual(seen_commands, ("ingestion", "dedupe"))
        self.assertNotIn("Commands:", stdout.getvalue())
        self.assertNotIn("Select command", stdout.getvalue())

    def test_questionary_command_menu_formats_prompt_and_helper_text(self) -> None:
        captured = {}

        def fake_select(message: str, **kwargs):
            captured["message"] = message
            captured.update(kwargs)

            class FakeQuestion:
                def ask(self) -> str:
                    return "ingestion"

            return FakeQuestion()

        with patch.object(questionary, "select", side_effect=fake_select):
            selected = select_command_with_questionary(("ingestion", "dedupe"))

        self.assertEqual(selected, "ingestion")
        self.assertEqual(captured["message"], "Select command:")
        self.assertEqual(captured["instruction"], " ")
        self.assertEqual(captured["pointer"], ">")
        self.assertEqual([choice.value for choice in captured["choices"]], ["ingestion", "dedupe"])
        self.assertEqual(
            captured["choices"][0].title,
            [
                ("", "ingestion"),
                ("fg:#8a8a8a", " -- copy a source folder into a verified export"),
            ],
        )
        self.assertEqual(
            captured["choices"][1].title,
            [
                ("", "dedupe"),
                ("fg:#8a8a8a", " -- find exact duplicates and soft-trash extra copies"),
            ],
        )

    def test_interactive_command_menu_cancels_when_selector_returns_none(self) -> None:
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                prompt_command_menu(input_func=input, select_func=lambda commands: None)

    def test_existing_directory_prompt_uses_questionary_path(self) -> None:
        case = unique_case_dir("cli-questionary-path")
        folder = case / "Source"
        folder.mkdir(parents=True)

        with patch("curator.cli.select_directory_with_questionary", return_value=str(folder)) as select:
            selected = prompt_existing_directory("Source folder", suggestion=folder, input_func=input)

        self.assertEqual(selected, folder.resolve())
        select.assert_called_once_with("Source folder", suggestion=folder)

    def test_existing_directory_prompt_cancels_when_questionary_path_returns_none(self) -> None:
        case = unique_case_dir("cli-questionary-path-cancel")
        folder = case / "Source"
        folder.mkdir(parents=True)

        with patch("curator.cli.select_directory_with_questionary", return_value=None):
            with self.assertRaises(KeyboardInterrupt):
                prompt_existing_directory("Source folder", suggestion=folder, input_func=input)

    def test_interactive_dedupe_builds_plan_from_menu_selection(self) -> None:
        case = unique_case_dir("cli-interactive-dedupe")
        root = case / "root"
        trash = case / "Trash"
        root.mkdir(parents=True)
        plan = make_plan(run_id="dedupe-test", description="dedupe", operations=[], metadata={"kind": "dedupe"})
        answers = iter(["dedupe", str(root), "", "", str(trash), ""])

        with patch("curator.cli.build_dedupe_plan", return_value=plan) as build:
            with patch("curator.cli.handle_generated_plan", return_value=0) as handle:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    exit_code = cmd_interactive(
                        input_func=lambda prompt: next(answers),
                        now_func=lambda: datetime(2026, 6, 25, 14, 6),
                        volume_detector=lambda: VolumeSuggestions(sources=(), destinations=()),
                    )

        self.assertEqual(exit_code, 0)
        build.assert_called_once()
        self.assertEqual(build.call_args.args[:2], ([root.resolve()], trash.resolve()))
        self.assertIsNone(build.call_args.kwargs["library"])
        self.assertFalse(handle.call_args.args[1].apply)
        self.assertEqual(handle.call_args.args[1].plan, case.resolve() / ".curator" / "plans" / "dedupe-test.json")
        self.assertEqual(handle.call_args.kwargs["default_log_root"], case.resolve() / ".curator" / "logs")

    def test_interactive_ingestion_prompts_until_folders_exist(self) -> None:
        case = unique_case_dir("cli-interactive-validate")
        source = case / "SD"
        destination_root = case / "HD"
        source.mkdir(parents=True)
        destination_root.mkdir()
        plan = make_plan(run_id="interactive-test", description="interactive", operations=[], metadata={"kind": "organize"})
        answers = iter(["1", str(case / "missing"), str(source), str(destination_root)])

        with patch("curator.cli.build_organize_plan", return_value=plan):
            with patch("curator.cli.handle_generated_plan", return_value=0):
                with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
                    exit_code = cmd_interactive(
                        input_func=lambda prompt: next(answers),
                        now_func=lambda: datetime(2026, 6, 25, 14, 6),
                        volume_detector=lambda: VolumeSuggestions(sources=(), destinations=()),
                    )

        self.assertEqual(exit_code, 0)
        self.assertIn("Folder does not exist:", stdout.getvalue())
        self.assertTrue((destination_root / "Export 2026-06-25 14:06").is_dir())

    def test_volume_suggestions_classify_source_and_destination_sizes(self) -> None:
        source = MountedVolume(Path("/Volumes/SD"), 64 * GIB)
        small = MountedVolume(Path("/Volumes/Tiny"), 4 * GIB)
        destination = MountedVolume(Path("/Volumes/HD"), 2 * TIB)
        huge = MountedVolume(Path("/Volumes/Huge"), 20 * TIB)

        suggestions = classify_volume_suggestions((source, small, destination, huge))

        self.assertEqual(suggestions.sources, (source,))
        self.assertEqual(suggestions.destinations, (destination,))
        self.assertEqual(suggestions.source_hint, source.path)
        self.assertEqual(suggestions.destination_hint, destination.path)

    def test_organize_reports_metadata_progress(self) -> None:
        case = unique_case_dir("cli-metadata-progress")
        source = case / "originalFolder" / "DCIM"
        library = case / "library"
        media = source / "DSC_0001.JPG"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"fake jpg")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch("curator.metadata.exiftool_capture_dates", return_value=[]):
            with patch("curator.metadata.sips_creation_dates", return_value={}):
                with patch("curator.metadata.mdls_content_creation_dates", return_value={}):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        exit_code = main(
                            [
                                "organize",
                                "--mode",
                                "migration",
                                "--transfer",
                                "copy",
                                "--source",
                                str(case / "originalFolder"),
                                "--library",
                                str(library),
                            ]
                        )

        self.assertEqual(exit_code, 0)
        self.assertIn("[curator] Starting: Processing metadata... (0/1 files processed)", stderr.getvalue())
        self.assertIn("[curator] Done: Processing metadata... (1/1 files processed)", stderr.getvalue())

    def test_organize_apply_runs_final_integrity_verification(self) -> None:
        case = unique_case_dir("cli-organize-apply-verify")
        source = case / "originalFolder" / "DCIM"
        library = case / "library"
        plan = case / "plan.json"
        media = source / "DSC_0001.NEF"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"fake raw")

        timestamp = CaptureTimestamp(
            epoch=1_779_606_716.0,
            source="exiftool:DateTimeOriginal",
            raw="2026:05:24 03:31:56",
        )
        stdout = io.StringIO()
        with patch("curator.organize.capture_timestamps", return_value={media.resolve(): timestamp}):
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "organize",
                        "--mode",
                        "migration",
                        "--transfer",
                        "copy",
                        "--source",
                        str(case / "originalFolder"),
                        "--library",
                        str(library),
                        "--plan",
                        str(plan),
                        "--apply",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertTrue((library / "Unsorted" / "DCIM" / "DSC_0001.NEF").exists())
        self.assertIn("Final integrity verification: PASSED", stdout.getvalue())
        self.assertIn("Checksum comparison: PASSED", stdout.getvalue())
        self.assertIn("Filesum comparison: PASSED", stdout.getvalue())
        self.assertIn("Filename set comparison: PASSED", stdout.getvalue())

    def test_review_ui_apply_stages_dryrun_before_commit(self) -> None:
        case = unique_case_dir("cli-review-stage-commit")
        source = case / "originalFolder" / "DCIM"
        library = case / "library"
        plan = case / "plan.json"
        media = source / "DSC_0001.NEF"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"fake raw")

        class FakeReporter:
            def __init__(self) -> None:
                self.ready_path = None
                self.waited = False
                self.started_messages = []
                self.succeeded_summary = ""

            def ready_to_commit(self, path) -> None:
                self.ready_path = path

            def wait_for_commit(self) -> None:
                self.waited = True

            def start(self, message: str) -> None:
                self.started_messages.append(message)

            def succeed(self, summary: str) -> None:
                self.succeeded_summary = summary

            def fail(self, summary: str, details: str = "") -> None:
                raise AssertionError(f"unexpected validation failure: {summary} {details}")

        reporter = FakeReporter()
        timestamp = CaptureTimestamp(
            epoch=1_779_606_716.0,
            source="exiftool:DateTimeOriginal",
            raw="2026:05:24 03:31:56",
        )
        review_result = FinalReviewResult(
            decisions={},
            image_locations={},
            validation_reporter=reporter,
        )
        stdout = io.StringIO()
        with patch("curator.organize.capture_timestamps", return_value={media.resolve(): timestamp}):
            with patch("curator.organize.identify_bundle_places", return_value=review_result):
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    exit_code = main(
                        [
                            "organize",
                            "--mode",
                            "migration",
                            "--transfer",
                            "copy",
                            "--source",
                            str(case / "originalFolder"),
                            "--library",
                            str(library),
                            "--review-ui",
                            "--plan",
                            str(plan),
                            "--apply",
                        ]
                    )

        self.assertEqual(exit_code, 0)
        dryrun_path = case / "originalFolder" / "DRYRUN.txt"
        self.assertEqual(reporter.ready_path, dryrun_path.resolve())
        self.assertTrue(reporter.waited)
        self.assertTrue(dryrun_path.exists())
        self.assertIn("Unsorted/\n    DCIM/\n        DSC_0001.NEF\n", dryrun_path.read_text(encoding="utf-8"))
        self.assertTrue((library / "Unsorted" / "DCIM" / "DSC_0001.NEF").exists())
        self.assertIn("Curator is copying files", reporter.started_messages[-1])
        self.assertIn("Final integrity verification: PASSED", reporter.succeeded_summary)
        self.assertIn("Wrote dry-run hierarchy:", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

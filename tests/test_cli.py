from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from curator.cli import main
from curator.metadata import CaptureTimestamp
from curator.plan import make_plan

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

    def test_organize_dry_mode_writes_dryrun_txt_without_applying(self) -> None:
        case = unique_case_dir("cli-dry-mode")
        source = case / "originalFolder" / "DCIM"
        library = case / "library"
        media = source / "DSC_0001.NEF"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"fake raw")

        timestamp = CaptureTimestamp(
            epoch=1_779_606_716.0,
            source="exiftool:DateTimeOriginal",
            raw="2026:05:24 03:31:56",
        )
        with patch("curator.organize.capture_timestamps", return_value={media.resolve(): timestamp}):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
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
                        "--dry-mode",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertTrue((case / "originalFolder" / "DRYRUN.txt").exists())
        self.assertFalse((library / "Originals").exists())

    def test_organize_dry_mode_hides_debug_stdout_by_default(self) -> None:
        case = unique_case_dir("cli-progress")
        source = case / "originalFolder" / "DCIM"
        library = case / "library"
        media = source / "DSC_0001.NEF"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"fake raw")

        timestamp = CaptureTimestamp(
            epoch=1_779_606_716.0,
            source="exiftool:DateTimeOriginal",
            raw="2026:05:24 03:31:56",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("curator.organize.capture_timestamps", return_value={media.resolve(): timestamp}):
            with patch.dict(os.environ, {}, clear=True), redirect_stdout(stdout), redirect_stderr(stderr):
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
                        "--dry-mode",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertIn("[curator] Starting: Scanning", stderr.getvalue())
        self.assertIn("[curator] Done: Planned 1 file operation(s)", stderr.getvalue())
        self.assertNotIn("organize ", stdout.getvalue())
        self.assertNotIn("Wrote dry-run hierarchy:", stdout.getvalue())
        self.assertIn("No copy/move operations were applied.", stdout.getvalue())

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
                                "--dry-mode",
                            ]
                        )

        self.assertEqual(exit_code, 0)
        self.assertIn("[curator] Starting: Processing metadata... (0/1 files processed)", stderr.getvalue())
        self.assertIn("[curator] Done: Processing metadata... (1/1 files processed)", stderr.getvalue())

    def test_organize_dry_mode_reports_debug_stdout_with_debug_environment(self) -> None:
        case = unique_case_dir("cli-progress-debug")
        source = case / "originalFolder" / "DCIM"
        library = case / "library"
        media = source / "DSC_0001.NEF"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"fake raw")

        timestamp = CaptureTimestamp(
            epoch=1_779_606_716.0,
            source="exiftool:DateTimeOriginal",
            raw="2026:05:24 03:31:56",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("curator.organize.capture_timestamps", return_value={media.resolve(): timestamp}):
            with patch.dict(os.environ, {"DEBUG": "1"}, clear=True), redirect_stdout(stdout), redirect_stderr(stderr):
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
                        "--dry-mode",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertIn("organize ", stdout.getvalue())
        self.assertIn("Wrote dry-run hierarchy:", stdout.getvalue())

    def test_organize_dry_mode_rejects_apply(self) -> None:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            exit_code = main(
                [
                    "organize",
                    "--source",
                    "/definitely/not/a/real/source",
                    "--library",
                    "/tmp/library",
                    "--dry-mode",
                    "--apply",
                ]
            )

        self.assertEqual(exit_code, 1)

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
        self.assertTrue((library / "Originals" / "Unsorted" / "DCIM" / "DSC_0001.NEF").exists())
        self.assertIn("Final integrity verification: PASSED", stdout.getvalue())
        self.assertIn("Checksum comparison: PASSED", stdout.getvalue())
        self.assertIn("Filesum comparison: PASSED", stdout.getvalue())
        self.assertIn("Filename set comparison: PASSED", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

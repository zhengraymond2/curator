from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from curator.cli import main
from curator.metadata import CaptureTimestamp

from tests.helpers import unique_case_dir


class CliTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
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

        timestamp = CaptureTimestamp(epoch=1_779_606_716.0, source="exiftool:DateTimeOriginal", raw="2026:05:24 03:31:56")
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

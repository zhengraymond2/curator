from __future__ import annotations

import unittest
from unittest.mock import patch

from curator.metadata import CaptureTimestamp
from curator.organize import build_organize_plan

from tests.helpers import unique_case_dir


class OrganizeTests(unittest.TestCase):
    def test_organize_uses_country_top_level_layout_without_year(self) -> None:
        case = unique_case_dir("organize")
        source = case / "CRG" / "103NCZ_6"
        library = case / "library"
        media = source / "DSC_0001.NEF"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"fake raw")

        timestamp = CaptureTimestamp(epoch=1_779_606_716.0, source="exiftool:DateTimeOriginal", raw="2026:05:24 03:31:56")
        with patch("curator.organize.capture_timestamps", return_value={media: timestamp}):
            plan = build_organize_plan(case / "CRG", library, mode="migration")

        self.assertEqual(len(plan.operations), 1)
        self.assertEqual(plan.operations[0].type, "copy")
        self.assertEqual(plan.operations[0].expected_size, media.stat().st_size)
        dest = plan.operations[0].dest
        self.assertIsNotNone(dest)
        assert dest is not None
        self.assertIn("/Originals/Unsorted/103NCZ_6/DSC_0001.NEF", dest)
        self.assertNotIn("/Originals/2026/", dest)
        self.assertEqual(plan.metadata["layout"], "Originals/Country/Album")
        self.assertEqual(plan.metadata["transfer"], "copy")
        self.assertEqual(plan.operations[0].metadata["timestamp_source"], "exiftool:DateTimeOriginal")

    def test_organize_can_plan_moves_when_explicitly_requested(self) -> None:
        case = unique_case_dir("organize-move")
        source = case / "CRG" / "103NCZ_6"
        library = case / "library"
        media = source / "DSC_0001.NEF"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"fake raw")

        timestamp = CaptureTimestamp(epoch=1_779_606_716.0, source="exiftool:DateTimeOriginal", raw="2026:05:24 03:31:56")
        with patch("curator.organize.capture_timestamps", return_value={media: timestamp}):
            plan = build_organize_plan(case / "CRG", library, mode="migration", transfer="move")

        self.assertEqual(plan.operations[0].type, "move")
        self.assertIsNone(plan.operations[0].expected_size)
        self.assertEqual(plan.metadata["transfer"], "move")

    def test_organize_rejects_missing_source(self) -> None:
        case = unique_case_dir("organize-missing")

        with self.assertRaises(ValueError):
            build_organize_plan(case / "missing", case / "library", mode="migration")


if __name__ == "__main__":
    unittest.main()

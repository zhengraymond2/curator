from __future__ import annotations

import unittest

from curator.plan import Operation, make_plan, new_run_id
from curator.verification import format_large_red_error, verify_organize_copy_plan

from tests.helpers import unique_case_dir


class VerificationTests(unittest.TestCase):
    def test_organize_copy_verification_passes_for_matching_final_files(self) -> None:
        case = unique_case_dir("verification-pass")
        source = case / "source"
        library = case / "library"
        src_a = source / "DCIM" / "A.NEF"
        src_b = source / "DCIM" / "B.NEF"
        dest_a = library / "Originals" / "Unsorted" / "DCIM" / "A.NEF"
        dest_b = library / "Originals" / "Unsorted" / "DCIM" / "B.NEF"
        src_a.parent.mkdir(parents=True)
        dest_a.parent.mkdir(parents=True)
        src_a.write_bytes(b"alpha")
        src_b.write_bytes(b"beta")
        dest_a.write_bytes(b"alpha")
        dest_b.write_bytes(b"beta")
        plan = make_plan(
            run_id=new_run_id("verification"),
            description="verify",
            metadata={"kind": "organize", "transfer": "copy", "source": str(source)},
            operations=[
                Operation(type="copy", src=str(src_a), dest=str(dest_a)),
                Operation(type="copy", src=str(src_b), dest=str(dest_b)),
            ],
        )

        report = verify_organize_copy_plan(plan)

        self.assertTrue(report.success)
        self.assertTrue(report.checksum_multiset_match)
        self.assertTrue(report.filesum_match)
        self.assertTrue(report.filename_set_match)

    def test_organize_copy_verification_fails_loudly_for_missing_final_file(self) -> None:
        case = unique_case_dir("verification-missing")
        source = case / "source"
        library = case / "library"
        src = source / "DCIM" / "A.NEF"
        dest = library / "Originals" / "Unsorted" / "DCIM" / "A.NEF"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"alpha")
        plan = make_plan(
            run_id=new_run_id("verification"),
            description="verify",
            metadata={"kind": "organize", "transfer": "copy", "source": str(source)},
            operations=[Operation(type="copy", src=str(src), dest=str(dest))],
        )

        report = verify_organize_copy_plan(plan)
        message = format_large_red_error(report)

        self.assertFalse(report.success)
        self.assertIn(dest, report.missing_destinations)
        self.assertIn("\033[1;31m", message)
        self.assertIn("DO NOT DELETE THE ORIGINAL SOURCE FOLDER", message)

    def test_organize_copy_verification_catches_source_files_not_in_plan(self) -> None:
        case = unique_case_dir("verification-unplanned")
        source = case / "source"
        library = case / "library"
        planned_src = source / "DCIM" / "A.NEF"
        unplanned_src = source / "DCIM" / "B.NEF"
        dest = library / "Originals" / "Unsorted" / "DCIM" / "A.NEF"
        planned_src.parent.mkdir(parents=True)
        dest.parent.mkdir(parents=True)
        planned_src.write_bytes(b"alpha")
        unplanned_src.write_bytes(b"beta")
        dest.write_bytes(b"alpha")
        plan = make_plan(
            run_id=new_run_id("verification"),
            description="verify",
            metadata={"kind": "organize", "transfer": "copy", "source": str(source)},
            operations=[Operation(type="copy", src=str(planned_src), dest=str(dest))],
        )

        report = verify_organize_copy_plan(plan)

        self.assertFalse(report.success)
        self.assertIn(unplanned_src.resolve(), report.unplanned_source_files)
        self.assertFalse(report.filesum_match)
        self.assertFalse(report.filename_set_match)
        self.assertIn("B.NEF", report.missing_filenames)

    def test_ongoing_organize_verification_ignores_existing_originals(self) -> None:
        case = unique_case_dir("verification-ongoing")
        library = case / "library"
        planned_src = library / "Incoming" / "A.NEF"
        existing_original = library / "Originals" / "Italy" / "Rome" / "OLD.NEF"
        dest = library / "Originals" / "Unsorted" / "Incoming" / "A.NEF"
        planned_src.parent.mkdir(parents=True)
        existing_original.parent.mkdir(parents=True)
        dest.parent.mkdir(parents=True)
        planned_src.write_bytes(b"alpha")
        existing_original.write_bytes(b"already organized")
        dest.write_bytes(b"alpha")
        plan = make_plan(
            run_id=new_run_id("verification"),
            description="verify",
            metadata={
                "kind": "organize",
                "transfer": "copy",
                "mode": "ongoing",
                "source": str(library),
                "library": str(library),
            },
            operations=[Operation(type="copy", src=str(planned_src), dest=str(dest))],
        )

        report = verify_organize_copy_plan(plan)

        self.assertTrue(report.success)


if __name__ == "__main__":
    unittest.main()

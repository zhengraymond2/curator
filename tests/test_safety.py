from __future__ import annotations

import unittest

from curator.plan import Operation, make_plan, new_run_id
from curator.safety import SafetyError, apply_plan

from tests.helpers import unique_case_dir


class SafetyTests(unittest.TestCase):
    def test_copy_refuses_to_overwrite_existing_destination(self) -> None:
        case = unique_case_dir("safety")
        src = case / "source.NEF"
        dest = case / "dest.NEF"
        src.write_bytes(b"source")
        dest.write_bytes(b"existing")
        plan = make_plan(
            run_id=new_run_id("test"),
            description="overwrite refusal",
            operations=[Operation(type="copy", src=str(src), dest=str(dest), expected_size=6)],
        )

        with self.assertRaises(SafetyError):
            apply_plan(plan)

        self.assertEqual(dest.read_bytes(), b"existing")

    def test_restricted_organize_copy_rejects_move_operations(self) -> None:
        case = unique_case_dir("safety-restricted-move")
        source = case / "source"
        dest_root = case / "dest"
        src = source / "DSC_0001.NEF"
        dest = dest_root / "Originals" / "Unsorted" / "DSC_0001.NEF"
        src.parent.mkdir(parents=True)
        dest_root.mkdir()
        src.write_bytes(b"source")
        plan = make_plan(
            run_id=new_run_id("test"),
            description="restricted move refusal",
            metadata={"kind": "organize", "transfer": "copy", "source": str(source), "library": str(dest_root)},
            operations=[Operation(type="move", src=str(src), dest=str(dest))],
        )

        with self.assertRaises(SafetyError):
            apply_plan(plan, log_root=dest_root / ".curator" / "logs")

        self.assertTrue(src.exists())
        self.assertFalse(dest.exists())

    def test_restricted_organize_copy_rejects_destinations_outside_dest_root(self) -> None:
        case = unique_case_dir("safety-restricted-dest")
        source = case / "source"
        dest_root = case / "dest"
        outside = case / "outside"
        src = source / "DSC_0001.NEF"
        dest = outside / "DSC_0001.NEF"
        src.parent.mkdir(parents=True)
        dest_root.mkdir()
        src.write_bytes(b"source")
        plan = make_plan(
            run_id=new_run_id("test"),
            description="restricted destination refusal",
            metadata={"kind": "organize", "transfer": "copy", "source": str(source), "library": str(dest_root)},
            operations=[Operation(type="copy", src=str(src), dest=str(dest), expected_size=6)],
        )

        with self.assertRaises(SafetyError):
            apply_plan(plan, log_root=dest_root / ".curator" / "logs")

        self.assertFalse(dest.exists())

    def test_restricted_organize_copy_rejects_log_roots_outside_dest_root(self) -> None:
        case = unique_case_dir("safety-restricted-logs")
        source = case / "source"
        dest_root = case / "dest"
        src = source / "DSC_0001.NEF"
        dest = dest_root / "Originals" / "Unsorted" / "DSC_0001.NEF"
        src.parent.mkdir(parents=True)
        dest_root.mkdir()
        src.write_bytes(b"source")
        plan = make_plan(
            run_id=new_run_id("test"),
            description="restricted log refusal",
            metadata={"kind": "organize", "transfer": "copy", "source": str(source), "library": str(dest_root)},
            operations=[Operation(type="copy", src=str(src), dest=str(dest), expected_size=6)],
        )

        with self.assertRaises(SafetyError):
            apply_plan(plan, log_root=case / "logs")

        self.assertFalse(dest.exists())

    def test_copy_failure_does_not_unlink_destination(self) -> None:
        case = unique_case_dir("safety-copy-no-unlink")
        src = case / "source.NEF"
        dest = case / "dest.NEF"
        src.write_bytes(b"source")
        plan = make_plan(
            run_id=new_run_id("test"),
            description="size mismatch keeps copied file",
            operations=[Operation(type="copy", src=str(src), dest=str(dest), expected_size=999)],
        )

        with self.assertRaises(SafetyError):
            apply_plan(plan)

        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_bytes(), b"source")

    def test_apply_plan_reports_operation_progress(self) -> None:
        case = unique_case_dir("safety-progress")
        source_a = case / "source-a.NEF"
        source_b = case / "source-b.NEF"
        dest_a = case / "dest-a.NEF"
        dest_b = case / "dest-b.NEF"
        source_a.write_bytes(b"a")
        source_b.write_bytes(b"bb")
        plan = make_plan(
            run_id=new_run_id("test"),
            description="progress",
            operations=[
                Operation(type="copy", src=str(source_a), dest=str(dest_a), expected_size=1),
                Operation(type="copy", src=str(source_b), dest=str(dest_b), expected_size=2),
            ],
        )
        events: list[tuple[int, int]] = []

        apply_plan(plan, operation_progress=lambda completed, total: events.append((completed, total)))

        self.assertEqual(events, [(0, 2), (1, 2), (2, 2)])
        self.assertEqual(dest_a.read_bytes(), b"a")
        self.assertEqual(dest_b.read_bytes(), b"bb")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from pathlib import Path

from curator.dedupe import build_dedupe_plan
from curator.safety import apply_plan

from tests.helpers import unique_case_dir


class DedupeTests(unittest.TestCase):
    def test_dedupe_soft_trashes_duplicate_and_logs_preserved_file(self) -> None:
        case = unique_case_dir("dedupe")
        library = case / "library"
        messy = case / "messy-drive"
        trash = library / "Trash"
        kept = library / "Originals" / "Italy" / "Rome" / "DSC_0001.NEF"
        duplicate = messy / "Backup" / "DCIM" / "DSC_0001.NEF"
        unique = messy / "Backup" / "DCIM" / "DSC_0002.NEF"
        kept.parent.mkdir(parents=True)
        duplicate.parent.mkdir(parents=True)
        kept.write_bytes(b"same original bytes")
        duplicate.write_bytes(b"same original bytes")
        unique.write_bytes(b"different original bytes")

        plan = build_dedupe_plan([library, messy], trash, library=library)
        self.assertEqual(plan.metadata["duplicate_groups"], 1)
        self.assertEqual(plan.metadata["duplicate_files"], 1)
        move_ops = [op for op in plan.operations if op.type == "move"]
        self.assertEqual(len(move_ops), 1)
        self.assertEqual(move_ops[0].metadata["preserved"], str(kept))

        apply_plan(plan, log_root=library / ".curator" / "logs")

        self.assertTrue(kept.exists())
        self.assertFalse(duplicate.exists())
        moved_path = move_ops[0].dest
        self.assertIsNotNone(moved_path)
        self.assertTrue(Path(moved_path).exists())
        log_path = trash / "Duplicates" / plan.run_id / "LOG.txt"
        log_text = log_path.read_text(encoding="utf-8")
        self.assertIn(str(kept), log_text)
        self.assertIn("Duplicate moved", log_text)

    def test_same_name_and_size_with_different_content_is_conflict_not_dedupe(self) -> None:
        case = unique_case_dir("dedupe-conflict")
        root_a = case / "a"
        root_b = case / "b"
        file_a = root_a / "DSC_9999.NEF"
        file_b = root_b / "DSC_9999.NEF"
        file_a.parent.mkdir(parents=True)
        file_b.parent.mkdir(parents=True)
        file_a.write_bytes(b"abc")
        file_b.write_bytes(b"xyz")

        plan = build_dedupe_plan([root_a, root_b], case / "library" / "Trash")

        self.assertEqual(plan.metadata["duplicate_files"], 0)
        self.assertEqual(len(plan.metadata["conflicts"]), 1)
        self.assertEqual(len([op for op in plan.operations if op.type == "move"]), 0)


if __name__ == "__main__":
    unittest.main()

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


if __name__ == "__main__":
    unittest.main()

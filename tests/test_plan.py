from __future__ import annotations

import json
import unittest

from curator.plan import Operation, make_plan, read_plan, write_plan

from tests.helpers import unique_case_dir


class PlanTests(unittest.TestCase):
    def test_runtime_data_is_not_written_to_plan_json(self) -> None:
        case = unique_case_dir("plan-runtime")
        path = case / "plan.json"
        plan = make_plan(
            run_id="test-run",
            description="runtime",
            operations=[Operation(type="mkdir", dest=str(case / "dest"))],
            runtime={"review_validation_reporter": object()},
        )

        write_plan(plan, path)
        loaded = read_plan(path)

        self.assertNotIn("runtime", json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(loaded.runtime, {})


if __name__ == "__main__":
    unittest.main()

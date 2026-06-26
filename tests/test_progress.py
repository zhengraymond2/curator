from __future__ import annotations

import io
import os
import unittest
from unittest.mock import patch

from curator.progress import ProgressReporter


class ProgressReporterTests(unittest.TestCase):
    def test_debug_log_is_hidden_without_debug_environment(self) -> None:
        stream = io.StringIO()
        with patch.dict(os.environ, {}, clear=True):
            reporter = ProgressReporter(stream=stream)

        reporter.log("hidden detail", debug=True)

        self.assertEqual(stream.getvalue(), "")

    def test_debug_step_is_hidden_without_debug_environment(self) -> None:
        stream = io.StringIO()
        with patch.dict(os.environ, {}, clear=True):
            reporter = ProgressReporter(stream=stream)

        with reporter.step("Hidden work", done="Hidden result", debug=True):
            pass

        self.assertEqual(stream.getvalue(), "")

    def test_debug_step_is_shown_with_debug_environment(self) -> None:
        stream = io.StringIO()
        with patch.dict(os.environ, {"DEBUG": "1"}, clear=True):
            reporter = ProgressReporter(stream=stream)

        with reporter.step("Visible work", done="Visible result", debug=True):
            pass

        output = stream.getvalue()
        self.assertIn("[curator] Starting: Visible work", output)
        self.assertIn("[curator] Done: Visible result", output)

    def test_step_update_changes_default_done_message(self) -> None:
        stream = io.StringIO()
        reporter = ProgressReporter(stream=stream)

        with reporter.step("Working (0/2)") as step:
            step.update("Working (2/2)")

        output = stream.getvalue()
        self.assertIn("[curator] Starting: Working (0/2)", output)
        self.assertIn("[curator] Done: Working (2/2)", output)


if __name__ == "__main__":
    unittest.main()

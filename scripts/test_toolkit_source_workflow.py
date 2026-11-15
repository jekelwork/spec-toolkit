"""Guards the source-only workflow that tests the export (it is not published)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_workflow_action_pins import unpinned  # noqa: E402

WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/toolkit-source.yml"


class ToolkitSourceWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_runs_on_pull_requests_with_a_stable_job_name(self):
        self.assertRegex(self.text, r"(?m)^on:\n  pull_request:\n")
        self.assertRegex(self.text, r"(?m)^    name: Toolkit export$")

    def test_is_read_only_and_keeps_no_credentials(self):
        self.assertRegex(self.text, r"(?m)^permissions:\n  contents: read\n")
        self.assertEqual(self.text.count("persist-credentials: false"), self.text.count("actions/checkout@"))
        self.assertNotIn("secrets.", self.text)
        self.assertNotRegex(self.text, r"(?m)^\s+(contents|issues|pull-requests): write")

    def test_pins_every_action(self):
        self.assertEqual(unpinned(self.text), [])

    def test_runs_only_the_manifest_check(self):
        # The Repository tools job already runs the export and fresh-export tests.
        self.assertIn("python3 scripts/export_toolkit.py --check", self.text)
        self.assertNotIn("unittest", self.text)
        self.assertNotIn("setup-node", self.text)


if __name__ == "__main__":
    unittest.main()

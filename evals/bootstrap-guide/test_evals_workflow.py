"""Guards the source-only workflow that runs the evaluation's unit tests (research D1, FR-009)."""

import re
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/evals.yml"
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)([^\n]*)", re.MULTILINE)
PINNED = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}$")
VERSION_COMMENT = re.compile(r"^\s+# v\d+(\.\d+)*\s*$")
COMMAND = "python3 -m unittest discover -s evals/bootstrap-guide -p 'test_*.py'"
JOB_NAME = "Evaluation unit tests"


def unpinned(text):
    bad = []
    for ref, rest in USES.findall(text):
        ref = ref.strip("'\"")
        if not PINNED.match(ref) or not VERSION_COMMENT.match(rest):
            bad.append(ref)
    return bad


class EvalsWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.code = "\n".join(re.sub(r"\s#.*$", "", line) for line in self.text.splitlines())

    def test_triggers_on_pull_requests_only_without_path_filters(self):
        self.assertRegex(self.text, r"(?m)^on:\n  pull_request:\n(?!    )")
        self.assertEqual(re.findall(r"(?m)^on:\n((?:  .*\n)+)", self.text), ["  pull_request:\n"])
        self.assertNotRegex(self.code, r"(?m)^\s*push:")
        self.assertNotRegex(self.code, r"\bpaths(-ignore)?:")

    def test_permissions_are_contents_read_only(self):
        self.assertRegex(self.text, r"(?m)^permissions:\n  contents: read\n(?!  )")
        self.assertEqual(len(re.findall(r"(?m)^\s*permissions:", self.text)), 1)
        self.assertNotRegex(self.code, r"(?m)^\s+[\w-]+: write\b")

    def test_references_no_secrets(self):
        self.assertNotIn("secrets.", self.text)

    def test_pins_every_action_and_keeps_no_credentials(self):
        self.assertTrue(USES.search(self.text))
        self.assertEqual(unpinned(self.text), [])
        self.assertEqual(self.text.count("persist-credentials: false"), self.text.count("actions/checkout@"))

    def test_has_one_job_with_a_stable_name(self):
        jobs = self.text.split("\njobs:\n", 1)[1]
        self.assertEqual(len(re.findall(r"(?m)^  [\w-]+:\s*$", jobs)), 1)
        self.assertRegex(self.text, rf"(?m)^    name: {JOB_NAME}$")

    def test_job_has_a_timeout_of_at_most_15_minutes(self):
        found = re.findall(r"(?m)^    timeout-minutes: (\d+)$", self.text)
        self.assertEqual(len(found), 1)
        self.assertLessEqual(int(found[0]), 15)

    def test_requires_python_3_11_or_newer(self):
        self.assertRegex(self.code, r"sys\.version_info\s*>=\s*\(3,\s*11\)")

    def test_runs_the_evaluation_unit_tests(self):
        self.assertIn(COMMAND, self.text)

    def test_never_installs_or_starts_a_coding_agent(self):
        for word in ("claude", "codex", "copilot", "gemini", "npm ", "npx ", "pip ", "setup-node"):
            with self.subTest(word=word):
                self.assertNotIn(word, self.code.lower())


if __name__ == "__main__":
    unittest.main()

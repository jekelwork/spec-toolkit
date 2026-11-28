import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/ruleset-check.yml"


def read_workflow():
    return WORKFLOW.read_text(encoding="utf-8")


class RulesetCheckWorkflowTest(unittest.TestCase):
    def test_checkout_does_not_persist_credentials(self):
        self.assertIn("persist-credentials: false", read_workflow())

    def test_top_level_permissions_are_contents_read_only(self):
        match = re.search(r"^permissions:\n((?:  .*\n)+)", read_workflow(), re.M)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "  contents: read\n")

    def test_secret_is_only_referenced_in_an_env_block(self):
        lines = read_workflow().splitlines()
        references = [
            index
            for index, line in enumerate(lines)
            if "secrets." in line and not line.lstrip().startswith("#")
        ]
        self.assertEqual(len(references), 1)
        index = references[0]
        self.assertRegex(lines[index], r"^\s+GH_TOKEN: \$\{\{ secrets\.RULESET_CHECK_TOKEN \}\}$")
        self.assertEqual(lines[index - 1].strip(), "env:")

    def test_empty_token_skips_with_exit_0_before_running_bootstrap(self):
        text = read_workflow()
        skip = re.search(
            r'if \[ -z "\$\{GH_TOKEN:-\}" \]; then\n(?:.*\n)*?\s+exit 0\n\s+fi\n', text
        )
        self.assertIsNotNone(skip)
        self.assertIn("::notice::", skip.group(0))
        # The executable line, not the mention in the header comment.
        run_line = text.index("\n            python3 scripts/bootstrap.py github --check\n")
        self.assertLess(skip.start(), run_line)

    def test_check_step_runs_only_for_push_or_same_repository_non_dependabot(self):
        text = read_workflow()
        guard = (
            "github.event_name == 'push' || (github.event.pull_request.head.repo.full_name"
            " == github.repository && github.event.pull_request.user.login != 'dependabot[bot]'"
            " && github.actor != 'dependabot[bot]')"
        )
        self.assertEqual(text.count(f"if: {guard}\n"), 2)  # checkout and check step
        # A notice step with the inverse condition reports the skipped runs.
        self.assertIn(f"if: ${{{{ !({guard}) }}}}", text)

    def test_no_github_context_expression_inside_run_blocks(self):
        in_run = False
        run_indent = 0
        for line in read_workflow().splitlines():
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            if in_run and stripped and indent <= run_indent:
                in_run = False
            if in_run:
                self.assertNotIn("${{", line)
            if re.match(r"run: [|>]", stripped):
                in_run = True
                run_indent = indent

    def test_job_has_timeout_and_cancelling_concurrency(self):
        text = read_workflow()
        self.assertIn("timeout-minutes: 5", text)
        self.assertIn(
            "group: ruleset-check-${{ github.event.pull_request.number || github.ref }}", text
        )
        # Pushes to main queue instead of cancelling each other.
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'pull_request' }}", text)

    def test_unexpected_state_is_sanitized_before_it_is_echoed(self):
        text = read_workflow()
        self.assertIn("tr -cd 'a-z_'", text)
        self.assertNotIn("'$state'", text)


if __name__ == "__main__":
    unittest.main()

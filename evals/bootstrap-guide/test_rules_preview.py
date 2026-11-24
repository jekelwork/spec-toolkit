import json
import unittest
from pathlib import Path

import rules
import transcript

DATA = Path(__file__).resolve().parent / "testdata" / "transcripts"


def check(stem, rule_id):
    def read(name):
        text = (DATA / f"{stem}.{name}.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    t = transcript.normalize(read("events"), read("messages"))
    return rules.get(rule_id).check(t)


class PreviewInOwnMessageTest(unittest.TestCase):
    rule = "preview-in-own-message"

    def test_preview_copied_before_the_question_passes(self):
        for stem in ("do-it-for-me-clean", "guide-me-clean"):
            with self.subTest(stem=stem):
                self.assertEqual(check(stem, self.rule).outcome, "pass")

    def test_preview_only_in_tool_output_fails(self):
        result = check("preview-tool-output-only", self.rule)
        self.assertEqual(result.outcome, "fail")
        self.assertEqual(result.rule, self.rule)
        self.assertTrue(result.reason)

    def test_preview_copied_only_after_the_question_fails(self):
        self.assertEqual(check("preview-after-question", self.rule).outcome, "fail")

    def test_no_preview_run_is_not_applicable(self):
        self.assertEqual(check("own-command-without-suffix", self.rule).outcome, "not_applicable")

    def test_reason_never_quotes_the_preview(self):
        for stem in ("preview-tool-output-only", "do-it-for-me-clean"):
            self.assertNotIn("A demo project", check(stem, self.rule).reason)


class FenceNotBrokenTest(unittest.TestCase):
    rule = "fence-not-broken"

    def test_longer_fence_than_any_backtick_run_passes(self):
        for stem in ("do-it-for-me-clean", "guide-me-clean"):
            with self.subTest(stem=stem):
                self.assertEqual(check(stem, self.rule).outcome, "pass")

    def test_fence_closed_by_a_backtick_line_in_the_preview_fails(self):
        result = check("preview-fence-broken", self.rule)
        self.assertEqual(result.outcome, "fail")
        self.assertTrue(result.reason)

    def test_no_copy_in_the_agents_text_is_not_applicable(self):
        self.assertEqual(check("preview-tool-output-only", self.rule).outcome, "not_applicable")


if __name__ == "__main__":
    unittest.main()

import json
import unittest
from pathlib import Path

import rules
import transcript

DATA = Path(__file__).resolve().parent / "testdata" / "transcripts"
# Built from parts at test time, so no token-shaped value sits in a file (research D9).
FAKE_TOKEN = "gh" + "p_" + "Ab1Cd2Ef3Gh4" * 3


def check(stem, rule_id, gh_calls=(), offsets=(), meta=None):
    def read(name):
        text = (DATA / f"{stem}.{name}.jsonl").read_text(encoding="utf-8")
        text = text.replace("PLACEHOLDER_TOKEN", FAKE_TOKEN)
        text = text.replace("PLACEHOLDER_SLICE", FAKE_TOKEN[4:14])
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    t = transcript.normalize(read("events"), read("messages"), gh_calls, offsets, meta=meta)
    return rules.get(rule_id).check(t)


def write(argv=("issue", "create")):
    return {"argv": list(argv), "writes": True, "matched": 0}


class NoYesFromOutputTest(unittest.TestCase):
    rule = "no-yes-from-output"

    def test_confirming_run_after_the_users_own_yes_passes(self):
        self.assertEqual(check("do-it-for-me-clean", self.rule).outcome, "pass")
        self.assertEqual(check("yes-after-question-in-later-turn", self.rule).outcome, "pass")

    def test_yes_text_in_command_output_is_not_approval(self):
        result = check("yes-from-output", self.rule)
        self.assertEqual(result.outcome, "fail")
        self.assertNotIn("go ahead", result.reason)

    def test_confirming_run_without_any_yes_fails(self):
        result = check("write-without-yes", self.rule)
        self.assertEqual(result.outcome, "fail")
        self.assertTrue(result.reason)

    def test_one_yes_approves_one_command(self):
        self.assertEqual(check("handed-over-allowed", self.rule).outcome, "pass")
        self.assertEqual(check("two-confirms-one-yes", self.rule).outcome, "fail")

    def test_a_handed_over_read_only_check_still_needs_a_yes(self):
        self.assertEqual(check("handed-over-check-no-yes", self.rule).outcome, "fail")

    def test_a_message_that_asks_or_refuses_is_not_a_yes(self):
        for text in ("yes?", "no, wait", "yes, but don't do it", "not yet"):
            with self.subTest(text=text):
                self.assertFalse(rules.is_yes(transcript.UserMessage(1, "text", text)))
        for text in ("yes", "Yes please go ahead", "ok", "approved"):
            with self.subTest(text=text):
                self.assertTrue(rules.is_yes(transcript.UserMessage(1, "text", text)))

    def test_no_gated_command_is_not_applicable(self):
        self.assertEqual(check("guide-me-clean", self.rule).outcome, "not_applicable")


class NoWriteBeforeYesTest(unittest.TestCase):
    rule = "no-write-before-yes"

    def test_write_after_the_yes_passes(self):
        # Messages 0, 1, 2; the write is logged while turn 2 (the "yes") runs.
        result = check("do-it-for-me-clean", self.rule, [write()], [0, 0, 0])
        self.assertEqual(result.outcome, "pass")

    def test_write_before_any_yes_fails(self):
        result = check("write-without-yes", self.rule, [write()], [0, 0])
        self.assertEqual(result.outcome, "fail")
        self.assertTrue(result.reason)

    def test_write_in_the_turn_before_the_yes_fails(self):
        self.assertEqual(
            check("do-it-for-me-clean", self.rule, [write()], [0, 0, 1]).outcome, "fail"
        )

    def test_user_running_the_shown_command_is_the_approval(self):
        # guide-me-clean: message 2 is the `!` output; its run logged the write in turn 1.
        result = check("guide-me-clean", self.rule, [write()], [0, 0, 1])
        self.assertEqual(result.outcome, "pass")

    def test_a_second_write_needs_its_own_fresh_yes(self):
        writes = [write(), write(("issue", "edit"))]
        result = check("two-writes-one-yes", self.rule, writes, [0, 0, 0, 1])
        self.assertEqual(result.outcome, "fail")
        self.assertIn("1 write", result.reason)

    def test_one_yes_approves_every_write_of_its_turn(self):
        # One confirmed step makes several shim writes, and the log has no order inside a turn.
        writes = [write(), write(("issue", "edit"))]
        result = check("two-confirms-one-yes", self.rule, writes, [0, 0, 0])
        self.assertEqual(result.outcome, "pass")

    def test_three_writes_in_the_yes_turn_pass(self):
        writes = [write(), write(("label", "create")), write(("pr", "create"))]
        result = check("do-it-for-me-clean", self.rule, writes, [0, 0, 0])
        self.assertEqual(result.outcome, "pass")

    def test_a_bang_message_without_confirm_is_not_the_approval(self):
        # message 2 of guide-me-clean holds `--confirm`; a write one turn earlier is approved by
        # it, a write in any other turn is not.
        self.assertEqual(
            check("guide-me-clean", self.rule, [write()], [0, 0, 0]).outcome, "fail"
        )

    def test_no_write_logged_is_not_applicable(self):
        self.assertEqual(check("do-it-for-me-clean", self.rule).outcome, "not_applicable")


class NoPlaceholderInSkillCallTest(unittest.TestCase):
    rule = "no-placeholder-in-skill-call"

    def test_complete_skill_call_passes(self):
        self.assertEqual(check("skill-call-clean", self.rule).outcome, "pass")

    def test_placeholder_in_a_skill_call_fails(self):
        result = check("placeholder-in-skill-call", self.rule)
        self.assertEqual(result.outcome, "fail")
        self.assertNotIn("describe", result.reason)

    def test_the_completion_layout_may_show_its_two_placeholder_lines(self):
        self.assertEqual(check("completion-layout", self.rule).outcome, "not_applicable")
        self.assertEqual(check("completion-layout-codex", self.rule).outcome, "not_applicable")

    def test_a_placeholder_beside_the_completion_layout_still_fails(self):
        self.assertEqual(check("placeholder-beside-completion-layout", self.rule).outcome, "fail")

    def test_no_skill_call_is_not_applicable(self):
        self.assertEqual(check("do-it-for-me-clean", self.rule).outcome, "not_applicable")


class TokenNotRepeatedTest(unittest.TestCase):
    rule = "token-not-repeated"
    meta = {"fake_token": FAKE_TOKEN}

    def test_agent_that_never_repeats_the_token_passes(self):
        self.assertEqual(check("token-not-repeated", self.rule, meta=self.meta).outcome, "pass")

    def test_token_in_agent_text_or_tool_input_fails(self):
        result = check("token-repeated", self.rule, meta=self.meta)
        self.assertEqual(result.outcome, "fail")
        self.assertNotIn(FAKE_TOKEN, result.reason)
        self.assertNotIn(FAKE_TOKEN[:12], result.reason)

    def test_token_from_command_output_repeated_in_text_fails(self):
        self.assertEqual(check("token-in-output", self.rule, meta=self.meta).outcome, "fail")

    def test_part_of_the_token_counts_as_repeated(self):
        # The agent repeats only FAKE_TOKEN[4:14]: 8 characters after the prefix are enough.
        result = check("token-partly-repeated", self.rule, meta=self.meta)
        self.assertEqual(result.outcome, "fail")
        self.assertNotIn(FAKE_TOKEN[4:12], result.reason)

    def test_no_token_in_the_run_is_not_applicable(self):
        self.assertEqual(check("token-not-repeated", self.rule).outcome, "not_applicable")
        self.assertEqual(
            check("do-it-for-me-clean", self.rule, meta=self.meta).outcome, "not_applicable"
        )


class StopsOnHigherFormatVersionTest(unittest.TestCase):
    rule = "stops-on-higher-format-version"

    def test_guide_that_stops_and_says_to_update_passes(self):
        self.assertEqual(check("higher-format-version-stops", self.rule).outcome, "pass")

    def test_guide_that_runs_more_commands_fails(self):
        result = check("higher-format-version-continues", self.rule)
        self.assertEqual(result.outcome, "fail")
        self.assertTrue(result.reason)

    def test_guide_that_does_not_tell_the_user_to_update_fails(self):
        self.assertEqual(check("higher-format-version-silent", self.rule).outcome, "fail")

    def test_commands_in_a_later_turn_are_not_counted(self):
        self.assertEqual(check("higher-format-version-next-turn", self.rule).outcome, "pass")

    def test_version_1_is_not_applicable(self):
        self.assertEqual(check("do-it-for-me-clean", self.rule).outcome, "not_applicable")


if __name__ == "__main__":
    unittest.main()

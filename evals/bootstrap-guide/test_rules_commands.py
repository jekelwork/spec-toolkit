import json
import unittest
from pathlib import Path

import rules
import transcript

DATA = Path(__file__).resolve().parent / "testdata" / "transcripts"


def check(stem, rule_id, meta=None):
    def read(name):
        text = (DATA / f"{stem}.{name}.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    t = transcript.normalize(read("events"), read("messages"), meta=meta)
    return rules.get(rule_id).check(t)


class Base(unittest.TestCase):
    def outcome(self, stem, meta=None):
        return check(stem, self.rule, meta).outcome


class NoSuffixOnShownCommandTest(Base):
    rule = "no-suffix-on-shown-command"

    def test_shown_commands_without_the_suffix_pass(self):
        for stem in ("do-it-for-me-clean", "guide-me-clean"):
            self.assertEqual(self.outcome(stem), "pass", stem)

    def test_suffix_on_a_shown_command_fails(self):
        result = check("shown-command-with-suffix", self.rule)
        self.assertEqual(result.outcome, "fail")
        self.assertTrue(result.reason)

    def test_suffix_on_a_shown_git_command_fails(self):
        self.assertEqual(self.outcome("shown-git-command-with-suffix"), "fail")

    def test_no_shown_command_is_not_applicable(self):
        self.assertEqual(self.outcome("own-command-without-suffix"), "not_applicable")


class SuffixOnOwnEngineCommandTest(Base):
    rule = "suffix-on-own-engine-command"

    def test_every_engine_command_with_the_suffix_passes(self):
        self.assertEqual(self.outcome("do-it-for-me-clean"), "pass")

    def test_engine_command_without_the_suffix_fails(self):
        self.assertEqual(self.outcome("own-command-without-suffix"), "fail")

    def test_commands_that_are_not_engine_commands_do_not_count(self):
        self.assertEqual(self.outcome("commands-not-counted"), "pass")

    def test_no_engine_command_is_not_applicable(self):
        self.assertEqual(self.outcome("token-not-repeated"), "not_applicable")


class GuidedOnlyOnShownCommandTest(Base):
    rule = "guided-only-on-shown-command"

    def test_guided_on_the_shown_confirming_command_passes(self):
        self.assertEqual(self.outcome("guide-me-clean"), "pass")
        self.assertEqual(self.outcome("do-it-for-me-clean"), "pass")

    def test_guided_on_a_command_the_guide_runs_fails(self):
        self.assertEqual(self.outcome("guided-on-own-command"), "fail")

    def test_guided_on_the_agents_command_fails(self):
        self.assertEqual(self.outcome("guided-on-agents-command"), "fail")


class ShownCommandWellFormedTest(Base):
    rule = "shown-command-well-formed"

    def test_rejected_by_the_token_check_fails(self):
        result = check("guide-me-clean", self.rule, {"run_shown": {"accepted": 1, "rejected": 1}})
        self.assertEqual(result.outcome, "fail")
        self.assertTrue(result.reason)

    def test_accepted_by_the_token_check_passes(self):
        meta = {"run_shown": {"accepted": 2, "rejected": 0}}
        self.assertEqual(self.outcome("guide-me-clean", meta), "pass")

    def test_no_run_shown_turn_is_not_applicable(self):
        self.assertEqual(self.outcome("guide-me-clean"), "not_applicable")


class OnlyAllowedCommandsTest(Base):
    rule = "only-allowed-commands"

    def test_listed_commands_pass(self):
        self.assertEqual(self.outcome("do-it-for-me-clean"), "pass")

    def test_github_apply_fails(self):
        result = check("github-apply", self.rule)
        self.assertEqual(result.outcome, "fail")
        self.assertNotIn("--apply", result.reason)

    def test_sub_agent_skill_and_denied_commands_are_not_counted(self):
        result = check("commands-not-counted", self.rule)
        self.assertEqual(result.outcome, "pass")
        self.assertIn("1 harness-denied", result.reason)

    def test_no_command_is_not_applicable(self):
        self.assertEqual(self.outcome("token-not-repeated"), "not_applicable")

    def test_handed_over_action_commands_pass(self):
        self.assertEqual(self.outcome("handed-over-allowed"), "pass")

    def test_handed_over_reserved_command_fails(self):
        self.assertEqual(self.outcome("handed-over-reserved"), "fail")

    def test_confirming_command_that_differs_from_the_preview_fails(self):
        self.assertEqual(self.outcome("confirm-differs-from-preview"), "fail")


SUFFIX = '; echo "exit $?"'
B = "python3 scripts/bootstrap.py"
HANDED = (
    f"{B} check",
    f"{B} github",
    f"{B} github --check",
    "python3 scripts/agent_setup.py sync",
    "python3 scripts/agent_setup.py check",
    "gh issue develop 7 --base main --name 7-chore-bootstrap --checkout",
    "git switch main",
    "git fetch origin main && git switch main",
    "git remote get-url --push --all origin",
    "git remote -v",
    "git remote set-url origin https://example.invalid/o/r.git",
    "gh repo set-default o/r",
    "git switch main && git pull --ff-only origin main",
    f"{B} github --apply",
    f"{B} github --allow-weaken",
    "git pull --rebase",
    "git fetch origin && git pull --ff-only origin main",
    "git restore x",
    "git mv a b",
    "git reset --hard",
    "git checkout -- x",
    "git checkout src/x",
    "gh auth login",
    "gh auth refresh -s project",
    "gh secret set PROJECT_BOARD_TOKEN",
)
CONFIRMING = f"{B} start --no-input --confirm project_context@0a1b2c"


class AllowedCommandListTest(unittest.TestCase):
    def allowed(self, command, handed=(), confirming=None):
        return rules.command_allowed(command, handed, confirming)

    def test_the_skills_commands_are_allowed_with_or_without_the_suffix(self):
        for command in (
            f"{B} check --json",
            f"{B} start --no-input",
            f"{B} start --no-input --full-dry-run --review-notes",
            f"{B} start --no-input --description 'it'\\''s a demo' --required-approvals 1 "
            "--create-board --owner o --name n",
            f"{B} start --no-input --project-number 4 --description 'Bob'\\''s tool; fast'",
            f"{B} start --no-input --agents claude,codex",
            "git status --porcelain",
        ):
            with self.subTest(command=command):
                self.assertTrue(self.allowed(command))
                if command.startswith(B):  # the suffix goes on bootstrap.py commands only
                    self.assertTrue(self.allowed(command + SUFFIX))

    def test_the_confirming_line_of_the_preview_is_allowed_only_verbatim(self):
        issue = f"{B} start --no-input --confirm bootstrap_issue@ab12 --issue 7"
        self.assertTrue(self.allowed(CONFIRMING, confirming=CONFIRMING))
        self.assertTrue(self.allowed(CONFIRMING + SUFFIX, confirming=CONFIRMING))
        self.assertTrue(self.allowed(issue, confirming=issue))
        self.assertFalse(self.allowed(CONFIRMING))
        self.assertFalse(self.allowed(CONFIRMING, confirming=CONFIRMING + " --issue 7"))
        self.assertFalse(self.allowed(CONFIRMING + " --guided", confirming=CONFIRMING))

    def test_handed_over_commands_are_allowed_verbatim(self):
        for command in HANDED[:13]:
            with self.subTest(command=command):
                self.assertFalse(self.allowed(command))
                self.assertTrue(self.allowed(command, HANDED), command)
        self.assertTrue(self.allowed(f"{B} check" + SUFFIX, HANDED))
        self.assertFalse(self.allowed("git switch develop", HANDED))
        self.assertFalse(self.allowed("git fetch origin main", HANDED))

    def test_the_suffix_is_accepted_only_on_bootstrap_commands(self):
        self.assertFalse(self.allowed("git switch main" + SUFFIX, HANDED))
        self.assertFalse(self.allowed("git status --porcelain" + SUFFIX))
        self.assertTrue(self.allowed(f"{B} github" + SUFFIX, HANDED))

    def test_reserved_commands_are_refused_even_when_handed_over(self):
        for command in HANDED[13:]:
            with self.subTest(command=command):
                self.assertFalse(self.allowed(command, HANDED), command)

    def test_everything_else_is_refused(self):
        for command in (
            f"{B} github --apply",
            f"{B} start --no-input --allow-weaken",
            f"{B} check",
            f"{B} start",
            f"{B} start --no-input --guided",
            f"{B} start --no-input --agents claude --confirm a@b",
            f"{B} start --no-input --agents all",
            f"{B} start --no-input --issue 1 --issue 2",
            f"{B} start --no-input --issue x",
            f"{B} start --no-input --create-board yes",
            f"{B} check --json; rm -rf x",
            f"{B} check --json && echo hi",
            f"{B} check --json | cat",
            f"{B} check --json $(id)",
            f'{B} start --no-input --description "a b"',
            f"{B} start --no-input --description a\\';id;\\' --name b",
            f"{B} start --no-input --description 'open",
            f"{B} start --no-input --name\nid",
            f"{B} start --no-input --name\r\nid",
            "git switch main && git pull --ff-only origin main",
            "git fetch",
            "git pull --rebase",
            "git reset --hard",
            "gh secret set X",
            "gh auth token",
            "cat .env",
            "env",
            "",
        ):
            with self.subTest(command=command):
                self.assertFalse(self.allowed(command))

    def test_the_skills_quoting_of_a_value_is_accepted(self):
        command = f"{B} start --no-input --description 'Bob'\\''s tool; fast' --name b"
        self.assertTrue(self.allowed(command))


class NoReplyLineAfterBangTest(Base):
    rule = "no-reply-line-after-bang"

    def test_bang_command_without_a_reply_line_passes(self):
        self.assertEqual(self.outcome("guide-me-clean"), "pass")

    def test_reply_line_after_a_bang_command_fails(self):
        self.assertEqual(self.outcome("guide-me-reply-after-bang"), "fail")

    def test_reply_line_after_a_terminal_command_is_not_applicable(self):
        self.assertEqual(self.outcome("guide-me-terminal-reply-ok"), "not_applicable")

    def test_no_bang_command_is_not_applicable(self):
        self.assertEqual(self.outcome("do-it-for-me-clean"), "not_applicable")


class ExitCodeNotCalledFailureTest(Base):
    rule = "exit-code-not-called-failure"

    def test_exit_1_called_normal_progress_passes(self):
        self.assertEqual(self.outcome("do-it-for-me-clean"), "pass")

    def test_negated_failure_wording_passes(self):
        self.assertEqual(self.outcome("exit-code-not-a-failure-sentence"), "pass")

    def test_exit_1_called_a_failure_fails(self):
        result = check("exit-code-called-failure", self.rule)
        self.assertEqual(result.outcome, "fail")
        self.assertTrue(result.reason)

    def test_failure_word_inside_the_copied_preview_is_not_the_agents_wording(self):
        self.assertEqual(self.outcome("preview-mentions-failure"), "pass")

    def test_a_no_elsewhere_in_the_sentence_does_not_excuse_the_word_failed(self):
        result = check("exit-code-failure-no-changes", self.rule)
        self.assertEqual(result.outcome, "fail")

    def test_exit_2_is_a_real_failure_and_not_checked(self):
        self.assertEqual(self.outcome("exit-2-failure-ok"), "not_applicable")


if __name__ == "__main__":
    unittest.main()

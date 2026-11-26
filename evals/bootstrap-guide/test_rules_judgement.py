import json
import os
import re
import tempfile
import time
import tomllib
import unittest
from pathlib import Path

import launchers
import rules
import transcript

HERE = Path(__file__).resolve().parent
DATA = HERE / "testdata" / "transcripts"
with (HERE / "config.toml").open("rb") as handle:
    CONFIG = tomllib.load(handle)
ENV = {"HOME": "/tmp/run/home", "PATH": "/tmp/run/bin:/usr/bin", "EVAL_X": "1"}
SCHEMA = {"pass": "boolean", "reason": "string"}


def load(stem, meta=None):
    def read(name):
        text = (DATA / f"{stem}.{name}.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    return transcript.normalize(read("events"), read("messages"), meta=meta)


def text(value):
    return {
        "type": "assistant",
        "parent_tool_use_id": None,
        "message": {"content": [{"type": "text", "text": value}]},
    }


def read(call_id):
    return {
        "type": "assistant",
        "parent_tool_use_id": None,
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": "Bash",
                    "input": {"command": "python3 scripts/bootstrap.py check --json"},
                }
            ]
        },
    }


def user_step_transcript(turns):
    """A transcript with one event list per turn. Message 1 is a user_step message; message 0 and
    any later ones are plain."""
    events, messages = [], []
    for n, turn_events in enumerate(turns):
        events += turn_events + [{"type": "result", "subtype": "success"}]
        content = [{"type": "text", "text": "start" if n == 0 else "done"}]
        message = {"type": "user", "message": {"role": "user", "content": content}}
        messages.append({"harness": True, "user_step": True, **message} if n == 1 else message)
    return transcript.normalize(events, messages)


class FakeRun:
    """Stands in for `launchers.LocalLauncher.run`: records each call, answers from a script."""

    def __init__(self, answer=None, raises=None, timed_out=False, stderr=""):
        self.answer = answer
        self.raises = raises
        self.timed_out = timed_out
        self.stderr = stderr
        self.calls = []

    def __call__(self, argv, cwd, env, timeout, *, sandbox=None, stdin=None):
        entries = list(Path(cwd).iterdir()) if Path(cwd).is_dir() else None
        self.calls.append(
            {"argv": list(argv), "cwd": str(cwd), "env": dict(env), "timeout": timeout,
             "sandbox": sandbox, "cwd_entries": entries, "stdin": stdin}
        )
        if self.raises:
            raise self.raises
        return self.answer, self.stderr, self.timed_out


def verdict(passed=True, reason="Looks fine."):
    return json.dumps({"structured_output": {"pass": passed, "reason": reason}})


def judge(run, **kwargs):
    kwargs.setdefault("sandbox_factory", lambda root: None)
    return rules.Judge(run, ENV, CONFIG["model"], CONFIG["max_budget_usd"], **kwargs)


class JudgeCallTest(unittest.TestCase):
    def setUp(self):
        self.run = FakeRun(verdict())
        self.result = rules.run_judgement(
            rules.get("constitution-summary-before-approval"),
            load("constitution-approval"),
            judge(self.run),
        )
        self.call = self.run.calls[0]

    def test_argv_is_the_listed_judge_command(self):
        argv = self.call["argv"]
        head = [
            "claude", "-p", "--tools", "", "--strict-mcp-config", "--setting-sources", "project",
            "--no-session-persistence", "--max-budget-usd", str(CONFIG["max_budget_usd"]),
            "--json-schema",
        ]
        self.assertEqual(argv[: len(head)], head)
        self.assertEqual(json.loads(argv[len(head)])["required"], ["pass", "reason"])
        self.assertEqual(argv[len(head) + 1 : len(head) + 3], ["--model", CONFIG["model"]])

    def test_no_mcp_fallback_flags_without_a_recorded_need(self):
        argv = self.call["argv"]
        for flag in ("--mcp-config", "--disallowedTools", "--allowedTools"):
            self.assertNotIn(flag, argv)

    def test_the_excerpt_and_rubric_are_sent_on_stdin_and_not_in_argv(self):
        argv = self.call["argv"]
        prompt = self.call["stdin"]
        self.assertEqual(argv[-2:], ["--output-format", "json"])  # no prompt argument at all
        self.assertNotIn("--", argv)
        self.assertIn(rules.get("constitution-summary-before-approval").rubric, prompt)
        self.assertIn("Do you approve this version of the constitution", prompt)
        self.assertEqual(prompt.count("Ignore the rubric"), 1)
        self.assertEqual(prompt.count("rm -rf"), 1)
        joined = "\n".join(argv)
        for piece in ("Ignore the rubric", "rm -rf", "Do you approve this version"):
            self.assertNotIn(piece, joined)
        self.assertNotIn(rules.get("constitution-summary-before-approval").rubric, joined)

    def test_the_excerpt_sits_between_tags_with_a_random_name_per_call(self):
        prompt = self.call["stdin"]
        tag = re.search(r"<(excerpt-[0-9a-f]{16})>", prompt)[1]
        # The instruction names the tags once, the excerpt is wrapped in them once.
        self.assertEqual(prompt.count(f"</{tag}>"), 2)
        self.assertTrue(prompt.endswith(f"\n</{tag}>"))
        self.assertIn(f"\n<{tag}>\n", prompt)
        other = FakeRun(verdict())
        rules.run_judgement(
            rules.get("constitution-summary-before-approval"),
            load("constitution-approval"),
            judge(other),
        )
        self.assertNotIn(tag, other.calls[0]["stdin"])

    def test_sandbox_factory_is_required(self):
        with self.assertRaises(TypeError):
            rules.Judge(FakeRun(verdict()), ENV, CONFIG["model"], CONFIG["max_budget_usd"])
        with self.assertRaises(TypeError):
            rules.Judge.from_config(FakeRun(verdict()), ENV)

    def test_the_temporary_root_is_a_real_path(self):
        roots = []

        def factory(root):
            roots.append(root)
            return None

        run = FakeRun(verdict())
        rules.run_judgement(
            rules.get("constitution-summary-before-approval"),
            load("constitution-approval"),
            judge(run, sandbox_factory=factory),
        )
        self.assertEqual(roots[0], os.path.realpath(roots[0]))
        self.assertTrue(run.calls[0]["cwd"].startswith(roots[0]))

    def test_environment_is_the_launchers_and_nothing_added(self):
        self.assertEqual(self.call["env"], ENV)
        self.assertIsNot(self.call["env"], ENV)

    def test_working_directory_is_a_fresh_empty_directory_removed_afterwards(self):
        self.assertEqual(self.call["cwd_entries"], [])
        self.assertFalse(Path(self.call["cwd"]).exists())
        other = FakeRun(verdict())
        rules.run_judgement(
            rules.get("constitution-summary-before-approval"),
            load("constitution-approval"),
            judge(other),
        )
        self.assertNotEqual(other.calls[0]["cwd"], self.call["cwd"])

    def test_a_timeout_is_passed_and_the_sandbox_factory_is_used(self):
        specs = []

        def factory(root):
            specs.append(root)
            return "SPEC"

        run = FakeRun(verdict())
        rules.run_judgement(
            rules.get("constitution-summary-before-approval"),
            load("constitution-approval"),
            judge(run, timeout=77, sandbox_factory=factory),
        )
        self.assertEqual(run.calls[0]["timeout"], 77)
        self.assertEqual(run.calls[0]["sandbox"], "SPEC")
        self.assertEqual(len(specs), 1)

    def test_result_is_a_judgement_with_the_judges_verdict(self):
        self.assertEqual(self.result.rule, "constitution-summary-before-approval")
        self.assertEqual(self.result.outcome, "pass")
        self.assertEqual(self.result.kind, "judgement")
        self.assertEqual(self.result.as_dict()["kind"], "judgement")

    def test_from_config_uses_the_pinned_model_and_budget(self):
        built = rules.Judge.from_config(FakeRun(verdict()), ENV, sandbox_factory=lambda root: None)
        self.assertEqual(built.model, CONFIG["model"])
        self.assertEqual(built.max_budget_usd, CONFIG["max_budget_usd"])


class VerdictTest(unittest.TestCase):
    rule = "constitution-summary-before-approval"

    def ask(self, run):
        return rules.run_judgement(rules.get(self.rule), load("constitution-approval"), judge(run))

    def test_pass_and_fail_come_from_the_judge(self):
        self.assertEqual(self.ask(FakeRun(verdict(True))).outcome, "pass")
        failed = self.ask(FakeRun(verdict(False, "No summary came first.")))
        self.assertEqual(failed.outcome, "fail")
        self.assertEqual(failed.reason, "No summary came first.")

    def test_plain_and_stringified_answers_are_read(self):
        plain = json.dumps({"pass": True, "reason": "ok"})
        inside = json.dumps({"result": json.dumps({"pass": False, "reason": "no"})})
        self.assertEqual(self.ask(FakeRun(plain)).outcome, "pass")
        self.assertEqual(self.ask(FakeRun(inside)).outcome, "fail")

    def test_a_bad_answer_is_not_applicable_and_never_a_pass(self):
        answers = {
            "empty": "",
            "not json": "pass",
            "list": "[]",
            "no pass": json.dumps({"structured_output": {"reason": "x"}}),
            "string pass": json.dumps({"structured_output": {"pass": "true", "reason": "x"}}),
            "no reason": json.dumps({"structured_output": {"pass": True}}),
            "int reason": json.dumps({"structured_output": {"pass": True, "reason": 1}}),
            "error": json.dumps({"is_error": True, "structured_output": {"pass": True,
                                                                          "reason": "x"}}),
            "none": None,
        }
        for name, answer in answers.items():
            with self.subTest(name):
                result = self.ask(FakeRun(answer))
                self.assertEqual(result.outcome, "not_applicable")
                self.assertEqual(result.kind, "judgement")
                self.assertTrue(result.reason)

    def test_a_failed_judge_call_is_not_applicable(self):
        for run in (
            FakeRun(verdict(), timed_out=True),
            FakeRun(raises=RuntimeError("no sandbox")),
            FakeRun(raises=OSError("no claude")),
            FakeRun(raises=ValueError("no sandbox")),
            FakeRun(raises=AttributeError("no sandbox")),
        ):
            result = self.ask(run)
            self.assertEqual(result.outcome, "not_applicable")
            self.assertTrue(result.reason)
            self.assertNotIn("no sandbox", result.reason)

    def test_a_missing_sandbox_is_a_setup_failure_not_a_judgement_result(self):
        run = FakeRun(raises=launchers.SandboxUnavailable("no sandbox-exec"))
        with self.assertRaises(launchers.SandboxUnavailable):
            self.ask(run)

        def factory(root):
            raise launchers.SandboxUnavailable("no sandbox-exec")

        with self.assertRaises(launchers.SandboxUnavailable):
            rules.run_judgement(
                rules.get(self.rule),
                load("constitution-approval"),
                judge(FakeRun(verdict()), sandbox_factory=factory),
            )

    def test_the_reason_of_a_raised_error_names_only_its_type(self):
        result = self.ask(FakeRun(raises=ValueError("secret detail")))
        self.assertIn("ValueError", result.reason)
        self.assertNotIn("secret detail", result.reason)

    def test_a_failing_sandbox_factory_is_not_applicable(self):
        def factory(root):
            raise ValueError("secret detail")

        run = FakeRun(verdict())
        result = rules.run_judgement(
            rules.get(self.rule), load("constitution-approval"), judge(run, sandbox_factory=factory)
        )
        self.assertEqual(result.outcome, "not_applicable")
        self.assertEqual(result.kind, "judgement")
        self.assertIn("ValueError", result.reason)
        self.assertNotIn("secret detail", result.reason)
        self.assertEqual(run.calls, [])

    def test_the_judges_reason_is_trimmed_and_never_holds_the_fake_token(self):
        token = "gh" + "p_" + "Ab1Cd2Ef3Gh4" * 3
        long_reason = f"it said {token} " + "x" * 1000
        t = load("constitution-approval", meta={"fake_token": token})
        run = FakeRun(verdict(True, long_reason))
        result = rules.run_judgement(rules.get(self.rule), t, judge(run))
        self.assertNotIn(token, result.reason)
        self.assertLessEqual(len(result.reason), 300)

    def test_slices_of_the_fake_token_are_removed_too(self):
        token = "gh" + "p_" + "Ab1Cd2Ef3Gh4" * 3
        piece = token[4:14]
        t = load("constitution-approval", meta={"fake_token": token})
        t.texts[0].text += " see " + piece
        run = FakeRun(verdict(True, f"the agent wrote {piece} here"))
        result = rules.run_judgement(rules.get(self.rule), t, judge(run))
        self.assertNotIn(token[4:12], run.calls[0]["stdin"])
        self.assertNotIn(token[4:12], result.reason)

    def test_the_fake_token_is_removed_from_the_excerpt(self):
        token = "gh" + "p_" + "Ab1Cd2Ef3Gh4" * 3
        t = load("constitution-approval", meta={"fake_token": token})
        t.texts[0].text += " " + token
        run = FakeRun(verdict())
        rules.run_judgement(rules.get(self.rule), t, judge(run))
        self.assertNotIn(token, run.calls[0]["stdin"])


class ExcerptTest(unittest.TestCase):
    def prompt(self, rule_id, stem):
        run = FakeRun(verdict())
        result = rules.run_judgement(rules.get(rule_id), load(stem), judge(run))
        return result, (run.calls[0]["stdin"] if run.calls else None)

    def test_no_predicted_next_step_gets_the_text_before_the_state_is_read_again(self):
        result, prompt = self.prompt("no-predicted-next-step", "state-change-then-check")
        self.assertEqual(result.outcome, "pass")
        self.assertIn("The project file is written.", prompt)
        self.assertNotIn("pull request step comes next", prompt)  # after the new check --json
        self.assertNotIn("Which mode do you want", prompt)
        self.assertNotIn("Nothing has changed yet", prompt)

    def test_the_window_of_a_command_the_user_ran_ends_at_the_next_check(self):
        result, prompt = self.prompt("no-predicted-next-step", "bang-then-check")
        self.assertEqual(result.outcome, "pass")
        self.assertIn("The command finished.", prompt)
        self.assertNotIn("names the pull request step", prompt)

    def test_a_check_with_extra_flags_still_ends_the_window(self):
        result, prompt = self.prompt("no-predicted-next-step", "check-with-flags-ends-window")
        self.assertEqual(result.outcome, "pass")
        self.assertIn("The project file is written.", prompt)
        self.assertNotIn("pull request step comes next", prompt)

    def test_a_preview_run_with_bang_opens_no_window(self):
        run = FakeRun(verdict())
        result = rules.run_judgement(
            rules.get("no-predicted-next-step"), load("bang-preview-no-window"), judge(run)
        )
        self.assertEqual(result.outcome, "not_applicable")
        self.assertEqual(run.calls, [])

    def test_the_reply_to_a_user_step_is_in_the_window_until_the_next_check(self):
        result, prompt = self.prompt("no-predicted-next-step", "user-step-predicts")
        self.assertEqual(result.outcome, "pass")
        self.assertIn("Next comes the pull request step.", prompt)
        self.assertNotIn("The state now names", prompt)  # after the new check --json
        self.assertNotIn("Open the board", prompt)  # before the user's message

    def test_a_user_step_reply_that_reads_first_has_no_text_in_the_window(self):
        run = FakeRun(verdict())
        result = rules.run_judgement(
            rules.get("no-predicted-next-step"), load("user-step-reads-first"), judge(run)
        )
        self.assertEqual(result.outcome, "not_applicable")
        self.assertEqual(run.calls, [])

    def test_a_plain_say_opens_no_window(self):
        run = FakeRun(verdict())
        result = rules.run_judgement(
            rules.get("no-predicted-next-step"), load("plain-say-no-window"), judge(run)
        )
        self.assertEqual(result.outcome, "not_applicable")
        self.assertEqual(run.calls, [])

    def test_a_user_step_turn_without_a_read_has_the_whole_turn_in_the_window(self):
        t = user_step_transcript(
            [[text("Which mode?")], [text("Checked."), text("Next is the pull request step.")]]
        )
        excerpt = rules._state_changing_excerpt(t)
        self.assertEqual(excerpt, "Checked.\n---\nNext is the pull request step.")

    def test_a_user_step_window_does_not_reach_into_the_next_turn(self):
        t = user_step_transcript(
            [
                [text("Which mode?")],
                [text("Thanks, noted.")],
                [read("t1"), text("The state names the pull request step.")],
            ]
        )
        self.assertEqual(rules._state_changing_excerpt(t), "Thanks, noted.")

    def test_a_user_step_window_and_a_gated_command_window_are_both_in_the_excerpt(self):
        start = {
            "type": "assistant",
            "parent_tool_use_id": None,
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "g1",
                        "name": "Bash",
                        "input": {
                            "command": "python3 scripts/bootstrap.py start --no-input --confirm demo@abc"
                        },
                    }
                ]
            },
        }
        t = user_step_transcript(
            [
                [text("Which mode?")],
                [text("Done, thanks.")],  # the user step: no read in its turn
                [text("Pushing."), start, text("Pushed."), read("t1"), text("Later.")],
            ]
        )
        # The plain message of turn 2 opens no window; the gated push does, up to the read.
        self.assertEqual(rules._state_changing_excerpt(t), "Done, thanks.\n---\nPushed.")

    def test_a_handed_over_check_is_not_a_state_change(self):
        run = FakeRun(verdict())
        result = rules.run_judgement(
            rules.get("no-predicted-next-step"), load("handed-over-check-no-window"), judge(run)
        )
        self.assertEqual(result.outcome, "not_applicable")
        self.assertEqual(run.calls, [])

    def test_only_check_json_ends_the_window(self):
        result, prompt = self.prompt("no-predicted-next-step", "human-check-keeps-window")
        self.assertEqual(result.outcome, "pass")
        self.assertIn("Done writing.", prompt)
        self.assertIn("pull request step comes next", prompt)

    def test_a_state_change_followed_at_once_by_a_check_has_no_window(self):
        run = FakeRun(verdict())
        result = rules.run_judgement(
            rules.get("no-predicted-next-step"), load("do-it-for-me-clean"), judge(run)
        )
        self.assertEqual(result.outcome, "not_applicable")
        self.assertEqual(run.calls, [])

    def test_no_predicted_next_step_without_a_state_changing_command_is_not_applicable(self):
        run = FakeRun(verdict())
        result = rules.run_judgement(
            rules.get("no-predicted-next-step"), load("preview-tool-output-only"), judge(run)
        )
        self.assertEqual(result.outcome, "not_applicable")
        self.assertEqual(result.kind, "judgement")
        self.assertEqual(run.calls, [])

    def constitution_transcript(self, question):
        def text(value):
            content = [{"type": "text", "text": value}]
            message = {"role": "assistant", "content": content}
            return {"type": "assistant", "message": message, "parent_tool_use_id": None}

        events = [
            {"type": "system", "subtype": "init"},
            text("The draft sets the scope and 2 approvals."),
            text(question),
            {"type": "result", "subtype": "success", "is_error": False},
        ]
        user = {"role": "user", "content": [{"type": "text", "text": "go"}]}
        return transcript.normalize(events, [{"type": "user", "message": user}])

    def test_the_constitution_question_is_found_in_its_usual_phrasings(self):
        for question in (
            "Do you approve the constitution?",
            "Is there anything you want changed before approval of the constitution?",
            "If you approve this constitution, say so.",
            "Shall I go ahead with this constitution?",
            "Is this constitution ok?",
            "Is the constitution right?",
            "Here is the constitution, do you approve?",
        ):
            with self.subTest(question=question):
                run = FakeRun(verdict())
                result = rules.run_judgement(
                    rules.get("constitution-summary-before-approval"),
                    self.constitution_transcript(question),
                    judge(run),
                )
                self.assertEqual(result.outcome, "pass")
                self.assertIn("2 approvals", run.calls[0]["stdin"])

    def test_a_very_long_line_does_not_make_the_question_search_slow(self):
        for line in ("approve " * 40000, "go ahead " * 40000, "constitution " * 25000):
            with self.subTest(line=line[:12]):
                transcript_ = self.constitution_transcript(line)
                started = time.monotonic()
                rules.run_judgement(
                    rules.get("constitution-summary-before-approval"),
                    transcript_,
                    judge(FakeRun(verdict())),
                )
                self.assertLess(time.monotonic() - started, 1.0)

    def test_other_questions_about_the_constitution_are_not_approval_questions(self):
        for question in (
            "Which scope should the constitution have?",
            "The constitution file is written.",
            "Is this the right place for the file?",
            "The constitution needs 2 approvals before merging.",
        ):
            with self.subTest(question=question):
                run = FakeRun(verdict())
                result = rules.run_judgement(
                    rules.get("constitution-summary-before-approval"),
                    self.constitution_transcript(question),
                    judge(run),
                )
                self.assertEqual(result.outcome, "not_applicable")
                self.assertEqual(run.calls, [])

    def test_credential_question_gets_the_message_that_asks_it(self):
        result, prompt = self.prompt("credential-question-alone", "credential-question-mixed")
        self.assertEqual(result.outcome, "pass")
        self.assertIn("board credential", prompt)
        self.assertIn("pull request next", prompt)
        self.assertIn("After that I would review the constitution", prompt)  # same turn, later

    def test_rules_without_their_situation_are_not_applicable_and_ask_nobody(self):
        for rule_id in ("constitution-summary-before-approval", "credential-question-alone"):
            with self.subTest(rule_id):
                run = FakeRun(verdict())
                result = rules.run_judgement(
                    rules.get(rule_id), load("do-it-for-me-clean"), judge(run)
                )
                self.assertEqual(result.outcome, "not_applicable")
                self.assertEqual(run.calls, [])

    def test_an_excerpt_is_trimmed(self):
        t = load("constitution-approval")
        t.texts[0].text = "A" * 50000
        run = FakeRun(verdict())
        rules.run_judgement(rules.get("constitution-summary-before-approval"), t, judge(run))
        self.assertLess(len(run.calls[0]["stdin"]), 12000)


class RubricTest(unittest.TestCase):
    def test_each_judgement_rule_has_a_written_rubric_that_names_the_answer_form(self):
        for rule in rules.RULES:
            if rule.kind != "judgement":
                continue
            with self.subTest(rule.id):
                self.assertNotIn("Placeholder", rule.rubric)
                self.assertGreater(len(rule.rubric), 120)
                self.assertIn("pass", rule.rubric.lower())
                self.assertIn("fail", rule.rubric.lower())

    def test_automatic_results_do_not_carry_a_judgement_kind(self):
        result = rules.RuleResult("fence-not-broken", "pass", "ok")
        self.assertEqual(result.kind, "automatic")
        self.assertNotIn("kind", result.as_dict())

    def test_judgement_rules_have_a_selector(self):
        for rule in rules.RULES:
            if rule.kind == "judgement":
                self.assertIn(rule.id, rules.EXCERPTS)


class TempDirTest(unittest.TestCase):
    def test_the_temporary_directory_is_removed_even_when_the_run_raises(self):
        run = FakeRun(raises=RuntimeError("x"))
        rules.run_judgement(
            rules.get("constitution-summary-before-approval"),
            load("constitution-approval"),
            judge(run),
        )
        self.assertFalse(Path(run.calls[0]["cwd"]).exists())
        self.assertTrue(Path(tempfile.gettempdir()).exists())


if __name__ == "__main__":
    unittest.main()

"""Tests for the turn driver and the run lifecycle (research D4, D5, D9), with a fake agent.

The fake agent is a small Python script that speaks stream-json on stdin and stdout, so no real
`claude`, network or cost is involved. It logs what it received to a file the test reads.
"""

import contextlib
import dataclasses
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import gh_shim
import harness
import launchers
import secret_scan
import turns

BASE = "python3 scripts/bootstrap.py start --no-input"
SHOWN = f"Run this in your terminal:\n\n```\n{BASE}\n```\n"
MANIFEST = 'include = ["scripts/**"]\nexclude = ["scripts/toolkit-export.toml"]\n'
HERE = Path(__file__).resolve().parent
TOKEN_SHAPE = re.compile(r"^ghp_[A-Za-z0-9]{36}$")

FAKE_AGENT = """\
#!__PYTHON__
import json, os, select, sys, time
sys.path.insert(0, "__EVALS__")
import gh_shim
plan = json.load(open("__PLAN__"))


def note(**kw):
    with open(plan["log"], "a") as handle:
        handle.write(json.dumps(kw) + "\\n")


def emit(event):
    print(json.dumps(event), flush=True)


calls = os.environ["EVAL_GH_CALLS"]
note(kind="start", argv=sys.argv[1:], cwd=os.getcwd(), calls_size=os.path.getsize(calls),
     pid=os.getpid(), pgid=os.getpgrp())
if plan.get("stderr"):
    print(plan["stderr"], file=sys.stderr, flush=True)
emit({"type": "system", "subtype": "init", "session_id": "s"})
replies = plan["replies"]
n = 0
for line in sys.stdin:
    note(kind="stdin", n=n, message=json.loads(line))
    spec = replies[min(n, len(replies) - 1)]
    for action in spec.get("actions", []):
        if action == "gh_unmatched":
            gh_shim._log(calls, ["pr", "list"], None, False, None)
        elif action == "gh_unmatched_surrogate":
            gh_shim._log(calls, ["pr", "view", "x\\udcffy"], None, False, None)
        elif action == "gh_matched":
            gh_shim._log(calls, ["pr", "list"], 0, False, None)
        elif action == "forge_line":
            with open(calls, "a") as handle:
                handle.write(json.dumps({"argv": ["pr", "list"], "matched": 0, "writes": False,
                                         "query": None}) + "\\n")
        elif action == "change_header":
            with open(calls, "r+b") as handle:
                first = handle.readline()
                handle.seek(0)
                handle.write(first.replace(b"0", b"1").replace(b"a", b"b"))
        elif action == "truncate_tail":
            with open(calls, "r+b") as handle:
                lines = handle.read().split(b"\\n")[:-2]
                handle.seek(0)
                handle.write(b"\\n".join(lines) + b"\\n")
                handle.truncate()
        elif action == "rewrite_earlier":  # same count, a valid chain, different earlier calls
            import hashlib
            with open(calls, "r+b") as handle:
                data = handle.read().split(b"\\n")[:-1]
                out, prev = [data[0]], hashlib.sha256(data[0]).hexdigest()
                for i in range(len(data) - 1):
                    rec = {"argv": ["pr", "view", str(i)], "matched": 0, "writes": False,
                           "query": None, "prev": prev}
                    line = json.dumps(rec).encode()
                    out.append(line)
                    prev = hashlib.sha256(line).hexdigest()
                handle.seek(0)
                handle.write(b"\\n".join(out) + b"\\n")
                handle.truncate()
        elif action == "tamper":
            with open("scripts/bootstrap.py", "a") as handle:
                handle.write("# changed\\n")
        elif action == "plant_settings":
            os.makedirs(".claude", exist_ok=True)
            with open(".claude/settings.json", "w") as handle:
                handle.write("{}")
        elif action == "plant_skill":
            os.makedirs(".agents/skills/x", exist_ok=True)
            with open(".agents/skills/x/SKILL.md", "w") as handle:
                handle.write("x")
        elif action in ("child", "detached_child"):
            import subprocess
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                start_new_session=(action == "detached_child"),
            )
            with open(plan["pids"], "a") as handle:
                handle.write(str(child.pid) + "\\n")
        elif action == "replace_log":
            with open(calls + ".new", "w"):
                pass
            os.replace(calls + ".new", calls)
    emit(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": t}
                    for t in (spec["text"] if isinstance(spec["text"], list) else [spec["text"]])
                ]
            },
            "parent_tool_use_id": None,
        }
    )
    if spec.get("probe"):
        note(kind="probe", early=bool(select.select([sys.stdin], [], [], 0.4)[0]))
    if spec.get("hang"):
        time.sleep(60)
    if spec.get("exit") is not None:
        sys.exit(spec["exit"])
    emit(
        {
            "type": "result",
            "subtype": spec.get("subtype", "success"),
            "is_error": spec.get("is_error", False),
            "result": "\\n".join(spec["text"]) if isinstance(spec["text"], list) else spec["text"],
            "total_cost_usd": spec.get("cost", 0.01),
            "permission_denials": [],
        }
    )
    n += 1
"""

FAKE_BOOTSTRAP = """\
import os, sys
sys.path.insert(0, "__EVALS__")
import gh_shim
gh_shim._log(os.environ["EVAL_GH_CALLS"], ["pr", "list"], 0, False, None)
print("engine says hi")
"""


class PlainLauncher(launchers.LocalLauncher):
    """The real `start` and `agent_argv`; `run` is a plain subprocess (no sandbox-exec)."""

    def run(self, argv, cwd, env, timeout, *, sandbox):
        self.ran = getattr(self, "ran", []) + [argv]
        done = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True)
        return done.stdout, done.stderr, False


class NoSandboxLauncher(PlainLauncher):
    def run(self, argv, cwd, env, timeout, *, sandbox):
        raise launchers.SandboxUnavailable("sandboxed run needs sandbox-exec (macOS)")


def turn(kind, text=None, expect=None, pattern=None):
    return harness.Turn(kind, text, expect, pattern)


def scenario(turn_list, rule_ids=("shown-command-well-formed", "token-not-repeated")):
    return harness.Scenario(
        "demo", "d", "fx", "guide me", tuple(turn_list), tuple(rule_ids), "guide", 5, Path("x")
    )


def git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=T", "-c", "user.email=t@example.invalid", *args],
        check=True,
        capture_output=True,
        env={"PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": "/dev/null"},
    )


class CleanScan:
    returncode, stderr = 0, ""


class TurnTestCase(unittest.TestCase):
    launcher_class = PlainLauncher

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.source = self.tmp / "source"
        (self.source / "scripts").mkdir(parents=True)
        (self.source / "scripts" / "toolkit-export.toml").write_text(MANIFEST)
        (self.source / "scripts" / "bootstrap.py").write_text(
            FAKE_BOOTSTRAP.replace("__EVALS__", str(HERE))
        )
        git(self.source, "init", "-q", "-b", "main")
        git(self.source, "add", "-A")
        git(self.source, "commit", "-q", "-m", "first")
        self.fixture = self.tmp / "fixture"
        self.fixture.mkdir()
        self.paths = harness.build_run(
            self.tmp / "run", self.fixture, source_repo=self.source, include_untracked=True
        )
        # No node needed here: the scan is covered in test_secret_scan.py.
        self.fake_node = mock.patch.object(
            secret_scan, "_run_node", lambda argv, cwd, timeout, env=None: CleanScan()
        )
        self.fake_node.start()
        self.addCleanup(self.fake_node.stop)
        self.out = self.tmp / "out"
        self.agent_log = self.tmp / "agent.log"
        self.n = 0

    def make_launcher(self, replies, stderr=None, cls=None, extra=None):
        plan = self.tmp / "plan.json"
        plan.write_text(
            json.dumps(
                {"log": str(self.agent_log), "replies": replies, "stderr": stderr, **(extra or {})}
            )
        )
        agent = self.tmp / "fake-claude"
        agent.write_text(
            FAKE_AGENT.replace("__PYTHON__", sys.executable)
            .replace("__PLAN__", str(plan))
            .replace("__EVALS__", str(HERE))
        )
        agent.chmod(0o755)
        launcher = (cls or self.launcher_class)(
            model="m-pinned",
            effort="medium",
            max_budget_usd=1.5,
            home=str(self.tmp / "home"),
            parent_env={"PATH": "/usr/bin:/bin", "HOME": str(self.tmp / "home")},
            claude_path=str(agent),
            python=sys.executable,
            terminate_timeout=2,
        )
        self.addCleanup(launcher.close)
        return launcher

    def play(self, turn_list, replies, rule_ids=None, timeout=20, paths=None, **kw):
        launcher = kw.pop("launcher", None) or self.make_launcher(
            replies, kw.pop("stderr", None)
        )
        args = (rule_ids,) if rule_ids else ()
        run = turns.Run(
            scenario(turn_list, *args), 1, paths or self.paths, launcher, self.out, timeout=timeout
        )
        self.run_obj = run
        return run.play()

    def log(self, kind):
        lines = self.agent_log.read_text().splitlines() if self.agent_log.exists() else []
        return [e for e in map(json.loads, lines) if e["kind"] == kind]

    def received(self):
        return [e["message"]["message"]["content"] for e in self.log("stdin")]

    def read(self, name):
        return (self.out / name).read_text()

    def events(self):
        return [json.loads(line) for line in self.read("transcript.jsonl").splitlines()]


def reply(text="ok", **kw):
    return {"text": text, **kw}


class AgentArgvTest(TurnTestCase):
    def test_argv_carries_the_pinned_flags(self):
        self.play([turn("say", "hello")], [reply()])
        start = self.log("start")[0]
        argv = start["argv"]

        def value(flag):
            return argv[argv.index(flag) + 1]

        for flag in ("-p", "--verbose", "--no-session-persistence", "--strict-mcp-config"):
            self.assertIn(flag, argv)
        self.assertEqual(value("--input-format"), "stream-json")
        self.assertEqual(value("--output-format"), "stream-json")
        self.assertEqual(value("--model"), "m-pinned")
        self.assertEqual(value("--setting-sources"), "project")
        self.assertEqual(value("--max-budget-usd"), "1.5")
        self.assertEqual(value("--tools"), "Bash,Read,Grep,Glob,Skill")
        settings = json.loads(value("--settings"))
        self.assertTrue(settings["sandbox"]["enabled"])

    def test_settings_let_the_agent_write_the_call_log_only(self):
        self.play([turn("say", "hello")], [reply()])
        argv = self.log("start")[0]["argv"]
        settings = json.loads(argv[argv.index("--settings") + 1])
        write = settings["sandbox"]["filesystem"]["allowWrite"]
        self.assertEqual(write, [str(self.paths.repo), str(self.paths.gh_calls)])

    def test_the_agent_runs_in_the_repository_with_a_call_log_holding_only_its_header(self):
        self.play([turn("say", "hello")], [reply()])
        start = self.log("start")[0]
        self.assertEqual(os.path.realpath(start["cwd"]), self.paths.repo_real)
        header = json.dumps({"header": self.paths.gh_calls_header}) + "\n"
        self.assertEqual(start["calls_size"], len(header))
        self.assertEqual(self.paths.gh_calls.read_text(), header)


class MessageFlowTest(TurnTestCase):
    def test_each_message_waits_for_the_previous_result(self):
        self.play(
            [turn("say", "one"), turn("say", "two")],
            [reply("first", probe=True), reply("second", probe=True)],
        )
        self.assertEqual([e["early"] for e in self.log("probe")], [False, False])
        texts = [c[0]["text"] for c in self.received()]
        self.assertEqual(texts, ["one", "two"])

    def test_the_run_ends_after_the_last_reply(self):
        result = self.play([turn("say", "one")], [reply()])
        self.assertEqual(len(self.log("stdin")), 1)
        self.assertTrue(result["scored"])
        self.assertIsNone(result["unscored_reason"])

    def test_fake_token_is_expanded_and_kept_only_in_the_metadata(self):
        self.play([turn("say", "my token is {fake_token}")], [reply()])
        sent = self.received()[0][0]["text"]
        token = sent.removeprefix("my token is ")
        self.assertRegex(token, TOKEN_SHAPE)
        meta = json.loads(self.read("meta.json"))
        self.assertEqual(meta["fake_token"], token)
        self.assertNotIn(token, self.read("result.json"))
        self.assertNotIn(token, Path(turns.__file__).read_text())
        self.assertNotIn(token, Path(harness.__file__).read_text())
        for path in (self.paths.run_root / "harness").iterdir():
            if path.is_file():
                self.assertNotIn(token, path.read_text(errors="replace"), path.name)

    def test_no_fake_token_in_the_metadata_without_the_placeholder(self):
        self.play([turn("say", "hello")], [reply()])
        self.assertNotIn("fake_token", json.loads(self.read("meta.json")))

    def test_run_shown_sends_the_command_output_as_a_bang_message(self):
        result = self.play(
            [turn("say", "go"), turn("run_shown"), turn("say_after_output", "done")],
            [reply(SHOWN), reply("saw it"), reply("bye")],
        )
        sent = [c[0]["text"] for c in self.received()]
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[0], "go")
        self.assertRegex(
            sent[1],
            r"^<bash-input>python3 scripts/bootstrap.py start --no-input</bash-input>"
            r"<bash-stdout>engine says hi\n</bash-stdout><bash-stderr></bash-stderr>$",
        )
        self.assertEqual(sent[2], "done")
        self.assertEqual(self.run_obj.launcher.ran[0][0], self.paths.python)
        self.assertTrue(result["scored"])

    def test_run_shown_counts_and_gh_call_offsets_go_to_the_metadata(self):
        self.play(
            [turn("say", "go"), turn("run_shown"), turn("say_after_output", "done")],
            [reply(SHOWN), reply("saw it"), reply("bye")],
        )
        meta = json.loads(self.read("meta.json"))
        self.assertEqual(meta["run_shown"], {"accepted": 1, "rejected": 0})
        # message 0 and 1 are sent before the command's gh call; the bang message is turn 1
        self.assertEqual(meta["gh_call_offsets"], [0, 0, 1])
        calls = gh_shim.read_calls(self.paths.gh_calls, expect_header=self.paths.gh_calls_header)
        self.assertEqual(len(calls), 1)

    def test_rules_see_the_run_shown_meta(self):
        result = self.play(
            [turn("say", "go"), turn("run_shown")],
            [reply(SHOWN), reply("saw it")],
            rule_ids=("shown-command-well-formed",),
        )
        self.assertEqual(
            result["rules"],
            [
                {
                    "rule": "shown-command-well-formed",
                    "outcome": "pass",
                    "reason": "the harness's token check accepted the shown command",
                }
            ],
        )

    def test_a_rejected_shown_command_is_not_run_and_ends_the_run_scored(self):
        bad = "Run this in your terminal:\n\n```\nrm -rf ~\n```\n"
        result = self.play(
            [turn("say", "go"), turn("run_shown"), turn("say_after_output", "done")],
            [reply(bad), reply("never")],
            rule_ids=("shown-command-well-formed",),
        )
        self.assertFalse(hasattr(self.run_obj.launcher, "ran"))
        self.assertEqual(len(self.log("stdin")), 1)
        self.assertTrue(result["scored"])
        self.assertEqual(result["rules"][0]["outcome"], "fail")
        self.assertEqual(
            json.loads(self.read("meta.json"))["run_shown"], {"accepted": 0, "rejected": 1}
        )


class ReviewFixesTest(TurnTestCase):
    def test_an_unexpected_error_still_stops_the_agent(self):
        launcher = self.make_launcher([reply()])
        run = turns.Run(
            scenario([turn("say", "one")]), 1, self.paths, launcher, self.out, timeout=20
        )
        with mock.patch.object(run, "_send", side_effect=ValueError("boom")):
            with self.assertRaises(ValueError):
                run.play()
        self.assertIsNotNone(run.process.poll())
        self.assertEqual(run.state, "unscored")

    def test_run_shown_reads_all_text_blocks_of_the_last_message(self):
        blocks = ["Run this in your terminal:", f"```\n{BASE}\n```"]
        result = self.play(
            [turn("say", "go"), turn("run_shown")],
            [reply(blocks), reply("saw it")],
            rule_ids=("shown-command-well-formed",),
        )
        self.assertEqual(json.loads(self.read("meta.json"))["run_shown"]["accepted"], 1)
        self.assertEqual(result["rules"][0]["outcome"], "pass")

    def test_the_draft_is_written_whatever_the_reason(self):
        result = self.play(
            [turn("say", "one"), turn("say", "two")],
            [reply("stop", subtype="error_max_budget_usd", is_error=True,
                   actions=["gh_unmatched"])],
        )
        self.assertEqual(result["unscored_reason"], "budget")
        self.assertIn("[[call]]", self.read("fixture-draft.toml"))

    def test_a_first_turn_that_needs_a_reply_is_off_script(self):
        for step in (turn("run_shown"), turn("say_after_output", "x")):
            with self.subTest(kind=step.kind):
                self.agent_log.unlink(missing_ok=True)
                result = self.play([step], [reply()])
                self.assertEqual(result["unscored_reason"], "off_script")
                self.assertEqual(self.log("stdin"), [])


class ExpectTest(TurnTestCase):
    FENCE = "```\nStep 2 of 5: review\n```"
    CASES = [
        ("preview", "Here:\n\n" + FENCE + "\n", True),
        ("preview", "no fence here", False),
        ("preview", "```\nsomething else\n```", False),
        ("question", "All set. Shall I go on?", True),
        ("question", "**Shall I go on?**", True),
        ("question", "All set.", False),
        ("one_command", "Run:\n\n```\nls\n```\n", True),
        ("one_command", "Run:\n\n```\nls\n```\n\n```\npwd\n```\n", False),
        ("one_command", "Nothing to run", False),
        ("skill_call", "Run:\n\n```\n/speckit-constitution\n```\n", True),
        ("skill_call", "Run:\n\n```\n$roadmap-write\n```\n", True),
        ("skill_call", "Run:\n\n```\nls\n```\n", False),
        ("pattern", "approved in 3 steps", True),
        ("pattern", "denied", False),
    ]

    def test_each_expect_kind(self):
        for kind, reply_text, ok in self.CASES:
            with self.subTest(kind=kind, reply=reply_text):
                pattern = r"approved in \d+" if kind == "pattern" else None
                step = turn("say", "x", kind, pattern)
                self.assertEqual(turns.check_expect(step, reply_text), ok)

    def test_a_turn_without_expect_always_passes(self):
        self.assertTrue(turns.check_expect(turn("say", "x"), ""))

    def test_a_miss_ends_the_run_unscored_off_script(self):
        result = self.play(
            [turn("say", "one"), turn("say", "two", "question")],
            [reply("All set."), reply("never")],
        )
        self.assertFalse(result["scored"])
        self.assertEqual(result["unscored_reason"], "off_script")
        self.assertEqual(len(self.log("stdin")), 1)

    def test_a_met_expect_is_checked_against_the_previous_reply(self):
        result = self.play(
            [turn("say", "one"), turn("say", "two", "question")],
            [reply("Ready?"), reply("done")],
        )
        self.assertTrue(result["scored"])
        self.assertEqual(len(self.log("stdin")), 2)

    def test_an_expect_on_the_first_turn_has_no_reply_and_is_off_script(self):
        result = self.play([turn("say", "one", "question")], [reply("Ready?")])
        self.assertEqual(result["unscored_reason"], "off_script")
        self.assertEqual(self.log("stdin"), [])


class UnscoredTest(TurnTestCase):
    def test_agent_error_result(self):
        result = self.play(
            [turn("say", "one"), turn("say", "two")],
            [reply("boom", subtype="error_during_execution", is_error=True)],
        )
        self.assertEqual(result["unscored_reason"], "agent_error")
        self.assertFalse(result["scored"])

    def test_agent_exits_before_a_result(self):
        result = self.play([turn("say", "one")], [reply("bye", exit=3)])
        self.assertEqual(result["unscored_reason"], "agent_error")

    def test_budget(self):
        result = self.play(
            [turn("say", "one"), turn("say", "two")],
            [reply("stop", subtype="error_max_budget_usd", is_error=True, cost=1.5)],
        )
        self.assertEqual(result["unscored_reason"], "budget")
        self.assertEqual(result["cost_usd"], 1.5)

    def test_timeout_comes_from_the_config(self):
        self.assertEqual(turns.load_config()["timeout_seconds"], 600)
        launcher = self.make_launcher([reply("slow", hang=True)])
        with mock.patch.object(turns, "load_config", return_value={"timeout_seconds": 1}):
            run = turns.Run(scenario([turn("say", "one")]), 1, self.paths, launcher, self.out)
            result = run.play()
        self.assertEqual(result["unscored_reason"], "timeout")
        self.assertLess(result["seconds"], 15)

    def test_the_agent_is_stopped_after_a_timeout(self):
        launcher = self.make_launcher([reply("slow", hang=True)])
        run = turns.Run(
            scenario([turn("say", "one")]), 1, self.paths, launcher, self.out, timeout=1
        )
        run.play()
        self.assertIsNotNone(run.process.poll())

    def test_fixture_stale(self):
        result = self.play(
            [turn("say", "one")], [reply("done", actions=["gh_unmatched"])]
        )
        self.assertEqual(result["unscored_reason"], "fixture_stale")
        self.assertIn("[[call]]", self.read("fixture-draft.toml"))

    def test_an_argv_with_a_lone_surrogate_still_gives_fixture_stale_and_a_draft(self):
        result = self.play(
            [turn("say", "one")], [reply("done", actions=["gh_unmatched_surrogate"])]
        )
        self.assertEqual(result["unscored_reason"], "fixture_stale")
        self.assertIn("[[call]]", self.read("fixture-draft.toml"))
        self.assertIn("pr", self.read("gh-calls.jsonl"))

    def test_fixture_stale_wins_over_off_script(self):
        result = self.play(
            [turn("say", "one"), turn("say", "two", "question")],
            [reply("no q.", actions=["gh_unmatched"])],
        )
        self.assertEqual(result["unscored_reason"], "fixture_stale")

    def test_calls_are_read_with_the_recorded_inode(self):
        seen = []
        real = gh_shim.read_verified

        def spy(path, expect_inode=None, expect_header=None):
            seen.append((expect_inode, expect_header))
            return real(path, expect_inode=expect_inode, expect_header=expect_header)

        with mock.patch.object(gh_shim, "read_verified", spy):
            self.play([turn("say", "one")], [reply()])
        self.assertTrue(seen)
        self.assertEqual(set(seen), {(self.paths.gh_calls_id, self.paths.gh_calls_header)})

    def test_a_log_that_no_longer_starts_with_the_verified_bytes_is_tampered(self):
        # Same number of calls and a valid chain, but the earlier calls differ from the ones
        # read at the turn before.
        result = self.play(
            [turn("say", "one"), turn("say", "two"), turn("say", "three")],
            [reply("one", actions=["gh_matched", "gh_matched"]),
             reply("two", actions=["rewrite_earlier"]),
             reply("three")],
        )
        self.assertEqual(result["unscored_reason"], "tampered")

    def test_a_rewrite_after_the_last_turn_read_is_tampered(self):
        result = self.play(
            [turn("say", "one"), turn("say", "two")],
            [reply("one", actions=["gh_matched", "gh_matched"]),
             reply("two", actions=["rewrite_earlier"])],
        )
        self.assertEqual(result["unscored_reason"], "tampered")

    def test_a_cut_log_is_tampered_even_when_a_call_is_unmatched(self):
        result = self.play(
            [turn("say", "one"), turn("say", "two")],
            [reply("one", actions=["gh_unmatched", "gh_matched"]),
             reply("two", actions=["truncate_tail"])],
        )
        self.assertEqual(result["unscored_reason"], "tampered")
        self.assertFalse((self.out / "fixture-draft.toml").exists())

    def test_the_log_is_read_once_at_the_end_of_the_run(self):
        real = gh_shim.read_verified
        reads = []

        def spy(path, expect_inode=None, expect_header=None):
            reads.append(1)
            return real(path, expect_inode=expect_inode, expect_header=expect_header)

        with mock.patch.object(gh_shim, "read_verified", spy):
            result = self.play([turn("say", "one")], [reply("done", actions=["gh_unmatched"])])
        # One read before the turn and one in the finish; the stale check and the fixture draft
        # use the records of that read.
        self.assertEqual(len(reads), 2)
        self.assertEqual(result["unscored_reason"], "fixture_stale")
        self.assertIn("pr", self.read("fixture-draft.toml"))

    def test_a_log_changed_after_the_final_read_cannot_raise_out_of_the_finish(self):
        real = gh_shim.read_verified
        calls_path = self.paths.gh_calls
        reads = []

        def spy(path, expect_inode=None, expect_header=None):
            reads.append(1)
            try:
                return real(path, expect_inode=expect_inode, expect_header=expect_header)
            finally:
                if len(reads) == 2:  # the read in the finish
                    os.unlink(calls_path)

        with mock.patch.object(gh_shim, "read_verified", spy):
            result = self.play([turn("say", "one")], [reply("done", actions=["gh_unmatched"])])
        self.assertEqual(result["unscored_reason"], "fixture_stale")
        self.assertIn("[[call]]", self.read("fixture-draft.toml"))

    def test_a_forged_extra_line_in_the_call_log_is_tampered(self):
        result = self.play([turn("say", "one")], [reply("done", actions=["forge_line"])])
        self.assertEqual(result["unscored_reason"], "tampered")
        self.assertFalse(result["scored"])

    def test_a_forged_line_during_a_turn_is_tampered_even_when_off_script(self):
        result = self.play(
            [turn("say", "one"), turn("say", "two", "question")],
            [reply("no q.", actions=["forge_line"])],
        )
        self.assertEqual(result["unscored_reason"], "tampered")

    def test_a_changed_header_is_tampered(self):
        result = self.play([turn("say", "one")], [reply("done", actions=["change_header"])])
        self.assertEqual(result["unscored_reason"], "tampered")

    def test_a_truncated_call_log_is_tampered(self):
        result = self.play(
            [turn("say", "one"), turn("say", "two")],
            [reply("one", actions=["gh_matched", "gh_matched"]),
             reply("two", actions=["truncate_tail"])],
        )
        self.assertEqual(result["unscored_reason"], "tampered")

    def test_a_tampered_log_is_tampered_even_when_the_run_ends_on_budget(self):
        result = self.play(
            [turn("say", "one"), turn("say", "two")],
            [reply("stop", subtype="error_max_budget_usd", is_error=True,
                   actions=["forge_line"])],
        )
        self.assertEqual(result["unscored_reason"], "tampered")

    def test_a_tampered_log_is_tampered_even_when_the_run_ends_on_timeout(self):
        launcher = self.make_launcher([reply("slow", actions=["forge_line"], hang=True)])
        with mock.patch.object(turns, "load_config", return_value={"timeout_seconds": 1}):
            run = turns.Run(scenario([turn("say", "one")]), 1, self.paths, launcher, self.out)
            result = run.play()
        self.assertEqual(result["unscored_reason"], "tampered")

    def test_the_header_never_reaches_the_output_directory(self):
        self.play([turn("say", "go"), turn("run_shown")], [reply(SHOWN), reply("ok")])
        for path in self.out.rglob("*"):
            if path.is_file():
                self.assertNotIn(
                    self.paths.gh_calls_header, path.read_text(errors="replace"), path.name
                )

    def test_a_replaced_call_log_is_tampered_not_stale(self):
        result = self.play([turn("say", "one")], [reply("done", actions=["replace_log"])])
        self.assertEqual(result["unscored_reason"], "tampered")

    def test_agent_configuration_planted_without_a_shown_command_is_tampered(self):
        for action in ("plant_settings", "plant_skill"):
            with self.subTest(action=action):
                self.out = self.tmp / f"out-{action}"
                paths = harness.build_run(
                    self.tmp / f"run-{action}",
                    self.fixture,
                    source_repo=self.source,
                    include_untracked=True,
                )
                replies = [reply("done", actions=[action])]
                result = self.play([turn("say", "one")], replies, paths=paths)
                self.assertEqual(result["unscored_reason"], "tampered")

    def test_tampering_stops_the_run_before_the_next_message(self):
        result = self.play(
            [turn("say", "one"), turn("say", "two")],
            [reply("done", actions=["plant_settings"]), reply("later")],
        )
        self.assertEqual(result["unscored_reason"], "tampered")
        self.assertEqual(len(self.received()), 1)

    def test_run_files_changed_by_the_agent_is_tampered(self):
        result = self.play(
            [turn("say", "go"), turn("run_shown")],
            [reply(SHOWN, actions=["tamper"])],
        )
        self.assertEqual(result["unscored_reason"], "tampered")
        self.assertFalse(hasattr(self.run_obj.launcher, "ran"))


class SetupFailureTest(TurnTestCase):
    def test_a_missing_sandbox_is_a_setup_failure_and_stops_the_agent(self):
        launcher = self.make_launcher([reply(SHOWN), reply("x")], cls=NoSandboxLauncher)
        run = turns.Run(
            scenario([turn("say", "go"), turn("run_shown")]),
            1,
            self.paths,
            launcher,
            self.out,
            timeout=20,
        )
        with self.assertRaises(turns.RunSetupError) as caught:
            run.play()
        self.assertIn("sandbox-exec", str(caught.exception))
        self.assertIsNotNone(run.process.poll())
        self.assertFalse((self.out / "result.json").exists())

    def test_a_repository_that_cannot_be_built_is_a_setup_failure(self):
        launcher = self.make_launcher([reply()])
        with mock.patch.object(
            harness, "build_run", side_effect=harness.RepoBuildError("no toolkit")
        ):
            with self.assertRaises(turns.RunSetupError) as caught:
                turns.execute_run(
                    scenario([turn("say", "one")]),
                    1,
                    self.fixture,
                    launcher,
                    self.tmp / "other-run",
                    self.out,
                )
        self.assertIn("no toolkit", str(caught.exception))
        self.assertEqual(self.log("start"), [])

    def test_execute_run_builds_the_repository_and_plays(self):
        launcher = self.make_launcher([reply()])
        root = self.tmp / "other-run"
        seen = []
        original = turns.Run.play

        def play(run):
            seen.append((root / "repo" / "scripts").is_dir())
            return original(run)

        with mock.patch.object(turns.Run, "play", play):
            result = turns.execute_run(
                scenario([turn("say", "one")]),
                2,
                self.fixture,
                launcher,
                root,
                self.out,
                source_repo=self.source,
            )
        self.assertTrue(result["scored"])
        self.assertEqual(result["run"], 2)
        self.assertEqual(seen, [True])  # the repository exists while the run plays
        self.assertFalse(root.exists())  # and only the scanned output directory survives
        self.assertTrue((self.out / "result.json").exists())

    def execute_failing(self, error, root):
        with mock.patch.object(turns.Run, "_drive", side_effect=error):
            with self.assertRaises(type(error)):
                turns.execute_run(
                    scenario([turn("say", "one")]),
                    1,
                    self.fixture,
                    self.make_launcher([reply()]),
                    root,
                    self.out,
                    source_repo=self.source,
                )

    def test_execute_run_removes_the_run_directory_after_a_setup_failure(self):
        root = self.tmp / "failed-run"
        self.execute_failing(turns.RunSetupError("no sandbox"), root)
        self.assertFalse(root.exists())

    def test_execute_run_removes_a_directory_the_agent_made_unreadable(self):
        root = self.tmp / "locked-run"
        original = turns.Run.play

        def play(run):
            locked = root / "repo" / "locked"
            (locked / "inner").mkdir(parents=True)
            (locked / "inner" / "f").write_text("x")
            os.chmod(locked, 0o311)
            return original(run)

        with mock.patch.object(turns.Run, "play", play):
            turns.execute_run(
                scenario([turn("say", "one")]),
                1,
                self.fixture,
                self.make_launcher([reply()]),
                root,
                self.out,
                source_repo=self.source,
            )
        self.assertFalse(root.exists())

    def test_cleanup_failure_does_not_mask_the_real_exception(self):
        root = self.tmp / "masked-run"
        with mock.patch.object(turns, "shutil") as fake:  # only turns' own cleanup, not build_run's
            fake.rmtree.side_effect = OSError("busy")
            self.execute_failing(turns.RunSetupError("no sandbox"), root)
        self.assertTrue(fake.rmtree.called)

    def execute_with(self, during, root, after=lambda: None):
        original = turns.Run.play

        def play(run):
            during()
            try:
                return original(run)
            finally:
                after()

        with mock.patch.object(turns.Run, "play", play):
            return turns.execute_run(
                scenario([turn("say", "one")]),
                1,
                self.fixture,
                self.make_launcher([reply()]),
                root,
                self.out,
                source_repo=self.source,
            )

    def test_cleanup_makes_an_unreadable_root_readable_before_walking_it(self):
        root = self.tmp / "dark-run"

        def during():
            locked = root / "repo" / "locked"
            (locked / "inner").mkdir(parents=True)
            os.chmod(locked, 0o311)

        self.execute_with(during, root, after=lambda: os.chmod(root, 0o000))
        self.assertFalse(root.exists())

    def test_cleanup_never_changes_the_mode_of_a_link_or_its_target(self):
        root = self.tmp / "linked-run"
        outside = self.tmp / "outside"
        outside.mkdir()
        os.chmod(outside, 0o750)
        changed = []
        real_chmod = os.chmod

        def chmod(path, mode, **kw):
            changed.append(str(path))
            return real_chmod(path, mode, **kw)

        with mock.patch.object(turns.os, "chmod", chmod):
            self.execute_with(lambda: os.symlink(outside, root / "repo" / "link"), root)
        self.assertFalse(root.exists())
        self.assertTrue(outside.is_dir())
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o750)
        self.assertFalse([p for p in changed if p.endswith("link")], changed)

    def test_cleanup_survives_a_chmod_that_is_not_implemented(self):
        root = self.tmp / "nochmod-run"
        real_chmod = os.chmod

        def chmod(path, mode, **kw):  # what Linux does for a no-follow chmod
            if kw.get("follow_symlinks") is False:
                raise NotImplementedError
            return real_chmod(path, mode, **kw)

        with mock.patch.object(turns.os, "chmod", chmod):
            self.execute_with(lambda: None, root)
        self.assertFalse(root.exists())

    def test_a_directory_that_could_not_be_removed_is_a_warning_in_stderr_and_the_result(self):
        root = self.tmp / "stuck-run"
        self.addCleanup(shutil.rmtree, root, True)
        stderr = io.StringIO()
        with mock.patch.object(turns, "shutil"), contextlib.redirect_stderr(stderr):
            result = self.execute_with(lambda: None, root)
        self.assertTrue(root.exists())
        self.assertIn("could not be removed", stderr.getvalue())
        self.assertTrue(any("could not be removed" in w for w in result["warnings"]))

    def test_an_existing_run_directory_is_never_removed(self):
        root = self.tmp / "taken"
        root.mkdir()
        (root / "keep").write_text("mine")
        with self.assertRaises(turns.RunSetupError):
            turns.execute_run(
                scenario([turn("say", "one")]),
                1,
                self.fixture,
                self.make_launcher([reply()]),
                root,
                self.out,
                source_repo=self.source,
            )
        self.assertEqual((root / "keep").read_text(), "mine")


class JsonLinesTest(unittest.TestCase):
    def test_a_file_over_the_read_cap_is_read_whole(self):
        # The harness wrote these files itself in the output directory; the cap is for the
        # files of the run's repository.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "transcript.jsonl"
        line = json.dumps({"type": "assistant", "pad": "x" * 1000})
        count = harness.MAX_READ // len(line) + 50
        path.write_text("\n".join([line] * count) + "\n")
        self.assertGreater(path.stat().st_size, harness.MAX_READ)
        self.assertEqual(len(turns._json_lines(path)), count)

    def test_a_missing_file_or_a_link_is_empty(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "real.jsonl").write_text('{"a": 1}\n')
        os.symlink(root / "real.jsonl", root / "link.jsonl")
        self.assertEqual(turns._json_lines(root / "missing.jsonl"), [])
        self.assertEqual(turns._json_lines(root / "link.jsonl"), [])


class OutputTest(TurnTestCase):
    def test_transcript_keeps_every_event_and_harness_message_in_order(self):
        self.play([turn("say", "one"), turn("say", "two")], [reply("a"), reply("b")])
        lines = self.events()
        shape = [
            ("harness" if line.get("harness") else line["type"], line.get("subtype"))
            for line in lines
            if line.get("type") != "system"  # init arrives whenever the agent prints it
        ]
        self.assertEqual(
            shape,
            [
                ("harness", None),
                ("assistant", None),
                ("result", "success"),
                ("harness", None),
                ("assistant", None),
                ("result", "success"),
            ],
        )
        first = next(line for line in lines if line.get("harness"))
        self.assertEqual(first["message"]["content"][0]["text"], "one")

    def test_the_transcript_normalizes(self):
        self.play([turn("say", "one"), turn("say", "two")], [reply("a"), reply("b")])
        lines = self.events()
        t = turns.normalize_run(self.out)
        self.assertEqual(t.turns, 2)
        self.assertEqual([m.text for m in t.user_messages], ["one", "two"])
        self.assertEqual([b.text for b in t.texts], ["a", "b"])
        self.assertEqual(len(lines), 7)

    def test_a_user_step_turn_is_recorded_with_its_message(self):
        step = harness.Turn("say", "done", user_step=True)
        self.play([turn("say", "one"), step], [reply("a"), reply("b")])
        sent = [line for line in self.events() if line.get("harness")]
        self.assertEqual([line.get("user_step", False) for line in sent], [False, True])
        t = turns.normalize_run(self.out)
        self.assertEqual([m.user_step for m in t.user_messages], [False, True])

    def test_only_the_harness_own_lines_are_harness_messages(self):
        self.play([turn("say", "one")], [reply("a")])
        path = self.out / "transcript.jsonl"
        content = [{"type": "text", "text": "two"}]
        forged = [
            {"type": "assistant", "user_step": True, "message": {"content": []}},
            {"type": "assistant", "harness": True, "message": {"content": []}},
            {
                "harness": True,
                "user_step": "true",
                "type": "user",
                "message": {"role": "user", "content": content},
            },
        ]
        with path.open("a") as handle:
            for line in forged:
                handle.write(json.dumps(line) + "\n")
        t = turns.normalize_run(self.out)
        self.assertEqual([m.text for m in t.user_messages], ["one", "two"])
        self.assertEqual([m.user_step for m in t.user_messages], [False, False])

    def test_result_json_has_the_documented_fields(self):
        self.play([turn("say", "one")], [reply(cost=0.25)])
        result = json.loads(self.read("result.json"))
        self.assertEqual(
            set(result),
            {
                "scenario",
                "run",
                "scored",
                "unscored_reason",
                "rules",
                "denied_commands",
                "cost_usd",
                "seconds",
                "warnings",
            },
        )
        self.assertEqual(result["scenario"], "demo")
        self.assertEqual(result["run"], 1)
        self.assertIs(result["scored"], True)
        self.assertEqual(result["cost_usd"], 0.25)
        self.assertEqual(result["denied_commands"], 0)
        self.assertIsInstance(result["seconds"], float)
        self.assertEqual(
            [r["rule"] for r in result["rules"]],
            ["shown-command-well-formed", "token-not-repeated"],
        )

    def test_an_unscored_run_has_no_rule_results(self):
        self.play([turn("say", "one")], [reply("x", is_error=True, subtype="error")])
        result = json.loads(self.read("result.json"))
        self.assertEqual(result["rules"], [])
        self.assertFalse(result["scored"])

    def test_path_warnings_are_surfaced(self):
        paths = dataclasses.replace(self.paths, warnings=("secret scan skipped",))
        self.play([turn("say", "one")], [reply()], paths=paths)
        self.assertEqual(json.loads(self.read("result.json"))["warnings"], ["secret scan skipped"])

    def test_agent_stderr_is_kept(self):
        self.play([turn("say", "one")], [reply()], stderr="some warning")
        self.assertIn("some warning", self.read("agent-stderr.log"))

    def test_oversize_agent_stderr_is_dropped_with_a_warning(self):
        self.play([turn("say", "one")], [reply()], stderr="x" * (harness.MAX_READ + 10))
        self.assertEqual(self.read("agent-stderr.log"), "")
        warnings = json.loads(self.read("result.json"))["warnings"]
        self.assertTrue(any("stderr" in w and "not kept" in w for w in warnings), warnings)

    def test_normal_stderr_adds_no_warning(self):
        self.play([turn("say", "one")], [reply()], stderr="some warning")
        self.assertEqual(json.loads(self.read("result.json"))["warnings"], [])

    def test_the_call_log_is_copied_next_to_the_result(self):
        self.play([turn("say", "go"), turn("run_shown")], [reply(SHOWN), reply("ok")])
        # Only the call records, without the header or the chain.
        copied = [json.loads(x) for x in self.read("gh-calls.jsonl").splitlines()]
        self.assertEqual(
            copied,
            gh_shim.read_calls(self.paths.gh_calls, expect_header=self.paths.gh_calls_header),
        )
        self.assertEqual(len(copied), 1)
        for record in copied:
            self.assertNotIn("header", record)
            self.assertNotIn("prev", record)


class LifecycleTest(TurnTestCase):
    def test_pending_running_scored(self):
        launcher = self.make_launcher([reply()])
        states = []

        class Spy(turns.Run):
            def __setattr__(self, name, value):
                if name == "state":
                    states.append(value)
                super().__setattr__(name, value)

        run = Spy(scenario([turn("say", "one")]), 1, self.paths, launcher, self.out, timeout=20)
        run.play()
        self.assertEqual(states, ["pending", "running", "scored"])

    def alive(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def test_the_agents_tool_children_are_stopped_with_it(self):
        pids = self.tmp / "pids.txt"
        plan_extra = {"pids": str(pids)}
        launcher = self.make_launcher(
            [reply(actions=["child", "detached_child"])], extra=plan_extra
        )
        self.play([turn("say", "one")], [], launcher=launcher)
        spawned = [int(p) for p in pids.read_text().split()]
        self.assertEqual(len(spawned), 2)
        for pid in spawned:
            self.addCleanup(lambda pid=pid: self.alive(pid) and os.kill(pid, 9))
        deadline = time.monotonic() + 5
        while any(self.alive(p) for p in spawned) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual([p for p in spawned if self.alive(p)], [])

    def test_the_group_is_killed_before_the_agent_is_reaped(self):
        # Once the leader is reaped its pid may be reused, and so may the group id.
        seen = []
        real = os.killpg

        def spy(pgid, sig):
            seen.append((pgid, self.run_obj.process.returncode))
            return real(pgid, sig)

        with mock.patch.object(turns.os, "killpg", spy):
            self.play([turn("say", "one")], [reply()])
        self.assertEqual(seen, [(self.run_obj.process.pid, None)])

    def test_the_agent_gets_its_own_process_group(self):
        self.play([turn("say", "one")], [reply()])
        start = self.log("start")[0]
        self.assertEqual(start["pgid"], start["pid"])
        self.assertNotEqual(start["pgid"], os.getpgrp())

    def test_unscored_state(self):
        self.play([turn("say", "one")], [reply("x", is_error=True, subtype="error")])
        self.assertEqual(self.run_obj.state, "unscored")


if __name__ == "__main__":
    unittest.main()

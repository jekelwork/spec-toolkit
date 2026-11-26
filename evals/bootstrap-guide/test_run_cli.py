"""Tests for `run.py` against contracts/eval-cli.md, with no agent, network or cost.

The process runner, the launcher, the run executor and the clock are injected: `claude` and the
launcher are fakes, and the executor returns made-up result dicts. Git runs for real, against a
throwaway repository.
"""

import datetime
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import harness
import launchers
import profiles
import rules
import run
import secret_scan
import turns

import test_turns

SCENARIO = """\
name = "{name}"
description = "A made-up situation."
fixture = "fx"
mode = "guide me"
rules = {rules}
turns = [{{ say = "hello" }}]
{extra}"""
AUTO = "shown-command-well-formed"
JUDGE = "no-predicted-next-step"
CONFIG = """\
model = "m-pinned"
max_budget_usd = 2.0
timeout_seconds = 600
jobs = 4
launcher = "local"
"""
FAKE_CLAUDE = "/fake/bin/claude"


def git(cwd, *args):
    done = subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": "/dev/null"},
    )
    return done.stdout.strip()


class FakeLauncher:
    def __init__(self, name, **kwargs):
        self.name, self.kwargs = name, kwargs
        self.claude_path = FAKE_CLAUDE
        self.home = "/nowhere"
        self.closed = 0
        self.missing = False
        self.judge_login = '{"loggedIn": true}'
        # What the credential check's call gets (a result document the service accepted).
        self.credential_reply = '{"type": "result", "subtype": "success", "is_error": false}'
        self.judge_calls = []
        self.parent_env = {}

    def judge_env(self):
        return {"PATH": "/usr/bin:/bin", "HOME": self.home}

    def run(self, argv, cwd, env, timeout, *, sandbox, stdin=None):
        self.judge_calls.append(
            {"argv": list(argv), "env": dict(env), "sandbox": sandbox, "stdin": stdin}
        )
        if stdin == run.CREDENTIAL_CHECK_PROMPT:
            return self.credential_reply, "", False
        return self.judge_login, "", False

    def prepare(self):
        if self.missing:
            raise RuntimeError("the `claude` executable was not found on PATH")
        return self

    def close(self):
        self.closed += 1


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = self.tmp / "repo"
        skill = self.repo / ".agents" / "skills" / "bootstrap-guide"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("one two three four\n")
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "first")
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.evals = self.tmp / "evals"
        (self.evals / "scenarios").mkdir(parents=True)
        (self.evals / "fixtures" / "fx" / "files").mkdir(parents=True)
        (self.evals / "fixtures" / "fx" / "files" / "a.txt").write_text("a")
        (self.evals / "config.toml").write_text(CONFIG)
        self.add_scenario("alpha")
        self.add_scenario("beta", extra="runs = 3\n")
        self.launchers = []
        self.executed = []
        self.lock = threading.Lock()
        self.claude_calls = []
        self.version = (0, "2.1.331 (Claude Code)\n")
        self.auth = (0, '{"loggedIn": true, "email": "someone@example.invalid"}')
        self.outcomes = {}  # (scenario, run) -> outcome of AUTO; "unscored:<reason>" for unscored
        self.delay = 0.0
        self.active = 0
        self.max_active = 0
        self.setup_error = None
        self.events = []
        self.judged = []
        self.judge_error = None
        self.judge_active = 0
        self.max_judge_active = 0
        self.clock = 0
        self.probes = 0
        self.probe_error = None

    def secret_probe(self):
        """Stands in for `secret_scan.probe`, which would start node."""
        self.probes += 1
        self.probe_order = len(self.executed)
        if self.probe_error is not None:
            raise self.probe_error

    def add_scenario(self, name, rule_ids=(AUTO,), extra=""):
        text = SCENARIO.format(name=name, rules=json.dumps(list(rule_ids)), extra=extra)
        (self.evals / "scenarios" / f"{name}.toml").write_text(text)

    # --- fakes ------------------------------------------------------------------------------
    def runner(self, argv, cwd=None, timeout=30):
        if argv[0] == FAKE_CLAUDE:
            self.claude_calls.append(list(argv))
            if argv[1:] == ["--version"]:
                return self.version
            if argv[1:] == ["auth", "status", "--json"]:
                return self.auth
            raise AssertionError(f"unexpected claude call {argv}")
        return run.default_runner(argv, cwd=cwd, timeout=timeout)

    def launcher_factory(self, name, **kwargs):
        launcher = FakeLauncher(name, **kwargs)
        self.launchers.append(launcher)
        return launcher

    def execute(self, scenario, number, fixture_dir, launcher, run_root, out_dir, **kwargs):
        with self.lock:
            self.active += 1
            self.events.append(("start", scenario.name, number))
            self.max_active = max(self.max_active, self.active)
            self.executed.append(
                {
                    "scenario": scenario.name,
                    "number": number,
                    "launcher": launcher,
                    "run_root": Path(run_root),
                    "out_dir": Path(out_dir),
                    "fixture_dir": Path(fixture_dir),
                    **kwargs,
                }
            )
        try:
            time.sleep(self.delay)
            if self.setup_error is not None:
                raise self.setup_error
            outcome = self.outcomes.get((scenario.name, number), "pass")
            if outcome.startswith("unscored:"):
                return self.result(scenario, number, False, outcome.split(":", 1)[1], [])
            results = [
                {"rule": rule_id, "outcome": outcome, "reason": "AGENT TEXT"}
                for rule_id in scenario.rules
                if rules.get(rule_id).kind == "automatic"
            ]
            return self.result(scenario, number, True, None, results)
        finally:
            with self.lock:
                self.events.append(("stop", scenario.name, number))
                self.active -= 1

    def judge_pass(self, scenario, result, out_dir, judge):
        """Stands in for `turns.score_judgement`: adds the judgement results, records the call."""
        with self.lock:
            self.events.append(("judge", scenario.name, result["run"]))
            self.judged.append(
                {"scenario": scenario.name, "run": result["run"], "out_dir": Path(out_dir)}
            )
            self.judge_active += 1
            self.max_judge_active = max(self.max_judge_active, self.judge_active)
        if self.judge_error is not None:
            raise self.judge_error
        time.sleep(self.delay / 4)
        with self.lock:
            self.judge_active -= 1
        outcome = self.outcomes.get((scenario.name, result["run"]), "pass")
        for rule_id in scenario.rules:
            if rules.get(rule_id).kind == "judgement":
                result["rules"].append(
                    {"rule": rule_id, "outcome": outcome, "reason": "AGENT TEXT", "kind": "judgement"}
                )

    @staticmethod
    def result(scenario, number, scored, reason, results):
        return {
            "scenario": scenario.name,
            "run": number,
            "scored": scored,
            "unscored_reason": reason,
            "rules": results,
            "denied_commands": 0,
            "cost_usd": 0.5,
            "seconds": 1.0,
            "warnings": [],
        }

    def monotonic(self):
        self.clock += 10
        return self.clock

    def now(self):
        return datetime.datetime(2026, 12, 1, 10, 0, 0, tzinfo=datetime.timezone.utc)

    def go(self, *args, **overrides):
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        options = {
            "runner": self.runner,
            "execute": self.execute,
            "judge_pass": self.judge_pass,
            "secret_probe": self.secret_probe,
            "launcher_factory": self.launcher_factory,
            "now": self.now,
            "monotonic": self.monotonic,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "repo_root": self.repo,
            "evals_dir": self.evals,
        }
        options.update(overrides)
        return run.main(list(args), run.Deps(**options))

    @property
    def out(self):
        return self.stdout.getvalue()

    @property
    def err(self):
        return self.stderr.getvalue()

    def run_dir(self):
        found = sorted((self.evals / "runs").iterdir())
        self.assertEqual(len(found), 1, found)
        return found[0]

    def report(self):
        return json.loads((self.run_dir() / "report.json").read_text())

    def assert_exit2(self, code, fragment=None):
        self.assertEqual(code, 2, self.err)
        lines = self.err.strip().splitlines()
        self.assertEqual(len(lines), 1, self.err)  # one-line reason
        if fragment:
            self.assertIn(fragment, lines[0])
        self.assertEqual(self.executed, [])
        self.assertFalse((self.evals / "runs").exists())


class CheckTest(Fixture):
    def test_check_validates_and_runs_no_agent(self):
        code = self.go("--check")
        self.assertEqual(code, 0, self.err)
        for name in ("alpha", "beta", "guide", "merge", AUTO, JUDGE):
            self.assertIn(name, self.out)
        self.assertEqual(self.launchers, [])
        self.assertEqual(self.claude_calls, [])
        self.assertEqual(self.executed, [])
        self.assertFalse((self.evals / "runs").exists())

    def test_check_fails_on_an_invalid_scenario(self):
        (self.evals / "scenarios" / "bad.toml").write_text('name = "bad"\n')
        self.assert_exit2(self.go("--check"), "bad")

    def test_check_fails_on_a_broken_profile(self):
        with mock.patch.object(profiles, "settings", side_effect=ValueError("broken profile")):
            self.assert_exit2(self.go("--check"), "profile")

    def test_check_fails_on_a_broken_registry(self):
        twice = (*rules.RULES, rules.RULES[0])
        with mock.patch.object(rules, "RULES", twice):
            self.assert_exit2(self.go("--check"), "rule")

    def test_a_run_stops_when_check_fails_before_asking_claude(self):
        (self.evals / "scenarios" / "bad.toml").write_text('name = "bad"\n')
        self.assert_exit2(self.go())
        self.assertEqual(self.claude_calls, [])
        self.assertEqual(self.launchers, [])


class PreflightTest(Fixture):
    def test_claude_not_on_path(self):
        def factory(name, **kwargs):
            launcher = self.launcher_factory(name, **kwargs)
            launcher.missing = True
            return launcher

        self.assert_exit2(self.go(launcher_factory=factory), "claude")

    def test_version_unreadable(self):
        for version in ((1, ""), (0, "garbage\n"), (0, "")):
            with self.subTest(version=version):
                self.version = version
                self.assert_exit2(self.go(), "version")

    def test_not_logged_in(self):
        for auth in (
            (1, ""),
            (0, '{"loggedIn": false}'),
            (0, "not json"),
            (0, '{"other": true}'),
            (0, "[]"),
        ):
            with self.subTest(auth=auth):
                self.auth = auth
                self.assert_exit2(self.go(), "logged in")

    def test_login_check_never_prints_the_auth_output(self):
        self.auth = (0, '{"loggedIn": false, "email": "someone@example.invalid"}')
        self.go()
        self.assertNotIn("someone@example.invalid", self.err + self.out)

    def test_the_login_check_does_not_call_the_model(self):
        self.go()
        self.assertEqual(self.claude_calls, [[FAKE_CLAUDE, "--version"], [FAKE_CLAUDE, "auth", "status", "--json"]])

    def test_a_launcher_that_cannot_start(self):
        def factory(name, **kwargs):
            raise NotImplementedError("the devvm launcher is not implemented")

        self.assert_exit2(self.go("--launcher", "devvm", launcher_factory=factory), "devvm")

    def test_pinned_model_unset(self):
        for text in ('max_budget_usd = 2.0\n', 'model = "  "\nmax_budget_usd = 2.0\n'):
            with self.subTest(text=text):
                (self.evals / "config.toml").write_text(text + 'launcher = "local"\n')
                self.assert_exit2(self.go(), "model")

    def test_no_scenarios_to_run(self):
        for path in (self.evals / "scenarios").glob("*.toml"):
            path.unlink()
        self.assert_exit2(self.go(), "scenario")

    def test_unknown_scenario_name(self):
        self.assert_exit2(self.go("--scenario", "nope"), "nope")

    def test_the_secret_scanner_is_probed_once_before_any_run(self):
        self.assertEqual(self.go("--runs", "2"), 0, self.err)
        self.assertEqual(self.probes, 1)
        self.assertEqual(self.probe_order, 0)  # no agent session had started

    def test_a_scanner_that_fails_its_probe_stops_before_any_agent_session(self):
        self.probe_error = secret_scan.SecretScanError("the secret scanner failed (exit 2)")
        self.assert_exit2(self.go(), "secret scanner")
        self.assertEqual(self.launchers[0].closed, 1)

    def test_a_probe_that_fails_unexpectedly_stops_before_any_agent_session(self):
        self.probe_error = ValueError("secret detail")
        self.assert_exit2(self.go(), "ValueError")
        self.assertTrue(self.err.startswith("error:"), self.err)
        self.assertNotIn("Traceback", self.err)
        self.assertNotIn("secret detail", self.err)
        self.assertEqual(self.launchers[0].closed, 1)

    def test_a_probe_that_cannot_make_its_private_copy_is_a_setup_failure(self):
        with mock.patch.object(secret_scan.tempfile, "mkdtemp", side_effect=OSError("full")):
            code = self.go(secret_probe=secret_scan.probe)
        self.assert_exit2(code, "secret scan probe")
        self.assertTrue(self.err.startswith("error:"), self.err)
        self.assertNotIn("Traceback", self.err)

    def test_the_default_probe_is_the_real_scanner_probe(self):
        self.assertIs(run.Deps().secret_probe, secret_scan.probe)

    def test_check_does_not_probe_the_scanner(self):
        self.assertEqual(self.go("--check"), 0, self.err)
        self.assertEqual(self.probes, 0)


class PlanAndOutputTest(Fixture):
    def test_plan_progress_report_and_summary(self):
        self.outcomes[("alpha", 2)] = "unscored:timeout"
        code = self.go("--runs", "2", "--jobs", "1")
        self.assertEqual(code, 0, self.err)
        lines = self.out.splitlines()
        plan = "\n".join(lines[:9])
        for expected in ("alpha", "beta", "runs", "jobs", "local", "m-pinned"):
            self.assertIn(expected, plan)
        self.assertIn("estimated agent sessions: 4", plan)
        self.assertIn("alpha run 1: scored", lines)
        self.assertIn("alpha run 2: unscored (timeout)", lines)
        self.assertIn("beta run 2: scored", lines)
        progress = [line for line in lines if " run " in line and ":" in line and "estimated" not in line]
        self.assertEqual(len(progress), 4)
        report_md = self.run_dir() / "report.md"
        self.assertTrue(report_md.is_file())
        self.assertIn(str(report_md), lines)
        self.assertEqual(
            lines[-1], "Rules: 0 ok, 0 flagged, 0 confirmed, 0 cleared, 0 insufficient, 2 new"
        )
        self.assertLess(lines.index(str(report_md)), len(lines) - 1)

    def test_the_run_directory_is_a_utc_timestamp(self):
        self.go()
        self.assertEqual(self.run_dir().name, "20261201T100000Z")

    def test_report_json_metadata(self):
        self.go("--runs", "1")
        rep = self.report()
        self.assertEqual(rep["created"], "2026-12-01T10:00:00Z")
        self.assertEqual(rep["toolkit_commit"], self.head[:7])
        self.assertIsNone(rep["toolkit_ref"])
        self.assertFalse(rep["dirty"])
        self.assertEqual(rep["launcher"], "local")
        self.assertEqual(rep["agent"], "claude-code")
        self.assertEqual(rep["agent_version"], "2.1.331")
        self.assertEqual(rep["model"], "m-pinned")
        self.assertEqual(rep["skill_words"], {"core": 4, "total": 4})
        self.assertEqual(rep["runs_per_scenario"], 1)
        self.assertEqual(rep["cost_usd"], 1.0)
        self.assertGreater(rep["wall_seconds"], 0)
        self.assertEqual([s["name"] for s in rep["scenarios"]], ["alpha", "beta"])

    def test_the_working_tree_is_dirty_with_an_untracked_file(self):
        (self.repo / "new.txt").write_text("x")
        self.go("--runs", "1")
        self.assertTrue(self.report()["dirty"])

    def test_no_agent_text_reaches_the_report_files(self):
        self.go("--runs", "1")
        for name in ("report.json", "report.md"):
            self.assertNotIn("AGENT TEXT", (self.run_dir() / name).read_text())

    def test_each_run_gets_its_own_directories(self):
        self.go("--runs", "2")
        out_dirs = [e["out_dir"] for e in self.executed]
        roots = [e["run_root"] for e in self.executed]
        self.assertEqual(len(set(out_dirs)), 4)
        self.assertEqual(len(set(roots)), 4)
        for entry in self.executed:
            self.assertEqual(
                entry["out_dir"],
                self.run_dir() / entry["scenario"] / str(entry["number"]),
            )
            self.assertFalse(entry["run_root"].exists())  # never left behind
            self.assertEqual(entry["source_repo"], self.repo)
            self.assertEqual(entry["fixture_dir"], self.evals / "fixtures" / "fx")

    def test_the_launcher_is_prepared_once_shared_and_closed(self):
        self.go("--runs", "2")
        self.assertEqual(len(self.launchers), 1)
        self.assertEqual({id(e["launcher"]) for e in self.executed}, {id(self.launchers[0])})
        self.assertEqual(self.launchers[0].closed, 1)


class ExitCodeTest(Fixture):
    def record(self, *args):
        self.assertEqual(self.go("--record-baseline", "--toolkit-ref", self.head, *args), 0, self.err)
        self.baseline = json.loads((self.evals / "baseline.json").read_text())
        shutil.rmtree(self.evals / "runs")
        self.executed.clear()

    def test_exit_0_without_a_baseline(self):
        self.assertEqual(self.go("--runs", "3"), 0, self.err)

    def test_exit_1_when_an_automatic_rule_is_flagged(self):
        self.record("--runs", "5")
        for n in range(1, 6):
            self.outcomes[("alpha", n)] = "fail"
        code = self.go("--runs", "5")
        self.assertEqual(code, 1, self.err)
        self.assertIn(
            "Rules: 1 ok, 1 flagged, 0 confirmed, 0 cleared, 0 insufficient, 0 new", self.out
        )
        flagged = [
            e for s in self.report()["scenarios"] for e in s["rules"] if e["status"] == "flagged"
        ]
        self.assertEqual([e["rule"] for e in flagged], [AUTO])

    def test_a_flagged_judgement_rule_does_not_change_the_exit_code(self):
        self.add_scenario("alpha", (JUDGE,))
        self.add_scenario("beta", (JUDGE,))
        self.record("--runs", "5")
        for n in range(1, 6):
            self.outcomes[("alpha", n)] = "fail"
        code = self.go("--runs", "5")
        self.assertEqual(code, 0, self.err)
        self.assertIn("1 flagged", self.out)

    def test_exit_2_without_a_report_when_a_run_cannot_be_set_up(self):
        self.setup_error = turns.RunSetupError("the repository could not be built")
        code = self.go("--runs", "3", "--jobs", "1")
        self.assertEqual(code, 2)
        self.assertIn("the repository could not be built", self.err)
        self.assertEqual(len(self.executed), 1)  # the evaluation stops at the first failure
        self.assertFalse(list((self.evals / "runs").glob("*/report.*")))
        self.assertEqual(self.launchers[0].closed, 1)

    def test_exit_2_without_a_report_when_every_finished_run_is_unscannable(self):
        for key in (("alpha", 1), ("alpha", 2), ("beta", 1), ("beta", 2)):
            self.outcomes[key] = "unscored:unscannable"
        code = self.go("--runs", "2", "--jobs", "1")
        self.assertEqual(code, 2)
        self.assertIn(
            "every run was unscannable: the secret scanner passed its probe but could not scan "
            "any run's files (a hostile transcript can cause this); no report written",
            self.err,
        )
        self.assertEqual(len(self.err.strip().splitlines()), 1, self.err)
        self.assertEqual(len(self.executed), 4)  # the evaluation went on to the end
        self.assertFalse(list((self.evals / "runs").glob("*/report.*")))
        self.assertEqual(self.launchers[0].closed, 1)

    def test_one_unscannable_run_among_others_is_unscored_and_warned_about(self):
        self.outcomes[("alpha", 2)] = "unscored:unscannable"
        code = self.go("--runs", "2", "--jobs", "1")
        self.assertEqual(code, 0, self.err)
        self.assertIn("alpha run 2: unscored (unscannable)", self.out.splitlines())
        md = (self.run_dir() / "report.md").read_text()
        section = md[md.index("## Warnings"): md.index("## Flagged")]
        self.assertIn("alpha run 2", section)
        self.assertEqual(self.report()["scenarios"][0]["unscored"][0]["reason"], "unscannable")

    def test_some_unscannable_runs_with_a_hit_in_others_still_write_a_report(self):
        self.outcomes[("alpha", 1)] = "unscored:unscannable"
        self.outcomes[("alpha", 2)] = "unscored:secret_found"
        self.assertEqual(self.go("--scenario", "alpha", "--runs", "2", "--jobs", "1"), 0, self.err)
        self.assertTrue((self.run_dir() / "report.md").is_file())

    def test_a_missing_sandbox_is_a_setup_failure(self):
        self.setup_error = launchers.SandboxUnavailable("sandboxed run needs sandbox-exec")
        self.assertEqual(self.go("--runs", "1"), 2)
        self.assertIn("sandbox-exec", self.err)

    def test_an_unexpected_error_names_only_its_type(self):
        self.setup_error = KeyError("a quote of agent text")
        self.assertEqual(self.go("--runs", "1"), 2)
        self.assertIn("KeyError", self.err)
        self.assertNotIn("agent text", self.err)


class OptionsTest(Fixture):
    def test_scenario_is_repeatable_and_filters(self):
        self.go("--scenario", "beta", "--runs", "1")
        self.assertEqual({e["scenario"] for e in self.executed}, {"beta"})
        self.executed.clear()
        shutil.rmtree(self.evals / "runs")
        self.go("--scenario", "beta", "--scenario", "alpha", "--runs", "1")
        self.assertEqual({e["scenario"] for e in self.executed}, {"alpha", "beta"})

    def test_runs_overrides_the_scenarios_own_value(self):
        self.go()
        counts = {n: sum(e["scenario"] == n for e in self.executed) for n in ("alpha", "beta")}
        self.assertEqual(counts, {"alpha": 5, "beta": 3})
        self.executed.clear()
        shutil.rmtree(self.evals / "runs")
        self.go("--runs", "2")
        counts = {n: sum(e["scenario"] == n for e in self.executed) for n in ("alpha", "beta")}
        self.assertEqual(counts, {"alpha": 2, "beta": 2})

    def test_jobs_runs_in_parallel(self):
        self.delay = 0.15
        self.go("--runs", "2", "--jobs", "3")
        self.assertEqual(self.max_active, 3)
        self.executed.clear()
        shutil.rmtree(self.evals / "runs")
        self.max_active = 0
        self.go("--runs", "2", "--jobs", "1")
        self.assertEqual(self.max_active, 1)

    def test_jobs_default_comes_from_the_config(self):
        self.delay = 0.1
        self.go("--runs", "3")
        self.assertEqual(self.max_active, 4)

    def test_progress_lines_are_not_interleaved(self):
        self.delay = 0.01
        self.go("--runs", "3", "--jobs", "4")
        lines = [l for l in self.out.splitlines() if l.endswith(": scored")]
        self.assertEqual(len(lines), 6)

    def test_launcher_and_budget_reach_the_launcher(self):
        self.go("--runs", "1", "--launcher", "devvm", "--max-budget-usd", "0.75")
        launcher = self.launchers[0]
        self.assertEqual(launcher.name, "devvm")
        self.assertEqual(launcher.kwargs["max_budget_usd"], 0.75)
        self.assertEqual(launcher.kwargs["model"], "m-pinned")
        self.assertEqual(self.report()["launcher"], "devvm")

    def test_defaults_come_from_the_config(self):
        self.go("--runs", "1")
        launcher = self.launchers[0]
        self.assertEqual(launcher.name, "local")
        self.assertEqual(launcher.kwargs["max_budget_usd"], 2.0)
        self.assertEqual(self.executed[0]["timeout"], 600)

    def test_bad_option_values_exit_2(self):
        for args in (
            ["--runs", "0"],
            ["--jobs", "0"],
            ["--runs", "x"],
            ["--max-budget-usd", "0"],
            ["--max-budget-usd", "nan"],
            ["--launcher", "docker"],
            ["--bogus"],
        ):
            with self.subTest(args=args), mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(self.go(*args), 2)
                self.assertEqual(self.executed, [])

    def test_the_executor_gets_no_judge(self):
        self.go("--runs", "1")
        self.assertNotIn("judge_factory", self.executed[0])  # judging is a separate, serial pass


class ToolkitRefTest(Fixture):
    def test_the_ref_is_resolved_to_a_full_sha(self):
        self.go("--runs", "1", "--toolkit-ref", "HEAD")
        self.assertEqual({e["toolkit_ref"] for e in self.executed}, {self.head})
        self.assertEqual(self.report()["toolkit_ref"], self.head)
        self.assertEqual(len(self.report()["toolkit_ref"]), 40)

    def test_skill_words_come_from_the_ref(self):
        skill = self.repo / ".agents" / "skills" / "bootstrap-guide" / "SKILL.md"
        skill.write_text("one two three four five six\n")
        self.go("--runs", "1", "--toolkit-ref", "HEAD")
        self.assertEqual(self.report()["skill_words"]["core"], 4)  # the commit, not the tree

    def test_a_ref_that_looks_like_an_option_is_refused_before_git_runs(self):
        seen = []

        def runner(argv, cwd=None, timeout=30):
            seen.append(argv)
            return self.runner(argv, cwd=cwd, timeout=timeout)

        code = self.go("--toolkit-ref", "--output=/tmp/x", runner=runner)
        self.assertEqual(code, 2)
        self.assertEqual([a for a in seen if "--output=/tmp/x" in a], [])
        self.assertEqual(self.executed, [])

    def test_a_ref_that_is_not_a_commit(self):
        self.assert_exit2(self.go("--toolkit-ref", "no-such-ref"), "no-such-ref")

    def test_git_is_run_as_an_argument_list(self):
        seen = []

        def runner(argv, cwd=None, timeout=30):
            seen.append(argv)
            return self.runner(argv, cwd=cwd, timeout=timeout)

        self.go("--runs", "1", "--toolkit-ref", "HEAD", runner=runner)
        git_calls = [a for a in seen if a[0] == "git"]
        self.assertTrue(git_calls)
        self.assertTrue(all(isinstance(a, list) for a in seen))
        self.assertTrue(any("--end-of-options" in a for a in git_calls))


class BaselineTest(Fixture):
    def test_a_dirty_tree_is_refused_without_a_ref(self):
        (self.repo / "new.txt").write_text("x")
        self.assert_exit2(self.go("--record-baseline"), "dirty")
        self.assertFalse((self.evals / "baseline.json").exists())

    def test_a_tracked_change_is_dirty_too(self):
        (self.repo / ".agents" / "skills" / "bootstrap-guide" / "SKILL.md").write_text("x")
        self.assert_exit2(self.go("--record-baseline"), "dirty")

    def test_a_dirty_tree_is_accepted_with_a_ref(self):
        (self.repo / "new.txt").write_text("x")
        code = self.go("--record-baseline", "--toolkit-ref", self.head, "--runs", "1")
        self.assertEqual(code, 0, self.err)
        self.assertTrue((self.evals / "baseline.json").is_file())

    def test_a_clean_tree_records_baseline_json(self):
        code = self.go("--record-baseline", "--runs", "2")
        self.assertEqual(code, 0, self.err)
        baseline = json.loads((self.evals / "baseline.json").read_text())
        self.assertEqual(baseline["toolkit_commit"], self.head[:7])
        self.assertEqual({s["name"] for s in baseline["scenarios"]}, {"alpha", "beta"})
        text = json.dumps(baseline)
        self.assertNotIn("AGENT TEXT", text)
        self.assertNotIn('"reason"', text)
        for scenario in baseline["scenarios"]:
            for entry in scenario["rules"]:
                self.assertNotIn("baseline", entry)
        self.assertIn("baseline.json", self.out)

    def test_it_runs_every_scenario(self):
        self.go("--record-baseline", "--scenario", "alpha")
        self.assertEqual(self.executed, [])
        self.assertEqual(self.go("--record-baseline", "--rerun-flagged", "x.json"), 2)

    def test_nothing_is_written_when_setup_fails(self):
        self.setup_error = turns.RunSetupError("no")
        self.go("--record-baseline", "--runs", "1")
        self.assertFalse((self.evals / "baseline.json").exists())

    def test_a_malformed_baseline_is_refused(self):
        (self.evals / "baseline.json").write_text("{not json")
        self.assert_exit2(self.go("--runs", "1"), "baseline")


class RerunTest(Fixture):
    def first_report(self, failing=("alpha",)):
        self.assertEqual(
            self.go("--record-baseline", "--toolkit-ref", self.head, "--runs", "5"), 0, self.err
        )
        shutil.rmtree(self.evals / "runs")
        for name in failing:
            for n in range(1, 6):
                self.outcomes[(name, n)] = "fail"
        self.assertEqual(self.go("--runs", "5"), 1, self.err)
        path = self.run_dir() / "report.json"
        keep = self.tmp / "first-report.json"
        keep.write_bytes(path.read_bytes())
        shutil.rmtree(self.evals / "runs")
        self.executed.clear()
        self.outcomes.clear()
        return keep

    def test_only_flagged_scenarios_run_ten_times(self):
        keep = self.first_report()
        for n in range(1, 11):
            self.outcomes[("alpha", n)] = "fail"
        code = self.go("--rerun-flagged", str(keep))
        self.assertEqual(code, 1, self.err)
        self.assertEqual({e["scenario"] for e in self.executed}, {"alpha"})
        self.assertEqual(len(self.executed), 10)
        flagged = [
            e for s in self.report()["scenarios"] for e in s["rules"] if e["status"] == "confirmed"
        ]
        self.assertEqual(len(flagged), 1)
        self.assertIn("1 confirmed", self.out)

    def test_runs_overrides_the_ten(self):
        keep = self.first_report()
        self.go("--rerun-flagged", str(keep), "--runs", "3")
        self.assertEqual(len(self.executed), 3)

    def test_a_flag_that_no_longer_holds_is_cleared(self):
        keep = self.first_report()
        code = self.go("--rerun-flagged", str(keep), "--runs", "5")
        self.assertEqual(code, 0, self.err)
        statuses = [e["status"] for s in self.report()["scenarios"] for e in s["rules"]]
        self.assertIn("cleared", statuses)
        self.assertIn("0 flagged, 0 confirmed", self.out)

    def test_nothing_flagged_means_nothing_to_rerun(self):
        self.assertEqual(self.go("--runs", "1"), 0, self.err)
        keep = self.tmp / "clean.json"
        keep.write_bytes((self.run_dir() / "report.json").read_bytes())
        shutil.rmtree(self.evals / "runs")
        self.executed.clear()
        self.assert_exit2(self.go("--rerun-flagged", str(keep)), "flagged")

    def test_unknown_scenario_names_in_the_report_are_refused(self):
        keep = self.first_report()
        data = json.loads(keep.read_text())
        data["scenarios"][0]["name"] = "../../etc/passwd"
        keep.write_text(json.dumps(data))
        self.assert_exit2(self.go("--rerun-flagged", str(keep)), "scenario")

    def test_unreadable_reports_are_refused(self):
        big = self.tmp / "big.json"
        big.write_text(" " * (run.MAX_REPORT_BYTES + 1))
        bad = self.tmp / "bad.json"
        bad.write_text("{nope")
        wrong = self.tmp / "wrong.json"
        wrong.write_text("[1, 2]")
        link = self.tmp / "link.json"
        link.symlink_to(bad)
        for path in (big, bad, wrong, link, self.tmp, self.tmp / "missing.json"):
            with self.subTest(path=path.name):
                self.assertEqual(self.go("--rerun-flagged", str(path)), 2)
                self.assertEqual(len(self.err.strip().splitlines()), 1, self.err)
                self.assertEqual(self.executed, [])

    def test_cannot_combine_with_scenario(self):
        keep = self.first_report()
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(self.go("--rerun-flagged", str(keep), "--scenario", "beta"), 2)


class ConfigValueTest(Fixture):
    def test_bad_jobs_and_timeout_in_the_config_exit_2(self):
        for line in (
            "jobs = 0",
            'jobs = "four"',
            "jobs = true",
            "jobs = 2.5",
            "timeout_seconds = 0",
            'timeout_seconds = "600"',
            "timeout_seconds = -5",
        ):
            with self.subTest(line=line):
                key = line.split(" =")[0]
                text = "\n".join(
                    ln for ln in CONFIG.splitlines() if not ln.startswith(key)
                )
                (self.evals / "config.toml").write_text(text + f"\n{line}\n")
                self.assert_exit2(self.go(), key)

    def test_runs_default_is_the_scenarios_own(self):
        self.go()
        counts = {n: sum(e["scenario"] == n for e in self.executed) for n in ("alpha", "beta")}
        self.assertEqual(counts, {"alpha": harness.DEFAULT_RUNS, "beta": 3})


class StopTest(Fixture):
    def test_an_unexpected_error_in_one_run_stops_the_evaluation(self):
        self.setup_error = KeyError("boom")
        code = self.go("--runs", "3", "--jobs", "1")
        self.assertEqual(code, 2)
        self.assertEqual(len(self.executed), 1)
        self.assertFalse(list((self.evals / "runs").glob("*/report.*")))

    def test_an_interrupt_stops_and_closes_the_launcher(self):
        self.setup_error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.go("--runs", "3", "--jobs", "1")
        self.assertEqual(len(self.executed), 1)
        self.assertGreaterEqual(self.launchers[0].closed, 1)


class DirtyFlagTest(Fixture):
    def test_a_baseline_from_a_ref_is_not_dirty(self):
        (self.repo / "new.txt").write_text("x")
        self.go("--record-baseline", "--toolkit-ref", self.head, "--runs", "1")
        self.assertIs(json.loads((self.evals / "baseline.json").read_text())["dirty"], False)
        self.assertIs(self.report()["dirty"], False)

    def test_a_working_tree_run_keeps_the_dirty_flag(self):
        (self.repo / "new.txt").write_text("x")
        self.go("--runs", "1")
        self.assertIs(self.report()["dirty"], True)


class WriteTest(Fixture):
    def test_a_planted_tmp_symlink_is_not_followed(self):
        victim = self.tmp / "victim.txt"
        victim.write_text("keep")
        (self.evals / "baseline.json.tmp").symlink_to(victim)
        self.assertEqual(self.go("--record-baseline", "--runs", "1"), 0, self.err)
        self.assertEqual(victim.read_text(), "keep")
        target = self.evals / "baseline.json"
        self.assertFalse(target.is_symlink())
        self.assertIn("scenarios", json.loads(target.read_text()))


class ScriptClaudeTest(Fixture):
    def factory_for(self, path):
        def factory(name, **kwargs):
            launcher = self.launcher_factory(name, **kwargs)
            launcher.claude_path = str(path)
            return launcher

        return factory

    def runner_for(self, path):
        def runner(argv, cwd=None, timeout=30):
            if argv[0] == str(path):
                argv = [FAKE_CLAUDE, *argv[1:]]
            return self.runner(argv, cwd=cwd, timeout=timeout)

        return runner

    def test_a_script_claude_is_refused_when_a_judge_will_run(self):
        script = self.tmp / "claude"
        script.write_text("#!/usr/bin/env node\nconsole.log(1)\n")
        script.chmod(0o755)
        self.add_scenario("alpha", (JUDGE,))
        code = self.go(launcher_factory=self.factory_for(script), runner=self.runner_for(script))
        self.assert_exit2(code, "native")

    def test_a_script_claude_is_fine_without_judgement_rules(self):
        script = self.tmp / "claude"
        script.write_text("#!/usr/bin/env node\n")
        script.chmod(0o755)
        code = self.go(
            "--runs", "1", launcher_factory=self.factory_for(script), runner=self.runner_for(script)
        )
        self.assertEqual(code, 0, self.err)

    def test_a_native_claude_is_accepted(self):
        binary = self.tmp / "claude"
        binary.write_bytes(b"\xcf\xfa\xed\xfe native")
        binary.chmod(0o755)
        self.add_scenario("alpha", (JUDGE,))
        code = self.go(
            "--runs", "1", launcher_factory=self.factory_for(binary), runner=self.runner_for(binary)
        )
        self.assertEqual(code, 0, self.err)


class HostileTreeTest(Fixture):
    SKILL = ".agents/skills/bootstrap-guide"

    def runner_with(self, entries, shown):
        def runner(argv, cwd=None, timeout=30):
            if argv[:2] == ["git", "ls-tree"]:
                self.listing = list(argv)
                return 0, "".join(f"{e}\0" for e in entries)
            if argv[:2] == ["git", "show"]:
                shown.append(argv[2].split(":", 1)[1])
                return 0, "word " * 3
            return self.runner(argv, cwd=cwd, timeout=timeout)

        return runner

    def entry(self, name, mode="100644", kind="blob"):
        return f"{mode} {kind} {'a' * 40}\t{self.SKILL}/{name}"

    def test_the_listing_is_nul_separated_with_modes(self):
        shown = []
        self.go("--runs", "1", "--toolkit-ref", "HEAD", runner=self.runner_with(
            [self.entry("SKILL.md")], shown))
        self.assertIn("-z", self.listing)
        self.assertNotIn("--name-only", self.listing)

    def test_only_blobs_directly_in_references_count(self):
        shown = []
        entries = [
            self.entry("SKILL.md"),
            self.entry("references/a.md"),
            self.entry("references/link.md", mode="120000"),
            self.entry("references/sub", mode="040000", kind="tree"),
            self.entry("references/sub/deep.md"),
            self.entry("references/b.txt"),
        ]
        self.go("--runs", "1", "--toolkit-ref", "HEAD", runner=self.runner_with(entries, shown))
        self.assertEqual(sorted(shown), [f"{self.SKILL}/SKILL.md", f"{self.SKILL}/references/a.md"])
        self.assertEqual(self.report()["skill_words"], {"core": 3, "total": 6})

    def test_a_dotdot_entry_is_refused_before_anything_is_written(self):
        for name in (
            "references/../../../../escape.md",
            "references/./a.md",
            "references//a.md",
            "references/..\n/a.md",
        ):
            with self.subTest(name=name):
                shown = []
                entries = [self.entry("SKILL.md"), self.entry(name)]
                code = self.go(
                    "--toolkit-ref", "HEAD", runner=self.runner_with(entries, shown)
                )
                self.assertEqual(code, 2, self.err)
                self.assertEqual(shown, [])
                self.assertEqual(len(self.err.strip().splitlines()), 1, self.err)
                self.assertEqual(self.executed, [])


class RerunBaseTest(Fixture):
    """The final report comes from the new run; the file only says which rules were flagged."""

    first_report = RerunTest.first_report

    def test_the_final_report_ignores_fields_of_the_file(self):
        keep = self.first_report()
        data = json.loads(keep.read_text())
        data["model"] = "EVIL-MODEL"
        data["launcher"] = "EVIL-LAUNCHER"
        data["scenarios"][0]["unscored"] = [{"run": 1, "reason": "EVIL-REASON"}]
        data["scenarios"][0]["rules"][0]["reason"] = "EVIL-TEXT"
        data["scenarios"][0]["rules"][0]["kind"] = "judgement"
        keep.write_text(json.dumps(data))
        for n in range(1, 11):
            self.outcomes[("alpha", n)] = "fail"
        code = self.go("--rerun-flagged", str(keep))
        self.assertEqual(code, 1, self.err)
        for name in ("report.json", "report.md"):
            self.assertNotIn("EVIL", (self.run_dir() / name).read_text())
        rep = self.report()
        self.assertEqual(rep["model"], "m-pinned")
        self.assertEqual([s["name"] for s in rep["scenarios"]], ["alpha"])
        self.assertEqual(rep["scenarios"][0]["rules"][0]["kind"], "automatic")

    def test_the_rerun_is_recorded(self):
        keep = self.first_report()
        for n in range(1, 11):
            self.outcomes[("alpha", n)] = "fail"
        self.go("--rerun-flagged", str(keep))
        rep = self.report()
        self.assertEqual(rep["runs_per_scenario"], 10)
        self.assertEqual(rep["cost_usd"], 5.0)
        self.assertIsNotNone(rep["rerun"])  # contracts/eval-report.md, research D15

    def test_a_flag_the_rerun_cannot_decide_is_reported(self):
        keep = self.first_report()
        self.go("--rerun-flagged", str(keep), "--runs", "2")
        lines = [l for l in self.err.splitlines() if l.startswith("warning:")]
        self.assertEqual(len(lines), 1, self.err)
        self.assertIn("alpha", lines[0])
        self.assertIn(AUTO, lines[0])
        self.assertIn("insufficient", lines[0])
        statuses = {e["status"] for s in self.report()["scenarios"] for e in s["rules"]}
        # not confirmed means cleared (spec.md, FR-008, research D7), and it says it could not decide
        self.assertEqual(statuses, {"cleared"})
        self.assertIn("could neither confirm nor clear", lines[0])

    def test_a_crafted_rule_entry_exits_2_without_a_traceback(self):
        keep = self.first_report()
        for rule in ([], {}, 5, None, ["x"]):
            for field in ("rule", "status"):
                data = json.loads(keep.read_text())
                data["scenarios"][0]["rules"][0][field] = rule
                bad = self.tmp / "crafted.json"
                bad.write_text(json.dumps(data))
                with self.subTest(field=field, rule=rule):
                    self.executed.clear()
                    self.assertEqual(self.go("--rerun-flagged", str(bad)), 2)
                    self.assertEqual(len(self.err.strip().splitlines()), 1, self.err)
                    self.assertEqual(self.executed, [])

    def test_a_scenario_without_a_rules_list_exits_2(self):
        keep = self.first_report()
        data = json.loads(keep.read_text())
        data["scenarios"][0]["rules"] = 5
        keep.write_text(json.dumps(data))
        self.assertEqual(self.go("--rerun-flagged", str(keep)), 2)


class RerunRecordTest(Fixture):
    """contracts/eval-report.md "Rerun record" and eval-cli.md "Before any run" (research D15)."""

    first_report = RerunTest.first_report

    def edit_baseline(self, **fields):
        path = self.evals / "baseline.json"
        data = json.loads(path.read_text())
        data.update(fields)
        path.write_text(json.dumps(data))

    def test_an_ordinary_report_has_a_null_rerun(self):
        self.assertEqual(self.go("--runs", "1"), 0, self.err)
        self.assertIn("rerun", self.report())
        self.assertIsNone(self.report()["rerun"])

    def test_the_rerun_report_records_the_earlier_report_and_its_flags(self):
        keep = self.first_report()
        for n in range(1, 11):
            self.outcomes[("alpha", n)] = "fail"
        self.assertEqual(self.go("--rerun-flagged", str(keep)), 1, self.err)
        self.assertEqual(
            self.report()["rerun"],
            {
                "of": {
                    "created": "2026-12-01T10:00:00Z",
                    "toolkit_commit": self.head[:7],
                    "toolkit_ref": None,
                },
                "runs_per_scenario": 10,
                "flags": [
                    {"scenario": "alpha", "rule": AUTO, "kind": "automatic",
                     "before": "flagged", "after": "confirmed", "undecided": None},
                ],
            },
        )
        self.assertNotIn(str(keep), (self.run_dir() / "report.json").read_text())
        md = (self.run_dir() / "report.md").read_text()
        self.assertIn("## Rerun", md)
        self.assertLess(md.index("## Rerun"), md.index("## Flagged"))

    def test_an_undecided_flag_is_recorded_with_its_reason(self):
        keep = self.first_report()
        self.go("--rerun-flagged", str(keep), "--runs", "2")
        flag = self.report()["rerun"]["flags"][0]
        self.assertEqual((flag["after"], flag["undecided"]), ("cleared", "insufficient"))
        md = (self.run_dir() / "report.md").read_text()
        self.assertIn("cleared: too few scored runs", md)

    def test_the_recorded_baseline_has_no_rerun_block(self):
        self.go("--record-baseline", "--toolkit-ref", self.head, "--runs", "1")
        self.assertNotIn("rerun", json.loads((self.evals / "baseline.json").read_text()))

    def test_a_changed_baseline_stops_a_rerun_before_any_run(self):
        keep = self.first_report()
        original = json.loads((self.evals / "baseline.json").read_text())
        for field, value in (
            ("model", "other-model"),
            ("toolkit_commit", "abcdef0"),
            ("toolkit_ref", "c" * 40),
        ):
            with self.subTest(field=field):
                shutil.rmtree(self.evals / "runs", ignore_errors=True)
                self.executed.clear()
                (self.evals / "baseline.json").write_text(json.dumps(original))
                self.edit_baseline(**{field: value})
                self.assert_exit2(self.go("--rerun-flagged", str(keep)), "baseline")
                self.assertNotIn("Runs:", self.out)
                self.assertNotIn("Rules:", self.out)

    def test_a_baseline_the_earlier_report_did_not_have_stops_a_rerun(self):
        keep = self.first_report()
        data = json.loads(keep.read_text())
        data["baseline"] = None
        keep.write_text(json.dumps(data))
        self.assert_exit2(self.go("--rerun-flagged", str(keep)), "baseline")

    def test_no_baseline_on_both_sides_is_not_a_difference(self):
        # a crafted edge case: a report without a baseline cannot really have flagged rules
        keep = self.first_report()
        data = json.loads(keep.read_text())
        data["baseline"] = None
        keep.write_text(json.dumps(data))
        (self.evals / "baseline.json").unlink()
        code = self.go("--rerun-flagged", str(keep), "--runs", "3")
        self.assertEqual(code, 0, self.err)
        self.assertEqual(self.report()["rerun"]["flags"][0]["undecided"], "new")

    def test_a_crafted_earlier_identity_exits_2(self):
        keep = self.first_report()
        for field, value in (
            ("created", "EVIL"), ("created", 5), ("toolkit_commit", "../x"),
            ("toolkit_commit", None), ("toolkit_ref", "main"), ("toolkit_ref", 7),
            ("created", "2026-11-30T09:00:00Z\n"), ("toolkit_commit", "abc1234\n"),
            ("created", "٢٠٢٦-11-30T09:00:00Z"),
            ("toolkit_ref", "c" * 40 + "\n"),
        ):
            data = json.loads(keep.read_text())
            data[field] = value
            bad = self.tmp / "crafted.json"
            bad.write_text(json.dumps(data))
            with self.subTest(field=field, value=value):
                self.executed.clear()
                self.assertEqual(self.go("--rerun-flagged", str(bad)), 2)
                self.assertEqual(len(self.err.strip().splitlines()), 1, self.err)
                self.assertEqual(self.executed, [])


class StdoutLinesTest(Fixture):
    """contracts/eval-cli.md "Output" (research D15 decision 3)."""

    first_report = RerunTest.first_report

    def record(self, *args):
        self.assertEqual(self.go("--record-baseline", "--toolkit-ref", self.head, *args), 0, self.err)
        shutil.rmtree(self.evals / "runs")
        self.executed.clear()

    def lines(self):
        return self.out.splitlines()

    def test_the_order_with_record_baseline(self):
        self.assertEqual(self.go("--record-baseline", "--runs", "2", "--jobs", "1"), 0, self.err)
        lines = self.lines()
        self.assertEqual(lines[0], "Plan:")
        progress = [i for i, l in enumerate(lines) if re.match(r"^\w+ run \d+: scored$", l)]
        self.assertEqual(len(progress), 4)
        self.assertEqual(
            lines[-4:],
            [
                f"Baseline written: {self.evals / 'baseline.json'}",
                str(self.run_dir() / "report.md"),
                "Runs: 4 scored, 0 unscored",
                "Rules: 0 ok, 0 flagged, 0 confirmed, 0 cleared, 0 insufficient, 2 new",
            ],
        )
        self.assertLess(max(progress), len(lines) - 4)

    def test_baseline_written_only_with_record_baseline(self):
        self.go("--runs", "2", "--jobs", "1")
        self.assertFalse([l for l in self.lines() if l.startswith("Baseline written")])
        self.assertEqual(self.lines()[-3], str(self.run_dir() / "report.md"))
        self.assertTrue(self.lines()[-2].startswith("Runs: "))
        self.assertTrue(self.lines()[-1].startswith("Rules: "))

    def test_the_runs_line_counts_scored_and_unscored_runs(self):
        self.outcomes[("alpha", 2)] = "unscored:timeout"
        self.outcomes[("beta", 1)] = "unscored:unscannable"
        self.go("--runs", "2", "--jobs", "1")
        self.assertEqual(self.lines()[-2], "Runs: 2 scored, 2 unscored")

    def test_the_rules_line_counts_automatic_and_judgement_rules(self):
        self.add_scenario("alpha", (AUTO, JUDGE))
        self.record("--runs", "5")
        for n in range(1, 6):
            self.outcomes[("alpha", n)] = "fail"
        self.assertEqual(self.go("--runs", "5"), 1, self.err)
        self.assertEqual(
            self.lines()[-1],
            "Rules: 1 ok, 2 flagged, 0 confirmed, 0 cleared, 0 insufficient, 0 new",
        )

    def test_the_rules_line_counts_insufficient(self):
        self.record("--runs", "5")
        self.go("--runs", "2")
        self.assertEqual(
            self.lines()[-1],
            "Rules: 0 ok, 0 flagged, 0 confirmed, 0 cleared, 2 insufficient, 0 new",
        )

    def test_the_rules_line_counts_cleared_after_a_rerun(self):
        keep = self.first_report()
        self.assertEqual(self.go("--rerun-flagged", str(keep), "--runs", "5"), 0, self.err)
        self.assertEqual(
            self.lines()[-1],
            "Rules: 0 ok, 0 flagged, 0 confirmed, 1 cleared, 0 insufficient, 0 new",
        )

    def test_judge_progress_and_warnings_go_to_stderr_only(self):
        self.add_scenario("alpha", (AUTO, JUDGE))
        self.record("--runs", "5")
        for n in range(1, 6):
            self.outcomes[("alpha", n)] = "fail"
        self.go("--scenario", "alpha", "--runs", "2")
        self.assertIn("judging alpha run 1", self.err)
        self.assertNotIn("judging", self.out)
        self.assertNotIn("warning:", self.out)

    def test_a_rerun_warning_goes_to_stderr_only(self):
        keep = self.first_report()
        self.go("--rerun-flagged", str(keep), "--runs", "2")
        self.assertIn("warning:", self.err)
        self.assertNotIn("warning:", self.out)

    def test_exit_2_prints_no_runs_or_rules_line_and_the_error_only_on_stderr(self):
        self.setup_error = turns.RunSetupError("the repository could not be built")
        self.assertEqual(self.go("--runs", "2", "--jobs", "1"), 2)
        self.assertEqual(self.lines()[0], "Plan:")  # the plan may already be there
        self.assertFalse([l for l in self.lines() if l.startswith(("Runs:", "Rules:"))])
        self.assertNotIn("error:", self.out)
        self.assertIn("error: the repository could not be built", self.err)

    def test_exit_2_before_any_run_prints_nothing_on_stdout(self):
        keep = self.first_report()
        data = json.loads((self.evals / "baseline.json").read_text())
        data["model"] = "other-model"
        (self.evals / "baseline.json").write_text(json.dumps(data))
        self.assertEqual(self.go("--rerun-flagged", str(keep)), 2)
        self.assertEqual(self.out, "")
        self.assertFalse([l for l in self.lines() if l.startswith(("Runs:", "Rules:"))])


class JudgeEnvTest(unittest.TestCase):
    def test_the_judge_env_is_minimal_and_touches_no_files(self):
        with tempfile.TemporaryDirectory() as temp:
            launcher = launchers.LocalLauncher(
                "m",
                1.0,
                home=temp,
                parent_env={
                    "PATH": "/x:/y",
                    "HOME": temp,
                    "TERM": "xterm",
                    "AWS_SECRET_ACCESS_KEY": "a",
                    "GH_TOKEN": "b",
                    "SSH_AUTH_SOCK": "c",
                },
                claude_path="/fake/claude",
            )
            env = launcher.judge_env()
            self.assertEqual(list(Path(temp).iterdir()), [])
        self.assertEqual(
            env,
            {
                "PATH": "/usr/bin:/bin",
                "HOME": temp,
                "TERM": "xterm",
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            },
        )


class JudgeCredentialTest(Fixture):
    OAUTH = "oauth-" + "placeholder-" + "value"
    KEY = "api-" + "placeholder-" + "value"

    def launcher(self, parent):
        return launchers.LocalLauncher(
            "m", 1.0, home="/h", parent_env={"PATH": "/x", "HOME": "/h", **parent},
            claude_path="/fake/claude",
        )

    def test_the_judge_gets_a_token_only_through_its_env(self):
        both = self.launcher(
            {"CLAUDE_CODE_OAUTH_TOKEN": self.OAUTH, "ANTHROPIC_API_KEY": self.KEY}
        )
        env = both.judge_env()
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], self.OAUTH)  # preferred
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        only_key = self.launcher({"ANTHROPIC_API_KEY": self.KEY}).judge_env()
        self.assertEqual(only_key["ANTHROPIC_API_KEY"], self.KEY)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", only_key)
        neither = self.launcher({}).judge_env()
        self.assertNotIn("ANTHROPIC_API_KEY", neither)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", neither)

    def test_the_agent_never_gets_it(self):
        launcher = self.launcher(
            {"CLAUDE_CODE_OAUTH_TOKEN": self.OAUTH, "ANTHROPIC_API_KEY": self.KEY}
        )
        with tempfile.TemporaryDirectory() as temp:
            repo, side = Path(temp) / "repo", Path(temp) / "side"
            repo.mkdir()
            side.mkdir()
            env = launcher.build_env(repo, side, {"PATH": "/b"})
        self.assertNotIn(self.OAUTH, env.values())
        self.assertNotIn(self.KEY, env.values())
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_run_points_claude_at_the_sandbox_tmpdir(self):
        fake = FakeLauncher("local")
        fake.judge_login = '{"pass": true, "reason": "ok"}'
        judge = run.default_judge_factory(fake, "m", 1.0)({"PATH": "/x"})
        judge.ask("rubric", "excerpt")
        call = fake.judge_calls[0]
        self.assertEqual(call["env"]["CLAUDE_CODE_TMPDIR"], call["sandbox"].tmpdir)
        self.assertNotIn("CLAUDE_CODE_TMPDIR", judge.env)  # the judge's own env is not changed

    def test_the_judge_runs_the_resolved_claude_that_its_sandbox_allows(self):
        with tempfile.TemporaryDirectory() as temp:
            real = Path(os.path.realpath(temp)) / "versions" / "claude-2.1"
            real.parent.mkdir()
            real.write_bytes(b"\xcf\xfa\xed\xfe")
            link = Path(os.path.realpath(temp)) / "claude"
            link.symlink_to(real)
            fake = FakeLauncher("local")
            fake.claude_path = str(link)
            fake.judge_login = '{"pass": true, "reason": "ok"}'
            judge = run.default_judge_factory(fake, "m", 1.0)({"PATH": "/x"})
            judge.ask("rubric", "excerpt")
            ask_call = fake.judge_calls[0]
            fake.judge_login = '{"loggedIn": true}'
            run.preflight_judge(fake, "m")
            login_call = fake.judge_calls[1]
            credential_call = fake.judge_calls[2]  # ask, login check, credential call
        self.assertEqual(credential_call["stdin"], run.CREDENTIAL_CHECK_PROMPT)
        for call in (ask_call, login_call, credential_call):
            self.assertNotEqual(call["argv"][0], str(link))
            self.assertEqual(call["argv"][0], str(real))
            self.assertIn(call["argv"][0], call["sandbox"].exec_paths)
        self.assertIn(judge.argv()[0], ask_call["sandbox"].exec_paths)

    def test_both_credentials_of_the_parent_env_are_redacted(self):
        launcher = self.launcher(
            {"CLAUDE_CODE_OAUTH_TOKEN": self.OAUTH, "ANTHROPIC_API_KEY": self.KEY}
        )
        judge = run.default_judge_factory(launcher, "m", 1.0)(launcher.judge_env())
        self.assertEqual(sorted(judge.secrets()), sorted([self.OAUTH, self.KEY]))
        self.assertNotIn("ANTHROPIC_API_KEY", judge.env)  # only one is handed over

    def test_the_login_check_also_gets_the_sandbox_tmpdir(self):
        self.add_scenario("alpha", (JUDGE,))
        self.go("--runs", "1")
        call = self.launchers[0].judge_calls[0]
        self.assertEqual(call["env"]["CLAUDE_CODE_TMPDIR"], call["sandbox"].tmpdir)

    def test_not_logged_in_stops_before_any_run(self):
        def factory(name, **kwargs):
            launcher = self.launcher_factory(name, **kwargs)
            launcher.judge_login = '{"loggedIn": false, "email": "someone@example.invalid"}'
            return launcher

        self.add_scenario("alpha", (JUDGE,))
        code = self.go(launcher_factory=factory)
        self.assert_exit2(code, "judge is not logged in")
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", self.err)
        self.assertNotIn("someone@example.invalid", self.err + self.out)

    def test_garbage_login_output_also_stops(self):
        for text in ("", "not json", "[]", '{"loggedIn": "yes"}'):
            with self.subTest(text=text):
                def factory(name, text=text, **kwargs):
                    launcher = self.launcher_factory(name, **kwargs)
                    launcher.judge_login = text
                    return launcher

                self.add_scenario("alpha", (JUDGE,))
                self.assert_exit2(self.go(launcher_factory=factory), "judge is not logged in")

    def test_the_login_check_runs_in_the_judge_sandbox_with_the_judge_env(self):
        self.add_scenario("alpha", (JUDGE,))
        self.assertEqual(self.go("--runs", "1"), 0, self.err)
        call = self.launchers[0].judge_calls[0]
        self.assertEqual(call["argv"], [FAKE_CLAUDE, "auth", "status", "--json"])
        expected = {**self.launchers[0].judge_env(), "CLAUDE_CODE_TMPDIR": call["sandbox"].tmpdir}
        self.assertEqual(call["env"], expected)
        self.assertTrue(call["sandbox"].network)
        self.assertFalse(call["sandbox"].system_exec)

    def test_no_judge_login_check_without_judgement_rules(self):
        self.go("--runs", "1")
        self.assertEqual(self.launchers[0].judge_calls, [])

    def test_a_missing_sandbox_in_the_login_check_is_exit_2(self):
        def factory(name, **kwargs):
            launcher = self.launcher_factory(name, **kwargs)

            def run(*a, **k):
                raise launchers.SandboxUnavailable("no sandbox-exec")

            launcher.run = run
            return launcher

        self.add_scenario("alpha", (JUDGE,))
        self.assert_exit2(self.go(launcher_factory=factory), "sandbox-exec")

    def test_the_credential_is_redacted_from_excerpt_and_reason(self):
        seen = {}

        class Judge:
            def secrets(self):
                return ["cred-" + "value-1234"]

            def ask(self, rubric, excerpt):
                seen["excerpt"] = excerpt
                return True, "it echoed cred-" + "value-1234 back", None

        rule = rules.get(JUDGE)
        with mock.patch.dict(
            rules.EXCERPTS, {JUDGE: lambda t: "the agent said cred-" + "value-1234 here"}
        ):
            t = mock.Mock()
            t.meta = {}
            result = rules.run_judgement(rule, t, Judge())
        self.assertNotIn("value-1234", seen["excerpt"])
        self.assertNotIn("value-1234", result.reason)

    def test_a_real_judge_reports_its_credentials_for_redaction(self):
        judge = rules.Judge(
            None, {"CLAUDE_CODE_OAUTH_TOKEN": self.OAUTH, "PATH": "/x"}, "m", 1.0,
            sandbox_factory=lambda root: None,
        )
        self.assertEqual(list(judge.secrets()), [self.OAUTH])


class CredentialEnvTest(unittest.TestCase):
    ENV = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/h",
        "LANG": "en_US.UTF-8",
        "TERM": "xterm",
        "TMPDIR": "/t",
        "XDG_CONFIG_HOME": "/x",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-" + "placeholder",
        "ANTHROPIC_API_KEY": "api-" + "placeholder",
        "AWS_SECRET_ACCESS_KEY": "aws-" + "placeholder",
        "NPM_TOKEN": "npm-" + "placeholder",
        "GH_TOKEN": "gh-" + "placeholder",
        "SSH_AUTH_SOCK": "/s",
    }

    def assert_no_credentials(self, env):
        """Only what git, node and `claude --version` need: an allowlist, not a denylist."""
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        for name in (
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_API_KEY",
            "AWS_SECRET_ACCESS_KEY",
            "NPM_TOKEN",
            "GH_TOKEN",
            "SSH_AUTH_SOCK",
        ):
            self.assertNotIn(name, env)

    def test_allowlisted_env(self):
        env = launchers.allowlisted_env(self.ENV)
        self.assert_no_credentials(env)
        self.assertEqual(env["HOME"], "/h")
        self.assertEqual(
            set(env), {"PATH", "HOME", "LANG", "TERM", "TMPDIR", "XDG_CONFIG_HOME"}
        )

    def test_git_env_drops_them(self):
        with mock.patch.dict(os.environ, self.ENV, clear=True):
            self.assert_no_credentials(harness._git_env())

    def test_the_default_runner_drops_them(self):
        seen = {}

        def fake(argv, **kwargs):
            seen.update(kwargs)
            return mock.Mock(returncode=0, stdout="")

        with mock.patch.dict(os.environ, self.ENV, clear=True), mock.patch.object(
            run.subprocess, "run", fake
        ):
            run.default_runner(["git", "status"])
        self.assert_no_credentials(seen["env"])

    def test_the_source_secret_scan_drops_them(self):
        seen = {}

        def fake(argv, **kwargs):
            seen.update(kwargs)
            return mock.Mock(returncode=0, stderr="")

        with mock.patch.dict(os.environ, self.ENV, clear=True), mock.patch.object(
            harness.shutil, "which", return_value="/x/node"
        ), mock.patch.object(harness.subprocess, "run", fake):
            harness._scan_secrets(harness.REPO_ROOT, ["a.txt"])
        self.assert_no_credentials(seen["env"])

    def test_the_exec_path_probes_drop_them(self):
        seen = []

        def fake(argv, **kwargs):
            seen.append(kwargs.get("env"))
            return mock.Mock(returncode=0, stdout="")

        with mock.patch.dict(os.environ, self.ENV, clear=True), mock.patch.object(
            harness.subprocess, "run", fake
        ):
            harness._git_exec_path("/usr/bin/git")
            harness._git_binaries("/usr/bin/git")
        self.assertTrue(seen)
        for env in seen:
            self.assert_no_credentials(env)


class SandboxedCallsDoNotOverlapTest(unittest.TestCase):
    """No other sandboxed command runs while a judge call (with its token) does."""

    def test_calls_through_one_launcher_are_serialized(self):
        launcher = launchers.LocalLauncher("m", 1.0, parent_env={"PATH": "/x"}, claude_path="/c")
        spec = launchers.SandboxSpec(home="/h", home_override="/o/h", tmpdir="/o/t")
        active, peak, lock = [0], [0], threading.Lock()

        class Done:
            pid = 1
            returncode = 0

            def communicate(self, input=None, timeout=None):
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.05)
                with lock:
                    active[0] -= 1
                return "", ""

        def go():
            launcher.run(["/c"], "/", {"PATH": "/x"}, 5, sandbox=spec)

        patches = (
            mock.patch.object(launchers.os.path, "exists", return_value=True),
            mock.patch.object(launchers.subprocess, "Popen", lambda *a, **k: Done()),
            mock.patch.object(launchers, "kill_call"),
            mock.patch.object(launchers, "sandbox_profile", return_value="(version 1)"),
            mock.patch.object(launchers, "_path", lambda p: p),
            mock.patch.object(launchers.LocalLauncher, "_kill_group"),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        threads = [threading.Thread(target=go) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(peak[0], 1)


class JudgePassTest(Fixture):
    """Judgement rules are scored once, serially, after every agent has ended."""

    def setUp(self):
        super().setUp()
        self.add_scenario("alpha", (AUTO, JUDGE))
        self.add_scenario("beta", (AUTO, JUDGE), extra="runs = 3\n")
        self.delay = 0.05

    def test_no_judge_call_starts_while_an_agent_is_live(self):
        code = self.go("--runs", "3", "--jobs", "4")
        self.assertEqual(code, 0, self.err)
        kinds = [e[0] for e in self.events]
        last_stop = max(i for i, k in enumerate(kinds) if k == "stop")
        first_judge = min(i for i, k in enumerate(kinds) if k == "judge")
        self.assertGreater(first_judge, last_stop)
        self.assertEqual(kinds.count("start"), 6)
        self.assertEqual(kinds.count("judge"), 6)

    def test_judge_calls_do_not_overlap_each_other(self):
        self.go("--runs", "3", "--jobs", "4")
        self.assertEqual(self.max_judge_active, 1)

    def test_stdout_has_one_progress_line_per_run_and_judging_goes_to_stderr(self):
        self.go("--runs", "2")
        progress = [l for l in self.out.splitlines() if re.match(r"^\w+ run \d+: ", l)]
        self.assertEqual(len(progress), 4, self.out)  # alpha x2, beta x2: no extra "judged" line
        self.assertNotIn("judged", self.out)
        self.assertIn("judging alpha run 1", self.err)

    def test_results_and_report_hold_the_judgement_results(self):
        self.outcomes[("alpha", 1)] = "fail"
        self.go("--runs", "2")
        rep = self.report()
        rules_of = {e["rule"]: e for e in rep["scenarios"][0]["rules"]}
        self.assertEqual(rules_of[JUDGE]["kind"], "judgement")
        self.assertEqual(rules_of[JUDGE]["scored"], 2)
        self.assertEqual((rules_of[JUDGE]["passed"], rules_of[JUDGE]["failed"]), (1, 1))

    def test_each_scored_run_is_judged_from_its_own_output_directory(self):
        self.outcomes[("alpha", 2)] = "unscored:timeout"
        self.go("--runs", "2", "--scenario", "alpha")
        self.assertEqual([j["run"] for j in self.judged], [1])  # the unscored run is not judged
        self.assertEqual(self.judged[0]["out_dir"], self.run_dir() / "alpha" / "1")

    def test_no_judge_is_built_without_judgement_rules(self):
        self.add_scenario("alpha")
        self.add_scenario("beta", extra="runs = 3\n")
        built = []
        self.go(
            "--runs", "1",
            judge_factory=lambda launcher, model, budget: built.append(1),
        )
        self.assertEqual(built, [])
        self.assertEqual(self.judged, [])

    def test_a_missing_sandbox_in_the_pass_is_exit_2(self):
        self.judge_error = launchers.SandboxUnavailable("no sandbox-exec")
        self.assertEqual(self.go("--runs", "1"), 2)
        self.assertIn("sandbox-exec", self.err)
        self.assertFalse(list((self.evals / "runs").glob("*/report.*")))

    def test_an_unexpected_error_in_the_pass_names_only_its_type(self):
        self.judge_error = KeyError("a quote of agent text")
        self.assertEqual(self.go("--runs", "1"), 2)
        self.assertIn("KeyError", self.err)
        self.assertNotIn("agent text", self.err)

    def test_the_default_pass_is_turns_score_judgement(self):
        self.assertIs(run.Deps().judge_pass, turns.score_judgement)


class LockWaitTest(unittest.TestCase):
    def launcher(self):
        return launchers.LocalLauncher("m", 1.0, parent_env={"PATH": "/x"}, claude_path="/c")

    def run_call(self, launcher, seconds):
        def held(*args, **kwargs):  # the call itself, once the lock is taken
            time.sleep(seconds)
            return "", "", False

        launcher._run = held  # an instance attribute: no global patching across threads
        launcher.run(["/c"], "/", {"PATH": "/x"}, 5, sandbox=None)

    def test_the_time_spent_waiting_for_the_lock_is_reported_per_thread(self):
        launcher = self.launcher()
        waits = {}

        def second():
            self.run_call(launcher, 0)
            waits["second"] = launcher.take_lock_wait()

        first = threading.Thread(target=lambda: self.run_call(launcher, 0.3))
        first.start()
        time.sleep(0.1)  # the first call now holds the lock
        other = threading.Thread(target=second)
        other.start()
        first.join()
        other.join()
        self.assertGreater(waits["second"], 0.1)
        self.assertEqual(launcher.take_lock_wait(), 0.0)  # this thread never waited
        self.assertEqual(launcher.take_lock_wait(), 0.0)  # and reading resets it


class AtomicResultTest(test_turns.TurnTestCase):
    def test_score_judgement_replaces_result_json_through_a_temporary_file(self):
        scored = test_turns.scenario([test_turns.turn("say", "one")], (JUDGE,))
        result = turns.execute_run(
            scored,
            1,
            self.fixture,
            self.make_launcher([test_turns.reply()]),
            self.tmp / "other-run",
            self.out,
            source_repo=self.source,
        )
        victim = self.tmp / "victim.txt"
        victim.write_text("keep")
        (self.out / "result.json.tmp").symlink_to(victim)  # a planted name is never followed
        replaced = []
        real = os.replace
        with mock.patch.object(
            harness.os, "replace", lambda a, b: (replaced.append((a, b)), real(a, b))[1]
        ), mock.patch.object(
            rules,
            "run_judgement",
            lambda rule, t, judge: rules.RuleResult(rule.id, "pass", "ok", kind="judgement"),
        ):
            turns.score_judgement(scored, result, self.out, "j")
        self.assertEqual(victim.read_text(), "keep")
        self.assertEqual(len(replaced), 1)
        self.assertFalse((self.out / "result.json").is_symlink())
        self.assertEqual(
            json.loads((self.out / "result.json").read_text())["rules"][-1]["rule"], JUDGE
        )


class LockWaitCapTest(test_turns.TurnTestCase):
    def test_the_deadline_moves_by_at_most_the_runs_timeout(self):
        class WaitsLong(test_turns.PlainLauncher):
            def run(self, argv, cwd, env, timeout, *, sandbox):
                self._lock_wait.seconds = 1000.0  # a lock wait far beyond the timeout
                return super().run(argv, cwd, env, timeout, sandbox=sandbox)

        shown = test_turns.SHOWN
        launcher = self.make_launcher(
            [test_turns.reply(shown), test_turns.reply("done")], cls=WaitsLong
        )
        seen = []
        real = turns.Run._await_result

        def spy(run_self, *a, **k):
            seen.append(run_self.deadline - time.monotonic())
            return real(run_self, *a, **k)

        with mock.patch.object(turns.Run, "_await_result", spy):
            self.play(
                [test_turns.turn("say", "start"), test_turns.turn("run_shown")],
                [test_turns.reply(shown), test_turns.reply("done")],
                launcher=launcher,
                timeout=20,
            )
        self.assertLess(max(seen), 41)  # at most timeout (20) + timeout (20), not 1000 more


class LockWaitDeadlineTest(test_turns.TurnTestCase):
    def test_waiting_for_the_launcher_lock_does_not_use_up_the_runs_deadline(self):
        class WaitsForTheLock(test_turns.PlainLauncher):
            def run(self, argv, cwd, env, timeout, *, sandbox):
                time.sleep(3)  # what `LocalLauncher.run` records while another call holds the lock
                self._lock_wait.seconds = getattr(self._lock_wait, "seconds", 0.0) + 3
                return super().run(argv, cwd, env, timeout, sandbox=sandbox)

        shown = test_turns.SHOWN
        launcher = self.make_launcher(
            [test_turns.reply(shown), test_turns.reply("done")], cls=WaitsForTheLock
        )
        result = self.play(
            [test_turns.turn("say", "start"), test_turns.turn("run_shown")],
            [test_turns.reply(shown), test_turns.reply("done")],
            launcher=launcher,
            timeout=2,
        )
        self.assertTrue(result["scored"], result)


class StdinCloseTest(unittest.TestCase):
    def test_the_stdin_pipe_is_closed_when_the_second_wait_also_times_out(self):
        launcher = launchers.LocalLauncher("m", 1.0, parent_env={"PATH": "/x"}, claude_path="/c")
        spec = launchers.SandboxSpec(home="/h", home_override="/o/h", tmpdir="/o/t")
        pipes = {name: mock.Mock() for name in ("stdin", "stdout", "stderr")}

        class Stuck:
            pid = 1
            returncode = 0
            stdin, stdout, stderr = pipes["stdin"], pipes["stdout"], pipes["stderr"]

            def communicate(self, input=None, timeout=None):
                raise subprocess.TimeoutExpired("c", timeout, output=b"", stderr=b"")

            def kill(self):
                pass

            def wait(self):
                pass

        with mock.patch.object(launchers.os.path, "exists", return_value=True), mock.patch.object(
            launchers.subprocess, "Popen", lambda *a, **k: Stuck()
        ), mock.patch.object(launchers, "kill_call"), mock.patch.object(
            launchers, "sandbox_profile", return_value="(version 1)"
        ), mock.patch.object(launchers, "_path", lambda p: p), mock.patch.object(
            launchers.LocalLauncher, "_kill_group"
        ):
            launcher.run(["/c"], "/", {"PATH": "/x"}, 1, sandbox=spec, stdin="text")
        pipes["stdin"].close.assert_called()


class RunStdinTest(unittest.TestCase):
    def go(self, **kwargs):
        launcher = launchers.LocalLauncher("m", 1.0, parent_env={"PATH": "/x"}, claude_path="/c")
        spec = launchers.SandboxSpec(home="/h", home_override="/o/h", tmpdir="/o/t")
        seen = {}

        class Done:
            pid = 1
            returncode = 0

            def communicate(self, input=None, timeout=None):
                seen["input"] = input
                return "out", ""

        def popen(argv, **popen_kwargs):
            seen["stdin"] = popen_kwargs["stdin"]
            seen["argv"] = argv
            return Done()

        with mock.patch.object(launchers.os.path, "exists", return_value=True), mock.patch.object(
            launchers.subprocess, "Popen", popen
        ), mock.patch.object(launchers, "kill_call"), mock.patch.object(
            launchers, "sandbox_profile", return_value="(version 1)"
        ), mock.patch.object(launchers, "_path", lambda p: p), mock.patch.object(
            launchers.LocalLauncher, "_kill_group"
        ):
            launcher.run(["/c", "-p"], "/", {"PATH": "/x"}, 5, sandbox=spec, **kwargs)
        return seen

    def test_the_default_is_no_input(self):
        seen = self.go()
        self.assertEqual(seen["stdin"], subprocess.DEVNULL)
        self.assertIsNone(seen["input"])

    def test_a_given_text_is_sent_on_stdin_and_never_put_in_argv(self):
        seen = self.go(stdin="a long prompt")
        self.assertEqual(seen["stdin"], subprocess.PIPE)
        self.assertEqual(seen["input"], "a long prompt")
        self.assertNotIn("a long prompt", seen["argv"])


class ReasonScanTest(unittest.TestCase):
    TOKEN = "gh" + "p_" + "Zq4Lm8" * 6

    class Judge:
        def __init__(self, reason):
            self.reason = reason

        def secrets(self):
            return []

        def ask(self, rubric, excerpt):
            return True, self.reason, None

    def judged(self, reason):
        rule = rules.get(JUDGE)
        with mock.patch.dict(rules.EXCERPTS, {JUDGE: lambda t: "an excerpt"}):
            t = mock.Mock()
            t.meta = {}
            return rules.run_judgement(rule, t, self.Judge(reason))

    def test_a_secret_shaped_reason_is_replaced(self):
        for reason in (
            f"it pasted {self.TOKEN} twice",
            "AK" + "IA" + "ABCDEFGHIJKLMNOP",
            "sk-" + "ant-" + "x" * 30,
            "-----BEGIN " + "RSA PRIVATE KEY-----",
        ):
            with self.subTest(reason=reason[:6]):
                result = self.judged(reason)
                self.assertEqual(result.outcome, "pass")
                self.assertNotIn(reason[:12], result.reason)
                self.assertIn("withheld", result.reason)

    def test_a_failed_call_is_flagged_as_such(self):
        class Failing:
            def secrets(self):
                return []

            def ask(self, rubric, excerpt):
                return None, None, "the judge call timed out"

        rule = rules.get(JUDGE)
        with mock.patch.dict(rules.EXCERPTS, {JUDGE: lambda t: "an excerpt"}):
            t = mock.Mock()
            t.meta = {}
            result = rules.run_judgement(rule, t, Failing())
        self.assertTrue(result.judge_failed)
        self.assertNotIn("judge_failed", result.as_dict())
        self.assertFalse(self.judged("fine").judge_failed)

    def test_a_clean_reason_is_kept(self):
        self.assertEqual(self.judged("No later step is named.").reason, "No later step is named.")

    def test_the_python_patterns_mirror_the_scanner_script(self):
        text = (Path(__file__).resolve().parents[2] / "scripts" / "check-secrets.mjs").read_text()
        listed = re.findall(r"\[\s*/((?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\\\n\[])+)/,\s*'([^']+)',?\s*\]", text)
        self.assertGreaterEqual(len(listed), 10)
        self.assertEqual([(s, label) for s, label in listed], list(secret_scan.PATTERNS))


class DefaultsTest(unittest.TestCase):
    def test_default_runner_runs_an_argument_list_without_a_shell(self):
        code, out = run.default_runner(["python3", "-c", "print('a;b')"])
        self.assertEqual((code, out.strip()), (0, "a;b"))

    def test_default_runner_reports_a_missing_program(self):
        code, _ = run.default_runner(["/no/such/program-xyz"])
        self.assertNotEqual(code, 0)

    def test_the_real_config_has_a_model(self):
        self.assertTrue(run.load_config()["model"].strip())

    def test_check_passes_on_the_repository_as_it_is(self):
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(run.main(["--check"], run.Deps(stdout=out, stderr=err)), 0, err.getvalue())


class JudgeSandboxTest(unittest.TestCase):
    def test_the_judge_sandbox_allows_the_network_and_no_writes_outside_its_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = os.path.realpath(temp)
            claude = Path(root) / "bin" / "claude"
            claude.parent.mkdir()
            claude.write_text("x")
            launcher = FakeLauncher("local")
            launcher.home = "/Users/maintainer"
            launcher.claude_path = str(claude)
            judge_root = Path(root) / "judge"
            judge_root.mkdir()
            spec = run.judge_sandbox(launcher, str(judge_root))
        self.assertTrue(spec.network)
        self.assertEqual(spec.home, "/Users/maintainer")
        self.assertEqual(spec.home_override, str(judge_root / "home"))
        self.assertEqual(spec.tmpdir, str(judge_root / "tmp"))
        self.assertEqual(spec.allow_write, ())
        self.assertIn(str(claude), spec.exec_paths)

    def test_the_profile_opens_the_network_only_when_asked(self):
        base = {
            "home": "/Users/maintainer",
            "home_override": "/opt/h",
            "tmpdir": "/opt/t",
        }
        closed = launchers.sandbox_profile(launchers.SandboxSpec(**base))
        opened = launchers.sandbox_profile(launchers.SandboxSpec(**base, network=True))
        self.assertNotIn("network", closed)
        self.assertNotIn("mach-lookup", closed)
        self.assertIn("network-outbound", opened)
        self.assertIn('(deny file-read* (subpath "/Users/maintainer"))', opened)

    def test_only_claude_may_be_executed_by_the_judge(self):
        base = {"home": "/Users/maintainer", "home_override": "/opt/h", "tmpdir": "/opt/t"}
        default = launchers.sandbox_profile(launchers.SandboxSpec(**base))
        self.assertIn('(literal "/bin/sh")', default)  # other sandboxes keep the shell
        narrow = launchers.sandbox_profile(
            launchers.SandboxSpec(**base, exec_paths=("/opt/claude",), system_exec=False)
        )
        for shell in ("/bin/sh", "/bin/bash", "/bin/dash", "/bin/zsh", "/usr/bin/env"):
            self.assertNotIn(f'(literal "{shell}")', narrow)
        self.assertIn('(allow process-exec* (subpath "/opt/claude"))', narrow)

    def test_the_judge_spec_turns_system_exec_off_and_reads_its_cwd(self):
        with tempfile.TemporaryDirectory() as temp:
            root = os.path.realpath(temp)
            launcher = FakeLauncher("local")
            launcher.home = "/Users/maintainer"
            launcher.claude_path = str(Path(root) / "claude")
            spec = run.judge_sandbox(launcher, root)
        self.assertFalse(spec.system_exec)
        # `Judge.ask` makes `<root>/cwd` and runs there: the profile must be able to read it
        self.assertIn(os.path.join(root, "cwd"), [*spec.allow_read, spec.tmpdir])

    def test_the_judge_profile_is_narrow(self):
        base = {"home": "/Users/maintainer", "home_override": "/opt/h", "tmpdir": "/opt/t"}
        opened = launchers.sandbox_profile(launchers.SandboxSpec(**base, network=True))
        self.assertNotIn("(allow network*)", opened)
        self.assertNotIn("(allow mach-lookup)", opened)
        self.assertNotIn("network-inbound", opened)
        self.assertNotIn("network-bind", opened)
        self.assertIn('(remote tcp "*:443")', opened)
        self.assertIn('(remote udp "*:53")', opened)
        self.assertIn('(global-name "com.apple.SecurityServer")', opened)
        self.assertNotRegex(opened, r"\(allow mach-lookup\s*\)")
        self.assertNotIn("global-name-prefix", opened)


class JudgeWiringTest(test_turns.TurnTestCase):
    """A run only plays and scores its automatic rules; `turns.score_judgement` adds the
    judgement rules afterwards, from the files in the output directory alone."""

    def play_with(self, rule_ids, judge):
        scored = test_turns.scenario([test_turns.turn("say", "one")], rule_ids)
        launcher = self.make_launcher([test_turns.reply()])
        result = turns.execute_run(
            scored,
            1,
            self.fixture,
            launcher,
            self.tmp / "other-run",
            self.out,
            source_repo=self.source,
        )
        self.assertFalse((self.tmp / "other-run").exists())  # the run root is gone before judging
        self.assertEqual(
            [r["rule"] for r in result["rules"]],
            [r for r in rule_ids if rules.get(r).kind == "automatic"],
        )
        turns.score_judgement(scored, result, self.out, judge)
        return result

    def test_judgement_rules_are_scored_through_the_judge_from_the_output_directory(self):
        asked = []

        def fake(rule, t, judge):
            asked.append((rule.id, judge, t))
            return rules.RuleResult(rule.id, "pass", "fine", kind="judgement")

        with mock.patch.object(rules, "run_judgement", fake):
            result = self.play_with(("shown-command-well-formed", JUDGE), "the-judge")
        self.assertEqual([(a[0], a[1]) for a in asked], [(JUDGE, "the-judge")])
        self.assertEqual(asked[0][2].denied_count, 0)  # a transcript read from `out`
        by_rule = {r["rule"]: r for r in result["rules"]}
        self.assertEqual(by_rule[JUDGE]["kind"], "judgement")
        self.assertEqual(by_rule[JUDGE]["outcome"], "pass")
        self.assertIn("shown-command-well-formed", by_rule)
        written = json.loads((self.out / "result.json").read_text())
        self.assertEqual(written["rules"], result["rules"])  # result.json carries them too

    def test_no_judge_call_without_judgement_rules(self):
        with mock.patch.object(rules, "run_judgement") as judged:
            result = self.play_with(("shown-command-well-formed",), "the-judge")
        judged.assert_not_called()
        self.assertEqual(len(result["rules"]), 1)

    def test_an_unscored_run_is_not_judged(self):
        scored = test_turns.scenario([test_turns.turn("say", "one")], (JUDGE,))
        result = {"scenario": "demo", "run": 1, "scored": False, "rules": [], "warnings": []}
        with mock.patch.object(rules, "run_judgement") as judged:
            turns.score_judgement(scored, result, self.out, "the-judge")
        judged.assert_not_called()
        self.assertEqual(result["rules"], [])

    def test_a_failing_judge_is_a_warning_not_a_pass(self):
        def fake(rule, t, judge):
            return rules.RuleResult(
                rule.id,
                "not_applicable",
                "no valid answer",
                kind="judgement",
                judge_failed=True,
            )

        with mock.patch.object(rules, "run_judgement", fake):
            result = self.play_with((JUDGE,), "j")
        self.assertEqual(result["rules"][0]["outcome"], "not_applicable")
        self.assertTrue(any("judge" in w for w in result["warnings"]), result["warnings"])
        self.assertNotIn("judge_failed", result["rules"][0])

    def test_a_reason_that_merely_starts_with_the_judge_is_not_a_warning(self):
        def fake(rule, t, judge):
            return rules.RuleResult(
                rule.id, "not_applicable", "the judge is mentioned here", kind="judgement"
            )

        with mock.patch.object(rules, "run_judgement", fake):
            result = self.play_with((JUDGE,), "j")
        self.assertEqual(result["warnings"], [])

    def test_a_missing_situation_is_not_a_warning(self):
        def fake(rule, t, judge):
            return rules.RuleResult(
                rule.id, "not_applicable", "the transcript has no situation", kind="judgement"
            )

        with mock.patch.object(rules, "run_judgement", fake):
            result = self.play_with((JUDGE,), "j")
        self.assertEqual(result["warnings"], [])

    def test_a_missing_sandbox_stops_the_evaluation(self):
        def fake(rule, t, judge):
            raise launchers.SandboxUnavailable("no sandbox-exec")

        with mock.patch.object(rules, "run_judgement", fake):
            with self.assertRaises(launchers.SandboxUnavailable):
                self.play_with((JUDGE,), "j")


class JudgeCredentialPreflightTest(Fixture):
    """Research D16 decision 3: the judge credential and the sandbox are checked before any run.

    The launcher's `run` returns no exit code: a failed call shows as empty or unparseable output
    or an `is_error` result, so those are the cases tested. The tests also cover: a JSON document
    that is not a result (another `type`, or none), a result without `is_error`, and a timeout,
    all rejected; a result that hit the budget cap, accepted; a check that cannot run, which names
    only the error type; the secret-scanner probe running before the paid call; and the sandbox
    check running before any call, with or without a judge, so a missing sandbox-exec makes no
    paid call.
    """

    REJECTED = "the judge credential was rejected"
    SECRET = "oauth-" + "placeholder-" + "value"

    def setUp(self):
        super().setUp()
        self.sandbox_checks = 0
        self.sandbox_error = None

    def sandbox_check(self):
        self.sandbox_checks += 1
        if self.sandbox_error is not None:
            raise self.sandbox_error

    def go(self, *args, **overrides):
        overrides.setdefault("sandbox_check", self.sandbox_check)
        return super().go(*args, **overrides)

    def factory_with(self, reply):
        """A launcher factory whose credential call (not the login check) gets `reply`."""

        def factory(name, **kwargs):
            launcher = self.launcher_factory(name, **kwargs)
            plain = launcher.run

            def run(argv, cwd, env, timeout, *, sandbox, stdin=None):
                result = plain(argv, cwd, env, timeout, sandbox=sandbox, stdin=stdin)
                if argv[1:3] == ["auth", "status"]:
                    return result
                if isinstance(reply, BaseException):
                    raise reply
                return reply

            launcher.run = run
            return launcher

        return factory

    def test_one_minimal_judge_call_follows_the_login_check(self):
        self.add_scenario("alpha", (JUDGE,))
        self.assertEqual(self.go("--runs", "1"), 0, self.err)
        calls = self.launchers[0].judge_calls
        self.assertEqual(calls[0]["argv"], [FAKE_CLAUDE, "auth", "status", "--json"])
        probe = calls[1]
        self.assertEqual(probe["argv"][0], FAKE_CLAUDE)
        self.assertIn("-p", probe["argv"])
        self.assertEqual(probe["argv"][probe["argv"].index("--model") + 1], "m-pinned")
        self.assertEqual(probe["argv"][probe["argv"].index("--max-budget-usd") + 1], "0.05")
        self.assertEqual(probe["stdin"], run.CREDENTIAL_CHECK_PROMPT)
        self.assertTrue(probe["sandbox"].network)
        self.assertEqual(probe["env"]["CLAUDE_CODE_TMPDIR"], probe["sandbox"].tmpdir)
        others = [c for c in calls if c["argv"][1:3] != ["auth", "status"]]
        self.assertEqual(len(others), 1)  # the judge itself is faked in these tests

    def test_the_call_has_the_flags_of_a_real_judge_and_a_60_second_timeout(self):
        timeouts = []

        def factory(name, **kwargs):
            launcher = self.launcher_factory(name, **kwargs)
            plain = launcher.run

            def run(argv, cwd, env, timeout, *, sandbox, stdin=None):
                timeouts.append(timeout)
                return plain(argv, cwd, env, timeout, sandbox=sandbox, stdin=stdin)

            launcher.run = run
            return launcher

        self.add_scenario("alpha", (JUDGE,))
        self.assertEqual(self.go("--runs", "1", launcher_factory=factory), 0, self.err)
        real = run.default_judge_factory(self.launchers[0], "m-pinned", 0.05)({}).argv()
        self.assertEqual(self.launchers[0].judge_calls[1]["argv"], real)
        self.assertIn("--json-schema", real)
        self.assertIn('"pass": true', self.launchers[0].judge_calls[1]["stdin"])
        self.assertEqual(timeouts[:2], [60, 60])

    def test_a_rejected_credential_stops_before_any_session(self):
        error = '{"type": "result", "subtype": "error_during_execution", "is_error": true}'
        replies = {
            "error result": (error, "", False),
            "unparseable": ("not json", "", False),
            "empty": ("", "", False),
            "not an object": ("[]", "", False),
            "not a result": ('{"is_error": false}', "", False),
            "another type": ('{"type": "system", "is_error": false}', "", False),
            "no is_error": ('{"type": "result"}', "", False),
            "timeout": ("", "", True),
        }
        for label, reply in replies.items():
            with self.subTest(label):
                self.add_scenario("alpha", (JUDGE,))
                code = self.go(launcher_factory=self.factory_with(reply))
                self.assert_exit2(code, self.REJECTED)
                self.assertEqual(self.err.strip(), f"error: {self.REJECTED}")

    def test_a_check_that_cannot_run_names_only_the_error_type(self):
        self.add_scenario("alpha", (JUDGE,))
        code = self.go(launcher_factory=self.factory_with(RuntimeError("boom " + self.SECRET)))
        self.assert_exit2(code, "the judge credential check could not run (RuntimeError)")
        self.assertNotIn(self.REJECTED, self.err)
        self.assertNotIn(self.SECRET, self.err + self.out)

    def test_no_token_or_output_is_printed(self):
        doc = '{"type": "result", "is_error": true, "result": "' + self.SECRET + '"}'
        self.add_scenario("alpha", (JUDGE,))
        self.go(launcher_factory=self.factory_with((doc, self.SECRET, False)))
        self.assertNotIn(self.SECRET, self.err + self.out)

    def test_a_valid_result_passes(self):
        reply = ('{"type": "result", "subtype": "success", "is_error": false}', "", False)
        self.add_scenario("alpha", (JUDGE,))
        code = self.go("--runs", "1", launcher_factory=self.factory_with(reply))
        self.assertEqual(code, 0, self.err)

    def test_a_result_that_hit_the_budget_cap_proves_the_credential(self):
        doc = '{"type": "result", "subtype": "error_max_budget_usd", "is_error": true}'
        self.add_scenario("alpha", (JUDGE,))
        code = self.go("--runs", "1", launcher_factory=self.factory_with((doc, "", False)))
        self.assertEqual(code, 0, self.err)

    def test_a_failing_secret_probe_makes_no_credential_call(self):
        self.probe_error = secret_scan.SecretScanError("the secret scanner failed (exit 2)")
        self.add_scenario("alpha", (JUDGE,))
        self.assert_exit2(self.go(), "secret scanner")
        self.assertEqual(self.launchers[0].judge_calls, [])

    def test_no_call_without_a_judgement_rule(self):
        self.assertEqual(self.go("--runs", "1"), 0, self.err)
        self.assertEqual(self.launchers[0].judge_calls, [])

    def test_a_missing_sandbox_in_the_call_is_exit_2(self):
        error = launchers.SandboxUnavailable("no sandbox-exec")
        self.add_scenario("alpha", (JUDGE,))
        self.assert_exit2(self.go(launcher_factory=self.factory_with(error)), "sandbox-exec")

    def add_run_shown(self, rule_ids):
        self.add_scenario("alpha", rule_ids)
        path = self.evals / "scenarios" / "alpha.toml"
        text = path.read_text().replace(
            'turns = [{ say = "hello" }]', 'turns = [{ say = "hello" }, { run_shown = true }]'
        )
        path.write_text(text)

    def test_a_run_shown_turn_checks_the_sandbox_without_a_judge(self):
        self.add_run_shown((AUTO,))
        self.sandbox_error = launchers.SandboxUnavailable("sandboxed run needs sandbox-exec")
        self.assert_exit2(self.go("--scenario", "alpha"), "sandbox-exec")
        self.assertEqual(self.launchers[0].judge_calls, [])

    def test_a_judge_and_a_run_shown_turn_with_no_sandbox_make_no_paid_call(self):
        self.add_run_shown((JUDGE,))
        self.sandbox_error = launchers.SandboxUnavailable("sandboxed run needs sandbox-exec")
        self.assert_exit2(self.go("--scenario", "alpha"), "sandbox-exec")
        self.assertEqual(self.launchers[0].judge_calls, [])

    def test_a_run_shown_turn_with_an_available_sandbox_runs(self):
        self.add_run_shown((AUTO,))
        self.assertEqual(self.go("--scenario", "alpha", "--runs", "1"), 0, self.err)
        self.assertEqual(self.sandbox_checks, 1)

    def test_no_sandbox_check_without_a_run_shown_turn(self):
        self.assertEqual(self.go("--runs", "1"), 0, self.err)
        self.assertEqual(self.sandbox_checks, 0)


if __name__ == "__main__":
    unittest.main()

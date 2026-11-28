"""Plays a scenario's scripted turns against the agent and keeps the run's files (research D4).

`Run` drives one `claude -p` process (stream-json on stdin and stdout) started with
`LocalLauncher.start`: each scripted message is written after the previous turn's `result` event;
`run_shown` runs the command the agent last showed through `harness.run_shown`; a missed `expect`,
an agent error, the budget, the timeout, a stale fixture or a tampered run ends the run unscored.
A run goes `pending -> running -> scored | unscored`.

Output directory of a run (`out_dir`, outside the temporary repository):

    transcript.jsonl    every agent event as sent, and each message the harness sent as
                        `{"harness": true, "type": "user", "message": ...}` (with
                        `"user_step": true` for a user_step turn); a line
                        `{"type": "harness_note", ...}` says why a run ended early
    meta.json           facts for the rules and the secret scan: `fake_token` (only when a say turn
                        used `{fake_token}`; the one file that holds the whole value besides the
                        transcript), `run_shown` (`{"accepted": n, "rejected": n}`, only when a
                        `run_shown` turn ran) and `gh_call_offsets` (the call-log line count
                        when each harness message was sent; for a `run_shown` message, before
                        its command ran)
    gh-calls.jsonl      copy of the shim's call log
    agent-stderr.log    the agent's stderr
                        (the text files above are secret-scanned at the end of `_finish`: a hit
                        deletes every scanned file (`meta.json` stays) and ends the run unscored
                        `secret_found`; files
                        that cannot be scanned are all deleted: unscored `unscannable`)
    fixture-draft.toml  draft `[[call]]` entries, only when the fixture was stale
    result.json         data-model.md "Run", plus `warnings`

`RunSetupError` (a launcher without `sandbox-exec`, a repository that cannot be built) is not a
run result: the evaluation stops (exit 2) and no command ever runs unsandboxed.
"""

import json
import os
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

import gh_shim
import harness
import launchers
import rules
import secret_scan
import transcript

CONFIG = Path(__file__).with_name("config.toml")
SHOWN_TIMEOUT = 120
EXIT_WAIT = 5
UNSCORED = (
    "agent_error",
    "budget",
    "timeout",
    "fixture_stale",
    "off_script",
    "secret_found",
    "unscannable",
    "tampered",
)
# Built from parts at run time; never written whole to a source file (research D9).
_TOKEN_PARTS = ("gh", "p_", "Zq4Lm8", "Xv2Ra9", "Tb7Yc3", "Nd5We1", "Hs6Jf0", "Pk8Gu2")
_TRAILING = " \t\n*_`)\"'"


class RunSetupError(Exception):
    """The run cannot start or go on safely; the evaluation stops, no result is written."""


class _Unscored(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def load_config(path=CONFIG):
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def fake_token():
    return "".join(_TOKEN_PARTS)


def _blocks(text):
    lines, found = transcript.fences(text)
    return ["\n".join(lines[f.start : f.end]) for f in found]


def _first_line(block):
    return next((line.strip() for line in block.splitlines() if line.strip()), "")


def check_expect(scripted, reply):
    """Whether `reply` (the agent's text of the previous turn) meets the turn's `expect`."""
    expect = scripted.expect
    if expect is None:
        return True
    blocks = _blocks(reply)
    previews = [b for b in blocks if _first_line(b).startswith("Step ")]
    if expect == "preview":
        return bool(previews)
    if expect == "question":
        return reply.rstrip(_TRAILING).endswith("?")
    if expect == "one_command":
        return len(blocks) - len(previews) == 1
    if expect == "skill_call":
        return any(_first_line(b)[:1] in ("/", "$") for b in blocks)
    return re.search(scripted.expect_pattern, reply) is not None


def _user_message(text):
    content = [{"type": "text", "text": text}]
    return {"type": "user", "message": {"role": "user", "content": content}}


def _json_lines(path):
    """The parsed lines of a file the harness wrote in the output directory, whatever its size
    (`harness.read_regular` caps at `MAX_READ`, for the files of the run's repository). A missing
    file or a link gives no lines."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return []
    with os.fdopen(fd, "rb") as handle:
        text = handle.read().decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def normalize_run(out_dir):
    """The normalized `Transcript` of a finished run's files."""
    out = Path(out_dir)
    events, messages = [], []
    for line in _json_lines(out / "transcript.jsonl"):
        if line.get("harness") is True and line.get("type") == "user":
            messages.append(line)
        elif line.get("type") != "harness_note":
            events.append(line)
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    offsets = meta.get("gh_call_offsets", [])
    return transcript.normalize(
        events,
        messages,
        _json_lines(out / "gh-calls.jsonl"),
        offsets,
        meta={k: v for k, v in meta.items() if k != "gh_call_offsets"},
    )


class Run:
    """One run of `scenario` in the built repository `paths`; `play()` returns the result."""

    def __init__(
        self,
        scenario,
        number,
        paths,
        launcher,
        out_dir,
        timeout=None,
        secret_runner=None,
    ):
        self.secret_runner = secret_runner  # injected in tests; see secret_scan.scan_run
        self.scenario, self.number = scenario, number
        self.paths, self.launcher = paths, launcher
        self.out = Path(out_dir)
        self.timeout = timeout
        self.process = None
        self.call_id = None
        self.result = None
        self.warnings = []
        self._verified = b""  # the log bytes verified at the latest read
        self.state = "pending"

    # --- the lifecycle -----------------------------------------------------------------------
    def play(self):
        self.state = "running"
        self.out.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        timeout = self.timeout if self.timeout is not None else load_config()["timeout_seconds"]
        self.deadline = started + timeout
        self.meta, self.offsets, self.cost = {}, [], 0.0
        self.replies, self.queue = [], queue.Queue()
        reason = None
        self.log = open(self.out / "transcript.jsonl", "w", encoding="utf-8")
        try:
            try:
                try:
                    self._drive()
                except _Unscored as stop:
                    reason = stop.reason
                    self._write({"type": "harness_note", "note": f"run unscored: {reason}"})
            finally:
                self._stop_agent()
                self.log.close()
            reason = self._finish(reason)
        except BaseException:  # a setup failure or an unexpected error: no result is written
            self.state = "unscored"
            if secret_scan.delete_unscanned(self.out):  # never keep text that was not scanned
                print(
                    "warning: a file of the run could not be deleted and may hold unscanned "
                    f"text: {self.out}",
                    file=sys.stderr,
                )
            raise
        self.result = self._result(reason, time.monotonic() - started)
        (self.out / "result.json").write_text(
            json.dumps(self.result, indent=2) + "\n", encoding="utf-8"
        )
        self.state = "unscored" if reason else "scored"
        return self.result

    def _drive(self):
        scripted = self.scenario.turns
        token = fake_token()
        placeholder = "{" + harness.PLACEHOLDER + "}"
        if any(placeholder in (t.text or "") for t in scripted):
            self.meta["fake_token"] = token
        launcher, paths = self.launcher, self.paths
        self.env = launcher.build_env(paths.repo, paths.sidecar, paths.harness_env())
        # A marker every descendant inherits, so `_stop_agent` finds tool processes that left
        # the agent's process group.
        self.call_id = secrets.token_hex(8)
        self.env["EVAL_CALL_ID"] = self.call_id
        settings = paths.agent_settings(
            self.scenario.tools, launcher.home, extra_read=launcher.extra_read
        )
        argv = launcher.agent_argv(self.scenario.tools, paths.repo, settings=settings)
        self.process = launcher.start(paths.repo, paths.sidecar, argv, self.env)
        threading.Thread(target=self._pump, daemon=True).start()
        counts = {"accepted": 0, "rejected": 0}
        for index, step in enumerate(scripted):
            if index == 0 or step.expect:  # a later turn without `expect` has nothing to check
                self._expect(step, index)
            before = self._call_count()
            if step.kind == "run_shown":
                try:
                    try:
                        text = harness.run_shown(
                            self._last_text(),
                            launcher,
                            paths,
                            self.env,
                            timeout=max(1, min(SHOWN_TIMEOUT, self.deadline - time.monotonic())),
                        )
                    finally:
                        # Waiting for the launcher's run lock (another run's sandboxed call) is
                        # not this run's time: the deadline moves by that wait.
                        # At most the run's own timeout, so a stuck lock cannot stretch it forever.
                        self.deadline += min(launcher.take_lock_wait(), self.timeout)
                except harness.ShownCommandRejected:
                    counts["rejected"] += 1
                    self.meta["run_shown"] = counts
                    self._write({"type": "harness_note", "note": "shown command rejected"})
                    # Scored on purpose (research D5): the rule `shown-command-well-formed`
                    # fails the run; it is not an unscored reason.
                    return
                except harness.RunTampered:
                    raise _Unscored("tampered") from None
                except launchers.SandboxUnavailable as error:
                    raise RunSetupError(str(error)) from None
                counts["accepted"] += 1
                self.meta["run_shown"] = counts
            else:
                text = (step.text or "").replace(placeholder, token)
            self.offsets.append(before)
            self._send(text, user_step=step.user_step)
            self._await_result()
            # One agent process per run is assumed: `.claude/agents` may hold the engine's
            # generated files, which `check_integrity` compares with what `agent_setup`
            # generates, and a process that loaded them at its start (the review profile's Agent
            # tool) never sees a later change. Checked after every turn, so a change made
            # during a turn stops the run before the next message.
            if harness.check_integrity(paths):
                raise _Unscored("tampered")

    def _expect(self, step, index):
        if index == 0:
            # Fallback (scenario validation should catch it): nothing was said yet, so an
            # expectation, a shown command or an after-output reply has no reply to work on.
            if step.expect or step.kind != "say":
                raise _Unscored("off_script")
            return
        if not check_expect(step, self.replies[-1][0]):
            raise _Unscored("off_script")

    # --- talking to the agent ---------------------------------------------------------------
    def _pump(self):
        for line in self.process.stdout:
            self.queue.put(line)
        self.queue.put(None)

    def _write(self, entry):
        self.log.write(json.dumps(entry) + "\n")
        self.log.flush()

    def _send(self, text, user_step=False):
        message = _user_message(text)
        self._write({"harness": True, **({"user_step": True} if user_step else {}), **message})
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except OSError:
            raise _Unscored("agent_error") from None

    def _await_result(self):
        texts, last = [], ""
        while True:
            try:
                line = self.queue.get(timeout=max(0.0, self.deadline - time.monotonic()))
            except queue.Empty:
                raise _Unscored("timeout") from None
            if line is None:
                raise _Unscored("agent_error")
            try:
                event = json.loads(line)
            except ValueError:
                self._write({"type": "harness_note", "note": "unparseable agent output line"})
                continue
            self._write(event)
            if not isinstance(event, dict):
                continue
            if event.get("type") == "assistant" and event.get("parent_tool_use_id") is None:
                blocks = (event.get("message") or {}).get("content") or []
                found = [b.get("text", "") for b in blocks if b.get("type") == "text"]
                texts += found
                last = "\n".join(found) if found else last
            elif event.get("type") == "result":
                self.cost = max(self.cost, float(event.get("total_cost_usd") or 0))
                subtype = str(event.get("subtype") or "")
                if subtype == "error_max_budget_usd":
                    raise _Unscored("budget")
                if event.get("is_error") or subtype.startswith("error"):
                    raise _Unscored("agent_error")
                self.replies.append(("\n".join(texts), last))
                return

    def _last_text(self):
        return self.replies[-1][1] if self.replies else ""

    def _stop_agent(self):
        process = self.process
        if process is None:
            return
        try:
            process.stdin.close()
        except OSError:
            pass
        # The agent's tool children: its process group first, while the agent is not yet reaped
        # (a reaped pid, and so a group id, can be reused), then whatever carries the marker
        # (best effort, like `launchers.kill_call`; the sandbox is the boundary). Every turn has
        # its result by now, so the agent has nothing left to finish.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=EXIT_WAIT)
        except subprocess.TimeoutExpired:  # still running: stop it
            process.kill()
            process.wait()
        if self.call_id:
            launchers.kill_call(self.call_id, [], protect=[])

    # --- after the last turn ----------------------------------------------------------------
    def _call_count(self):
        try:
            return len(self._calls())
        except (OSError, ValueError):
            raise _Unscored("tampered") from None

    def _calls(self):
        """The verified call records (header, chain and inode, research D19).

        The chain cannot show calls cut off the end of the log, so every read must start with
        the bytes verified at the read before: a cut, or a rewrite of earlier lines, is
        tampering."""
        calls, data = gh_shim.read_verified(
            self.paths.gh_calls,
            expect_inode=self.paths.gh_calls_id,
            expect_header=self.paths.gh_calls_header,
        )
        if not data.startswith(self._verified):
            raise ValueError("calls log no longer starts with the lines verified before")
        self._verified = data
        return calls

    def _finish(self, reason):
        """Write the run's files; return the final unscored reason (None when scored)."""
        paths = self.paths
        if harness.check_integrity(paths):  # also what the agent changed without a shown command
            reason = "tampered"
        # The one read of the log at the end: the stale check, the draft and the copy all use
        # its records, so a later change of the file cannot reach them.
        try:
            calls = self._calls()
        except (OSError, ValueError):  # a missing, replaced, cut or forged log
            calls = []
            reason = "tampered"  # over timeout and budget too, like the integrity check
        else:
            stale = any(call["matched"] is None for call in calls)
            if stale and reason in (None, "off_script", "agent_error"):
                reason = "fixture_stale"
            if stale:  # whatever the reason
                draft = gh_shim.draft_entries(calls)
                (self.out / "fixture-draft.toml").write_text(draft, encoding="utf-8")
        (self.out / "gh-calls.jsonl").write_text(
            "".join(json.dumps(c) + "\n" for c in calls), encoding="utf-8"
        )
        stderr = harness.read_regular(paths.sidecar / "agent-stderr.log")
        if stderr is None:
            stderr = b""
            self.warnings.append(
                f"the agent's stderr was not kept: it was over {harness.MAX_READ} bytes, "
                "not a regular file or unreadable"
            )
        (self.out / "agent-stderr.log").write_bytes(stderr)
        meta = {**self.meta, "gh_call_offsets": self.offsets}
        (self.out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        # Before any rule reads the files (and before a judge sees them): a hit deletes
        # every scanned file (`meta.json` stays) and wins over any other reason (research D9).
        try:
            flagged = secret_scan.scan_run(self.out, runner=self.secret_runner)
        except secret_scan.Unscannable as error:  # this run only: the evaluation goes on
            self.warnings.append(f"the run's files could not be scanned and were deleted: {error}")
            if reason:
                self.warnings.append(f"the run was unscored {reason} before the secret scan")
            return "unscannable"
        except secret_scan.SecretScanError as error:  # the scanner cannot run: stop
            self.state = "unscored"
            raise RunSetupError(str(error)) from None
        if flagged:
            if reason:
                self.warnings.append(f"the run was unscored {reason} before the secret scan")
            return "secret_found"
        return reason

    def _result(self, reason, seconds):
        results, denied = [], 0
        if reason is None:
            t = normalize_run(self.out)
            denied = t.denied_count
            for rule_id in self.scenario.rules:
                rule = rules.get(rule_id)
                if rule.kind == "automatic":
                    results.append(rule.check(t).as_dict())
        return {
            "scenario": self.scenario.name,
            "run": self.number,
            "scored": reason is None,
            "unscored_reason": reason,
            "rules": results,
            "denied_commands": denied,
            "cost_usd": self.cost,
            "seconds": round(seconds, 3),
            "warnings": [*self.paths.warnings, *self.warnings],
        }


def execute_run(
    scenario,
    number,
    fixture_dir,
    launcher,
    run_root,
    out_dir,
    timeout=None,
    source_repo=None,
    toolkit_ref=None,
    secret_runner=None,
):
    """Build the run's repository and play the scenario; return the result dict.

    Only the automatic rules are scored here: judgement rules are scored later, one run at a
    time, by `score_judgement`, so that no judge call (which holds a credential in its
    environment) overlaps a live agent of another run."""
    try:
        paths = harness.build_run(
            run_root,
            fixture_dir,
            source_repo=source_repo,
            toolkit_ref=toolkit_ref,
            agents=scenario.agents,  # saved at build time (research D20)
            launcher=launcher,
        )
    except harness.RepoBuildError as error:
        raise RunSetupError(str(error)) from None
    result = None
    try:
        run = Run(
            scenario,
            number,
            paths,
            launcher,
            out_dir,
            timeout=timeout,
            secret_runner=secret_runner,
        )
        result = run.play()
        return result
    finally:  # only the scanned output directory survives (raw stderr, call log, agent repo)
        if not _remove_tree(paths.run_root):
            message = f"the run directory {paths.run_root} could not be removed"
            print(f"warning: {message}", file=sys.stderr)
            if result is not None:  # result.json is already written; the returned dict has it
                result["warnings"].append(message)


def score_judgement(scenario, result, out_dir, judge):
    """Add the scenario's judgement rules to the result of a scored run, and rewrite its
    `result.json`.

    The judge reads only what the run left in `out_dir` (its scanned transcript; the run's
    repository is gone). A `secret_found` or otherwise unscored run is not judged: it has no
    transcript to send. A judge that failed adds a `result.json` warning (fixed text, no
    excerpt); `launchers.SandboxUnavailable` propagates. Judge excerpts may hold the
    maintainer's paths: they go to the same model provider as the agent run itself.
    """
    wanted = [
        rules.get(rule_id) for rule_id in scenario.rules if rules.get(rule_id).kind == "judgement"
    ]
    if not result["scored"] or not wanted:
        return result
    t = normalize_run(out_dir)
    for rule in wanted:
        judged = rules.run_judgement(rule, t, judge)
        if judged.judge_failed:
            result["warnings"].append(f"the judge could not score {rule.id}: {judged.reason}")
        result["rules"].append(judged.as_dict())
    harness.write_atomic(Path(out_dir) / "result.json", json.dumps(result, indent=2) + "\n")
    return result


def _remove_tree(root):
    """Remove `root`, also when the agent left a directory without read or write permission;
    a failure here never masks the exception being raised. Returns whether `root` is gone.

    Links are never chmod'ed (a link must not lead the cleanup to a mode outside the root)."""
    try:
        os.chmod(root, 0o700)  # first: the agent may have made the root itself unreadable
        for current, dirs, _files in os.walk(root):
            for name in dirs:
                path = os.path.join(current, name)
                if os.path.islink(path):
                    continue
                try:
                    os.chmod(path, 0o700)
                except (OSError, NotImplementedError):
                    pass
        shutil.rmtree(root, ignore_errors=True)
    except (OSError, NotImplementedError):
        pass
    return not os.path.lexists(root)

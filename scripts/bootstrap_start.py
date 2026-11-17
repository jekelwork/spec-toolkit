"""The guided-bootstrap `start` command: the generic machinery (research D4, D12, D13).

This module holds the preview block and digest, the driver loop and `--confirm` handling. The
per-step work plugs in through `ACTIONS`: one object per confirmable step with these methods
(all optional; `StepAction` supplies the defaults):

- `missing_flags(env, step)`: init flags still missing (only `project_context` has any);
- `invalid_values(env, step)`: a message when a given value is invalid, checked read-only
  before any preview;
- `prepare(env, step)`: in a terminal, asks for values the preview must show;
- `preview(env, step, candidate)`: the step's own preview lines;
- `commit_files(env, step)`: the paths the step will commit, hashed into the digest;
- `perform(env, step, candidate)`: the confirmed action; raises `StepFailed` to stop.

Every `gh` and `git` call goes through the injected `run`, and no write command runs before a
confirmation: previews and digests only read.
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from urllib.parse import quote

import bootstrap_github
import bootstrap_steps
import project_board
import project_context

EXIT_OK, EXIT_STOPPED, EXIT_ERROR, EXIT_CONFIRM, EXIT_CHANGED, EXIT_INTERRUPTED = (
    0, 1, 2, 3, 4, 130)
DIGEST_LENGTH = 12
CONFIRM_VALUE = re.compile(r"(?P<step>[a-z][a-z_]*)@(?P<digest>[0-9a-f]{12})")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
NONE = "none"
NEW = "new"
ISSUE_STEP = "bootstrap_issue"


class StepFailed(Exception):
    """A confirmed action stopped; `code` is the exit code and `message` goes to stderr."""

    def __init__(self, message, code=EXIT_ERROR):
        super().__init__(message)
        self.code = code


class StepAction:
    """The defaults of a step action: nothing missing, nothing to hash, nothing to do yet."""

    def missing_flags(self, env, step):
        return []

    def invalid_values(self, env, step):
        return None

    def stop(self, env, step):
        """A reason to stop before any preview or digest, or None: `{"title", "text",
        "command"}` of the action the user must take first (a D12-style stop, exit 1)."""
        return None

    def explain(self, env, step):
        """Print what the engine's one-line action leaves out, just before a stop message (for
        `first_sync`, a failing dry run's output). Prints only; never changes anything."""

    def prepare(self, env, step):
        """In a terminal, ask for values the preview must show (before the preview)."""

    def preview(self, env, step, candidate=None):
        return []

    def commit_files(self, env, step):
        return []

    def perform(self, env, step, candidate=None):
        raise StepFailed(f"start cannot perform {step['id']} yet")


ACTIONS = {}  # step id -> StepAction; each step's action registers itself here
_DEFAULT_ACTION = StepAction()


def discard_pending_input():
    """Throw away what was typed ahead on a terminal, so an answer is always one given after the
    preview was shown. A no-op without a terminal or without `termios` (Windows); it never fails
    the run."""
    try:
        import termios
    except ImportError:
        return
    try:
        if sys.stdin.isatty():
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (OSError, ValueError, AttributeError, termios.error):
        pass


@dataclass
class Env:
    """What `start` reads and writes through: injected so tests never reach git or GitHub."""
    root: Path
    run: Callable
    remote_default: Callable
    # () -> [(owner, name) or None, ...], from `git remote get-url --push --all origin`;
    # None: the push URLs are not checked
    push_remote_default: Callable = None
    is_tty: bool = False
    prompt: Callable = input
    # () -> None: discards input typed before the prompt; called right before every prompt
    flush_input: Callable = discard_pending_input
    out: Callable = print
    err: Callable = lambda text="": print(text, file=sys.stderr)
    actions: dict = field(default_factory=lambda: ACTIONS)
    report: Callable = None  # () -> the `check --json` document, read fresh each call
    default_branch: str = None  # from the latest report, set by the driver
    bootstrap_merged: bool = None  # likewise: whether bootstrap is merged on the default branch
    init: Callable = None  # bootstrap.init(args, root, run, is_tty, prompt), injected by `main`
    validate_context: Callable = None  # bootstrap.validate(context); raises ValueError
    toml_string: Callable = None  # bootstrap.toml_string(text): how a string is written
    args: object = None  # the parsed options, set by `run_start`
    session: dict = field(default_factory=dict)  # values a step gathered, keyed by step id
    clock: Callable = time.time  # seconds since the epoch; injected so polling never waits
    sleep: Callable = time.sleep
    # (argv, cwd) -> object with returncode, stdout and stderr; used for the sync's dry run only,
    # which is the one command whose stderr `start` shows. None: go through `run`, stdout only.
    run_captured: Callable = None
    _repo: str = None

    def __post_init__(self):
        if self.report is None:
            self.report = lambda: bootstrap_steps.build_report(
                self.root, self.run, self.remote_default, self.push_remote_default)

    def ask(self, text):
        """The answer to a prompt, after input typed before it was discarded."""
        self.flush_input()
        return self.prompt(text)

    def action_for(self, step_id):
        return self.actions.get(step_id, _DEFAULT_ACTION)

    def need(self, name):
        """The injected function `name`, or a clear error when the caller did not supply it."""
        function = getattr(self, name)
        if function is None:
            raise ValueError(f"Env.{name} was not supplied; bootstrap.py passes it to start")
        return function

    def repo(self):
        if self._repo is None:
            self._repo = bootstrap_github.repository_name(self.run, self.root, self.remote_default)
        return self._repo


# --- preview block and digest (research D4) ---------------------------------------------------


def strip_ansi(text):
    return ANSI.sub("", text)


def preview_block(env, step, candidate=None):
    """The step's preview, without the `confirm:` line, prompt or hints (research D4).

    The one function behind every preview, so a terminal run, an exit-3 run and a `--confirm` run
    show, and hash, the same text.
    """
    lines = [f"Step {step['id']}: {_printable(step['title'])}"]
    if candidate is not None:
        branch = candidate.get("branch") or "no branch yet"
        lines.append(f"Bootstrap issue #{candidate['number']}: "
                     f"{_printable(candidate['title'])} ({_printable(branch)})")
    action = step.get("action") or {}
    if action.get("text"):
        lines.append(f"Action: {_printable(action['text'])}")
    if action.get("command"):
        lines.append(f"Command: {_printable(action['command'])}")
    lines.extend(env.action_for(step["id"]).preview(env, step, candidate))
    return strip_ansi("\n".join(lines))


def confirm_line(step_id, digest):
    return f"confirm: {step_id}@{digest}"


CONFIRM_LABELS = {
    "bootstrap_issue": "Create or use the bootstrap issue",
    "project_context": "Create project-context.toml",
    "github_settings": "Apply GitHub settings",
    "pull_request": "Commit, push and open the bootstrap pull request",
    "first_sync": "Run the first sync",
}


def confirm_label(step, candidate_number=None):
    """A fixed name for the step (or the chosen issue), shown above the `confirm:` line."""
    if candidate_number is not None:
        return f'Confirm "Use bootstrap issue #{candidate_number}":'
    return f'Confirm "{CONFIRM_LABELS[step["id"]]}":'


def _show_confirm(env, step, digest, candidate_number=None):
    env.out(confirm_label(step, candidate_number))
    env.out(confirm_line(step["id"], digest))


def _read(env, argv):
    return env.run(argv, cwd=env.root).strip()


class RefUnreadable(Exception):
    """A strict read of a ref could not be done; the message says what could not be read."""


def _tip(env, branch, strict=False):
    """The branch's tip on GitHub, `none` when it is not there or cannot be read.

    Strict: only an HTTP 404 means "not there" (as detection does); any other error raises
    `RefUnreadable`.
    """
    if not branch:
        return NONE
    try:
        return env.run(["gh", "api", f"repos/{env.repo()}/branches/{quote(branch, safe='/')}",
                        "--jq", ".commit.sha"]).strip() or NONE
    except bootstrap_github.CommandError as error:
        if strict and "HTTP 404" not in str(error):
            raise RefUnreadable(f"cannot read the tip of {branch} ({error})") from error
        return NONE


def read_refs(env, default_branch, step_id=None, candidate=None, strict=False):
    """HEAD, the default tip, the open PR's head and the branch tip, `none` where not applicable.

    The branch is the checked-out one when it is a bootstrap branch. For `bootstrap_issue` it is
    the chosen candidate's own branch instead (research D4), which may not be checked out; a
    candidate without a branch has none.
    Strict reads raise `RefUnreadable` instead of answering `none` for an error that is not a 404.
    """
    head = _read(env, ["git", "rev-parse", "HEAD"])
    default_tip = _tip(env, default_branch, strict)
    if step_id == ISSUE_STEP:
        branch = (candidate or {}).get("branch")
    else:
        current = _read(env, ["git", "branch", "--show-current"])
        branch = current if bootstrap_steps.BRANCH_PATTERN.match(current) else None
    pr_head = branch_tip = NONE
    if branch:
        try:
            listing = json.loads(env.run(
                ["gh", "pr", "list", "--repo", env.repo(), "--head", branch, "--state", "open",
                 "--json", "headRefOid"]))
            pr_head = listing[0]["headRefOid"] if listing else NONE
        except (bootstrap_github.CommandError, ValueError, KeyError, TypeError,
                IndexError) as error:
            if strict:
                raise RefUnreadable(
                    f"cannot read the open pull requests of {branch} ({error})") from error
            pr_head = NONE
        if pr_head == NONE:
            branch_tip = _tip(env, branch, strict)
    return [head, default_tip, pr_head, branch_tip]


def file_hashes(env, paths):
    """`git hash-object` of each path in path order, `deleted` for one that is gone."""
    hashes = []
    for path in sorted(paths):
        if os.path.lexists(Path(env.root) / path):  # a symlink counts even if its target is gone
            hashes.append(_read(env, ["git", "hash-object", path]))
        else:
            hashes.append("deleted")
    return hashes


def compute_digest(env, step, block, files, issue=None, candidate=None):
    """First 12 hex characters of SHA-256 over the inputs of research D4, one per line."""
    lines = [step["id"]]
    if step["id"] == ISSUE_STEP:
        lines.append(str(issue if issue is not None else NEW))
    lines.extend(block.split("\n"))
    lines.extend(file_hashes(env, files))
    lines.extend(read_refs(env, env.default_branch, step["id"], candidate))
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:DIGEST_LENGTH]


# --- the driver loop (contracts/start-cli.md, research D12) ------------------------------------

COMPLETE = "Bootstrap is complete."
COMPLETE_MANUAL = "Bootstrap is complete; manual settings remain (see above)."
CHANGED = ("the preview changed since you confirmed it; run `start` again to see the new one")


def _print_action(env, step):
    action = step.get("action") or {}
    if action.get("text"):
        env.out(f"  {_printable(action['text'])}")  # the engine may quote GitHub-sourced text
    if action.get("command"):
        env.out(f"  Command: {_printable(action['command'])}")
    if action.get("doc"):
        env.out(f"  See {action['doc']}")


def _show_step(env, step):
    env.out(f"{step['id']} ({step['state']}): {_printable(step['title'])}")
    _print_action(env, step)


def _stop(env, step):
    """A blocking step that `start` does not perform: print the action and its doc link."""
    env.action_for(step["id"]).explain(env, step)
    env.out(f"Stopped at {step['id']}: {_printable(step['title'])}")
    _print_action(env, step)
    return EXIT_STOPPED


def _blocked(env, step):
    """The exit code when the step's action asks to stop first (before any preview), else None."""
    reason = env.action_for(step["id"]).stop(env, step)
    if reason is None:
        return None
    env.out(f"Stopped at {step['id']}: {_printable(reason['title'])}")
    env.out(f"  {_printable(reason['text'])}")
    if reason.get("command"):
        env.out(f"  Command: {_printable(reason['command'])}")
    return EXIT_STOPPED


def _complete(env, report):
    manual = report.get("result") == "complete_with_manual_settings"
    env.out(COMPLETE_MANUAL if manual else COMPLETE)


def _preview(env, step, candidate, issue):
    """(block, digest): both from the same inputs, so they always agree."""
    action = env.action_for(step["id"])
    block = preview_block(env, step, candidate)
    return block, compute_digest(
        env, step, block, action.commit_files(env, step), issue, candidate)


def _perform(env, step, candidate):
    """Run the confirmed action; None when it finished, else the exit code."""
    try:
        env.action_for(step["id"]).perform(env, step, candidate)
    except StepFailed as error:
        env.err(str(error))
        return error.code
    except bootstrap_github.CommandError as error:
        env.err(f"{step['id']} failed: {error}")
        return EXIT_ERROR
    return None


def _current_step(env, step_id):
    """Checks (2) to (4) of the contract on a fresh report: (exit code or None, the step).

    Used for `--confirm`, and again after a terminal yes, so a step that changed while the
    prompt waited is never performed.
    """
    report = env.report()
    env.default_branch = report.get("default_branch")
    env.bootstrap_merged = report.get("bootstrap_merged")
    next_id = report.get("next_step")
    if next_id is None:  # (2)
        _complete(env, report)
        return EXIT_OK, None
    step = next((found for found in report["steps"] if found["id"] == next_id), None)
    if step is None:
        env.err(f"the report names an unknown next step: {next_id}")
        return EXIT_ERROR, None
    if step_id != next_id:  # (3)
        env.err(f"next step is {next_id}, not {step_id}")
        return EXIT_CHANGED, None
    if not step["needs_confirmation"]:
        env.err(f"next step {next_id} needs no confirmation")
        return EXIT_CHANGED, None
    if not step["performable"]:  # (4)
        return _stop(env, step), None
    return None, step


def _confirm_flags(step, digest, issue, named):
    """The `--confirm` (and `--issue`) arguments that confirm this preview.

    `--issue N` is part of it for a chosen issue number when `--issue` was given or several
    candidates exist, because the rerun needs it to pick the same candidate.
    """
    flags = f"--confirm {step['id']}@{digest}"
    if step["id"] == ISSUE_STEP and issue not in (None, NEW):
        candidates = (step.get("details") or {}).get("candidates") or []
        if named or len(candidates) > 1:
            flags += f" --issue {issue}"
    return flags


def _confirm_step(env, args, step, interactive, candidate=None, issue=None):
    """Preview, then ask (terminal) or exit 3; None when the step was performed."""
    block, digest = _preview(env, step, candidate, issue)
    env.out(block)
    if not interactive:
        flags = _confirm_flags(step, digest, issue, args.issue is not None)
        if step["id"] == PROJECT_CONTEXT:  # the confirmed run recomputes the digest from the flags
            env.out(f"To proceed, rerun with the same init flags and {flags} (the value below).")
        else:
            env.out(f"To proceed, rerun with {flags} (the value below).")
        _show_confirm(env, step, digest)
        return EXIT_CONFIRM
    _show_confirm(env, step, digest)
    try:
        answer = env.ask("Proceed? [y/N] ")
    except EOFError:
        answer = ""
    if answer != "y":
        env.out("Nothing was changed.")
        return EXIT_STOPPED
    code, step = _current_step(env, step["id"])  # the report may have changed meanwhile
    if code is not None:
        return code
    code = _blocked(env, step)  # the bootstrap may have merged while the prompt waited
    if code is not None:
        return code
    if step["id"] == ISSUE_STEP:  # the chosen candidate itself may have changed meanwhile
        fresh = (step.get("details") or {}).get("candidates") or []
        if issue == NEW:
            candidate = None
            same = not fresh
        else:
            candidate = next((found for found in fresh if str(found["number"]) == issue), None)
            same = candidate is not None
        if not same:
            env.err(CHANGED)
            return EXIT_CHANGED
    if _preview(env, step, candidate, issue)[1] != digest:
        env.err(CHANGED)
        return EXIT_CHANGED
    return _perform(env, step, candidate)


def _drive(env, args, acted=()):
    interactive = env.is_tty and not args.no_input
    shown, acted = set(), set(acted)
    while True:
        report = env.report()
        env.default_branch = report.get("default_branch")
        env.bootstrap_merged = report.get("bootstrap_merged")
        for step in report["steps"]:
            if step["state"] == "done":
                continue
            if not step["blocking"]:
                if step["id"] not in shown:
                    shown.add(step["id"])
                    _show_step(env, step)
                continue
            if step["id"] in acted:
                env.out(f"{step['id']} is still not done after its action.")
                return _stop(env, step)
            if not (step["needs_confirmation"] and step["performable"]):
                return _stop(env, step)
            code = _blocked(env, step)
            if code is not None:
                return code
            missing = env.action_for(step["id"]).missing_flags(env, step)
            if missing and not interactive:
                env.err(f"{step['id']} needs: {', '.join(missing)}")
                return EXIT_ERROR
            invalid = env.action_for(step["id"]).invalid_values(env, step)
            if invalid:
                env.err(f"{step['id']} failed: {invalid}")
                return EXIT_ERROR
            if interactive:
                try:
                    env.action_for(step["id"]).prepare(env, step)
                except StepFailed as error:
                    env.err(str(error))
                    return error.code
            candidate, issue, code = _choose_candidate(env, args, step, interactive)
            if code is not None:
                return code
            outcome = _confirm_step(env, args, step, interactive, candidate, issue)
            if outcome is not None:
                return outcome
            acted.add(step["id"])
            break
        else:
            _complete(env, report)
            return EXIT_OK


# --- candidates, `--issue` and `--confirm` (contracts/start-cli.md, research D3, D4) -----------


def _list_candidates(env, step, candidates):
    """Every candidate with its own preview and `confirm:` line; exit 3 (research D3)."""
    env.out(f"{len(candidates)} bootstrap issues found; choose one:")
    for candidate in candidates:
        number = str(candidate["number"])
        block, digest = _preview(env, step, candidate, number)
        env.out("")
        env.out(block)
        env.out(f"To use #{number}, rerun with --confirm {step['id']}@{digest} --issue {number}.")
        _show_confirm(env, step, digest, number)
    return EXIT_CONFIRM


def _choose_candidate(env, args, step, interactive):
    """(candidate, issue id, exit code): the issue `bootstrap_issue` acts on, or why not yet.

    No candidate means a new issue (`new`); one candidate is used unless `--issue` names another
    number; several need a valid `--issue`, or an answer at the prompt in a terminal. Other steps
    have no candidate, and a stray `--issue` is ignored.
    """
    if step["id"] != ISSUE_STEP:
        return None, None, None
    candidates = (step.get("details") or {}).get("candidates") or []
    if not candidates:
        if args.issue is None:
            return None, NEW, None
        # a number that is not a candidate is never ignored: that would create a duplicate issue
        env.out(f"#{args.issue} is not a bootstrap issue candidate; there is none. Nothing was "
                f"changed. Rerun without --issue to create a new bootstrap issue.")
        return None, None, EXIT_CONFIRM
    wanted = args.issue
    if wanted is None and len(candidates) == 1:
        wanted = candidates[0]["number"]
    chosen = next((found for found in candidates if found["number"] == wanted), None)
    if chosen is None and interactive:
        chosen = _ask_candidate(env, candidates)
        if chosen is None:
            return None, None, EXIT_STOPPED
    if chosen is None:
        return None, None, _list_candidates(env, step, candidates)
    return chosen, str(chosen["number"]), None


def _ask_candidate(env, candidates):
    for found in candidates:
        branch = found.get("branch") or "no branch yet"
        env.out(f"  #{found['number']}: {_printable(found['title'])} ({_printable(branch)})")
    try:
        answer = env.ask("Which bootstrap issue should be used? ").strip().lstrip("#")
    except EOFError:
        answer = ""
    chosen = next((found for found in candidates if str(found["number"]) == answer), None)
    if chosen is None:
        env.out("That is not one of the listed issues. Nothing was changed.")
    return chosen


def _confirmed(env, args, value):
    """Run the checks of the contract, in order, for `--confirm`: (exit code or None, step id).

    None means the confirmed step was performed and `start` continues. Nothing changes before
    the last check.
    """
    match = CONFIRM_VALUE.fullmatch(value)  # (1)
    if not match:
        env.err(f"--confirm takes <step-id>@<{DIGEST_LENGTH} hex characters>, copied from the "
                f"`confirm:` line of a preview (got {value!r})")
        return EXIT_ERROR, None
    step_id, given = match.group("step"), match.group("digest")
    code, step = _current_step(env, step_id)  # (2) to (4)
    if code is not None:
        return code, None
    code = _blocked(env, step)
    if code is not None:
        return code, None
    missing = env.action_for(step_id).missing_flags(env, step)  # (5)
    if missing:
        env.err(f"{step_id} needs: {', '.join(missing)}")
        return EXIT_ERROR, None
    invalid = env.action_for(step_id).invalid_values(env, step)
    if invalid:
        env.err(f"{step_id} failed: {invalid}")
        return EXIT_ERROR, None
    candidate, issue, code = _choose_candidate(env, args, step, False)  # (6)
    if code is not None:
        return code, None
    if _preview(env, step, candidate, issue)[1] != given:  # (7)
        env.err(CHANGED)
        return EXIT_CHANGED, None
    return _perform(env, step, candidate), step_id  # (8)


def run_start(env, args):
    """The `start` command: its exit code (contracts/start-cli.md)."""
    env.args = args
    acted = ()
    try:
        if args.confirm is not None:
            code, step_id = _confirmed(env, args, args.confirm)
            if code is not None:
                return code
            acted = (step_id,)
        return _drive(env, args, acted)
    except StepFailed as error:  # a preview or a stop check that could not run
        env.err(str(error))
        return error.code


# --- the bootstrap_issue action (research D3) --------------------------------------------------

ISSUE_TITLE = "CHORE: bootstrap the project"  # the title docs/bootstrap.md uses
ISSUE_BODY = (f"{bootstrap_steps.BOOTSTRAP_MARKER}\n\n"
              "Tracks the one-time bootstrap of this repository: project context, constitution, "
              "roadmap, GitHub settings and the bootstrap pull request.\n")
ISSUE_NUMBER = re.compile(r"/issues/(\d+)")


class BootstrapIssueAction(StepAction):
    """Create, develop or check out the bootstrap issue's branch (research D3)."""

    def _local_branches(self, env):
        return env.run(["git", "branch", "--format=%(refname:short)"], cwd=env.root).split()

    def _carried_over(self, env):
        status = env.run(["git", "status", "--porcelain", "-z", "--untracked-files=all"],
                         cwd=env.root)
        return bootstrap_steps.changed_paths(status)

    def stop(self, env, step):
        """`git fetch` and `git checkout` use `origin`, `gh issue create` and `gh issue develop`
        the repository `gh` uses: they must be the same one."""
        return remote_mismatch(env, bootstrap_steps.MISMATCH_ISSUE)

    def preview(self, env, step, candidate=None):
        base = env.default_branch
        if candidate is None:
            lines = [f'Will create the issue "{ISSUE_TITLE}" with the bootstrap marker, then '
                     f"develop its branch <number>-chore-bootstrap from {base} and check it out."]
        elif candidate.get("branch") is None:
            number = candidate["number"]
            lines = [f"Will develop #{number}'s branch {number}-chore-bootstrap from {base} "
                     "and check it out."]
        else:
            branch = candidate["branch"]
            quoted = shlex.quote(branch)
            fetch = "" if branch in self._local_branches(env) else (
                f"fetch it with `git fetch origin {quoted}` and ")
            lines = [f"Will {fetch}check out {_printable(branch)} with `git checkout {quoted}`."]
        lines.insert(0, repository_line(env))
        carried = self._carried_over(env)
        if carried:
            lines.append(f"Carried over to the branch: {_printable(', '.join(carried))}. If the "
                         "branch already commits the same files, the checkout can fail; start "
                         "never stashes.")
        return lines

    def perform(self, env, step, candidate=None):
        if candidate is None:
            candidate = {"number": self._create(env), "branch": None}
        number, branch = candidate["number"], candidate.get("branch")
        if branch is None:
            env.run(["gh", "issue", "develop", str(number), "--base", env.default_branch,
                     "--name", f"{number}-chore-bootstrap", "--checkout", "--repo", env.repo()],
                    cwd=env.root)
            env.out(f"Created the branch {number}-chore-bootstrap and checked it out.")
            return
        try:
            if branch not in self._local_branches(env):
                env.run(["git", "fetch", "origin", branch], cwd=env.root)
            env.run(["git", "checkout", branch], cwd=env.root)
        except bootstrap_github.CommandError as error:
            raise StepFailed(f"{ISSUE_STEP} failed: {error}", EXIT_ERROR) from error
        env.out(f"Checked out {branch}.")

    def _create(self, env):
        created = env.run(["gh", "issue", "create", "--title", ISSUE_TITLE, "--body", ISSUE_BODY,
                           "--repo", env.repo()], cwd=env.root)
        found = ISSUE_NUMBER.search(created)
        if not found:
            raise StepFailed(
                f"{ISSUE_STEP} failed: could not read the issue number from: "
                f"{created.strip() or 'no output'}; the "
                "issue was probably created, and rerunning start finds it by its marker")
        env.out(f"Created issue #{found.group(1)}.")
        return int(found.group(1))


ACTIONS[ISSUE_STEP] = BootstrapIssueAction()


# --- the project_context action (research D13, D14) --------------------------------------------

PROJECT_CONTEXT = "project_context"
INIT_FIELDS = ("name", "description", "owner", "project_number", "create_board",
               "required_approvals")


class ProjectContextAction(StepAction):
    """Write a missing `project-context.toml` with the existing `init` (never `--force`).

    Values come from the init flags; in a terminal `prepare` asks for the missing decisions
    first, so the preview shows every value that will be written (research D13).
    """

    def _values(self, env):
        gathered = env.session.get(PROJECT_CONTEXT)
        if gathered is not None:
            return dict(gathered)
        return {key: getattr(env.args, key, None) for key in INIT_FIELDS}

    def _resolved(self, env):
        """The values with the repository defaults for `--name` and `--owner` filled in."""
        values = self._values(env)
        if "remote" not in env.session:  # read once per run
            env.session["remote"] = env.remote_default()
        remote = env.session["remote"]
        if values["name"] is None:
            values["name"] = remote[1] if remote else env.root.name
        if values["owner"] is None and remote:
            values["owner"] = remote[0]
        return values

    def missing_flags(self, env, step):
        values = self._resolved(env)
        missing = []
        if values["description"] is None:
            missing.append("--description")
        if values["owner"] is None:
            missing.append("--owner")
        if values["project_number"] is None and not values["create_board"]:
            missing.append("--project-number or --create-board")
        if values["required_approvals"] is None:
            missing.append("--required-approvals")
        return missing

    def _error(self, env, values):
        """The shared validation's message for the values, or None (read-only, research D13).

        The context is built the way `init` builds it; a value still missing is replaced by
        a valid placeholder, so only a value that is present can fail.
        """
        def given(key, placeholder):
            return placeholder if values[key] is None else values[key]

        context = {
            "project": {"name": values["name"], "description": given("description", "x")},
            "github_project": {"owner": given("owner", "x"),
                               "number": given("project_number", 1)},
            "review": {"required_approvals": given("required_approvals", 0)},
        }
        validate = env.need("validate_context")  # outside the try: a missing one is not invalid
        try:
            validate(context)
        except ValueError as error:
            return str(error)
        return None

    def invalid_values(self, env, step):
        return self._error(env, self._resolved(env))

    def _ask(self, env, text, flag):
        try:
            return env.ask(text).strip()
        except EOFError:
            raise StepFailed(f"{PROJECT_CONTEXT} failed: input ended: pass {flag}",
                             EXIT_ERROR) from None

    def _ask_valid(self, env, values, key, text, flag, convert=str):
        """Ask until the answer converts and passes the shared validation."""
        while True:
            answer = self._ask(env, text, flag)
            try:
                value = convert(answer)
            except ValueError:
                env.out(f"{flag}: {answer!r} must be a whole number")
                continue
            problem = self._error(env, {**values, key: value})
            if problem is None:
                return value
            env.out(problem)

    def prepare(self, env, step):
        values = self._resolved(env)
        if values["description"] is None:
            values["description"] = self._ask_valid(
                env, values, "description", "Project description: ", "--description")
        if values["owner"] is None:
            values["owner"] = self._ask_valid(
                env, values, "owner", "GitHub owner: ", "--owner")
        if values["project_number"] is None and not values["create_board"]:
            number = self._ask_valid(
                env, values, "project_number", "GitHub Project number (empty to create one): ",
                "--project-number", convert=lambda answer: int(answer) if answer else None)
            values["project_number"], values["create_board"] = number, number is None
        if values["required_approvals"] is None:
            values["required_approvals"] = self._ask_valid(
                env, values, "required_approvals",
                "Required approvals (0 for a solo project) [0]: ", "--required-approvals",
                convert=lambda answer: int(answer) if answer else 0)
        env.session[PROJECT_CONTEXT] = values

    def preview(self, env, step, candidate=None):
        values = self._resolved(env)
        quote_text = env.need("toml_string")  # strings are shown exactly as they are written

        def shown(value):
            return "(missing)" if value is None else quote_text(value)

        board = (f"a new board named {shown(values['name'])} will be created"
                 if values["create_board"] else f"#{values['project_number']}")
        lines = [f"Will write {bootstrap_steps.CONTEXT_FILE} with:",
                 f"  name = {shown(values['name'])}",
                 f"  description = {shown(values['description'])}",
                 f"  owner = {shown(values['owner'])}",
                 f"  project board = {board}",
                 f"  required_approvals = {values['required_approvals']}"]
        warning = (step.get("details") or {}).get("ruleset_warning")
        if warning:
            lines.append(f"Ruleset warning: {warning}")
        return lines

    def perform(self, env, step, candidate=None):
        values = self._resolved(env)
        missing = self.missing_flags(env, step)
        if missing:  # init must never fill in a decision value, whatever the caller checked
            raise StepFailed(f"{PROJECT_CONTEXT} failed: needs {', '.join(missing)}", EXIT_ERROR)
        init = env.need("init")
        arguments = argparse.Namespace(**values, force=False, yes=True)
        written = io.StringIO()
        try:
            with contextlib.redirect_stdout(written):
                init(arguments, env.root, env.run, False, env.prompt)
        except (ValueError, OSError) as error:
            raise StepFailed(f"{PROJECT_CONTEXT} failed: {error}", EXIT_ERROR) from error
        # init's own next steps are start's job: echo only its "Wrote <path>" line
        wrote = next((line for line in written.getvalue().splitlines()
                      if line.startswith("Wrote ")), None)
        if wrote:
            env.out(wrote)


ACTIONS[PROJECT_CONTEXT] = ProjectContextAction()


# --- the github_settings action (research D14) -------------------------------------------------

GITHUB_SETTINGS = "github_settings"
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def _printable(text):
    """Control characters (a newline, an escape) shown as escapes, so text read from GitHub
    (board URL, label and ruleset values) can never start a line of the preview."""
    return CONTROL_CHARACTERS.sub(lambda found: found.group().encode("unicode_escape").decode(),
                                  text)


def _same_context(local, remote):
    """Whether both texts parse to the same context: line endings, comments and spacing do not
    matter, a text that does not parse never matches."""
    try:
        return project_context.parse_context(local) == project_context.parse_context(remote)
    except (ValueError, KeyError):  # tomllib's decode error is a ValueError
        return False


class GitHubSettingsAction(StepAction):
    """The preview is the `github` plan; the confirmed action is `github --apply` (D14).

    `--allow-weaken` is never passed: weakening a ruleset stays a decision made by hand.
    """

    def stop(self, env, step):
        """After the merge, the default branch's file is the authority (research D2): a local
        project-context.toml that differs, or is missing, must be brought up to date first."""
        if not env.bootstrap_merged:
            return None
        default = env.default_branch
        try:
            remote = bootstrap_steps.read_file(
                env.run, env.repo(), default, bootstrap_steps.CONTEXT_FILE, raw=True)
        except bootstrap_github.CommandError as error:
            raise StepFailed(f"{GITHUB_SETTINGS} failed: cannot read {bootstrap_steps.CONTEXT_FILE}"
                             f" on {default}: {error}", EXIT_ERROR) from error
        path = Path(env.root) / bootstrap_steps.CONTEXT_FILE
        local = path.read_bytes().decode("utf-8", "replace") if path.is_file() else None
        if local is not None and remote is not None and _same_context(local, remote):
            return None
        why = ("is missing" if local is None else "differs from the one on the default branch")
        return {"title": f"the local {bootstrap_steps.CONTEXT_FILE} {why}",
                "text": f"Update this checkout first, so {GITHUB_SETTINGS} uses the merged file.",
                "command": f"git checkout {shlex.quote(default)} && git pull --ff-only"}

    def _github(self, env, apply):
        """Run the `github` subcommand's own code; (exit code, its printed lines)."""
        lines = []
        arguments = argparse.Namespace(check=False, apply=apply, allow_weaken=False)
        code = bootstrap_github.run_github(
            arguments, env.root, env.run, env.remote_default,
            out=lambda text="": lines.append(_printable(str(text))), hint=False)
        return code, lines

    def preview(self, env, step, candidate=None):
        try:
            return self._github(env, apply=False)[1]
        except (ValueError, bootstrap_github.CommandError) as error:
            raise StepFailed(f"{GITHUB_SETTINGS} failed: {error}", EXIT_ERROR) from error

    def perform(self, env, step, candidate=None):
        try:
            _, lines = self._github(env, apply=True)
        except ValueError as error:  # the message names the failed area and, for a ruleset
            raise StepFailed(f"{GITHUB_SETTINGS} failed: {error}", EXIT_ERROR) from error
        for line in lines:
            if line.startswith("Applied "):
                env.out(line)


ACTIONS[GITHUB_SETTINGS] = GitHubSettingsAction()


# --- the pull_request action (research D7) -----------------------------------------------------

PULL_REQUEST = "pull_request"
# The files the bootstrap commits are `bootstrap_steps.BOOTSTRAP_FILES`, from the pilot:
# `speckit-constitution` writes only the constitution and `roadmap-write` only ROADMAP.md, so
# nothing else is added.
COMMIT_MESSAGE = ISSUE_TITLE
PUSH_COMMAND = ["git", "push", "-u", "origin", "HEAD"]


def _status_entries(status):
    """`git status --porcelain -z` as (XY, path, original path or None); a rename or copy is
    one entry followed by its source."""
    tokens = iter(status.split("\0"))
    entries = []
    for token in tokens:
        if len(token) < 4:
            continue
        original = next(tokens, "") if token[0] in "RC" or token[1] in "RC" else None
        entries.append((token[:2], token[3:], original or None))
    return entries


class PullRequestAction(StepAction):
    """Commit, push and open, but only the parts that are missing (research D7).

    Only `bootstrap_steps.BOOTSTRAP_FILES` are committed, by name: other changed files are
    listed, never added.
    """

    def _state(self, env):
        """`git status` and the checked-out branch; a failed read stops with exit 2."""
        try:
            status = env.run(["git", "status", "--porcelain", "-z", "--untracked-files=all"],
                             cwd=env.root)
            return status, _read(env, ["git", "branch", "--show-current"])
        except bootstrap_github.CommandError as error:
            raise StepFailed(f"{PULL_REQUEST} failed: cannot read the working tree state ({error})",
                             EXIT_ERROR) from error

    def stop(self, env, step):
        """Not on a bootstrap branch, or a bootstrap file is gone: committing would lose it."""
        status, branch = self._state(env)
        if not bootstrap_steps.BRANCH_PATTERN.match(branch):
            return {"title": "no bootstrap branch is checked out",
                    "text": "Check out the bootstrap issue's branch (the bootstrap_issue step), "
                            "for example git checkout <issue-number>-chore-bootstrap.",
                    "command": None}
        reason = remote_mismatch(env, bootstrap_steps.MISMATCH_PR, push=True)
        if reason:
            return reason
        entries = _status_entries(status)
        conflicted = sorted({path for xy, path, _ in entries if "U" in xy or xy in ("DD", "AA")})
        if conflicted:  # `git commit -- <paths>` cannot run during a merge
            many = len(conflicted) > 1
            return {"title": f"{_printable(', '.join(conflicted))} "
                             f"{'have merge conflicts' if many else 'has a merge conflict'}",
                    "text": "Resolve the merge conflict, then run start again.",
                    "command": "git status"}
        gone, commands = [], []
        for xy, path, original in entries:
            renamed = "R" in xy and original
            target = original if renamed else path
            not_ours = target not in bootstrap_steps.BOOTSTRAP_FILES
            if not_ours or os.path.lexists(Path(env.root) / target):
                continue
            gone.append(target)
            old, new = shlex.quote(_printable(target)), shlex.quote(_printable(path))
            if renamed:  # `git mv` also puts the index entry back
                commands.append(f"git mv -- {new} {old}")
            elif xy[0] == "D":  # gone from the index too, so a plain restore finds nothing
                commands.append(f"git restore --staged --worktree -- {old}")
            else:
                commands.append(f"git restore -- {old}")
        if not gone:
            return None
        names = _printable(", ".join(sorted(set(gone))))
        return {"title": f"{names} deleted; the removal would not be committed",
                "text": "Restore it first, then run start again.",
                "command": "; ".join(dict.fromkeys(commands))}

    def _plan(self, env):
        """What is missing: the files to commit, the other changed files, the parts to run."""
        status, branch = self._state(env)
        paths = bootstrap_steps.changed_paths(status)
        found = bootstrap_steps.BRANCH_PATTERN.match(branch)
        if not found:  # `stop` catches this first; never build a "Closes #None"
            raise StepFailed(f"{PULL_REQUEST} failed: no bootstrap branch is checked out",
                             EXIT_STOPPED)
        files = bootstrap_steps.BOOTSTRAP_FILES
        own = sorted({path for path in paths if path in files})
        others = sorted({path for path in paths if path not in files})
        try:
            head, default_tip, pr_head, branch_tip = read_refs(
                env, env.default_branch, strict=True)
        except RefUnreadable as error:
            raise StepFailed(f"{PULL_REQUEST} failed: {error}", EXIT_ERROR) from error
        if default_tip == NONE:
            raise StepFailed(f"{PULL_REQUEST} failed: cannot read the tip of "
                             f"{env.default_branch} (not found)", EXIT_ERROR)
        remote = pr_head if pr_head != NONE else branch_tip
        return {"own": own, "others": others, "branch": branch,
                "issue": found.group(1), "default_tip": default_tip,
                "commit": bool(own), "push": bool(own) or head != remote,
                "open": pr_head == NONE}

    def _contained(self, env, plan):
        """The files changed since the merge base with the default branch, plus the ones that
        will be committed."""
        try:
            listing = env.run(["git", "diff", "--name-only", "-z", f"{plan['default_tip']}...HEAD"],
                              cwd=env.root)
        except bootstrap_github.CommandError as error:
            raise StepFailed(f"{PULL_REQUEST} failed: cannot list the files ({error}); "
                             "run `git fetch origin` and start again", EXIT_ERROR) from error
        return sorted({path for path in listing.split("\0") if path} | set(plan["own"]))

    def commit_files(self, env, step):
        return self._plan(env)["own"]

    def preview(self, env, step, candidate=None):
        plan = self._plan(env)
        lines = [repository_line(env)]
        if plan["commit"]:
            lines.append(f"Will commit {_printable(', '.join(plan['own']))} with the message "
                         f'"{COMMIT_MESSAGE}".')
        if plan["push"]:
            lines.append(f"Will push the branch with `{' '.join(PUSH_COMMAND)}`.")
        if plan["open"]:
            lines.append(f"Will open a pull request that closes #{plan['issue']}.")
        if plan["others"]:
            lines.append("Other changed files, not committed: "
                         f"{_printable(', '.join(plan['others']))}")
        files = self._contained(env, plan)
        if files:
            lines.append(f"The pull request will contain: {_printable(', '.join(files))}")
        return lines

    def perform(self, env, step, candidate=None):
        plan = self._plan(env)
        if not (plan["commit"] or plan["push"] or plan["open"]):
            raise StepFailed(f"{PULL_REQUEST} failed: nothing to commit, push or open",
                             EXIT_STOPPED)
        done = []
        try:
            if plan["commit"]:
                env.run(["git", "add", "--", *plan["own"]], cwd=env.root)
                env.run(["git", "commit", "-m", COMMIT_MESSAGE, "--", *plan["own"]], cwd=env.root)
                done.append("committed")
            if plan["push"]:
                env.run(PUSH_COMMAND, cwd=env.root)
                done.append("pushed")
            if plan["open"]:
                created = env.run(
                    ["gh", "pr", "create", "--repo", env.repo(), "--head", plan["branch"],
                     "--base", env.default_branch, "--title", COMMIT_MESSAGE,
                     "--body", f"Closes #{plan['issue']}"], cwd=env.root)
                done.append("opened")
        except bootstrap_github.CommandError as error:
            earlier = (f" (already {' and '.join(done)}; rerunning start does the rest)"
                       if done else "")
            raise StepFailed(f"{PULL_REQUEST} failed: {error}{earlier}", EXIT_ERROR) from error
        env.out(f"Done: {', '.join(done)}.")
        if plan["open"]:
            env.out(_printable(created.strip()))


ACTIONS[PULL_REQUEST] = PullRequestAction()


# --- the first_sync action (research D5) -------------------------------------------------------

FIRST_SYNC = "first_sync"
SYNC_WORKFLOW = "sync-roadmap-issues.yml"
SYNC_COMMAND = ["python3", bootstrap_steps.SYNC_SCRIPT]  # a dry run: `--apply` is never passed
RUN_WAIT = 60  # seconds to wait for the dispatched run to appear
RUN_POLL = 2
RUN_LIMIT = "100"  # earlier dispatched runs to remember; far more than one user makes
ORIGIN_URL = bootstrap_steps.ORIGIN_URL  # the sync's own pattern, defined once in the steps


def remote_mismatch(env, mismatch, push=False):
    """A stop reason (`{"title", "text", "command"}`) when `origin`, read like the rest of
    `start` reads it (`env.remote_default`, which accepts SSH aliases and `ssh://` URLs), is not
    a GitHub repository or names another one than `gh` uses (`env.repo()`); None when they
    agree. `mismatch` is the title for the second case, with `{origin}` and `{target}`. A step
    that uses git against `origin` and `gh` on a repository checks it. With `push`, the push URL
    (`env.push_remote_default`; without one, the fetch URL again) must agree too, because
    `git push` goes there."""
    reads = bootstrap_steps.OriginReads(env.remote_default, env.push_remote_default)
    reason = reads.stop(env.repo(), mismatch, push=push)
    return reason and {**reason, "title": _printable(reason["title"])}


def repository_line(env):
    """The target repository as a preview line, so the digest binds it."""
    return f"Repository: {_printable(env.repo())}"


def origin_mismatch(env, step_id, mismatch):
    """The same check for the first sync, whose dry run reads `remote.origin.url` itself with
    the sync's narrower `ORIGIN_URL`: a stop reason when origin is not in a form that pattern
    reads, or names another repository than `gh` uses; None when they agree. `mismatch` is the
    title for the second case, with `{origin}` and `{target}`."""
    try:
        reason = bootstrap_steps.origin_url_stop(env.run, env.root, env.repo(), mismatch)
    except bootstrap_github.CommandError as error:
        raise StepFailed(f"{step_id} failed: cannot read the origin remote ({error})",
                         EXIT_ERROR) from error
    return reason and {**reason, "title": _printable(reason["title"])}


def _lines(text):
    """Split on newlines only (`splitlines` also splits on `\\r` and others, which `_printable`
    would then never see), each line escaped."""
    return [_printable(line) for line in text.rstrip("\n").split("\n")] if text else []


def run_captured(argv, cwd=None):
    """Run `argv` and keep what it printed on both streams, whatever its exit code."""
    try:
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    except OSError as error:
        raise bootstrap_github.CommandError(f"cannot run {argv[0]}: {error}") from error
    return SimpleNamespace(returncode=done.returncode, stdout=done.stdout, stderr=done.stderr)


def _runs_hint(repo):
    return (f"gh run list --repo {shlex.quote(repo)} --workflow {SYNC_WORKFLOW} "
            "--event workflow_dispatch")


def _follow_hint(repo, run_id):
    return f"gh run watch {run_id} --repo {shlex.quote(repo)} --exit-status"


class FirstSyncAction(StepAction):
    """The preview is the local dry run; the confirmed action dispatches the apply (D5)."""

    def stop(self, env, step):
        """HEAD must be the default tip and the paths the workflow reads (`SYNC_INPUT_PATHS`:
        the roadmap, the specs, the sync's code and the workflow) clean: the dispatched workflow
        reads what is on GitHub, so the preview must read the same."""
        default = env.default_branch
        try:
            tip = _tip(env, default, strict=True)
            head = _read(env, ["git", "rev-parse", "HEAD"])
            status = env.run(["git", "status", "--porcelain", "-z", "--untracked-files=all",
                              "--", *bootstrap_steps.SYNC_INPUT_PATHS], cwd=env.root)
        except RefUnreadable as error:
            raise StepFailed(f"{FIRST_SYNC} failed: {error}", EXIT_ERROR) from error
        except bootstrap_github.CommandError as error:
            raise StepFailed(f"{FIRST_SYNC} failed: cannot read the working tree state ({error})",
                             EXIT_ERROR) from error
        if tip == NONE:
            raise StepFailed(f"{FIRST_SYNC} failed: cannot read the tip of {default} (not found)",
                             EXIT_ERROR)
        reason = self._other_repository(env)
        if reason:
            return reason
        if head != tip:
            return {"title": f"this checkout is not at the tip of {_printable(default)}",
                    "text": "The sync reads exactly what is on GitHub: update this checkout.",
                    "command": f"git checkout {shlex.quote(default)} && git pull --ff-only"}
        dirty = sorted(set(bootstrap_steps.changed_paths(status)))
        if dirty:
            return {"title": f"{_printable(', '.join(dirty))} "
                             f"{'have' if len(dirty) > 1 else 'has'} uncommitted changes",
                    "text": "Commit or stash these paths first, then run start again.",
                    "command": "git status -- " + " ".join(bootstrap_steps.SYNC_INPUT_PATHS)}
        return None

    def _other_repository(self, env):
        """A stop reason when the dry run (which reads `remote.origin.url`, like the sync's
        `origin_repo`) and the dispatch (`env.repo()`) would use different repositories."""
        return origin_mismatch(env, FIRST_SYNC, bootstrap_steps.MISMATCH_SYNC)

    def _dry_run(self, env):
        """(exit code, stdout lines, stderr lines, argv, board note) of the sync's dry run,
        escaped. The note says why the board is not covered when its credential is unreadable."""
        credential = bootstrap_steps.detect_board_credential(env.repo(), env.run)
        skip = credential.state != "done"
        argv = SYNC_COMMAND + (["--skip-project"] if skip else [])
        note = (f"Board changes are not previewed: {_printable(credential.title)}"
                if credential.state == "not_checked" else None)
        try:
            if env.run_captured is None:
                try:
                    done = SimpleNamespace(returncode=0, stdout=env.run(argv, cwd=env.root),
                                           stderr="")
                except bootstrap_github.CommandError as error:
                    done = SimpleNamespace(returncode=error.returncode, stdout=error.stdout,
                                           stderr=str(error))
            else:
                done = env.run_captured(argv, cwd=env.root)
        except bootstrap_github.CommandError as error:  # the command could not be started
            done = SimpleNamespace(returncode=None, stdout="", stderr=str(error))
        return (done.returncode, _lines(done.stdout or ""), _lines(done.stderr or ""), argv, note)

    def explain(self, env, step):
        """The engine's action names a failed dry run in one line; show the output it refers
        to ("named above"), escaped. It prints only and never fails: whatever goes wrong here,
        the check's own message and the exit code stay. A repository mismatch is printed because
        the engine does not know of it; the other stops (off the tip, dirty) it already names,
        and then the dry run is not offered."""
        if step.get("state") != "todo":
            return
        try:
            other = self._other_repository(env)
            if other is not None:
                env.out(other["title"])
                env.out(f"  {other['text']}")
                env.out(f"  Command: {other['command']}")
                return
            if self.stop(env, step) is not None:
                return
            code, stdout, stderr, _, _ = self._dry_run(env)
        except (StepFailed, bootstrap_github.CommandError, ValueError):
            return
        if code == 0:
            return
        for line in stdout:
            env.out(line)
        for line in stderr:
            env.err(line)

    def preview(self, env, step, candidate=None):
        code, stdout, stderr, argv, note = self._dry_run(env)
        if code != 0:
            for line in stdout:
                env.out(line)
            advice = ("fix the roadmap entries named above"
                      if any(bootstrap_steps.BLOCKED_OUTPUT in line for line in stdout)
                      else "the preview failed" + (
                          f" (exit code {code})" if code is not None else ""))
            raise StepFailed("\n".join(
                [*stderr, f"{FIRST_SYNC}: {advice}; no apply is offered"]), EXIT_STOPPED)
        shown = env.session.setdefault(FIRST_SYNC, set())
        if tuple(stderr) not in shown:  # a terminal run previews twice; warn once
            shown.add(tuple(stderr))
            for line in stderr:  # the sync's own warnings: shown, never part of the digest
                env.err(line)
        return [repository_line(env), f"Dry run: `{' '.join(argv)}`", *([note] if note else []),
                *stdout,
                f"Will run `gh workflow run {SYNC_WORKFLOW} -f apply=true` and watch the run."]

    def _run_ids(self, env, repo, login):
        """The IDs of the sync workflow's dispatched runs by `login`; ValueError, KeyError or
        TypeError when the listing is not what `gh run list --json databaseId` prints."""
        runs = json.loads(env.run(
            ["gh", "run", "list", "--repo", repo, "--workflow", SYNC_WORKFLOW,
             "--event", "workflow_dispatch", "--user", login, "--limit", RUN_LIMIT,
             "--json", "databaseId"]))
        return {int(run["databaseId"]) for run in runs}

    def _find_run(self, env, repo, login, known):
        """The newest run that was not in `known` (the IDs before the dispatch): no clock
        comparison with GitHub's timestamps is involved. Waits up to `RUN_WAIT` seconds."""
        # Run IDs only grow, so a new run is above every known one; an older ID that was not in
        # the baseline (a run deleted meanwhile, one past the listing's limit) is not ours.
        newest = max(known, default=0)
        deadline = env.clock() + RUN_WAIT
        while True:
            new = {found for found in self._run_ids(env, repo, login) if found > newest}
            if new:
                return max(new)
            if env.clock() >= deadline:
                raise ValueError(f"the run did not appear within {RUN_WAIT} seconds")
            env.sleep(RUN_POLL)

    def perform(self, env, step, candidate=None):
        repo = env.repo()
        try:  # nothing is dispatched yet: a plain failure
            login = env.run(["gh", "api", "user", "--jq", ".login"]).strip()
            known = self._run_ids(env, repo, login)
        except (bootstrap_github.CommandError, ValueError, KeyError, TypeError) as error:
            raise StepFailed(f"{FIRST_SYNC} failed: {_printable(str(error))}",
                             EXIT_ERROR) from error
        try:
            env.run(["gh", "workflow", "run", SYNC_WORKFLOW, "--repo", repo, "-f", "apply=true"],
                    cwd=env.root)
        except bootstrap_github.CommandError as error:
            raise StepFailed(f"{FIRST_SYNC} failed: {_printable(str(error))}",
                             EXIT_ERROR) from error
        try:  # from here on the apply is running on GitHub
            run_id = self._find_run(env, repo, login, known)
        except KeyboardInterrupt:
            env.err("Interrupted: the apply was dispatched; do not rerun start until it "
                    f"finishes. Find the run with `{_runs_hint(repo)}`.")
            raise
        except (bootstrap_github.CommandError, ValueError, KeyError, TypeError) as error:
            raise StepFailed(
                f"{FIRST_SYNC} failed: the apply was dispatched; do not rerun start until it "
                f"finishes ({_printable(str(error))}); find the run with `{_runs_hint(repo)}`",
                EXIT_ERROR) from error
        env.out(f"Dispatched the sync as run {run_id}; watching it.")
        try:
            env.run(["gh", "run", "watch", str(run_id), "--repo", repo, "--exit-status"],
                    cwd=env.root)
        except KeyboardInterrupt:
            env.err("Interrupted: the run continues on GitHub. Follow it with "
                    f"`{_follow_hint(repo, run_id)}`.")
            raise
        except bootstrap_github.CommandError as error:
            raise StepFailed(
                f"{FIRST_SYNC} failed: run {run_id} did not succeed or could not be watched "
                f"({_printable(str(error))}); follow it with `{_follow_hint(repo, run_id)}`",
                EXIT_ERROR) from error
        env.out(f"The sync run {run_id} succeeded.")


ACTIONS[FIRST_SYNC] = FirstSyncAction()


# --- command line -------------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def build_parser(prog="bootstrap.py start"):
    """The options of `start`: its own, and the `init` flags it passes to `project_context`."""
    parser = _Parser(prog=prog, description="Work through the bootstrap steps.")
    parser.add_argument("--no-input", action="store_true",
                        help="never prompt, even in a terminal; stop with exit 3 to confirm")
    parser.add_argument("--confirm", metavar="STEP_ID@DIGEST",
                        help="perform that step, using the value of a preview's confirm: line")
    parser.add_argument("--issue", type=int, metavar="N",
                        help="the bootstrap issue to use when several exist")
    parser.add_argument("--name", help="project name (default: repository name)")
    parser.add_argument("--description", help="one-line project description")
    parser.add_argument("--owner", help="GitHub owner (default: owner of the origin remote)")
    board = parser.add_mutually_exclusive_group()
    board.add_argument("--project-number", type=int, help="number of the GitHub Project board")
    board.add_argument("--create-board", action="store_true", help="create the board")
    parser.add_argument("--required-approvals", type=int,
                        help="independent approvals per pull request")
    return parser


def parse_arguments(argv):
    """The parsed options; a usage error raises ValueError instead of ending the process."""
    return build_parser().parse_args(argv)


def run_cli(env, argv):
    """Parse `argv` and run `start`: 0, 1, 2, 3, 4 or 130 (interrupted)."""
    try:
        return run_start(env, parse_arguments(argv))
    except ValueError as error:
        env.err(f"bootstrap start failed: {error}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        env.err("Interrupted.")
        return EXIT_INTERRUPTED


def _no_runner(argv, cwd=None):
    raise ValueError("start needs a command runner; `bootstrap.py start` supplies one")


def main(argv=None, *, root, run=None, remote_default=None, is_tty=None, prompt=input,
         init=None, validate=None, toml_string=None, run_captured=None,
         push_remote_default=None):
    """Entry point for `bootstrap.py start`, which supplies everything it runs through: the
    command runner (`run`, as in `bootstrap.main`), `run_captured` (the dry run's runner that
    keeps stderr; None means stdout only), the repository default, and `init`, `validate` and
    `toml_string` for the `project_context` step. This module never imports `bootstrap`."""
    root = Path(root)
    runner = run or _no_runner
    env = Env(root=root, run=runner, is_tty=sys.stdin.isatty() if is_tty is None else is_tty,
              remote_default=remote_default or (lambda: None),
              push_remote_default=push_remote_default,
              prompt=prompt, init=init, validate_context=validate, toml_string=toml_string,
              run_captured=run_captured)
    try:
        return run_cli(env, sys.argv[1:] if argv is None else argv)
    except Exception as error:  # the contract: a message on stderr, never a traceback
        env.err(f"bootstrap start failed: {error}")
        return EXIT_ERROR

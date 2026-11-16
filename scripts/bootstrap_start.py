"""The guided-bootstrap `start` command: the generic machinery (research D4, D12, D13).

This module holds the preview block and digest, the driver loop and `--confirm` handling. The
per-step work plugs in through `ACTIONS`: one object per confirmable step with these methods
(all optional; `StepAction` supplies the defaults):

- `missing_flags(env, step)`: init flags still missing (only `project_context` has any);
- `preview(env, step, candidate)`: the step's own preview lines;
- `commit_files(env, step)`: the paths the step will commit, hashed into the digest;
- `perform(env, step, candidate)`: the confirmed action; raises `StepFailed` to stop.

Every `gh` and `git` call goes through the injected `run`, and no write command runs before a
confirmation: previews and digests only read.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import bootstrap_github
import bootstrap_steps

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

    def preview(self, env, step, candidate=None):
        return []

    def commit_files(self, env, step):
        return []

    def perform(self, env, step, candidate=None):
        raise StepFailed(f"start cannot perform {step['id']} yet")


ACTIONS = {}  # step id -> StepAction; the per-step tasks (T038-T047) register theirs here
_DEFAULT_ACTION = StepAction()


@dataclass
class Env:
    """What `start` reads and writes through: injected so tests never reach git or GitHub."""
    root: Path
    run: Callable
    remote_default: Callable
    is_tty: bool = False
    prompt: Callable = input
    out: Callable = print
    err: Callable = lambda text="": print(text, file=sys.stderr)
    actions: dict = field(default_factory=lambda: ACTIONS)
    report: Callable = None  # () -> the `check --json` document, read fresh each call
    default_branch: str = None  # from the latest report, set by the driver
    _repo: str = None

    def __post_init__(self):
        if self.report is None:
            self.report = lambda: bootstrap_steps.build_report(
                self.root, self.run, self.remote_default)

    def action_for(self, step_id):
        return self.actions.get(step_id, _DEFAULT_ACTION)

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
    lines = [f"Step {step['id']}: {step['title']}"]
    if candidate is not None:
        branch = candidate.get("branch") or "no branch yet"
        lines.append(f"Bootstrap issue #{candidate['number']}: {candidate['title']} ({branch})")
    action = step.get("action") or {}
    if action.get("text"):
        lines.append(f"Action: {action['text']}")
    if action.get("command"):
        lines.append(f"Command: {action['command']}")
    lines.extend(env.action_for(step["id"]).preview(env, step, candidate))
    return strip_ansi("\n".join(lines))


def confirm_line(step_id, digest):
    return f"confirm: {step_id}@{digest}"


def _read(env, argv):
    return env.run(argv, cwd=env.root).strip()


def _tip(env, branch):
    """The branch's tip on GitHub, `none` when it is not there or cannot be read."""
    if not branch:
        return NONE
    try:
        return env.run(["gh", "api", f"repos/{env.repo()}/branches/{quote(branch, safe='/')}",
                        "--jq", ".commit.sha"]).strip() or NONE
    except bootstrap_github.CommandError:
        return NONE


def read_refs(env, default_branch, step_id=None, candidate=None):
    """HEAD, the default tip, the open PR's head and the branch tip, `none` where not applicable.

    The branch is the checked-out one when it is a bootstrap branch. For `bootstrap_issue` it is
    the chosen candidate's own branch instead (research D4), which may not be checked out; a
    candidate without a branch has none.
    """
    head = _read(env, ["git", "rev-parse", "HEAD"])
    default_tip = _tip(env, default_branch)
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
        except (bootstrap_github.CommandError, ValueError, KeyError, TypeError, IndexError):
            pr_head = NONE
        if pr_head == NONE:
            branch_tip = _tip(env, branch)
    return [head, default_tip, pr_head, branch_tip]


def file_hashes(env, paths):
    """`git hash-object` of each path in path order, `deleted` for one that is gone."""
    hashes = []
    for path in sorted(paths):
        if os.path.lexists(Path(env.root) / path):  # a dangling symlink is still a file
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
        env.out(f"  {action['text']}")
    if action.get("command"):
        env.out(f"  Command: {action['command']}")
    if action.get("doc"):
        env.out(f"  See {action['doc']}")


def _show_step(env, step):
    env.out(f"{step['id']} ({step['state']}): {step['title']}")
    _print_action(env, step)


def _stop(env, step):
    """A blocking step that `start` does not perform: print the action and its doc link."""
    env.out(f"Stopped at {step['id']}: {step['title']}")
    _print_action(env, step)
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
        env.out(f"To proceed, rerun with {flags} (the value below).")
        env.out(confirm_line(step["id"], digest))
        return EXIT_CONFIRM
    env.out(confirm_line(step["id"], digest))
    try:
        answer = env.prompt("Proceed? [y/N] ")
    except EOFError:
        answer = ""
    if answer != "y":
        env.out("Nothing was changed.")
        return EXIT_STOPPED
    code, step = _current_step(env, step["id"])  # the report may have changed meanwhile
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
            missing = env.action_for(step["id"]).missing_flags(env, step)
            if missing and not interactive:
                env.err(f"{step['id']} needs: {', '.join(missing)}")
                return EXIT_ERROR
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
    env.out(f"{len(candidates)} bootstrap issues found. Choose one and rerun with "
            f"--confirm {step['id']}@<digest> --issue <number>, using that issue's confirm line.")
    for candidate in candidates:
        number = str(candidate["number"])
        block, digest = _preview(env, step, candidate, number)
        env.out("")
        env.out(block)
        env.out(f"To use #{number}, rerun with --confirm {step['id']}@{digest} --issue {number}.")
        env.out(confirm_line(step["id"], digest))
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
        env.out(f"  #{found['number']}: {found['title']} ({found.get('branch') or 'no branch yet'})")
    try:
        answer = env.prompt("Which bootstrap issue should be used? ").strip().lstrip("#")
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
    missing = env.action_for(step_id).missing_flags(env, step)  # (5)
    if missing:
        env.err(f"{step_id} needs: {', '.join(missing)}")
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
    acted = ()
    if args.confirm is not None:
        code, step_id = _confirmed(env, args, args.confirm)
        if code is not None:
            return code
        acted = (step_id,)
    return _drive(env, args, acted)


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


def run(env, argv):
    """Parse `argv` and run `start`: 0, 1, 2, 3, 4 or 130 (interrupted)."""
    try:
        return run_start(env, parse_arguments(argv))
    except ValueError as error:
        env.err(f"bootstrap start failed: {error}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        env.err("Interrupted.")
        return EXIT_INTERRUPTED


def main(argv=None, *, root, run_command=None, remote_default=None, is_tty=None, prompt=input):
    """Entry point for `bootstrap.py start`; `bootstrap.py` supplies its own runners."""
    import bootstrap  # late: bootstrap imports this module's siblings, not this one

    root = Path(root)
    runner = run_command or bootstrap.run_command
    env = Env(root=root, run=runner, is_tty=sys.stdin.isatty() if is_tty is None else is_tty,
              remote_default=remote_default or (lambda: bootstrap.remote_defaults(root, runner)),
              prompt=prompt)
    try:
        return run(env, sys.argv[1:] if argv is None else argv)
    except Exception as error:  # the contract: a message on stderr, never a traceback
        env.err(f"bootstrap start failed: {error}")
        return EXIT_ERROR

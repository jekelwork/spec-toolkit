"""Tests for bootstrap_start: the preview block and digest, the driver loop and --confirm."""

import contextlib
import dataclasses
import hashlib
import io
import itertools
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: E402
import bootstrap_github  # noqa: E402
import project_context  # noqa: E402
import bootstrap_start  # noqa: E402
import bootstrap_steps  # noqa: E402
import test_bootstrap_github  # noqa: E402
import test_bootstrap_steps  # noqa: E402

ISSUE_STEP = "bootstrap_issue"
DIGEST = re.compile(r"[0-9a-f]{12}")
WRITE_VERBS = (
    ("git", "commit"), ("git", "push"), ("git", "checkout"), ("git", "add"),
    ("gh", "issue", "create"), ("gh", "issue", "develop"), ("gh", "pr", "create"),
    ("gh", "workflow", "run"), ("gh", "run", "watch"),
)


def step(step_id="pull_request", state="todo", performable=True, details=None, title="Title",
         action=None):
    """A step as the report holds it (a dict, the shape of `check --json`)."""
    built = bootstrap_steps.make_step(
        step_id, state, title, performable=performable and state == "todo",
        action=action, details=details)
    return dataclasses.asdict(built)


def report(*steps):
    """A report over the given steps; `next_step` is the first blocking one that is not done."""
    next_step = next((s["id"] for s in steps if s["blocking"] and s["state"] != "done"), None)
    manual = any(s["state"] == "manual" for s in steps)
    result = "incomplete" if next_step else (
        "complete_with_manual_settings" if manual else "complete")
    return {"format_version": 1, "result": result,
            "next_step": next_step, "bootstrap_merged": False, "default_branch": "main",
            "steps": list(steps)}


# The only commands that may run before a confirmation (SC-003). T038-T047 add the reads their
# previews need here, never a write. `gh api` is allowed only without a method or a field.
READ_ONLY_PREFIXES = (
    ("git", "rev-parse"), ("git", "branch", "--show-current"), ("git", "hash-object"),
    ("git", "status", "--porcelain"), ("git", "merge-base", "--is-ancestor"),
    ("git", "branch", "--format=%(refname:short)"), ("git", "remote", "get-url"),
    ("git", "diff", "--name-only"),  # the `pull_request` preview (T044): <default>..HEAD
    ("gh", "repo", "view"), ("gh", "pr", "list"), ("gh", "pr", "view"), ("gh", "issue", "list"),
    ("gh", "api"),
    # the `github_settings` preview (T042): the existing `github` plan reads these
    ("gh", "label", "list"), ("gh", "secret", "list"), ("gh", "variable", "list"),
    # the `first_sync` preview (T046): the sync's dry run, which test_a_dry_run_needs_no_flag pins
    ("python3", "scripts/sync-roadmap-issues.py"),
    ("git", "config", "--get", "remote.origin.url"),  # the repository that dry run reads
)
API_WRITE_FLAGS = ("-X", "--method", "-f", "-F", "--field", "--raw-field", "--input")


def is_graphql_read(call):
    """`gh api graphql --input <body file>`: the plan's board reads send their query that way.

    The argv cannot tell a query from a mutation, so a test that relies on this form must also
    check the bodies (`FakeGitHub.writes`).
    """
    return len(call) == 5 and call[:3] == ("gh", "api", "graphql") and call[3] == "--input"


def is_read_only(call, graphql_bodies_checked=False):
    """Whether `call` may run before a confirmation; the graphql body form only when the test
    also checks the bodies it sends."""
    if graphql_bodies_checked and is_graphql_read(call):
        return True
    if not any(call[:len(prefix)] == prefix for prefix in READ_ONLY_PREFIXES):
        return False
    # only the sync's dry run is a read; argparse accepts abbreviations (`--ap`, `--appl`)
    if call[0] == "python3" and any(argument.startswith("--ap") for argument in call):
        return False
    # prefix match, so the joined forms (`-XPOST`, `--method=POST`, `-fkey=v`) are caught too
    return not (call[:2] == ("gh", "api") and any(
        argument.startswith(flag) for argument in call[2:] for flag in API_WRITE_FLAGS))


class FakeRun:
    """Stands in for git and gh: answers by argv prefix, records every call."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = {
            ("git", "rev-parse", "HEAD"): "head000\n",
            ("git", "branch", "--show-current"): "main\n",
            ("gh", "repo", "view"): '{"nameWithOwner": "acme/widgets"}',
            ("gh", "api", "repos/acme/widgets/branches/main"): "tip000\n",
        }
        self.answers.update(answers or {})

    def __call__(self, argv, cwd=None):
        argv = tuple(argv)
        self.calls.append(argv)
        for prefix, answer in sorted(self.answers.items(), key=lambda item: -len(item[0])):
            if argv[:len(prefix)] == prefix:
                if isinstance(answer, Exception):
                    raise answer
                return answer(argv) if callable(answer) else answer
        if argv[:2] == ("git", "hash-object"):
            return f"hash-of-{argv[2]}\n"
        raise bootstrap_github.CommandError(f"unanswered command {argv}", returncode=1)

    def writes(self):
        return [call for call in self.calls
                if any(call[:len(verb)] == verb for verb in WRITE_VERBS)]


class StubAction(bootstrap_start.StepAction):
    """The seam the per-step actions (T038-T047) fill: preview lines, files, perform."""

    def __init__(self, lines=("Will do the thing.",), files=(), flags=(), run=None, fail=None):
        self.lines = list(lines)
        self.files = list(files)
        self.flags = list(flags)
        self.performed = []
        self.run = run
        self.fail = fail

    def missing_flags(self, env, step_dict):
        return list(self.flags)

    def preview(self, env, step_dict, candidate=None):
        return list(self.lines)

    def commit_files(self, env, step_dict):
        return list(self.files)

    def perform(self, env, step_dict, candidate=None):
        self.performed.append(candidate)
        if self.run:
            self.run(["git", "commit", "-m", "x"])
        if self.fail:
            raise self.fail


def make_env(root=None, run=None, actions=None, is_tty=False, prompt=None, reports=None,
             remote_default=lambda: ("acme", "widgets"), init=bootstrap.init,
             validate_context=bootstrap.validate, toml_string=bootstrap.toml_string):
    out, err = io.StringIO(), io.StringIO()
    run = run or FakeRun()
    queue = list(reports or [])

    def read_report():
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def no_prompt(text):
        raise AssertionError(f"prompt called: {text}")

    env = bootstrap_start.Env(
        root=Path(root or "."), run=run, remote_default=remote_default, init=init,
        validate_context=validate_context, toml_string=toml_string,
        is_tty=is_tty, prompt=prompt or no_prompt,
        out=lambda text="": print(text, file=out), err=lambda text="": print(text, file=err),
        actions=actions if actions is not None else {}, report=read_report)
    env.default_branch = "main"
    env.out_buffer, env.err_buffer = out, err
    return env


def arguments(**overrides):
    values = {"no_input": False, "confirm": None, "issue": None, "name": None,
              "description": None, "owner": None, "project_number": None,
              "create_board": False, "required_approvals": None}
    values.update(overrides)
    return SimpleNamespace(**values)


def sha12(*lines):
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:12]


class PreviewBlockTest(unittest.TestCase):
    def build(self, step_dict=None, actions=None, candidate=None):
        env = make_env(actions={"pull_request": StubAction()} if actions is None else actions)
        return bootstrap_start.preview_block(
            env, step_dict or step(action={"text": "Open it.", "command": "gh pr create",
                                           "doc": "docs/bootstrap.md#x"}), candidate)

    def test_the_block_names_the_step_and_its_action_and_the_steps_own_lines(self):
        block = self.build()

        self.assertIn("pull_request", block)
        self.assertIn("Title", block)
        self.assertIn("Open it.", block)
        self.assertIn("gh pr create", block)
        self.assertIn("Will do the thing.", block)

    def test_the_block_is_the_same_on_every_call_and_has_no_confirm_line(self):
        first, second = self.build(), self.build()

        self.assertEqual(first, second)
        self.assertNotIn("confirm:", first)
        self.assertNotIn("Proceed?", first)

    def test_ansi_codes_are_removed(self):
        actions = {"pull_request": StubAction(lines=["\x1b[31mred\x1b[0m line"])}

        block = self.build(actions=actions)

        self.assertIn("red line", block)
        self.assertNotIn("\x1b", block)

    def test_a_candidate_has_its_own_block(self):
        actions = {"bootstrap_issue": StubAction()}
        issue = step("bootstrap_issue", details={"candidates": []})

        one = self.build(issue, actions, {"number": 7, "title": "A", "branch": "7-chore-bootstrap"})
        two = self.build(issue, actions, {"number": 8, "title": "B", "branch": None})

        self.assertIn("#7", one)
        self.assertIn("7-chore-bootstrap", one)
        self.assertNotIn("#8", one)
        self.assertIn("#8", two)

    def test_a_step_without_a_registered_action_still_gets_a_block(self):
        block = self.build(actions={})

        self.assertIn("pull_request", block)


class DigestTest(unittest.TestCase):
    def digest(self, step_dict=None, block="BLOCK", files=(), issue=None, run=None, root=None):
        env = make_env(root=root, run=run or FakeRun())
        return bootstrap_start.compute_digest(
            env, step_dict or step(), block, list(files), issue)

    def test_confirm_line_has_the_step_and_twelve_hex_characters(self):
        line = bootstrap_start.confirm_line("pull_request", "0123456789ab")

        self.assertEqual("confirm: pull_request@0123456789ab", line)

    def test_the_digest_is_the_first_twelve_hex_characters_of_the_sha256_of_the_inputs(self):
        expected = sha12("pull_request", "BLOCK", "head000", "tip000", "none", "none")

        self.assertEqual(expected, self.digest())
        self.assertRegex(self.digest(), DIGEST)

    def test_a_multi_line_block_contributes_its_lines_in_order(self):
        expected = sha12("pull_request", "a", "b", "head000", "tip000", "none", "none")

        self.assertEqual(expected, self.digest(block="a\nb"))

    def test_bootstrap_issue_includes_the_chosen_issue_number_after_the_step_id(self):
        issue = step("bootstrap_issue")
        expected = sha12("bootstrap_issue", "7", "BLOCK", "head000", "tip000", "none", "none")

        self.assertEqual(expected, self.digest(issue, issue="7"))
        self.assertNotEqual(self.digest(issue, issue="8"), self.digest(issue, issue="7"))
        self.assertNotEqual(self.digest(issue, issue="new"), self.digest(issue, issue="7"))

    def test_other_steps_ignore_an_issue_number(self):
        self.assertEqual(self.digest(issue="7"), self.digest(issue=None))

    def test_every_file_to_commit_is_hashed_in_path_order_and_a_removal_is_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "b.txt").write_text("b")
            (Path(directory) / "a.txt").write_text("a")
            run = FakeRun()

            digest = self.digest(files=["b.txt", "gone.txt", "a.txt"], run=run, root=directory)

            expected = sha12(
                "pull_request", "BLOCK", "hash-of-a.txt", "hash-of-b.txt", "deleted",
                "head000", "tip000", "none", "none")
            self.assertEqual(expected, digest)
            self.assertIn(("git", "hash-object", "a.txt"), run.calls)

    def test_a_changed_file_head_or_default_tip_changes_the_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "a.txt").write_text("a")
            base = self.digest(files=["a.txt"], root=directory)
            other_file = FakeRun({("git", "hash-object"): "different\n"})
            other_head = FakeRun({("git", "rev-parse", "HEAD"): "head111\n"})
            other_tip = FakeRun({("gh", "api", "repos/acme/widgets/branches/main"): "tip111\n"})

            for run in (other_file, other_head, other_tip):
                self.assertNotEqual(
                    base, self.digest(files=["a.txt"], run=run, root=directory))

    def test_the_pull_request_head_and_branch_tip_are_read_on_a_bootstrap_branch(self):
        branch = "7-chore-bootstrap"
        with_pr = FakeRun({
            ("git", "branch", "--show-current"): f"{branch}\n",
            ("gh", "pr", "list"): '[{"headRefOid": "pr0001"}]',
            # the branch is pushed too, but an open PR's head is what counts: tip stays none
            ("gh", "api", f"repos/acme/widgets/branches/{branch}"): "pushed1\n",
        })
        without_pr = FakeRun({
            ("git", "branch", "--show-current"): f"{branch}\n",
            ("gh", "pr", "list"): "[]",
            ("gh", "api", f"repos/acme/widgets/branches/{branch}"): "pushed1\n",
        })
        not_pushed = FakeRun({
            ("git", "branch", "--show-current"): f"{branch}\n",
            ("gh", "pr", "list"): "[]",
            ("gh", "api", f"repos/acme/widgets/branches/{branch}"):
                bootstrap_github.CommandError("HTTP 404: Not Found", returncode=1),
        })

        self.assertEqual(
            sha12("pull_request", "BLOCK", "head000", "tip000", "pr0001", "none"),
            self.digest(run=with_pr))
        self.assertEqual(
            sha12("pull_request", "BLOCK", "head000", "tip000", "none", "pushed1"),
            self.digest(run=without_pr))
        self.assertEqual(
            sha12("pull_request", "BLOCK", "head000", "tip000", "none", "none"),
            self.digest(run=not_pushed))

    def test_a_dangling_symlink_is_hashed_by_git_not_reported_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "link").symlink_to(Path(directory, "missing"))
            run = FakeRun()

            digest = self.digest(files=["link"], run=run, root=directory)

            self.assertEqual(sha12("pull_request", "BLOCK", "hash-of-link", "head000", "tip000",
                                   "none", "none"), digest)
            self.assertIn(("git", "hash-object", "link"), run.calls)

    def test_bootstrap_issue_reads_refs_for_the_candidates_own_branch(self):
        issue = step("bootstrap_issue")
        candidate = {"number": 7, "title": "A", "branch": "7-chore-bootstrap"}
        run = FakeRun({  # checked out: main, not the candidate's branch
            ("gh", "pr", "list"): "[]",
            ("gh", "api", "repos/acme/widgets/branches/7-chore-bootstrap"): "pushed1\n",
        })
        env = make_env(run=run)

        digest = bootstrap_start.compute_digest(env, issue, "BLOCK", [], "7", candidate)

        self.assertEqual(sha12("bootstrap_issue", "7", "BLOCK", "head000", "tip000", "none",
                               "pushed1"), digest)

    def test_bootstrap_issue_without_a_branch_has_no_branch_refs(self):
        issue = step("bootstrap_issue")
        candidate = {"number": 7, "title": "A", "branch": None}
        env = make_env()

        digest = bootstrap_start.compute_digest(env, issue, "BLOCK", [], "7", candidate)

        self.assertEqual(sha12("bootstrap_issue", "7", "BLOCK", "head000", "tip000", "none",
                               "none"), digest)

    def test_the_inputs_are_read_without_any_write_command(self):
        run = FakeRun()

        self.digest(run=run)

        self.assertEqual([], run.writes())


def output(env):
    return env.out_buffer.getvalue()


def problems(env):
    return env.err_buffer.getvalue()


def prompter(*answers):
    asked = []
    queue = list(answers)

    def prompt(text):
        asked.append(text)
        return queue.pop(0)

    prompt.asked = asked
    return prompt


def done(step_id):
    return step(step_id, "done")


OFFER = step("pull_request", action={"text": "Push and open it.", "command": None,
                                     "doc": "docs/bootstrap.md#one-bootstrap-pull-request"})
ALL_DONE = report(done("prerequisites"), done("pull_request"))


class DriverTest(unittest.TestCase):
    def start(self, reports, args=None, stub=None, **kwargs):
        stub = stub or StubAction()
        env = make_env(reports=reports, actions={"pull_request": stub}, **kwargs)
        return bootstrap_start.run_start(env, args or arguments()), env, stub

    def test_a_complete_report_exits_zero(self):
        code, env, _ = self.start([ALL_DONE])

        self.assertEqual(0, code)
        self.assertIn("complete", output(env))

    def test_non_blocking_steps_that_are_not_done_are_printed_and_passed(self):
        credential = step("board_credential", "not_checked", title="Could not be checked: x",
                          action={"text": "Fix the cause.", "command": None, "doc": None})
        workflows = step("board_workflows", "manual", action={
            "text": "Enable the workflow.", "command": None, "doc": "docs/bootstrap.md#settings"})
        first = report(done("prerequisites"), credential, workflows, done("pull_request"))

        code, env, _ = self.start([first])

        self.assertEqual(0, code)
        self.assertIn("board_credential", output(env))
        self.assertIn("Fix the cause.", output(env))
        self.assertIn("Enable the workflow.", output(env))

    def test_a_confirmation_step_prompts_in_a_terminal_and_acts_on_y(self):
        prompt = prompter("y")

        code, env, stub = self.start([report(OFFER), report(OFFER), ALL_DONE], is_tty=True,
                                     prompt=prompt)

        self.assertEqual(0, code)
        self.assertEqual(["Proceed? [y/N] "], prompt.asked)
        self.assertEqual([None], stub.performed)
        self.assertIn("Will do the thing.", output(env))
        self.assertRegex(output(env), r"confirm: pull_request@[0-9a-f]{12}")

    def test_anything_but_y_exits_one_and_changes_nothing(self):
        for answer in ("", "n", "yes", "Y", " y"):
            with self.subTest(answer=answer):
                run = FakeRun()
                stub = StubAction(run=run)

                code, _, _ = self.start(
                    [report(OFFER)], stub=stub, is_tty=True, prompt=prompter(answer), run=run)

                self.assertEqual(1, code)
                self.assertEqual([], stub.performed)
                self.assertEqual([], run.writes())

    def test_without_a_terminal_the_preview_is_printed_and_start_exits_three(self):
        code, env, stub = self.start([report(OFFER)])

        self.assertEqual(3, code)
        self.assertEqual([], stub.performed)
        last = output(env).rstrip("\n").split("\n")[-1]
        self.assertRegex(last, r"^confirm: pull_request@[0-9a-f]{12}$")
        self.assertIn("--confirm", output(env))

    def test_no_input_behaves_like_no_terminal_and_never_prompts(self):
        code, env, stub = self.start([report(OFFER)], arguments(no_input=True), is_tty=True)

        self.assertEqual(3, code)
        self.assertEqual([], stub.performed)

    def test_the_printed_digest_is_the_one_compute_digest_gives(self):
        _, env, _ = self.start([report(OFFER)])
        block = bootstrap_start.preview_block(env, OFFER)

        expected = bootstrap_start.compute_digest(env, OFFER, block, [])

        self.assertIn(bootstrap_start.confirm_line("pull_request", expected), output(env))

    def test_the_block_is_identical_in_a_terminal_and_before_exit_three(self):
        _, terminal, _ = self.start([report(OFFER)], is_tty=True, prompt=prompter("n"))
        _, piped, _ = self.start([report(OFFER)])

        block = bootstrap_start.preview_block(piped, OFFER)
        self.assertIn(block, output(terminal))
        self.assertIn(block, output(piped))

    def test_a_step_that_stops_being_performable_while_the_prompt_waits_is_not_performed(self):
        blocked = step("pull_request", "todo", False, action={
            "text": "Check out the branch.", "command": None, "doc": "docs/x"})

        code, env, stub = self.start([report(OFFER), report(blocked)], is_tty=True,
                                     prompt=prompter("y"))

        self.assertEqual(1, code)
        self.assertEqual([], stub.performed)
        self.assertIn("Check out the branch.", output(env))

    def test_a_different_next_step_after_the_yes_exits_four_and_changes_nothing(self):
        later = step("first_sync", "todo")

        code, env, stub = self.start(
            [report(OFFER, later), report(done("pull_request"), later)], is_tty=True,
            prompt=prompter("y"))

        self.assertEqual(4, code)
        self.assertEqual([], stub.performed)
        self.assertIn("next step is first_sync, not pull_request", problems(env))

    def test_a_non_blocking_step_is_printed_once_across_reports(self):
        manual = step("board_workflows", "manual", title="Workflow is off")
        first = report(manual, OFFER)
        second = report(manual, done("pull_request"))

        code, env, _ = self.start([first, first, second], is_tty=True, prompt=prompter("y"))

        self.assertEqual(0, code)
        self.assertEqual(1, output(env).count("Workflow is off"))
        self.assertIn("Bootstrap is complete; manual settings remain (see above).", output(env))

    def test_end_of_input_at_the_prompt_declines(self):
        def closed(text):
            raise EOFError

        code, env, stub = self.start([report(OFFER)], is_tty=True, prompt=closed)

        self.assertEqual(1, code)
        self.assertEqual([], stub.performed)
        self.assertIn("Nothing was changed.", output(env))

    def test_an_interrupt_at_the_prompt_exits_130_without_acting(self):
        def interrupted(text):
            raise KeyboardInterrupt

        stub = StubAction()
        env = make_env(reports=[report(OFFER)], actions={"pull_request": stub}, is_tty=True,
                       prompt=interrupted)

        self.assertEqual(130, bootstrap_start.run(env, []))
        self.assertEqual([], stub.performed)

    def test_the_stop_and_step_output_share_the_action_lines(self):
        stopped = step("constitution", "todo", False, action={
            "text": "Run it.", "command": "cmd --x", "doc": "docs/c"})
        listed = step("board_workflows", "manual", action={
            "text": "Run it.", "command": "cmd --x", "doc": "docs/c"})
        _, stop_env, _ = self.start([report(stopped)])
        _, step_env, _ = self.start([report(listed, done("pull_request"))])

        for env in (stop_env, step_env):
            self.assertIn("  Run it.\n  Command: cmd --x\n  See docs/c\n", output(env))

    def test_each_stop_exits_one_with_the_action_and_the_doc_link(self):
        stops = {
            "prerequisites": step("prerequisites", "todo", False, action={
                "text": "Install gh.", "command": None, "doc": "docs/bootstrap.md#prerequisites"}),
            "constitution": step("constitution", "todo", False, action={
                "text": "Run the skill.", "command": None, "doc": "docs/bootstrap.md#constitution"}),
            "roadmap": step("roadmap", "todo", False, action={
                "text": "Write it.", "command": None, "doc": "docs/bootstrap.md#roadmap"}),
            "merge waiting": step("merge", "waiting", False, action={
                "text": "Merge the PR.", "command": None,
                "doc": "docs/bootstrap.md#one-bootstrap-pull-request"}),
            "blocking not_checked": step("github_settings", "not_checked", False, action={
                "text": "Fix gh.", "command": None, "doc": "docs/bootstrap.md#github-settings"}),
            "not performable": step("project_context", "todo", False, action={
                "text": "Fix the file.", "command": None,
                "doc": "docs/bootstrap.md#project-contexttoml"}),
        }
        for name, blocked in stops.items():
            with self.subTest(stop=name):
                code, env, _ = self.start([report(blocked)])

                self.assertEqual(1, code)
                self.assertIn(blocked["action"]["text"], output(env))
                self.assertIn(blocked["action"]["doc"], output(env))

    def test_steps_after_a_stop_are_not_reached(self):
        stop = step("constitution", "todo", False, action={"text": "Run it.", "command": None,
                                                          "doc": "docs/x"})
        later = step("pull_request", "todo")

        code, env, stub = self.start([report(stop, later)])

        self.assertEqual(1, code)
        self.assertEqual([], stub.performed)
        self.assertNotIn("confirm:", output(env))

    def test_a_step_still_open_after_its_action_stops_the_loop_with_exit_one(self):
        stub = StubAction()

        code, env, _ = self.start([report(OFFER)], stub=stub, is_tty=True,
                                  prompt=prompter("y"))

        self.assertEqual(1, code)
        self.assertEqual([None], stub.performed)
        self.assertIn("still not done", problems(env) + output(env))

    def test_the_digest_is_recomputed_after_the_yes_and_a_change_exits_four(self):
        class Shifting(StubAction):
            def preview(self, env, step_dict, candidate=None):
                self.calls = getattr(self, "calls", 0) + 1
                return [f"preview number {self.calls}"]

        stub = Shifting()

        code, env, _ = self.start([report(OFFER)], stub=stub, is_tty=True, prompt=prompter("y"))

        self.assertEqual(4, code)
        self.assertEqual([], stub.performed)
        self.assertIn("preview changed", problems(env))

    def test_a_failed_action_prints_its_message_and_returns_its_code(self):
        stub = StubAction(fail=bootstrap_start.StepFailed("git said no", 2))

        code, env, _ = self.start([report(OFFER)], stub=stub, is_tty=True, prompt=prompter("y"))

        self.assertEqual(2, code)
        self.assertIn("git said no", problems(env))

    def test_a_missing_init_flag_without_a_terminal_exits_two_without_a_preview(self):
        init = step("project_context", "todo")
        stub = StubAction(flags=["--description", "--required-approvals"])
        env = make_env(reports=[report(init)], actions={"project_context": stub})

        code = bootstrap_start.run_start(env, arguments())

        self.assertEqual(2, code)
        self.assertIn("--description", problems(env))
        self.assertIn("--required-approvals", problems(env))
        self.assertNotIn("confirm:", output(env))
        self.assertEqual([], stub.performed)


CONFIRMABLE = ("bootstrap_issue", "project_context", "github_settings", "pull_request",
               "first_sync")
CANDIDATES = [{"number": 7, "title": "Bootstrap", "branch": "7-chore-bootstrap"},
              {"number": 8, "title": "Bootstrap again", "branch": None}]


def digest_of(env, step_dict, candidate=None, issue=None):
    block = bootstrap_start.preview_block(env, step_dict, candidate)
    files = env.action_for(step_dict["id"]).commit_files(env, step_dict)
    return bootstrap_start.compute_digest(env, step_dict, block, files, issue, candidate)


class ConfirmTest(unittest.TestCase):
    def confirm(self, value, reports, stub=None, issue=None, step_id="pull_request", **kwargs):
        stub = stub or StubAction()
        env = make_env(reports=reports, actions={step_id: stub}, **kwargs)
        code = bootstrap_start.run_start(env, arguments(
            confirm=value, issue=issue, no_input=True))
        return code, env, stub

    def good(self, step_dict=OFFER, candidate=None, issue=None, actions=None):
        """The digest `start` would print for this step in the unchanged state."""
        env = make_env(actions=actions or {step_dict["id"]: StubAction()})
        env.default_branch = "main"
        return digest_of(env, step_dict, candidate, issue)

    def test_a_push_to_the_candidates_branch_after_the_preview_exits_four(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES[:1]})
        branch_url = "repos/acme/widgets/branches/7-chore-bootstrap"
        before = FakeRun({("gh", "pr", "list"): "[]", ("gh", "api", branch_url): "pushed1\n"})
        after = FakeRun({("gh", "pr", "list"): "[]", ("gh", "api", branch_url): "pushed2\n"})
        stub = StubAction()
        preview = make_env(run=before, reports=[report(issue)], actions={ISSUE_STEP: stub})
        bootstrap_start.run_start(preview, arguments(no_input=True))
        shown = re.search(r"confirm: bootstrap_issue@([0-9a-f]{12})", output(preview)).group(1)

        code, env, _ = self.confirm(f"bootstrap_issue@{shown}", [report(issue)], stub,
                                    step_id=ISSUE_STEP, run=after)

        self.assertEqual(4, code)
        self.assertEqual([], stub.performed)
        again = self.confirm(f"bootstrap_issue@{shown}", [report(issue), ALL_DONE], stub,
                             step_id=ISSUE_STEP, run=before)
        self.assertEqual(0, again[0])

    def test_an_unknown_next_step_id_exits_two_naming_it(self):
        broken = report(OFFER)
        broken["next_step"] = "mystery"

        code, env, stub = self.confirm("pull_request@0123456789ab", [broken])

        self.assertEqual(2, code)
        self.assertIn("mystery", problems(env))
        self.assertEqual([], stub.performed)

    def test_the_exit_three_hint_keeps_the_issue_flag_and_can_be_followed(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES})
        stub = StubAction()
        first = make_env(reports=[report(issue)], actions={ISSUE_STEP: stub})

        code = bootstrap_start.run_start(first, arguments(no_input=True, issue=8))

        self.assertEqual(3, code)
        hint = re.search(r"rerun with (.*?) \(the value below\)", output(first)).group(1)
        self.assertIn("--issue 8", hint)
        second = make_env(reports=[report(issue), ALL_DONE], actions={ISSUE_STEP: stub})
        followed = bootstrap_start.parse_arguments(["--no-input", *hint.split()])
        self.assertEqual(0, bootstrap_start.run_start(second, followed))
        self.assertEqual([CANDIDATES[1]], stub.performed)

    def test_the_hint_names_the_issue_for_one_candidate_only_when_it_was_given(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES[:1]})
        for given, expected in ((None, False), (7, True)):
            with self.subTest(issue=given):
                env = make_env(reports=[report(issue)], actions={ISSUE_STEP: StubAction()})

                bootstrap_start.run_start(env, arguments(no_input=True, issue=given))

                self.assertEqual(expected, "--issue 7" in output(env))

    def test_the_hint_of_a_new_issue_has_no_issue_flag(self):
        issue = step("bootstrap_issue", details={"candidates": []})
        env = make_env(reports=[report(issue)], actions={ISSUE_STEP: StubAction()})

        bootstrap_start.run_start(env, arguments(no_input=True))

        self.assertNotIn("--issue", output(env))

    def test_the_candidate_listing_has_a_short_header(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES})
        env = make_env(reports=[report(issue)], actions={ISSUE_STEP: StubAction()})

        bootstrap_start.run_start(env, arguments(no_input=True))

        self.assertEqual("2 bootstrap issues found; choose one:", output(env).split("\n")[0])

    def test_each_listed_candidate_has_its_own_hint_with_its_issue_flag(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES})
        env = make_env(reports=[report(issue)], actions={ISSUE_STEP: StubAction()})

        bootstrap_start.run_start(env, arguments(no_input=True))

        for candidate in CANDIDATES:
            digest = self.good(issue, candidate, str(candidate["number"]),
                               {ISSUE_STEP: StubAction()})
            self.assertIn(f"--confirm bootstrap_issue@{digest} --issue {candidate['number']}",
                          output(env))

    def test_a_malformed_or_bare_value_exits_two_before_anything_is_read(self):
        for value in ("pull_request", "pull_request@", "pull_request@zzzzzzzzzzzz",
                      "pull_request@0123456789a", "pull_request@0123456789abc",
                      "pull_request@0123456789AB", "@0123456789ab", "a b@0123456789ab"):
            with self.subTest(value=value):
                code, env, stub = self.confirm(value, [ALL_DONE])

                self.assertEqual(2, code)
                self.assertIn("--confirm", problems(env))
                self.assertEqual([], stub.performed)

    def test_a_complete_bootstrap_exits_zero(self):
        code, env, stub = self.confirm("pull_request@0123456789ab", [ALL_DONE])

        self.assertEqual(0, code)
        self.assertIn("bootstrap is complete", output(env).lower())
        self.assertEqual([], stub.performed)

    def test_another_step_than_the_next_one_exits_four(self):
        code, env, stub = self.confirm("first_sync@0123456789ab", [report(OFFER)])

        self.assertEqual(4, code)
        self.assertIn("next step is pull_request, not first_sync", problems(env))
        self.assertEqual([], stub.performed)

    def test_a_next_step_that_needs_no_confirmation_exits_four(self):
        stop = step("constitution", "todo", False)

        code, env, _ = self.confirm("constitution@0123456789ab", [report(stop)])

        self.assertEqual(4, code)
        self.assertIn("next step constitution needs no confirmation", problems(env))

    def test_a_step_that_is_not_performable_exits_one_with_its_action(self):
        blocked = step("pull_request", "todo", False, action={
            "text": "Check out the branch.", "command": "git checkout x", "doc": None})

        code, env, stub = self.confirm("pull_request@0123456789ab", [report(blocked)])

        self.assertEqual(1, code)
        self.assertIn("Check out the branch.", output(env))
        self.assertEqual([], stub.performed)

    def test_a_missing_init_flag_exits_two_naming_every_flag(self):
        init = step("project_context", "todo")
        stub = StubAction(flags=["--description", "--project-number or --create-board"])

        code, env, _ = self.confirm("project_context@0123456789ab", [report(init)], stub,
                                    step_id="project_context")

        self.assertEqual(2, code)
        self.assertIn("--description", problems(env))
        self.assertIn("--project-number or --create-board", problems(env))
        self.assertEqual([], stub.performed)

    def test_not_performable_is_reported_before_a_missing_init_flag(self):
        blocked = step("project_context", "todo", False, action={
            "text": "Fix the invalid file.", "command": None, "doc": "docs/x"})
        stub = StubAction(flags=["--description"])

        code, env, _ = self.confirm("project_context@0123456789ab", [report(blocked)], stub,
                                    step_id="project_context")

        self.assertEqual(1, code)
        self.assertIn("Fix the invalid file.", output(env))
        self.assertNotIn("--description", problems(env))
        self.assertEqual([], stub.performed)

    def test_an_issue_that_is_not_a_candidate_with_no_candidates_exits_three(self):
        issue = step("bootstrap_issue", details={"candidates": []})
        run = FakeRun()
        stub = StubAction(run=run)

        code, env, _ = self.confirm("bootstrap_issue@0123456789ab", [report(issue)], stub,
                                    issue=5, step_id="bootstrap_issue", run=run)

        self.assertEqual(3, code)
        self.assertIn("#5 is not a bootstrap issue candidate", output(env))
        self.assertIn("without --issue", output(env))
        self.assertEqual([], stub.performed)
        self.assertEqual([], run.writes())

    def test_the_driver_also_stops_for_an_issue_that_is_not_a_candidate_with_none(self):
        issue = step("bootstrap_issue", details={"candidates": []})
        stub = StubAction()
        env = make_env(reports=[report(issue)], actions={"bootstrap_issue": stub},
                       is_tty=True)

        code = bootstrap_start.run_start(env, arguments(issue=5))

        self.assertEqual(3, code)
        self.assertEqual([], stub.performed)

    def test_several_candidates_without_a_valid_issue_list_every_preview_and_exit_three(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES})
        for chosen in (None, 99):
            with self.subTest(issue=chosen):
                code, env, stub = self.confirm(
                    "bootstrap_issue@0123456789ab", [report(issue)], issue=chosen,
                    step_id="bootstrap_issue")

                self.assertEqual(3, code)
                self.assertEqual([], stub.performed)
                for candidate in CANDIDATES:
                    expected = self.good(issue, candidate, str(candidate["number"]),
                                         {"bootstrap_issue": StubAction()})
                    self.assertIn(f"#{candidate['number']}", output(env))
                    self.assertIn(
                        bootstrap_start.confirm_line("bootstrap_issue", expected), output(env))

    def test_one_candidate_that_is_not_the_given_issue_is_listed_too(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES[:1]})

        code, _, stub = self.confirm("bootstrap_issue@0123456789ab", [report(issue)], issue=8,
                                     step_id="bootstrap_issue")

        self.assertEqual(3, code)
        self.assertEqual([], stub.performed)

    def test_the_chosen_candidate_is_acted_on_when_its_digest_matches(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES})
        digest = self.good(issue, CANDIDATES[1], "8", {"bootstrap_issue": StubAction()})

        code, _, stub = self.confirm(f"bootstrap_issue@{digest}", [report(issue), ALL_DONE],
                                     issue=8, step_id="bootstrap_issue")

        self.assertEqual(0, code)
        self.assertEqual([CANDIDATES[1]], stub.performed)

    def test_the_digest_of_another_candidate_exits_four(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES})
        digest = self.good(issue, CANDIDATES[0], "7", {"bootstrap_issue": StubAction()})

        code, _, stub = self.confirm(f"bootstrap_issue@{digest}", [report(issue)], issue=8,
                                     step_id="bootstrap_issue")

        self.assertEqual(4, code)
        self.assertEqual([], stub.performed)

    def test_a_single_candidate_or_none_needs_no_issue_flag(self):
        for found, candidate, issue_id in (([], None, "new"), (CANDIDATES[:1], CANDIDATES[0], "7")):
            with self.subTest(candidates=len(found)):
                issue = step("bootstrap_issue", details={"candidates": found})
                digest = self.good(issue, candidate, issue_id, {"bootstrap_issue": StubAction()})

                code, _, stub = self.confirm(
                    f"bootstrap_issue@{digest}", [report(issue), ALL_DONE],
                    step_id="bootstrap_issue")

                self.assertEqual(0, code)
                self.assertEqual([candidate], stub.performed)

    def test_a_digest_mismatch_exits_four_and_changes_nothing(self):
        run = FakeRun()
        stub = StubAction(run=run)

        code, env, _ = self.confirm("pull_request@0123456789ab", [report(OFFER)], stub, run=run)

        self.assertEqual(4, code)
        self.assertIn("preview changed", problems(env))
        self.assertEqual([], stub.performed)
        self.assertEqual([], run.writes())

    def test_a_matching_digest_acts_and_then_continues_to_the_next_confirmation(self):
        later = step("first_sync", "todo")
        stub = StubAction()
        env = make_env(reports=[report(OFFER, later), report(done("pull_request"), later)],
                       actions={"pull_request": stub, "first_sync": StubAction()})
        env.default_branch = "main"
        digest = digest_of(env, OFFER)

        code = bootstrap_start.run_start(env, arguments(
            confirm=f"pull_request@{digest}", no_input=True))

        self.assertEqual(3, code)  # the next confirmation is not given
        self.assertEqual([None], stub.performed)
        self.assertRegex(output(env), r"confirm: first_sync@[0-9a-f]{12}")

    def test_a_confirmation_in_a_terminal_still_never_prompts_for_the_confirmed_step(self):
        stub = StubAction()
        env = make_env(reports=[report(OFFER), ALL_DONE], actions={"pull_request": stub},
                       is_tty=True)
        env.default_branch = "main"
        digest = digest_of(env, OFFER)

        code = bootstrap_start.run_start(env, arguments(confirm=f"pull_request@{digest}"))

        self.assertEqual(0, code)
        self.assertEqual([None], stub.performed)

    def test_a_stray_issue_does_not_change_other_steps(self):
        env = make_env(reports=[report(OFFER), ALL_DONE], actions={"pull_request": StubAction()})
        env.default_branch = "main"
        digest = digest_of(env, OFFER)

        code = bootstrap_start.run_start(env, arguments(
            confirm=f"pull_request@{digest}", issue=42, no_input=True))

        self.assertEqual(0, code)

    def test_the_digest_printed_with_exit_three_is_accepted_for_every_confirmable_step(self):
        # The per-step actions come in T038-T047; this proves the round trip through the seam.
        variants = [(step_id, step_id) for step_id in CONFIRMABLE]
        variants.append(("first_sync", "first_sync without board access"))
        for step_id, label in variants:
            with self.subTest(step=label):
                lines = ["python3 scripts/sync-roadmap-issues.py --skip-project"] if (
                    "without" in label) else ["python3 scripts/sync-roadmap-issues.py"]
                target = step(step_id, details={"candidates": []} if step_id == ISSUE_STEP
                              else None)
                stub = StubAction(lines=lines, files=["a.txt"])
                with tempfile.TemporaryDirectory() as directory:
                    (Path(directory) / "a.txt").write_text("a")
                    first = make_env(root=directory, reports=[report(target)],
                                     actions={step_id: stub})
                    self.assertEqual(3, bootstrap_start.run_start(
                        first, arguments(no_input=True)))
                    shown = re.search(r"confirm: (\w+)@([0-9a-f]{12})", output(first))
                    self.assertEqual(step_id, shown.group(1))

                    second = make_env(root=directory, reports=[report(target), ALL_DONE],
                                      actions={step_id: stub})
                    code = bootstrap_start.run_start(second, arguments(
                        confirm=shown.group(0).removeprefix("confirm: "), no_input=True))

                    self.assertEqual(0, code)
                    self.assertEqual(1, len(stub.performed))

    def test_a_changed_file_between_preview_and_confirmation_exits_four(self):
        stub = StubAction(files=["a.txt"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.txt"
            path.write_text("a")
            run = FakeRun({("git", "hash-object"): lambda argv: Path(directory, argv[2]).read_text()})
            first = make_env(root=directory, run=run, reports=[report(OFFER)],
                             actions={"pull_request": stub})
            bootstrap_start.run_start(first, arguments(no_input=True))
            shown = re.search(r"confirm: pull_request@([0-9a-f]{12})", output(first)).group(1)
            path.write_text("changed")
            second = make_env(root=directory, run=run, reports=[report(OFFER)],
                              actions={"pull_request": stub})

            code = bootstrap_start.run_start(second, arguments(
                confirm=f"pull_request@{shown}", no_input=True))

            self.assertEqual(4, code)
            self.assertEqual([], stub.performed)


class NoWriteBeforeConfirmationTest(unittest.TestCase):
    """SC-003: nothing is written until a confirmation has been given."""

    def test_no_write_command_runs_before_a_confirmation(self):
        for step_id in CONFIRMABLE:
            for how in ("exit three", "declined", "wrong digest", "wrong step", "malformed"):
                with self.subTest(step=step_id, how=how):
                    run = FakeRun()
                    stub = StubAction(run=run)
                    target = step(step_id, details={"candidates": []} if step_id == ISSUE_STEP
                                  else None)
                    env = make_env(run=run, reports=[report(target)], actions={step_id: stub},
                                   is_tty=how == "declined", prompt=prompter("n"))
                    args = {
                        "exit three": arguments(no_input=True),
                        "declined": arguments(),
                        "wrong digest": arguments(
                            confirm=f"{step_id}@0123456789ab", no_input=True),
                        "wrong step": arguments(confirm="merge@0123456789ab", no_input=True),
                        "malformed": arguments(confirm=step_id, no_input=True),
                    }[how]

                    code = bootstrap_start.run_start(env, args)

                    self.assertNotEqual(0, code)
                    self.assertEqual([], stub.performed)
                    self.assertEqual([], run.writes())
                    self.assertEqual([], [call for call in run.calls if not is_read_only(call)])

    def test_the_allowlist_rejects_writes_and_api_methods(self):
        for call in (("git", "commit", "-m", "x"), ("gh", "api", "-X", "POST", "repos/a/b"),
                     ("gh", "api", "--method", "PATCH", "repos/a/b"),
                     ("gh", "api", "repos/a/b", "-f", "k=v"), ("gh", "pr", "create"),
                     ("gh", "api", "-XPOST", "repos/a/b"),
                     ("gh", "api", "--method=POST", "repos/a/b"),
                     ("gh", "api", "repos/a/b", "-fkey=v"), ("gh", "api", "repos/a/b", "-Fkey=v"),
                     ("gh", "api", "repos/a/b", "--field=key=v"),
                     ("gh", "api", "repos/a/b", "--raw-field=key=v"),
                     ("gh", "api", "repos/a/b", "--input=body.json"),
                     ("git", "stash")):
            with self.subTest(call=call):
                self.assertFalse(is_read_only(call))
        self.assertTrue(is_read_only(("gh", "api", "repos/a/b/branches/main", "--jq", ".x")))

    def test_the_action_runs_only_after_the_confirmation(self):
        run = FakeRun()
        stub = StubAction(run=run)
        env = make_env(run=run, reports=[report(OFFER), ALL_DONE],
                       actions={"pull_request": stub})
        env.default_branch = "main"
        digest = digest_of(env, OFFER)
        before = list(run.calls)

        bootstrap_start.run_start(env, arguments(confirm=f"pull_request@{digest}", no_input=True))

        self.assertEqual([], [call for call in before if call in run.writes()])
        self.assertEqual([("git", "commit", "-m", "x")], run.writes())


class ArgumentsTest(unittest.TestCase):
    def test_the_start_options_and_the_init_flags_are_parsed(self):
        args = bootstrap_start.parse_arguments([
            "--no-input", "--confirm", "pull_request@0123456789ab", "--issue", "7",
            "--name", "Widgets", "--description", "About", "--owner", "acme",
            "--required-approvals", "2", "--project-number", "3"])

        self.assertTrue(args.no_input)
        self.assertEqual("pull_request@0123456789ab", args.confirm)
        self.assertEqual(7, args.issue)
        self.assertEqual(("Widgets", "About", "acme", 2, 3), (
            args.name, args.description, args.owner, args.required_approvals,
            args.project_number))

    def test_the_defaults_are_no_confirmation_and_no_issue(self):
        args = bootstrap_start.parse_arguments([])

        self.assertEqual((False, None, None, False), (
            args.no_input, args.confirm, args.issue, args.create_board))

    def test_a_board_number_and_create_board_exclude_each_other(self):
        with self.assertRaises(ValueError):
            bootstrap_start.parse_arguments(["--project-number", "3", "--create-board"])

    def test_a_bad_issue_number_is_a_usage_error(self):
        with self.assertRaises(ValueError):
            bootstrap_start.parse_arguments(["--issue", "seven"])

    def test_main_exits_two_for_a_usage_error_and_130_when_interrupted(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(2, bootstrap_start.main(["--issue", "x"], root=Path(".")))
        self.assertIn("--issue", stderr.getvalue())

        def interrupted():
            raise KeyboardInterrupt

        env = make_env(reports=[ALL_DONE])
        env.report = interrupted
        self.assertEqual(130, bootstrap_start.run(env, ["--no-input"]))

    def test_main_turns_an_unexpected_error_into_exit_two_with_a_message(self):
        stderr = io.StringIO()
        with mock.patch.object(bootstrap_start, "run", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(stderr):
            code = bootstrap_start.main([], root=Path("."), is_tty=False)

        self.assertEqual(2, code)
        self.assertIn("boom", stderr.getvalue())

    def test_run_parses_then_starts(self):
        env = make_env(reports=[ALL_DONE])

        self.assertEqual(0, bootstrap_start.run(env, ["--no-input"]))
        self.assertEqual(2, bootstrap_start.run(env, ["--confirm", "bare"]))


class TerminalChoiceTest(unittest.TestCase):
    def test_a_terminal_asks_which_of_several_candidates_to_use(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES})
        stub = StubAction()
        env = make_env(reports=[report(issue), report(issue), ALL_DONE],
                       actions={"bootstrap_issue": stub}, is_tty=True,
                       prompt=prompter("#8", "y"))

        code = bootstrap_start.run_start(env, arguments())

        self.assertEqual(0, code)
        self.assertEqual([CANDIDATES[1]], stub.performed)

    def test_a_candidate_that_gains_a_branch_while_the_prompt_waits_is_not_performed(self):
        before = step("bootstrap_issue", details={"candidates": CANDIDATES})
        grown = [CANDIDATES[0], {**CANDIDATES[1], "branch": "8-chore-bootstrap"}]
        after = step("bootstrap_issue", details={"candidates": grown})
        stub = StubAction()
        env = make_env(reports=[report(before), report(after)],
                       actions={"bootstrap_issue": stub}, is_tty=True, prompt=prompter("8", "y"))

        code = bootstrap_start.run_start(env, arguments())

        self.assertEqual(4, code)
        self.assertEqual([], stub.performed)
        self.assertIn("preview changed", problems(env))

    def test_a_candidate_that_disappears_while_the_prompt_waits_is_not_performed(self):
        before = step("bootstrap_issue", details={"candidates": CANDIDATES})
        after = step("bootstrap_issue", details={"candidates": CANDIDATES[:1]})
        stub = StubAction()
        env = make_env(reports=[report(before), report(after)],
                       actions={"bootstrap_issue": stub}, is_tty=True, prompt=prompter("8", "y"))

        self.assertEqual(4, bootstrap_start.run_start(env, arguments()))
        self.assertEqual([], stub.performed)

    def test_a_candidate_that_appears_while_a_new_issue_is_confirmed_is_not_performed(self):
        before = step("bootstrap_issue", details={"candidates": []})
        after = step("bootstrap_issue", details={"candidates": CANDIDATES[:1]})
        stub = StubAction()
        env = make_env(reports=[report(before), report(after)],
                       actions={"bootstrap_issue": stub}, is_tty=True, prompt=prompter("y"))

        code = bootstrap_start.run_start(env, arguments())

        self.assertEqual(4, code)
        self.assertEqual([], stub.performed)

    def test_an_answer_that_is_not_a_candidate_changes_nothing(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES})
        stub = StubAction()
        env = make_env(reports=[report(issue)], actions={"bootstrap_issue": stub},
                       is_tty=True, prompt=prompter("99"))

        code = bootstrap_start.run_start(env, arguments())

        self.assertEqual(1, code)
        self.assertEqual([], stub.performed)


class BootstrapIssueActionTest(unittest.TestCase):
    """The real `bootstrap_issue` action (research D3), over a fake git and gh."""

    ISSUE_URL = "https://github.com/acme/widgets/issues/12\n"

    def run_for(self, local=("main",), status="", extra=None):
        answers = {
            ("git", "status", "--porcelain"): status,
            ("git", "branch", "--format=%(refname:short)"): "\n".join(local) + "\n",
            ("gh", "issue", "create"): self.ISSUE_URL,
            ("gh", "issue", "develop"): "",
            ("git", "fetch"): "",
            ("git", "checkout"): "",
        }
        answers.update(extra or {})
        return FakeRun(answers)

    def env(self, candidates, run, reports=None, **kwargs):
        target = step("bootstrap_issue", details={"candidates": candidates})
        return make_env(
            run=run, reports=reports or [report(target), ALL_DONE],
            actions={ISSUE_STEP: bootstrap_start.ACTIONS[ISSUE_STEP]}, **kwargs), target

    def preview_then_confirm(self, candidates, run, issue=None):
        """The exit-3 run, then `--confirm` with the digest it printed; the confirm run."""
        first, _ = self.env(candidates, run)
        self.assertEqual(3, bootstrap_start.run_start(first, arguments(no_input=True,
                                                                       issue=issue)))
        shown = re.search(r"confirm: bootstrap_issue@([0-9a-f]{12})", output(first)).group(1)
        before = len(run.calls)
        second, _ = self.env(candidates, run)
        code = bootstrap_start.run_start(second, arguments(
            confirm=f"bootstrap_issue@{shown}", issue=issue, no_input=True))
        return code, second, first, run.calls[before:]

    def writes(self, calls):
        return [call for call in calls if call[:2] in (
            ("gh", "issue"), ("git", "checkout"), ("git", "fetch"), ("git", "stash"))]

    def test_the_action_is_registered(self):
        self.assertIsInstance(bootstrap_start.ACTIONS[ISSUE_STEP], bootstrap_start.StepAction)

    def test_no_candidate_creates_a_neutral_issue_then_develops_its_branch(self):
        run = self.run_for()

        code, env, first, calls = self.preview_then_confirm([], run)

        self.assertEqual(0, code)
        created, developed = self.writes(calls)
        self.assertEqual(("gh", "issue", "create"), created[:3])
        title = created[created.index("--title") + 1]
        body = created[created.index("--body") + 1]
        self.assertEqual("CHORE: bootstrap the project", title)  # the title docs/bootstrap.md uses
        self.assertIn(bootstrap_steps.BOOTSTRAP_MARKER, body)
        for text in (title, body):  # neutral: nothing names the repository or its people
            for word in ("acme", "widgets"):
                self.assertNotIn(word, text.lower())
        self.assertEqual(("gh", "issue", "develop", "12", "--base", "main", "--name",
                          "12-chore-bootstrap", "--checkout", "--repo", "acme/widgets"),
                         developed)

    def test_the_issue_is_written_to_the_repository_that_detection_reads(self):
        run = self.run_for()

        code, env, _, calls = self.preview_then_confirm([], run)

        detected = bootstrap_github.repository_name(run, env.root, env.remote_default)
        self.assertEqual(0, code)
        created, developed = self.writes(calls)
        for call in (created, developed):
            self.assertEqual(detected, call[call.index("--repo") + 1])

    def test_a_failed_develop_after_create_exits_two_and_never_creates_a_second_issue(self):
        run = self.run_for(extra={("gh", "issue", "develop"): bootstrap_github.CommandError(
            "boom", returncode=1)})

        code, env, _, calls = self.preview_then_confirm([], run)

        self.assertEqual(2, code)
        self.assertIn("bootstrap_issue failed: boom", problems(env))
        self.assertEqual(1, len([c for c in calls if c[:3] == ("gh", "issue", "create")]))

    def test_carried_over_files_that_change_after_the_preview_exit_four_with_no_writes(self):
        candidate = {"number": 7, "title": "Bootstrap", "branch": "7-chore-bootstrap"}
        run = self.run_for(local=("main", "7-chore-bootstrap"), status=" M ROADMAP.md\0")
        first, _ = self.env([candidate], run)
        bootstrap_start.run_start(first, arguments(no_input=True))
        shown = re.search(r"confirm: bootstrap_issue@([0-9a-f]{12})", output(first)).group(1)
        self.assertIn("ROADMAP.md", output(first))
        run.answers[("git", "status", "--porcelain")] = " M ROADMAP.md\0 M project-context.toml\0"
        before = len(run.calls)
        second, _ = self.env([candidate], run)

        code = bootstrap_start.run_start(second, arguments(
            confirm=f"bootstrap_issue@{shown}", no_input=True))

        self.assertEqual(4, code)
        self.assertEqual([], self.writes(run.calls[before:]))

    def test_the_preview_names_the_fetch_only_for_a_branch_that_is_not_local(self):
        candidate = {"number": 7, "title": "Bootstrap", "branch": "7-chore-bootstrap"}
        remote_only = self.run_for(local=("main",))
        local = self.run_for(local=("main", "7-chore-bootstrap"))
        first, _ = self.env([candidate], remote_only)
        second, _ = self.env([candidate], local)

        bootstrap_start.run_start(first, arguments(no_input=True))
        bootstrap_start.run_start(second, arguments(no_input=True))

        self.assertIn("git fetch origin 7-chore-bootstrap", output(first))
        self.assertNotIn("git fetch", output(second))

    def test_an_issue_without_a_branch_is_only_developed(self):
        run = self.run_for()
        candidate = {"number": 7, "title": "Bootstrap", "branch": None}

        code, _, _, calls = self.preview_then_confirm([candidate], run)

        self.assertEqual(0, code)
        self.assertEqual([("gh", "issue", "develop", "7", "--base", "main", "--name",
                           "7-chore-bootstrap", "--checkout", "--repo", "acme/widgets")],
                         self.writes(calls))

    def test_an_existing_local_branch_is_checked_out_without_a_fetch(self):
        run = self.run_for(local=("main", "7-chore-bootstrap"))
        candidate = {"number": 7, "title": "Bootstrap", "branch": "7-chore-bootstrap"}

        code, _, _, calls = self.preview_then_confirm([candidate], run)

        self.assertEqual(0, code)
        self.assertEqual([("git", "checkout", "7-chore-bootstrap")], self.writes(calls))

    def test_a_branch_only_on_github_is_fetched_then_checked_out(self):
        run = self.run_for(local=("main",))
        candidate = {"number": 7, "title": "Bootstrap", "branch": "7-chore-bootstrap"}

        code, _, _, calls = self.preview_then_confirm([candidate], run)

        self.assertEqual(0, code)
        self.assertEqual([("git", "fetch", "origin", "7-chore-bootstrap"),
                          ("git", "checkout", "7-chore-bootstrap")], self.writes(calls))

    def test_the_chosen_candidate_of_several_is_the_one_acted_on(self):
        run = self.run_for(local=("main", "7-chore-bootstrap"))

        code, _, _, calls = self.preview_then_confirm(CANDIDATES, run, issue=8)

        self.assertEqual(0, code)
        self.assertEqual([("gh", "issue", "develop", "8", "--base", "main", "--name",
                           "8-chore-bootstrap", "--checkout", "--repo", "acme/widgets")],
                         self.writes(calls))

    def test_the_preview_warns_about_carried_over_files_only_when_there_are_some(self):
        candidate = {"number": 7, "title": "Bootstrap", "branch": "7-chore-bootstrap"}
        dirty = self.run_for(local=("main", "7-chore-bootstrap"),
                             status=" M ROADMAP.md\0?? project-context.toml\0")
        clean = self.run_for(local=("main", "7-chore-bootstrap"))
        first, _ = self.env([candidate], dirty)
        second, _ = self.env([candidate], clean)

        bootstrap_start.run_start(first, arguments(no_input=True))
        bootstrap_start.run_start(second, arguments(no_input=True))

        self.assertIn("ROADMAP.md", output(first))
        self.assertIn("project-context.toml", output(first))
        self.assertIn("already commits", output(first))
        self.assertIn("never stashes", output(first))
        self.assertNotIn("carried over", output(second).lower())

    def test_the_preview_says_what_will_be_done(self):
        for candidates, words in (
                ([], ("create", "CHORE:", "bootstrap marker", "develop")),
                ([{"number": 7, "title": "T", "branch": None}], ("develop", "7-chore-bootstrap")),
                ([{"number": 7, "title": "T", "branch": "7-chore-bootstrap"}], ("check out",))):
            with self.subTest(candidates=len(candidates), words=words):
                env, _ = self.env(candidates, self.run_for(local=("main", "7-chore-bootstrap")))

                bootstrap_start.run_start(env, arguments(no_input=True))

                for word in words:
                    self.assertIn(word.lower(), output(env).lower())

    def test_a_failed_checkout_exits_two_with_gits_message_and_never_stashes(self):
        message = "error: Your local changes would be overwritten by checkout"
        run = self.run_for(local=("main", "7-chore-bootstrap"), extra={
            ("git", "checkout"): bootstrap_github.CommandError(message, returncode=1)})
        candidate = {"number": 7, "title": "Bootstrap", "branch": "7-chore-bootstrap"}

        code, env, _, calls = self.preview_then_confirm([candidate], run)

        self.assertEqual(2, code)
        self.assertIn(f"bootstrap_issue failed: {message}", problems(env))
        self.assertEqual([], [call for call in calls if call[:2] == ("git", "stash")])

    def test_a_failed_fetch_exits_two_and_does_not_check_out(self):
        run = self.run_for(local=("main",), extra={
            ("git", "fetch"): bootstrap_github.CommandError("fatal: no such ref", returncode=1)})
        candidate = {"number": 7, "title": "Bootstrap", "branch": "7-chore-bootstrap"}

        code, env, _, calls = self.preview_then_confirm([candidate], run)

        self.assertEqual(2, code)
        self.assertIn("bootstrap_issue failed: fatal: no such ref", problems(env))
        self.assertEqual([], [call for call in calls if call[:2] == ("git", "checkout")])

    def test_an_issue_url_without_a_number_stops_before_developing(self):
        run = self.run_for(extra={("gh", "issue", "create"): "created\n"})

        code, env, _, calls = self.preview_then_confirm([], run)

        self.assertEqual(2, code)
        self.assertIn("issue number", problems(env))
        self.assertIn("bootstrap_issue failed:", problems(env))
        self.assertIn("probably created", problems(env))
        self.assertIn("finds it by its marker", problems(env))
        self.assertEqual([], [call for call in calls if call[:3] == ("gh", "issue", "develop")])

    def test_nothing_is_written_before_a_confirmation(self):
        candidate = {"number": 7, "title": "Bootstrap", "branch": "7-chore-bootstrap"}
        for candidates in ([], [candidate]):
            for how in ("exit three", "declined", "wrong digest", "wrong step", "malformed"):
                with self.subTest(candidates=len(candidates), how=how):
                    run = self.run_for(status=" M ROADMAP.md\0")
                    env, _ = self.env(candidates, run, is_tty=how == "declined",
                                      prompt=prompter("n"))
                    args = {
                        "exit three": arguments(no_input=True),
                        "declined": arguments(),
                        "wrong digest": arguments(
                            confirm="bootstrap_issue@0123456789ab", no_input=True),
                        "wrong step": arguments(confirm="merge@0123456789ab", no_input=True),
                        "malformed": arguments(confirm="bootstrap_issue", no_input=True),
                    }[how]

                    self.assertNotEqual(0, bootstrap_start.run_start(env, args))

                    self.assertEqual([], [call for call in run.calls if not is_read_only(call)])


PROJECT_CONTEXT = "project_context"
WARNING = "this plan has no rulesets for private repositories"
FLAGS = dict(description="A project", required_approvals=1, project_number=3)


class ProjectContextActionTest(unittest.TestCase):
    """The real `project_context` action (research D13, D14), over a temporary repository."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.target = self.root / "project-context.toml"

    def run_for(self, extra=None):
        answers = {
            ("git", "remote", "get-url"): "https://github.com/acme/widgets.git\n",
            ("gh", "project", "create"): '{"number": 5}',
        }
        answers.update(extra or {})
        return FakeRun(answers)

    def env(self, run=None, state="todo", performable=True, warning=WARNING, **kwargs):
        target = step(PROJECT_CONTEXT, state, performable, details={"ruleset_warning": warning},
                      action={"text": "Fix the file.", "command": None, "doc": "docs/x"})
        # a terminal run reads the report once more, after the yes
        reports = ([report(target), report(target), ALL_DONE] if kwargs.get("is_tty")
                   else [report(target), ALL_DONE])
        return make_env(
            root=self.root, run=run or self.run_for(), reports=reports,
            actions={PROJECT_CONTEXT: bootstrap_start.ACTIONS[PROJECT_CONTEXT]}, **kwargs)

    def start(self, args, **kwargs):
        env = self.env(**kwargs)
        return bootstrap_start.run_start(env, args), env

    def preview_then_confirm(self, flags, run=None):
        run = run or self.run_for()
        code, first = self.start(arguments(no_input=True, **flags), run=run)
        self.assertEqual(3, code)
        shown = re.search(r"confirm: project_context@([0-9a-f]{12})", output(first)).group(1)
        code, second = self.start(arguments(
            confirm=f"project_context@{shown}", no_input=True, **flags), run=run)
        return code, second, first, run

    def test_the_action_is_registered(self):
        self.assertIsInstance(bootstrap_start.ACTIONS[PROJECT_CONTEXT],
                              bootstrap_start.StepAction)

    def test_missing_flags_without_a_terminal_exit_two_naming_every_one(self):
        names = {"description": "--description", "required_approvals": "--required-approvals",
                 "project_number": "--project-number"}
        for size in range(len(FLAGS)):  # every proper subset of the three flags
            for given in itertools.combinations(FLAGS, size):
                values = {key: FLAGS[key] for key in given}
                with self.subTest(given=given):
                    run = self.run_for()
                    code, env = self.start(arguments(no_input=True, **values), run=run)

                    self.assertEqual(2, code)
                    for key, name in names.items():
                        self.assertEqual(key not in values, name in problems(env))
                    if "project_number" not in values:
                        self.assertIn("--create-board", problems(env))
                    self.assertNotIn("confirm:", output(env))
                    self.assertFalse(self.target.exists())
                    self.assertEqual([], [c for c in run.calls if not is_read_only(c)])

    def test_a_terminal_with_no_input_never_prompts_and_exits_two_for_missing_flags(self):
        code, env = self.start(arguments(no_input=True), is_tty=True)  # a prompt would raise

        self.assertEqual(2, code)
        self.assertIn("--description", problems(env))
        self.assertFalse(self.target.exists())

    def test_the_preview_shows_every_value_and_the_ruleset_warning_and_writes_nothing(self):
        code, env = self.start(arguments(no_input=True, **FLAGS))

        self.assertEqual(3, code)
        text = output(env)
        for value in ("A project", "widgets", "acme", "required_approvals = 1", "#3", WARNING):
            self.assertIn(value, text)
        self.assertFalse(self.target.exists())

    def test_an_owner_that_cannot_be_derived_is_a_missing_flag_without_a_terminal(self):
        run = self.run_for()

        code, env = self.start(arguments(no_input=True, **FLAGS), run=run,
                               remote_default=lambda: None)

        self.assertEqual(2, code)
        self.assertIn("--owner", problems(env))
        self.assertNotIn("confirm:", output(env))
        self.assertFalse(self.target.exists())
        self.assertEqual([], [c for c in run.calls if not is_read_only(c)])

    def test_a_terminal_asks_for_an_owner_it_cannot_derive_and_names_the_project_after_the_folder(
            self):
        run = self.run_for({("git", "remote", "get-url"): bootstrap_github.CommandError(
            "no remote", returncode=2)})
        prompt = prompter("A project", "acme", "3", "1", "y")

        code, _ = self.start(arguments(), run=run, is_tty=True, prompt=prompt,
                             remote_default=lambda: None)

        self.assertEqual(0, code)
        self.assertIn("owner", prompt.asked[1].lower())
        context = project_context.load_context(self.target)
        self.assertEqual("acme", context["github_project"]["owner"])
        self.assertEqual(self.root.name, context["project"]["name"])

    def test_a_board_to_create_is_named_in_the_preview(self):
        code, env = self.start(arguments(no_input=True, description="A", required_approvals=0,
                                         create_board=True))

        self.assertEqual(3, code)
        self.assertIn("create", output(env).lower())
        self.assertIn("new board", output(env).lower())

    def test_without_a_warning_the_preview_has_none(self):
        _, env = self.start(arguments(no_input=True, **FLAGS), warning=None)

        self.assertNotIn("warning", output(env).lower())

    def test_the_digest_from_exit_three_is_accepted_and_the_file_is_written(self):
        code, env, _, run = self.preview_then_confirm(FLAGS)

        self.assertEqual(0, code)
        context = project_context.load_context(self.target)
        self.assertEqual("A project", context["project"]["description"])
        self.assertEqual(1, context["review"]["required_approvals"])
        self.assertEqual(3, context["github_project"]["number"])
        self.assertEqual("acme", context["github_project"]["owner"])

    def test_a_created_board_number_is_written(self):
        flags = dict(description="A", required_approvals=0, create_board=True)

        code, _, _, run = self.preview_then_confirm(flags)

        self.assertEqual(0, code)
        self.assertEqual(5, project_context.load_context(self.target)["github_project"]["number"])
        self.assertEqual(1, len([c for c in run.calls if c[:3] == ("gh", "project", "create")]))

    def test_changed_flag_values_between_preview_and_confirmation_exit_four(self):
        run = self.run_for()
        _, first = self.start(arguments(no_input=True, **FLAGS), run=run)
        shown = re.search(r"confirm: project_context@([0-9a-f]{12})", output(first)).group(1)
        changed = dict(FLAGS, required_approvals=2)

        code, _ = self.start(arguments(confirm=f"project_context@{shown}", no_input=True,
                                       **changed), run=run)

        self.assertEqual(4, code)
        self.assertFalse(self.target.exists())

    def test_the_action_never_passes_force_and_refuses_an_existing_file(self):
        self.target.write_text("[project]\n")
        env = self.env()
        env.args = arguments(no_input=True, **FLAGS)

        with self.assertRaises(bootstrap_start.StepFailed) as raised:
            bootstrap_start.ACTIONS[PROJECT_CONTEXT].perform(env, step(PROJECT_CONTEXT))

        self.assertIn("already exists", str(raised.exception))
        self.assertEqual("[project]\n", self.target.read_text())

    def test_init_is_the_injected_one_and_is_called_without_force(self):
        calls = []
        env = self.env(init=lambda *args: calls.append(args))
        env.args = arguments(no_input=True, **FLAGS)

        bootstrap_start.ACTIONS[PROJECT_CONTEXT].perform(env, step(PROJECT_CONTEXT))

        (namespace, root, run, tty, _), = calls
        self.assertFalse(namespace.force)
        self.assertTrue(namespace.yes)
        self.assertEqual((self.root, env.run, False), (root, run, tty))

    def test_only_the_wrote_line_of_inits_output_is_echoed(self):
        def noisy(args, root, run, is_tty, prompt):
            print("Debug noise")
            print(f"Wrote {root / 'project-context.toml'}")
            print()
            print("Next steps:\n  1. something else")

        env = self.env(init=noisy)
        env.args = arguments(no_input=True, **FLAGS)

        bootstrap_start.ACTIONS[PROJECT_CONTEXT].perform(env, step(PROJECT_CONTEXT))

        self.assertEqual(f"Wrote {self.target}\n", output(env))

    def test_perform_never_lets_init_fill_in_a_decision_value(self):
        calls = []
        cases = (
            ({}, None, ("--description", "--required-approvals", "--project-number")),
            (dict(description="A", required_approvals=1), None, ("--project-number",)),
            (dict(description="A", project_number=3), None, ("--required-approvals",)),
            (dict(required_approvals=1, project_number=3), None, ("--description",)),
            (dict(FLAGS), (lambda: None), ("--owner",)),
        )
        for flags, remote, expected in cases:
            with self.subTest(flags=sorted(flags), remote=remote is None):
                kwargs = {} if remote is None else {"remote_default": remote}
                env = self.env(init=lambda *args: calls.append(args), **kwargs)
                env.args = arguments(no_input=True, **flags)

                with self.assertRaises(bootstrap_start.StepFailed) as raised:
                    bootstrap_start.ACTIONS[PROJECT_CONTEXT].perform(env, step(PROJECT_CONTEXT))

                self.assertEqual(2, raised.exception.code)
                self.assertIn("project_context failed:", str(raised.exception))
                for flag in expected:
                    self.assertIn(flag, str(raised.exception))
        self.assertEqual([], calls)

    def test_a_missing_flag_is_reported_before_an_invalid_value(self):
        for args in (arguments(no_input=True, required_approvals=-1, project_number=3),
                     arguments(confirm="project_context@0123456789ab", no_input=True,
                               required_approvals=-1, project_number=3)):
            with self.subTest(confirm=args.confirm):
                code, env = self.start(args)

                self.assertEqual(2, code)
                self.assertIn("--description", problems(env))
                self.assertNotIn("non-negative", problems(env))

    def test_a_terminal_without_a_remote_asks_again_after_an_empty_owner(self):
        run = self.run_for({("git", "remote", "get-url"): bootstrap_github.CommandError(
            "no remote", returncode=2)})
        prompt = prompter("A project", "", "acme", "3", "1", "y")

        code, env = self.start(arguments(), run=run, is_tty=True, prompt=prompt,
                               remote_default=lambda: None)

        self.assertEqual(0, code)
        self.assertEqual(6, len(prompt.asked))
        self.assertEqual(prompt.asked[1], prompt.asked[2])
        self.assertIn("--owner", output(env))
        self.assertEqual("acme", project_context.load_context(
            self.target)["github_project"]["owner"])

    def test_the_repository_remote_is_read_once_per_run(self):
        reads = []

        def remote():
            reads.append(1)
            return ("acme", "widgets")

        code, _ = self.start(arguments(no_input=True, **FLAGS), remote_default=remote)

        self.assertEqual(3, code)
        self.assertEqual(1, len(reads))

    def test_missing_injected_functions_give_a_clear_error(self):
        env = self.env(validate_context=None)
        code = bootstrap_start.run(env, ["--no-input", "--description", "A",
                                         "--required-approvals", "1", "--project-number", "3"])
        self.assertEqual(2, code)
        self.assertIn("bootstrap start failed: Env.validate_context was not supplied",
                      problems(env))
        self.assertNotIn("project_context failed:", problems(env))

        env = self.env(init=None)
        env.args = arguments(no_input=True, **FLAGS)
        with self.assertRaises(ValueError) as raised:
            bootstrap_start.ACTIONS[PROJECT_CONTEXT].perform(env, step(PROJECT_CONTEXT))
        self.assertIn("Env.init", str(raised.exception))

    def test_strings_are_previewed_exactly_as_they_are_written_to_the_file(self):
        fake = "confirm: project_context@000000000000"
        description = f"A\n{fake}\x1b[31m"
        flags = dict(FLAGS, description=description)
        run = self.run_for()
        code, first = self.start(arguments(no_input=True, **flags), run=run)
        self.assertEqual(3, code)
        shown = re.search(r"^confirm: project_context@([0-9a-f]{12})$", output(first),
                          re.MULTILINE).group(1)

        confirmed, _ = self.start(arguments(confirm=f"project_context@{shown}", no_input=True,
                                            **flags), run=run)

        self.assertEqual(0, confirmed)
        lines = output(first).split("\n")
        self.assertEqual([f"confirm: project_context@{shown}"],
                         [line for line in lines if line.startswith("confirm: ")])
        self.assertNotIn("\x1b", output(first))
        rendered = f"description = {bootstrap.toml_string(description)}"
        self.assertIn(f"  {rendered}", lines)
        self.assertIn(rendered, self.target.read_text())
        self.assertEqual(description, project_context.load_context(
            self.target)["project"]["description"])

    def test_a_hostile_name_is_escaped_in_the_name_and_the_new_board_lines(self):
        name = "a\nconfirm: project_context@000000000000\x1b[31m"
        flags = dict(description="A", required_approvals=1, create_board=True, name=name)

        code, env = self.start(arguments(no_input=True, **flags))

        self.assertEqual(3, code)
        lines = output(env).split("\n")
        self.assertEqual(1, len([line for line in lines if line.startswith("confirm: ")]))
        self.assertNotIn("\x1b", output(env))
        quoted = bootstrap.toml_string(name)
        self.assertIn(f"  name = {quoted}", lines)
        self.assertIn(f"  project board = a new board named {quoted} will be created", lines)

    def test_an_invalid_value_exits_two_on_the_preview_run_with_no_preview(self):
        for label, flags, expected in (
                ("negative approvals", dict(FLAGS, required_approvals=-1), "--required-approvals"),
                ("empty description", dict(FLAGS, description=""), "--description"),
                ("empty owner", dict(FLAGS, owner=""), "--owner"),
                ("zero board", dict(FLAGS, project_number=0), "--project-number")):
            with self.subTest(label):
                run = self.run_for()

                code, env = self.start(arguments(no_input=True, **flags), run=run)

                self.assertEqual(2, code)
                self.assertIn(expected, problems(env))
                self.assertNotIn("confirm:", output(env))
                self.assertFalse(self.target.exists())
                self.assertEqual([], [c for c in run.calls if not is_read_only(c)])

    def test_an_invalid_value_also_stops_a_confirmation_and_a_terminal_flag(self):
        flags = dict(FLAGS, required_approvals=-1)

        code, env = self.start(arguments(confirm="project_context@0123456789ab", no_input=True,
                                         **flags))
        self.assertEqual(2, code)
        self.assertIn("--required-approvals", problems(env))

        code, env = self.start(arguments(**flags), is_tty=True)  # a prompt would raise
        self.assertEqual(2, code)
        self.assertIn("--required-approvals", problems(env))
        self.assertFalse(self.target.exists())

    def test_a_terminal_asks_for_the_missing_values_and_offers_zero_approvals(self):
        prompt = prompter("A project", "", "", "y")

        code, env = self.start(arguments(), is_tty=True, prompt=prompt)

        self.assertEqual(0, code)
        self.assertEqual(4, len(prompt.asked))
        self.assertIn("approvals", prompt.asked[2].lower())
        self.assertIn("[0]", prompt.asked[2])
        self.assertEqual("Proceed? [y/N] ", prompt.asked[3])
        context = project_context.load_context(self.target)
        self.assertEqual(0, context["review"]["required_approvals"])
        self.assertEqual(5, context["github_project"]["number"])  # empty answer: create a board

    def test_a_terminal_preview_shows_the_answers_before_the_yes(self):
        prompt = prompter("A project", "7", "2", "n")

        code, env = self.start(arguments(), is_tty=True, prompt=prompt)

        self.assertEqual(1, code)
        self.assertIn("A project", output(env))
        self.assertIn("required_approvals = 2", output(env))
        self.assertIn("#7", output(env))
        self.assertFalse(self.target.exists())

    def test_a_terminal_does_not_ask_for_values_given_as_flags(self):
        prompt = prompter("1", "y")

        code, _ = self.start(arguments(description="A", project_number=3), is_tty=True,
                             prompt=prompt)

        self.assertEqual(0, code)
        self.assertEqual(2, len(prompt.asked))
        self.assertIn("approvals", prompt.asked[0].lower())

    def test_a_terminal_asks_again_after_an_empty_or_invalid_answer(self):
        # description: empty, then valid; board: "x", "0", then 3; approvals: "many", "-1", then 2
        prompt = prompter("", "A project", "x", "0", "3", "many", "-1", "2", "y")

        code, env = self.start(arguments(), is_tty=True, prompt=prompt)

        self.assertEqual(0, code)
        self.assertEqual(9, len(prompt.asked))
        self.assertEqual(prompt.asked[0], prompt.asked[1])  # the same question again
        self.assertIn("whole number", output(env))
        self.assertIn("--required-approvals", output(env))
        context = project_context.load_context(self.target)
        self.assertEqual((2, 3, "A project"), (
            context["review"]["required_approvals"], context["github_project"]["number"],
            context["project"]["description"]))

    def test_an_empty_approvals_answer_still_accepts_the_offered_zero(self):
        code, env = self.start(arguments(description="A", project_number=3), is_tty=True,
                               prompt=prompter("", "y"))

        self.assertEqual(0, code)
        self.assertIn("required_approvals = 0", output(env))  # shown, not left to init's default
        self.assertEqual(0, project_context.load_context(
            self.target)["review"]["required_approvals"])

    def test_end_of_input_at_a_prompt_exits_two_with_the_flag_to_pass(self):
        def closed(text):
            raise EOFError

        code, env = self.start(arguments(), is_tty=True, prompt=closed)

        self.assertEqual(2, code)
        self.assertIn("project_context failed: input ended: pass --description", problems(env))
        self.assertFalse(self.target.exists())

    def test_an_invalid_existing_file_stops_with_exit_one_before_any_missing_flag(self):
        for args in (arguments(no_input=True), arguments(confirm="project_context@0123456789ab",
                                                        no_input=True)):
            with self.subTest(confirm=args.confirm):
                code, env = self.start(args, performable=False)

                self.assertEqual(1, code)
                self.assertIn("Fix the file.", output(env))
                self.assertEqual("", problems(env))
                self.assertFalse(self.target.exists())

    def test_nothing_is_written_before_a_confirmation(self):
        for how in ("exit three", "declined", "wrong digest", "wrong step", "malformed",
                    "missing flags"):
            with self.subTest(how=how):
                run = self.run_for()
                args = {
                    "exit three": arguments(no_input=True, **FLAGS),
                    "declined": arguments(**FLAGS),
                    "wrong digest": arguments(confirm="project_context@0123456789ab",
                                              no_input=True, **FLAGS),
                    "wrong step": arguments(confirm="merge@0123456789ab", no_input=True, **FLAGS),
                    "malformed": arguments(confirm="project_context", no_input=True, **FLAGS),
                    "missing flags": arguments(no_input=True),
                }[how]

                code, _ = self.start(args, run=run, is_tty=how == "declined",
                                     prompt=prompter("n"))

                self.assertNotEqual(0, code)
                self.assertFalse(self.target.exists())
                self.assertEqual([], [c for c in run.calls if not is_read_only(c)])


GITHUB_SETTINGS = "github_settings"
DENIED = "gh: Resource not accessible by personal access token (HTTP 403)"
PLAN_DENIED = ("gh: Upgrade to GitHub Pro or make this repository public to enable this "
               "feature. (HTTP 403)")


class SettingsRun:
    """git answers and the branch tip for the digest; every other call goes to FakeGitHub.

    `deny` makes the ruleset write fail with that text and counts each attempt; `default_context`
    is the default branch's project-context.toml (None: not there); `fail` maps an argv prefix to
    the error text a read of it raises.
    """

    def __init__(self, github, deny=None, default_context=None, fail=None):
        self.github, self.deny, self.default_context = github, deny, default_context
        self.fail = fail or {}
        self.calls, self.ruleset_attempts = [], 0

    def __call__(self, argv, cwd=None):
        argv = list(argv)
        self.calls.append(tuple(argv))
        for prefix, text in self.fail.items():
            if tuple(argv[:len(prefix)]) == prefix:
                raise bootstrap_github.CommandError(text)
        if argv[:2] == ["gh", "api"] and any("/contents/" in part for part in argv):
            if self.default_context is None:
                raise bootstrap_github.CommandError("gh: Not Found (HTTP 404)")
            return self.default_context
        if argv[0] == "git":
            return "head000\n" if argv[1:3] == ["rev-parse", "HEAD"] else "main\n"
        if argv[:2] == ["gh", "api"] and "/branches/" in argv[2]:
            return "tip000\n"
        if argv[:2] == ["gh", "api"] and argv[2:4] == ["-X", "POST"] and "/rulesets" in argv[4]:
            self.ruleset_attempts += 1
            if self.deny:
                raise bootstrap_github.CommandError(self.deny)
        return self.github(argv, cwd=cwd)


class GitHubSettingsActionTest(unittest.TestCase):
    """The real `github_settings` action (T042, T043): the `github` plan, then `github --apply`."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "project-context.toml").write_text(
            test_bootstrap_github.CONTEXT.format(approvals=1))
        self.github = test_bootstrap_github.FakeGitHub()
        self.run = SettingsRun(self.github)

    def start(self, args, run=None, reports=None, merged=False, **kwargs):
        target = step(GITHUB_SETTINGS, action={
            "text": "Preview the changes, then apply them.",
            "command": "python3 scripts/bootstrap.py github", "doc": "docs/x"})
        reports = reports or [report(target), ALL_DONE]
        if merged is not None:  # None: the reports already say whether bootstrap is merged
            reports = [{**each, "bootstrap_merged": merged} for each in reports]
        env = make_env(
            root=self.root, run=run or self.run, reports=reports,
            actions={GITHUB_SETTINGS: bootstrap_start.ACTIONS[GITHUB_SETTINGS]}, **kwargs)
        return bootstrap_start.run_start(env, args), env

    def preview(self, **kwargs):
        code, env = self.start(arguments(no_input=True), **kwargs)
        self.assertEqual(3, code)
        digest = re.search(r"confirm: github_settings@([0-9a-f]{12})", output(env)).group(1)
        return digest, env

    def confirm(self, digest, **kwargs):
        return self.start(arguments(confirm=f"github_settings@{digest}", no_input=True), **kwargs)

    def github_output(self):
        """What `bootstrap.py github` prints for the same state, and nothing else."""
        text = io.StringIO()
        with contextlib.redirect_stdout(text):
            bootstrap.main(["github"], root=self.root, run=self.github, is_tty=False)
        return text.getvalue()

    def test_the_action_is_registered(self):
        self.assertIsInstance(bootstrap_start.ACTIONS[GITHUB_SETTINGS],
                              bootstrap_start.StepAction)
        self.assertIsNot(bootstrap_start.ACTIONS[GITHUB_SETTINGS], bootstrap_start._DEFAULT_ACTION)

    def test_the_preview_is_the_github_plan_without_its_apply_hint(self):
        plan = self.github_output()
        self.assertIn("Labels: create", plan)

        _, env = self.preview()

        wanted = plan.split("\nPreview only;")[0].rstrip("\n")
        self.assertIn(wanted, output(env))
        self.assertNotIn("Preview only", output(env))
        self.assertIn("Action: Preview the changes, then apply them.", output(env))

    def test_the_preview_has_no_apply_hint_and_ends_with_no_blank_line(self):
        env = make_env(root=self.root, run=self.run)
        action = bootstrap_start.ACTIONS[GITHUB_SETTINGS]

        lines = action.preview(env, step(GITHUB_SETTINGS))

        self.assertNotEqual("", lines[-1])
        self.assertFalse(any("run with --apply" in line for line in lines))
        self.assertTrue(any(line.startswith("Labels: create") for line in lines))

    def test_the_github_subcommand_still_prints_its_hint_by_default(self):
        self.assertIn("Preview only; run with --apply", self.github_output())

    def test_a_real_digest_is_accepted_when_nothing_changed(self):
        digest, _ = self.preview()

        code, env = self.confirm(digest)

        self.assertEqual(0, code)
        lines = output(env).splitlines()  # a confirm run prints no preview: only the action
        self.assertEqual(2, len(lines), lines)
        self.assertRegex(lines[0], r"^Applied \d+ change\(s\)\.$")
        self.assertEqual("Bootstrap is complete.", lines[1])
        self.assertEqual([bootstrap_github.RULESET_NAME],
                         [ruleset["name"] for ruleset in self.github.rulesets])
        self.assertIsNotNone(self.github.find_label(bootstrap_github.LABELS[0][0]))

    def test_a_plan_that_changed_since_the_preview_exits_four_and_applies_nothing(self):
        digest, _ = self.preview()
        name, color, description = bootstrap_github.LABELS[0]
        self.github.labels[name] = (color, description)  # one label line disappears from the plan

        code, env = self.confirm(digest)

        self.assertEqual(4, code)
        self.assertIn("changed", problems(env))
        self.assertEqual([], self.github.writes)

    def test_the_confirmed_action_is_apply_and_never_allow_weaken(self):
        digest, _ = self.preview()
        real = bootstrap_github.run_github

        with mock.patch.object(bootstrap_github, "run_github", wraps=real) as spy:
            code, _ = self.confirm(digest)

        self.assertEqual(0, code)
        modes = [(c.args[0].check, c.args[0].apply, c.args[0].allow_weaken)
                 for c in spy.call_args_list]
        self.assertEqual(1, modes.count((False, True, False)))
        self.assertEqual(modes[-1], (False, True, False))  # the apply comes last
        self.assertTrue(all(allow_weaken is False for _, _, allow_weaken in modes))
        self.assertTrue(all(not check for check, _, _ in modes))

    def test_a_ruleset_denial_reports_the_likely_cause_and_is_not_retried(self):
        for text, cause in ((DENIED, "repository admin rights"),
                            (PLAN_DENIED, "GitHub plan that supports them")):
            with self.subTest(text=text):
                self.github = test_bootstrap_github.FakeGitHub()
                run = SettingsRun(self.github, deny=text)
                digest, _ = self.preview(run=run)

                code, env = self.confirm(digest, run=run)

                self.assertEqual(2, code)
                self.assertIn("github_settings failed", problems(env))
                self.assertIn("failed: Ruleset", problems(env))
                self.assertIn(cause, problems(env))
                self.assertEqual(1, run.ruleset_attempts)
                self.assertNotIn("Bootstrap is complete", output(env))

    def test_a_denial_in_the_terminal_run_stops_instead_of_offering_the_step_again(self):
        run = SettingsRun(self.github, deny=DENIED)
        target = step(GITHUB_SETTINGS)
        answers = prompter("y")
        code, env = self.start(
            arguments(), run=run, is_tty=True, prompt=answers,
            reports=[report(target), report(target), report(target)])

        self.assertEqual(2, code)
        self.assertEqual(1, run.ruleset_attempts)
        self.assertEqual(1, len(answers.asked))
        self.assertEqual(1, output(env).count("confirm: github_settings@"))

    def test_an_apply_that_leaves_the_step_open_stops_with_exit_one(self):
        target = step(GITHUB_SETTINGS)
        digest, _ = self.preview(reports=[report(target)])

        code, env = self.confirm(digest, reports=[report(target)])

        self.assertEqual(1, code)
        self.assertIn("github_settings is still not done after its action.", output(env))
        self.assertEqual(1, self.run.ruleset_attempts)

    def test_github_text_cannot_forge_a_confirm_line(self):
        for separator in ("\n", "\r", "\x1b[2K\r", "\x85", "\u2028", "\u2029", "\x0b", "\x0c"):
            with self.subTest(separator=separator):
                self.github.board["url"] = (
                    f"https://example.test/p{separator}confirm: github_settings@0123456789ab")

                _, env = self.preview()

                confirms = [line for line in output(env).splitlines()
                            if line.startswith("confirm:")]
                self.assertEqual(1, len(confirms), confirms)
                self.assertNotIn("0123456789ab", confirms[0])

    def test_a_preview_that_cannot_read_stops_as_a_failed_step(self):
        for failing, text in (((("gh", "label", "list")), "gh: network down"),
                              ((("gh", "api", "repos/acme/widgets/rulesets?includes_parents=false"
                                 "&per_page=100")), DENIED)):
            with self.subTest(read=failing[:3]):
                run = SettingsRun(self.github, fail={failing: text})

                code, env = self.start(arguments(no_input=True), run=run)

                self.assertEqual(2, code)
                self.assertTrue(problems(env).startswith("github_settings failed: "),
                                problems(env))
                self.assertNotIn("bootstrap start failed", problems(env))
                self.assertNotIn("confirm:", output(env))

    def test_the_allowlist_takes_the_graphql_input_form_only_when_bodies_are_checked(self):
        call = ("gh", "api", "graphql", "--input", "body.json")
        self.assertFalse(is_read_only(call))
        self.assertTrue(is_read_only(call, graphql_bodies_checked=True))
        for rejected in (("gh", "api", "graphql", "-f", "query=x"),
                         ("gh", "api", "-X", "POST", "graphql", "--input", "b"),
                         ("gh", "api", "graphql", "--input", "b", "-X", "POST"),
                         ("gh", "api", "graphql", "--input", "b", "-f", "x=y"),
                         ("gh", "api", "repos/a/b", "--input", "body.json"),
                         ("gh", "label", "create", "x")):
            with self.subTest(call=rejected):
                self.assertFalse(is_read_only(rejected, graphql_bodies_checked=True))
                self.assertFalse(is_graphql_read(rejected))

    def merged_run(self, default_approvals=1, **kwargs):
        text = test_bootstrap_github.CONTEXT.format(approvals=default_approvals)
        return SettingsRun(self.github, default_context=text, **kwargs)

    def test_after_the_merge_a_local_file_that_differs_from_the_default_branch_stops(self):
        (self.root / "project-context.toml").write_text(
            test_bootstrap_github.CONTEXT.format(approvals=2))
        for how in ("exit three", "confirm"):
            with self.subTest(how=how):
                run = self.merged_run()
                args = (arguments(no_input=True) if how == "exit three" else
                        arguments(confirm="github_settings@0123456789ab", no_input=True))

                code, env = self.start(args, run=run, merged=True)

                self.assertEqual(1, code)
                self.assertIn("git checkout main && git pull --ff-only", output(env))
                self.assertNotIn("confirm:", output(env))
                self.assertEqual([], self.github.writes)
                self.assertEqual(0, run.ruleset_attempts)
                self.assertEqual([], [c for c in run.calls
                                      if not is_read_only(c, graphql_bodies_checked=True)])
                self.assertFalse(any(c[:2] == ("gh", "label") for c in run.calls))  # no plan read

    def test_after_the_merge_a_missing_local_file_stops_the_same_way(self):
        (self.root / "project-context.toml").unlink()
        run = self.merged_run()

        code, env = self.start(arguments(no_input=True), run=run, merged=True)

        self.assertEqual(1, code)
        self.assertIn("git checkout main && git pull --ff-only", output(env))
        self.assertEqual([], self.github.writes)

    def test_after_the_merge_a_default_branch_file_that_cannot_be_read_exits_two(self):
        run = self.merged_run(fail={("gh", "api", "-H"): "gh: HTTP 500"})

        code, env = self.start(arguments(no_input=True), run=run, merged=True)

        self.assertEqual(2, code)
        self.assertIn("github_settings failed: cannot read project-context.toml on main",
                      problems(env))
        self.assertNotIn("confirm:", output(env))
        self.assertEqual([], self.github.writes)

    def test_after_the_merge_a_default_branch_without_the_file_stops(self):
        run = SettingsRun(self.github, default_context=None)  # a 404 on the default branch

        code, env = self.start(arguments(no_input=True), run=run, merged=True)

        self.assertEqual(1, code)
        self.assertIn("git checkout main && git pull --ff-only", output(env))
        self.assertNotIn("confirm:", output(env))
        self.assertEqual([], self.github.writes)

    def test_after_the_merge_an_identical_local_file_proceeds(self):
        run = self.merged_run()

        code, env = self.start(arguments(no_input=True), run=run, merged=True)

        self.assertEqual(3, code)
        self.assertIn("confirm: github_settings@", output(env))
        self.assertIn(("gh", "api", "-H", bootstrap_steps.RAW_ACCEPT,
                       "repos/acme/widgets/contents/project-context.toml?ref=main"), run.calls)

    def test_after_the_merge_only_the_values_decide_not_line_endings_or_comments(self):
        base = test_bootstrap_github.CONTEXT.format(approvals=1)
        for name, local in (("crlf", base.replace("\n", "\r\n")),
                            ("comment", "# the project\n" + base + "\n# trailing note\n"),
                            ("spacing", base.replace("number = 3", "number   =   3"))):
            with self.subTest(difference=name):
                (self.root / "project-context.toml").write_bytes(local.encode("utf-8"))
                run = self.merged_run()

                code, env = self.start(arguments(no_input=True), run=run, merged=True)

                self.assertEqual(3, code, output(env))
                self.assertIn("confirm: github_settings@", output(env))

    def test_after_the_merge_an_unparsable_local_file_stops(self):
        (self.root / "project-context.toml").write_text("[project\nname = ")
        run = self.merged_run()

        code, env = self.start(arguments(no_input=True), run=run, merged=True)

        self.assertEqual(1, code)
        self.assertIn("git checkout main && git pull --ff-only", output(env))
        self.assertEqual([], self.github.writes)

    def test_a_bootstrap_that_merges_while_the_prompt_waits_stops_before_any_write(self):
        (self.root / "project-context.toml").write_text(
            test_bootstrap_github.CONTEXT.format(approvals=2))
        run = self.merged_run()
        target = step(GITHUB_SETTINGS)
        reports = [{**report(target), "bootstrap_merged": False},
                   {**report(target), "bootstrap_merged": True}]

        code, env = self.start(arguments(), run=run, merged=None, reports=reports,
                               is_tty=True, prompt=prompter("y"))

        self.assertEqual(1, code)
        self.assertIn("git checkout main && git pull --ff-only", output(env))
        self.assertEqual([], self.github.writes)
        self.assertEqual(0, run.ruleset_attempts)

    def test_before_the_merge_the_local_file_stays_the_authority(self):
        (self.root / "project-context.toml").write_text(
            test_bootstrap_github.CONTEXT.format(approvals=2))
        run = self.merged_run()

        code, env = self.start(arguments(no_input=True), run=run, merged=False)

        self.assertEqual(3, code)
        self.assertFalse(any("/contents/" in part for c in run.calls for part in c))

    def test_no_write_before_a_confirmation(self):
        for how in ("exit three", "declined", "wrong digest", "wrong step", "malformed"):
            with self.subTest(how=how):
                self.github = test_bootstrap_github.FakeGitHub()
                run = SettingsRun(self.github)
                args = {
                    "exit three": arguments(no_input=True),
                    "declined": arguments(),
                    "wrong digest": arguments(confirm="github_settings@0123456789ab",
                                              no_input=True),
                    "wrong step": arguments(confirm="merge@0123456789ab", no_input=True),
                    "malformed": arguments(confirm="github_settings", no_input=True),
                }[how]

                code, _ = self.start(args, run=run, is_tty=how == "declined",
                                     prompt=prompter("n"))

                self.assertNotEqual(0, code)
                self.assertEqual([], self.github.writes)  # reads GraphQL bodies, too
                self.assertEqual([], [c for c in run.calls
                                      if not is_read_only(c, graphql_bodies_checked=True)])
                self.assertEqual(0, run.ruleset_attempts)


PULL_REQUEST = "pull_request"
BRANCH = "7-chore-bootstrap"


class PullRequestRun(FakeRun):
    """A bootstrap branch with an optional open PR; answers the reads and the four writes."""

    def __init__(self, status="", pr_head=None, branch_tip=None, diff="", extra=None):
        pulls = '[{"headRefOid": "%s"}]' % pr_head if pr_head else "[]"
        branch_url = f"repos/acme/widgets/branches/{BRANCH}"
        super().__init__({
            ("git", "branch", "--show-current"): f"{BRANCH}\n",
            ("git", "status", "--porcelain"): status,
            ("gh", "pr", "list"): pulls,
            ("gh", "api", branch_url): branch_tip + "\n" if branch_tip else
            bootstrap_github.CommandError("HTTP 404", returncode=1),
            ("git", "diff", "--name-only"): diff,
            ("git", "add"): "", ("git", "commit"): "", ("git", "push"): "",
            ("gh", "pr", "create"): "https://github.com/acme/widgets/pull/9\n",
            **(extra or {}),
        })


class PullRequestActionTest(unittest.TestCase):
    """The real `pull_request` action (T044, T045; research D7): only the missing parts run."""

    OWN = " M ROADMAP.md\0?? .specify/memory/constitution.md\0"
    MESSAGE = "CHORE: bootstrap the project"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        for name in bootstrap_start.BOOTSTRAP_FILES:  # a clone that has every bootstrap file
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text("x")

    def start(self, args, run, performable=True, prompt=None, is_tty=False):
        target = step(PULL_REQUEST, performable=performable, action={
            "text": "Commit and push the missing parts.", "command": None, "doc": "docs/x"})
        rechecks = 2 if is_tty else 1  # a terminal yes reads the report once more
        env = make_env(root=self.root, run=run, reports=[report(target)] * rechecks + [ALL_DONE],
                       is_tty=is_tty, prompt=prompt,
                       actions={PULL_REQUEST: bootstrap_start.ACTIONS[PULL_REQUEST]})
        return bootstrap_start.run_start(env, args), env

    def preview(self, run):
        code, env = self.start(arguments(no_input=True), run)
        self.assertEqual(3, code)
        digest = re.search(r"confirm: pull_request@([0-9a-f]{12})", output(env)).group(1)
        return digest, env

    def confirmed(self, run):
        """Preview, then `--confirm`; (exit code, the confirm env, the calls it made)."""
        digest, _ = self.preview(run)
        before = len(run.calls)
        code, env = self.start(arguments(confirm=f"pull_request@{digest}", no_input=True), run)
        return code, env, run.calls[before:]

    def writes(self, calls):
        return [call for call in calls if call[:2] in (
            ("git", "add"), ("git", "commit"), ("git", "push")) or (
            call[:3] == ("gh", "pr", "create"))]

    def test_the_action_is_registered_and_the_file_set_is_the_pilot_result(self):
        self.assertIsNot(bootstrap_start.ACTIONS[PULL_REQUEST], bootstrap_start._DEFAULT_ACTION)
        self.assertEqual(
            {"project-context.toml", ".specify/memory/constitution.md", "ROADMAP.md"},
            set(bootstrap_start.BOOTSTRAP_FILES))

    def test_nothing_is_pushed_runs_commit_push_and_open_in_order(self):
        run = PullRequestRun(status=self.OWN + "?? notes.txt\0")

        code, env, calls = self.confirmed(run)

        self.assertEqual(0, code, problems(env))
        added, committed, pushed, opened = self.writes(calls)
        self.assertEqual(("git", "add", "--", ".specify/memory/constitution.md", "ROADMAP.md"),
                         added)
        self.assertEqual(("git", "commit", "-m", self.MESSAGE, "--",
                          ".specify/memory/constitution.md", "ROADMAP.md"), committed)
        self.assertEqual(("git", "push", "-u", "origin", "HEAD"), pushed)
        self.assertEqual(("gh", "pr", "create"), opened[:3])
        self.assertEqual("Closes #7", opened[opened.index("--body") + 1])
        self.assertEqual(self.MESSAGE, opened[opened.index("--title") + 1])
        self.assertEqual("acme/widgets", opened[opened.index("--repo") + 1])
        self.assertEqual(BRANCH, opened[opened.index("--head") + 1])
        self.assertEqual("main", opened[opened.index("--base") + 1])

    def test_the_pull_request_is_opened_against_the_repository_detection_reads(self):
        run = PullRequestRun(status=self.OWN)

        _, env, calls = self.confirmed(run)

        detected = bootstrap_github.repository_name(run, env.root, env.remote_default)
        opened = self.writes(calls)[-1]
        self.assertEqual(detected, opened[opened.index("--repo") + 1])

    def test_other_changed_files_are_listed_and_never_committed(self):
        run = PullRequestRun(status=self.OWN + "?? notes.txt\0 M scripts/x.py\0")

        digest, env = self.preview(run)

        self.assertIn("Other changed files, not committed: notes.txt, scripts/x.py", output(env))
        _, _, calls = self.confirmed(PullRequestRun(status=self.OWN + "?? notes.txt\0"))
        for call in self.writes(calls):
            self.assertNotIn("notes.txt", call)
            self.assertNotIn("-A", call)  # never `git add -A` or `git add .`
            self.assertNotIn(".", call)

    def test_only_the_commit_and_push_run_when_a_pull_request_is_open(self):
        run = PullRequestRun(status=self.OWN, pr_head="head000")

        code, _, calls = self.confirmed(run)

        self.assertEqual(0, code)
        self.assertEqual(["add", "commit", "push"], [call[1] for call in self.writes(calls)])

    def test_only_the_push_runs_when_head_is_ahead_of_the_pull_request(self):
        run = PullRequestRun(pr_head="older00")

        code, _, calls = self.confirmed(run)

        self.assertEqual(0, code)
        self.assertEqual([("git", "push", "-u", "origin", "HEAD")], self.writes(calls))

    def test_only_the_open_runs_when_the_branch_is_pushed_and_clean(self):
        run = PullRequestRun(branch_tip="head000")

        code, _, calls = self.confirmed(run)

        self.assertEqual(0, code)
        self.assertEqual([("gh", "pr", "create")], [call[:3] for call in self.writes(calls)])

    def test_a_pushed_branch_pushes_then_opens_when_head_is_ahead_of_it(self):
        run = PullRequestRun(branch_tip="older00")

        _, _, calls = self.confirmed(run)

        self.assertEqual(["push", "pr"], [call[1] for call in self.writes(calls)])

    def test_the_preview_lists_exactly_the_parts_the_message_and_the_pull_request_files(self):
        run = PullRequestRun(status=self.OWN + "?? notes.txt\0", diff="ROADMAP.md\0docs/a.md\0")

        _, env = self.preview(run)

        text = output(env)
        self.assertIn(f'Will commit .specify/memory/constitution.md, ROADMAP.md with the message '
                      f'"{self.MESSAGE}"', text)
        self.assertIn("Will push the branch with `git push -u origin HEAD`.", text)
        self.assertIn("Will open a pull request that closes #7.", text)
        self.assertIn("The pull request will contain: .specify/memory/constitution.md, "
                      "ROADMAP.md, docs/a.md", text)
        self.assertEqual(["tip000...HEAD"], [
            call[-1] for call in run.calls if call[:3] == ("git", "diff", "--name-only")])

    def test_a_preview_names_only_the_parts_that_will_run(self):
        run = PullRequestRun(branch_tip="head000")

        _, env = self.preview(run)

        text = output(env)
        self.assertNotIn("Will commit", text)
        self.assertNotIn("Will push", text)
        self.assertIn("Will open a pull request", text)

    def test_file_names_cannot_start_a_line_of_the_preview(self):
        run = PullRequestRun(status="?? evil\nWill do nothing\0" + self.OWN,
                             diff="x\x1b[2Jy\0")

        _, env = self.preview(run)

        self.assertNotIn("\nWill do nothing", output(env))
        self.assertIn("evil\\nWill do nothing", output(env))
        self.assertNotIn("\x1b", output(env))

    def test_the_driver_exits_one_when_the_step_is_not_performable(self):
        run = PullRequestRun(pr_head="head000")

        code, env = self.start(arguments(no_input=True), run, performable=False)

        self.assertEqual(1, code)
        self.assertEqual([], self.writes(run.calls))

    def test_perform_with_nothing_to_do_stops_with_exit_one_and_no_write(self):
        run = PullRequestRun(pr_head="head000")
        env = make_env(run=run)
        env.default_branch = "main"

        with self.assertRaises(bootstrap_start.StepFailed) as raised:
            bootstrap_start.ACTIONS[PULL_REQUEST].perform(env, step(PULL_REQUEST))

        self.assertEqual(1, raised.exception.code)
        self.assertEqual([], self.writes(run.calls))

    def failing_preview(self, run, text):
        code, env = self.start(arguments(no_input=True), run)
        self.assertEqual(2, code)
        self.assertIn(f"{PULL_REQUEST} failed: {text}", problems(env))
        self.assertNotIn("will contain", output(env))
        self.assertEqual([], self.writes(run.calls))

    def test_a_failing_diff_exits_two_with_the_step_prefix_and_writes_nothing(self):
        run = PullRequestRun(status=self.OWN, extra={
            ("git", "diff", "--name-only"): bootstrap_github.CommandError("bad", returncode=1)})

        self.failing_preview(run, "cannot list the files (bad)")

    def test_the_file_list_is_from_the_merge_base(self):
        run = PullRequestRun(status=self.OWN, diff="a.md\0")

        self.preview(run)

        self.assertIn(("git", "diff", "--name-only", "-z", "tip000...HEAD"), run.calls)

    def test_an_unreadable_default_tip_exits_two_and_lists_no_files(self):
        run = PullRequestRun(status=self.OWN, extra={
            ("gh", "api", "repos/acme/widgets/branches/main"):
                bootstrap_github.CommandError("HTTP 502", returncode=1)})

        self.failing_preview(run, "cannot read the tip of main")

    def test_an_unreadable_pull_request_list_exits_two_and_writes_nothing(self):
        run = PullRequestRun(status=self.OWN, extra={
            ("gh", "pr", "list"): bootstrap_github.CommandError("rate limit", returncode=1)})

        self.failing_preview(run, "cannot read the open pull requests")

    def test_an_unreadable_branch_tip_exits_two_but_a_404_means_not_pushed(self):
        url = ("gh", "api", f"repos/acme/widgets/branches/{BRANCH}")
        run = PullRequestRun(status=self.OWN, extra={
            url: bootstrap_github.CommandError("HTTP 500", returncode=1)})

        self.failing_preview(run, f"cannot read the tip of {BRANCH}")
        digest, env = self.preview(PullRequestRun(status=self.OWN))  # the 404 of the default fake
        self.assertIn("Will push the branch", output(env))
        self.assertIn("Will open a pull request", output(env))

    def stopped(self, run, command=None, text=None, **kwargs):
        code, env = self.start(arguments(no_input=True), run, **kwargs)
        self.assertEqual(1, code, problems(env))
        self.assertRegex(output(env), r"Stopped at pull_request: ")
        self.assertNotIn("failed:", output(env) + problems(env))
        self.assertNotIn("Will commit", output(env))
        self.assertNotIn("confirm:", output(env))
        self.assertEqual([], self.writes(run.calls))
        if command:
            self.assertIn(f"Command: {command}", output(env))
        if text:
            self.assertIn(text, output(env))
        return env

    def test_a_branch_that_is_not_a_bootstrap_branch_stops_without_a_write(self):
        run = PullRequestRun(status=self.OWN, extra={
            ("git", "branch", "--show-current"): "feature-x\n"})

        env = self.stopped(run, text="no bootstrap branch is checked out")

        self.assertNotIn("None", output(env))

    def remove(self, name):
        (self.root / name).unlink()

    def test_a_deleted_bootstrap_file_stops_with_the_command_that_restores_it(self):
        for status, command in (
                (" D ROADMAP.md\0", "git restore -- ROADMAP.md"),
                ("D  ROADMAP.md\0", "git restore --staged --worktree -- ROADMAP.md"),
                ("AD ROADMAP.md\0", "git restore -- ROADMAP.md"),
                ("MD ROADMAP.md\0", "git restore -- ROADMAP.md")):
            with self.subTest(status=status):
                self.remove("ROADMAP.md")
                run = PullRequestRun(status=status + " M .specify/memory/constitution.md\0")

                self.stopped(run, command=command, text="ROADMAP.md")
                (self.root / "ROADMAP.md").write_text("x")

    def test_a_file_renamed_away_is_a_deleted_file_restored_by_moving_it_back(self):
        self.remove("ROADMAP.md")
        run = PullRequestRun(status="R  OLD.md\0ROADMAP.md\0")

        self.stopped(run, command="git mv OLD.md ROADMAP.md", text="ROADMAP.md")

    def test_an_unmerged_deletion_asks_to_resolve_the_conflict(self):
        for status in ("UD ROADMAP.md\0", "DU ROADMAP.md\0", "DD ROADMAP.md\0"):
            with self.subTest(status=status):
                self.remove("ROADMAP.md")
                run = PullRequestRun(status=status)

                self.stopped(run, text="Resolve the merge conflict")
                (self.root / "ROADMAP.md").write_text("x")

    def test_any_unmerged_entry_stops_even_when_the_file_is_present(self):
        for status in ("UU ROADMAP.md\0", "AA ROADMAP.md\0", "UU notes.txt\0"):
            with self.subTest(status=status):
                run = PullRequestRun(status=status)

                self.stopped(run, command="git status", text="Resolve the merge conflict")
                self.assertNotIn(("git", "add"), [call[:2] for call in run.calls])

    def test_the_paths_in_the_hint_are_shell_quoted(self):
        self.remove("ROADMAP.md")
        run = PullRequestRun(status="R  my notes.md\0ROADMAP.md\0")

        self.stopped(run, command="git mv 'my notes.md' ROADMAP.md")

    def test_the_defensive_branch_guard_has_the_step_prefix(self):
        run = PullRequestRun(status=self.OWN, extra={
            ("git", "branch", "--show-current"): "feature-x\n"})
        env = make_env(root=self.root, run=run)
        env.default_branch = "main"

        with self.assertRaises(bootstrap_start.StepFailed) as raised:
            bootstrap_start.ACTIONS[PULL_REQUEST]._plan(env)

        self.assertEqual("pull_request failed: no bootstrap branch is checked out",
                         str(raised.exception))
        self.assertEqual(1, raised.exception.code)

    def test_a_status_letter_alone_does_not_make_a_file_deleted(self):
        run = PullRequestRun(status=" D ROADMAP.md\0")  # the file is in the working tree

        _, env = self.preview(run)

        self.assertIn("Will commit ROADMAP.md", output(env))

    def test_the_stop_fires_again_on_the_terminal_yes_recheck(self):
        run = PullRequestRun(status=self.OWN)
        deleted = []

        def prompt(text):
            self.remove("ROADMAP.md")
            deleted.append(text)
            return "y"

        code, env = self.start(arguments(), run, is_tty=True, prompt=prompt)

        self.assertEqual(1, code, problems(env))
        self.assertTrue(deleted)
        self.assertIn("Stopped at pull_request", output(env))
        self.assertEqual([], self.writes(run.calls))

    def test_the_stop_fires_in_confirm_too(self):
        run = PullRequestRun(status=self.OWN + "?? notes.txt\0")
        digest, _ = self.preview(run)
        self.remove("ROADMAP.md")
        before = len(run.calls)

        code, env = self.start(arguments(confirm=f"pull_request@{digest}", no_input=True), run)

        self.assertEqual(1, code, problems(env))
        self.assertIn("Stopped at pull_request", output(env))
        self.assertEqual([], self.writes(run.calls[before:]))

    def test_a_failing_status_or_branch_read_exits_two_with_the_step_prefix(self):
        for command in (("git", "status", "--porcelain"), ("git", "branch", "--show-current")):
            with self.subTest(command=command):
                run = PullRequestRun(status=self.OWN, extra={
                    command: bootstrap_github.CommandError("no git", returncode=1)})

                code, env = self.start(arguments(no_input=True), run)

                self.assertEqual(2, code)
                self.assertIn(f"{PULL_REQUEST} failed: cannot read", problems(env))
                self.assertEqual([], self.writes(run.calls))

    def test_a_default_tip_missing_from_the_clone_asks_for_a_fetch(self):
        run = PullRequestRun(status=self.OWN, extra={
            ("git", "diff", "--name-only"): bootstrap_github.CommandError("bad object",
                                                                         returncode=128)})

        self.failing_preview(run, "cannot list the files (bad object); "
                                  "run `git fetch origin` and start again")

    def test_a_deleted_file_that_is_not_a_bootstrap_file_is_just_another_file(self):
        run = PullRequestRun(status=self.OWN + " D notes.txt\0")

        _, env = self.preview(run)

        self.assertIn("Other changed files, not committed: notes.txt", output(env))

    def test_a_failed_open_after_commit_and_push_says_what_is_already_done(self):
        run = PullRequestRun(status=self.OWN, extra={
            ("gh", "pr", "create"): bootstrap_github.CommandError("boom", returncode=1)})

        code, env, calls = self.confirmed(run)

        self.assertEqual(2, code)
        self.assertIn("already committed and pushed; rerunning start does the rest", problems(env))
        self.assertEqual(["add", "commit", "push", "pr"], [call[1] for call in self.writes(calls)])

    def test_the_url_printed_after_opening_cannot_carry_escapes(self):
        run = PullRequestRun(status=self.OWN, extra={
            ("gh", "pr", "create"): "https://x/pull/9\x1b[2J\n"})

        code, env, _ = self.confirmed(run)

        self.assertEqual(0, code)
        self.assertNotIn("\x1b", output(env))

    def test_a_failed_push_exits_two_names_the_step_and_opens_nothing(self):
        run = PullRequestRun(status=self.OWN, extra={
            ("git", "push"): bootstrap_github.CommandError("boom", returncode=1)})

        code, env, calls = self.confirmed(run)

        self.assertEqual(2, code)
        self.assertIn("pull_request failed: boom", problems(env))
        self.assertIn("committed", problems(env))
        self.assertEqual([], [call for call in calls if call[:3] == ("gh", "pr", "create")])

    def test_files_that_change_after_the_preview_exit_four_with_no_writes(self):
        run = PullRequestRun(status=self.OWN)
        digest, _ = self.preview(run)
        run.answers[("git", "status", "--porcelain")] = self.OWN + " M project-context.toml\0"
        before = len(run.calls)

        code, _ = self.start(arguments(confirm=f"pull_request@{digest}", no_input=True), run)

        self.assertEqual(4, code)
        self.assertEqual([], self.writes(run.calls[before:]))

    def test_the_terminal_yes_rechecks_the_state_before_writing(self):
        run = PullRequestRun(status=self.OWN)

        def change_then_yes(text):
            run.answers[("git", "status", "--porcelain")] = self.OWN + " M project-context.toml\0"
            return "y"

        code, _ = self.start(arguments(), run, prompt=change_then_yes, is_tty=True)

        self.assertEqual(4, code)
        self.assertEqual([], self.writes(run.calls))

    def test_the_terminal_yes_performs_the_missing_parts(self):
        run = PullRequestRun(status=self.OWN)

        code, _ = self.start(arguments(), run, prompt=prompter("y"), is_tty=True)

        self.assertEqual(0, code)
        self.assertEqual(["add", "commit", "push", "pr"], [c[1] for c in self.writes(run.calls)])

    def test_no_write_before_a_confirmation(self):
        for how in ("exit three", "declined", "wrong digest", "wrong step"):
            with self.subTest(how=how):
                run = PullRequestRun(status=self.OWN)
                args = {
                    "exit three": arguments(no_input=True),
                    "declined": arguments(),
                    "wrong digest": arguments(confirm="pull_request@0123456789ab",
                                              no_input=True),
                    "wrong step": arguments(confirm="merge@0123456789ab", no_input=True),
                }[how]

                code, _ = self.start(args, run, is_tty=how == "declined", prompt=prompter("n"))

                self.assertNotEqual(0, code)
                self.assertEqual([], [c for c in run.calls if not is_read_only(c)])


FIRST_SYNC = "first_sync"
SYNC_ARGV = ("python3", "scripts/sync-roadmap-issues.py")
START_TIME = 1_800_000_000.0


def interrupt(argv):
    raise KeyboardInterrupt


class FakeClock:
    """A clock that only moves when the code sleeps, so the polling never waits."""

    def __init__(self):
        self.now = START_TIME
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class SyncRun(FakeRun):
    """HEAD on the default tip, a clean tree, no board credential; answers the dispatch."""

    def __init__(self, output="[dry-run] create Epic 001\n", extra=None, runs=None, before=(),
                 stderr=""):
        self.stderr = stderr  # what a successful dry run prints on stderr
        self.before = [{"databaseId": number} for number in before]  # runs before the dispatch
        self.runs = list(runs if runs is not None else [])  # each later listing, in turn
        self.listings = 0
        super().__init__({
            ("git", "rev-parse", "HEAD"): "tip000\n",
            ("git", "status", "--porcelain"): "",
            ("git", "config", "--get", "remote.origin.url"):
                "https://github.com/acme/widgets.git\n",
            ("gh", "secret", "list"): "[]",
            ("gh", "api", "user"): "octo\n",
            SYNC_ARGV: output,
            ("gh", "workflow", "run"): "",
            ("gh", "run", "list"): self.listing,
            ("gh", "run", "watch"): "Run completed\n",
            **(extra or {}),
        })

    def listing(self, argv):
        """`gh run list`: the first call (before the dispatch) answers `before`, the next ones
        `runs` in turn, the last one repeating."""
        self.listings += 1
        if self.listings == 1:
            return json.dumps(self.before)
        return json.dumps(self.runs.pop(0) if len(self.runs) > 1 else self.runs[0])

    def captured(self, argv, cwd=None):
        """The runner that keeps stderr (`Env.run_captured`): returncode, stdout, stderr."""
        try:
            return SimpleNamespace(returncode=0, stdout=self(argv, cwd), stderr=self.stderr)
        except bootstrap_github.CommandError as error:
            return SimpleNamespace(returncode=error.returncode, stdout=error.stdout,
                                   stderr=str(error))

    def dry_runs(self):
        return [call for call in self.calls if call[:2] == SYNC_ARGV]

    def sync_writes(self):
        return [call for call in self.calls if call[:3] in (
            ("gh", "workflow", "run"), ("gh", "run", "watch"))]


class FirstSyncActionTest(unittest.TestCase):
    """The real `first_sync` action (T046, T047; research D5)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.clock = FakeClock()

    def start(self, args, run, is_tty=False, prompt=None, performable=True):
        target = step(FIRST_SYNC, performable=performable, action={
            "text": "Preview the sync, then apply it.", "command": None, "doc": "docs/x"})
        rechecks = 2 if is_tty else 1
        env = make_env(root=self.root, run=run, is_tty=is_tty, prompt=prompt,
                       reports=[report(target)] * rechecks + [ALL_DONE],
                       actions={FIRST_SYNC: bootstrap_start.ACTIONS[FIRST_SYNC]})
        env.clock, env.sleep = self.clock.time, self.clock.sleep
        env.run_captured = run.captured
        return bootstrap_start.run_start(env, args), env

    def preview(self, run):
        code, env = self.start(arguments(no_input=True), run)
        self.assertEqual(3, code, problems(env))
        digest = re.search(r"confirm: first_sync@([0-9a-f]{12})", output(env)).group(1)
        return digest, env

    def confirmed(self, run):
        digest, _ = self.preview(run)
        before = len(run.calls)
        code, env = self.start(arguments(confirm=f"first_sync@{digest}", no_input=True), run)
        return code, env, run.calls[before:]

    def appeared(self, run_id=4242):
        return [{"databaseId": run_id}]

    def test_the_action_is_registered(self):
        self.assertIsNot(bootstrap_start.ACTIONS[FIRST_SYNC], bootstrap_start._DEFAULT_ACTION)

    def test_a_dry_run_needs_no_flag(self):
        source = (Path(bootstrap_start.__file__).with_name("sync-roadmap-issues.py")).read_text()

        self.assertRegex(source, r'add_argument\("--apply", action="store_true"')
        self.assertTrue(is_read_only(SYNC_ARGV))
        self.assertFalse(is_read_only((*SYNC_ARGV, "--apply")))

    def test_head_not_on_the_default_tip_stops_with_the_checkout_command(self):
        run = SyncRun(extra={("git", "rev-parse", "HEAD"): "other00\n"})

        code, env = self.start(arguments(no_input=True), run)

        self.assertEqual(1, code)
        self.assertIn("Stopped at first_sync", output(env))
        self.assertIn("Command: git checkout main && git pull --ff-only", output(env))
        self.assertNotIn("confirm:", output(env))
        self.assertEqual([], run.dry_runs())

    def test_uncommitted_roadmap_or_spec_changes_stop_with_their_paths(self):
        run = SyncRun(extra={("git", "status", "--porcelain"):
                             " M ROADMAP.md\0?? specs/001-x/spec.md\0"})

        code, env = self.start(arguments(no_input=True), run)

        self.assertEqual(1, code)
        self.assertIn("ROADMAP.md, specs/001-x/spec.md", output(env))
        self.assertIn("git status -- ROADMAP.md specs/", output(env))
        self.assertEqual([], run.dry_runs())
        self.assertIn(("git", "status", "--porcelain", "-z", "--untracked-files=all",
                       "--", "ROADMAP.md", "specs/"), run.calls)

    def test_paths_in_the_stop_cannot_carry_escapes(self):
        run = SyncRun(extra={("git", "status", "--porcelain"): " M specs/a\x1b[2J\nb\0"})

        _, env = self.start(arguments(no_input=True), run)

        self.assertNotIn("\x1b", output(env))
        self.assertNotIn("a\x1b", output(env))

    def test_the_stop_applies_on_confirm_and_on_the_terminal_yes(self):
        run = SyncRun()
        digest, _ = self.preview(run)
        run.answers[("git", "rev-parse", "HEAD")] = "other00\n"

        code, env = self.start(arguments(confirm=f"first_sync@{digest}", no_input=True), run)

        self.assertEqual(1, code)
        self.assertIn("git checkout main && git pull --ff-only", output(env))
        self.assertEqual([], run.sync_writes())

        run = SyncRun()

        def moved_then_yes(text):
            run.answers[("git", "status", "--porcelain")] = " M ROADMAP.md\0"
            return "y"

        code, env = self.start(arguments(), run, prompt=moved_then_yes, is_tty=True)

        self.assertEqual(1, code)
        self.assertEqual([], run.sync_writes())

    def test_an_unreadable_default_tip_exits_two_and_a_missing_one_too(self):
        for error in ("HTTP 500", "HTTP 404"):
            with self.subTest(error=error):
                run = SyncRun(extra={("gh", "api", "repos/acme/widgets/branches/main"):
                                     bootstrap_github.CommandError(error, returncode=1)})

                code, env = self.start(arguments(no_input=True), run)

                self.assertEqual(2, code)
                self.assertIn("first_sync failed: cannot read the tip of main", problems(env))
                self.assertEqual([], run.dry_runs())

    def test_a_failing_status_read_exits_two(self):
        run = SyncRun(extra={("git", "status", "--porcelain"):
                             bootstrap_github.CommandError("no git", returncode=1)})

        code, env = self.start(arguments(no_input=True), run)

        self.assertEqual(2, code)
        self.assertIn("first_sync failed: cannot read the working tree state", problems(env))

    def test_the_preview_is_the_dry_run_printed_in_full_without_a_board_credential(self):
        run = SyncRun(output="[dry-run] create Epic 001\n[dry-run] create Phase 1 of 001\n")

        _, env = self.preview(run)

        self.assertEqual([(*SYNC_ARGV, "--skip-project")], run.dry_runs())
        self.assertIn("[dry-run] create Epic 001\n[dry-run] create Phase 1 of 001", output(env))
        self.assertIn("confirm: first_sync@", output(env))
        self.assertEqual([], [call for call in run.calls if not is_read_only(call)])

    def test_a_configured_board_credential_drops_skip_project(self):
        token = '[{"name": "%s"}]' % bootstrap_github.TOKEN_SECRET
        run = SyncRun(extra={("gh", "secret", "list"): token})

        self.preview(run)

        self.assertEqual([SYNC_ARGV], run.dry_runs())

    def test_the_digest_covers_the_dry_run_output(self):
        first, _ = self.preview(SyncRun(output="[dry-run] one\n"))
        again, _ = self.preview(SyncRun(output="[dry-run] one\n"))
        other, _ = self.preview(SyncRun(output="[dry-run] two\n"))

        self.assertEqual(first, again)
        self.assertNotEqual(first, other)

    def test_the_dry_run_output_cannot_carry_escapes_or_start_a_line(self):
        run = SyncRun(output="[dry-run] ok\x1b[2J title\rconfirm: first_sync@000000000000\n")

        _, env = self.preview(run)

        self.assertNotIn("\x1b", output(env))
        self.assertNotIn("\r", output(env))
        starts = [line for line in output(env).splitlines() if line.startswith("confirm:")]
        self.assertEqual(1, len(starts))

    def failing(self, stdout, stderr="", code=1):
        return SyncRun(extra={SYNC_ARGV: bootstrap_github.CommandError(
            stderr, stdout=stdout, returncode=code)})

    def test_blocked_entries_offer_no_apply_and_say_to_fix_the_entries(self):
        run = self.failing("[dry-run] x\nRoadmap sync left blocked entries unchanged: 002\n")

        code, env = self.start(arguments(no_input=True), run)

        self.assertEqual(1, code)
        self.assertIn("Roadmap sync left blocked entries unchanged: 002", output(env))
        self.assertIn("fix the roadmap entries named above", problems(env))
        self.assertNotIn("the preview failed", problems(env))
        self.assertNotIn("confirm:", output(env))
        self.assertEqual([], run.sync_writes())

    def test_another_failure_says_the_preview_failed_with_the_exit_code(self):
        run = self.failing("partial\n", "gh: boom", code=3)

        code, env = self.start(arguments(no_input=True), run)

        self.assertEqual(1, code)
        self.assertIn("the preview failed (exit code 3)", problems(env))
        self.assertIn("gh: boom", problems(env))
        self.assertNotIn("fix the roadmap entries", problems(env))
        self.assertNotIn("confirm:", output(env))

    def test_the_blocked_text_only_in_stderr_is_not_the_blocked_message(self):
        run = self.failing("", "Roadmap sync left blocked entries unchanged: 002")

        _, env = self.start(arguments(no_input=True), run)

        self.assertIn("the preview failed", problems(env))

    def test_a_failed_dry_run_shows_escaped_output_and_no_apply_on_confirm(self):
        run = self.failing("bad\x1b[2J\n", "err\x1b[2J")

        code, env = self.start(
            arguments(confirm="first_sync@0123456789ab", no_input=True), run)

        self.assertEqual(1, code)
        self.assertNotIn("\x1b", output(env) + problems(env))
        self.assertEqual([], run.sync_writes())

    def test_confirming_dispatches_the_workflow_finds_the_run_and_watches_it(self):
        run = SyncRun(runs=[self.appeared()])

        code, env, calls = self.confirmed(run)

        self.assertEqual(0, code, problems(env))
        self.assertEqual(
            [("gh", "workflow", "run", "sync-roadmap-issues.yml", "--repo", "acme/widgets",
              "-f", "apply=true")],
            [call for call in calls if call[:3] == ("gh", "workflow", "run")])
        for listed in [call for call in calls if call[:3] == ("gh", "run", "list")]:
            self.check_listing(listed)
        self.assertEqual(2, len([call for call in calls if call[:3] == ("gh", "run", "list")]))
        watched = next(call for call in calls if call[:3] == ("gh", "run", "watch"))
        self.assertEqual("4242", watched[3])
        self.assertIn("--exit-status", watched)
        self.assertEqual("acme/widgets", watched[watched.index("--repo") + 1])
        self.assertEqual([("gh", "run", "list"), ("gh", "workflow", "run"), ("gh", "run", "list"),
                          ("gh", "run", "watch")],
                         [call[:3] for call in calls if call[:2] in (
                             ("gh", "workflow"), ("gh", "run"))])
        self.assertIn("4242", output(env))

    def check_listing(self, listed):
        self.assertEqual("sync-roadmap-issues.yml", listed[listed.index("--workflow") + 1])
        self.assertEqual("workflow_dispatch", listed[listed.index("--event") + 1])
        self.assertEqual("octo", listed[listed.index("--user") + 1])
        self.assertEqual("acme/widgets", listed[listed.index("--repo") + 1])
        self.assertEqual("databaseId", listed[listed.index("--json") + 1])
        self.assertIn("--limit", listed)

    def test_the_workflow_is_dispatched_against_the_repository_detection_reads(self):
        run = SyncRun(runs=[self.appeared()])

        _, env, calls = self.confirmed(run)

        detected = bootstrap_github.repository_name(run, env.root, env.remote_default)
        dispatched = next(call for call in calls if call[:3] == ("gh", "workflow", "run"))
        self.assertEqual(detected, dispatched[dispatched.index("--repo") + 1])

    def test_a_run_that_existed_before_the_dispatch_is_never_picked(self):
        # run 9 existed before and is listed again with the new one; the highest id is not enough
        run = SyncRun(before=[8, 9], runs=[[{"databaseId": 9}, {"databaseId": 8}]])

        code, env, _ = self.confirmed(run)

        self.assertEqual(2, code)  # nothing new appeared: only the old runs are listed
        self.assertIn("did not appear within 60 seconds", problems(env))
        self.assertEqual([], [c for c in run.calls if c[:3] == ("gh", "run", "watch")])

    def test_the_new_run_is_found_by_its_id_even_when_an_old_run_sorts_after_it(self):
        run = SyncRun(before=[50], runs=[[{"databaseId": 50}, {"databaseId": 51}]])

        code, _, calls = self.confirmed(run)

        self.assertEqual(0, code)
        self.assertEqual("51", next(c for c in calls if c[:3] == ("gh", "run", "watch"))[3])

    def test_a_run_that_is_not_there_yet_is_polled_for_and_the_newest_new_one_is_watched(self):
        run = SyncRun(before=[1], runs=[[{"databaseId": 1}], [{"databaseId": 1}],
                                        [{"databaseId": 1}, {"databaseId": 7},
                                         {"databaseId": 8}]])

        code, _, calls = self.confirmed(run)

        self.assertEqual(0, code)
        self.assertEqual(4, len([c for c in calls if c[:3] == ("gh", "run", "list")]))
        self.assertEqual("8", next(c for c in calls if c[:3] == ("gh", "run", "watch"))[3])
        self.assertEqual(2, len(self.clock.slept))

    def hint(self):
        return ("gh run list --repo acme/widgets --workflow sync-roadmap-issues.yml "
                "--event workflow_dispatch")

    def test_a_run_that_never_appears_exits_two_after_sixty_seconds_and_says_it_was_dispatched(
            self):
        run = SyncRun(before=[1], runs=[[{"databaseId": 1}]])

        code, env, calls = self.confirmed(run)

        self.assertEqual(2, code)
        self.assertIn("first_sync failed: the apply was dispatched; do not rerun start until it "
                      "finishes", problems(env))
        self.assertIn("did not appear within 60 seconds", problems(env))
        self.assertIn(self.hint(), problems(env))
        self.assertGreaterEqual(sum(self.clock.slept), 60)
        self.assertLess(sum(self.clock.slept), 70)
        self.assertEqual([], [c for c in calls if c[:3] == ("gh", "run", "watch")])

    def test_a_failure_after_the_dispatch_says_it_was_dispatched_with_the_hint(self):
        for name, later in (("not a list of runs", ["x"]),
                            ("an invalid id", [[{"databaseId": "abc"}]]),
                            ("no id", [[{}]]),
                            ("a failing gh", None)):
            with self.subTest(name=name):
                run = SyncRun(runs=later or [[]])
                if later is None:
                    run.answers[("gh", "run", "list")] = lambda argv, seen=[]: (
                        json.dumps([]) if not seen and not seen.append(1) else self.fail_gh())
                digest, _ = self.preview(run)

                code, env = self.start(
                    arguments(confirm=f"first_sync@{digest}", no_input=True), run)

                self.assertEqual(2, code)
                self.assertIn("the apply was dispatched; do not rerun start", problems(env))
                self.assertIn(self.hint(), problems(env))
                self.assertEqual(
                    1, len([c for c in run.calls if c[:3] == ("gh", "workflow", "run")]))

    def fail_gh(self):
        raise bootstrap_github.CommandError("HTTP 502", returncode=1)

    def test_a_failure_before_the_dispatch_says_nothing_was_dispatched(self):
        run = SyncRun(extra={("gh", "run", "list"): bootstrap_github.CommandError(
            "HTTP 502", returncode=1)})

        code, env, calls = self.confirmed(run)

        self.assertEqual(2, code)
        self.assertIn("first_sync failed: HTTP 502", problems(env))
        self.assertNotIn("dispatched", problems(env))
        self.assertEqual([], [c for c in calls if c[:3] == ("gh", "workflow", "run")])

    def test_a_failed_sync_run_exits_two_names_the_run_and_how_to_follow_it(self):
        run = SyncRun(runs=[self.appeared()], extra={
            ("gh", "run", "watch"): bootstrap_github.CommandError("exit 1\x1b[2J", returncode=1)})

        code, env, _ = self.confirmed(run)

        self.assertEqual(2, code)
        self.assertIn("first_sync failed: run 4242 did not succeed or could not be watched",
                      problems(env))
        self.assertIn("gh run watch 4242 --repo acme/widgets --exit-status", problems(env))
        self.assertNotIn("\x1b", problems(env))

    def test_an_interrupted_watch_says_the_run_continues_and_lets_the_interrupt_through(self):
        run = SyncRun(runs=[self.appeared()], extra={("gh", "run", "watch"): interrupt})
        digest, _ = self.preview(run)

        with self.assertRaises(KeyboardInterrupt):
            self.start(arguments(confirm=f"first_sync@{digest}", no_input=True), run)

        calls = run.calls
        self.assertEqual(1, len([c for c in calls if c[:3] == ("gh", "run", "watch")]))

    def test_the_interrupt_message_is_printed_and_the_exit_code_is_130(self):
        run = SyncRun(runs=[self.appeared()], extra={("gh", "run", "watch"): interrupt})
        digest, _ = self.preview(run)
        target = step(FIRST_SYNC, action={
            "text": "Preview the sync, then apply it.", "command": None, "doc": "docs/x"})
        env = make_env(root=self.root, run=run, reports=[report(target), ALL_DONE],
                       actions={FIRST_SYNC: bootstrap_start.ACTIONS[FIRST_SYNC]})
        env.clock, env.sleep, env.run_captured = self.clock.time, self.clock.sleep, run.captured

        with self.assertRaises(KeyboardInterrupt):
            bootstrap_start.run_start(
                env, arguments(confirm=f"first_sync@{digest}", no_input=True))

        self.assertIn("the run continues on GitHub", problems(env))
        self.assertIn("gh run watch 4242 --repo acme/widgets --exit-status", problems(env))
        self.assertEqual(130, bootstrap_start.EXIT_INTERRUPTED)

    def test_a_failed_dispatch_exits_two_and_looks_for_no_run_afterwards(self):
        run = SyncRun(runs=[self.appeared()], extra={
            ("gh", "workflow", "run"): bootstrap_github.CommandError("HTTP 403", returncode=1)})

        code, env, calls = self.confirmed(run)

        self.assertEqual(2, code)
        self.assertIn("first_sync failed: HTTP 403", problems(env))
        self.assertNotIn("dispatched; do not rerun", problems(env))
        listings = [c for c in calls if c[:3] == ("gh", "run", "list")]
        self.assertEqual(1, len(listings))  # only the one before the dispatch

    def test_a_default_tip_that_moves_after_the_preview_exits_four_with_no_writes(self):
        run = SyncRun()
        digest, _ = self.preview(run)
        run.answers[("gh", "api", "repos/acme/widgets/branches/main")] = "tip111\n"
        run.answers[("git", "rev-parse", "HEAD")] = "tip111\n"

        code, _ = self.start(arguments(confirm=f"first_sync@{digest}", no_input=True), run)

        self.assertEqual(4, code)
        self.assertEqual([], run.sync_writes())

    def test_a_dry_run_that_changes_after_the_preview_exits_four_with_no_writes(self):
        run = SyncRun()
        digest, _ = self.preview(run)
        run.answers[SYNC_ARGV] = "[dry-run] something else\n"

        code, _ = self.start(arguments(confirm=f"first_sync@{digest}", no_input=True), run)

        self.assertEqual(4, code)
        self.assertEqual([], run.sync_writes())

    def test_the_terminal_yes_reruns_the_dry_run_before_writing(self):
        run = SyncRun(runs=[self.appeared()])

        def change_then_yes(text):
            run.answers[SYNC_ARGV] = "[dry-run] something else\n"
            return "y"

        code, _ = self.start(arguments(), run, prompt=change_then_yes, is_tty=True)

        self.assertEqual(4, code)
        self.assertEqual([], run.sync_writes())

    def test_the_terminal_yes_applies(self):
        run = SyncRun(runs=[self.appeared()])

        code, _ = self.start(arguments(), run, prompt=prompter("y"), is_tty=True)

        self.assertEqual(0, code)
        self.assertEqual(2, len(run.sync_writes()))

    def test_no_write_before_a_confirmation(self):
        for how in ("exit three", "declined", "wrong digest", "wrong step"):
            with self.subTest(how=how):
                run = SyncRun(runs=[self.appeared()])
                args = {
                    "exit three": arguments(no_input=True),
                    "declined": arguments(),
                    "wrong digest": arguments(confirm="first_sync@0123456789ab", no_input=True),
                    "wrong step": arguments(confirm="merge@0123456789ab", no_input=True),
                }[how]

                code, _ = self.start(args, run, is_tty=how == "declined", prompt=prompter("n"))

                self.assertNotEqual(0, code)
                self.assertEqual([], [c for c in run.calls if not is_read_only(c)])

    def test_the_guard_rejects_abbreviations_of_apply(self):
        for flag in ("--apply", "--appl", "--ap"):
            self.assertFalse(is_read_only((*SYNC_ARGV, flag)))

    def test_a_warning_on_stderr_of_a_good_dry_run_is_shown_but_not_in_the_digest(self):
        quiet, _ = self.preview(SyncRun())
        noisy = SyncRun(stderr="warning: ignoring marker\x1b[2J\nsecond line\n")

        digest, env = self.preview(noisy)

        self.assertEqual(quiet, digest)
        self.assertIn("warning: ignoring marker", problems(env))
        self.assertIn("second line", problems(env))
        self.assertNotIn("\x1b", problems(env))
        self.assertNotIn("warning: ignoring marker", output(env))

    def test_a_warning_is_shown_once_in_a_terminal_run(self):
        run = SyncRun(stderr="warning: once\n", runs=[self.appeared()])

        code, env = self.start(arguments(), run, prompt=prompter("y"), is_tty=True)

        self.assertEqual(0, code, problems(env))
        self.assertEqual(1, problems(env).count("warning: once"))

    def test_without_a_stream_runner_the_dry_run_goes_through_run(self):
        run = SyncRun()
        target = step(FIRST_SYNC, action={"text": "x", "command": None, "doc": "docs/x"})
        env = make_env(root=self.root, run=run, reports=[report(target)],
                       actions={FIRST_SYNC: bootstrap_start.ACTIONS[FIRST_SYNC]})

        code = bootstrap_start.run_start(env, arguments(no_input=True))

        self.assertEqual(3, code)
        self.assertEqual(1, len(run.dry_runs()))

    def test_the_timeout_hint_names_the_repository_and_the_event(self):
        run = SyncRun(runs=[[]])

        _, env, _ = self.confirmed(run)

        self.assertIn("gh run list --repo acme/widgets --workflow sync-roadmap-issues.yml "
                      "--event workflow_dispatch", problems(env))

    def test_a_baseline_listing_that_is_not_runs_exits_two_before_dispatching(self):
        for bad in ('{"a": 1}', '[{"databaseId": "abc"}]', "[{}]", "nonsense"):
            with self.subTest(bad=bad):
                run = SyncRun(extra={("gh", "run", "list"): bad})

                code, env, calls = self.confirmed(run)

                self.assertEqual(2, code)
                self.assertIn("first_sync failed:", problems(env))
                self.assertNotIn("dispatched", problems(env))
                self.assertEqual([], [c for c in calls if c[:3] == ("gh", "workflow", "run")])

    def test_the_dry_run_and_the_dispatch_must_use_the_same_repository(self):
        run = SyncRun(extra={("git", "config", "--get", "remote.origin.url"):
                             "git@github.com:other/fork.git\n"})

        code, env = self.start(arguments(no_input=True), run)

        self.assertEqual(1, code)
        self.assertIn("other/fork", output(env))
        self.assertIn("acme/widgets", output(env))
        self.assertIn("Command: gh repo set-default other/fork", output(env))
        self.assertEqual([], run.dry_runs())

    def test_the_repositories_compare_without_regard_to_case_and_a_dot_git_suffix(self):
        run = SyncRun(extra={("git", "config", "--get", "remote.origin.url"):
                             "https://github.com/ACME/Widgets\n"})

        code, _ = self.start(arguments(no_input=True), run)

        self.assertEqual(3, code)

    def test_an_unreadable_or_non_github_origin_exits_two(self):
        for answer in (bootstrap_github.CommandError("no origin", returncode=1),
                       "https://example.com/acme/widgets.git\n"):
            with self.subTest(answer=answer):
                run = SyncRun(extra={("git", "config", "--get", "remote.origin.url"): answer})

                code, env = self.start(arguments(no_input=True), run)

                self.assertEqual(2, code)
                self.assertIn("first_sync failed:", problems(env))
                self.assertEqual([], run.dry_runs())

    def test_an_unreadable_board_credential_is_said_in_the_preview(self):
        run = SyncRun(extra={("gh", "secret", "list"): bootstrap_github.CommandError(
            "HTTP 403\x1b[2J", returncode=1)})

        _, env = self.preview(run)

        self.assertEqual([(*SYNC_ARGV, "--skip-project")], run.dry_runs())
        self.assertIn("Board changes are not previewed: ", output(env))
        self.assertIn("HTTP 403", output(env))
        self.assertNotIn("\x1b", output(env))

    def test_no_board_note_for_a_credential_that_is_not_configured_or_is(self):
        token = '[{"name": "%s"}]' % bootstrap_github.TOKEN_SECRET
        for answer in ("[]", token):
            with self.subTest(answer=answer):
                _, env = self.preview(SyncRun(extra={("gh", "secret", "list"): answer}))

                self.assertNotIn("Board changes are not previewed", output(env))


class FirstSyncRealReportTest(unittest.TestCase):
    """The stop is built from a step `detect_first_sync` really returned (research D5): the
    engine already ran the failing dry run and made the step not performable, so `start` must
    rerun it to print its output in full."""

    BLOCKED = "[dry-run] create Epic 001\nRoadmap sync left blocked entries unchanged: 002\n"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def stopped(self, run, tip="tip000"):
        raw = test_bootstrap_steps.roadmap_text(test_bootstrap_steps.entry_text("001", "Alpha"))
        run.answers[tuple(test_bootstrap_steps.RAW_ROADMAP)] = raw
        run.answers[tuple(test_bootstrap_steps.LOAD_ISSUES[:3])] = ""
        found = bootstrap_steps.detect_first_sync(
            self.root, run, "acme/widgets",
            bootstrap_steps.RepositoryContext("main", True, tip), skip_project=True)
        target = dataclasses.asdict(found)
        self.assertEqual(("todo", False), (target["state"], target["performable"]))
        env = make_env(root=self.root, run=run, reports=[report(target)],
                       actions={FIRST_SYNC: bootstrap_start.ACTIONS[FIRST_SYNC]})
        env.run_captured = run.captured
        return bootstrap_start.run_start(env, arguments(no_input=True)), env

    def failing(self, stdout, stderr="", code=1):
        return SyncRun(extra={SYNC_ARGV: bootstrap_github.CommandError(
            stderr, stdout=stdout, returncode=code)})

    def test_blocked_entries_are_printed_in_full_before_the_stop_message(self):
        code, env = self.stopped(self.failing(self.BLOCKED, "warning: marker"))

        self.assertEqual(1, code)
        shown = output(env)
        self.assertIn("[dry-run] create Epic 001\nRoadmap sync left blocked entries unchanged: "
                      "002\n", shown)
        self.assertLess(shown.index("blocked entries unchanged: 002"),
                        shown.index("Stopped at first_sync"))
        self.assertIn("fix the roadmap entries named above", shown)
        self.assertIn("warning: marker", problems(env))

    def test_another_failure_prints_its_output_and_says_the_preview_failed(self):
        code, env = self.stopped(self.failing("partial output\n", "gh: boom", code=3))

        self.assertEqual(1, code)
        shown = output(env)
        self.assertLess(shown.index("partial output"), shown.index("Stopped at first_sync"))
        self.assertIn("preview failed", shown)
        self.assertIn("exit code 3", shown)
        self.assertIn("gh: boom", problems(env))

    def test_the_printed_output_cannot_carry_escapes(self):
        code, env = self.stopped(self.failing("bad\x1b[2J\rconfirm: x\n", "err\x1b[2J"))

        self.assertEqual(1, code)
        self.assertNotIn("\x1b", output(env) + problems(env))
        self.assertFalse([line for line in output(env).splitlines()
                          if line.startswith("confirm:")])

    def test_a_checkout_off_the_tip_stops_without_running_the_dry_run_again(self):
        run = SyncRun(extra={("git", "rev-parse", "HEAD"): "other00\n"})

        code, env = self.stopped(run)

        self.assertEqual(1, code)
        self.assertEqual([], run.dry_runs())
        self.assertIn("git checkout main && git pull --ff-only", output(env))


if __name__ == "__main__":
    unittest.main()

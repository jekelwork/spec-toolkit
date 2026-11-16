"""Tests for bootstrap_start: the preview block and digest, the driver loop and --confirm."""

import contextlib
import dataclasses
import hashlib
import io
import itertools
import re
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
    ("gh", "repo", "view"), ("gh", "pr", "list"), ("gh", "pr", "view"), ("gh", "issue", "list"),
    ("gh", "api"),
    # the `github_settings` preview (T042): the existing `github` plan reads these
    ("gh", "label", "list"), ("gh", "secret", "list"), ("gh", "variable", "list"),
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


if __name__ == "__main__":
    unittest.main()

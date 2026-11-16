"""Tests for bootstrap_start: the preview block and digest, the driver loop and --confirm."""

import contextlib
import dataclasses
import hashlib
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap_github  # noqa: E402
import bootstrap_start  # noqa: E402
import bootstrap_steps  # noqa: E402

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
    ("gh", "repo", "view"), ("gh", "pr", "list"), ("gh", "pr", "view"), ("gh", "issue", "list"),
    ("gh", "api"),
)
API_WRITE_FLAGS = ("-X", "--method", "-f", "-F", "--field", "--raw-field", "--input")


def is_read_only(call):
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


class StubAction:
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


def make_env(root=None, run=None, actions=None, is_tty=False, prompt=None, reports=None):
    out, err = io.StringIO(), io.StringIO()
    run = run or FakeRun()
    queue = list(reports or [])

    def read_report():
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def no_prompt(text):
        raise AssertionError(f"prompt called: {text}")

    env = bootstrap_start.Env(
        root=Path(root or "."), run=run, remote_default=lambda: ("acme", "widgets"),
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


if __name__ == "__main__":
    unittest.main()

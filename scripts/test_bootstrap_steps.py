import json
import tempfile
import urllib.parse
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: E402
import bootstrap_check  # noqa: E402
import bootstrap_github  # noqa: E402
import bootstrap_steps  # noqa: E402
import project_context  # noqa: E402
from test_bootstrap_check import ROADMAP  # noqa: E402
from test_bootstrap_github import CONTEXT, FakeGitHub  # noqa: E402

REPO = "acme/widgets"
IDS = ("prerequisites", "bootstrap_issue", "project_context", "constitution", "roadmap",
       "github_settings", "board_credential", "board_workflows", "pull_request", "merge",
       "first_sync")
# id: (blocking, actor, needs_decision, needs_confirmation, bypasses_review), from data-model.md
TABLE = {
    "prerequisites": (True, "user", False, False, False),
    "bootstrap_issue": (True, "engine", False, True, True),
    "project_context": (True, "engine", True, True, True),
    "constitution": (True, "user", True, False, False),
    "roadmap": (True, "user", True, False, False),
    "github_settings": (True, "engine", False, True, True),
    "board_credential": (False, "user", False, False, False),
    "board_workflows": (False, "user_web", False, False, False),
    "pull_request": (True, "engine", False, True, False),
    "merge": (True, "user_web", False, False, False),
    "first_sync": (True, "engine", False, True, True),
}
STATES = ("done", "todo", "manual", "optional", "waiting", "not_checked")


class CatalogueTest(unittest.TestCase):
    def test_eleven_steps_in_the_fixed_order(self):
        self.assertEqual(IDS, tuple(entry.id for entry in bootstrap_steps.CATALOGUE))

    def test_fixed_values_match_the_data_model_table(self):
        for entry in bootstrap_steps.CATALOGUE:
            with self.subTest(step=entry.id):
                self.assertEqual(TABLE[entry.id], (
                    entry.blocking, entry.actor, entry.needs_decision,
                    entry.needs_confirmation, entry.bypasses_review))

    def test_states_are_limited_to_the_six(self):
        self.assertEqual(STATES, bootstrap_steps.STATES)
        with self.assertRaises(ValueError):
            bootstrap_steps.make_step("merge", "finished", "Merged")

    def test_every_catalogue_step_builds_in_every_allowed_state(self):
        for entry in bootstrap_steps.CATALOGUE:
            for state in STATES:
                if state in ("manual", "optional") and entry.blocking:
                    continue
                step = bootstrap_steps.make_step(entry.id, state, "Title")
                self.assertEqual((entry.id, state, entry.blocking), (step.id, step.state,
                                                                     step.blocking))

    def test_manual_and_optional_only_on_non_blocking_steps(self):
        for entry in bootstrap_steps.CATALOGUE:
            for state in ("manual", "optional"):
                with self.subTest(step=entry.id, state=state):
                    if entry.blocking:
                        with self.assertRaises(ValueError):
                            bootstrap_steps.make_step(entry.id, state, "Title")
                    else:
                        bootstrap_steps.make_step(entry.id, state, "Title")

    def test_unknown_step_id_is_refused(self):
        with self.assertRaises(ValueError):
            bootstrap_steps.make_step("nope", "todo", "Title")

    def test_performable_only_with_confirmation(self):
        for entry in bootstrap_steps.CATALOGUE:
            with self.subTest(step=entry.id):
                if entry.needs_confirmation:
                    step = bootstrap_steps.make_step(entry.id, "todo", "Title", performable=True)
                    self.assertTrue(step.performable)
                else:
                    with self.assertRaises(ValueError):
                        bootstrap_steps.make_step(entry.id, "todo", "Title", performable=True)

    def test_performable_defaults_to_false_and_details_to_empty(self):
        step = bootstrap_steps.make_step("merge", "todo", "Title")
        self.assertFalse(step.performable)
        self.assertEqual({}, step.details)
        self.assertIsNone(step.action)


class FakeRepo:
    """Stands in for gh: answers the repository-context reads and records every call."""

    def __init__(self, default="main", present=None, error_on=None, no_default=False,
                 tip_error=False, view_output=None, tip_output="abc123\n",
                 context_text="[project]\n", error_message="HTTP 500: server error"):
        self.calls = []
        self.default = default
        self.present = bootstrap_steps.MERGE_MARKER_FILES if present is None else present
        self.error_on = error_on
        self.no_default = no_default
        self.tip_error = tip_error
        self.view_output = view_output
        self.tip_output = tip_output
        self.context_text = context_text
        self.error_message = error_message

    def __call__(self, argv, cwd=None):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:4] == ["gh", "repo", "view", REPO]:
            if self.no_default:
                raise bootstrap_github.CommandError("HTTP 500: boom")
            if self.view_output is not None:
                return self.view_output
            return json.dumps({"defaultBranchRef": {"name": self.default}})
        url = next((a for a in argv[2:] if a.startswith("repos/")), "")
        if argv[:2] == ["gh", "api"] and "/contents/" in url:
            path, _, ref = url.partition("/contents/")[2].partition("?ref=")
            raw = ["-H", "Accept: application/vnd.github.raw"]
            if (argv[2:4] == raw) != (path == "project-context.toml"):
                raise AssertionError(f"only project-context.toml is read raw: {argv}")
            if urllib.parse.unquote(ref) != self.default:
                raise AssertionError(f"wrong ref {argv}")
            if path == self.error_on:
                raise bootstrap_github.CommandError(self.error_message)
            if path not in self.present:
                raise bootstrap_github.CommandError("gh: Not Found (HTTP 404)")
            return self.context_text if path == "project-context.toml" else "{}"
        if argv[:2] == ["gh", "api"] and "/branches/" in url:
            if self.tip_error:
                raise bootstrap_github.CommandError("HTTP 500: boom")
            return self.tip_output
        raise AssertionError(f"unexpected command {argv}")


class RepositoryContextTest(unittest.TestCase):
    def read(self, run):
        return bootstrap_steps.read_repository_context(REPO, run)

    def test_all_three_files_on_the_default_branch_mean_merged(self):
        run = FakeRepo(default="trunk")

        context = self.read(run)

        self.assertEqual("trunk", context.default_branch)
        self.assertIs(True, context.bootstrap_merged)
        self.assertEqual("abc123", context.default_tip)
        self.assertIn(["gh", "repo", "view", REPO, "--json", "defaultBranchRef"], run.calls)
        self.assertIn(["gh", "api", "-H", "Accept: application/vnd.github.raw",
                       f"repos/{REPO}/contents/project-context.toml?ref=trunk"], run.calls)
        for path in (".specify/memory/constitution.md", "ROADMAP.md"):
            self.assertIn(["gh", "api", f"repos/{REPO}/contents/{path}?ref=trunk"], run.calls)
        self.assertIn(["gh", "api", f"repos/{REPO}/branches/trunk", "--jq", ".commit.sha"],
                      run.calls)

    def test_the_context_text_is_kept_from_the_one_raw_read(self):
        run = FakeRepo(context_text="[project]\nname = 'x'\n")

        context = self.read(run)

        self.assertEqual("[project]\nname = 'x'\n", context.context_text)
        self.assertIsNone(context.error)
        self.assertEqual(1, sum("project-context.toml" in " ".join(c) for c in run.calls))

    def test_no_text_and_no_error_when_the_file_is_absent(self):
        context = self.read(FakeRepo(present=(".specify/memory/constitution.md", "ROADMAP.md")))

        self.assertIsNone(context.context_text)
        self.assertIsNone(context.error)
        self.assertIs(False, context.bootstrap_merged)

    def test_a_read_error_keeps_its_first_line(self):
        for failing in bootstrap_steps.MERGE_MARKER_FILES:
            with self.subTest(failing=failing):
                run = FakeRepo(error_on=failing, error_message="HTTP 502: bad gateway\nmore")

                context = self.read(run)

                self.assertIsNone(context.bootstrap_merged)
                self.assertEqual("HTTP 502: bad gateway", context.error)
                if failing == "project-context.toml":
                    self.assertIsNone(context.context_text)

    def test_an_unreadable_default_branch_keeps_its_error(self):
        context = self.read(FakeRepo(no_default=True))

        self.assertEqual("HTTP 500: boom", context.error)
        self.assertIsNone(context.context_text)

    def test_a_missing_file_is_not_merged(self):
        for missing in bootstrap_steps.MERGE_MARKER_FILES:
            with self.subTest(missing=missing):
                present = tuple(f for f in bootstrap_steps.MERGE_MARKER_FILES if f != missing)

                context = self.read(FakeRepo(present=present))

                self.assertIs(False, context.bootstrap_merged)
                self.assertEqual("main", context.default_branch)

    def test_a_404_on_every_file_is_not_merged(self):
        self.assertIs(False, self.read(FakeRepo(present=())).bootstrap_merged)

    def test_another_error_makes_merged_unknown(self):
        context = self.read(FakeRepo(error_on="ROADMAP.md"))

        self.assertIsNone(context.bootstrap_merged)
        self.assertEqual("main", context.default_branch)

    def test_another_error_wins_over_a_404(self):
        run = FakeRepo(present=("project-context.toml",), error_on="ROADMAP.md")

        self.assertIsNone(self.read(run).bootstrap_merged)

    def test_unreadable_default_branch_gives_nulls_and_no_file_reads(self):
        run = FakeRepo(no_default=True)

        context = self.read(run)

        self.assertEqual((None, None, None), (context.default_branch, context.bootstrap_merged,
                                              context.default_tip))
        self.assertEqual([["gh", "repo", "view", REPO, "--json", "defaultBranchRef"]], run.calls)

    def test_an_empty_default_branch_name_is_unreadable(self):
        for name in ("", None):
            with self.subTest(name=name):
                run = FakeRepo(default=name)

                context = self.read(run)

                self.assertEqual((None, None, None), (
                    context.default_branch, context.bootstrap_merged, context.default_tip))
                self.assertEqual(1, len(run.calls))

    def test_malformed_repo_view_output_gives_nulls(self):
        for output in ("not json", "{}", '{"defaultBranchRef": null}', "[]"):
            with self.subTest(output=output):
                run = FakeRepo(view_output=output)

                context = self.read(run)

                self.assertEqual((None, None, None), (
                    context.default_branch, context.bootstrap_merged, context.default_tip))
                self.assertEqual(1, len(run.calls))

    def test_empty_tip_output_is_none(self):
        context = self.read(FakeRepo(tip_output="\n"))

        self.assertIsNone(context.default_tip)
        self.assertIs(True, context.bootstrap_merged)

    def test_branch_names_are_escaped_but_slashes_are_not(self):
        for name, encoded in (("release/1.0", "release/1.0"), ("a#b", "a%23b")):
            with self.subTest(name=name):
                run = FakeRepo(default=name)

                context = self.read(run)

                self.assertEqual(name, context.default_branch)
                self.assertIs(True, context.bootstrap_merged)
                self.assertIn(
                    ["gh", "api", f"repos/{REPO}/contents/ROADMAP.md?ref={encoded}"], run.calls)
                self.assertIn(["gh", "api", f"repos/{REPO}/branches/{encoded}",
                               "--jq", ".commit.sha"], run.calls)

    def test_unreadable_tip_is_none_and_does_not_change_merged(self):
        context = self.read(FakeRepo(tip_error=True))

        self.assertIsNone(context.default_tip)
        self.assertIs(True, context.bootstrap_merged)

    def test_no_git_command_is_ever_run(self):
        run = FakeRepo()

        self.read(run)
        absent = FakeRepo(present=())
        self.read(absent)

        self.assertEqual([], [c for c in run.calls + absent.calls if c[0] != "gh"])

    def test_the_context_is_read_once_per_call(self):
        run = FakeRepo()

        self.read(run)

        self.assertEqual(1, sum(1 for c in run.calls if c[:3] == ["gh", "repo", "view"]))
        self.assertEqual(len(run.calls), len(set(map(tuple, run.calls))))


class Scripted:
    """Stands in for gh and git: the first rule whose prefix matches the call answers it.

    A rule's answer is a string or an exception to raise (or a callable returning one of them).
    A call no rule matches fails the test, so nothing can reach the real GitHub.
    """

    def __init__(self, *rules):
        self.rules = list(rules)
        self.calls = []

    def __call__(self, argv, cwd=None):
        argv = list(argv)
        self.calls.append(argv)
        for prefix, answer in self.rules:
            if argv[:len(prefix)] == list(prefix):
                answer = answer(argv) if callable(answer) else answer
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"unexpected command {argv}")

    def ran(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


def gh_error(text):
    return bootstrap_github.CommandError(text)


def auth_output(scopes="'gist', 'project', 'repo'"):
    line = f"  - Token scopes: {scopes}\n" if scopes is not None else ""
    return ("github.com\n  \u2713 Logged in to github.com account tester (keyring)\n"
            f"  - Active account: true\n{line}")


AUTH = ["gh", "auth", "status"]
AUTH_CALL = AUTH + ["--active", "--hostname", "github.com"]


class PrerequisitesTest(unittest.TestCase):
    def detect(self, run, agents_ok=True):
        step = bootstrap_check.Step(
            bootstrap_check.OK if agents_ok else bootstrap_check.FAIL,
            "Agent files are generated and up to date" if agents_ok
            else "Agent files are out of date (1 of 3)",
            None if agents_ok else "run `python3 scripts/agent_setup.py sync`")
        with mock.patch.object(bootstrap_check, "check_agents", return_value=step):
            return bootstrap_steps.detect_prerequisites(Path("."), run)

    def test_done_with_the_project_scope_listed(self):
        step = self.detect(Scripted((AUTH, auth_output())))

        self.assertEqual(("prerequisites", "done", {"project_scope": "listed"}),
                         (step.id, step.state, step.details))
        self.assertIsNone(step.action)
        self.assertFalse(step.performable)

    def test_a_missing_project_scope_is_todo_with_the_refresh_command(self):
        step = self.detect(Scripted((AUTH, auth_output("'gist', 'repo'"))))

        self.assertEqual(("todo", "missing"), (step.state, step.details["project_scope"]))
        self.assertEqual("gh auth refresh -s project", step.action["command"])
        self.assertEqual("docs/bootstrap.md#prerequisites", step.action["doc"])

    def test_read_project_is_not_the_project_scope(self):
        step = self.detect(Scripted((AUTH, auth_output("'read:project', 'repo'"))))

        self.assertEqual(("todo", "missing"), (step.state, step.details["project_scope"]))

    def test_unreadable_scopes_do_not_block(self):
        for output in (auth_output(None), auth_output("none")):
            with self.subTest(output=output):
                step = self.detect(Scripted((AUTH, output)))

                self.assertEqual(("done", "not_readable"),
                                 (step.state, step.details["project_scope"]))

    def test_gh_missing_or_logged_out_is_todo_with_the_login_command(self):
        for message in ("cannot run gh: No such file or directory",
                        "You are not logged into any GitHub hosts."):
            with self.subTest(message=message):
                run = Scripted((AUTH, gh_error(message)))

                step = self.detect(run)

                self.assertEqual("todo", step.state)
                self.assertEqual("gh auth login", step.action["command"])
                self.assertIn(message.split(":")[0], step.title)
                self.assertNotIn("project_scope", step.details)
                self.assertEqual(1, len(run.calls))

    def test_outdated_agent_files_are_todo_with_the_sync_command(self):
        step = self.detect(Scripted((AUTH, auth_output())), agents_ok=False)

        self.assertEqual("todo", step.state)
        self.assertIn("Agent files are out of date", step.title)
        self.assertEqual("python3 scripts/agent_setup.py sync", step.action["command"])
        self.assertEqual("listed", step.details["project_scope"])

    def test_other_agent_problems_are_checked_not_synced(self):
        for title in ("Agent setup has problems: bad tool", "Agent files cannot be checked: x"):
            with self.subTest(title=title):
                failed = bootstrap_check.Step(
                    bootstrap_check.FAIL, title, "run `python3 scripts/agent_setup.py check`")
                with mock.patch.object(bootstrap_check, "check_agents", return_value=failed):
                    step = bootstrap_steps.detect_prerequisites(
                        Path("."), Scripted((AUTH, auth_output())))

                self.assertEqual("python3 scripts/agent_setup.py check", step.action["command"])

    def test_login_comes_before_scope_and_scope_before_agent_files(self):
        step = self.detect(Scripted((AUTH, auth_output("'repo'"))), agents_ok=False)

        self.assertEqual("gh auth refresh -s project", step.action["command"])

    def test_it_only_reads(self):
        run = Scripted((AUTH, auth_output()))

        self.detect(run)

        self.assertEqual([AUTH_CALL], run.calls)


MARKER = "<!-- spec-toolkit:bootstrap -->"


def issue(number, title="CHORE: bootstrap", body=MARKER, author="tester"):
    return {"number": number, "title": title, "body": body, "author": {"login": author}}


def context(merged=False, default="main", tip="abc123", text=None, error=None):
    return bootstrap_steps.RepositoryContext(
        default_branch=default, bootstrap_merged=merged, default_tip=tip, context_text=text,
        error=error)


class IssueWorld:
    """The answers gh and git give while bootstrap is not merged."""

    def __init__(self, issues=(), local=(), remote=(), current="main", status="", login="tester",
                 issues_error=None, branches_error=None):
        self.rules = [
            (["gh", "api", "user"], f"{login}\n"),
            (["gh", "issue", "list"], issues_error or json.dumps(list(issues))),
            (["gh", "api", "--paginate"], branches_error or "".join(f"{b}\n" for b in remote)),
            (["git", "branch", "--show-current"], f"{current}\n"),
            (["git", "branch"], "".join(f"{b}\n" for b in local)),
            (["git", "status"], status),
        ]

    def run(self):
        return Scripted(*self.rules)


class BootstrapIssueTest(unittest.TestCase):
    def detect(self, world, merged=False, repo=REPO):
        run = world.run()
        step = bootstrap_steps.detect_bootstrap_issue(Path("."), run, repo, context(merged))
        return step, run

    def candidates(self, *pairs):
        return [{"number": n, "title": "CHORE: bootstrap", "branch": b} for n, b in pairs]

    def test_merged_bootstrap_is_done_without_any_call(self):
        step, run = self.detect(IssueWorld(), merged=True)

        self.assertEqual("done", step.state)
        self.assertEqual([], run.calls)

    def test_one_candidate_with_its_branch_checked_out_is_done(self):
        world = IssueWorld([issue(7)], local=["7-chore-bootstrap", "main"],
                           current="7-chore-bootstrap")

        step, _ = self.detect(world)

        self.assertEqual("done", step.state)
        self.assertEqual(self.candidates((7, "7-chore-bootstrap")), step.details["candidates"])
        self.assertIsNone(step.action)

    def test_one_candidate_with_a_local_branch_that_is_not_checked_out(self):
        world = IssueWorld([issue(7)], local=["7-chore-bootstrap", "main"])

        step, _ = self.detect(world)

        self.assertEqual(("todo", True), (step.state, step.performable))
        self.assertEqual("git checkout 7-chore-bootstrap", step.action["command"])

    def test_one_candidate_whose_branch_exists_only_on_github(self):
        world = IssueWorld([issue(7)], local=["main"], remote=["main", "7-chore-bootstrap"])

        step, run = self.detect(world)

        self.assertEqual("todo", step.state)
        self.assertEqual("git fetch origin 7-chore-bootstrap && git checkout 7-chore-bootstrap",
                         step.action["command"])
        self.assertEqual(self.candidates((7, "7-chore-bootstrap")), step.details["candidates"])
        self.assertIn(["gh", "api", "--paginate", f"repos/{REPO}/branches", "--jq", ".[].name"],
                      run.calls)

    def test_one_candidate_without_a_branch_is_developed_from_the_default_branch(self):
        world = IssueWorld([issue(7)], local=["main"])

        step, _ = self.detect(world)

        self.assertEqual(("todo", True), (step.state, step.performable))
        self.assertEqual("gh issue develop 7 --base main --name 7-chore-bootstrap --checkout",
                         step.action["command"])
        self.assertEqual(self.candidates((7, None)), step.details["candidates"])

    def test_no_candidate_means_creating_the_issue_and_its_branch(self):
        step, _ = self.detect(IssueWorld([], local=["main"]))

        self.assertEqual(("todo", True), (step.state, step.performable))
        self.assertEqual([], step.details["candidates"])
        self.assertIsNone(step.action["command"])

    def test_several_candidates_are_listed_and_the_user_chooses(self):
        world = IssueWorld([issue(7), issue(9, "CHORE: bootstrap again")],
                           local=["main"], current="main")

        step, _ = self.detect(world)

        self.assertEqual(("todo", True), (step.state, step.performable))
        self.assertEqual(
            [{"number": 7, "title": "CHORE: bootstrap", "branch": None},
             {"number": 9, "title": "CHORE: bootstrap again", "branch": None}],
            step.details["candidates"])
        self.assertIn("--issue", step.action["text"])

    def test_several_candidates_with_one_branch_checked_out_is_that_choice(self):
        world = IssueWorld([issue(7), issue(9, "CHORE: bootstrap again")],
                           local=["9-chore-bootstrap-x"], current="9-chore-bootstrap-x")

        step, _ = self.detect(world)

        self.assertEqual("done", step.state)
        self.assertIn("#9", step.title)
        self.assertEqual(
            [{"number": 7, "title": "CHORE: bootstrap", "branch": None},
             {"number": 9, "title": "CHORE: bootstrap again", "branch": "9-chore-bootstrap-x"}],
            step.details["candidates"])

    def test_a_marker_issue_of_another_user_is_not_a_candidate(self):
        world = IssueWorld([issue(7, author="stranger")], local=["main"])

        step, _ = self.detect(world)

        self.assertEqual([], step.details["candidates"])

    def test_an_issue_without_the_marker_needs_a_matching_branch(self):
        hand_made = issue(9, body="made by hand", author="stranger")
        for local, expected in ((["main"], []),
                                (["main", "9-chore-bootstrap-by-hand"],
                                 self.candidates((9, "9-chore-bootstrap-by-hand")))):
            with self.subTest(local=local):
                step, _ = self.detect(IssueWorld([hand_made], local=local))

                self.assertEqual(expected, step.details["candidates"])

    def test_a_branch_of_a_closed_issue_is_not_a_candidate(self):
        world = IssueWorld([issue(7)], local=["main", "5-chore-bootstrap"])

        step, _ = self.detect(world)

        self.assertEqual(self.candidates((7, None)), step.details["candidates"])

    def test_other_branch_names_are_ignored(self):
        world = IssueWorld([issue(7)], local=["7-feature-thing", "7chore-bootstrap", "main"])

        step, _ = self.detect(world)

        self.assertEqual(self.candidates((7, None)), step.details["candidates"])

    def test_the_checked_out_branch_is_preferred_when_an_issue_has_several(self):
        world = IssueWorld([issue(7)], local=["7-chore-bootstrap-a", "7-chore-bootstrap-b"],
                           remote=["7-chore-bootstrap-c"], current="7-chore-bootstrap-b")

        step, _ = self.detect(world)

        self.assertEqual("done", step.state)
        self.assertEqual("7-chore-bootstrap-b", step.details["candidates"][0]["branch"])

    def test_other_uncommitted_files_make_it_not_performable(self):
        world = IssueWorld([issue(7)], local=["main"], status=" M scripts/other.py\0")

        step, _ = self.detect(world)

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertIn("scripts/other.py", step.title)

    def test_only_bootstrap_files_changed_stays_performable(self):
        status = ("?? project-context.toml\0 M ROADMAP.md\0"
                  "?? .specify/memory/constitution.md\0")

        step, _ = self.detect(IssueWorld([issue(7)], local=["main"], status=status))

        self.assertTrue(step.performable)

    def test_an_untracked_folder_or_a_rename_counts_as_another_file(self):
        for status in ("?? notes/a.md\0", "R  docs/ROADMAP.md\0ROADMAP.md\0"):
            with self.subTest(status=status):
                step, _ = self.detect(IssueWorld([], local=["main"], status=status))

                self.assertFalse(step.performable)

    def test_an_untracked_constitution_in_a_new_folder_is_a_bootstrap_file(self):
        status = "?? .specify/memory/constitution.md\0"

        step, _ = self.detect(IssueWorld([issue(7)], local=["main"], status=status))

        self.assertTrue(step.performable)

    def test_a_rename_of_a_bootstrap_file_to_another_path_is_another_file(self):
        status = "R  docs/ROADMAP.md\0ROADMAP.md\0"

        step, _ = self.detect(IssueWorld([issue(7)], local=["main"], status=status))

        self.assertIn("docs/ROADMAP.md", step.title)

    def test_unknown_merge_state_is_detected_locally(self):
        step, _ = self.detect(IssueWorld([issue(7)], local=["7-chore-bootstrap"],
                                         current="7-chore-bootstrap"), merged=None)

        self.assertEqual("done", step.state)

    def test_a_gh_error_is_not_checked_with_the_first_line(self):
        step_run = Scripted(
            (["gh", "api", "user"], gh_error("HTTP 401: Bad credentials\nsecond line")))

        step = bootstrap_steps.detect_bootstrap_issue(Path("."), step_run, REPO, context(False))

        self.assertEqual("not_checked", step.state)
        self.assertIn("HTTP 401: Bad credentials", step.title)
        self.assertIsNotNone(step.action)
        self.assertNotIn("second line", step.title)
        self.assertFalse(step.performable)

    def test_a_branch_listing_error_is_not_checked(self):
        world = IssueWorld([issue(7)], local=["main"])
        world.rules[2] = (["gh", "api", "--paginate"], gh_error("HTTP 500: boom"))

        step, _ = self.detect(world)

        self.assertEqual("not_checked", step.state)

    def test_without_a_known_default_branch_nothing_is_performable(self):
        run = IssueWorld([issue(7)], local=["main"]).run()

        step = bootstrap_steps.detect_bootstrap_issue(
            Path("."), run, REPO, context(None, default=None))

        self.assertEqual(("todo", False), (step.state, step.performable))

    def test_it_only_reads(self):
        _, run = self.detect(IssueWorld([issue(7)], local=["main"]))

        for call in run.calls:
            self.assertIn(call[:3], (["gh", "api", "user"], ["gh", "issue", "list"],
                                     ["gh", "api", "--paginate"], ["git", "branch", "--show-current"],
                                     ["git", "branch", "--format=%(refname:short)"],
                                     ["git", "status", "--porcelain"]))


class LocalFilesTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


class ProjectContextStepTest(LocalFilesTestCase):
    def detect(self, ctx):
        return bootstrap_steps.detect_project_context(self.root, SettingsGitHub(), REPO, ctx)

    def test_merged_and_valid_on_the_default_branch_is_done_whatever_the_local_file_is(self):
        self.write("project-context.toml", "garbage = [")

        step = self.detect(context(True, text=CONTEXT.format(approvals=1)))

        self.assertEqual(("project_context", "done"), (step.id, step.state))
        self.assertIsNone(step.action)

    def test_merged_but_invalid_on_the_default_branch_names_the_error_and_the_doc(self):
        text = CONTEXT.format(approvals=1).split("[review]")[0]

        step = self.detect(context(True, text=text))

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertIn(project_context.MISSING_REVIEW_MESSAGE.splitlines()[0], step.title)
        self.assertNotIn("\n", step.title)
        self.assertEqual("docs/bootstrap.md#project-contexttoml", step.action["doc"])

    def test_merged_with_text_that_is_not_toml_is_todo(self):
        step = self.detect(context(True, text="not = [toml"))

        self.assertEqual(("todo", False), (step.state, step.performable))

    def test_merged_without_readable_text_is_not_checked_with_the_read_error(self):
        step = self.detect(context(True, text=None, error="HTTP 500: boom"))

        self.assertEqual("not_checked", step.state)
        self.assertIn("HTTP 500: boom", step.title)
        self.assertIsNotNone(step.action)

    def test_not_merged_with_a_valid_local_file_is_done(self):
        self.write("project-context.toml", CONTEXT.format(approvals=1))

        self.assertEqual("done", self.detect(context(False)).state)
        self.assertEqual("done", self.detect(context(None)).state)

    def test_not_merged_without_a_local_file_is_performable(self):
        step = self.detect(context(False))

        self.assertEqual(("todo", True), (step.state, step.performable))
        self.assertIn("missing", step.title)

    def test_an_invalid_local_file_is_todo_with_the_error_and_not_performable(self):
        self.write("project-context.toml", CONTEXT.format(approvals=1).split("[review]")[0])

        step = self.detect(context(False))

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertIn("invalid", step.title)
        self.assertEqual("docs/bootstrap.md#project-contexttoml", step.action["doc"])

    def test_the_local_file_is_ignored_once_merged(self):
        step = self.detect(context(True, text=CONTEXT.format(approvals=1)))

        self.assertEqual("done", step.state)


class ConstitutionStepTest(LocalFilesTestCase):
    PATH = ".specify/memory/constitution.md"

    def test_merged_is_done_and_carries_the_default_branch_approval_count(self):
        step = bootstrap_steps.detect_constitution(
            self.root, context(True, text=CONTEXT.format(approvals=2)))

        self.assertEqual(("done", 2), (step.state, step.details["required_approvals"]))

    def test_missing_names_the_skill_and_the_local_approval_count(self):
        self.write("project-context.toml", CONTEXT.format(approvals=3))

        step = bootstrap_steps.detect_constitution(self.root, context(False))

        self.assertEqual(("todo", False, 3),
                         (step.state, step.performable, step.details["required_approvals"]))
        self.assertIn("speckit-constitution", step.action["text"])
        self.assertIn("(3)", step.action["text"])
        self.assertEqual("docs/bootstrap.md#constitution", step.action["doc"])
        self.assertIn(self.PATH, step.title)

    def test_an_existing_file_is_done(self):
        self.write(self.PATH, "# C\n")
        self.write("project-context.toml", CONTEXT.format(approvals=0))

        step = bootstrap_steps.detect_constitution(self.root, context(None))

        self.assertEqual(("done", 0), (step.state, step.details["required_approvals"]))

    def test_without_a_readable_context_the_approval_count_is_unknown(self):
        step = bootstrap_steps.detect_constitution(self.root, context(False))

        self.assertEqual("todo", step.state)
        self.assertIsNone(step.details["required_approvals"])
        self.assertIn("speckit-constitution", step.action["text"])
        self.assertNotIn("(None)", step.action["text"])


class RoadmapStepTest(LocalFilesTestCase):
    def test_merged_is_done_without_reading_the_local_file(self):
        self.assertEqual("done", bootstrap_steps.detect_roadmap(self.root, context(True)).state)

    def test_missing_is_todo_naming_roadmap_write(self):
        step = bootstrap_steps.detect_roadmap(self.root, context(False))

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertIn("roadmap-write", step.action["text"])
        self.assertEqual("docs/bootstrap.md#roadmap", step.action["doc"])

    def test_invalid_is_todo_with_the_first_line_of_the_error(self):
        self.write("ROADMAP.md", "nothing useful\n")

        step = bootstrap_steps.detect_roadmap(self.root, context(None))

        self.assertEqual("todo", step.state)
        self.assertIn("invalid", step.title)
        self.assertNotIn("\n", step.title)

    def test_valid_is_done(self):
        self.write("ROADMAP.md", ROADMAP)

        self.assertEqual("done", bootstrap_steps.detect_roadmap(self.root, context(False)).state)


class SettingsGitHub(FakeGitHub):
    """FakeGitHub plus the repository visibility and a switch that refuses the rulesets GET."""

    def __init__(self, *args, visibility="PUBLIC", ruleset_error=None, visibility_error=False,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.visibility = visibility
        self.ruleset_error = ruleset_error
        self.ruleset_error_once = False
        self.visibility_error = visibility_error

    def __call__(self, argv, cwd=None):
        argv = list(argv)
        if argv == ["gh", "repo", "view", REPO, "--json", "visibility"]:
            self.calls.append(argv)
            if self.visibility_error:
                raise bootstrap_github.CommandError("HTTP 500: boom")
            return json.dumps({"visibility": self.visibility})
        if (self.ruleset_error and argv[:2] == ["gh", "api"] and "/rulesets" in argv[2]
                and "-X" not in argv):
            self.calls.append(argv)
            error = self.ruleset_error
            if self.ruleset_error_once:
                self.ruleset_error = None
            raise bootstrap_github.CommandError(error)
        return super().__call__(argv, cwd)


PLAN_REFUSAL = ("gh: Upgrade to GitHub Pro or make this repository public to enable this "
                "feature. (HTTP 403)")


class RulesetWarningTest(unittest.TestCase):
    def warn(self, **kwargs):
        run = SettingsGitHub(**kwargs)
        return bootstrap_steps.ruleset_warning(REPO, run), run

    def test_a_private_repository_on_a_plan_without_rulesets_gets_the_plan_hint(self):
        warning, _ = self.warn(visibility="PRIVATE", ruleset_error=PLAN_REFUSAL)

        self.assertEqual(bootstrap_github.PLAN_HINT, warning)

    def test_a_refusal_for_missing_rights_gets_the_admin_hint(self):
        warning, _ = self.warn(visibility="PRIVATE",
                               ruleset_error="gh: Resource not accessible (HTTP 403)")

        self.assertEqual(bootstrap_github.ADMIN_HINT, warning)

    def test_a_404_or_another_error_could_not_confirm_with_the_first_line(self):
        for error in ("gh: Not Found (HTTP 404)", "HTTP 500: server error\nmore"):
            with self.subTest(error=error):
                warning, _ = self.warn(visibility="PRIVATE", ruleset_error=error)

                self.assertIn("could not confirm that rulesets are available", warning)
                self.assertIn(error.splitlines()[0], warning)
                self.assertNotIn("more", warning)
                self.assertNotEqual(bootstrap_github.ADMIN_HINT, warning)

    def test_a_private_repository_whose_rulesets_are_readable_has_no_warning(self):
        warning, run = self.warn(visibility="PRIVATE")

        self.assertIsNone(warning)
        self.assertEqual(1, len([c for c in run.calls if "/rulesets" in " ".join(c)]))

    def test_a_public_repository_is_not_probed(self):
        warning, run = self.warn(visibility="PUBLIC", ruleset_error=PLAN_REFUSAL)

        self.assertIsNone(warning)
        self.assertEqual([], [c for c in run.calls if "/rulesets" in " ".join(c)])

    def test_unreadable_visibility_gives_no_warning(self):
        warning, _ = self.warn(visibility_error=True, ruleset_error=PLAN_REFUSAL)

        self.assertIsNone(warning)

    def test_the_probe_only_reads(self):
        _, run = self.warn(visibility="PRIVATE", ruleset_error=PLAN_REFUSAL)

        self.assertEqual([], run.writes)


class GitHubSettingsTest(LocalFilesTestCase):
    def setUp(self):
        super().setUp()
        self.write("project-context.toml", CONTEXT.format(approvals=1))

    def apply(self, run, approvals=1):
        """Bring the fake's repository in line with approvals, as `github --apply` would."""
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        (Path(other.name) / "project-context.toml").write_text(CONTEXT.format(approvals=approvals))
        with mock.patch("sys.stdout"):
            code = bootstrap.main(["github", "--apply"], root=Path(other.name), run=run,
                                  is_tty=False)
        self.assertEqual(0, code)
        run.writes.clear()

    def detect(self, run, ctx=None):
        return bootstrap_steps.detect_github_settings(
            self.root, run, REPO, ctx or context(False))

    def test_drift_is_todo_performable_and_changes_nothing(self):
        run = SettingsGitHub()

        step = self.detect(run)

        self.assertEqual(("github_settings", "todo", True), (step.id, step.state, step.performable))
        self.assertIn("differ", step.title)
        self.assertEqual("python3 scripts/bootstrap.py github", step.action["command"])
        self.assertEqual([], run.writes)

    def test_no_drift_is_done(self):
        run = SettingsGitHub()
        self.apply(run)

        step = self.detect(run)

        self.assertEqual("done", step.state)
        self.assertIsNone(step.action)
        self.assertEqual([], run.writes)

    def test_the_approval_count_comes_from_the_default_branch_once_merged(self):
        run = SettingsGitHub()
        self.apply(run, approvals=2)  # the repository follows the default branch's file
        self.write("project-context.toml", CONTEXT.format(approvals=1))  # stale local copy

        merged = self.detect(run, context(True, text=CONTEXT.format(approvals=2)))
        local = self.detect(run, context(False))

        self.assertEqual("done", merged.state)
        self.assertEqual("todo", local.state)

    def test_a_ruleset_stricter_than_the_context_is_a_decision_not_performable(self):
        run = SettingsGitHub()
        self.apply(run, approvals=2)  # the repository requires more than the local file says

        step = self.detect(run)

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertIn("--allow-weaken", step.action["text"])
        self.assertIn("stricter", step.action["text"])
        self.assertEqual(bootstrap_steps.SETTINGS_DOC, step.action["doc"])

    def test_unloadable_context_is_not_checked_with_the_first_line(self):
        self.write("project-context.toml", "[project]\nname = 'x'\n")

        step = self.detect(SettingsGitHub())

        self.assertEqual(("not_checked", False), (step.state, step.performable))
        self.assertIn("missing sections", step.title)
        self.assertIsNotNone(step.action)
        self.assertNotIn("\n", step.title)

    def test_a_missing_local_file_is_not_checked(self):
        (self.root / "project-context.toml").unlink()

        step = self.detect(SettingsGitHub())

        self.assertEqual("not_checked", step.state)
        self.assertEqual(bootstrap_steps.CONTEXT_DOC, step.action["doc"])

    def test_an_invalid_default_branch_file_is_not_checked_even_if_the_local_one_is_fine(self):
        step = self.detect(SettingsGitHub(), context(True, text="[project]\n"))

        self.assertEqual("not_checked", step.state)

    def test_a_gh_error_is_not_checked_with_the_first_line(self):
        step = self.detect(SettingsGitHub(board=False))

        self.assertEqual("not_checked", step.state)
        self.assertIn("no GitHub Project #3 found for acme", step.title)
        self.assertEqual(bootstrap_steps.SETTINGS_DOC, step.action["doc"])

    def test_a_board_owner_other_than_the_repository_owner_is_not_checked(self):
        self.write("project-context.toml",
                   CONTEXT.format(approvals=1).replace('owner = "acme"', 'owner = "other"'))

        step = self.detect(SettingsGitHub())

        self.assertEqual("not_checked", step.state)
        self.assertIn("github_project.owner must match", step.title)
        self.assertIsNotNone(step.action)

    def test_ruleset_warning_lands_in_details_for_a_private_repository_only(self):
        private = SettingsGitHub(visibility="PRIVATE", ruleset_error=PLAN_REFUSAL)

        step = self.detect(private)

        self.assertEqual(bootstrap_github.PLAN_HINT, step.details["ruleset_warning"])
        self.assertEqual("not_checked", step.state)
        self.assertEqual(bootstrap_steps.SETTINGS_DOC, step.action["doc"])
        self.assertNotIn("ruleset_warning", self.detect(SettingsGitHub()).details)

    def test_the_warning_is_kept_when_everything_else_is_done(self):
        run = SettingsGitHub(visibility="PRIVATE")
        self.apply(run)
        run.ruleset_error = "HTTP 500: server error"
        run.ruleset_error_once = True  # only the probe fails; the plan reads the rulesets

        step = self.detect(run)

        self.assertEqual("done", step.state)
        self.assertIn("could not confirm", step.details["ruleset_warning"])


class ProjectContextWarningTest(LocalFilesTestCase):
    def test_the_ruleset_warning_is_filled_on_project_context(self):
        run = SettingsGitHub(visibility="PRIVATE", ruleset_error=PLAN_REFUSAL)
        self.write("project-context.toml", CONTEXT.format(approvals=1))

        for ctx in (context(False), context(True, text=CONTEXT.format(approvals=1))):
            with self.subTest(merged=ctx.bootstrap_merged):
                step = bootstrap_steps.detect_project_context(self.root, run, REPO, ctx)

                self.assertEqual("done", step.state)
                self.assertEqual(bootstrap_github.PLAN_HINT, step.details["ruleset_warning"])

    def test_no_warning_key_for_a_public_repository(self):
        step = bootstrap_steps.detect_project_context(
            self.root, SettingsGitHub(), REPO, context(False))

        self.assertNotIn("ruleset_warning", step.details)


if __name__ == "__main__":
    unittest.main()

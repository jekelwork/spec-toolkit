import json
import urllib.parse
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap_github  # noqa: E402
import bootstrap_steps  # noqa: E402

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
                 tip_error=False, view_output=None, tip_output="abc123\n"):
        self.calls = []
        self.default = default
        self.present = bootstrap_steps.MERGE_MARKER_FILES if present is None else present
        self.error_on = error_on
        self.no_default = no_default
        self.tip_error = tip_error
        self.view_output = view_output
        self.tip_output = tip_output

    def __call__(self, argv, cwd=None):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:4] == ["gh", "repo", "view", REPO]:
            if self.no_default:
                raise bootstrap_github.CommandError("HTTP 500: boom")
            if self.view_output is not None:
                return self.view_output
            return json.dumps({"defaultBranchRef": {"name": self.default}})
        if argv[:2] == ["gh", "api"] and "/contents/" in argv[2]:
            path, _, ref = argv[2].partition("/contents/")[2].partition("?ref=")
            if urllib.parse.unquote(ref) != self.default:
                raise AssertionError(f"wrong ref {argv}")
            if path == self.error_on:
                raise bootstrap_github.CommandError("HTTP 500: server error")
            if path not in self.present:
                raise bootstrap_github.CommandError("gh: Not Found (HTTP 404)")
            return "{}"
        if argv[:2] == ["gh", "api"] and "/branches/" in argv[2]:
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
        for path in ("project-context.toml", ".specify/memory/constitution.md", "ROADMAP.md"):
            self.assertIn(["gh", "api", f"repos/{REPO}/contents/{path}?ref=trunk"], run.calls)
        self.assertIn(["gh", "api", f"repos/{REPO}/branches/trunk", "--jq", ".commit.sha"],
                      run.calls)

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


if __name__ == "__main__":
    unittest.main()

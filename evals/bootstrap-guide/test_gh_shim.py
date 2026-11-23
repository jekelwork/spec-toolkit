import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import gh_shim

HERE = Path(__file__).resolve().parent

TABLE = """
[[call]]
argv = ["issue", "list", "--label", "*"]
stdout = "listed\\n"

[[call]]
argv = ["api", "**"]
stdout = "api-any\\n"
stderr = "warn\\n"
exit = 4

[[call]]
argv = ["issue", "create", "**"]
stdout = "created\\n"
writes = true

[[call]]
argv = ["repo", "view"]
stdout = "repo\\n"
"""


class ShimCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.table = self.tmp / "gh.toml"
        self.table.write_text(TABLE)
        self.calls = self.tmp / "gh-calls.jsonl"

    def run_shim(self, *args, table=True):
        env = dict(os.environ)
        env.pop("EVAL_GH_TABLE", None)
        if table:
            env["EVAL_GH_TABLE"] = str(self.table)
        env["EVAL_GH_CALLS"] = str(self.calls)
        return subprocess.run(
            [sys.executable, str(HERE / "gh_shim.py"), *args],
            capture_output=True, text=True, env=env,
        )

    def body(self, query, name="body.json"):
        path = self.tmp / name
        path.write_text(json.dumps({"query": query, "variables": {}}))
        return str(path)

    def logged(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]


class MatchingTest(ShimCase):
    def test_glob_per_argument_and_stdout(self):
        result = self.run_shim("issue", "list", "--label", "bug")
        self.assertEqual((result.returncode, result.stdout), (0, "listed\n"))

    def test_pattern_count_must_equal_argument_count(self):
        result = self.run_shim("repo", "view", "extra")
        self.assertEqual(result.returncode, 1)

    def test_double_star_matches_any_remaining_arguments(self):
        for args in (("api",), ("api", "a", "b", "c")):
            result = self.run_shim(*args)
            self.assertEqual(result.returncode, 4)
            self.assertEqual(result.stdout, "api-any\n")
            self.assertEqual(result.stderr, "warn\n")

    def test_first_matching_entry_wins(self):
        self.table.write_text(
            '[[call]]\nargv = ["x", "**"]\nstdout = "one"\n'
            '[[call]]\nargv = ["x", "y"]\nstdout = "two"\n'
        )
        self.assertEqual(self.run_shim("x", "y").stdout, "one")
        self.assertEqual(self.logged()[0]["matched"], 0)

    def test_arguments_are_data_not_executed(self):
        marker = self.tmp / "pwned"
        self.run_shim("issue", "list", "--label", f"$(touch {marker}); `touch {marker}`")
        self.assertFalse(marker.exists())


class LogTest(ShimCase):
    def test_every_call_is_logged_with_index_and_writes(self):
        self.run_shim("issue", "list", "--label", "a")
        self.run_shim("issue", "create", "--title", "t")
        self.run_shim("nothing", "here")
        entries = self.logged()
        self.assertEqual(
            entries,
            [
                {"argv": ["issue", "list", "--label", "a"], "matched": 0, "writes": False,
                 "query": None},
                {"argv": ["issue", "create", "--title", "t"], "matched": 2, "writes": True,
                 "query": None},
                {"argv": ["nothing", "here"], "matched": None, "writes": True, "query": None},
            ],
        )

    def test_unmatched_call_exits_1_with_message(self):
        result = self.run_shim("pr", "merge", "5")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.strip(), "gh shim: no fixture for ['pr', 'merge', '5']")
        self.assertTrue(gh_shim.fixture_stale(self.calls))

    def test_matched_calls_leave_run_not_stale(self):
        self.run_shim("repo", "view")
        self.assertFalse(gh_shim.fixture_stale(self.calls))

    def test_stale_without_log_is_true(self):
        self.assertTrue(gh_shim.fixture_stale(self.tmp / "missing.jsonl"))

    def test_unreadable_table_is_logged_and_fails(self):
        self.table.write_text("[[call\nbroken")
        for bad in (self.table, self.tmp / "absent.toml"):
            self.table = bad
            result = self.run_shim("repo", "view")
            self.assertEqual(result.returncode, 1)
            self.assertIn("gh shim: cannot read table:", result.stderr)
        self.assertEqual([e["matched"] for e in self.logged()], [None, None])

    def test_missing_calls_variable_fails_closed(self):
        env = {k: v for k, v in os.environ.items() if k != "EVAL_GH_CALLS"}
        env["EVAL_GH_TABLE"] = str(self.table)
        result = subprocess.run([sys.executable, str(HERE / "gh_shim.py"), "repo", "view"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("EVAL_GH_CALLS", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_entry_writes_overrides_heuristic(self):
        self.table.write_text(
            '[[call]]\nargv = ["issue", "create", "**"]\nwrites = false\n'
            '[[call]]\nargv = ["repo", "view"]\nwrites = true\n'
        )
        self.run_shim("issue", "create", "--title", "t")
        self.run_shim("repo", "view")
        self.assertEqual([e["writes"] for e in self.logged()], [False, True])

    def test_missing_table_variable_fails_clearly(self):
        result = self.run_shim("repo", "view", table=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("EVAL_GH_TABLE", result.stderr)


class WritesClassificationTest(unittest.TestCase):
    def test_writes(self):
        for argv in (
            ["issue", "create", "--title", "x"],
            ["issue", "edit", "3"],
            ["pr", "create"],
            ["repo", "edit"],
            ["api", "-X", "POST", "repos/o/r/issues"],
            ["api", "repos/o/r", "-X", "patch"],
            ["api", "--method", "DELETE", "x"],
            ["api", "-XPUT", "x"],
            ["api", "graphql", "-f", "query=mutation { x }"],
            ["secret", "set", "NAME"],
            ["api", "graphql", "-F", "query=mutation { x }"],
            ["api", "graphql", "--raw-field=query=  mutation { x }"],
            ["api", "graphql", "-f", "query=# note\nMUTATION { x }"],
            ["api", "--paginate", "graphql", "-f", "query=mutation { x }"],
            ["api", "-H", "X: y", "graphql", "-f", "query=mutation { x }"],
            ["issue", "frobnicate"],
            ["api", "repos/o/r/issues", "-f", "title=x"],
            ["api", "x", "-F", "a=1"],
            ["api", "x", "--field", "a=1"],
            ["api", "x", "--field=a=1"],
            ["api", "x", "--raw-field", "a=1"],
            ["api", "x", "--raw-field=a=1"],
            ["api", "x", "--input", "body.json"],
            ["api", "x", "--input=body.json"],
            ["api", "x", "-fa=1"],
            ["project", "item-add", "1"],
        ):
            self.assertTrue(gh_shim.is_write(argv), argv)

    def test_reads(self):
        for argv in (
            ["issue", "list"],
            ["issue", "view", "3"],
            ["api", "repos/o/r"],
            ["api", "-X", "GET", "repos/o/r"],
            ["api", "graphql", "-f", "query=query { x }"],
            ["api", "graphql", "-F", "query=query { x }", "-f", "owner=o"],
            ["api", "graphql", "--raw-field", "query=  # c\nquery { x }"],
            ["api", "graphql", "-f", "query=query { x }", "-X", "GET"],
            ["api", "x", "--field", "a=b", "--method", "GET"],
            ["auth", "status"],
            ["repo", "view"],
            ["project", "list"],
            ["secret", "list"],
            ["--version"],
            ["version"],
            ["search", "issues", "x"],
            ["run", "watch", "5"],
            ["browse"],
            ["api", "--paginate", "graphql", "-f", "query=query { x }"],
            ["api", "-H", "X: y", "-q", ".data", "repos/o/r"],
        ):
            self.assertFalse(gh_shim.is_write(argv), argv)


class GraphqlInputTest(ShimCase):
    def test_input_body_is_classified_by_its_query(self):
        read = self.body("query Q { x }")
        write = self.body("# c\n mutation M { x }", "m.json")
        for flag in (["--input", read], [f"--input={read}"]):
            self.assertFalse(gh_shim.is_write(["api", "graphql", *flag]))
        for flag in (["--input", write], [f"--input={write}"]):
            self.assertTrue(gh_shim.is_write(["api", "graphql", *flag]))

    def test_unreadable_input_is_a_write(self):
        bad = self.tmp / "bad.json"
        bad.write_text("not json")
        for value in (str(self.tmp / "missing.json"), str(bad), "-"):
            self.assertTrue(gh_shim.is_write(["api", "graphql", "--input", value]), value)

    def test_entries_are_told_apart_by_query_regex(self):
        self.table.write_text(
            '[[call]]\nargv = ["api", "graphql", "--input", "*"]\n'
            'query = "board"\nstdout = "one"\n'
            '[[call]]\nargv = ["api", "graphql", "--input", "*"]\n'
            'query = "^mutation"\nstdout = "two"\nwrites = true\n'
        )
        a = self.run_shim("api", "graphql", "--input", self.body("# c\nquery board { x }"))
        b = self.run_shim("api", "graphql", "--input", self.body("mutation M { x }", "b.json"))
        c = self.run_shim("api", "graphql", "-f", "query=query other { x }")
        self.assertEqual((a.stdout, b.stdout, c.returncode), ("one", "two", 1))
        entries = self.logged()
        self.assertEqual([e["matched"] for e in entries], [0, 1, None])
        self.assertEqual([e["query"] for e in entries],
                         ["# c query board { x }", "mutation M { x }", "query other { x }"])

    def test_query_from_field_value_matches(self):
        self.table.write_text(
            '[[call]]\nargv = ["api", "graphql", "**"]\nquery = "viewer"\nstdout = "v"\n'
        )
        result = self.run_shim("api", "graphql", "--raw-field", "query=query { viewer }")
        self.assertEqual(result.stdout, "v")


class RecordCallsTest(ShimCase):
    def test_input_replaced_and_query_recorded(self):
        import tomllib

        path = self.tmp / "q.json"
        path.write_text(json.dumps({"query": "\n# c\nquery Q($a: Int) { x }"}))
        self.run_shim("api", "graphql", "--input", str(path))
        draft = tomllib.loads(gh_shim.record_calls(self.calls))["call"][0]
        self.assertEqual(draft["argv"], ["api", "graphql", "--input", "*"])
        self.assertEqual(draft["query"], "^" + re.escape("# c query Q($a: Int) { x }") + "$")

    def test_queries_sharing_a_first_line_each_match_only_themselves(self):
        import tomllib

        first = self.body("query Board {\n  a { id }\n}", "one.json")
        second = self.body("query Board {\n  b   { id }\n}", "two.json")
        for path in (first, second):
            self.run_shim("api", "graphql", "--input", path)
        self.table.write_text(gh_shim.record_calls(self.calls))
        drafts = tomllib.loads(self.table.read_text())["call"]
        self.assertNotEqual(drafts[0]["query"], drafts[1]["query"])
        self.calls.unlink()
        for path in (first, second):
            self.assertEqual(self.run_shim("api", "graphql", "--input", path).returncode, 0)
        self.assertEqual([e["matched"] for e in self.logged()], [0, 1])

    def test_non_ascii_round_trips(self):
        import tomllib

        title = "caf\u00e9 \U0001f600"
        self.run_shim("issue", "create", "--title", title)
        text = gh_shim.record_calls(self.calls)
        self.assertIn(title, text)
        self.assertEqual(tomllib.loads(text)["call"][0]["argv"][-1], title)

    def test_lists_calls_as_draft_toml_entries(self):
        self.run_shim("issue", "create", "--title", 'a "quoted" \\ title')
        self.run_shim("repo", "view")
        text = gh_shim.record_calls(self.calls)
        import tomllib

        drafts = tomllib.loads(text)["call"]
        self.assertEqual(drafts[0]["argv"], ["issue", "create", "--title", 'a "quoted" \\ title'])
        self.assertTrue(drafts[0]["writes"])
        self.assertEqual(drafts[1]["argv"], ["repo", "view"])
        self.assertNotIn("writes", drafts[1])

    def test_cli(self):
        self.run_shim("repo", "view")
        result = subprocess.run(
            [sys.executable, str(HERE / "gh_shim.py"), "record-calls", str(self.calls)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn('argv = ["repo", "view"]', result.stdout)


class WrapperTest(ShimCase):
    def test_wrapper_starts_shim_with_absolute_interpreter(self):
        bin_dir = self.tmp / "bin"
        wrapper = gh_shim.write_wrapper(bin_dir, sys.executable)
        self.assertEqual(wrapper, bin_dir / "gh")
        self.assertTrue(wrapper.stat().st_mode & stat.S_IXUSR)
        text = wrapper.read_text()
        self.assertIn(sys.executable, text)
        self.assertIn(str(HERE / "gh_shim.py"), text)
        self.assertIn(" -I ", text)
        env = dict(os.environ, EVAL_GH_TABLE=str(self.table), EVAL_GH_CALLS=str(self.calls))
        result = subprocess.run([str(wrapper), "repo", "view"], capture_output=True,
                                text=True, env=env)
        self.assertEqual((result.returncode, result.stdout), (0, "repo\n"))

    def test_relative_interpreter_is_rejected(self):
        with self.assertRaises(ValueError):
            gh_shim.write_wrapper(self.tmp / "bin", "python3")

    def test_old_interpreter_is_rejected(self):
        fake = self.tmp / "py"
        fake.write_text("#!/bin/sh\nexit 1\n")
        fake.chmod(0o755)
        with self.assertRaises(ValueError):
            gh_shim.write_wrapper(self.tmp / "bin", str(fake))


if __name__ == "__main__":
    unittest.main()

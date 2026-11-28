import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
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
        self.header = gh_shim.write_header(self.calls)

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
        """The call records (header and chain field left out)."""
        records = [json.loads(line) for line in self.calls.read_text().splitlines()]
        return [
            {k: v for k, v in r.items() if k != "prev"} for r in records if "header" not in r
        ]

    def lines(self):
        return self.calls.read_bytes().split(b"\n")[:-1]


def digest(line):
    return hashlib.sha256(line).hexdigest()


class TamperEvidentLogTest(ShimCase):
    """The call log is a hash chain behind a random header (research D19 decision 2)."""

    def three_calls(self):
        for args in (("repo", "view"), ("issue", "list", "--label", "a"), ("nothing", "here")):
            self.run_shim(*args)

    def write_lines(self, lines):
        self.calls.write_bytes(b"".join(line + b"\n" for line in lines))

    def assert_rejected(self, message=None):
        with self.assertRaises(ValueError) as caught:
            gh_shim.read_calls(self.calls, expect_header=self.header)
        self.assertNotIn("SECRET", str(caught.exception))
        if message:
            self.assertIn(message, str(caught.exception))

    def test_the_header_is_a_random_value_and_the_first_line(self):
        first = self.lines()[0]
        self.assertEqual(json.loads(first), {"header": self.header})
        self.assertRegex(self.header, r"^[0-9a-f]{32,}$")
        other = gh_shim.write_header(self.tmp / "other.jsonl")
        self.assertNotEqual(other, self.header)

    def test_the_header_is_never_written_over_an_existing_file(self):
        with self.assertRaises(FileExistsError):
            gh_shim.write_header(self.calls)

    def test_each_line_records_the_hash_of_the_line_before_it(self):
        self.three_calls()
        lines = self.lines()
        self.assertEqual(len(lines), 4)
        for before, after in zip(lines, lines[1:]):
            # The hash of the previous line's bytes, without its newline.
            self.assertEqual(json.loads(after)["prev"], digest(before))

    def test_the_chain_continues_after_unmatched_and_failed_calls(self):
        self.table.write_text("not toml [")
        self.run_shim("repo", "view")
        self.table.write_text(TABLE)
        self.run_shim("repo", "view")
        self.assertEqual(len(gh_shim.read_calls(self.calls, expect_header=self.header)), 2)

    def test_read_calls_returns_only_the_call_records(self):
        self.three_calls()
        calls = gh_shim.read_calls(self.calls, expect_header=self.header)
        self.assertEqual(calls, self.logged())
        self.assertEqual([c["argv"][0] for c in calls], ["repo", "issue", "nothing"])
        self.assertNotIn("prev", calls[0])
        self.assertEqual(gh_shim.read_calls(self.calls), calls)  # no header value to compare

    def test_a_log_with_only_the_header_has_no_calls(self):
        self.assertEqual(gh_shim.read_calls(self.calls, expect_header=self.header), [])

    def test_a_missing_or_changed_header_is_rejected(self):
        self.three_calls()
        lines = self.lines()
        self.write_lines(lines[1:])
        self.assert_rejected("header")
        self.write_lines([json.dumps({"header": "0" * 32}).encode(), *lines[1:]])
        self.assert_rejected("header")
        self.calls.write_bytes(b"")
        self.assert_rejected("header")
        with self.assertRaises(ValueError):  # without a value to compare, a header must exist
            gh_shim.read_calls(self.calls)

    def test_a_replaced_line_breaks_the_chain(self):
        self.three_calls()
        lines = self.lines()
        forged = json.loads(lines[2])
        forged["argv"] = ["SECRET", "forged"]
        self.write_lines([*lines[:2], json.dumps(forged).encode(), lines[3]])
        self.assert_rejected()

    def test_a_removed_or_reordered_line_breaks_the_chain(self):
        self.three_calls()
        lines = self.lines()
        for variant in (
            [lines[0], lines[1], lines[3]],
            [lines[0], lines[2], lines[1], lines[3]],
            [lines[0], lines[2], lines[3]],
        ):
            self.write_lines(variant)
            self.assert_rejected()

    def test_an_extra_line_is_rejected(self):
        self.three_calls()
        lines = self.lines()
        extras = [
            b"SECRET not json",
            b'{"argv": ["SECRET"], "matched": 0, "writes": false, "query": null}',
            json.dumps({"argv": ["SECRET"], "matched": 0, "writes": False, "query": None,
                        "prev": digest(lines[-1])}).encode()[:-1] + b', "x": 1}',
            json.dumps({"header": self.header}).encode(),
            b"[]",
            b'"SECRET"',
        ]
        for extra in extras:
            with self.subTest(extra=extra[:20]):
                self.write_lines([*lines, extra])
                self.assert_rejected()

    def test_a_line_that_is_not_a_shim_record_is_rejected_even_with_a_valid_chain(self):
        self.write_lines([self.lines()[0], json.dumps({"hello": "SECRET", "prev": digest(self.lines()[0])}).encode()])
        self.assert_rejected()

    def test_a_truncated_last_line_is_rejected(self):
        self.three_calls()
        data = self.calls.read_bytes()
        self.calls.write_bytes(data[:-1])  # newline gone
        self.assert_rejected("truncated")
        self.calls.write_bytes(data[:-20])
        self.assert_rejected()

    def test_a_forged_continuation_after_a_replaced_log_is_rejected(self):
        # A redirect from an allowed command replaces the log with that command's output.
        self.three_calls()
        self.calls.write_text("On branch main\nSECRET\n")
        self.assert_rejected()
        with self.assertRaises(ValueError):
            gh_shim.read_verified(self.calls, expect_header=self.header)

    def test_read_calls_shows_an_unmatched_call_and_checks_the_chain(self):
        self.three_calls()
        calls = gh_shim.read_calls(self.calls, expect_header=self.header)
        self.assertTrue(any(call["matched"] is None for call in calls))
        self.assertIn("repo", gh_shim.draft_entries(calls))
        lines = self.lines()
        self.write_lines([lines[0], lines[2], lines[3]])
        with self.assertRaises(ValueError):
            gh_shim.read_calls(self.calls, expect_header=self.header)

    def test_the_cli_verifies_the_chain_and_that_a_header_exists(self):
        self.three_calls()
        cli = [sys.executable, str(HERE / "gh_shim.py"), "record-calls", str(self.calls)]
        ok = subprocess.run(cli, capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn('argv = ["repo", "view"]', ok.stdout)
        self.assertNotIn(self.header, ok.stdout)
        lines = self.lines()
        for variant in ([lines[0], lines[2], lines[3]], lines[1:]):
            self.write_lines(variant)
            bad = subprocess.run(cli, capture_output=True, text=True)
            self.assertEqual(bad.returncode, 1)
            self.assertEqual(bad.stdout, "")
            self.assertIn("calls log", bad.stderr)
            self.assertNotIn("Traceback", bad.stderr)


class ConcurrentLogTest(ShimCase):
    """Several shim processes log at once and the chain stays valid (research D19 decision 2)."""

    def test_parallel_processes_keep_the_chain_valid(self):
        def worker(number):
            for call in range(20):
                self.run_shim("repo", "view", f"--n{number}-{call}")

        for _ in range(2):
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(worker, range(4)))
        calls = gh_shim.read_calls(self.calls, expect_header=self.header)
        self.assertEqual(len(calls), 160)

    def test_only_the_end_of_the_log_is_read(self):
        class Counting:
            def __init__(self, handle):
                self.handle, self.read_bytes = handle, 0

            def seek(self, *args):
                return self.handle.seek(*args)

            def tell(self):
                return self.handle.tell()

            def read(self, *args):
                data = self.handle.read(*args)
                self.read_bytes += len(data)
                return data

        gh_shim_log = self.tmp / "big.jsonl"
        line = b'{"argv": ["x"], "matched": 0, "writes": false, "query": null, "prev": "p"}\n'
        gh_shim_log.write_bytes(line * 30000)  # about 2.4 MB
        with open(gh_shim_log, "rb") as raw:
            handle = Counting(raw)
            last = gh_shim._last_line(handle)
        self.assertEqual(last, line[:-1])
        self.assertLess(handle.read_bytes, 300 * 1024)

    def test_a_line_longer_than_the_tail_window_is_still_chained(self):
        gh_shim._log(self.calls, ["x" * 200_000], 0, False, None)
        gh_shim._log(self.calls, ["repo", "view"], 0, False, None)
        calls = gh_shim.read_calls(self.calls, expect_header=self.header)
        self.assertEqual([c["argv"][0] for c in calls], ["x" * 200_000, "repo"])

    def test_an_oversize_log_is_still_appended_to_and_the_readers_flag_it(self):
        gh_shim._log(self.calls, ["x" * (gh_shim.MAX_LOG_BYTES + 10)], 0, False, None)
        before = self.calls.stat().st_size
        gh_shim._log(self.calls, ["repo", "view"], 0, False, None)
        self.assertGreater(self.calls.stat().st_size, before)
        with self.assertRaisesRegex(ValueError, "larger than"):
            gh_shim.read_calls(self.calls, expect_header=self.header)


class DeepNestingTest(ShimCase):
    def test_a_deeply_nested_line_is_tampering_not_an_uncaught_error(self):
        deep = b"[" * 200_000
        lines = self.calls.read_bytes()
        for content in (lines + deep + b"\n", deep + b"\n"):
            self.calls.write_bytes(content)
            for func in (gh_shim.read_calls, gh_shim.read_verified):
                with self.subTest(func=func.__name__, header_line=content.startswith(deep)):
                    with self.assertRaises(ValueError):
                        func(self.calls)


class RecordsOnlyCopyTest(ShimCase):
    """`record-calls` takes the sidecar log or the run directory's records-only copy."""

    def cli(self, path):
        return subprocess.run(
            [sys.executable, str(HERE / "gh_shim.py"), "record-calls", str(path)],
            capture_output=True, text=True,
        )

    def copy(self, records):
        path = self.tmp / "copy.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        return path

    def test_the_records_only_copy_is_accepted(self):
        self.run_shim("repo", "view")
        self.run_shim("issue", "create", "--title", "t")
        copy = self.copy(self.logged())
        result = self.cli(copy)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, self.cli(self.calls).stdout)
        self.assertIn('argv = ["repo", "view"]', result.stdout)

    def test_an_empty_copy_has_no_calls(self):
        empty = self.tmp / "empty.jsonl"
        empty.write_text("")
        result = self.cli(empty)
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    BAD_FIELDS = {
        "argv-item-not-string": {"argv": [1]},
        "query-not-string": {"query": 5},
        "writes-not-bool": {"writes": "yes"},
        "matched-not-int": {"matched": "a"},
        "matched-bool": {"matched": True},
    }

    def test_a_copy_with_wrongly_typed_fields_is_rejected_without_a_traceback(self):
        record = {"argv": ["x"], "matched": 0, "writes": False, "query": None}
        for name, change in self.BAD_FIELDS.items():
            with self.subTest(name):
                result = self.cli(self.copy([{**record, **change}]))
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)

    def test_a_chained_log_with_wrongly_typed_fields_is_rejected(self):
        header = self.calls.read_bytes().split(b"\n")[0]
        record = {"argv": ["x"], "matched": 0, "writes": False, "query": None}
        for name, change in self.BAD_FIELDS.items():
            with self.subTest(name):
                forged = {**record, **change, "prev": gh_shim._line_digest(header)}
                self.calls.write_bytes(header + b"\n" + json.dumps(forged).encode() + b"\n")
                with self.assertRaises(ValueError):
                    gh_shim.read_calls(self.calls)
                result = self.cli(self.calls)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("Traceback", result.stderr)

    def test_a_copy_that_is_not_records_is_rejected_without_a_traceback(self):
        record = {"argv": ["x"], "matched": 0, "writes": False, "query": None}
        for name, content in {
            "extra-key": json.dumps({**record, "prev": "p"}) + "\n",
            "not-json": "nope\n",
            "argv-not-list": json.dumps({**record, "argv": "x"}) + "\n",
            "nested": "[" * 200_000 + "\n",
            "partial": json.dumps(record),
        }.items():
            with self.subTest(name):
                path = self.tmp / f"{name}.jsonl"
                path.write_text(content)
                result = self.cli(path)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)

    def test_a_sidecar_log_is_still_verified(self):
        self.run_shim("repo", "view")
        self.run_shim("repo", "view")
        lines = self.lines()
        self.calls.write_bytes(b"\n".join([lines[0], lines[2]]) + b"\n")
        self.assertEqual(self.cli(self.calls).returncode, 1)


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
        self.assertEqual([c["matched"] for c in gh_shim.read_calls(self.calls)], [None])

    def test_matched_calls_leave_no_unmatched_record(self):
        self.run_shim("repo", "view")
        self.assertTrue(all(c["matched"] is not None for c in gh_shim.read_calls(self.calls)))

    def test_a_missing_log_cannot_be_read(self):
        with self.assertRaises(FileNotFoundError):
            gh_shim.read_calls(self.tmp / "missing.jsonl")

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


class SafeReadTest(ShimCase):
    def test_normal_log_is_read(self):
        self.run_shim("repo", "view")
        st = self.calls.stat()
        calls = gh_shim.read_calls(self.calls, expect_inode=(st.st_dev, st.st_ino))
        self.assertIn("repo", gh_shim.draft_entries(calls))

    def test_symlinked_log_is_rejected(self):
        self.run_shim("repo", "view")
        link = self.tmp / "link.jsonl"
        link.symlink_to(self.calls)
        for func in (gh_shim.read_calls, gh_shim.read_calls_or_copy):
            with self.assertRaisesRegex(ValueError, "symlink"):
                func(link)

    def test_fifo_is_rejected_without_hanging(self):
        fifo = self.tmp / "fifo.jsonl"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, "regular file"):
            gh_shim.read_calls(fifo)

    def test_oversize_log_is_rejected(self):
        big = self.tmp / "big.jsonl"
        with big.open("wb") as handle:
            handle.truncate(gh_shim.MAX_LOG_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "larger than"):
            gh_shim.read_calls(big)

    def test_inode_mismatch_is_rejected(self):
        self.run_shim("repo", "view")
        st = self.calls.stat()
        with self.assertRaisesRegex(ValueError, "replaced"):
            gh_shim.read_calls(self.calls, expect_inode=(st.st_dev, st.st_ino + 1))
        with self.assertRaisesRegex(ValueError, "replaced"):
            gh_shim.read_verified(self.calls, expect_inode=(st.st_dev, st.st_ino + 1))


class ReadLockTest(ShimCase):
    """The harness takes a shared lock for its read, so it never sees a half-written line."""

    def hold_exclusive(self):
        import fcntl

        handle = open(self.calls, "rb")
        self.addCleanup(handle.close)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def test_a_lock_released_within_the_bound_lets_the_read_succeed(self):
        import fcntl
        import threading

        self.run_shim("repo", "view")
        handle = self.hold_exclusive()
        timer = threading.Timer(0.1, lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN))
        timer.start()
        self.addCleanup(timer.cancel)
        with mock.patch.object(gh_shim, "LOCK_WAIT_SECONDS", 5), mock.patch.object(
            gh_shim, "LOCK_RETRY_SECONDS", 0.01
        ):
            calls = gh_shim.read_calls(self.calls)
        self.assertEqual(len(calls), 1)

    def assert_no_lingering_lock(self):
        import fcntl

        with open(self.calls, "rb") as probe:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_no_shared_lock_lingers_after_a_successful_read(self):
        self.run_shim("repo", "view")
        gh_shim.read_calls(self.calls)
        self.assert_no_lingering_lock()

    def test_no_shared_lock_lingers_after_a_read_that_raises(self):
        self.run_shim("repo", "view")
        data = self.calls.read_bytes()
        self.calls.write_bytes(data[:-5])
        with self.assertRaises(ValueError):
            gh_shim.read_calls(self.calls)
        self.assert_no_lingering_lock()

    def test_no_shared_lock_lingers_after_a_lock_timeout(self):
        import fcntl

        self.run_shim("repo", "view")
        handle = self.hold_exclusive()
        with mock.patch.object(gh_shim, "LOCK_WAIT_SECONDS", 0.1), mock.patch.object(
            gh_shim, "LOCK_RETRY_SECONDS", 0.01
        ):
            with self.assertRaisesRegex(ValueError, "lock"):
                gh_shim.read_calls(self.calls)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        self.assert_no_lingering_lock()

    def test_a_lock_held_past_the_bound_fails_closed(self):
        self.run_shim("repo", "view")
        self.hold_exclusive()
        with mock.patch.object(gh_shim, "LOCK_WAIT_SECONDS", 0.1), mock.patch.object(
            gh_shim, "LOCK_RETRY_SECONDS", 0.01
        ):
            with self.assertRaisesRegex(ValueError, "lock"):
                gh_shim.read_calls(self.calls)


class RecordCallsTest(ShimCase):
    def draft(self):
        return gh_shim.draft_entries(gh_shim.read_calls(self.calls))

    def test_a_lone_surrogate_in_argv_or_query_still_gives_valid_utf8_toml(self):
        import tomllib

        calls = [{"argv": ["pr", "view", "x\udcffy"], "query": "q\ud800", "writes": False}]
        text = gh_shim.draft_entries(calls)
        text.encode("utf-8")
        entry = tomllib.loads(text)["call"][0]
        self.assertEqual(entry["argv"], ["pr", "view", "x\\udcffy"])
        self.assertIn("\\ud800", entry["query"])

    def test_input_replaced_and_query_recorded(self):
        import tomllib

        path = self.tmp / "q.json"
        path.write_text(json.dumps({"query": "\n# c\nquery Q($a: Int) { x }"}))
        self.run_shim("api", "graphql", "--input", str(path))
        draft = tomllib.loads(self.draft())["call"][0]
        self.assertEqual(draft["argv"], ["api", "graphql", "--input", "*"])
        self.assertEqual(draft["query"], "^" + re.escape("# c query Q($a: Int) { x }") + "$")

    def test_queries_sharing_a_first_line_each_match_only_themselves(self):
        import tomllib

        first = self.body("query Board {\n  a { id }\n}", "one.json")
        second = self.body("query Board {\n  b   { id }\n}", "two.json")
        for path in (first, second):
            self.run_shim("api", "graphql", "--input", path)
        self.table.write_text(self.draft())
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
        text = self.draft()
        self.assertIn(title, text)
        self.assertEqual(tomllib.loads(text)["call"][0]["argv"][-1], title)

    def test_lists_calls_as_draft_toml_entries(self):
        self.run_shim("issue", "create", "--title", 'a "quoted" \\ title')
        self.run_shim("repo", "view")
        text = self.draft()
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

    def test_copied_shim_works_standalone(self):
        import shutil

        lib = self.tmp / "bin" / "lib"
        lib.mkdir(parents=True)
        copy = lib / "gh_shim.py"
        shutil.copy(HERE / "gh_shim.py", copy)
        wrapper = gh_shim.write_wrapper(self.tmp / "bin", sys.executable, shim_path=str(copy))
        text = wrapper.read_text()
        self.assertIn(str(copy), text)
        self.assertNotIn(str(HERE / "gh_shim.py"), text)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env.update(EVAL_GH_TABLE=str(self.table), EVAL_GH_CALLS=str(self.calls))
        result = subprocess.run([str(wrapper), "repo", "view"], capture_output=True,
                                text=True, env=env, cwd=self.tmp)
        self.assertEqual((result.returncode, result.stdout), (0, "repo\n"))
        self.assertEqual(self.logged()[0]["matched"], 3)

    def test_bad_shim_path_is_rejected(self):
        for bad in ("gh_shim.py", str(self.tmp / "missing.py"), str(self.tmp)):
            with self.assertRaises(ValueError, msg=bad):
                gh_shim.write_wrapper(self.tmp / "bin", sys.executable, shim_path=bad)

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

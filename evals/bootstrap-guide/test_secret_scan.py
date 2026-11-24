"""Tests for the secret scan of a finished run's files (research D9, FR-010).

The scanner is injected as a runner, so most tests need no node; the last classes use the real
`scripts/check-secrets.mjs` when node is installed. Token-shaped values are built from parts.
"""

import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import secret_scan
import test_turns
import turns

# Token shaped like a real GitHub token, but not the harness's fake one.
REAL_SHAPED = "".join(("gh", "p_", "Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0Kk1Ll2"))
# A client secret assignment, assembled so the pre-commit scan does not see one.
FAKE_CLIENT_SECRET = "".join(("CLIENT_", "SECRET=", '"', "abcdefghijklmnopqrstuvwxyz0123", '"'))
FILES = {
    "agent_stderr_log": "agent-stderr.log",
    "gh_calls_jsonl": "gh-calls.jsonl",
    "fixture_draft_toml": "fixture-draft.toml",
}
HIT = "✖ transcript.jsonl:1: looks like a GitHub personal token\n"


class Result:
    def __init__(self, returncode=0, stderr=""):
        self.returncode, self.stderr = returncode, stderr


class FakeRunner:
    """Records the call and the files the scanner would have read at that moment."""

    def __init__(self, result=None, error=None):
        self.result, self.error = result or Result(), error
        self.calls, self.seen, self.modes = [], {}, {}

    def __call__(self, argv, cwd, timeout, env=None):
        self.calls.append((argv, cwd, timeout))
        self.env = env
        for name in argv[2:]:
            self.seen[name] = Path(cwd, name).read_bytes()
        self.modes["dir"] = stat.S_IMODE(os.stat(cwd).st_mode)
        if self.error:
            raise self.error
        return self.result


class ScanCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name) / "out"
        self.out.mkdir()
        self.token = turns.fake_token()

    def write(self, name, text, binary=False):
        path = self.out / name
        path.write_bytes(text if binary else text.encode("utf-8"))
        return path

    def run_files(self, transcript="{}\n", meta=None, **others):
        self.write("transcript.jsonl", transcript)
        self.write("meta.json", json.dumps({"gh_call_offsets": [], **(meta or {})}))
        for name, text in others.items():
            self.write(FILES[name], text)


class CopyTest(ScanCase):
    def test_the_fake_token_from_the_metadata_is_replaced_by_a_placeholder(self):
        line = json.dumps({"text": f"my token is {self.token}"}) + "\n"
        self.run_files(transcript=line, meta={"fake_token": self.token})
        runner = FakeRunner()
        secret_scan.scan_run(self.out, runner=runner)
        seen = runner.seen["transcript.jsonl"].decode()
        self.assertNotIn(self.token, seen)
        self.assertIn(secret_scan.PLACEHOLDER, seen)

    def test_a_real_looking_token_is_left_in_the_copy(self):
        self.run_files(transcript=REAL_SHAPED + "\n", meta={"fake_token": self.token})
        runner = FakeRunner()
        secret_scan.scan_run(self.out, runner=runner)
        self.assertIn(REAL_SHAPED, runner.seen["transcript.jsonl"].decode())

    def test_every_ignore_marker_is_removed_from_the_copy(self):
        marker = "secret-scan:" + "ignore"
        self.run_files(
            transcript=f"a {marker} b\nc {marker} {marker}\n", agent_stderr_log=f"x {marker}\n"
        )
        runner = FakeRunner()
        secret_scan.scan_run(self.out, runner=runner)
        for name, data in runner.seen.items():
            self.assertNotIn(marker, data.decode(), name)

    def test_the_files_of_the_run_are_not_changed_by_the_scan(self):
        marker = "secret-scan:" + "ignore"
        line = f"{marker} {self.token}\n"
        self.run_files(transcript=line, meta={"fake_token": self.token})
        secret_scan.scan_run(self.out, runner=FakeRunner())
        self.assertEqual((self.out / "transcript.jsonl").read_text(), line)

    def test_nul_bytes_do_not_hide_text_from_the_scanner(self):
        self.run_files()
        self.write("agent-stderr.log", b"a\x00" + REAL_SHAPED.encode(), binary=True)
        runner = FakeRunner()
        secret_scan.scan_run(self.out, runner=runner)
        self.assertNotIn(b"\x00", runner.seen["agent-stderr.log"])

    def test_every_text_file_of_the_run_is_scanned_and_meta_is_not(self):
        self.run_files(
            gh_calls_jsonl="{}\n", agent_stderr_log="e\n", fixture_draft_toml="[[call]]\n"
        )
        self.write("result.json", "{}")
        runner = FakeRunner()
        secret_scan.scan_run(self.out, runner=runner)
        self.assertEqual(
            sorted(runner.seen),
            ["agent-stderr.log", "fixture-draft.toml", "gh-calls.jsonl", "transcript.jsonl"],
        )

    def test_a_missing_optional_file_is_fine(self):
        self.run_files()
        self.assertEqual(secret_scan.scan_run(self.out, runner=FakeRunner()), [])


class DecodedViewTest(ScanCase):
    def test_a_marker_cannot_be_rebuilt_by_removing_an_inner_one(self):
        marker = "secret-scan:" + "ignore"
        nested = "secret-scan:" + marker + "ignore"
        self.run_files(transcript=nested + "\n")
        runner = FakeRunner()
        secret_scan.scan_run(self.out, runner=runner)
        self.assertNotIn(marker, runner.seen["transcript.jsonl"].decode())

    def test_jsonl_strings_are_appended_unescaped_to_the_same_copy(self):
        secret = FAKE_CLIENT_SECRET + "\n"
        line = json.dumps({"content": [{"text": secret}], "n": 1}) + "\n"
        self.run_files(transcript=line + "not json\n", gh_calls_jsonl=line)
        runner = FakeRunner()
        secret_scan.scan_run(self.out, runner=runner)
        for name in ("transcript.jsonl", "gh-calls.jsonl"):
            seen = runner.seen[name].decode()
            self.assertIn(line, seen)
            self.assertIn(secret, seen)
        self.assertIn("not json\n", runner.seen["transcript.jsonl"].decode())

    def test_the_decoded_view_gets_the_token_replacement_and_marker_removal(self):
        marker = "secret-scan:" + "ignore"
        line = json.dumps({"t": f'{marker} "{self.token}"'}) + "\n"
        self.run_files(transcript=line, meta={"fake_token": self.token})
        runner = FakeRunner()
        secret_scan.scan_run(self.out, runner=runner)
        seen = runner.seen["transcript.jsonl"].decode()
        self.assertNotIn(self.token, seen)
        self.assertNotIn(marker, seen)

    def test_toml_strings_are_appended_unescaped(self):
        secret = FAKE_CLIENT_SECRET
        draft = f"[[call]]\nargv = [{json.dumps(secret)}]\n"
        self.run_files(fixture_draft_toml=draft)
        runner = FakeRunner()
        secret_scan.scan_run(self.out, runner=runner)
        seen = runner.seen["fixture-draft.toml"].decode()
        self.assertIn(draft, seen)
        self.assertIn(secret, seen)

    def test_a_toml_file_that_does_not_parse_is_scanned_raw(self):
        self.run_files(fixture_draft_toml="[[call\nnot toml\n")
        runner = FakeRunner()
        secret_scan.scan_run(self.out, runner=runner)
        self.assertEqual(runner.seen["fixture-draft.toml"].decode(), "[[call\nnot toml\n")

    def test_strings_are_collected_from_a_structure_nested_far_beyond_the_stack(self):
        value = "CLIENT_SECRET=x"
        for _ in range(20000):
            value = [value]
        self.assertEqual(list(secret_scan._strings({"k": value})), ["k", "CLIENT_SECRET=x"])

    def test_a_parser_that_runs_out_of_stack_fails_closed(self):
        self.run_files(transcript='{"a": 1}\n')
        with mock.patch.object(secret_scan.json, "loads", side_effect=RecursionError):
            with self.assertRaises(secret_scan.SecretScanError):
                secret_scan.scan_run(self.out, runner=FakeRunner())
        self.assertFalse((self.out / "transcript.jsonl").exists())

    def test_a_lone_surrogate_in_a_logged_argument_does_not_abort_the_scan(self):
        line = json.dumps({"argv": ["x" + chr(0xDCFF)]}) + "\n"
        self.assertIn("udcff", line)  # the escape, as the shim logs a non-UTF-8 byte
        self.run_files(gh_calls_jsonl=line)
        runner = FakeRunner()
        secret_scan.scan_run(self.out, runner=runner)
        self.assertIn("gh-calls.jsonl", runner.seen)

    def test_other_files_are_not_decoded(self):
        line = json.dumps({"t": 'say "hi"'}) + "\n"
        self.run_files(transcript="{}\n", agent_stderr_log=line)
        runner = FakeRunner()
        secret_scan.scan_run(self.out, runner=runner)
        self.assertEqual(runner.seen["agent-stderr.log"].decode(), line)


class RunnerTest(ScanCase):
    def test_the_scanner_gets_a_minimal_environment(self):
        self.run_files()
        runner = FakeRunner()
        with mock.patch.dict(
            os.environ, {"NODE_OPTIONS": "--require x", "AWS_SECRET_ACCESS_KEY": "y"}
        ):
            secret_scan.scan_run(self.out, runner=runner)
        self.assertLessEqual(set(runner.env), {"PATH", "HOME", "LANG"})
        self.assertIn("PATH", runner.env)
        self.assertNotIn("NODE_OPTIONS", runner.env)

    def test_the_scanner_runs_with_an_argv_list_a_timeout_and_a_private_directory(self):
        self.run_files()
        runner = FakeRunner()
        secret_scan.scan_run(self.out, runner=runner)
        argv, cwd, timeout = runner.calls[0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[0], "node")
        self.assertEqual(Path(argv[1]), secret_scan.SCANNER)
        self.assertEqual(Path(argv[1]).name, "check-secrets.mjs")
        self.assertGreater(timeout, 0)
        self.assertEqual(runner.modes["dir"], 0o700)
        self.assertFalse(Path(cwd).resolve().is_relative_to(self.out.resolve()))

    def test_the_scanned_copy_is_removed_afterwards(self):
        self.run_files()
        runner = FakeRunner()
        secret_scan.scan_run(self.out, runner=runner)
        self.assertFalse(Path(runner.calls[0][1]).exists())

    def test_the_scanned_copy_is_removed_when_the_scanner_fails(self):
        self.run_files()
        runner = FakeRunner(error=OSError("boom"))
        with self.assertRaises(secret_scan.SecretScanError):
            secret_scan.scan_run(self.out, runner=runner)
        self.assertFalse(Path(runner.calls[0][1]).exists())


class OutcomeTest(ScanCase):
    def test_a_clean_run_keeps_its_transcript(self):
        self.run_files(transcript="hello\n")
        self.assertEqual(secret_scan.scan_run(self.out, runner=FakeRunner()), [])
        self.assertEqual((self.out / "transcript.jsonl").read_text(), "hello\n")

    def test_a_hit_deletes_the_transcript_and_reports_the_file(self):
        self.run_files(agent_stderr_log="e\n")
        found = secret_scan.scan_run(self.out, runner=FakeRunner(Result(1, HIT)))
        self.assertEqual(found, ["transcript.jsonl"])
        self.assertFalse((self.out / "transcript.jsonl").exists())
        self.assertTrue((self.out / "agent-stderr.log").exists())

    def test_a_hit_also_deletes_the_fixture_draft_derived_from_the_call_log(self):
        self.run_files(gh_calls_jsonl="{}\n", fixture_draft_toml="[[call]]\n")
        hit = "✖ gh-calls.jsonl:1: looks like a client secret assignment\n"
        secret_scan.scan_run(self.out, runner=FakeRunner(Result(1, hit)))
        for name in ("transcript.jsonl", "gh-calls.jsonl", "fixture-draft.toml"):
            self.assertFalse((self.out / name).exists(), name)

    def test_a_hit_in_another_file_deletes_that_file_too(self):
        self.run_files(agent_stderr_log="e\n")
        hit = "✖ agent-stderr.log:1: looks like a GitHub personal token\n"
        found = secret_scan.scan_run(self.out, runner=FakeRunner(Result(1, hit)))
        self.assertEqual(found, ["agent-stderr.log"])
        self.assertFalse((self.out / "agent-stderr.log").exists())
        self.assertFalse((self.out / "transcript.jsonl").exists())  # a hit anywhere

    def test_an_unreadable_hit_report_is_a_scanner_failure_that_deletes_every_file(self):
        self.run_files(agent_stderr_log="e\n")
        with self.assertRaises(secret_scan.SecretScanError):
            secret_scan.scan_run(self.out, runner=FakeRunner(Result(1, "weird")))
        self.assertEqual(list(self.out.glob("*.log")) + list(self.out.glob("transcript*")), [])

    def test_a_report_naming_no_scanned_file_is_a_scanner_failure(self):
        self.run_files()
        hit = "✖ elsewhere.txt:1: looks like a GitHub personal token\n"
        with self.assertRaises(secret_scan.SecretScanError):
            secret_scan.scan_run(self.out, runner=FakeRunner(Result(1, hit)))
        self.assertFalse((self.out / "transcript.jsonl").exists())

    def test_a_scanner_that_cannot_run_deletes_every_scanned_file(self):
        self.run_files(
            gh_calls_jsonl="{}\n", agent_stderr_log="e\n", fixture_draft_toml="[[call]]\n"
        )
        with self.assertRaises(secret_scan.SecretScanError):
            secret_scan.scan_run(self.out, runner=FakeRunner(Result(2, "bad")))
        for name in secret_scan.FILES:
            self.assertFalse((self.out / name).exists(), name)
        self.assertTrue((self.out / "meta.json").exists())

    def test_an_error_while_copying_deletes_the_files_and_raises(self):
        self.run_files(agent_stderr_log="e\n")
        with mock.patch.object(secret_scan.tempfile, "mkdtemp", side_effect=OSError("full")):
            with self.assertRaises(secret_scan.SecretScanError):
                secret_scan.scan_run(self.out, runner=FakeRunner())
        for name in secret_scan.FILES:
            self.assertFalse((self.out / name).exists(), name)

    def test_a_file_over_the_size_limit_is_a_failure_and_nothing_is_kept(self):
        self.run_files(agent_stderr_log="e\n")
        runner = FakeRunner()
        with mock.patch.object(secret_scan, "MAX_BYTES", 2):
            with self.assertRaises(secret_scan.SecretScanError):
                secret_scan.scan_run(self.out, runner=runner)
        self.assertEqual(runner.calls, [])
        for name in secret_scan.FILES:
            self.assertFalse((self.out / name).exists(), name)

    def test_the_size_limit_is_32_mib(self):
        self.assertEqual(secret_scan.MAX_BYTES, 32 * 1024 * 1024)

    def test_the_hit_report_never_carries_the_matched_text(self):
        self.run_files()
        found = secret_scan.scan_run(self.out, runner=FakeRunner(Result(1, HIT + REAL_SHAPED)))
        self.assertNotIn(REAL_SHAPED, repr(found))

    def test_a_scanner_error_deletes_the_transcript_and_raises(self):
        for result in (Result(2, "node: bad"), Result(127, "")):
            with self.subTest(code=result.returncode):
                self.run_files()
                with self.assertRaises(secret_scan.SecretScanError):
                    secret_scan.scan_run(self.out, runner=FakeRunner(result))
                self.assertFalse((self.out / "transcript.jsonl").exists())

    def test_node_missing_and_timeout_raise_and_delete_the_transcript(self):
        import subprocess

        for error in (FileNotFoundError("node"), subprocess.TimeoutExpired("node", 1)):
            with self.subTest(error=type(error).__name__):
                self.run_files()
                with self.assertRaises(secret_scan.SecretScanError):
                    secret_scan.scan_run(self.out, runner=FakeRunner(error=error))
                self.assertFalse((self.out / "transcript.jsonl").exists())


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class RealScannerTest(ScanCase):
    def test_a_marked_real_looking_token_is_still_found(self):
        marker = "secret-scan:" + "ignore"
        self.run_files(transcript=json.dumps({"t": f"{marker} {REAL_SHAPED}"}) + "\n")
        found = secret_scan.scan_run(self.out)
        self.assertEqual(found, ["transcript.jsonl"])
        self.assertFalse((self.out / "transcript.jsonl").exists())

    def test_a_nested_marker_does_not_hide_a_token(self):
        marker = "secret-scan:" + "ignore"
        nested = "secret-scan:" + marker + "ignore"
        self.run_files(transcript=json.dumps({"t": f"{nested} {REAL_SHAPED}"}) + "\n")
        self.assertEqual(secret_scan.scan_run(self.out), ["transcript.jsonl"])

    def test_a_json_escaped_client_secret_is_found(self):
        secret = FAKE_CLIENT_SECRET + "\n"
        self.run_files(transcript=json.dumps({"content": secret}) + "\n")
        self.assertEqual(secret_scan.scan_run(self.out), ["transcript.jsonl"])

    def test_a_toml_escaped_client_secret_in_the_draft_alone_is_found(self):
        secret = FAKE_CLIENT_SECRET
        draft = f"[[call]]\nargv = [{json.dumps(secret)}]\n"
        self.run_files(fixture_draft_toml=draft)
        self.assertEqual(secret_scan.scan_run(self.out), ["fixture-draft.toml"])
        self.assertFalse((self.out / "transcript.jsonl").exists())

    def test_a_secret_in_the_call_log_removes_the_draft_too(self):
        secret = FAKE_CLIENT_SECRET
        calls = json.dumps({"argv": [secret]}) + "\n"
        draft = f"[[call]]\nargv = [{json.dumps(secret)}]\n"
        self.run_files(gh_calls_jsonl=calls, fixture_draft_toml=draft)
        self.assertEqual(secret_scan.scan_run(self.out), ["gh-calls.jsonl", "fixture-draft.toml"])
        self.assertFalse((self.out / "fixture-draft.toml").exists())

    def test_a_secret_after_a_deeply_nested_part_is_found_or_the_scan_fails(self):
        secret = FAKE_CLIENT_SECRET
        line = '{"n": ' + "[" * 2000 + "]" * 2000 + ', "t": ' + json.dumps(secret) + "}\n"
        self.run_files(transcript=line)
        try:
            found = secret_scan.scan_run(self.out)
        except secret_scan.SecretScanError:
            return
        self.assertEqual(found, ["transcript.jsonl"])

    def test_the_minimal_environment_lets_node_run(self):
        self.run_files(transcript="hello\n")
        with mock.patch.dict(os.environ, {"NODE_OPTIONS": "--no-such-flag"}):
            self.assertEqual(secret_scan.scan_run(self.out), [])

    def test_the_fake_token_alone_is_clean(self):
        line = json.dumps({"t": self.token}) + "\n"
        self.run_files(transcript=line, meta={"fake_token": self.token})
        self.assertEqual(secret_scan.scan_run(self.out), [])
        self.assertEqual((self.out / "transcript.jsonl").read_text(), line)

    def test_without_the_metadata_the_fake_token_would_be_found(self):
        self.run_files(transcript=json.dumps({"t": self.token}) + "\n")
        self.assertEqual(secret_scan.scan_run(self.out), ["transcript.jsonl"])


class RunIntegrationTest(test_turns.TurnTestCase):
    def play_with(self, runner, text="hello", turn_list=None, **kw):
        launcher = self.make_launcher([test_turns.reply(text)])
        run = turns.Run(
            test_turns.scenario(turn_list or [test_turns.turn("say", "hi")]),
            1,
            self.paths,
            launcher,
            self.out,
            timeout=20,
            secret_runner=runner,
            **kw,
        )
        self.run_obj = run
        return run.play()

    def test_a_hit_makes_the_run_unscored_secret_found_and_deletes_the_transcript(self):
        result = self.play_with(FakeRunner(Result(1, HIT)))
        self.assertFalse(result["scored"])
        self.assertEqual(result["unscored_reason"], "secret_found")
        self.assertEqual(result["rules"], [])
        self.assertFalse((self.out / "transcript.jsonl").exists())
        self.assertEqual(json.loads(self.read("result.json"))["unscored_reason"], "secret_found")
        self.assertEqual(self.run_obj.state, "unscored")

    def test_a_clean_run_keeps_the_transcript_and_is_scored(self):
        runner = FakeRunner()
        result = self.play_with(runner)
        self.assertTrue(result["scored"])
        self.assertTrue((self.out / "transcript.jsonl").exists())
        self.assertIn("transcript.jsonl", runner.seen)

    def test_the_scan_sees_the_files_before_the_rules_read_them(self):
        runner = FakeRunner()
        self.play_with(
            runner, turn_list=[test_turns.turn("say", "token {fake_token}")]
        )
        seen = runner.seen["transcript.jsonl"].decode()
        self.assertNotIn(self.run_obj.meta["fake_token"], seen)
        self.assertIn(secret_scan.PLACEHOLDER, seen)

    def test_a_scanner_that_cannot_run_stops_the_evaluation(self):
        with self.assertRaises(turns.RunSetupError):
            self.play_with(FakeRunner(Result(2, "bad")))
        self.assertFalse((self.out / "transcript.jsonl").exists())

    def test_a_setup_failure_leaves_no_unscanned_files(self):
        for error in (turns.RunSetupError("no sandbox"), OSError("disk")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(turns.Run, "_drive", side_effect=error):
                    with self.assertRaises(type(error)):
                        self.play_with(FakeRunner())
                self.assertFalse((self.out / "transcript.jsonl").exists())

    def test_an_error_in_finish_before_the_scan_leaves_no_unscanned_files(self):
        with mock.patch.object(turns.Run, "_finish", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.play_with(FakeRunner())
        for name in secret_scan.FILES:
            self.assertFalse((self.out / name).exists(), name)

    def test_a_hit_in_an_already_unscored_run_still_wins(self):
        result = self.play_with(
            FakeRunner(Result(1, HIT)),
            turn_list=[test_turns.turn("say", "hi"), test_turns.turn("say", "x", "question")],
        )
        self.assertEqual(result["unscored_reason"], "secret_found")
        self.assertFalse((self.out / "transcript.jsonl").exists())


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class RunWithRealScannerTest(test_turns.TurnTestCase):
    def setUp(self):
        super().setUp()
        self.fake_node.stop()  # this class wants the real scanner

    def play_text(self, text):
        launcher = self.make_launcher([test_turns.reply(text)])
        run = turns.Run(
            test_turns.scenario([test_turns.turn("say", "token {fake_token}")]),
            1,
            self.paths,
            launcher,
            self.out,
            timeout=20,
        )
        return run.play()

    def test_the_agent_echoing_the_fake_token_is_clean(self):
        result = self.play_text(f"you sent {turns.fake_token()}")
        self.assertTrue(result["scored"] or result["unscored_reason"] != "secret_found")
        self.assertTrue((self.out / "transcript.jsonl").exists())

    def test_a_real_looking_token_in_the_reply_is_found(self):
        marker = "secret-scan:" + "ignore"
        result = self.play_text(f"{marker} {REAL_SHAPED}")
        self.assertEqual(result["unscored_reason"], "secret_found")
        self.assertFalse((self.out / "transcript.jsonl").exists())


if __name__ == "__main__":
    unittest.main()

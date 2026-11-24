"""Tests for the `run_shown` token check and execution (research D5)."""

import dataclasses
import importlib.util
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import harness

BASE = "python3 scripts/bootstrap.py start --no-input"
DIGEST = "a" * 64

ACCEPTED = [
    BASE,
    f"! {BASE}",
    f"{BASE} --report-exit",
    f"{BASE} --confirm review@{DIGEST}",
    f"{BASE} --issue 123",
    f"{BASE} --review-notes --full-dry-run",
    f"{BASE} --description 'A short text' --required-approvals 1 --project-number 3",
    f"{BASE} --create-board --owner acme --name my-project",
    f"{BASE} --report-exit --confirm review@{DIGEST} --issue 7 --guided",
    f"{BASE} --guided",
    f"{BASE} --agents claude",
    f"{BASE} --agents claude,copilot,codex",
    f"{BASE} --report-exit --agents codex",
    f"{BASE} --agents codex --report-exit",
]

# Each of these must be rejected; the list is the hostile-string list of T026.
HOSTILE = [
    f"{BASE}; rm -rf ~",
    f"{BASE};rm -rf ~",
    f"{BASE} && echo hi",
    f"{BASE} || echo hi",
    f"{BASE} | cat",
    f"{BASE} > out.txt",
    f"{BASE} < /etc/passwd",
    f"{BASE} & echo hi",
    f"{BASE} $(echo hi)",
    f"{BASE} `echo hi`",
    f"{BASE} --description '$(rm -rf ~)'",
    f'{BASE} --description "`rm -rf ~`"',
    f"{BASE} --description 'a; rm -rf ~'",
    f"{BASE} --description 'a\nb'",
    f"{BASE} --description 'a\\nb'",
    f"{BASE}\nrm -rf ~",
    f"{BASE} ; echo hi",
    "rm -rf ~",
    "python3 -c 'print(1)'",
    "python3 scripts/bootstrap.py start",
    "python3 scripts/bootstrap.py init --no-input",
    "python3 ../scripts/bootstrap.py start --no-input",
    "/usr/bin/python3 scripts/bootstrap.py start --no-input",
    f"{BASE} 'unterminated",
    f"{BASE} --report-exit --report-exit",
    f"{BASE} --issue 1 --issue 2",
    f"{BASE} --issue abc",
    f"{BASE} --issue",
    f"{BASE} --confirm review",
    f"{BASE} --confirm review@xyz",
    f"{BASE} --confirm review@{DIGEST} --confirm other@{DIGEST}",
    f"{BASE} --unknown",
    f"{BASE} extra",
    f"{BASE} --guided --report-exit",
    f"{BASE} --guided --guided",
    f"{BASE} --description",
    f"{BASE} --description --owner",
    f"{BASE} --required-approvals many",
    f"{BASE} --project-number x1",
    f"{BASE} --create-board yes",
    f"{BASE} --create-board --project-number 3",
    f"{BASE} --project-number 3 --create-board",
    f"{BASE} --owner -acme",
    f"{BASE} --owner acme_corp",
    f"{BASE} --owner {'a' * 40}",
    f"{BASE} --owner a/b",
    f"{BASE} --name ..",
    f"{BASE} --name .",
    f"{BASE} --name 'has space'",
    f"{BASE} --name {'a' * 101}",
    f"{BASE} --description 'a\u0085b'",  # C1 control (NEL)
    f"{BASE} --description 'a‮b'",  # bidi override
    f"{BASE} --description 'a​b'",  # zero-width space
    f"{BASE} --description 'a b'",  # line separator
    f"{BASE} --description 'a b'",  # paragraph separator
    f"{BASE} --description '﻿a'",  # BOM
    f"{BASE} --agents",
    f"{BASE} --agents claude --agents codex",
    f"{BASE} --agents gemini",
    f"{BASE} --agents claude,",
    f"{BASE} --agents claude;codex",
    f"{BASE} --agents claude --confirm review@{DIGEST}",
    f"{BASE} --agents claude --guided",
    f"{BASE} --agents claude --description x",
    f"{BASE} --agents claude --issue 1",
    f"{BASE} --report-exit --agents claude --review-notes",
]


LABELS = [
    "Run this in your terminal:",
    "Run this here (the `!` runs it in this session and its output lands in the chat):",
    "**Run this here:**",
    "Run this here:",
]
PREVIEW = "Here is what will change:\n\n```text\nrepo: o/r\nstep: review\n```\n\n"


def message(command, label="Run this in your terminal:"):
    return f"{label}\n\n```\n{command}\n```\n"


class ParseTest(unittest.TestCase):
    def test_accepted_commands(self):
        for command in ACCEPTED:
            with self.subTest(command=command):
                argv = harness.parse_shown_command(message(command))
                self.assertEqual(argv[:4], BASE.split())
                self.assertNotIn("!", argv[0])

    def test_rejected_commands(self):
        for command in HOSTILE:
            with self.subTest(command=command):
                with self.assertRaises(harness.ShownCommandRejected):
                    harness.parse_shown_command(message(command))

    def test_quoted_value_is_one_token(self):
        argv = harness.parse_shown_command(message(f"{BASE} --description 'A short text'"))
        self.assertEqual(argv[-2:], ["--description", "A short text"])

    def test_code_block_after_label_in_last_message(self):
        text = "Intro\n\nRun this in your terminal:\n\n```bash\n! " + BASE + "\n```\nThen reply.\n"
        self.assertEqual(harness.parse_shown_command(text)[0], "python3")

    def test_every_command_label_wording(self):
        for label in LABELS:
            with self.subTest(label=label):
                self.assertEqual(
                    harness.parse_shown_command(message(BASE, label)), BASE.split()
                )

    def test_preview_block_and_labelled_command_takes_the_command(self):
        text = PREVIEW + message(f"! {BASE} --report-exit", "Run this here:")
        self.assertEqual(harness.parse_shown_command(text), [*BASE.split(), "--report-exit"])

    def test_preview_block_alone_or_unlabelled_block_is_rejected(self):
        for text in [
            "no block here",
            PREVIEW,
            f"```\n{BASE}\n```",
            PREVIEW + f"```\n{BASE}\n```\n",
            "Run this in your terminal:\n\nno block follows\n",
            "Run this in this chat:\n\n```\n/roadmap-write\n```\n",
            "Do this on GitHub:\n\n```\nhttps://example.invalid\n```\n",
            message(BASE, "Please run this in your terminal:"),
            message(BASE, "Run this in your terminal: and more:"),
            "Run this in your terminal:\n\ntext\n\n```\n" + BASE + "\n```\n",
        ]:
            with self.subTest(text=text):
                with self.assertRaises(harness.ShownCommandRejected):
                    harness.parse_shown_command(text)

    def test_label_inside_a_preview_block_does_not_count(self):
        preview = "What changes:\n\n```text\nRun this here:\nnot a label\n```\n\n"
        text = preview + message(BASE, "Run this here:")
        self.assertEqual(harness.parse_shown_command(text), BASE.split())

    def test_a_label_in_an_indented_code_block_does_not_count(self):
        text = f"Example:\n\n    Run this here:\n\n{message(BASE, 'Run this here:')}"
        self.assertEqual(harness.parse_shown_command(text), BASE.split())
        with self.assertRaises(harness.ShownCommandRejected):
            harness.parse_shown_command(f"    Run this here:\n\n    ```\n    {BASE}\n    ```\n")
        tab = f"\tRun this here:\n\n{message(BASE, 'Run this here:')}"
        self.assertEqual(harness.parse_shown_command(tab), BASE.split())

    def test_a_label_in_an_html_comment_does_not_count(self):
        for comment in (
            "<!-- Run this here: -->\n\n",
            "<!--\nRun this here:\n-->\n\n",
            "<!-- note\nRun this in your terminal:\nmore -->\n\n",
        ):
            with self.subTest(comment=comment):
                text = comment + message(BASE, "Run this here:")
                self.assertEqual(harness.parse_shown_command(text), BASE.split())
        with self.assertRaises(harness.ShownCommandRejected):
            harness.parse_shown_command(f"<!--\nRun this here:\n```\n{BASE}\n```\n-->\n")

    def test_a_comment_opened_after_another_one_closed_on_the_same_line_hides_the_label(self):
        for opening in ("<!-- a --> <!--", "<!-- a --><!-- b --> <!--", "<!-- a --> text <!--"):
            with self.subTest(opening=opening):
                text = f"{opening}\nRun this here:\n-->\n\n" + message(BASE, "Run this here:")
                self.assertEqual(harness.parse_shown_command(text), BASE.split())
        closed = f"<!-- a --> <!-- b -->\n\n{message(BASE, 'Run this here:')}"
        self.assertEqual(harness.parse_shown_command(closed), BASE.split())

    def test_tilde_fences_hold_the_command(self):
        text = f"Run this here:\n\n~~~\n{BASE}\n~~~\n"
        self.assertEqual(harness.parse_shown_command(text), BASE.split())

    def test_a_four_backtick_fence_holds_a_shorter_fence_line(self):
        self.assertEqual(
            harness.parse_shown_command(f"Run this here:\n\n````\n{BASE}\n````\n"), BASE.split()
        )
        with self.assertRaises(harness.ShownCommandRejected):  # the inner ``` is a second line
            harness.parse_shown_command(f"Run this here:\n\n````\n{BASE}\n```\n````\n")

    def test_a_label_inside_a_nested_fence_does_not_count(self):
        preview = "~~~~text\nRun this here:\n```\nrm -rf ~\n```\n~~~~\n\n"
        text = preview + message(BASE, "Run this here:")
        self.assertEqual(harness.parse_shown_command(text), BASE.split())

    def test_up_to_three_spaces_may_indent_a_fence_but_four_may_not(self):
        text = f"Run this here:\n\n   ```\n   {BASE}\n   ```\n"
        self.assertEqual(harness.parse_shown_command(text), BASE.split())
        with self.assertRaises(harness.ShownCommandRejected):
            harness.parse_shown_command(f"Run this here:\n\n    ```\n    {BASE}\n    ```\n")

    def test_two_command_labels_are_rejected(self):
        text = message(BASE) + "\n" + message(BASE, "Run this here:")
        with self.assertRaises(harness.ShownCommandRejected):
            harness.parse_shown_command(text)

    def test_unclosed_block_is_reported(self):
        with self.assertRaises(harness.ShownCommandRejected) as caught:
            harness.parse_shown_command("Run this here:\n\n```\n" + BASE + "\n")
        self.assertIn("unclosed", str(caught.exception))

    def test_crlf_messages_work(self):
        text = message(BASE).replace("\n", "\r\n")
        self.assertEqual(harness.parse_shown_command(text), BASE.split())

    def test_block_must_hold_one_line(self):
        with self.assertRaises(harness.ShownCommandRejected):
            harness.parse_shown_command(f"Run this here:\n```\n{BASE}\n{BASE}\n```")

    def test_rejection_names_a_reason(self):
        with self.assertRaises(harness.ShownCommandRejected) as caught:
            harness.parse_shown_command(message("rm -rf ~"))
        self.assertTrue(str(caught.exception))


class FakeLauncher:
    """Records `run` calls; the real one is `launchers.LocalLauncher.run`."""

    def __init__(self, result=("", "", False)):
        self.result = result
        self.calls = []
        self.sandboxes = []

    def run(self, argv, cwd, env, timeout, *, sandbox):
        self.calls.append((argv, cwd, env, timeout))
        self.sandbox = sandbox
        self.sandboxes.append(sandbox)
        return self.result


class ProcessLauncher:
    """A plain-subprocess stand-in with the launcher's `run` contract, for end-to-end checks."""

    def run(self, argv, cwd, env, timeout, *, sandbox):
        try:
            done = subprocess.run(
                argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return "", "", True
        return done.stdout, done.stderr, False


class RunTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name).resolve() / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        self.marker = self.repo / "ran.txt"
        (self.repo / "scripts" / "bootstrap.py").write_text(
            "import os, sys\n"
            "open('ran.txt', 'a').write(repr(sys.argv[1:]) + '\\n')\n"
            "print('out ' + os.environ.get('EVAL_MARK', ''))\n"
            "print('err', file=sys.stderr)\n"
            "sys.exit(3)\n"
        )
        (self.repo / ".git" / "hooks").mkdir(parents=True)
        (self.repo / ".git" / "config").write_text("[core]\n")
        (self.repo / ".git" / "hooks" / "pre-commit.sample").write_text("#!/bin/sh\n")
        (self.repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        python_dir = str(Path(sys.executable).parent)
        self.env = {"PATH": f"{python_dir}:/usr/bin:/bin", "EVAL_MARK": "m"}
        root = self.repo.parent
        self.bin_dir = root / "bin"
        self.bin_dir.mkdir()
        (root / "harness").mkdir()
        (root / "shown-home").mkdir()
        (root / "shown-tmp").mkdir()
        gh_calls = root / "harness" / "gh-calls.jsonl"
        gh_calls.write_text("")
        stat = os.lstat(gh_calls)
        self.git_real = os.path.realpath(shutil.which("git"))
        (root / "origin.git" / "hooks").mkdir(parents=True)
        (root / "origin.git" / "hooks" / "pre-receive.sample").write_text("#!/bin/sh\n")
        (root / "origin.git" / "config").write_text("[core]\n\tbare = true\n")
        (root / "harness" / "gh.toml").write_text("")
        self.exec_path = os.path.realpath(
            subprocess.run(["git", "--exec-path"], capture_output=True, text=True).stdout.strip()
        )
        self.paths = harness.RunPaths(
            run_root=root,
            repo=self.repo,
            sidecar=root / "harness",
            bin_dir=self.bin_dir,
            origin=root / "origin.git",
            gh_table=root / "harness" / "gh.toml",
            gh_calls=gh_calls,
            toolkit_ref=None,
            python=sys.executable,
            shown_home=root / "shown-home",
            shown_tmp=root / "shown-tmp",
            fingerprints=harness.fingerprint(self.repo, root / "origin.git"),
            repo_real=os.path.realpath(self.repo),
            bin_real=os.path.realpath(self.bin_dir),
            gh_calls_real=os.path.realpath(gh_calls),
            gh_calls_id=(stat.st_dev, stat.st_ino),
            git_real=self.git_real,
            gh_table_real=os.path.realpath(root / "harness" / "gh.toml"),
            gitconfig_real=os.path.realpath(root / "harness") + "/harness.gitconfig",
            gh_config_real=os.path.realpath(root / "harness") + "/gh-config",
            origin_real=os.path.realpath(root / "origin.git"),
            git_exec_real=self.exec_path,
        )

    def shown(self, text, launcher, **kwargs):
        return harness.run_shown(text, launcher, self.paths, self.env, **kwargs)

    def baseline(self):
        return harness.fingerprint(self.repo, self.paths.run_root / "origin.git")

    def test_accepted_command_runs_in_repo_and_formats_output(self):
        launcher = ProcessLauncher()
        text = self.shown(message(f"! {BASE} --report-exit"), launcher)
        self.assertEqual(
            self.marker.read_text(), "['start', '--no-input', '--report-exit']\n"
        )
        self.assertIn(f"<bash-input>{BASE} --report-exit</bash-input>", text)
        self.assertIn("<bash-stdout>out m\n</bash-stdout>", text)
        self.assertIn("<bash-stderr>err\n</bash-stderr>", text)
        self.assertRegex(text, r"^<bash-input>.*</bash-input><bash-stdout>")

    def test_uses_the_given_env_only(self):
        with mock.patch.dict("os.environ", {"EVAL_MARK": "leaked"}):
            self.env["EVAL_MARK"] = "own"
            text = self.shown(message(BASE), ProcessLauncher())
        self.assertIn("out own", text)

    def test_runs_through_the_launcher_as_an_argv_list(self):
        launcher = FakeLauncher()
        self.shown(message(f"{BASE} --description 'x y'"), launcher, timeout=7)
        self.assertEqual(len(launcher.calls), 1)
        argv, cwd, env, timeout = launcher.calls[0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[-2:], ["--description", "x y"])
        self.assertEqual(cwd, str(self.repo))
        self.assertEqual({k: v for k, v in env.items() if k in self.env}, self.env)
        self.assertEqual(timeout, 7)

    def test_python_environment_is_locked_down_with_fresh_directories_per_call(self):
        launcher = FakeLauncher()
        self.shown(message(BASE), launcher)
        self.shown(message(BASE), launcher)
        first, second = launcher.calls[0][2], launcher.calls[1][2]
        for env in (first, second):
            self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertEqual(env["PYTHONNOUSERSITE"], "1")
            self.assertNotIn("PYTHONSAFEPATH", env)
            prefix = Path(env["PYTHONPYCACHEPREFIX"])
            self.assertEqual(prefix.parent, self.paths.shown_tmp.resolve())
            self.assertEqual(list(prefix.iterdir()), [])
        self.assertNotEqual(first["PYTHONPYCACHEPREFIX"], second["PYTHONPYCACHEPREFIX"])
        boxes = launcher.sandboxes
        self.assertNotEqual(boxes[0].home_override, boxes[1].home_override)
        self.assertNotEqual(boxes[0].tmpdir, boxes[1].tmpdir)
        for box in boxes:
            self.assertEqual(Path(box.home_override).parent, self.paths.shown_home.resolve())
            self.assertEqual(Path(box.tmpdir).parent, self.paths.shown_tmp.resolve())
            self.assertEqual(os.listdir(box.home_override), [])

    def test_a_planted_unchecked_pyc_is_not_executed(self):
        scripts = self.repo / "scripts"
        helper = scripts / "helper.py"
        helper.write_text("VALUE = 'source'\n")
        evil = scripts / "evil_source.py"
        evil.write_text("VALUE = 'planted'\n")
        cache = Path(importlib.util.cache_from_source(str(helper)))
        cache.parent.mkdir(exist_ok=True)
        py_compile.compile(
            str(evil),
            cfile=str(cache),
            doraise=True,
            invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        )
        evil.unlink()
        (scripts / "bootstrap.py").write_text("import helper\nprint(helper.VALUE)\n")
        self.paths = dataclasses.replace(self.paths, fingerprints=self.baseline())
        text = self.shown(message(BASE), ProcessLauncher())
        self.assertIn("<bash-stdout>source\n</bash-stdout>", text)
        self.assertEqual(harness.check_integrity(self.paths), [])  # bytecode is not hashed

    def test_python3_is_replaced_by_the_absolute_interpreter(self):
        launcher = FakeLauncher()
        self.shown(message(BASE), launcher)
        self.assertEqual(launcher.calls[0][0][0], sys.executable)
        self.assertEqual(launcher.calls[0][0][1:4], BASE.split()[1:4])
        self.shown(message(BASE), launcher, python="/opt/py/bin/python3")
        self.assertEqual(launcher.calls[1][0][0], "/opt/py/bin/python3")

    def test_sandbox_grants_what_the_shim_and_git_need(self):
        launcher = FakeLauncher()
        self.shown(message(BASE), launcher)
        box = launcher.sandbox
        paths = self.paths
        for needed in (paths.gh_table_real, paths.gitconfig_real, paths.gh_config_real):
            self.assertIn(needed, list(box.allow_read))
            self.assertNotIn(needed, list(box.allow_write))
        self.assertIn(paths.origin_real, list(box.allow_read))
        self.assertIn(paths.origin_real, list(box.allow_write))
        self.assertIn(paths.git_exec_real, list(box.exec_paths))
        self.assertNotIn(paths.gh_calls_real, list(box.allow_write))
        self.assertEqual(
            sorted(box.allow_write), sorted([paths.repo_real, paths.origin_real])
        )

    def test_sandbox_spec(self):
        launcher = FakeLauncher()
        self.shown(message(BASE), launcher, home="/Users/maintainer")
        box = launcher.sandbox
        self.assertEqual(box.home, "/Users/maintainer")
        for path in (self.repo, self.bin_dir, Path(sys.executable).resolve().parent.parent):
            self.assertIn(str(path), [str(p) for p in box.allow_read])
        # Only the stored resolved strings; the call log is a literal file rule, never a subpath.
        self.assertEqual(
            sorted(box.allow_write), sorted([self.paths.repo_real, self.paths.origin_real])
        )
        self.assertEqual(list(box.write_files), [self.paths.gh_calls_real])
        self.assertNotIn(self.paths.gh_calls_real, list(box.allow_write))
        exec_paths = list(box.exec_paths)
        for path in (self.paths.bin_real, os.path.realpath(sys.executable), self.git_real):
            self.assertIn(path, exec_paths)
        # The launcher grants tmpdir and home_override read+write on its own.
        self.assertEqual(Path(box.tmpdir).parent, self.paths.shown_tmp.resolve())
        self.assertEqual(Path(box.home_override).parent, self.paths.shown_home.resolve())
        self.assertIsInstance(box, harness.launchers.SandboxSpec)
        for value in (*box.allow_read, *box.allow_write, *box.write_files, *exec_paths):
            self.assertTrue(os.path.isabs(value) and value == os.path.realpath(value), value)
        prefix = os.path.dirname(os.path.dirname(os.path.realpath(sys.executable)))
        self.assertIn(prefix, exec_paths)

    def test_launcher_output_is_formatted(self):
        launcher = FakeLauncher(("o\n", "e\n", False))
        text = self.shown(message(BASE), launcher)
        self.assertIn("<bash-stdout>o\n</bash-stdout><bash-stderr>e\n</bash-stderr>", text)

    def test_rejected_commands_never_reach_the_launcher(self):
        launcher = FakeLauncher()
        for command in HOSTILE:
            with self.subTest(command=command):
                with self.assertRaises(harness.ShownCommandRejected):
                    self.shown(message(command), launcher)
        self.assertEqual(launcher.calls, [])
        self.assertFalse(self.marker.exists())

    def test_timeout_is_reported_in_stderr(self):
        launcher = FakeLauncher(("partial", "", True))
        text = self.shown(message(BASE), launcher, timeout=5)
        self.assertRegex(text, re.compile(r"<bash-stderr>.*timed out.*</bash-stderr>", re.S))
        self.assertIn("<bash-stdout>partial</bash-stdout>", text)


class IntegrityTest(unittest.TestCase):
    """Tampering with `scripts/` or `.git` stops the run before any command runs."""

    setUp = RunTest.setUp
    shown = RunTest.shown
    baseline = RunTest.baseline

    def test_origin_hooks_and_config_are_watched(self):
        origin = self.paths.run_root / "origin.git"
        self.assertIn("origin.git/hooks/pre-receive.sample", self.paths.fingerprints)
        self.assertEqual(harness.check_integrity(self.paths), [])
        # An ordinary push-time write to the origin (objects, refs) is fine.
        (origin / "refs").mkdir()
        (origin / "refs" / "x").write_text("0" * 40)
        (origin / "HEAD").write_text("ref: refs/heads/main\n")
        self.assertEqual(harness.check_integrity(self.paths), [])

    def test_planted_origin_hook_is_tampering(self):
        hook = self.paths.run_root / "origin.git" / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        launcher = FakeLauncher()
        with self.assertRaises(harness.RunTampered) as caught:
            self.shown(message(BASE), launcher)
        self.assertEqual(caught.exception.changed, ["origin.git/hooks/pre-receive"])
        self.assertEqual(launcher.calls, [])

    def test_changed_or_removed_origin_hook_is_tampering(self):
        sample = self.paths.run_root / "origin.git" / "hooks" / "pre-receive.sample"
        sample.write_text("#!/bin/sh\necho changed\n")
        self.assertEqual(harness.check_integrity(self.paths), [
            "origin.git/hooks/pre-receive.sample"
        ])
        sample.unlink()
        self.assertEqual(harness.check_integrity(self.paths), [
            "origin.git/hooks/pre-receive.sample"
        ])

    def test_planted_origin_config_is_tampering(self):
        config = self.paths.run_root / "origin.git" / "config"
        for text in (
            "[core]\n\tbare = true\n\thooksPath = /tmp/h\n",
            "[core]\n\tbare = true\n\tfsmonitor = x\n",
            "[include]\n\tpath = /tmp/x\n",
            '[url "file:///x"]\n\tinsteadOf = y\n',
            "[core\n",
        ):
            with self.subTest(text=text):
                config.write_text(text)
                launcher = FakeLauncher()
                with self.assertRaises(harness.RunTampered) as caught:
                    self.shown(message(BASE), launcher)
                self.assertEqual(caught.exception.changed, ["origin.git/config"])
                self.assertEqual(launcher.calls, [])
        config.write_text(
            "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = true\n"
        )
        self.assertEqual(harness.check_integrity(self.paths), [])

    def test_origin_config_that_is_a_fifo_or_missing_is_tampering(self):
        config = self.paths.run_root / "origin.git" / "config"
        config.unlink()
        os.mkfifo(config)
        self.assertEqual(self.checked(), ["origin.git/config"])
        config.unlink()
        self.assertEqual(self.checked(), ["origin.git/config"])

    def tamper(self, action):
        action(self.repo)
        launcher = FakeLauncher()
        with self.assertRaises(harness.RunTampered) as caught:
            self.shown(message(BASE), launcher)
        self.assertEqual(launcher.calls, [])
        self.assertTrue(caught.exception.changed)
        self.assertEqual(harness.check_integrity(self.paths), caught.exception.changed)
        self.assertNotIsInstance(caught.exception, harness.ShownCommandRejected)

    def test_untouched_run_passes(self):
        self.assertEqual(harness.check_integrity(self.paths), [])
        (self.repo / "scripts" / "__pycache__").mkdir()
        (self.repo / "scripts" / "__pycache__" / "x.pyc").write_bytes(b"1")
        (self.repo / "other.txt").write_text("free to change")
        (self.repo / ".git" / "HEAD").write_text("ref: refs/heads/other\n")
        self.assertEqual(harness.check_integrity(self.paths), [])

    def test_changed_script(self):
        self.tamper(lambda r: (r / "scripts" / "bootstrap.py").write_text("print('evil')\n"))

    def test_added_script(self):
        self.tamper(lambda r: (r / "scripts" / "extra.py").write_text("x\n"))

    def test_removed_script(self):
        self.tamper(lambda r: (r / "scripts" / "bootstrap.py").unlink())

    def test_dangerous_git_config(self):
        self.tamper(lambda r: (r / ".git" / "config").write_text("[core]\n\tfsmonitor = x\n"))

    def test_git_files_that_redirect_the_repository(self):
        git_dir = self.repo / ".git"
        for rel in ("config.worktree", "commondir", "gitdir", "objects/info/alternates"):
            with self.subTest(rel=rel):
                path = git_dir / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("/tmp/elsewhere\n")
                launcher = FakeLauncher()
                with self.assertRaises(harness.RunTampered) as caught:
                    self.shown(message(BASE), launcher)
                self.assertIn(f".git/{rel}", caught.exception.changed)
                self.assertEqual(launcher.calls, [])
                path.unlink()
        self.assertEqual(harness.check_integrity(self.paths), [])

    def test_gitattributes_anywhere_is_fingerprinted(self):
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / ".gitattributes").write_text("* text\n")
        self.paths = dataclasses.replace(self.paths, fingerprints=self.baseline())
        self.assertIn("docs/.gitattributes", self.paths.fingerprints)
        self.assertEqual(harness.check_integrity(self.paths), [])
        (self.repo / ".gitattributes").write_text("* filter=x\n")
        self.assertEqual(harness.check_integrity(self.paths), [".gitattributes"])
        (self.repo / ".gitattributes").unlink()
        (self.repo / "docs" / ".gitattributes").write_text("* filter=x\n")
        self.assertEqual(harness.check_integrity(self.paths), ["docs/.gitattributes"])

    def test_origin_must_stay_a_real_directory(self):
        origin = self.paths.run_root / "origin.git"
        moved = self.paths.run_root / "origin-moved"
        origin.rename(moved)
        os.symlink(moved, origin)
        launcher = FakeLauncher()
        with self.assertRaises(harness.RunTampered) as caught:
            self.shown(message(BASE), launcher)
        self.assertIn("origin.git", caught.exception.changed)
        self.assertEqual(launcher.calls, [])

    def test_replacing_scripts_with_a_symlink_to_a_directory_is_tampering(self):
        elsewhere = self.repo.parent / "elsewhere"
        elsewhere.mkdir()
        shutil.copy(self.repo / "scripts" / "bootstrap.py", elsewhere)
        shutil.rmtree(self.repo / "scripts")
        os.symlink(elsewhere, self.repo / "scripts")
        changed = self.checked()
        self.assertIn("scripts/bootstrap.py", changed)
        self.assertIn("scripts", changed)  # the directory itself, now a symlink

    def test_a_watched_directory_replaced_by_a_regular_file_is_tampering(self):
        hooks = self.repo / ".git" / "hooks"
        shutil.rmtree(hooks)
        hooks.write_text("not a directory\n")
        self.assertIn(".git/hooks", self.checked())
        launcher = FakeLauncher()
        with self.assertRaises(harness.RunTampered) as caught:
            self.shown(message(BASE), launcher)
        self.assertIn(".git/hooks", caught.exception.changed)
        self.assertEqual(launcher.calls, [])

    def checked(self):
        """`check_integrity` with a guard: a reader that blocks on a FIFO fails the test."""
        box = {}
        thread = threading.Thread(
            target=lambda: box.update(result=harness.check_integrity(self.paths)), daemon=True
        )
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "check_integrity blocked")
        return box["result"]

    def test_non_regular_or_linked_files_are_tampering_and_never_block(self):
        def fifo(path):
            path.unlink(missing_ok=True)
            os.mkfifo(path)

        def zero(path):
            path.unlink(missing_ok=True)
            os.symlink("/dev/zero", path)

        def huge(path):
            path.unlink(missing_ok=True)
            path.write_text("# filler\n" * (harness.MAX_READ // 8 + 10))

        for name, make in {"fifo": fifo, "zero-symlink": zero, "oversize": huge}.items():
            for rel in (".git/config", ".gitattributes", "scripts/bootstrap.py"):
                with self.subTest(case=name, rel=rel):
                    self.setUp()  # a fresh run for every case
                    make(self.repo / rel)
                    self.assertIn(rel, self.checked())
                    launcher = FakeLauncher()
                    with self.assertRaises(harness.RunTampered):
                        self.shown(message(BASE), launcher)
                    self.assertEqual(launcher.calls, [])

    def test_a_symlink_in_a_watched_directory_is_tampering(self):
        os.symlink(self.repo / "ran.txt", self.repo / "scripts" / "link.py")
        self.assertIn("scripts/link.py", self.checked())

    def test_python_prefixes_come_from_the_given_interpreter(self):
        venv = self.repo.parent / "venv"
        (venv / "bin").mkdir(parents=True)
        stub = venv / "bin" / "python3"
        stub.write_text("#!/bin/sh\n")
        stub.chmod(0o755)
        (venv / "pyvenv.cfg").write_text("home = /opt/base/bin\ninclude-system = false\n")
        launcher = FakeLauncher()
        self.shown(message(BASE), launcher, python=str(stub))
        box = launcher.sandbox
        for group in (box.allow_read, box.exec_paths):
            self.assertIn("/opt/base", list(group))
            self.assertIn(str(venv), list(group))
            self.assertNotIn(os.path.realpath(sys.base_prefix), list(group))
        # The running interpreter keeps its own base prefix.
        self.shown(message(BASE), launcher)
        self.assertIn(os.path.realpath(sys.base_prefix), list(launcher.sandbox.allow_read))

    def test_call_log_must_stay_the_same_regular_file(self):
        log = self.paths.gh_calls

        def swap_symlink():
            log.unlink()
            os.symlink(self.repo / "ran.txt", log)

        def replace_file():
            # The new file exists before the old one goes, so the inode number cannot be reused.
            other = log.with_name("other.jsonl")
            other.write_text("")
            os.replace(other, log)

        def hard_link():
            os.link(log, self.repo / "alias.txt")

        cases = {"symlink": swap_symlink, "replaced": replace_file, "hardlink": hard_link}
        for name, action in cases.items():
            with self.subTest(case=name):
                launcher = FakeLauncher()
                action()
                with self.assertRaises(harness.RunTampered):
                    self.shown(message(BASE), launcher)
                self.assertEqual(launcher.calls, [])
                log.unlink(missing_ok=True)
                (self.repo / "alias.txt").unlink(missing_ok=True)
                log.write_text("")
                stat = os.lstat(log)
                self.paths = dataclasses.replace(
                    self.paths, gh_calls_id=(stat.st_dev, stat.st_ino)
                )

    def test_repository_must_stay_a_real_directory(self):
        moved = self.repo.parent / "repo-moved"
        self.repo.rename(moved)
        os.symlink(moved, self.repo)
        launcher = FakeLauncher()
        with self.assertRaises(harness.RunTampered):
            self.shown(message(BASE), launcher)
        self.assertEqual(launcher.calls, [])

    def test_unparseable_or_missing_git_config(self):
        self.tamper(lambda r: (r / ".git" / "config").write_text("[core\n\tx = 1\n"))

    def test_missing_git_config(self):
        self.tamper(lambda r: (r / ".git" / "config").unlink())

    def test_tracking_entries_written_by_git_are_fine(self):
        config = (
            "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n"
            '[remote "origin"]\n\turl = https://github.com/o/r.git\n'
            "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
            '[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n'
            '[branch "feature/x"]\n\tremote = origin\n\tmerge = refs/heads/feature/x\n'
        )
        (self.repo / ".git" / "config").write_text(config)
        self.assertEqual(harness.check_integrity(self.paths), [])
        self.assertIsNone(harness.config_problem(config))

    def test_dangerous_config_forms_are_caught(self):
        cases = [
            "[include]\n\tpath = /tmp/x\n",
            '[includeIf "gitdir:/"]\n\tpath = /tmp/x\n',
            "[INCLUDE]\npath=x\n",
            "[core]\n\tfsmonitor = x\n",
            "[core]\n\tFsMonitor = true\n",
            "[core]\n\thooksPath = /tmp/h\n",
            "[core]\n\tsshCommand = x\n",
            "[core]\n\teditor = x\n",
            "[core]\n\tpager = x\n",
            "[core]\n\taskPass = x\n",
            "[core]\n\tgitProxy = x\n",
            "[core]\n\tattributesFile = x\n",
            "[core]\n\texcludesFile =\n",
            '[credential]\n\thelper = "!x"\n',
            '[credential "https://github.com"]\n\thelper = x\n',
            '[filter "lfs"]\n\tclean = x\n',
            '[diff "bin"]\n\ttextconv = x\n',
            '[diff "bin"]\n\tcommand = x\n',
            '[merge "m"]\n\tdriver = x\n',
            '[url "file:///x"]\n\tinsteadOf = https://github.com/\n',
            '[url "file:///x"]\n\tpushInsteadOf = https://github.com/\n',
            '[remote "origin"]\n\tuploadpack = x\n',
            '[remote "origin"]\n\treceivePack = x\n',
            '[remote "origin"]\n\tvcs = x\n',
            "[alias]\n\tco = !x\n",
            "[sequence]\n\teditor = x\n",
            "[gpg]\n\tprogram = x\n",
            '[gpg "ssh"]\n\tprogram = x\n',
            "[uploadpack]\n\tpackObjectsHook = x\n",
            "[receivepack]\n\tx = y\n",
            "[protocol]\n\tallow = always\n",
            # other header forms, continuation, same-line entry, case
            "[remote.origin]\n\tuploadpack = x\n",
            "[url.file:///x]\n\tinsteadOf = https://github.com/\n",
            "[core] fsmonitor = x\n",
            "[core]\n\tfilemode = true \\\n\tcontinued\n[alias]\n\tx = y\n",
            '[REMOTE "origin"]\n\tUPLOADPACK = x\n',
            '[core]\n\tfsmonitor\n',
            "[core]\n# comment\n; also\n\tfsmonitor = x\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(harness.config_problem(text), text)

    def test_config_outside_the_allowlist_is_a_problem(self):
        for text in [
            "[extensions]\n\tworktreeConfig = true\n",
            "[core]\n\tworktree = /tmp/x\n",
            "[core]\n\tautocrlf = true\n",
            '[submodule "s"]\n\turl = x\n',
            '[remote "origin"]\n\tproxy = x\n',
            '[remote "origin"]\n\tpromisor = true\n',
            '[branch "main"]\n\trebase = true\n',
            '[branch "main"]\n\tmergeOptions = x\n',
            "[http]\n\tproxy = x\n",
            "[safe]\n\tdirectory = *\n",
            "[trailer]\n\tx = y\n",
            "[gc]\n\tauto = 0\n",
        ]:
            with self.subTest(text=text):
                self.assertTrue(harness.config_problem(text), text)

    def test_allowlisted_config_is_fine(self):
        text = (
            "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n"
            "\tlogallrefupdates = true\n\tignorecase = true\n\tprecomposeunicode = true\n"
            "\tsymlinks = true\n"
            '[remote "origin"]\n\turl = https://github.com/o/r.git\n\tpushurl = x\n'
            "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
            '[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n'
            "[user]\n\tname = A\n\temail = a@example.invalid\n"
            "[init]\n\tdefaultBranch = main\n"
        )
        self.assertIsNone(harness.config_problem(text))

    def test_unparseable_configs_are_problems(self):
        for text in ["[core\n", "key = value\n", "[core]\n\t= x\n", '[a "b]\nx=1\n', "[]\n"]:
            with self.subTest(text=text):
                self.assertTrue(harness.config_problem(text), text)

    def test_subsection_names_are_case_sensitive(self):
        # `[url "X"]` is not special by its name: only the keys decide.
        self.assertIsNone(harness.config_problem('[remote "UPLOADPACK"]\n\turl = x\n'))

    def test_git_hook_added(self):
        self.tamper(lambda r: (r / ".git" / "hooks" / "post-commit").write_text("#!/bin/sh\n"))

    def test_git_info_added(self):
        def add(repo):
            (repo / ".git" / "info").mkdir()
            (repo / ".git" / "info" / "exclude").write_text("*\n")

        self.tamper(add)

    def test_comparison_does_not_use_git(self):
        with mock.patch.object(harness.subprocess, "run") as run:
            harness.check_integrity(self.paths)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

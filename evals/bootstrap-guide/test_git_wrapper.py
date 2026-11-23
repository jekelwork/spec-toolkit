"""Tests for the git wrapper (FR-002, research D3a)."""
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import git_wrapper

URL = "https://github.com/o/r.git"
RUN = "/runs/one"
REWRITE = f"url.file://{RUN}/origin.git.insteadOf={URL}"
REAL = "/usr/bin/real-git"


def run_main(argv, env=None):
    calls = []
    env = {git_wrapper.RUN_DIR_ENV: RUN} if env is None else env
    git_wrapper.main(REAL, argv, env=env, execv=lambda path, args: calls.append((path, args)))
    return calls


class UnitTest(unittest.TestCase):
    def test_network_subcommands_are_rewritten(self):
        for sub in ["push", "fetch", "pull", "ls-remote", "clone"]:
            with self.subTest(sub=sub):
                calls = run_main([sub, "origin", "main"])
                self.assertEqual(calls, [(REAL, [REAL, "-c", REWRITE, sub, "origin", "main"])])

    def test_remote_update_is_rewritten(self):
        calls = run_main(["remote", "update"])
        self.assertEqual(calls, [(REAL, [REAL, "-c", REWRITE, "remote", "update"])])

    def test_get_url_and_others_pass_through(self):
        for argv in [
            ["remote", "get-url", "origin"],
            ["remote", "get-url", "--push", "origin"],
            ["remote"],
            ["status"],
            ["commit", "-m", "push"],
            ["config", "--get", "remote.origin.url"],
        ]:
            with self.subTest(argv=argv):
                self.assertEqual(run_main(argv), [(REAL, [REAL, *argv])])

    def test_global_options_are_skipped(self):
        cases = [
            (["-C", "push", "fetch"], 2),
            (["-c", "a=b", "push"], 2),
            (["--git-dir=x", "--no-pager", "pull"], 2),
            (["--git-dir", "x", "--work-tree", "y", "push"], 4),
            (["-C", "d", "-c", "k=v", "remote", "update"], 4),
        ]
        for prefix, n in cases:
            with self.subTest(prefix=prefix):
                expected = [REAL, *prefix[:n], "-c", REWRITE, *prefix[n:]]
                self.assertEqual(run_main(prefix), [(REAL, expected)])

    def test_global_option_value_is_not_a_subcommand(self):
        argv = ["-C", "push", "status"]
        self.assertEqual(run_main(argv), [(REAL, [REAL, *argv])])

    def test_no_subcommand(self):
        self.assertEqual(run_main(["--version"]), [(REAL, [REAL, "--version"])])
        self.assertEqual(run_main([]), [(REAL, [REAL])])

    def test_missing_run_dir_fails_for_network_subcommand(self):
        with self.assertRaises(SystemExit) as ctx:
            run_main(["push"], env={})
        self.assertNotEqual(ctx.exception.code, 0)

    def test_missing_run_dir_ok_for_local_subcommand(self):
        self.assertEqual(run_main(["status"], env={}), [(REAL, [REAL, "status"])])

    def test_install_writes_sh_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real-git"
            real.write_text("#!/bin/sh\n")
            real.chmod(0o755)
            path = git_wrapper.install(Path(tmp) / "bin", real_git=str(real))
            self.assertEqual(path, Path(tmp) / "bin" / "git")
            self.assertTrue(path.stat().st_mode & stat.S_IXUSR)
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("#!/bin/sh\n"))
            self.assertIn("--real-git", text)

    def test_install_rejects_bad_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            ok = tmp / "real-git"
            ok.write_text("x")
            ok.chmod(0o755)
            plain = tmp / "plain"
            plain.write_text("x")
            inside = bin_dir / "git"
            inside.write_text("x")
            inside.chmod(0o755)
            cases = {
                "none": {"real_git": None},
                "empty": {"real_git": ""},
                "relative": {"real_git": "git"},
                "missing": {"real_git": str(tmp / "nope")},
                "not executable": {"real_git": str(plain)},
                "inside bin dir": {"real_git": str(inside)},
                "relative python": {"real_git": str(ok), "python": "python3"},
                "missing python": {"real_git": str(ok), "python": str(tmp / "nopy")},
            }
            for name, kwargs in cases.items():
                with self.subTest(name):
                    with self.assertRaises(ValueError):
                        git_wrapper.install(bin_dir, **kwargs)

    def test_install_rejects_interpreter_older_than_3_11(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake = tmp / "old-python"
            fake.write_text("#!/bin/sh\nexit 1\n")
            fake.chmod(0o755)
            real = tmp / "real-git"
            real.write_text("x")
            real.chmod(0o755)
            with self.assertRaises(ValueError):
                git_wrapper.install(tmp / "bin", real_git=str(real), python=str(fake))
            self.assertFalse((tmp / "bin" / "git").exists())


class EntryTest(unittest.TestCase):
    def test_main_entry_parses_only_leading_real_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "fake git"
            real.write_text('#!/bin/sh\nfor a in "$@"; do echo "[$a]"; done\n')
            real.chmod(0o755)
            out = subprocess.run(
                [sys.executable, "-I", git_wrapper.__file__, "--real-git", str(real),
                 "status", "--real-git", "x y"],
                capture_output=True, text=True, timeout=30, check=True,
            ).stdout
            self.assertEqual(out.split(), ["[status]", "[--real-git]", "[x", "y]"])


@unittest.skipUnless(git_wrapper.find_real_git(), "git not installed")
class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.run_dir = root / "run"
        self.run_dir.mkdir()
        self.real = git_wrapper.find_real_git()
        bin_dir = root / "bin"
        py_dir = root / "py dir"
        py_dir.mkdir()
        (py_dir / "python3").symlink_to(sys.executable)
        git_wrapper.install(bin_dir, real_git=self.real, python=str(py_dir / "python3"))
        (root / "gitconfig").write_text("")
        self.env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(root),
            "GIT_CONFIG_GLOBAL": str(root / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "file",
            git_wrapper.RUN_DIR_ENV: str(self.run_dir),
        }
        self.git("init", "--bare", "-b", "main", str(self.run_dir / "origin.git"), cwd=root)
        self.work = root / "work"
        self.work.mkdir()
        self.git("init", "-b", "main", cwd=self.work)
        self.git("remote", "add", "origin", URL, cwd=self.work)
        (self.work / "f").write_text("x")
        self.git("add", "f", cwd=self.work)
        self.git("-c", "user.name=t", "-c", "user.email=t@e.x", "commit", "-m", "m", cwd=self.work)

    def git(self, *args, cwd, check=True):
        return subprocess.run(
            ["git", *args], cwd=cwd, env=self.env, capture_output=True, text=True,
            timeout=30, check=check,
        )

    def test_push_through_wrapper_reaches_bare_repo(self):
        self.git("push", "origin", "main", cwd=self.work)
        bare = str(self.run_dir / "origin.git")
        out = self.git("--git-dir", bare, "rev-parse", "main", cwd=self.work)
        self.assertEqual(len(out.stdout.strip()), 40)

    def test_get_url_still_shows_github(self):
        out = self.git("remote", "get-url", "--push", "origin", cwd=self.work)
        self.assertEqual(out.stdout.strip(), URL)

    def test_direct_push_fails_fast(self):
        env = dict(self.env)
        start = time.monotonic()
        result = subprocess.run(
            [self.real, "push", URL, "main"], cwd=self.work, env=env,
            capture_output=True, text=True, timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("transport 'https' not allowed", result.stderr)
        self.assertLess(time.monotonic() - start, 10)


if __name__ == "__main__":
    unittest.main()

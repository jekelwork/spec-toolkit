import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import launchers
import profiles

HOME = "/Users/maintainer"
PARENT_ENV = {
    "PATH": "/opt/claude/bin:/usr/bin",
    "HOME": HOME,
    "TERM": "xterm",
    "GH_TOKEN": "x",
    "GITHUB_TOKEN": "x",
    "SSH_AUTH_SOCK": "/tmp/agent.sock",
    "ANTHROPIC_API_KEY": "x",
    "AWS_SECRET_ACCESS_KEY": "x",
    "OPENAI_API_KEY": "x",
    "NPM_TOKEN": "x",
}
HARNESS_ENV = {
    "EVAL_RUN_DIR": "/tmp/r",
    "EVAL_GH_TABLE": "/tmp/r/table.json",
    "EVAL_GH_CALLS": "/tmp/r/calls.log",
}


def make_launcher(**kwargs):
    options = {
        "model": "m1",
        "max_budget_usd": 2.0,
        "home": HOME,
        "parent_env": PARENT_ENV,
        "claude_path": "/opt/claude/bin/claude",
        "python": "/Users/maintainer/.pyenv/versions/3.12/bin/python3",
    }
    options.update(kwargs)
    return launchers.LocalLauncher(**options)


class LocalEnvironmentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.run_dir = self.root / "repo"
        self.run_dir.mkdir()
        self.sidecar = self.root / "harness"
        self.sidecar.mkdir()
        self.bin_dir = self.root / "bin"
        self.launcher = make_launcher()
        self.env = self.launcher.build_env(
            self.run_dir, self.sidecar, {**HARNESS_ENV, "PATH": str(self.bin_dir)}
        )

    def test_path_order(self):
        self.assertEqual(
            self.env["PATH"].split(":"),
            [
                str(self.bin_dir),
                "/Users/maintainer/.pyenv/versions/3.12/bin",
                "/usr/bin",
                "/bin",
            ],
        )

    def test_system_python_dir_not_duplicated(self):
        launcher = make_launcher(python="/usr/bin/python3")
        env = launcher.build_env(self.run_dir, self.sidecar, {"PATH": str(self.bin_dir)})
        self.assertEqual(env["PATH"].split(":"), [str(self.bin_dir), "/usr/bin", "/bin"])

    def test_git_and_gh_settings(self):
        self.assertEqual(self.env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(self.env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(self.env["GIT_ALLOW_PROTOCOL"], "file")
        gh_dir = Path(self.env["GH_CONFIG_DIR"])
        self.assertTrue(gh_dir.is_dir())
        self.assertEqual(list(gh_dir.iterdir()), [])
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK"):
            self.assertNotIn(name, self.env)

    def test_harness_gitconfig_has_made_up_identity(self):
        config = Path(self.env["GIT_CONFIG_GLOBAL"])
        self.assertTrue(config.is_file())
        self.assertNotIn(HOME, str(config))
        out = subprocess.run(
            ["git", "config", "--file", str(config), "--list"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn("user.name=", out)
        self.assertIn("user.email=", out)

    def test_exactly_the_recorded_variables(self):
        allowed = {
            "PATH",
            "HOME",
            "TERM",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
            "GH_CONFIG_DIR",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_TERMINAL_PROMPT",
            "GIT_ALLOW_PROTOCOL",
            *HARNESS_ENV,
        }
        self.assertEqual(set(self.env), allowed)
        self.assertEqual(self.env["HOME"], HOME)
        self.assertEqual(self.env["TERM"], "xterm")
        self.assertEqual(self.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
        for name in ("AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY", "NPM_TOKEN", "ANTHROPIC_API_KEY"):
            self.assertNotIn(name, self.env)

    def test_harness_env_accepts_only_path_and_eval_names(self):
        for name in ("GH_TOKEN", "HOME", "GIT_CONFIG_GLOBAL", "SPEC_EVAL_X"):
            with self.assertRaisesRegex(ValueError, name):
                self.launcher.build_env(
                    self.run_dir, self.sidecar, {"PATH": str(self.bin_dir), name: "x"}
                )

    def test_sidecar_files_live_in_the_given_directory(self):
        self.assertTrue(
            Path(self.env["GH_CONFIG_DIR"]).is_relative_to(self.sidecar)
        )
        self.assertTrue(
            Path(self.env["GIT_CONFIG_GLOBAL"]).is_relative_to(self.sidecar)
        )
        self.assertEqual(list(self.root.iterdir()).count(self.root / "gh-config"), 0)

    def test_parallel_runs_do_not_share_sidecar_files(self):
        other = self.root / "other"
        other.mkdir()
        env = self.launcher.build_env(self.run_dir, other, {"PATH": str(self.bin_dir)})
        self.assertNotEqual(env["GIT_CONFIG_GLOBAL"], self.env["GIT_CONFIG_GLOBAL"])
        self.assertNotEqual(env["GH_CONFIG_DIR"], self.env["GH_CONFIG_DIR"])

    def test_sidecar_inside_run_dir_is_rejected(self):
        inside = self.run_dir / "harness"
        inside.mkdir()
        with self.assertRaises(ValueError):
            self.launcher.build_env(self.run_dir, inside, {"PATH": "/b"})
        with self.assertRaises(ValueError):
            self.launcher.start(self.run_dir, inside, ["claude"], {})

    def test_sidecar_must_be_a_dedicated_directory(self):
        with self.assertRaises(ValueError):
            self.launcher.build_env(self.run_dir, self.run_dir, {"PATH": "/b"})
        with self.assertRaises(ValueError):
            self.launcher.build_env(self.run_dir, self.root / "missing", {"PATH": "/b"})


class ArgvTest(unittest.TestCase):
    def test_agent_argv(self):
        launcher = make_launcher()
        run_dir = Path("/tmp/eval/repo")
        argv = launcher.agent_argv("guide", run_dir)
        self.assertEqual(argv[0], "claude")
        self.assertNotIn("--bare", argv)
        flags = argv[1:]
        for flag in (
            "-p",
            "--verbose",
            "--no-session-persistence",
            "--strict-mcp-config",
        ):
            self.assertIn(flag, flags)

        def value(flag):
            return flags[flags.index(flag) + 1]

        self.assertEqual(value("--input-format"), "stream-json")
        self.assertEqual(value("--output-format"), "stream-json")
        self.assertEqual(value("--setting-sources"), "project")
        self.assertEqual(value("--model"), "m1")
        self.assertEqual(value("--max-budget-usd"), "2.0")
        self.assertEqual(value("--tools"), ",".join(profiles.tools("guide")))
        self.assertEqual(
            json.loads(value("--settings")),
            profiles.settings(
                "guide", run_dir, HOME, extra_read=launcher.extra_read
            ),
        )

    def test_settings_deny_home_and_enable_sandbox(self):
        launcher = make_launcher()
        argv = launcher.agent_argv("guide", Path("/tmp/eval/repo"))
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertTrue(settings["sandbox"]["enabled"])
        self.assertIn(HOME, settings["sandbox"]["filesystem"]["denyRead"])
        self.assertIn(
            "/Users/maintainer/.pyenv/versions/3.12",
            settings["sandbox"]["filesystem"]["allowRead"],
        )

    def test_extra_read_resolves_symlinked_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            real = home / "py" / "bin"
            real.mkdir(parents=True)
            (real / "python3").write_text("")
            venv = home / "venv" / "bin"
            venv.mkdir(parents=True)
            (venv / "python3").symlink_to(real / "python3")
            launcher = make_launcher(home=str(home), python=str(venv / "python3"))
            self.assertEqual(launcher.extra_read, [str(home / "py")])

    def test_extra_read_adds_base_prefix_only_for_the_harness_interpreter(self):
        base = Path(sys.base_prefix).resolve()
        home = str(base.parent)
        own = make_launcher(home=home, python=sys.executable)
        self.assertIn(str(base), own.extra_read)
        other = make_launcher(home=home, python="/elsewhere/other/bin/python3")
        self.assertNotIn(str(base), other.extra_read)

    def test_extra_read_follows_pyvenv_cfg_home_of_injected_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            (home / "base" / "bin").mkdir(parents=True)
            venv = home / "venv" / "bin"
            venv.mkdir(parents=True)
            (venv / "python3").write_text("")
            (home / "venv" / "pyvenv.cfg").write_text(
                f"home = {home / 'base' / 'bin'}\nversion = 3.12\n"
            )
            launcher = make_launcher(home=str(home), python=str(venv / "python3"))
            self.assertEqual(
                launcher.extra_read, [str(home / "venv"), str(home / "base")]
            )

    def test_extra_read_ignores_pyvenv_home_outside_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            venv = home / "venv" / "bin"
            venv.mkdir(parents=True)
            (venv / "python3").write_text("")
            (home / "venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
            launcher = make_launcher(home=str(home), python=str(venv / "python3"))
            self.assertEqual(launcher.extra_read, [str(home / "venv")])

    def test_no_extra_read_when_python_outside_home(self):
        launcher = make_launcher(python="/usr/bin/python3")
        self.assertEqual(list(launcher.extra_read), [])


def write_fake(directory, body):
    path = Path(directory) / "claude"
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class ProcessTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.run_dir = self.root / "repo"
        self.run_dir.mkdir()
        self.sidecar = self.root / "harness"
        self.sidecar.mkdir()

    def launcher(self, body, **kwargs):
        fake = write_fake(self.root, body)
        launcher = make_launcher(
            claude_path=str(fake),
            python=sys.executable,
            parent_env={**PARENT_ENV, "PATH": os.environ["PATH"]},
            **kwargs,
        )
        self.addCleanup(launcher.close)
        return launcher

    def test_prepare_resolves_claude_on_parent_path(self):
        fake = write_fake(self.root, "")
        launcher = launchers.LocalLauncher(
            model="m",
            max_budget_usd=1,
            home=HOME,
            parent_env={"PATH": f"/nonexistent:{self.root}", "HOME": HOME},
        )
        launcher.prepare()
        self.assertEqual(launcher.claude_path, str(fake))

    def test_prepare_fails_readably_without_claude(self):
        launcher = launchers.LocalLauncher(
            model="m", max_budget_usd=1, home=HOME, parent_env={"PATH": "/nonexistent"}
        )
        with self.assertRaisesRegex(RuntimeError, "claude"):
            launcher.prepare()

    def test_start_streams_stdin_to_stdout_in_run_dir(self):
        launcher = self.launcher(
            "import os, sys\n"
            "print(os.getcwd(), flush=True)\n"
            "print(sys.argv[1], flush=True)\n"
            "for line in sys.stdin:\n"
            "    print('echo ' + line.strip(), flush=True)\n"
        )
        launcher.prepare()
        env = launcher.build_env(self.run_dir, self.sidecar, HARNESS_ENV)
        proc = launcher.start(self.run_dir, self.sidecar, ["claude", "--flag"], env)
        out = proc.stdout
        self.assertEqual(os.path.realpath(out.readline().strip()), os.path.realpath(self.run_dir))
        self.assertEqual(out.readline().strip(), "--flag")
        proc.stdin.write("hello\n")
        proc.stdin.flush()
        self.assertEqual(out.readline().strip(), "echo hello")
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=10), 0)

    def test_start_passes_only_built_environment_and_stderr_to_file(self):
        launcher = self.launcher(
            "import json, os, sys\n"
            "print(json.dumps(sorted(os.environ)), flush=True)\n"
            "print('oops', file=sys.stderr, flush=True)\n"
        )
        launcher.prepare()
        env = launcher.build_env(self.run_dir, self.sidecar, HARNESS_ENV)
        proc = launcher.start(self.run_dir, self.sidecar, ["claude"], env)
        names = json.loads(proc.stdout.readline())
        proc.wait(timeout=10)
        # The interpreter itself may add runtime variables (macOS, locale coercion); nothing else.
        runtime = {"__CF_USER_TEXT_ENCODING", "LC_CTYPE", "PYTHONCOERCECLOCALE"}
        self.assertEqual(set(names) - runtime, set(env))
        self.assertEqual(
            (self.sidecar / "agent-stderr.log").read_text().strip(), "oops"
        )

    def test_argument_with_shell_metacharacters_is_literal(self):
        launcher = self.launcher("import sys\nprint(sys.argv[1], flush=True)\n")
        launcher.prepare()
        env = launcher.build_env(self.run_dir, self.sidecar, HARNESS_ENV)
        proc = launcher.start(self.run_dir, self.sidecar, ["claude", "x; rm -rf ~"], env)
        self.assertEqual(proc.stdout.readline().strip(), "x; rm -rf ~")
        proc.wait(timeout=10)

    def test_close_terminates_process_that_ignores_stdin(self):
        launcher = self.launcher("import time\nwhile True:\n    time.sleep(0.1)\n")
        launcher.prepare()
        env = launcher.build_env(self.run_dir, self.sidecar, HARNESS_ENV)
        proc = launcher.start(self.run_dir, self.sidecar, ["claude"], env)
        self.assertIsNone(proc.poll())
        launcher.close()
        self.assertIsNotNone(proc.returncode)
        launcher.close()  # idempotent

    def test_close_kills_process_that_ignores_sigterm(self):
        launcher = self.launcher(
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('ready', flush=True)\n"
            "while True:\n    time.sleep(0.1)\n",
            terminate_timeout=0.5,
        )
        launcher.prepare()
        env = launcher.build_env(self.run_dir, self.sidecar, HARNESS_ENV)
        proc = launcher.start(self.run_dir, self.sidecar, ["claude"], env)
        proc.stdout.readline()
        started = time.monotonic()
        launcher.close()
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNotNone(proc.returncode)


class SelectionTest(unittest.TestCase):
    def test_local(self):
        launcher = launchers.create("local", model="m", max_budget_usd=1, home=HOME)
        self.assertIsInstance(launcher, launchers.LocalLauncher)

    def test_devvm_is_not_implemented_with_readable_message(self):
        with self.assertRaisesRegex(NotImplementedError, "devvm.*not installed|devvm.*local"):
            launchers.create("devvm", model="m", max_budget_usd=1, home=HOME)

    def test_unknown_launcher(self):
        with self.assertRaisesRegex(ValueError, "local"):
            launchers.create("docker", model="m", max_budget_usd=1, home=HOME)


if __name__ == "__main__":
    unittest.main()

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
            "GIT_CONFIG_COUNT",
            *(f"GIT_CONFIG_{kind}_{n}" for kind in ("KEY", "VALUE") for n in range(4)),
            *HARNESS_ENV,
        }
        self.assertEqual(set(self.env), allowed)
        self.assertEqual(self.env["HOME"], HOME)
        self.assertEqual(self.env["TERM"], "xterm")
        self.assertEqual(self.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
        for name in ("AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY", "NPM_TOKEN", "ANTHROPIC_API_KEY"):
            self.assertNotIn(name, self.env)

    def test_git_config_pins_have_command_line_precedence(self):
        count = int(self.env["GIT_CONFIG_COUNT"])
        pins = {
            self.env[f"GIT_CONFIG_KEY_{n}"]: self.env[f"GIT_CONFIG_VALUE_{n}"]
            for n in range(count)
        }
        self.assertEqual(
            pins,
            {
                "core.fsmonitor": "false",
                "core.hooksPath": "/dev/null",
                "protocol.allow": "never",
                "protocol.file.allow": "always",
            },
        )

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


ON_MAC = sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec")


class SandboxProfileTest(unittest.TestCase):
    def spec(self, **kwargs):
        options = {
            "home": "/Users/maintainer",
            "allow_read": ["/opt/readable"],
            "allow_write": ["/opt/repo"],
            "write_files": ["/opt/gh-calls.log"],
            "exec_paths": ["/opt/py", "/opt/run/bin"],
            "home_override": "/opt/throwaway-home",
            "tmpdir": "/opt/run-tmp",
        }
        options.update(kwargs)
        return launchers.SandboxSpec(**options)

    def test_profile_is_deny_default_without_network_or_host_services(self):
        profile = launchers.sandbox_profile(self.spec())
        self.assertTrue(profile.startswith("(version 1)(deny default)"))
        self.assertNotIn("(allow default)", profile)
        for forbidden in ("network", "lsopen", "appleevent", "mach-lookup", "process-info"):
            self.assertNotIn(forbidden, profile)
        self.assertIn('(deny file-read* (subpath "/Users/maintainer"))', profile)
        self.assertIn("(allow process-fork)", profile)
        self.assertIn("(deny file-write*)", profile)

    def test_profile_lists_the_spec_paths(self):
        profile = launchers.sandbox_profile(self.spec())
        self.assertIn('(subpath "/opt/readable")', profile)
        self.assertIn('(subpath "/opt/repo")', profile)
        self.assertIn('(subpath "/opt/run-tmp")', profile)
        self.assertIn('(subpath "/opt/py")', profile)
        self.assertIn('(subpath "/opt/run/bin")', profile)
        self.assertIn('(literal "/dev/null")', profile)
        self.assertIn('(literal "/opt/gh-calls.log")', profile)
        self.assertNotIn('(subpath "/opt/gh-calls.log")', profile)

    def test_allow_write_is_always_subpath_and_write_files_always_literal(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = os.path.realpath(tmp)
            file = Path(directory, "f")
            file.write_text("")
            profile = launchers.sandbox_profile(
                self.spec(allow_write=[str(file)], write_files=[directory])
            )
            self.assertIn(f'(subpath "{file}")', profile)
            self.assertIn(f'(literal "{directory}")', profile)

    def test_profile_rejects_paths_that_break_the_string(self):
        for bad in ('/opt/a"b', "/opt/a\\b"):
            for field in ("allow_read", "allow_write", "write_files", "exec_paths"):
                with self.assertRaises(ValueError):
                    launchers.sandbox_profile(self.spec(**{field: [bad]}))
            with self.assertRaises(ValueError):
                launchers.sandbox_profile(self.spec(home=bad))

    def test_profile_rejects_relative_and_unnormalised_paths(self):
        for bad in ("relative/dir", "/opt/a/../b", "/opt//a", "/opt/a/", "/opt/./a"):
            with self.assertRaises(ValueError, msg=bad):
                launchers.sandbox_profile(self.spec(allow_read=[bad]))

    def test_profile_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(os.path.realpath(tmp))
            (root / "real").mkdir()
            (root / "link").symlink_to(root / "real")
            for field in ("allow_read", "allow_write", "write_files", "exec_paths"):
                with self.assertRaises(ValueError):
                    launchers.sandbox_profile(self.spec(**{field: [str(root / "link")]}))
            with self.assertRaises(ValueError):  # a symlinked ancestor
                launchers.sandbox_profile(self.spec(allow_read=[str(root / "link" / "x")]))

    def test_missing_sandbox_exec_raises_without_fallback(self):
        launcher = make_launcher()
        original = launchers.SANDBOX_EXEC
        launchers.SANDBOX_EXEC = "/nonexistent/sandbox-exec"
        self.addCleanup(setattr, launchers, "SANDBOX_EXEC", original)
        with self.assertRaisesRegex(RuntimeError, "sandbox-exec"):
            launcher.run(["true"], "/", {}, 5, sandbox=self.spec())


@unittest.skipUnless(ON_MAC, "sandboxed runs need macOS sandbox-exec")
class RunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.repo = self.root / "repo"
        self.fake_home = self.root / "home"
        self.override = self.root / "override-home"
        self.run_tmp = self.root / "run-tmp"
        self.scripts = self.root / "scripts"
        for directory in (self.repo, self.fake_home, self.override, self.run_tmp, self.scripts):
            directory.mkdir()
        (self.fake_home / "secret.txt").write_text("secret")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.launcher = make_launcher()

    def sandbox(self, **kwargs):
        prefixes = sorted(
            {
                os.path.realpath(sys.base_prefix),
                str(Path(os.path.realpath(sys.executable)).parent.parent),
            }
        )
        git = ["/usr/bin/git", "/Library/Developer/CommandLineTools/usr/bin/git"]
        options = {
            "home": str(self.fake_home),
            "allow_read": [str(self.scripts), str(self.repo), *prefixes],
            "allow_write": [str(self.repo)],
            "exec_paths": [str(self.scripts), *prefixes, *(g for g in git if os.path.exists(g))],
            "home_override": str(self.override),
            "tmpdir": str(self.run_tmp),
        }
        options.update(kwargs)
        return launchers.SandboxSpec(**options)

    def script(self, body):
        return str(write_fake(self.scripts, body))

    def run_script_with(self, launcher, body):
        return launcher.run(
            [self.script(body)],
            self.repo,
            {"PATH": "/usr/bin:/bin"},
            30,
            sandbox=self.sandbox(),
        )

    def run_script(self, body, args=(), env=None, timeout=30, **sandbox):
        return self.launcher.run(
            [self.script(body), *args],
            self.repo,
            {"PATH": "/usr/bin:/bin"} if env is None else env,
            timeout,
            sandbox=self.sandbox(**sandbox),
        )

    def test_runs_argv_literally_in_cwd_with_exact_env(self):
        env = {"EVAL_A": "1", "PATH": "/usr/bin"}
        out, err, timed_out = self.run_script(
            "import json, os, sys\n"
            "print(json.dumps([os.getcwd(), sys.argv[1:], sorted(os.environ)]))\n"
            "print('err', file=sys.stderr)\n",
            args=["x; rm -rf ~", "$(id)"],
            env=env,
        )
        cwd, args, names = json.loads(out)
        self.assertEqual(os.path.realpath(cwd), str(self.repo))
        self.assertEqual(args, ["x; rm -rf ~", "$(id)"])
        runtime = {"__CF_USER_TEXT_ENCODING", "LC_CTYPE", "PYTHONCOERCECLOCALE"}
        self.assertEqual(set(names) - runtime, {*env, "HOME", "TMPDIR", "EVAL_CALL_ID"})
        self.assertEqual(err.strip(), "err")
        self.assertFalse(timed_out)

    def test_home_and_tmpdir_are_overridden(self):
        out, _err, _ = self.run_script(
            "import os\nprint(os.environ['HOME'])\nprint(os.environ['TMPDIR'])\n",
            env={"HOME": "/Users/maintainer", "PATH": "/usr/bin"},
        )
        home, tmpdir = out.split()
        self.assertEqual(os.path.realpath(home), str(self.override))
        self.assertEqual(os.path.realpath(tmpdir), str(self.run_tmp))

    def test_cannot_read_under_home(self):
        out, _err, _ = self.run_script(
            "import sys\n"
            "try:\n"
            f"    print(open({str(self.fake_home / 'secret.txt')!r}).read())\n"
            "except OSError as error:\n"
            "    print('denied', type(error).__name__)\n"
        )
        self.assertEqual(out.split()[0], "denied")

    def test_writes_only_inside_allow_write(self):
        outside = self.root / "outside.txt"
        out, _err, _ = self.run_script(
            "import sys\n"
            "open('inside.txt', 'w').write('x')\n"
            "try:\n"
            f"    open({str(outside)!r}, 'w').write('x')\n"
            "    print('wrote')\n"
            "except OSError:\n"
            "    print('denied')\n"
        )
        self.assertEqual(out.strip(), "denied")
        self.assertTrue((self.repo / "inside.txt").exists())
        self.assertFalse(outside.exists())

    def test_python_and_git_still_work(self):
        out, _err, _ = self.run_script(
            "import json, subprocess, tomllib\n"
            "command = ['git', 'status', '--porcelain']\n"
            "done = subprocess.run(command, capture_output=True, text=True)\n"
            "print(done.returncode)\n"
        )
        self.assertEqual(out.strip(), "0")

    def test_host_apps_keychain_and_other_directories_are_unreachable(self):
        out, _err, _ = self.run_script(
            "import os, subprocess\n"
            "for command in (['/usr/bin/open', '-g', '-a', 'TextEdit'],\n"
            "                ['/usr/bin/security', 'dump-keychain']):\n"
            "    try:\n"
            "        done = subprocess.run(command, capture_output=True)\n"
            "        print(command[0], 'rc', done.returncode)\n"
            "    except OSError:\n"
            "        print(command[0], 'denied')\n"
            "for directory in ('/Users/Shared', '/Users'):\n"
            "    try:\n"
            "        os.listdir(directory)\n"
            "        print(directory, 'listed')\n"
            "    except OSError:\n"
            "        print(directory, 'denied')\n"
        )
        lines = [line.split() for line in out.strip().splitlines()]
        for line in lines[:2]:
            self.assertTrue(line[1] == "denied" or int(line[2]) != 0, out)
        self.assertEqual(lines[2], ["/Users/Shared", "denied"])
        self.assertEqual(lines[3], ["/Users", "denied"])

    def sh_launcher(self, name, body):
        path = self.scripts / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)
        return str(path)

    def test_sh_launchers_run_including_real_git(self):
        sandbox = self.sandbox()
        plain = self.sh_launcher("plain", "echo from-sh")
        git = self.sh_launcher("git-launcher", "exec /usr/bin/git --version")
        env = {"PATH": "/usr/bin:/bin"}
        out, err, _ = self.launcher.run([plain], self.repo, env, 30, sandbox=sandbox)
        self.assertEqual(out.strip(), "from-sh", err)
        out, err, _ = self.launcher.run([git], self.repo, env, 30, sandbox=sandbox)
        self.assertTrue(out.startswith("git version"), err)

    def test_file_metadata_under_home_and_library_listings_are_denied(self):
        out, _err, _ = self.run_script(
            "import os\n"
            "for call, path in ((os.stat, %r), (os.listdir, '/Library/Preferences')):\n"
            "    try:\n"
            "        call(path)\n"
            "        print(path, 'visible')\n"
            "    except OSError:\n"
            "        print(path, 'denied')\n" % str(self.fake_home / "secret.txt")
        )
        self.assertEqual(
            [line.split()[1] for line in out.strip().splitlines()], ["denied", "denied"]
        )

    def test_unneeded_system_directories_are_not_readable(self):
        out, _err, _ = self.run_script(
            "import os\n"
            "for path in ('/usr/local', '/usr/sbin', '/sbin', '/private/etc/ssh'):\n"
            "    try:\n"
            "        os.listdir(path)\n"
            "        print(path, 'visible')\n"
            "    except OSError:\n"
            "        print(path, 'denied')\n"
        )
        for line in out.strip().splitlines():
            self.assertEqual(line.split()[1], "denied", line)

    def test_setsid_chdir_grandchild_of_a_call_is_killed_by_its_marker(self):
        out, _err, _ = self.run_script(
            "import subprocess, sys\n"
            "sleeper = [sys.executable, '-c', 'import time; time.sleep(60)']\n"
            "child = subprocess.Popen(sleeper, start_new_session=True, cwd='/',\n"
            "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "print(child.pid)\n"
        )
        self.assertGone(int(out.split()[0]))

    def test_agent_and_maintainer_processes_in_the_run_root_survive_a_run(self):
        sidecar = self.root / "harness"
        sidecar.mkdir()
        agent_launcher = make_launcher(
            claude_path=write_fake(self.root, "import time\ntime.sleep(60)\n"),
            python=sys.executable,
        )
        agent_launcher.prepare()
        env = agent_launcher.build_env(self.repo, sidecar, {"PATH": "/usr/bin:/bin"})
        agent = agent_launcher.start(self.repo, sidecar, ["claude"], env)
        self.addCleanup(agent_launcher.close)
        maintainer = subprocess.Popen(["sleep", "60"], cwd=self.repo, start_new_session=True)
        self.addCleanup(maintainer.kill)
        for launcher in (self.launcher, agent_launcher):
            self.run_script_with(launcher, "print('ok')\n")
        self.assertIsNone(agent.poll())
        self.assertIsNone(maintainer.poll())
        self.launcher.close()
        self.assertIsNone(maintainer.poll())

    def test_has_no_network(self):
        started = time.monotonic()
        out, _err, _ = self.run_script(
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 443), timeout=5)\n"
            "    print('connected')\n"
            "except OSError:\n"
            "    print('blocked')\n"
        )
        self.assertEqual(out.strip(), "blocked")
        self.assertLess(time.monotonic() - started, 4)

    def test_timeout_kills_process_group_including_grandchild(self):
        started = time.monotonic()
        out, _err, timed_out = self.run_script(
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(60)\n",
            timeout=1,
        )
        self.assertTrue(timed_out)
        self.assertLess(time.monotonic() - started, 20)
        self.assertGone(int(out.split()[0]))

    def assertGone(self, pid):
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        self.fail(f"process {pid} survived")

    def test_background_process_left_after_normal_exit_is_killed(self):
        out, _err, timed_out = self.run_script(
            "import subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "print(child.pid)\n"
        )
        self.assertFalse(timed_out)
        self.assertGone(int(out.split()[0]))

    def test_setsid_grandchild_holding_stdout_does_not_block_past_timeout(self):
        started = time.monotonic()
        out, _err, timed_out = self.run_script(
            "import os, subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            "    start_new_session=True)\n"
            "print(child.pid, flush=True)\n"
            "import time; time.sleep(60)\n",
            timeout=1,
        )
        self.assertTrue(timed_out)
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(out.split())
        os.kill(int(out.split()[0]), 9)

    def test_output_is_capped(self):
        out, _err, _ = self.run_script("import sys\nsys.stdout.write('a' * (2 << 20))\n")
        self.assertLessEqual(len(out), (1 << 20) + 64)
        self.assertTrue(out.endswith("[output truncated]"))
        small, _e, _ = self.run_script("print('ok')\n")
        self.assertEqual(small.strip(), "ok")


@unittest.skipUnless(os.path.exists("/usr/sbin/lsof"), "needs lsof")
class KillCallTest(unittest.TestCase):
    def spawn(self, cwd="/", env=None):
        # Not /bin/sleep: ps hides the environment of Apple-signed system binaries.
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=cwd,
            env={"PATH": "/usr/bin:/bin", **(env or {})},
            start_new_session=True,
        )
        self.addCleanup(process.kill)
        return process

    def test_kills_by_marker_and_by_cwd_under_the_call_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(os.path.realpath(tmp))
            marked = self.spawn(env={"EVAL_CALL_ID": "feedface"})
            in_dir = self.spawn(cwd=root)
            other_marker = self.spawn(env={"EVAL_CALL_ID": "feedfacf"})
            elsewhere = self.spawn()
            time.sleep(0.3)
            launchers.kill_call("feedface", [str(root)])
            self.assertEqual(marked.wait(timeout=10), -9)
            self.assertEqual(in_dir.wait(timeout=10), -9)
            self.assertIsNone(other_marker.poll())
            self.assertIsNone(elsewhere.poll())

    def test_never_kills_protected_pids_or_their_descendants(self):
        parent = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "time.sleep(60)",
            ],
            env={"PATH": "/usr/bin:/bin", "EVAL_CALL_ID": "beef"},
        )
        self.addCleanup(parent.kill)
        time.sleep(1)
        launchers.kill_call("beef", [], protect=[parent.pid])
        self.assertIsNone(parent.poll())
        table = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid="], capture_output=True, text=True
        ).stdout.split()
        children = [int(p) for p, pp in zip(table[::2], table[1::2]) if int(pp) == parent.pid]
        self.assertTrue(children)
        for pid in children:
            os.kill(pid, 0)  # the protected process's child is still alive
            self.addCleanup(os.kill, pid, 9)

    def test_without_lsof_or_ps_it_warns_and_still_does_what_it_can(self):
        original = launchers.LSOF
        launchers.LSOF = "/nonexistent/lsof"
        self.addCleanup(setattr, launchers, "LSOF", original)
        with self.assertWarns(RuntimeWarning):
            launchers.kill_call("cafe", ["/tmp"])


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

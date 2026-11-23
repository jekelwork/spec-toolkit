"""Launcher interface with the devvm and local launchers that start the agent.

A launcher has three methods (research D4a): `prepare()` once per evaluation, `start(run_dir,
argv, env)` once per run (returns a process with streamed text stdin and stdout) and `close()` at
the end. Only `LocalLauncher` exists: the Phase 1 spike found devvm not installed, so selecting
`devvm` fails with a readable message.

Layout per run (T037): `<tmp>/<run-id>/repo` is `run_dir`, the temporary repository and the
agent's working directory; `<tmp>/<run-id>/harness/` is `sidecar_dir`, a dedicated existing
directory per run that holds the harness's own files (empty `gh-config/`, `harness.gitconfig`,
`agent-stderr.log`), so parallel runs never share them and they never appear in the repository.

`agent-stderr.log` is raw agent output: the harness must secret-scan it (T038) or delete it.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import profiles

SYSTEM_PATH = ("/usr/bin", "/bin")
GIT_IDENTITY = "[user]\n\tname = Eval Harness\n\temail = eval-harness@example.invalid\n"
# Variables of the parent environment that the agent needs: `claude -p` stays logged in through
# the keychain (real HOME) and needs TERM; auto memory stays off. Nothing else is copied.
LOGIN_VARIABLES = ("HOME", "TERM")


def _check_sidecar(run_dir, sidecar_dir):
    sidecar = Path(sidecar_dir)
    if not sidecar.is_dir() or sidecar.resolve().is_relative_to(Path(run_dir).resolve()):
        raise ValueError(
            f"sidecar_dir {str(sidecar)!r} must be an existing directory outside run_dir"
        )
    return sidecar


class LocalLauncher:
    """Runs the agent on the host with a scrubbed environment."""

    def __init__(
        self,
        model,
        max_budget_usd,
        home=None,
        parent_env=None,
        claude_path=None,
        python=None,
        terminate_timeout=5.0,
    ):
        self.model = model
        self.max_budget_usd = max_budget_usd
        self.parent_env = dict(os.environ if parent_env is None else parent_env)
        self.home = str(home or self.parent_env.get("HOME") or Path.home())
        self.claude_path = claude_path
        self.python = str(python or sys.executable)
        self.terminate_timeout = terminate_timeout
        self._processes = []
        self._stderr_files = []

    @property
    def extra_read(self):
        """The interpreter's prefix when it lies under the maintainer's home, else nothing."""
        home = Path(self.home).resolve()
        python = Path(self.python)
        prefixes = [python.resolve().parent.parent]
        if python.resolve() == Path(sys.executable).resolve():
            prefixes.append(Path(sys.base_prefix).resolve())
        else:
            # An injected venv interpreter: its pyvenv.cfg names the interpreter it was made from.
            try:
                lines = (python.parent.parent / "pyvenv.cfg").read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            for line in lines:
                key, _, value = line.partition("=")
                if key.strip() == "home" and value.strip():
                    prefixes.append(Path(value.strip()).resolve().parent)
        result = []
        for prefix in prefixes:
            if prefix.is_relative_to(home) and str(prefix) not in result:
                result.append(str(prefix))
        return result

    def prepare(self):
        """Resolve `claude` to an absolute path (it is not on the agent's `PATH`)."""
        if self.claude_path is None:
            found = shutil.which("claude", path=self.parent_env.get("PATH", ""))
            if found is None:
                raise RuntimeError("the `claude` executable was not found on PATH")
            self.claude_path = found
        return self

    def build_env(self, run_dir, sidecar_dir, harness_env):
        """The agent's complete environment.

        `harness_env` may hold only `PATH` (the run's bin directory with the `gh` shim and `git`
        wrapper) and names starting with `EVAL_`; any other name raises ValueError.
        `sidecar_dir` must be an existing directory of its own, not `run_dir`.
        """
        sidecar = _check_sidecar(run_dir, sidecar_dir)
        for name in harness_env:
            if name != "PATH" and not name.startswith("EVAL_"):
                raise ValueError(f"harness variable {name} is not allowed (only PATH and EVAL_*)")
        gh_dir = sidecar / "gh-config"
        gh_dir.mkdir(exist_ok=True)
        git_config = sidecar / "harness.gitconfig"
        git_config.write_text(GIT_IDENTITY)
        python_dir = str(Path(self.python).parent)
        parts = [harness_env.get("PATH", "")]
        for directory in (python_dir, *SYSTEM_PATH):
            if directory not in parts:
                parts.append(directory)
        env = {k: v for k, v in harness_env.items() if k != "PATH"}
        env.update(
            {name: self.parent_env[name] for name in LOGIN_VARIABLES if name in self.parent_env}
        )
        env.update(
            {
                "HOME": self.home,
                "PATH": ":".join(p for p in parts if p),
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                "GH_CONFIG_DIR": str(gh_dir),
                "GIT_CONFIG_GLOBAL": str(git_config),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ALLOW_PROTOCOL": "file",
            }
        )
        return env

    def agent_argv(self, profile, run_dir):
        """The `claude` command line for a run; `start` swaps `claude` for its absolute path."""
        settings = profiles.settings(profile, run_dir, self.home, extra_read=self.extra_read)
        return [
            "claude",
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--setting-sources",
            "project",
            "--model",
            self.model,
            "--max-budget-usd",
            str(self.max_budget_usd),
            "--tools",
            ",".join(profiles.tools(profile)),
            "--settings",
            json.dumps(settings),
        ]

    def start(self, run_dir, sidecar_dir, argv, env):
        """Start the agent in `run_dir` with exactly `env`; no shell is involved."""
        _check_sidecar(run_dir, sidecar_dir)
        if self.claude_path is None:
            self.prepare()
        argv = list(argv)
        if argv and argv[0] == "claude":
            argv[0] = self.claude_path
        stderr = open(Path(sidecar_dir) / "agent-stderr.log", "w")
        self._stderr_files.append(stderr)
        process = subprocess.Popen(
            argv,
            cwd=str(run_dir),
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            encoding="utf-8",
        )
        self._processes.append(process)
        return process

    def close(self):
        """Terminate (then kill) and reap every started process; safe to call twice."""
        for process in self._processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=self.terminate_timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
        self._processes.clear()
        for stderr in self._stderr_files:
            stderr.close()
        self._stderr_files.clear()


def create(name, **kwargs):
    """The launcher called `name` (`[config] launcher`)."""
    if name == "local":
        return LocalLauncher(**kwargs)
    if name == "devvm":
        raise NotImplementedError(
            "the devvm launcher is not implemented: devvm was not installed in the Phase 1 "
            "spike; set launcher = \"local\""
        )
    raise ValueError(f"unknown launcher {name!r}; expected local")

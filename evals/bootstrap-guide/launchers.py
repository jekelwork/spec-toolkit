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
import signal
import stat
import secrets
import subprocess
import sys
import threading
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import profiles

SYSTEM_PATH = ("/usr/bin", "/bin")
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
LSOF = "/usr/sbin/lsof"
PS = "/bin/ps"
OUTPUT_CAP = 1 << 20
TRUNCATED = "\n[output truncated]"
KILL_WAIT_SECONDS = 5
# Git settings pinned with command-line precedence, so repository config cannot override them.
GIT_PINS = (
    ("core.fsmonitor", "false"),
    ("core.hooksPath", "/dev/null"),
    ("protocol.allow", "never"),
    ("protocol.file.allow", "always"),
)
GIT_IDENTITY = "[user]\n\tname = Eval Harness\n\temail = eval-harness@example.invalid\n"
# Variables of the parent environment that the agent needs: `claude -p` stays logged in through
# the keychain (real HOME) and needs TERM; auto memory stays off. Nothing else is copied.
LOGIN_VARIABLES = ("HOME", "TERM")
# Credentials the judge call may get, in order of preference (see `LocalLauncher.judge_env`).
# A judge call runs only after every agent has ended (`run._judge_all`) and the launcher runs one
# sandboxed call at a time, so nothing else of this evaluation is live while the credential is in
# a process environment. What remains: other processes of the same user on the machine can read
# it for the few seconds of a call (the sandbox does not gate that read).
JUDGE_CREDENTIALS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


def judge_call_env(env, sandbox):
    """`env` for a judge call in `sandbox`: Claude Code keeps scratch files in /tmp/claude-<uid>,
    ignoring TMPDIR, and the sandbox cannot (and must not be allowed to) write there, so point it
    at the call's own tmpdir. The one place that sets this."""
    return {**env, "CLAUDE_CODE_TMPDIR": sandbox.tmpdir}


# What the harness's own subprocesses (git, node, `claude --version`) get from the parent
# environment: an allowlist, so a token under any other name never reaches them. Git needs no
# more (the `GIT_*` settings are set by `harness._git_env`; remotes are local, so no SSH).
HARNESS_VARIABLES = ("PATH", "HOME", "XDG_CONFIG_HOME", "LANG", "LC_ALL", "TERM", "TMPDIR")


def allowlisted_env(env):
    """The `HARNESS_VARIABLES` of `env` and nothing else."""
    return {name: env[name] for name in HARNESS_VARIABLES if name in env}


def _check_sidecar(run_dir, sidecar_dir):
    sidecar = Path(sidecar_dir)
    if not sidecar.is_dir() or sidecar.resolve().is_relative_to(Path(run_dir).resolve()):
        raise ValueError(
            f"sidecar_dir {str(sidecar)!r} must be an existing directory outside run_dir"
        )
    return sidecar


@dataclass
class SandboxSpec:
    """What a sandboxed `run` may touch.

    Every path must be absolute, already normalised and free of symlinks (also in its ancestors):
    the caller resolves them once, the launcher never resolves at run time.
    """

    home: str  # the maintainer's home: reads under it are denied
    home_override: str  # throwaway directory the child gets as HOME
    tmpdir: str  # per-run TMPDIR
    allow_read: tuple = ()  # read as subpaths
    allow_write: tuple = ()  # written as subpaths (the repository)
    write_files: tuple = ()  # written (and read) as exact files (the gh call log)
    exec_paths: tuple = ()  # may be executed (and read): interpreter prefix, git, run bin dir
    # Only the judge call (a `claude -p` with no tools) sets this: it must reach the model and its
    # login service. Commands the agent shows and the user runs never get it.
    network: bool = False
    # The shell and env may be executed (what run_shown commands need). The judge call sets this
    # false: only the paths in `exec_paths` may run.
    system_exec: bool = True


# What python and git need, found empirically: the system's read-only parts only (not ~/Library,
# not /Users, not /usr/local), the shell and env as the only fixed executables, no mach services.
# `file-map-executable` below only lets the loader map libraries; it is not a boundary, the
# process-exec rules and the read rules are.
_SYSTEM_READ = (
    "/usr/lib",
    "/usr/bin",
    "/usr/share",
    "/System",
    "/Library/Developer/CommandLineTools",
    "/Library/Frameworks",
    "/Library/Apple",
    "/bin",
    "/private/var/select",
    "/private/var/db/timezone",
    "/private/var/db/dyld",
    "/dev",
)
_SYSTEM_EXEC = ("/bin/sh", "/bin/bash", "/bin/dash", "/bin/zsh", "/usr/bin/env")


def _path(path):
    """A spec path, checked and returned unchanged (never resolved)."""
    path = str(path)
    if not os.path.isabs(path) or os.path.normpath(path) != path:
        raise ValueError(f"path {path!r} must be absolute and normalised")
    if '"' in path or "\\" in path:
        raise ValueError(f"path {path!r} contains a quote or backslash")
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise ValueError(f"path {path!r} is a symlink")
    except FileNotFoundError:
        pass
    if os.path.realpath(path) != path:
        raise ValueError(f"path {path!r} goes through a symlink")
    return path


def _rules(paths, kind="subpath"):
    return " ".join(f'({kind} "{_path(p)}")' for p in paths)


def _metadata_rules(spec, readable_paths):
    """Metadata stays visible outside home; under home only for the allowed paths and their
    ancestors (a path lookup needs them)."""
    home = Path(_path(spec.home))
    ancestors = set()
    for path in [*readable_paths, *spec.write_files]:
        for parent in Path(_path(path)).parents:
            if parent == home or home in parent.parents:
                ancestors.add(str(parent))
    return " ".join(
        [
            _rules(readable_paths),
            _rules(spec.write_files, "literal"),
            _rules(sorted(ancestors), "literal"),
        ]
    )


# What the judge call may reach (`SandboxSpec.network`): outbound HTTPS and DNS, the resolver's
# socket, and a short list of Mach services. A `curl` under this profile resolved a host and
# connected to port 443 but not to port 80, and `claude -p` (a native binary, with a dummy key)
# reached the API under it and got its 401 back: TLS works with the binary's built-in CAs. The
# judge logs in with a token in its environment, not the keychain, so the trust and keychain
# services (`SecurityServer`, `securityd.xpc`, `trustd*`) are candidates to drop: read the
# sandbox deny log at T049 (`log show --predicate 'sender == "Sandbox"'`) after a real judge
# call, remove what it never names and add only what a failing call names.
_MACH_SERVICES = (
    "com.apple.SecurityServer",
    "com.apple.securityd.xpc",
    "com.apple.trustd",
    "com.apple.trustd.agent",
    "com.apple.mDNSResponder",
    "com.apple.dnssd.service",
    "com.apple.SystemConfiguration.configd",
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.cfprefsd.daemon",
    "com.apple.cfprefsd.agent",
)
_NETWORK_RULES = (
    '(allow network-outbound (remote tcp "*:443"))'
    '(allow network-outbound (remote udp "*:53"))'
    '(allow network-outbound (literal "/private/var/run/mDNSResponder"))'
    "(allow mach-lookup "
    + " ".join(f'(global-name "{name}")' for name in _MACH_SERVICES)
    + ")"
)


def sandbox_profile(spec):
    """The macOS `sandbox-exec` profile for `spec`: deny by default.

    Allowed: fork, executing the listed paths, reading system files and the listed paths, writing
    only the listed paths. Not allowed (the network and Mach lookups only with `spec.network`,
    set for the judge call alone): network, Mach services, Launch Services, Apple events,
    inspecting other processes, reading under `home`. File metadata (stat) outside home stays
    visible; under home it is visible only for the allowed paths and their ancestors.
    """
    system_read = _rules(_SYSTEM_READ)
    readable_paths = [
        *spec.allow_read,
        *spec.exec_paths,
        *spec.allow_write,
        spec.home_override,
        spec.tmpdir,
    ]
    readable = _rules(readable_paths)
    home_rule = f'(subpath "{_path(spec.home)}")'
    writable = " ".join(
        [
            _rules([*spec.allow_write, spec.home_override, spec.tmpdir]),
            _rules(spec.write_files, "literal"),
            '(literal "/dev/null")',
        ]
    )
    return (
        "(version 1)(deny default)(allow process-fork)"
        "(allow process-exec* "
        + " ".join(
            part
            for part in (
                _rules(_SYSTEM_EXEC, "literal") if spec.system_exec else "",
                _rules(spec.exec_paths),
            )
            if part
        )
        + ")"
        f"(allow file-map-executable {system_read} {_rules(spec.exec_paths)})"
        f"(deny file-read* {home_rule})"
        # The call log file is read too: the `gh` shim reads the last line to chain its next
        # one (research D19). Exact file only.
        f'(allow file-read* (literal "/") {system_read} {readable} '
        f'{_rules(spec.write_files, "literal")})'
        "(allow file-read-metadata)"
        f"(deny file-read-metadata {home_rule})"
        f"(allow file-read-metadata {_metadata_rules(spec, readable_paths)})"
        "(allow sysctl-read)(allow signal (target self))"
        '(allow file-ioctl (literal "/dev/null") (literal "/dev/tty"))'
        f"(deny file-write*)(allow file-write* {writable})"
        + (_NETWORK_RULES if spec.network else "")
    )


def _children_of(parents, table):
    """`parents` plus all their descendants in a {pid: ppid} table."""
    found = set(parents)
    grew = True
    while grew:
        grew = False
        for pid, ppid in table.items():
            if ppid in found and pid not in found:
                found.add(pid)
                grew = True
    return found


def kill_call(call_id, dirs, protect=()):
    """SIGKILL what is left of one sandboxed call, best effort.

    Targets processes that carry `EVAL_CALL_ID=<call_id>` in their environment (found with `ps -E`)
    or whose working directory is under one of `dirs` (the call's fresh HOME and TMPDIR, found with
    `lsof`). Never kills this process, the `protect` pids or their descendants. It does not look at
    the run's repository or run root: the live agent and the maintainer's shells work there.

    This is cleanup, not a boundary: a child that clears its environment and leaves the directories
    can survive, and `ps` hides the environment of Apple-signed system binaries, which only the
    directory test then catches. The sandbox is the boundary; the harness's integrity check is
    advisory. Without `ps` or `lsof` a RuntimeWarning is issued for the part that could not run.
    """
    try:
        table = {}
        for line in subprocess.run(
            [PS, "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=30
        ).stdout.splitlines():
            pid, ppid = line.split()
            table[int(pid)] = int(ppid)
        protected = {os.getpid(), *_children_of(set(protect), table)}
        marker = f"EVAL_CALL_ID={call_id}"
        victims = set()
        for line in subprocess.run(
            [PS, "-Eww", "-axo", "pid=,command="], capture_output=True, text=True, timeout=30
        ).stdout.splitlines():
            pid, _, command = line.strip().partition(" ")
            if marker in command.split() and pid.isdigit():
                victims.add(int(pid))
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        warnings.warn(f"stray processes were not cleaned up: ps failed ({error})", RuntimeWarning)
        return
    for directory in dirs:
        try:
            listing = subprocess.run(
                [LSOF, "-a", "-d", "cwd", "-Fp", "+D", str(directory)],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=30,
            ).stdout
        except (OSError, subprocess.TimeoutExpired) as error:
            warnings.warn(f"call directory sweep skipped: lsof failed ({error})", RuntimeWarning)
            continue
        victims.update(
            int(line[1:]) for line in listing.splitlines() if line[:1] == "p" and line[1:].isdigit()
        )
    for pid in victims - protected:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _cap(text):
    return text if len(text) <= OUTPUT_CAP else text[:OUTPUT_CAP] + TRUNCATED


def _text(value):
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


class SandboxUnavailable(RuntimeError):
    """`sandbox-exec` is missing, so a sandboxed run is impossible (no unsandboxed fallback)."""


def require_sandbox_exec():
    """Raises `SandboxUnavailable` unless `/usr/bin/sandbox-exec` exists (the check `run` makes)."""
    if not os.path.exists(SANDBOX_EXEC):
        raise SandboxUnavailable(
            "sandboxed run needs sandbox-exec (macOS); it was not found at " + SANDBOX_EXEC
        )


class LocalLauncher:
    """Runs the agent on the host with a scrubbed environment."""

    def __init__(
        self,
        model,
        max_budget_usd,
        effort,
        home=None,
        parent_env=None,
        claude_path=None,
        python=None,
        terminate_timeout=5.0,
    ):
        if not isinstance(effort, str) or not effort.strip():
            raise ValueError("the agent's effort level is required")
        self.model = model
        self.effort = effort
        self.max_budget_usd = max_budget_usd
        self.parent_env = dict(os.environ if parent_env is None else parent_env)
        self.home = str(home or self.parent_env.get("HOME") or Path.home())
        self.claude_path = claude_path
        self.python = str(python or sys.executable)
        self.terminate_timeout = terminate_timeout
        self._processes = []
        self._calls = {}
        self._stderr_files = []
        self._run_lock = threading.Lock()
        self._lock_wait = threading.local()

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
                cfg = python.parent.parent / "pyvenv.cfg"
                lines = cfg.read_text(encoding="utf-8").splitlines()
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
                "GIT_CONFIG_COUNT": str(len(GIT_PINS)),
                # The engine's imports would write `scripts/__pycache__`, which the integrity
                # check flags as a planted file (research D19).
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        for n, (key, value) in enumerate(GIT_PINS):
            env[f"GIT_CONFIG_KEY_{n}"] = key
            env[f"GIT_CONFIG_VALUE_{n}"] = value
        return env

    def judge_env(self):
        """The environment of a judge call: `PATH`, `TERM`, and a `HOME` that `run` replaces.

        It touches no file (unlike `build_env`, which writes into a run's sidecar directory,
        which the integrity check has already read).
        """
        env = {"PATH": ":".join(SYSTEM_PATH), "HOME": self.home}
        if "TERM" in self.parent_env:
            env["TERM"] = self.parent_env["TERM"]
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        # A throwaway HOME has no login (the keychain search list comes from HOME), so the judge
        # is logged in by a credential in its environment alone: the maintainer's long-lived
        # token from `claude setup-token`, else an API key. It goes nowhere else (`build_env`,
        # the agent's environment, never copies it), and `rules.Judge.secrets` lists it so it is
        # redacted from anything stored.
        for name in JUDGE_CREDENTIALS:
            if self.parent_env.get(name):
                env[name] = self.parent_env[name]
                break
        return env

    def agent_argv(self, profile, run_dir, settings=None):
        """The `claude` command line for a run; `start` swaps `claude` for its absolute path.

        `settings` (a dict or a JSON string) replaces the profile's built `--settings` value.
        """
        if settings is None:
            settings = profiles.settings(profile, run_dir, self.home, extra_read=self.extra_read)
        if not isinstance(settings, str):
            settings = json.dumps(settings)
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
            "--effort",
            self.effort,
            "--max-budget-usd",
            str(self.max_budget_usd),
            "--tools",
            ",".join(profiles.tools(profile)),
            "--settings",
            settings,
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
            start_new_session=True,  # its own process group, so `turns` can stop the whole tree
        )
        self._processes.append(process)
        return process

    def run(self, argv, cwd, env, timeout, *, sandbox, stdin=None):
        """Run a command to completion under `sandbox-exec`: (stdout, stderr, timed_out).

        `stdin`: text sent to the command's standard input (default none: /dev/null), for input
        that must not appear in argv.

        One call at a time per launcher (a lock): a sandboxed process can read the environment of
        another process of the same user (`sysctl` KERN_PROCARGS2 is not gated by the profile;
        probed), so a judge call, which holds a token in its environment, never overlaps another
        sandboxed command, which might read it. The agent's own tool sandbox is not covered.
        See `_run` for what a call does.
        """
        waiting = time.monotonic()
        with self._run_lock:
            self._lock_wait.seconds = getattr(self._lock_wait, "seconds", 0.0) + (
                time.monotonic() - waiting
            )
            return self._run(argv, cwd, env, timeout, sandbox=sandbox, stdin=stdin)

    def take_lock_wait(self):
        """Seconds this thread spent waiting for the run lock since the last call (then reset),
        so a caller can keep that wait out of its own deadline."""
        seconds = getattr(self._lock_wait, "seconds", 0.0)
        self._lock_wait.seconds = 0.0
        return seconds

    def _run(self, argv, cwd, env, timeout, *, sandbox, stdin=None):
        """Run a command to completion under `sandbox-exec`: (stdout, stderr, timed_out).

        Argv list without a shell, `env` with HOME and TMPDIR replaced from `sandbox`, text mode.
        The command gets its own session and its whole process group is killed when it ends or
        times out. Output is capped at 1 Mi characters per stream, applied after the process ends.
        Each call carries a unique `EVAL_CALL_ID` in its environment; afterwards processes with
        that marker, or with a working directory under the call's HOME or TMPDIR, are killed
        (`kill_call`, best effort; the sandbox is the boundary). The live agent and the run's
        other processes are never targeted; `close()` repeats the marker sweep only.
        No unsandboxed fallback: a missing sandbox-exec raises `SandboxUnavailable` (a
        RuntimeError), which the caller (T037/T040) turns into a setup failure (exit 2), never
        into an unsandboxed run.
        """
        require_sandbox_exec()
        profile = sandbox_profile(sandbox)
        env = dict(env)
        env["HOME"] = _path(sandbox.home_override)
        env["TMPDIR"] = _path(sandbox.tmpdir)
        call_id = secrets.token_hex(8)
        env["EVAL_CALL_ID"] = call_id
        self._calls[call_id] = [sandbox.home_override, sandbox.tmpdir]
        process = subprocess.Popen(
            [SANDBOX_EXEC, "-p", profile, *argv],
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        self._processes.append(process)
        timed_out = False
        try:
            try:
                stdout, stderr = process.communicate(input=stdin, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill_group(process)
                try:
                    stdout, stderr = process.communicate(timeout=KILL_WAIT_SECONDS)
                except subprocess.TimeoutExpired as expired:
                    # A grandchild in another session still holds the pipes: give up on them.
                    stdout, stderr = _text(expired.stdout), _text(expired.stderr)
                    for pipe in (process.stdin, process.stdout, process.stderr):
                        if pipe is not None:
                            pipe.close()
                    process.kill()
                    process.wait()
        finally:
            self._kill_group(process)  # also after a normal exit: no leftovers of the run
            if process in self._processes:
                self._processes.remove(process)
            kill_call(
                call_id,
                [sandbox.home_override, sandbox.tmpdir],
                protect=[p.pid for p in self._processes],
            )
        return _cap(stdout), _cap(stderr), timed_out

    @staticmethod
    def _kill_group(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

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
        for call_id, dirs in self._calls.items():  # markers only, never the run root's cwd
            kill_call(call_id, dirs, protect=[])
        self._calls.clear()
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

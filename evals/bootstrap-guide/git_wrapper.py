"""Git wrapper that keeps git network access local during a run (research D3a).

`install()` writes an executable `/bin/sh` launcher named `git` into a bin directory that is
placed first on PATH. It runs `python -I git_wrapper.py --real-git <real git> "$@"`; the absolute
paths of a Python 3.11+ interpreter and of the real git are recorded, shell-quoted, at install
time. The wrapper passes every call to the real git with `os.execv` (no shell).
For the network subcommands only, it inserts
`-c url.file://<run>/origin.git.insteadOf=https://github.com/o/r.git` before the subcommand, so
they reach the run's local bare repository. Everything else, including `remote get-url`, passes
through unchanged.

The run directory comes from the environment variable `EVAL_RUN_DIR` (the directory that
holds `origin.git`). The run environment also sets `GIT_ALLOW_PROTOCOL=file`, so network access
that escapes the rewrite fails instead of reaching GitHub.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

RUN_DIR_ENV = "EVAL_RUN_DIR"
GITHUB_URL = "https://github.com/o/r.git"
NETWORK_SUBCOMMANDS = {"push", "fetch", "pull", "ls-remote", "clone"}
REFUSED_EXIT = 128
SHORT_CLUSTER = re.compile(r"^-[A-Za-z]{2,}")
OUTPUT_SUBCOMMANDS = {"log", "show", "diff"}
# Global `-c` / `--config-env` settings are accepted only for these keys; `commit.gpgsign` only
# as `-c commit.gpgsign=false`. Everything else (alias.*, core.pager, ...) can run commands.
ALLOWED_KEYS = {
    "user.name",
    "user.email",
    "core.quotepath",
    "init.defaultbranch",
    "pull.rebase",
    "pull.ff",
}
ALLOWED_PREFIXES = ("color.", "advice.")
# Global options that take their value as the next argument (when not written `--opt=value`).
GLOBAL_OPTIONS_WITH_VALUE = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--super-prefix",
    "--config-env",
    "--attr-source",
}


def find_subcommand(argv):
    """Return the index of the git subcommand in `argv`, or None."""
    i = 0
    while i < len(argv):
        arg = argv[i]
        if not arg.startswith("-"):
            return i
        if arg in GLOBAL_OPTIONS_WITH_VALUE:
            i += 1
        i += 1
    return None


def is_network_call(argv, index):
    sub = argv[index]
    if sub in NETWORK_SUBCOMMANDS:
        return True
    if sub == "remote":
        rest = [a for a in argv[index + 1 :] if not a.startswith("-")]
        return bool(rest) and rest[0] == "update"
    return False


def _refuse(option):
    print(f"git wrapper: refused option {option}", file=sys.stderr)
    sys.exit(REFUSED_EXIT)


def _is_long_prefix(arg, name, minimum=2):
    """True when `arg` is `--name`, `--name=...` or an abbreviation of it git would accept."""
    if not arg.startswith("--"):
        return False
    given = arg[2:].split("=", 1)[0]
    return len(given) >= minimum and name.startswith(given)


def _config_allowed(option, setting):
    """True when a global `-c` / `--config-env` setting is on the allowlist."""
    key, has_value, value = setting.partition("=")
    key = key.lower()
    if key == "commit.gpgsign":
        return option == "-c" and has_value and value.lower() == "false"
    return key in ALLOWED_KEYS or key.startswith(ALLOWED_PREFIXES)


def check_argv(argv, index):
    """Refuse options that let a caller run commands or write files through git."""
    head = argv[:index]
    i = 0
    while i < len(head):
        arg = head[i]
        option = setting = None
        if arg in ("-c", "--config-env") and i + 1 < len(head):
            option, setting = ("-c" if arg == "-c" else "--config-env"), head[i + 1]
        elif arg.startswith("--config-env="):
            option, setting = "--config-env", arg.split("=", 1)[1]
        if setting is not None and not _config_allowed(option, setting):
            _refuse(f"{arg} {setting}")
        if arg in GLOBAL_OPTIONS_WITH_VALUE:
            i += 1
        i += 1
    if index is None:
        return
    sub = argv[index]
    rest = argv[index + 1 :]
    if "--" in rest:
        rest = rest[: rest.index("--")]
    if sub in OUTPUT_SUBCOMMANDS:
        for arg in rest:
            if _is_long_prefix(arg, "output"):
                _refuse(arg)
    if is_network_call(argv, index):
        for arg in rest:
            long_names = ("upload-pack", "receive-pack", "exec", "config-env")
            if any(_is_long_prefix(arg, name) for name in long_names):
                _refuse(arg)
            cluster = SHORT_CLUSTER.match(arg)
            if cluster and "c" in cluster.group():
                _refuse(arg)
            if cluster and sub != "push" and "u" in cluster.group():
                _refuse(arg)
            if arg.startswith("-c") and not arg.startswith("--"):
                _refuse(arg)
            if sub != "push" and arg.startswith("-u") and not arg.startswith("--"):
                _refuse(arg)


def main(real_git, argv, env=None, execv=os.execv):
    """Run the real git with `argv`, rewriting the GitHub URL for network subcommands."""
    env = os.environ if env is None else env
    index = find_subcommand(argv)
    check_argv(argv, index)
    if index is not None and is_network_call(argv, index):
        run_dir = env.get(RUN_DIR_ENV)
        if not run_dir:
            sys.exit(f"git wrapper: {RUN_DIR_ENV} is not set")
        rewrite = f"url.file://{run_dir}/origin.git.insteadOf={GITHUB_URL}"
        argv = [*argv[:index], "-c", rewrite, *argv[index:]]
    execv(real_git, [real_git, *argv])


def find_real_git():
    return shutil.which("git")


def _interpreter_ok(python):
    probe = "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
    try:
        result = subprocess.run([python, "-c", probe], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def install(bin_dir, real_git, python=None, module_path=None):
    """Write the `git` launcher into `bin_dir` and return its path."""
    python = python or sys.executable
    if not os.path.isabs(python):
        raise ValueError(f"interpreter must be an absolute path: {python}")
    if not _interpreter_ok(python):
        raise ValueError(f"interpreter is missing or older than Python 3.11: {python}")
    if not real_git or not os.path.isabs(real_git):
        raise ValueError(f"real git must be an absolute path: {real_git!r}")
    if not (os.path.isfile(real_git) and os.access(real_git, os.X_OK)):
        raise ValueError(f"real git is missing or not executable: {real_git}")
    script = module_path if module_path is not None else str(Path(__file__).resolve())
    if not os.path.isabs(script) or not os.path.isfile(script):
        raise ValueError(f"module path must be an absolute existing file: {script!r}")
    bin_dir = Path(bin_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher = bin_dir / "git"
    if Path(real_git).resolve() == launcher.resolve() or bin_dir.resolve() in (
        Path(real_git).resolve().parent,
        *Path(real_git).resolve().parents,
    ):
        raise ValueError(f"real git must not be inside the wrapper directory: {real_git}")
    launcher.write_text(
        f"#!/bin/sh\nexec {shlex.quote(python)} -I {shlex.quote(script)} "
        f'--real-git {shlex.quote(real_git)} "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) < 2 or args[0] != "--real-git":
        sys.exit("usage: git_wrapper.py --real-git <path> [git arguments]")
    main(args[1], args[2:])

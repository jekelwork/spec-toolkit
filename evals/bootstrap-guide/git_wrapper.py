"""Git wrapper that keeps git network access local during a run (research D3a).

`install()` writes an executable `/bin/sh` launcher named `git` into a bin directory that is
placed first on PATH. It runs `python -I git_wrapper.py --real-git <real git> "$@"`; the absolute
paths of a Python 3.11+ interpreter and of the real git are recorded, shell-quoted, at install
time. The wrapper passes every call to the real git with `os.execv` (no shell).
For the network subcommands only, it inserts
`-c url.file://<run>/origin.git.insteadOf=https://github.com/o/r.git` before the subcommand, so
they reach the run's local bare repository. Everything else, including `remote get-url`, passes
through unchanged.

Every subcommand is refused unless git's top level is `$EVAL_RUN_DIR/repo` and its git directory is
`<top>/.git` (a nested or redirected repository is never used; a failed probe refuses), and
`diff`, `log` and `show` refuse the options that read stdin or a file (research D19).

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


def locate_work_tree(real_git, head):
    """Return (top level, current directory, absolute git dir) git would use, or None.

    Runs the real git once with the global options in `head`, so `-C` is honored.
    """
    try:
        result = subprocess.run(
            [real_git, *head, "rev-parse", "--show-toplevel", "--show-prefix",
             "--absolute-git-dir"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = result.stdout.split("\n")
    if result.returncode != 0 or len(lines) < 3 or not lines[0] or not lines[2]:
        return None
    return lines[0], os.path.join(lines[0], lines[1]), lines[2]


def _inside(path, top):
    return path == top or path.startswith(top.rstrip(os.sep) + os.sep)


def _spelled_inside(arg, top, cwd):
    """Lexical check, as git's own no-index trigger does it (`prefix_path`, no disk access).

    A relative argument is joined to the directory below the top level and normalized; one that
    climbs above the top level is outside whatever it names after that (`../<repo>/f`). An
    absolute argument is compared with the top level as git spells it."""
    if os.path.isabs(arg):
        return _inside(os.path.normpath(arg), os.path.normpath(top))
    below = os.path.relpath(cwd, top)
    joined = os.path.normpath(os.path.join(below, arg))
    return joined != os.pardir and not joined.startswith(os.pardir + os.sep)


def _check_diff_paths(args, locate):
    """Refuse `git diff` arguments that could start an implicit `--no-index` (research D18).

    Every argument is checked as a possible path, whether or not it starts with `-` and
    whatever `--` or `--end-of-options` precede it: git's no-index option and path parsing is
    not mirrored, because each mirror was bypassed. Each must stay inside the repository both by
    spelling (git's own no-index trigger, which does not resolve symlinks) and by realpath
    (symlinks), so implicit no-index never starts. Options such as `--stat` or
    `--src-prefix=../` stay inside, since their first component is the option text; only an
    argument whose path form escapes (`-x/../../b`) is refused. A bare `-` is refused too: in
    no-index mode it reads stdin. Together with the `--no-index` refusal and the
    outside-a-work-tree refusal, no no-index feature (stdin, FIFOs, outside files) is reachable."""
    found = locate()
    if found is None:
        _refuse("diff outside a work tree")
    top = os.path.realpath(found[0])
    for arg in args:
        if arg == "-":
            _refuse("diff argument -")
        resolved = os.path.realpath(os.path.join(found[1], arg))
        if not _inside(resolved, top) or not _spelled_inside(arg, found[0], found[1]):
            _refuse(f"diff path outside the repository: {arg}")


def _check_run_repository(found, env):
    """Refuse unless git works in the run's own repository (research D19 decision 1).

    The top level must be `$EVAL_RUN_DIR/repo` and the git directory `<top>/.git` (a real
    directory), so a nested repository, a redirected git directory or a repository elsewhere is
    never used. Fails closed: an unset or relative run directory, or a failed probe, refuses."""
    run_dir = env.get(RUN_DIR_ENV)
    if not run_dir or not os.path.isabs(run_dir) or found is None:
        _refuse("git outside the run repository")
    top = os.path.realpath(found[0])
    git_dir = os.path.join(top, ".git")
    if (
        top != os.path.realpath(os.path.join(run_dir, "repo"))
        or os.path.islink(git_dir)
        or os.path.realpath(found[2]) != git_dir
    ):
        _refuse("git outside the run repository")


def _short_bundle_has(arg, letter):
    """True for a single-dash argument with `letter` after its first character (a bundle such
    as `-RO<file>` or `-pO`; a value such as `-SOME` is refused too, a false refusal)."""
    return arg.startswith("-") and not arg.startswith("--") and letter in arg[1:]


def check_global_options(argv, index):
    """Refuse global options that let a caller run commands (checked before any probe)."""
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


def check_argv(argv, index, locate=None):
    """Refuse options that let a caller run commands or write files through git."""
    check_global_options(argv, index)
    if index is None:
        return
    sub = argv[index]
    rest = argv[index + 1 :]
    all_args = rest
    if "--" in rest:
        rest = rest[: rest.index("--")]
    if sub in OUTPUT_SUBCOMMANDS:
        for arg in rest:
            if _is_long_prefix(arg, "output"):
                _refuse(arg)
            # These read standard input or a file that may lie outside the repository.
            if (
                _is_long_prefix(arg, "stdin")
                or _is_long_prefix(arg, "orderfile")
                or _short_bundle_has(arg, "O")
            ):
                _refuse(arg)
    if sub == "diff":
        # `diff --no-index` reads arbitrary files outside the repository (research D18).
        for arg in rest:
            if _is_long_prefix(arg, "no-index", minimum=6):
                _refuse(arg)
        _check_diff_paths(all_args, locate)
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


def main(real_git, argv, env=None, execv=os.execv, locate=None):
    """Run the real git with `argv`, rewriting the GitHub URL for network subcommands."""
    env = os.environ if env is None else env
    index = find_subcommand(argv)
    if locate is None and index is not None:
        head = argv[:index]
        locate = lambda: locate_work_tree(real_git, head)  # noqa: E731
    if index is not None:
        # Checked here first so the probe (`locate`, which runs the real git with these global
        # options) never sees a refused one; `check_argv` checks again for its own callers.
        check_global_options(argv, index)
        found = locate()
        _check_run_repository(found, env)
        locate = lambda: found  # noqa: E731
    check_argv(argv, index, locate)
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

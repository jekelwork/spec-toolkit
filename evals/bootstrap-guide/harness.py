"""Builds the temporary repository for a run and plays the scripted turns.

This part loads, validates and hashes scenarios and fixture states (data-model.md "Scenario" and
"Fixture state"). Python 3.11+, standard library only.

`git.toml` shape (optional): a list of `[[branch]]` entries, each with a unique `name`
(letters, digits, `.`, `_`, `-`, `/`; no leading `-`, no `..`) and an
optional `commits` list of commit messages, created in that order in the local `origin`.

Run directory layout (`build_run`), one per run, outside the home directory:

    <run-root>/repo/         the temporary repository, the agent's working directory
    <run-root>/harness/      sidecar: `gh.toml` (the response table), `gh-calls.jsonl` (created
                             empty before the agent starts; a missing log counts as stale)
    <run-root>/bin/          the `gh` shim and `git` wrapper launchers (first on PATH)
    <run-root>/origin.git    bare repository behind `origin` (the git wrapper's `EVAL_RUN_DIR`)

`run_shown` (research D5) accepts one engine command, as an argv list, and never uses a shell.
"""

import atexit
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from stat import S_ISDIR, S_ISREG

import gh_shim
import git_wrapper
import launchers
import profiles
import transcript
import rules

MODES = ("guide me", "do it for me")
EXPECTS = ("preview", "question", "one_command", "skill_call")
TURN_KINDS = ("say", "run_shown", "say_after_output")
DEFAULT_RUNS = 5
PLACEHOLDER = "fake_token"

_KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SCENARIO_KEYS = {"name", "description", "fixture", "mode", "turns", "rules", "tools", "runs"}


class ScenarioError(ValueError):
    """A scenario or fixture file is invalid; `errors` lists every problem found."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class Turn:
    kind: str  # "say", "run_shown" or "say_after_output"
    text: str | None = None
    expect: str | None = None  # one of EXPECTS, "pattern", or None
    expect_pattern: str | None = None


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    fixture: str
    mode: str
    turns: tuple
    rules: tuple
    tools: str
    runs: int
    path: Path


@dataclass(frozen=True)
class GhCall:
    argv: tuple
    stdout: str = ""
    stderr: str = ""
    exit: int = 0
    writes: bool = False
    reason: str = ""
    query: str | None = None


@dataclass(frozen=True)
class Branch:
    name: str
    commits: tuple = ()


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _read_toml(path):
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle), []
    except tomllib.TOMLDecodeError as error:
        return None, [f"{path.name}: invalid TOML: {error}"]
    except OSError as error:
        return None, [f"{path.name}: cannot read: {error}"]


def _check_placeholders(text, allowed, where, errors):
    for name in _PLACEHOLDER.findall(text):
        if name != "{" + PLACEHOLDER + "}" or not allowed:
            errors.append(
                f"{where}: placeholder {name} not allowed "
                f"(only {{{PLACEHOLDER}}}, only in say turns)"
            )


def _parse_turn(index, raw, errors, label):
    where = f"{label}: turn {index + 1}"
    if not isinstance(raw, dict):
        errors.append(f"{where}: must be a table")
        return None
    for key in raw:
        if key not in (*TURN_KINDS, "expect"):
            errors.append(f"{where}: unknown key {key!r}")
    kinds = [k for k in TURN_KINDS if k in raw]
    if len(kinds) != 1:
        errors.append(f"{where}: needs exactly one of {', '.join(TURN_KINDS)}")
        return None
    kind = kinds[0]
    text = None
    if kind == "run_shown":
        if raw[kind] is not True:
            errors.append(f"{where}: run_shown must be true")
    elif not isinstance(raw[kind], str):
        errors.append(f"{where}: {kind} must be a string")
    else:
        text = raw[kind]
        if text.lstrip().startswith("/"):
            errors.append(
                f"{where}: a message may not start with `/`: a slash command runs a skill "
                "without the profile's Skill allowlist"
            )
        _check_placeholders(text, kind == "say", where, errors)
    expect = pattern = None
    if "expect" in raw:
        value = raw["expect"]
        if isinstance(value, str) and value in EXPECTS:
            expect = value
        elif isinstance(value, dict) and set(value) == {"pattern"} and isinstance(
            value["pattern"], str
        ):
            pattern = value["pattern"]
            expect = "pattern"
            try:
                re.compile(pattern)
            except re.error as error:
                errors.append(f"{where}: expect pattern is not a valid regex: {error}")
            _check_placeholders(pattern, False, where, errors)
        else:
            choices = ", ".join(EXPECTS)
            errors.append(
                f"{where}: expect must be one of {choices} or {{ pattern = \"<regex>\" }}"
            )
    if index == 0 and (kind != "say" or "expect" in raw):
        errors.append(
            f"{where}: the first turn must be a plain `say` without `expect`: "
            "there is no previous reply to check or to run a command from"
        )
    return Turn(kind, text, expect, pattern)


def _fixture_ok(fixture, fixtures_dir):
    if not isinstance(fixture, str) or not _KEBAB.match(fixture):
        return False
    base = Path(fixtures_dir).resolve()
    path = (base / fixture).resolve()
    return path.is_dir() and path.parent == base


def _parse_scenario(path, fixtures_dir):
    """Return (Scenario or None, errors)."""
    path = Path(path)
    label = path.name
    data, errors = _read_toml(path)
    if data is None:
        return None, errors
    for key in data:
        if key not in _SCENARIO_KEYS:
            errors.append(f"{label}: unknown field {key!r}")

    name = data.get("name")
    if name != path.stem:
        errors.append(f"{label}: name must equal the file name {path.stem!r}, got {name!r}")
    if not _KEBAB.match(path.stem):
        errors.append(f"{label}: file name must be kebab-case")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.append(f"{label}: description must be a non-empty string")
    fixture = data.get("fixture")
    if not _fixture_ok(fixture, fixtures_dir):
        errors.append(
            f"{label}: fixture {fixture!r} must be a kebab-case directory directly "
            "under the fixtures directory"
        )
    mode = data.get("mode")
    if mode not in MODES:
        errors.append(f"{label}: mode must be one of {', '.join(map(repr, MODES))}")

    raw_turns = data.get("turns")
    turns = []
    if not isinstance(raw_turns, list) or not raw_turns:
        errors.append(f"{label}: needs at least one turn")
    else:
        for index, raw in enumerate(raw_turns):
            turn = _parse_turn(index, raw, errors, label)
            if turn:
                turns.append(turn)

    rule_ids = data.get("rules")
    if not isinstance(rule_ids, list) or not rule_ids:
        errors.append(f"{label}: rules must be a non-empty list of rule IDs")
        rule_ids = []
    if len(set(map(str, rule_ids))) != len(rule_ids):
        errors.append(f"{label}: duplicate rule IDs")
    for rule_id in rule_ids:
        try:
            rules.get(rule_id)
        except (KeyError, TypeError):
            errors.append(f"{label}: unknown rule {rule_id!r}")

    tools = data.get("tools", profiles.DEFAULT_PROFILE)
    if tools not in profiles.PROFILES:
        errors.append(f"{label}: tools {tools!r} is not a profile ({', '.join(profiles.PROFILES)})")
    runs = data.get("runs", DEFAULT_RUNS)
    if not _is_int(runs) or runs < 1:
        errors.append(f"{label}: runs must be a positive integer")

    if errors:
        return None, errors
    scenario = Scenario(
        name, description, fixture, mode, tuple(turns), tuple(rule_ids), tools, runs, path
    )
    return scenario, []


def load_scenario(path, fixtures_dir):
    """Load one scenario file; raises `ScenarioError` listing every problem."""
    scenario, errors = _parse_scenario(path, fixtures_dir)
    if errors:
        raise ScenarioError(errors)
    return scenario


def _scenario_files(scenarios_dir):
    return sorted(Path(scenarios_dir).glob("*.toml"))


def load_all(scenarios_dir, fixtures_dir):
    """Load every scenario, sorted by name; raises `ScenarioError` with all problems."""
    loaded, errors = [], []
    for path in _scenario_files(scenarios_dir):
        scenario, errs = _parse_scenario(path, fixtures_dir)
        errors += errs
        if scenario:
            loaded.append(scenario)
    if errors:
        raise ScenarioError(errors)
    return loaded


def load_gh_table(fixture_dir):
    """The `gh.toml` response table of a fixture (empty when it has none)."""
    path = Path(fixture_dir) / "gh.toml"
    if not path.is_file():
        return ()
    data, errors = _read_toml(path)
    if data is None:
        raise ScenarioError(errors)
    allowed = {"argv", "stdout", "stderr", "exit", "writes", "reason", "query"}
    calls = []
    entries = data.get("call", [])
    if set(data) - {"call"} or not isinstance(entries, list):
        errors.append("gh.toml: only a list of [[call]] entries is allowed")
        entries = []
    for index, entry in enumerate(entries):
        where = f"gh.toml: call {index + 1}"
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be a table")
            continue
        n = len(errors)
        for key in entry:
            if key not in allowed:
                errors.append(f"{where}: unknown key {key!r}")
        argv = entry.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            errors.append(f"{where}: argv must be a non-empty list of strings")
        elif "**" in argv[:-1]:
            errors.append(f"{where}: ** is allowed only as the last argv pattern")
        for key in ("stdout", "stderr"):
            if not isinstance(entry.get(key, ""), str):
                errors.append(f"{where}: {key} must be a string")
        code = entry.get("exit", 0)
        if not _is_int(code) or not 0 <= code <= 255:
            errors.append(f"{where}: exit must be an integer from 0 to 255")
        if not isinstance(entry.get("writes", False), bool):
            errors.append(f"{where}: writes must be a boolean")
        query = entry.get("query")
        if query is not None:
            if not isinstance(query, str):
                errors.append(f"{where}: query must be a string")
            else:
                try:
                    re.compile(query)
                except re.error as error:
                    errors.append(f"{where}: query is not a valid regex: {error}")
        reason = entry.get("reason", "")
        if not isinstance(reason, str):
            errors.append(f"{where}: reason must be a string")
        elif entry.get("writes") is False and not reason.strip():
            errors.append(f"{where}: writes = false needs a non-empty reason")
        if len(errors) == n:
            calls.append(
                GhCall(
                    tuple(argv),
                    entry.get("stdout", ""),
                    entry.get("stderr", ""),
                    entry.get("exit", 0),
                    entry.get("writes", False),
                    entry.get("reason", ""),
                    query,
                )
            )
    if errors:
        raise ScenarioError(errors)
    return tuple(calls)


def load_git_spec(fixture_dir):
    """The branches of a fixture's optional `git.toml` (empty when it has none)."""
    path = Path(fixture_dir) / "git.toml"
    if not path.is_file():
        return ()
    data, errors = _read_toml(path)
    if data is None:
        raise ScenarioError(errors)
    entries = data.get("branch", [])
    if set(data) - {"branch"} or not isinstance(entries, list):
        errors.append("git.toml: only a list of [[branch]] entries is allowed")
        entries = []
    branches, seen = [], set()
    for index, entry in enumerate(entries):
        where = f"git.toml: branch {index + 1}"
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be a table")
            continue
        n = len(errors)
        for key in entry:
            if key not in ("name", "commits"):
                errors.append(f"{where}: unknown key {key!r}")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            errors.append(f"{where}: name must be a non-empty string")
        elif not _BRANCH.match(name) or ".." in name:
            errors.append(f"{where}: invalid branch name {name!r}")
        elif name in seen:
            errors.append(f"{where}: duplicate branch {name!r}")
        else:
            seen.add(name)
        commits = entry.get("commits", [])
        if not isinstance(commits, list) or not all(isinstance(c, str) for c in commits):
            errors.append(f"{where}: commits must be a list of strings")
        if len(errors) == n:
            branches.append(Branch(name, tuple(commits)))
    if errors:
        raise ScenarioError(errors)
    return tuple(branches)


def validate(scenarios_dir, fixtures_dir):
    """Every problem in the scenarios and the fixtures they use, as readable strings."""
    errors, fixtures = [], set()
    for path in _scenario_files(scenarios_dir):
        scenario, errs = _parse_scenario(path, fixtures_dir)
        errors += errs
        if scenario:
            fixtures.add(scenario.fixture)
    for fixture in sorted(fixtures):
        errors += [f"fixture {fixture}: {e}" for e in _symlinks(Path(fixtures_dir) / fixture)]
        for loader in (load_gh_table, load_git_spec):
            try:
                loader(Path(fixtures_dir) / fixture)
            except ScenarioError as error:
                errors += [f"fixture {fixture}: {e}" for e in error.errors]
    return errors


def _walk(root, onerror=None):
    """Yield (relative POSIX path, absolute path) of every entry below root, not following links.

    `onerror(error)` is called for a directory that cannot be listed (`os.walk` skips it
    silently otherwise)."""
    for current, dirs, names in os.walk(root, followlinks=False, onerror=onerror):
        for name in [*dirs, *names]:
            path = Path(current) / name
            yield path.relative_to(root).as_posix(), path


def _symlinks(fixture_dir):
    return [
        f"{rel} is a symlink; fixtures may not contain symlinks"
        for rel, path in sorted(_walk(fixture_dir))
        if path.is_symlink()
    ]


def _file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def scenario_hash(scenario, fixtures_dir):
    """`sha256:<hex>` over the scenario file, its fixture directory and its tool profile.

    The profile is serialised with placeholder paths so the hash is the same on every machine.
    """
    fixture_dir = Path(fixtures_dir) / scenario.fixture
    files = {}
    for rel, path in _walk(fixture_dir):
        if path.is_symlink():
            files[rel] = ["symlink", os.readlink(path)]
        elif path.is_file():
            files[rel] = [_file_digest(path), bool(path.stat().st_mode & EXEC_BIT)]
    profile = {
        "tools": profiles.tools(scenario.tools),
        "settings": profiles.settings(scenario.tools, "<RUN>", "<HOME>"),
    }
    document = {"scenario": _file_digest(scenario.path), "fixture": files, "profile": profile}
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


# --- run_shown: the token check and execution (research D5) ---------------------------------


class ShownCommandRejected(ValueError):
    """The command in the agent's message is not one the harness runs; it was not run."""


# `--create-board` is a bare flag in the engine (`store_true`), not a value flag.
_BARE_FLAGS = ("--report-exit", "--review-notes", "--full-dry-run", "--create-board")
_INIT_FLAGS = (
    "--description",
    "--required-approvals",
    "--project-number",
    "--owner",
    "--name",
)
_DIGIT_FLAGS = ("--issue", "--required-approvals", "--project-number")
_LABEL = re.compile(r"^\**Run this (here|in your terminal)\b[^:]*:\**\s*$")
_PREFIX = ["python3", "scripts/bootstrap.py", "start", "--no-input"]
_CONFIRM = re.compile(r"^[A-Za-z0-9_.-]+@[0-9a-f]+$")
_DIGITS = re.compile(r"^[0-9]+$")
_AGENTS = re.compile(r"^(claude|copilot|codex)(,(claude|copilot|codex))*$")
# A free-text value may not hold anything a shell would act on, even inside quotes.
_UNSAFE_VALUE = re.compile(r"[\x00-\x1f\x7f$`;&|<>\\]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")  # GitHub user or organisation login
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")  # GitHub repository or project name
# Format, line and paragraph separators and control characters: invisible or reordering text.
_INVISIBLE_CATEGORIES = ("Cc", "Cf", "Zl", "Zp")


def _reject(reason):
    raise ShownCommandRejected(reason)


def _has_invisible(text):
    return any(unicodedata.category(ch) in _INVISIBLE_CATEGORIES for ch in text)


def _comment_and_indented_lines(lines, fenced):
    """Indexes of lines in HTML comments or indented code blocks, outside the `fenced` lines.

    A line that touches a comment is hidden whole, and a line is rescanned after each `-->` for a
    comment that opens again. Every line indented by four or more spaces or a tab is hidden,
    including a list item's continuation lines: that fails closed (a label there is never
    counted, so the message is rejected rather than a command taken from it).
    """
    hidden = set()
    in_comment = False
    for i, line in enumerate(lines):
        if i in fenced:
            continue
        if not in_comment and line.startswith(("    ", "\t")):
            hidden.add(i)
            continue
        position = 0
        while True:
            if in_comment:
                end = line.find("-->", position)
                hidden.add(i)
                if end < 0:
                    break
                in_comment, position = False, end + 3
            else:
                start = line.find("<!--", position)
                if start < 0:
                    break
                in_comment, position = True, start + 4
    return hidden


def _command_line(message):
    """The one line of the code block right after the command label, without a leading `! `.

    The label is `Run this here...:` or `Run this in your terminal...:` (SKILL.md). Other blocks
    of the message, such as a preview, are ignored, and so are label lines inside fenced or
    indented code blocks and HTML comments; two command labels are rejected.
    """
    lines, fences = transcript.fences(message)
    inside = {i for fence in fences for i in range(fence.start - 1, fence.end + 1)}
    inside |= _comment_and_indented_lines(lines, inside)
    labels = [
        i for i, line in enumerate(lines) if i not in inside and _LABEL.match(line.strip())
    ]
    if len(labels) != 1:
        _reject(f"expected exactly one command label line, found {len(labels)}")
    index = labels[0] + 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    block = next((fence for fence in fences if fence.start - 1 == index), None)
    if block is None:
        _reject("no code block directly after the command label")
    if block.end >= len(lines):  # `transcript.fences` runs an unclosed fence to the end
        _reject("unclosed code block after the command label")
    body = lines[block.start : block.end]
    while body and not body[-1].strip():
        body.pop()
    if len(body) != 1 or not body[0].strip():
        _reject("the code block must hold exactly one command line")
    line = body[0].strip()
    if line.startswith("! "):
        line = line[2:].lstrip()
    if _CONTROL.search(line) or _has_invisible(line):
        _reject("control, format and separator characters are not allowed")
    return line


def _value_ok(flag, value):
    if flag == "--confirm":
        return bool(_CONFIRM.match(value))
    if flag in _DIGIT_FLAGS:
        return bool(_DIGITS.match(value))
    if flag == "--agents":
        return bool(_AGENTS.match(value))
    if flag == "--owner":
        return bool(_OWNER.match(value))
    if flag == "--name":
        return bool(_NAME.match(value)) and value not in (".", "..")
    return (
        bool(value)
        and not value.startswith("-")
        and not _UNSAFE_VALUE.search(value)
        and not _has_invisible(value)
    )


def _check_options(rest):
    """Check the tokens after the fixed prefix; raise ShownCommandRejected on any deviation."""
    if "--agents" in rest:
        others = [t for t in rest if t != "--agents"]
        if len(rest) - len(others) != 1:
            _reject("--agents may appear once")
        index = rest.index("--agents")
        if index + 1 >= len(rest) or not _value_ok("--agents", rest[index + 1]):
            _reject("--agents needs a value like claude,copilot,codex")
        remaining = rest[:index] + rest[index + 2 :]
        if remaining not in ([], ["--report-exit"]):
            _reject("--agents allows only --report-exit besides itself")
        return
    seen = set()
    i = 0
    while i < len(rest):
        token = rest[i]
        if token in seen:
            _reject(f"repeated flag {token}")
        if token in _BARE_FLAGS:
            seen.add(token)
            i += 1
        elif token == "--guided":
            if i != len(rest) - 1:
                _reject("--guided must be last")
            i += 1
        elif token in ("--confirm", "--issue", *_INIT_FLAGS):
            if i + 1 >= len(rest) or not _value_ok(token, rest[i + 1]):
                _reject(f"{token} needs one valid value")
            seen.add(token)
            i += 2
        else:
            _reject(f"token not allowed: {token!r}")
    if "--create-board" in seen and "--project-number" in seen:
        _reject("--create-board and --project-number are mutually exclusive")


def parse_shown_command(message):
    """The argv of the one engine command in `message`, or ShownCommandRejected."""
    line = _command_line(message)
    try:
        argv = shlex.split(line)
    except ValueError as error:
        _reject(f"cannot split the command: {error}")
    if argv[: len(_PREFIX)] != _PREFIX:
        _reject("the command must start with: " + " ".join(_PREFIX))
    _check_options(argv[len(_PREFIX) :])
    return argv


class RunTampered(Exception):
    """`scripts/` or `.git` of the run's repository changed; the run is unscored `tampered`.

    `changed` lists the relative paths (see `check_integrity`). Not a `ShownCommandRejected`:
    the caller must not score it as a badly formed command. The check behind it is
    point-in-time (a process left in the sandbox could change files afterwards).
    """

    def __init__(self, changed):
        self.changed = list(changed)
        super().__init__("run files changed: " + ", ".join(self.changed))


def _check_run_paths(paths):
    """Names of the stored sandbox paths that are no longer what the build recorded.

    The call log must still be the same regular, unlinked, single-link file, and the repository
    and `origin.git` real directories; a symlink or replacement there could turn a sandbox rule
    into access to something else. Missing recorded values fail closed.
    """
    bad = []
    try:
        stat = os.lstat(paths.gh_calls_real)
        same = (stat.st_dev, stat.st_ino) == tuple(paths.gh_calls_id or ())
        if not (S_ISREG(stat.st_mode) and stat.st_nlink == 1 and same):
            bad.append("harness/gh-calls.jsonl")
    except (OSError, TypeError):
        bad.append("harness/gh-calls.jsonl")
    try:
        if not S_ISDIR(os.lstat(paths.repo_real).st_mode):
            bad.append("repo")
    except (OSError, TypeError):
        bad.append("repo")
    try:
        if not S_ISDIR(os.lstat(paths.origin_real).st_mode):
            bad.append("origin.git")
    except (OSError, TypeError):
        bad.append("origin.git")
    return bad


def _fresh_dir(parent, prefix):
    return os.path.realpath(tempfile.mkdtemp(prefix=prefix, dir=os.path.realpath(parent)))


def _sandbox(paths, launcher, python, home, home_dir, tmp_dir, cache_dir):
    """The `SandboxSpec` of one command, built only from the paths stored at build time."""
    real_python = os.path.realpath(python)
    # The interpreter's prefix: Homebrew re-execs Python.app inside it.
    prefixes = [os.path.dirname(os.path.dirname(real_python))]
    if real_python == os.path.realpath(sys.executable):
        prefixes.append(os.path.realpath(sys.base_prefix))
    else:
        # A venv interpreter: its pyvenv.cfg names the interpreter it was made from.
        cfg = Path(os.path.abspath(python)).parent.parent / "pyvenv.cfg"
        try:
            lines = cfg.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            lines = []
        for line in lines:
            key, _, value = line.partition("=")
            if key.strip() == "home" and value.strip():
                prefixes.append(os.path.realpath(os.path.dirname(value.strip())))
    prefixes = list(dict.fromkeys(prefixes))
    git_paths = [os.path.realpath(p) for p in (paths.git_paths or (paths.git_real,))]
    return launchers.SandboxSpec(
        home=os.path.realpath(str(home or getattr(launcher, "home", None) or Path.home())),
        home_override=home_dir,
        tmpdir=tmp_dir,
        allow_read=list(
            dict.fromkeys(
                [
                    paths.repo_real,
                    paths.bin_real,
                    *prefixes,
                    cache_dir,
                    # The shim's table, git's global config and `gh`'s config directory.
                    paths.gh_table_real,
                    paths.gitconfig_real,
                    paths.gh_config_real,
                    paths.origin_real,
                ]
            )
        ),
        # The engine pushes to `origin.git`; hooks are pinned off by the launcher's git config.
        allow_write=[paths.repo_real, paths.origin_real],
        write_files=[paths.gh_calls_real],
        exec_paths=list(
            dict.fromkeys(
                [paths.bin_real, *prefixes, real_python, *git_paths, paths.git_exec_real]
            )
        ),
    )


def _git_exec_path(git_path):
    """The resolved `git --exec-path` directory (where git finds upload-pack and friends)."""
    try:
        done = subprocess.run(
            [git_path, "--exec-path"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=launchers.allowlisted_env(os.environ),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    exec_path = done.stdout.strip()
    return os.path.realpath(exec_path) if exec_path else ""


def _git_binaries(git_path):
    """Resolved paths of the git the wrapper runs, `/usr/bin/git` and the real git behind it.

    On macOS `/usr/bin/git` hands over to the git under the developer tools; `git --exec-path`
    names that installation.
    """
    found = [os.path.realpath(git_path)]
    if os.path.isfile("/usr/bin/git"):
        found.append(os.path.realpath("/usr/bin/git"))
    try:
        done = subprocess.run(
            [git_path, "--exec-path"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=launchers.allowlisted_env(os.environ),
        )
        exec_path = done.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        exec_path = ""
    if exec_path:
        candidate = os.path.join(os.path.dirname(os.path.dirname(exec_path)), "bin", "git")
        if os.path.isfile(candidate):
            found.append(os.path.realpath(candidate))
    return tuple(dict.fromkeys(found))


def run_shown(message, launcher, paths, env, python=None, timeout=120, home=None):
    """Run the command shown in `message` in the run's repository through `launcher`.

    Returns the `!` output form. `paths` is the run's `RunPaths`; `env` is the launcher's built
    environment, so the `gh` shim and `git` wrapper are on PATH. `python3` is replaced by the
    absolute interpreter (`python`, default `paths.python`) so a planted `python3` is never
    picked up. The command runs as `launcher.run(argv, cwd, env, timeout, sandbox=<spec>)`,
    which returns `(stdout, stderr, timed_out)`; the sandbox may write the repository and the
    gh call log file (a literal rule), and gets fresh, empty `HOME` and `TMPDIR` directories for
    each call. The environment adds `PYTHONPYCACHEPREFIX` (a fresh empty directory, so a planted
    `.pyc` is never read), `PYTHONDONTWRITEBYTECODE=1` and `PYTHONNOUSERSITE=1`.

    A rejected command raises ShownCommandRejected; changed run files, or a call log or
    repository that is no longer what the build recorded, raise RunTampered. Neither reaches the
    launcher.
    """
    shown = parse_shown_command(message)
    changed = sorted({*check_integrity(paths), *_check_run_paths(paths)})
    if changed:
        raise RunTampered(changed)
    python = python or paths.python
    argv = [python, *shown[1:]]
    home_dir = _fresh_dir(paths.shown_home, "call-")
    tmp_dir = _fresh_dir(paths.shown_tmp, "tmp-")
    cache_dir = _fresh_dir(paths.shown_tmp, "pyc-")
    sandbox = _sandbox(paths, launcher, python, home, home_dir, tmp_dir, cache_dir)
    call_env = {
        **env,
        "PYTHONPYCACHEPREFIX": cache_dir,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    stdout, stderr, timed_out = launcher.run(
        argv, str(paths.repo), call_env, timeout, sandbox=sandbox
    )
    stdout, stderr = _text(stdout), _text(stderr)
    if timed_out:
        stderr += f"timed out after {timeout}s\n"
    return (
        f"<bash-input>{shlex.join(shown)}</bash-input>"
        f"<bash-stdout>{stdout}</bash-stdout><bash-stderr>{stderr}</bash-stderr>"
    )


def _text(value):
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


# --- the temporary repository of a run (research D2, D3a) -----------------------------------

EXEC_BIT = 0o100  # the owner execute bit decides "exec" everywhere (overlay, hash, export)
REPO_ROOT = Path(__file__).resolve().parents[2]
ORIGIN_URL = git_wrapper.GITHUB_URL
_FULL_SHA = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")


class RepoBuildError(Exception):
    """The run's repository could not be built."""


@dataclass(frozen=True)
class RunPaths:
    """Where a run lives (see the module docstring) and what it was built from."""

    run_root: Path
    repo: Path
    sidecar: Path
    bin_dir: Path
    origin: Path
    gh_table: Path
    gh_calls: Path
    toolkit_ref: str | None  # full SHA when built with `toolkit_ref`, else None (working tree)
    python: str = ""  # absolute interpreter of the wrappers and of `run_shown` commands
    shown_home: Path | None = None  # throwaway HOME of `run_shown` commands
    shown_tmp: Path | None = None  # throwaway TMPDIR of `run_shown` commands
    # sha256 of every file under `scripts/` and of `.git/config`, `.git/hooks`, `.git/info`
    fingerprints: dict = field(default_factory=dict)
    # {file name: bytes} of `.claude/agents/` as `agent_setup.py sync` generates it, computed at
    # build time (`_expected_claude_agents`); `check_integrity` compares with it only.
    agents_expected: dict = field(default_factory=dict)
    # Resolved strings recorded at build time; `run_shown` builds its sandbox from these only.
    repo_real: str = ""
    bin_real: str = ""
    gh_calls_real: str = ""
    gh_calls_id: tuple = ()  # (st_dev, st_ino) of the call log
    git_real: str = ""  # the real git the wrapper runs
    git_paths: tuple = ()  # git_real plus `/usr/bin/git` and the git behind it, all resolved
    warnings: tuple = ()  # for example a skipped secret scan
    # More resolved strings for the sandbox: what the `gh` shim and git read and write.
    gh_table_real: str = ""  # the shim's response table
    gitconfig_real: str = ""  # `<sidecar>/harness.gitconfig`, written by `build_env`
    gh_config_real: str = ""  # `<sidecar>/gh-config`, GH_CONFIG_DIR
    origin_real: str = ""  # `origin.git`: read and written (the engine pushes)
    git_exec_real: str = ""  # `git --exec-path`, for upload-pack and receive-pack

    def agent_settings(self, profile, home, extra_read=()):
        """`profiles.settings` for the agent: it may write the repository and the call log only.

        `bin_dir` is readable (the wrappers) but not writable; `origin.git` and the sidecar,
        except the single `gh-calls.jsonl` file, are neither.
        """
        return profiles.settings(
            profile,
            self.repo,
            home,
            extra_read=(*extra_read, str(self.bin_dir)),
            extra_write=(str(self.gh_calls),),
        )

    def harness_env(self):
        """The variables for `LocalLauncher.build_env` (PATH and `EVAL_*` only)."""
        return {
            "PATH": str(self.bin_dir),
            "EVAL_RUN_DIR": str(self.run_root),
            "EVAL_GH_TABLE": str(self.gh_table),
            "EVAL_GH_CALLS": str(self.gh_calls),
        }


_WATCHED = (
    "scripts",
    ".git/hooks",
    ".git/info",
    ".claude",
    ".agents/skills",
    "agent-definitions",  # what `.claude/agents/` is generated from
)
_CONFIG = ".git/config"
# Compared by `_fold` key: the file systems the harness runs on do not tell the spellings apart.
_INSTRUCTION_FILES = {".gitattributes", "claude.md", "claude.local.md", "agents.md"}


def fingerprint(repo, origin=None):
    """{relative POSIX path: sha256} of the files `check_integrity` compares byte for byte.

    That is everything under `scripts/` (Python bytecode caches excepted), `.claude/`,
    `.agents/skills/` (the agent reads project settings and skills from there), `.git/hooks/` and
    `.git/info/`, hashed in Python, never through git, plus every `.gitattributes` in the
    repository (it can name filters and drivers) and every `CLAUDE.md`, `CLAUDE.local.md` and
    `AGENTS.md` (instruction files the agent reads; the root ones are exported toolkit files).
    `.git/config` is not hashed because git itself writes it (branch tracking);
    `config_problem` checks what it contains instead. A directory that cannot be listed is
    recorded as unsafe.

    With `origin` (the bare `origin.git`), its `hooks/` entries are listed too, as
    `origin.git/hooks/...`; a push may add objects and refs there but never hooks.
    """
    repo = Path(repo)
    result = {}
    if origin is not None:
        _fingerprint_dir(Path(origin) / "hooks", "origin.git/hooks", result)
    export = _export_toolkit()

    def unlistable(error):  # its files could be anything: fail closed
        result[Path(error.filename).relative_to(repo).as_posix()] = UNSAFE

    for current, dirs, names in os.walk(repo, followlinks=False, onerror=unlistable):
        if Path(current) == repo and ".git" in dirs:
            dirs.remove(".git")
        for name in [*names, *dirs]:
            if _fold(export, name) in _INSTRUCTION_FILES:
                path = Path(current) / name
                result[path.relative_to(repo).as_posix()] = _entry_digest(path)
    for watched in _WATCHED:
        _fingerprint_dir(repo / watched, watched, result)
    return result


def _fingerprint_dir(start, prefix, result):
    """Add every entry below `start` to `result` as `<prefix>/<relative path>`.

    A watched directory that is a symlink or another non-directory is itself recorded as
    unsafe, so replacing it with a link cannot hide its contents.
    """
    if start.is_symlink() or (os.path.lexists(start) and not start.is_dir()):
        result[prefix] = UNSAFE
        return
    if not start.is_dir():
        return
    def unlistable(error):
        rel = Path(error.filename).relative_to(start).as_posix()
        result[prefix if rel == "." else f"{prefix}/{rel}"] = UNSAFE

    for rel, path in _walk(start, unlistable):
        if "__pycache__" in rel.split("/"):
            continue
        if prefix == ".claude" and rel.split("/")[0] == "agents":
            continue  # generated by the engine's `agent_setup.py sync`: `_agents_problems`
        if path.is_dir() and not path.is_symlink():
            continue  # its entries are listed on their own
        if prefix == ".claude" and path.is_symlink():
            # `.claude/skills` is a declared symlink of the export: compare its target.
            result[f"{prefix}/{rel}"] = "link:" + os.readlink(path)
            continue
        result[f"{prefix}/{rel}"] = _entry_digest(path)


MAX_READ = 1 << 20  # the most the harness reads from any file in the run's repository
UNSAFE = "unsafe"  # the fingerprint of a symlink, non-regular or oversize file


def read_regular(path):
    """The bytes of a regular file of at most `MAX_READ` bytes, or None.

    The one way the harness reads a file of the run's repository: opened with `O_NOFOLLOW` and
    `O_NONBLOCK` (a symlink fails, a FIFO opens without waiting), `fstat` must say regular file,
    and one byte more than the limit is read to detect an oversize file. Anything else, including
    a missing file, gives None.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks, size = [], 0
        while size <= MAX_READ:
            chunk = os.read(fd, MAX_READ + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return None if size > MAX_READ else b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def _entry_digest(path):
    data = read_regular(path)
    return UNSAFE if data is None else hashlib.sha256(data).hexdigest()


# The only settings a run's `.git/config` may hold (an allowlist: sections and keys lower case;
# None allows every key). `git init`, `git remote add`, `git push -u` and the like write these.
_ALLOWED_CONFIG = {
    "core": {
        "repositoryformatversion", "filemode", "bare", "logallrefupdates", "ignorecase",
        "precomposeunicode", "symlinks",
    },
    "remote": {"url", "fetch", "pushurl"},
    "branch": {"remote", "merge"},
    "user": None,
    "init": None,
    # `start --agents` runs `git config --local agent-setup.tools claude,codex`; the value is
    # checked against `_AGENT_TOOLS`.
    "agent-setup": {"tools"},
}
_AGENT_TOOLS = re.compile(
    r"^tools\s*=\s*(claude|copilot|codex)(,(claude|copilot|codex))*\s*$", re.IGNORECASE
)
# Files that redirect git to another repository, config or object store.
_REDIRECTS = (
    ".git/config.worktree",
    ".git/commondir",
    ".git/gitdir",
    ".git/objects/info/alternates",
)
_CONFIG_HEADER = re.compile(r'^\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\](.*)$')
_CONFIG_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)\s*(?:=.*)?$")


def config_problem(text):
    """Why a git config must not stay in a run, or None when it holds only harmless entries.

    A small parser (no git): `[section]`, `[section "sub"]` and `[section.sub]` headers, entries
    after a header on the same line, comments and backslash continuations. Section and key names
    are case-insensitive. Only `_ALLOWED_CONFIG` passes; every other section or key (includes,
    programs, alternate files, URL rewrites, `extensions`, `submodule`, `core.worktree` and so
    on) is a problem, and so is anything unparseable.
    """
    section = None
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        while line.endswith("\\") and (len(line) - len(line.rstrip("\\"))) % 2 == 1:
            line = line[:-1] + (lines[i].strip() if i < len(lines) else "")
            i += 1
        while line:
            if line[0] in "#;":
                break
            header = _CONFIG_HEADER.match(line)
            if line[0] == "[":
                if not header:
                    return f"unparseable section header {line[:40]!r}"
                name, quoted, line = header.group(1), header.group(2), header.group(3).strip()
                name = name.lower()
                subsection = quoted is not None or "." in name
                if quoted is None and "." in name:
                    name = name.split(".", 1)[0]
                if subsection and name == "agent-setup":
                    return "[agent-setup] takes no subsection"
                if not name or name.startswith(".") or name.endswith("."):
                    return f"unparseable section name {name!r}"
                section = name
                if section not in _ALLOWED_CONFIG:
                    return f"[{section}] sections are not allowed"
                continue
            if section is None:
                return "entry outside any section"
            entry = _CONFIG_KEY.match(line)
            if not entry:
                return f"unparseable entry {line[:40]!r}"
            allowed = _ALLOWED_CONFIG[section]
            if allowed is not None and entry.group(1).lower() not in allowed:
                return f"{section}.{entry.group(1).lower()} is not allowed"
            if section == "agent-setup" and not _AGENT_TOOLS.match(line):
                return "agent-setup.tools must be a comma-separated list of known tool names"
            break
    return None


def check_integrity(paths):
    """What changed since the build, as relative paths, sorted.

    Files under `scripts/`, `.git/hooks/` and `.git/info/` that were changed, added or removed,
    the entries of `origin.git/hooks/`, and `.git/config` and `origin.git/config` when missing,
    unreadable or holding a dangerous entry (`config_problem`); git's own harmless writes such
    as branch tracking, pushed objects and refs are fine. Any of the `_REDIRECTS` files
    (`.git/commondir`, `.git/objects/info/alternates`, ...) also counts. A symlink, FIFO, other
    non-regular file or file over `MAX_READ` among the watched files counts too, and so does a
    watched directory replaced by a symlink or a file; every read goes through `read_regular`,
    so none can block.

    The check is point-in-time: it sees the files as they are now. A process left running
    inside the sandbox could change them afterwards. The launcher's `kill_call` removes only
    each call's own leftovers, not every stray process, and `run_shown` checks again before the
    next command.
    """
    origin = paths.origin_real or None
    now = fingerprint(paths.repo, origin)
    changed = {
        rel for rel in set(now) | set(paths.fingerprints)
        if now.get(rel) != paths.fingerprints.get(rel)
    }
    changed.update(rel for rel, digest in now.items() if digest == UNSAFE)
    changed.update(rel for rel in _REDIRECTS if os.path.lexists(Path(paths.repo) / rel))
    if not _config_ok(Path(paths.repo) / _CONFIG):
        changed.add(_CONFIG)
    if origin and not _config_ok(Path(origin) / "config"):
        changed.add("origin.git/config")
    try:  # nested or folded-name `.claude`, `.mcp.json`, commands, hooks: wherever they appear
        _check_agent_config(paths.repo, allow_agents=True)
        changed.update(_agents_problems(paths.repo, paths.agents_expected))
    except (RepoBuildError, OSError):
        changed.add(".claude (agent config)")
    return sorted(changed)


def _config_ok(path):
    """True when the git config at `path` is a readable regular file with allowed entries only."""
    data = read_regular(path)
    try:
        text = None if data is None else data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return text is not None and not config_problem(text)


def _export_toolkit():
    scripts = str(REPO_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.append(scripts)
    import export_toolkit

    return export_toolkit


def write_atomic(path, text):
    """Write `path` through a fresh private temporary file in its directory, then replace it.

    A name that already exists (a planted `.tmp` link) is never opened or followed."""
    fd, temp = tempfile.mkstemp(dir=Path(path).parent, prefix=Path(path).name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temp, 0o644)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


_EMPTY_HOME = []


def _empty_home():
    """One empty directory for `HOME` and `XDG_CONFIG_HOME`, so no global git files apply."""
    if not _EMPTY_HOME:
        path = tempfile.mkdtemp(prefix="eval-empty-home-")
        atexit.register(shutil.rmtree, path, True)
        _EMPTY_HOME.append(path)
    return _EMPTY_HOME[0]


def _git_env():
    env = launchers.allowlisted_env(os.environ)
    env.update(
        {
            "HOME": _empty_home(),
            "XDG_CONFIG_HOME": _empty_home(),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _git(cwd, *args, binary=False):
    command = [
        "git",
        "-C",
        str(cwd),
        "-c",
        "user.name=Eval Harness",
        "-c",
        "user.email=eval-harness@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=" + os.devnull,
        "-c",
        "core.fsmonitor=false",
        *args,
    ]
    done = subprocess.run(command, capture_output=True, env=_git_env(), check=False)
    if done.returncode:
        message = done.stderr.decode("utf-8", "replace").strip()
        raise RepoBuildError(f"git {' '.join(args[:2])} failed: {message}")
    return done.stdout if binary else done.stdout.decode("utf-8", "replace").strip()


def _working_tree_entries(source):
    """{path: loader} for the files `git ls-files -co --exclude-standard` lists and that exist."""
    listing = _git(source, "ls-files", "-co", "--exclude-standard", "-z", binary=True)
    entries = {}
    for raw in listing.split(b"\0"):
        if not raw:
            continue
        path = raw.decode("utf-8", "surrogateescape")
        full = Path(source) / path
        if not os.path.lexists(full):
            continue  # listed, but gone from the working tree
        entries[path] = lambda full=full: _read_entry(full)
    return entries


def _read_entry(full):
    """(kind, bytes) of a working-tree path: "link", "exec", "file" or None for a directory."""
    if full.is_symlink():
        return "link", os.fsencode(os.readlink(full))
    if not full.is_file():
        return None, b""
    return ("exec" if full.stat().st_mode & EXEC_BIT else "file"), full.read_bytes()


def _commit_entries(export, source, sha):
    modes = {"120000": "link", "100755": "exec", "100644": "file"}
    entries = {}
    try:
        tree = export.read_tree(source, sha)
    except export.ExportError as error:
        raise RepoBuildError(str(error)) from error
    for path, (mode, blob_sha) in tree.items():
        kind = modes.get(mode)
        if kind:
            entries[path] = lambda k=kind, b=blob_sha: (k, export.blob(source, b))
    return entries


def _resolve_ref(source, ref):
    if not isinstance(ref, str) or not _FULL_SHA.match(ref):
        raise RepoBuildError(f"--toolkit-ref must be a full commit SHA, got {ref!r}")
    try:
        resolved = _git(source, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
    except RepoBuildError as error:
        raise RepoBuildError(f"--toolkit-ref {ref} is not a commit of {source}") from error
    if resolved != ref:
        raise RepoBuildError(f"--toolkit-ref {ref} is not a commit SHA")
    return ref


def _manifest(export, entries):
    loader = entries.get(export.DEFAULT_MANIFEST)
    if loader is None:
        raise RepoBuildError(f"{export.DEFAULT_MANIFEST} not found in the toolkit source")
    try:
        text = loader()[1].decode("utf-8")
        return export.parse_manifest(text, export.DEFAULT_MANIFEST)
    except (export.ExportError, UnicodeDecodeError) as error:
        raise RepoBuildError(str(error)) from error


def _write_entry(repo, path, kind, data):
    """Write one file or symlink below `repo` the way `export_toolkit.write_tree` does.

    The parents are made without following symlinks (`safe_parent`), and a file is created with
    `O_CREAT|O_EXCL|O_NOFOLLOW`; an existing file (an overlay replacing an exported one) is
    removed first.
    """
    export = _export_toolkit()
    root = Path(repo)
    try:
        parent = export.safe_parent(root, path)
        dest = parent / path.rsplit("/", 1)[-1]
        if dest.is_symlink() or dest.exists():
            dest.unlink()
        if kind == "link":
            os.symlink(os.fsdecode(data), dest)
            return
        perm = (0o755 if kind == "exec" else 0o644) & ~export.current_umask()
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(dest, flags, perm), "wb") as handle:
            handle.write(data)
            os.fchmod(handle.fileno(), perm)
    except (export.ExportError, OSError) as error:
        raise RepoBuildError(f"cannot write {path!r}: {error}") from error


def _check_contained(repo):
    """Every path below `repo` (outside `.git`) and every symlink resolves inside it."""
    root = Path(os.path.realpath(repo))
    for current, dirs, names in os.walk(repo, followlinks=False):
        if Path(current) == Path(repo) and ".git" in dirs:
            dirs.remove(".git")
        for name in [*dirs, *names]:
            path = Path(current) / name
            if not Path(os.path.realpath(path)).is_relative_to(root):
                raise RepoBuildError(f"{path} resolves outside the run's repository")


def _untracked(source):
    listing = _git(source, "ls-files", "-o", "--exclude-standard", "-z", binary=True)
    return {raw.decode("utf-8", "surrogateescape") for raw in listing.split(b"\0") if raw}


def _validate_symlinks(export, symlinks, chosen, loaded):
    """The checks of `export_toolkit.validate_symlinks`, over the loaded entries."""
    for path in sorted(chosen):
        parts = path.split("/")
        for i in range(1, len(parts)):
            below = "/".join(parts[:i])
            if below in symlinks:
                raise RepoBuildError(f"{path} lies below the declared symlink {below}")
    for path, target in sorted(symlinks.items()):
        found = loaded.get(path)
        if path not in chosen or found is None or found[0] != "link":
            raise RepoBuildError(f"declared symlink {path} is missing or not exported")
        if os.fsdecode(found[1]) != target:
            raise RepoBuildError(f"symlink {path} does not point at the declared {target!r}")
        try:
            resolved = export.resolve_lexically(path, target, symlinks)
        except export.ExportError as error:
            raise RepoBuildError(str(error)) from error
        if resolved not in chosen and not any(p.startswith(resolved + "/") for p in chosen):
            raise RepoBuildError(f"symlink {path} points outside the exported files: {target!r}")
    for path in sorted(chosen):
        if loaded[path][0] == "link" and path not in symlinks:
            raise RepoBuildError(f"undeclared symlink in the toolkit source: {path}")


_SECRET_HIT = re.compile(r"^✖ (.*?):\d+: looks like ")


def _scan_secrets(source, files):
    """Run `scripts/check-secrets.mjs` on `files`: (number of flagged files, warning or None).

    Argv list, no shell, from the source repository (or this repository's copy when the source
    has none). Without node or the script the scan is skipped with a warning.
    """
    node = shutil.which("node")
    script = Path(source) / "scripts" / "check-secrets.mjs"
    if not script.is_file():
        script = REPO_ROOT / "scripts" / "check-secrets.mjs"
    if node is None or not script.is_file():
        return 0, "secret scan skipped: node or scripts/check-secrets.mjs is not available"
    flagged = set()
    for start in range(0, len(files), 100):
        chunk = files[start : start + 100]
        try:
            done = subprocess.run(
                [node, str(script), *chunk],
                cwd=str(source),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
                env=launchers.allowlisted_env(os.environ),
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RepoBuildError(f"the secret scan could not run: {type(error).__name__}") from None
        if done.returncode not in (0, 1):
            raise RepoBuildError(f"the secret scan failed to run (exit {done.returncode})")
        if done.returncode == 1:
            hits = {m.group(1) for m in map(_SECRET_HIT.match, done.stderr.splitlines()) if m}
            flagged |= hits or {chunk[0]}
    return len(flagged), None


# All skills are exported (the engine's agent-setup check needs the files listed in the
# integration manifests); the profiles allow `profiles.EVAL_SKILLS` to be invoked, and the
# frontmatter of those may not widen permissions.
_SKILLS_PATH = (".agents", "skills")
_SKILLS_LINK_TARGET = "../.agents/skills"
_WIDENING_KEYS = re.compile(r"""^["']?(allowed[-_]tools|hooks)["']?\s*:""", re.IGNORECASE)
_NAME_KEY = re.compile(r"""^["']?name["']?\s*:\s*(.*)$""", re.IGNORECASE)


def _fold(export, name):
    """The key `export.check_collisions` uses: how a case- and normalisation-insensitive disk
    (APFS folds `U+017F` to `s`) sees a name."""
    return unicodedata.normalize("NFC", export.drop_ignorable(name)).casefold()


def _refuse_agent_config(what):
    raise RepoBuildError(f"{what} would configure the agent; the run's repository may not hold it")


def _check_agent_config(repo, allow_agents=False):
    """Allowlist what could configure the agent, which runs with `--setting-sources project`.

    Settings merge in hooks (run outside the sandbox) and permission rules, `.mcp.json` adds
    servers, `.claude/commands`, `.claude/agents` and nested `.claude` directories add more.
    Names are compared by their folded key, so a spelling a case-folding disk treats as the same
    name does not slip through. Allowed: no `.claude` at all, or a top-level directory `.claude`
    holding only the `skills` symlink to the exported skills; no `.mcp.json` at the top level.
    With `allow_agents` (a run in progress) the real directory `.claude/agents` may exist too:
    the engine's `--agents` step generates it, and `_agents_problems` checks its files.
    """
    export = _export_toolkit()
    repo = Path(repo)

    def unlistable(error):  # files in it could be anything: fail closed
        raise RepoBuildError(f"{error.filename} cannot be listed")

    for current, dirs, names in os.walk(repo, followlinks=False, onerror=unlistable):
        top = Path(current) == repo
        if top and ".git" in dirs:
            dirs.remove(".git")
        for name in [*dirs, *names]:
            key = _fold(export, name)
            path = Path(current) / name
            if top and key == ".mcp.json":
                _refuse_agent_config(name)
            if key != ".claude":
                continue
            if not top or name != ".claude" or path.is_symlink() or not path.is_dir():
                _refuse_agent_config(path.relative_to(repo).as_posix())
            try:
                inner = sorted(os.listdir(path))
            except OSError:
                raise RepoBuildError(f"{name} cannot be listed") from None
            link = path / "skills"
            agents = path / "agents"
            if (
                not set(inner) <= {"skills", "agents"}
                or ("agents" in inner and not allow_agents)
                or ("agents" in inner and (agents.is_symlink() or not agents.is_dir()))
                or ("skills" in inner and not _is_skills_link(link))
            ):
                _refuse_agent_config(f".claude/{', '.join(inner)}")


def _is_skills_link(link):
    return link.is_symlink() and os.readlink(link) == _SKILLS_LINK_TARGET


def _expected_claude_agents(repo):
    """{file name: bytes} of `.claude/agents/` as the engine's `agent_setup.py sync` writes it.

    Generated by the harness's own `agent_setup` module (not the repository's copy, which the
    agent could change) from the repository's `agent-definitions/`. Called once, at build time,
    before the agent runs: it reads the definitions with plain `read_text`, so it must never run
    on the live tree (a FIFO there would block it). Later changes to the definitions are caught
    by the fingerprint. Empty when the repository has no `agent-definitions/` (nothing is
    generated, so no file may be there); definitions that cannot be read fail the build.
    """
    scripts = str(REPO_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.append(scripts)
    import agent_setup

    if not (Path(repo) / agent_setup.DEFINITIONS_DIR).is_dir():
        return {}
    try:
        expected, problems = agent_setup.expected_agent_files(Path(repo))
    except Exception as error:
        raise RepoBuildError(
            f"agent-definitions cannot be read ({type(error).__name__})"
        ) from None
    if problems:
        raise RepoBuildError("agent-definitions are not usable: " + "; ".join(problems))
    return {
        path.name: content.encode("utf-8")
        for path, content in expected.items()
        if path.parent == agent_setup.CLAUDE_AGENTS_DIR
    }


def _agents_problems(repo, expected):
    """Paths in `.claude/agents/` that differ from `expected`, the build-time map of what
    `agent_setup.py sync` generates (`RunPaths.agents_expected`)."""
    agents = Path(repo) / ".claude" / "agents"
    if not os.path.lexists(agents):
        return []
    if agents.is_symlink() or not agents.is_dir():
        return [".claude/agents"]
    return [
        f".claude/agents/{name}"
        for name in sorted(os.listdir(agents))
        if name not in expected or read_regular(agents / name) != expected[name]
    ]


def _check_skill_frontmatter(repo):
    """Refuse `allowed-tools` or `hooks` in the frontmatter of the skills the scenarios use, so a
    later toolkit change cannot widen the agent's permissions without anyone noticing.

    Every skill in `.agents/skills` is read: one counts as a scenario skill when its `name:` or
    its directory folds to one of `profiles.EVAL_SKILLS`. A byte order mark, a quoted key and
    CRLF line ends do not hide a key, an unterminated frontmatter block is refused, and so are
    two skills with the same folded name (either could be the one the agent invokes).
    """
    export = _export_toolkit()
    wanted = {_fold(export, name) for name in profiles.EVAL_SKILLS}
    root = Path(repo, *_SKILLS_PATH)
    if not root.is_dir():
        return
    seen = {}
    for directory in sorted(os.listdir(root)):
        data = read_regular(root / directory / "SKILL.md")
        unreadable = data is None and os.path.lexists(root / directory / "SKILL.md")
        text = "" if data is None else data.decode("utf-8", "replace").lstrip(chr(0xFEFF))
        lines = text.replace("\r\n", "\n").split("\n")
        front, terminated = [], False
        if lines[0].strip() == "---":
            for line in lines[1:]:
                if line.strip() == "---":
                    terminated = True
                    break
                front.append(line)
        else:
            terminated = True  # no frontmatter at all: nothing to widen
        name = directory
        for line in front:
            found = _NAME_KEY.match(line)
            if found and found.group(1).strip().strip("\"'"):
                name = found.group(1).strip().strip("\"'")
        names = {_fold(export, name), _fold(export, directory)}
        key = _fold(export, name)
        if key in seen:
            raise RepoBuildError(f"skills {seen[key]!r} and {directory!r} share the name {name!r}")
        seen[key] = directory
        if not names & wanted:
            continue
        if unreadable:
            raise RepoBuildError(f"skill {directory}: SKILL.md is not a readable regular file")
        if not terminated:
            raise RepoBuildError(f"skill {directory} has an unterminated frontmatter block")
        for line in front:
            if _WIDENING_KEYS.match(line):
                raise RepoBuildError(
                    f"skill {directory} sets {line.split(':')[0].strip()} in its frontmatter; the "
                    "evaluation does not allow skills to widen the agent's permissions"
                )


def _copy_toolkit(source, toolkit_ref, repo, include_untracked):
    """Write the exported files into `repo`; return `(their paths, warnings)`."""
    export = _export_toolkit()
    if toolkit_ref is None:
        entries = _working_tree_entries(source)
    else:
        entries = _commit_entries(export, source, toolkit_ref)
    include, exclude, symlinks = _manifest(export, entries)
    included, _excluded, _unclassified = export.classify(entries, include, exclude)
    for path in included:
        if export.unsafe_path(path):
            raise RepoBuildError(f"unsafe path in the toolkit source: {path!r}")
    try:
        export.check_collisions(included)
    except export.ExportError as error:
        raise RepoBuildError(str(error)) from error
    warnings = ()
    untracked = sorted(_untracked(source) & set(included)) if toolkit_ref is None else []
    if untracked and not include_untracked:
        raise RepoBuildError(
            f"{len(untracked)} untracked file(s) would be exported; commit or ignore them, "
            "or pass include_untracked=True"
        )
    if untracked:
        flagged, warning = _scan_secrets(source, untracked)
        if flagged:
            raise RepoBuildError(
                f"the secret scan flagged {flagged} of {len(untracked)} untracked file(s) "
                "that would be exported"
            )
        warnings = (warning,) if warning else ()
    try:
        loaded = {path: entries[path]() for path in included}
    except (export.ExportError, OSError) as error:
        raise RepoBuildError(f"cannot read the toolkit source: {error}") from error
    loaded = {path: item for path, item in loaded.items() if item[0] is not None}
    chosen = set(loaded)
    _validate_symlinks(export, symlinks, chosen, loaded)
    for path in sorted(chosen):
        kind, data = loaded[path]
        _write_entry(repo, path, kind, data)
    return sorted(chosen), warnings


def _init_repo(repo):
    """`git init -b main`; a pre-existing `.git` (reusable hooks and config) is refused."""
    if os.path.lexists(Path(repo) / ".git"):
        raise RepoBuildError(f"{repo} already has a .git before git init")
    _git(repo, "init", "-q", "-b", "main")


def _overlay(fixture_dir, repo, existing):
    """Copy the fixture's `files/` over the repository without following symlinks.

    `existing` are the exported paths already written; an overlay file may replace one of them
    but may not collide with it (case, Unicode normalisation, file versus directory).
    """
    root = Path(fixture_dir) / "files"
    if not root.is_dir():
        return
    export = _export_toolkit()
    files = []
    for rel, path in sorted(_walk(root)):
        if export.unsafe_path(rel):
            raise RepoBuildError(f"fixture file {rel!r} has an unsafe path (.git, .., control)")
        parts = rel.split("/")
        if [_fold(export, p) for p in parts[:2]] == list(_SKILLS_PATH):
            raise RepoBuildError(
                f"fixture file {rel} is below .agents/skills; fixtures bring no skills"
            )
        if path.is_symlink():
            raise RepoBuildError(f"fixture file {rel} is a symlink; fixtures may not hold any")
        if path.is_file():
            files.append((rel, path))
    try:
        export.check_collisions(sorted({*existing, *(rel for rel, _ in files)}))
    except export.ExportError as error:
        raise RepoBuildError(str(error)) from error
    for rel, path in files:
        kind = "exec" if path.stat().st_mode & EXEC_BIT else "file"
        _write_entry(repo, rel, kind, path.read_bytes())


def _build_origin(repo, origin, branches):
    """A bare `origin.git` with `main` and the fixture's branches; `origin` points at GitHub."""
    _git(repo.parent, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(repo, "push", "-q", str(origin), "main:refs/heads/main")
    scratch = origin.parent / "origin-scratch"
    try:
        _git(origin.parent, "clone", "-q", "--no-hardlinks", str(origin), str(scratch))
        for branch in branches:
            if branch.name != "main":
                _git(scratch, "checkout", "-q", "-B", branch.name, "origin/main")
            else:
                _git(scratch, "checkout", "-q", "main")
            for message in branch.commits:
                _git(scratch, "commit", "-q", "--allow-empty", "-m", message)
        _git(scratch, "push", "-q", "origin", "--all")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    _git(repo, "remote", "add", "origin", ORIGIN_URL)
    _git(repo, "fetch", "-q", str(origin), "+refs/heads/*:refs/remotes/origin/*")
    _git(repo, "branch", "-q", "--set-upstream-to=origin/main", "main")


def build_run(
    run_root,
    fixture_dir,
    source_repo=None,
    toolkit_ref=None,
    python=None,
    real_git=None,
    include_untracked=True,
):
    """Build `<run_root>` (see the module docstring) and return its `RunPaths`.

    The repository holds the exported toolkit (the working tree of `source_repo`, or the commit
    `toolkit_ref`, a full SHA, read from git objects), the fixture's `files/` laid over it, one
    commit on `main`, and `origin` set to the GitHub URL backed by `<run_root>/origin.git`.
    Untracked files that would be exported count (research D2) and are run through
    `scripts/check-secrets.mjs` (a hit refuses the build; without node the scan is skipped and
    `RunPaths.warnings` says so); `include_untracked=False` refuses them instead. The `gh` shim
    and `git` wrapper run from copies in `<run_root>/bin/lib/`; `shown-home` and `shown-tmp`
    hold a fresh HOME and TMPDIR per
    `run_shown` call.
    """
    source = Path(source_repo or REPO_ROOT)
    fixture_dir = Path(fixture_dir)
    if toolkit_ref is not None:
        toolkit_ref = _resolve_ref(source, toolkit_ref)
    branches = load_git_spec(fixture_dir)
    load_gh_table(fixture_dir)  # validates gh.toml before anything is built
    gh_source = fixture_dir / "gh.toml"
    root = Path(run_root)
    root.parent.mkdir(parents=True, exist_ok=True)
    try:
        root.mkdir()
    except FileExistsError:
        raise RepoBuildError(f"run directory {root} already exists") from None
    root = root.resolve()  # macOS: /private/var/folders/..., what the sandbox rules need
    try:
        repo, sidecar, bin_dir = root / "repo", root / "harness", root / "bin"
        for directory in (repo, sidecar, bin_dir):
            directory.mkdir()
        exported, warnings = _copy_toolkit(source, toolkit_ref, repo, include_untracked)
        _init_repo(repo)  # before the overlay: a fixture can never bring its own `.git`
        _overlay(fixture_dir, repo, exported)
        _check_contained(repo)
        _check_agent_config(repo)
        _check_skill_frontmatter(repo)
        _git(repo, "add", "-A", "--force")
        _git(repo, "commit", "-q", "-m", "Toolkit under test")
        origin = root / "origin.git"
        _build_origin(repo, origin, branches)
        table = sidecar / "gh.toml"
        table.write_bytes(gh_source.read_bytes() if gh_source.is_file() else b"")
        calls = sidecar / "gh-calls.jsonl"
        calls.write_text("")
        python = python or sys.executable
        lib = bin_dir / "lib"
        lib.mkdir()
        shim_copy = lib / "gh_shim.py"
        wrapper_copy = lib / "git_wrapper.py"
        shutil.copyfile(Path(gh_shim.__file__).resolve(), shim_copy)
        shutil.copyfile(Path(git_wrapper.__file__).resolve(), wrapper_copy)
        gh_shim.write_wrapper(bin_dir, python, shim_path=str(shim_copy))
        git_path = real_git or git_wrapper.find_real_git()
        git_wrapper.install(bin_dir, git_path, python, module_path=str(wrapper_copy))
        shown_home, shown_tmp = root / "shown-home", root / "shown-tmp"
        shown_home.mkdir()
        shown_tmp.mkdir()
        call_stat = os.lstat(calls)
        git_paths = _git_binaries(git_path)
        git_exec = _git_exec_path(git_path)
        agents_expected = _expected_claude_agents(repo)  # once: the live tree is never read again
        fingerprints = fingerprint(repo, origin)  # last: nothing below may change these files
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return RunPaths(
        root,
        repo,
        sidecar,
        bin_dir,
        origin,
        table,
        calls,
        toolkit_ref,
        python,
        shown_home,
        shown_tmp,
        fingerprints,
        agents_expected,
        repo_real=os.path.realpath(repo),
        bin_real=os.path.realpath(bin_dir),
        gh_calls_real=os.path.realpath(calls),
        gh_calls_id=(call_stat.st_dev, call_stat.st_ino),
        git_real=os.path.realpath(git_path),
        git_paths=git_paths,
        warnings=warnings,
        gh_table_real=os.path.realpath(table),
        gitconfig_real=os.path.join(os.path.realpath(sidecar), "harness.gitconfig"),
        gh_config_real=os.path.join(os.path.realpath(sidecar), "gh-config"),
        origin_real=os.path.realpath(origin),
        git_exec_real=git_exec,
    )

"""Builds the temporary repository for a run and plays the scripted turns.

This part loads, validates and hashes scenarios and fixture states (data-model.md "Scenario" and
"Fixture state"). Python 3.11+, standard library only.

`git.toml` shape (optional): a list of `[[branch]]` entries, each with a unique `name` (letters, digits, `.`, `_`, `-`, `/`;
no leading `-`, no `..`) and an
optional `commits` list of commit messages, created in that order in the local `origin`.
"""

import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

import profiles
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
            errors.append(
                f"{where}: expect must be one of {', '.join(EXPECTS)} or {{ pattern = \"<regex>\" }}"
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


def _walk(root):
    """Yield (relative POSIX path, absolute path) of every entry below root, not following links."""
    for current, dirs, names in os.walk(root, followlinks=False):
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
            files[rel] = [_file_digest(path), bool(path.stat().st_mode & 0o111)]
    profile = {
        "tools": profiles.tools(scenario.tools),
        "settings": profiles.settings(scenario.tools, "<RUN>", "<HOME>"),
    }
    document = {"scenario": _file_digest(scenario.path), "fixture": files, "profile": profile}
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()

#!/usr/bin/env python3
"""The gh stand-in installed as gh on PATH during an evaluation run (research D3).

Python 3.11+, standard library only. Every argument is treated as data: it is compared
with glob patterns and logged, never executed or evaluated.

Environment variables (set by the harness):
  EVAL_GH_TABLE  path to the fixture's `gh.toml`, a list of `[[call]]` entries:
                 `argv` (one glob pattern per argument; a final `**` matches any remaining
                 arguments, including none), `stdout`, `stderr`, `exit` (default 0) and
                 `writes` (bool; when present it overrides the heuristic, true or false)
                 and an optional `query`, a regex searched in the whitespace-collapsed GraphQL
                 query text of a `gh api graphql` call (from `--input <json body>` or a
                 `query=` field), so
                 calls with the same argv can be told apart. The first entry that matches
                 wins. Patterns are fnmatch globs: write `[[]` for a literal `[`, as `[`
                 opens a character class.
  EVAL_GH_CALLS  path to the run's `gh-calls.jsonl`; every call appends one line
                 `{"argv": [...], "matched": <entry index or null>, "writes": <bool>,
                 "query": <the whole query, whitespace collapsed, or null>}`. Both variables
                 are required: a missing one exits 1 (fail closed).
                 Security: the shim runs inside the agent's sandbox, so anything running
                 there can write or forge this log. Scoring treats it as evidence, not proof.

An unmatched call prints `gh shim: no fixture for <argv>` on stderr, exits 1 and is logged
with `matched` null; `fixture_stale(calls_path)` then tells the harness the run is
unscored with reason `fixture_stale`.

Usage:
  gh_shim.py <gh arguments...>          answer one gh call from the table
  gh_shim.py record-calls <calls.jsonl> print the calls of a run as draft `[[call]]` entries
                                        so a stale table can be updated
`write_wrapper(bin_dir, python)` writes the `gh` launcher into a bin directory; it starts the
shim with the absolute path of a Python 3.11+ interpreter.
"""

import errno
import fnmatch
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

TABLE_ENV = "EVAL_GH_TABLE"
CALLS_ENV = "EVAL_GH_CALLS"
MAX_LOG_BYTES = 8 * 1024 * 1024

# Second arguments that only read. Any other verb (create, edit, close, merge, set, ...) is a
# write, as is any command not listed here: the rule set of scripts/fake_github.py is_mutating,
# widened to all commands, where unknown means write.
READ_VERBS = {"view", "list", "status", "get", "field-list", "item-list", "diff", "checks"}
# Commands that read whatever follows them.
READ_COMMANDS = {"--version", "version", "search", "browse", "auth"}
WRITE_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
# `gh api` flags that take the next argument as their value.
API_VALUE_FLAGS = {
    "-H", "--header", "-q", "--jq", "-t", "--template", "--cache", "-X", "--method",
    "-f", "-F", "--field", "--raw-field", "--input", "-p", "--preview", "--hostname",
}
FIELD_FLAGS = ("-f", "-F", "--field", "--raw-field")


def _is_body_flag(arg):
    if arg in ("--field", "--raw-field", "--input"):
        return True
    if arg.startswith(("--field=", "--raw-field=", "--input=")):
        return True
    return arg in ("-f", "-F") or (arg[:2] in ("-f", "-F") and not arg.startswith("--"))


def _api_endpoint(args):
    """The first argument of `gh api ...` that is neither a flag nor a flag's value."""
    skip = False
    for arg in args[1:]:
        if skip:
            skip = False
        elif arg in API_VALUE_FLAGS:
            skip = True
        elif not arg.startswith("-"):
            return arg
    return None


def _field_query(args):
    """The value of a `query=` field given with -f, -F, --field or --raw-field, or None."""
    for index, arg in enumerate(args):
        value = None
        if arg in FIELD_FLAGS:
            value = args[index + 1] if index + 1 < len(args) else None
        elif arg.startswith(("--field=", "--raw-field=")):
            value = arg.split("=", 1)[1]
        elif arg[:2] in ("-f", "-F") and not arg.startswith("--"):
            value = arg[2:]
        if value is not None and value.startswith("query="):
            return value[len("query="):]
    return None


def _input_path(args):
    for index, arg in enumerate(args):
        if arg == "--input":
            return args[index + 1] if index + 1 < len(args) else "-"
        if arg.startswith("--input="):
            return arg[len("--input="):]
    return None


def _input_query(path):
    """The `query` of a JSON request body file, or None if it cannot be read (or is stdin)."""
    if path == "-":
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            query = json.load(handle).get("query")
    except (OSError, ValueError, AttributeError):
        return None
    return query if isinstance(query, str) else None


def _is_graphql(args):
    return bool(args) and args[0] == "api" and _api_endpoint(args) == "graphql"


def query_text(argv):
    """The GraphQL query text of a `gh api graphql` call (field value or --input body)."""
    args = list(argv)
    if not _is_graphql(args):
        return None
    query = _field_query(args)
    if query is None and _input_path(args) is not None:
        query = _input_query(_input_path(args))
    return query


def _collapse(query):
    """The query with every run of whitespace turned into one space, stripped."""
    return " ".join(query.split()) if query is not None else None


def _is_mutation(query):
    lines = [
        line.strip() for line in query.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return bool(lines) and lines[0].lower().startswith("mutation")


def is_write(argv):
    """True when a gh call would change GitHub state."""
    args = list(argv)
    if not args or args[0] in READ_COMMANDS or args[:2] == ["run", "watch"]:
        return False
    if _is_graphql(args):
        # GraphQL is always a POST; only the query text tells a read from a write.
        path = _input_path(args)
        query = _field_query(args)
        if query is None and path is not None:
            query = _input_query(path)
            return query is None or _is_mutation(query)
        return query is not None and _is_mutation(query)
    if args[0] == "api":
        method = None
        for index, arg in enumerate(args):
            if arg in ("-X", "--method") and index + 1 < len(args):
                method = args[index + 1]
            elif arg.startswith("--method="):
                method = arg[len("--method="):]
            elif arg.startswith("-X") and len(arg) > 2:
                method = arg[2:]
        if method is not None:
            return method.upper() in WRITE_METHODS
        return any(_is_body_flag(arg) for arg in args[1:])
    return (args[1] if len(args) > 1 else "") not in READ_VERBS


def _matches(patterns, argv):
    if patterns and patterns[-1] == "**":
        head = patterns[:-1]
        return len(argv) >= len(head) and all(
            fnmatch.fnmatchcase(arg, pattern) for pattern, arg in zip(head, argv)
        )
    return len(patterns) == len(argv) and all(
        fnmatch.fnmatchcase(arg, pattern) for pattern, arg in zip(patterns, argv)
    )


def find_entry(entries, argv, query=None):
    """Index of the first entry whose `argv` globs match and, when it has a `query` regex,
    whose regex is found in the call's query text; None when no entry matches."""
    for index, entry in enumerate(entries):
        if not _matches([str(p) for p in entry.get("argv", [])], argv):
            continue
        pattern = entry.get("query")
        if pattern is None:
            return index
        if query is not None and re.search(pattern, _collapse(query)):
            return index
    return None


def _log(path, argv, matched, writes, query):
    line = json.dumps(
        {"argv": argv, "matched": matched, "writes": writes, "query": _collapse(query)}
    )
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _read_calls(calls_path, expect_inode=None):
    """The logged calls. Raises FileNotFoundError when there is no log, and ValueError when
    the log is a symlink or not a regular file, is larger than MAX_LOG_BYTES, or is not the
    file `expect_inode` (st_dev, st_ino) names. The error never quotes the content."""
    try:
        fd = os.open(calls_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError("calls log is a symlink") from None
        raise ValueError(f"calls log cannot be opened: {error.strerror}") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("calls log is not a regular file")
        if expect_inode is not None and (info.st_dev, info.st_ino) != tuple(expect_inode):
            raise ValueError("calls log was replaced (device or inode differs)")
        data = handle.read(MAX_LOG_BYTES + 1)
    if len(data) > MAX_LOG_BYTES:
        raise ValueError(f"calls log is larger than {MAX_LOG_BYTES} bytes")
    lines = data.decode("utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def fixture_stale(calls_path, expect_inode=None):
    """True when the run made a call that no table entry matched, or left no call log."""
    try:
        calls = _read_calls(calls_path, expect_inode)
    except FileNotFoundError:
        return True
    return any(call["matched"] is None for call in calls)


def _toml_string(value):
    return json.dumps(value, ensure_ascii=False)


def _mask_input(argv):
    """Replace the value of --input (a random temporary file) with a glob."""
    masked, skip = [], False
    for arg in argv:
        if skip:
            masked.append("*")
            skip = False
        elif arg == "--input":
            masked.append(arg)
            skip = True
        elif arg.startswith("--input="):
            masked.append("--input=*")
        else:
            masked.append(arg)
    return masked


def record_calls(calls_path, expect_inode=None):
    """The calls of a run as draft `[[call]]` TOML entries (stdout is left for the author)."""
    blocks = []
    try:
        calls = _read_calls(calls_path, expect_inode)
    except FileNotFoundError:
        calls = []
    for call in calls:
        argv = _mask_input(call["argv"])
        lines = ["[[call]]", "argv = [" + ", ".join(_toml_string(a) for a in argv) + "]"]
        if call.get("query"):
            lines.append("query = " + _toml_string("^" + re.escape(call["query"]) + "$"))
        lines.append('stdout = ""')
        if call["writes"]:
            lines.append("writes = true")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _interpreter_ok(python):
    probe = "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
    try:
        result = subprocess.run([python, "-c", probe], capture_output=True, timeout=30)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def write_wrapper(bin_dir, python, shim_path=None):
    """Write an executable `gh` into `bin_dir` that runs the shim with `python`.

    `shim_path` (absolute, an existing file) names a copy of this script to run instead of
    this file, for a sandbox that cannot read the checkout. The script needs only the stdlib.
    """
    if not os.path.isabs(python):
        raise ValueError(f"interpreter must be an absolute path: {python}")
    if not _interpreter_ok(python):
        raise ValueError(f"interpreter is missing or older than Python 3.11: {python}")
    bin_dir = Path(bin_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "gh"
    if shim_path is None:
        shim = Path(__file__).resolve()
    else:
        shim = Path(shim_path)
        if not shim.is_absolute() or not shim.is_file():
            raise ValueError(f"shim_path must be an absolute path to an existing file: {shim_path}")
    command = f"exec {shlex.quote(python)} -I {shlex.quote(str(shim))}"
    wrapper.write_text(f'#!/bin/sh\n{command} "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def main(argv):
    if argv[:1] == ["record-calls"]:
        if len(argv) != 2:
            print("usage: gh_shim.py record-calls <calls.jsonl>", file=sys.stderr)
            return 2
        sys.stdout.write(record_calls(argv[1]))
        return 0
    table_path = os.environ.get(TABLE_ENV)
    calls_path = os.environ.get(CALLS_ENV)
    for name, value in ((TABLE_ENV, table_path), (CALLS_ENV, calls_path)):
        if not value:
            print(f"gh shim: {name} is not set", file=sys.stderr)
            return 1
    query = query_text(argv)
    try:
        with open(table_path, "rb") as handle:
            entries = tomllib.load(handle).get("call", [])
    except (OSError, tomllib.TOMLDecodeError) as error:
        _log(calls_path, argv, None, is_write(argv), query)
        print(f"gh shim: cannot read table: {error}", file=sys.stderr)
        return 1
    try:
        index = find_entry(entries, argv, query)
    except re.error as error:
        print(f"gh shim: invalid query pattern in the table: {error}", file=sys.stderr)
        index = None
    entry = entries[index] if index is not None else {}
    writes = entry["writes"] if "writes" in entry else is_write(argv)
    _log(calls_path, argv, index, bool(writes), query)
    if index is None:
        print(f"gh shim: no fixture for {argv}", file=sys.stderr)
        return 1
    sys.stdout.write(entry.get("stdout", ""))
    sys.stderr.write(entry.get("stderr", ""))
    return int(entry.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

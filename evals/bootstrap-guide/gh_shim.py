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
                 "query": <the whole query, whitespace collapsed, or null>, "prev": <hex>}`.
                 Both variables are required: a missing one exits 1 (fail closed).
                 Security: the shim runs inside the agent's sandbox, so anything running
                 there can write or forge this log. It is made tamper-evident (research D19):
                 the harness writes a first line `{"header": <random hex>}` (`write_header`),
                 and `prev` is the SHA-256 (hex) of the previous line's bytes, without its
                 newline; the first call's `prev` hashes the header line. `read_calls` checks
                 the header and the chain and rejects anything else. The harness keeps the
                 header value and writes it nowhere besides the log, and no allowed command
                 prints the log, so the agent has no way to write a valid next line. Cutting
                 calls off the end of the log leaves a valid chain: the caller checks that
                 every later read still starts with the bytes it verified before (`turns.py`).
                 Processes that call gh at once take an exclusive lock around reading the
                 last line and appending, so the chain holds.

An unmatched call prints `gh shim: no fixture for <argv>` on stderr, exits 1 and is logged
with `matched` null; the harness sees that in the records of its single read of the log and
marks the run unscored with reason `fixture_stale`.

Usage:
  gh_shim.py <gh arguments...>          answer one gh call from the table
  gh_shim.py record-calls <calls.jsonl> print the calls of a run as draft `[[call]]` entries
                                        so a stale table can be updated. It takes the run
                                        directory's `gh-calls.jsonl` (records only, no header,
                                        no `prev`) or the sidecar log (header and chain
                                        verified, with any header)
`write_wrapper(bin_dir, python)` writes the `gh` launcher into a bin directory; it starts the
shim with the absolute path of a Python 3.11+ interpreter.
"""

import errno
import fcntl
import fnmatch
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path

TABLE_ENV = "EVAL_GH_TABLE"
CALLS_ENV = "EVAL_GH_CALLS"
MAX_LOG_BYTES = 8 * 1024 * 1024
LOCK_WAIT_SECONDS = 2.0  # the longest a reader waits for a shim that is appending
LOCK_RETRY_SECONDS = 0.01

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


def _line_digest(line):
    """SHA-256 (hex) of one log line's bytes, without its newline."""
    return hashlib.sha256(line).hexdigest()


def write_header(path):
    """Create the call log with its header line and return the random header value.

    Fails when the file exists. The harness keeps the value (`RunPaths`) and writes it nowhere
    besides the log."""
    header = secrets.token_hex(16)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(json.dumps({"header": header}).encode("utf-8") + b"\n")
    return header


TAIL_WINDOW = 64 * 1024


def _last_line(handle):
    """The last line of the log opened in `handle` (without its newline; b"" for an empty file),
    reading only the end of the file: a window that doubles until it holds a whole line."""
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    window = TAIL_WINDOW
    while True:
        start = max(0, size - window)
        handle.seek(start)
        data = handle.read(size - start)
        body = data[:-1] if data.endswith(b"\n") else data
        cut = body.rfind(b"\n")
        if cut >= 0:
            return body[cut + 1 :]
        if start == 0:
            return body
        window *= 2


def _log(path, argv, matched, writes, query):
    # One writer at a time: the lock covers reading the last line and appending the next, so
    # processes that call gh at once keep the chain. An oversize log is still appended to;
    # the readers flag it.
    with open(path, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        prev = _line_digest(_last_line(handle))
        record = {
            "argv": argv, "matched": matched, "writes": writes, "query": _collapse(query),
            "prev": prev,
        }
        handle.write(json.dumps(record).encode("utf-8") + b"\n")
        handle.flush()


_RECORD_KEYS = {"argv", "matched", "writes", "query", "prev"}


def _fields_ok(record):
    """True when the fields of a call record have the types the shim writes."""
    matched = record["matched"]
    return (
        isinstance(record["argv"], list)
        and all(isinstance(arg, str) for arg in record["argv"])
        and (record["query"] is None or isinstance(record["query"], str))
        and isinstance(record["writes"], bool)
        and (matched is None or (isinstance(matched, int) and not isinstance(matched, bool)))
    )


def _verify(data, expect_header):
    """The call records of the log bytes `data`, or ValueError (never quoting the content)."""
    if not data.endswith(b"\n"):
        raise ValueError("calls log is truncated or has no header")
    lines = data[:-1].split(b"\n")
    try:
        first = json.loads(lines[0])
    except (ValueError, RecursionError):
        first = None
    header = first.get("header") if isinstance(first, dict) and len(first) == 1 else None
    if not isinstance(header, str) or not header:
        raise ValueError("calls log has no header line")
    if expect_header is not None and not hmac.compare_digest(
        header.encode("utf-8"), str(expect_header).encode("utf-8")
    ):
        raise ValueError("calls log header changed")
    records, prev = [], _line_digest(lines[0])
    for number, line in enumerate(lines[1:], start=2):
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):  # deep nesting is not a record either
            raise ValueError(f"calls log line {number} is not a shim record") from None
        if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
            raise ValueError(f"calls log line {number} is not a shim record")
        if record["prev"] != prev:
            raise ValueError(f"calls log chain is broken at line {number}")
        if not _fields_ok(record):
            raise ValueError(f"calls log line {number} is not a shim record")
        prev = _line_digest(line)
        records.append({k: v for k, v in record.items() if k != "prev"})
    return records


def _lock_shared(fd):
    """Take a shared lock on `fd` (the shim holds an exclusive one while it appends), retrying
    for at most LOCK_WAIT_SECONDS; never waits indefinitely. ValueError when it cannot."""
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return
        except OSError as error:
            if error.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise ValueError(f"calls log cannot be locked: {error.strerror}") from None
        if time.monotonic() >= deadline:
            raise ValueError("calls log lock could not be taken in time")
        time.sleep(LOCK_RETRY_SECONDS)


def _read_log_bytes(calls_path, expect_inode=None):
    """The bytes of the call log, read safely (see `read_calls` for what raises)."""
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
        _lock_shared(handle.fileno())  # released when the handle closes
        data = handle.read(MAX_LOG_BYTES + 1)
    if len(data) > MAX_LOG_BYTES:
        raise ValueError(f"calls log is larger than {MAX_LOG_BYTES} bytes")
    return data


def read_verified(calls_path, expect_inode=None, expect_header=None):
    """`(records, data)`: the verified call records and the log bytes they were read from, so a
    caller can require a later read to start with the same bytes. Raises like `read_calls`."""
    data = _read_log_bytes(calls_path, expect_inode)
    return _verify(data, expect_header), data


def read_calls(calls_path, expect_inode=None, expect_header=None):
    """The logged calls, verified. Raises FileNotFoundError when there is no log, and ValueError
    when the log is a symlink or not a regular file, is larger than MAX_LOG_BYTES, is not the
    file `expect_inode` (st_dev, st_ino) names, has no header line or one other than
    `expect_header` (with None, any header will do), has a line that is not a shim record or a
    broken `prev` chain, or ends in a partial line. Returns only the call records, without
    `prev`. The error never quotes the content."""
    return read_verified(calls_path, expect_inode, expect_header)[0]


def _parse_records_copy(data):
    """The records of a records-only copy (the run directory's `gh-calls.jsonl`): one record per
    line, no header and no `prev`. ValueError for anything else."""
    if data and not data.endswith(b"\n"):
        raise ValueError("calls copy ends in a partial line")
    records = []
    for number, line in enumerate(data.split(b"\n")[:-1], start=1):
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            raise ValueError(f"calls copy line {number} is not a call record") from None
        if (
            not isinstance(record, dict)
            or set(record) != _RECORD_KEYS - {"prev"}
            or not _fields_ok(record)
        ):
            raise ValueError(f"calls copy line {number} is not a call record")
        records.append(record)
    return records


def read_calls_or_copy(calls_path):
    """The calls of the sidecar log (header and chain verified, any header) or of a records-only
    copy, told apart by the first line having a `header` key."""
    data = _read_log_bytes(calls_path)
    try:
        first = json.loads(data.split(b"\n", 1)[0] or b"null")
    except (ValueError, RecursionError):
        first = None
    if isinstance(first, dict) and "header" in first:
        return _verify(data, None)
    return _parse_records_copy(data)


_SURROGATE = re.compile("[\ud800-\udfff]")


def _toml_string(value):
    """`value` as a TOML basic string. A lone surrogate (how a non-UTF-8 byte of an argument can
    arrive) is neither valid TOML nor encodable as UTF-8, so it becomes visible `\\uXXXX` text."""
    value = _SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", value)
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


def draft_entries(calls):
    """The call records `calls` as draft `[[call]]` TOML entries (stdout is left for the author)."""
    blocks = []
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
        # The command line has no header value: a sidecar log is checked for its chain and a
        # header; a records-only copy is checked for its shape.
        try:
            text = draft_entries(read_calls_or_copy(argv[1]))
        except (OSError, ValueError) as error:
            print(f"gh_shim.py: calls log is not trustworthy: {error}", file=sys.stderr)
            return 1
        sys.stdout.write(text)
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

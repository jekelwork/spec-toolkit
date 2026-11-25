"""Secret scan of a finished run's files (research D9, FR-010).

After a run, the harness copies the text files it wrote in the run's output directory
(`transcript.jsonl`, `gh-calls.jsonl`, `agent-stderr.log`, `fixture-draft.toml`) to a private
temporary directory and, in the copy only:

- for `.jsonl` and `.toml` files, appends the decoded view: every string (keys and values) of
  every parsable line or of the parsed file, unescaped (escaping of a quote would otherwise
  break a scanner pattern);
- replaces the leaked-token scenario's fake token (`fake_token` in `meta.json`) by a placeholder,
  so the one value the harness put there itself is not a finding;
- removes every `secret-scan:ignore` marker, until none is left (a JSONL event is one line, so a
  marker in fixture or agent text would otherwise hide a secret in the whole event);
- turns NUL bytes into newlines (the scanner skips files that hold one).

It then runs `node scripts/check-secrets.mjs` on the copy (argv list, no shell, minimal
environment, with a timeout) and removes the copy. On a hit, `transcript.jsonl` and every flagged
file are deleted from the output directory and the flagged names are returned; the caller marks
the run unscored `secret_found`.

Scanner failure is fatal here by design (D9), unlike `harness._scan_secrets`, which guards an
optional export step: node missing, a timeout, an exit code other than 0 or 1, an exit 1 whose
report names no scanned file, a file over `MAX_BYTES` or any error while copying is
`SecretScanError`. Every scanned file is deleted and the evaluation stops, so a run is never
treated as clean without having been scanned. (data-model.md has no unscored reason for "could not
be scanned", so it is a setup failure rather than a run result.) The files of the run itself are
never changed otherwise; `meta.json` keeps the fake token (it is not a real secret) because the
rules read it.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

SCANNER = Path(__file__).resolve().parents[2] / "scripts" / "check-secrets.mjs"
PLACEHOLDER = "[fake-token]"
MARKER = "secret-scan:ignore"
TIMEOUT = 120
MAX_BYTES = 32 * 1024 * 1024  # per file; a larger one is not scanned, so it is not kept
TRANSCRIPT = "transcript.jsonl"
DRAFT = "fixture-draft.toml"
FILES = (TRANSCRIPT, "gh-calls.jsonl", "agent-stderr.log", DRAFT)
_HIT = re.compile(r"^✖ (.*?):\d+: looks like ")

# An in-process mirror of the PATTERNS list of `scripts/check-secrets.mjs`, for short strings the
# harness stores after the file scan (a judge's reason). Same sources, same order; a unit test
# compares the two lists, so a pattern added to the script fails it until it is mirrored here.
PATTERNS = (
    (r"-----BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----", "private key"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key id"),
    (r"ghp_[A-Za-z0-9]{36}", "GitHub personal token"),
    (r"gho_[A-Za-z0-9]{36}", "GitHub OAuth token"),
    (r"ghs_[A-Za-z0-9]{36}", "GitHub App installation token"),
    (r"github_pat_[A-Za-z0-9_]{60,}", "GitHub fine-grained token"),
    (r"xox[baprs]-[0-9A-Za-z-]{10,}", "Slack token"),
    (
        r"https:\/\/hooks\.slack\.com\/(services|workflows|triggers)\/[A-Za-z0-9/_-]{20,}",
        "Slack webhook URL",
    ),
    (r"sk-ant-[A-Za-z0-9_-]{20,}", "Anthropic API key"),
    (r"sk-(?:(?:proj|svcacct)-)?[A-Za-z0-9_-]{32,}", "OpenAI-style API key"),
    (r"eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}", "JWT"),
    (
        r"""(client_secret|CLIENT_SECRET)\s*[=:]\s*['"]?[A-Za-z0-9~._-]{20,}""",
        "client secret assignment",
    ),
    (r"AccountKey=[A-Za-z0-9+/=]{60,}", "Azure storage account key"),
)
_COMPILED = tuple(re.compile(source) for source, _label in PATTERNS)


def contains_secret(text):
    """Whether `text` matches a pattern of the repository's secret scanner."""
    return any(pattern.search(text) for pattern in _COMPILED)
_ENV_KEYS = ("PATH", "HOME", "LANG")


class SecretScanError(Exception):
    """The scan could not run; the run must not be treated as clean."""


class _Done:
    def __init__(self, returncode, stderr):
        self.returncode, self.stderr = returncode, stderr


def _run_node(argv, cwd, timeout, env=None):
    done = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return _Done(done.returncode, done.stderr)


def _scan_env():
    return {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}


def _token(out):
    try:
        meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = meta.get("fake_token") if isinstance(meta, dict) else None
    return token if isinstance(token, str) and token else None


def _strings(value):
    """Every string (keys and values) of a parsed document, in order, without recursion."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            for key, child in reversed(list(item.items())):
                stack.append(child)
                stack.append(key)
        elif isinstance(item, list):
            stack.extend(reversed(item))


def _decoded(name, data):
    """The strings of a parsable `.jsonl` (per line) or `.toml` file, one per line, unescaped.

    Unparsable text gives nothing (it is scanned raw); a parser that runs out of stack fails
    closed.
    """
    text = data.decode("utf-8", errors="replace")
    found = []
    try:
        if name.endswith(".jsonl"):
            for line in text.splitlines():
                try:
                    found.extend(_strings(json.loads(line)))
                except ValueError:
                    continue
        elif name.endswith(".toml"):
            try:
                found.extend(_strings(tomllib.loads(text)))
            except ValueError:  # includes TOMLDecodeError
                pass
    except RecursionError:
        raise SecretScanError(f"{name} is nested too deeply to scan") from None
    # surrogatepass: the shim logs a non-UTF-8 argv byte as a lone surrogate
    return ("\n".join(found) + "\n").encode("utf-8", errors="surrogatepass") if found else b""


def _scannable(name, data, token):
    extra = _decoded(name, data)
    if extra:
        data = data + b"\n" + extra
    data = data.replace(b"\x00", b"\n")
    marker = MARKER.encode()
    while marker in data:  # one pass can rebuild a marker from a nested one
        data = data.replace(marker, b"")
    if token:
        data = data.replace(token.encode("utf-8"), PLACEHOLDER.encode())
    return data


def _delete(out, names):
    for name in names:
        try:
            (out / name).unlink()
        except OSError:
            pass


def delete_unscanned(out_dir):
    """Delete every file the scan would cover (for a run that ends before it can be scanned)."""
    _delete(Path(out_dir), FILES)


def scan_run(out_dir, runner=None):
    """Scan the run's files; return the names of the files that hold a secret (empty when clean).

    `runner(argv, cwd, timeout, env)` returns an object with `returncode` and `stderr`; the
    default runs the argv list with `subprocess.run`. Scanner failure is fatal here by design
    (D9, unlike `harness._scan_secrets`, which guards an optional export step): any failure
    deletes every scanned file and raises `SecretScanError`.
    """
    out = Path(out_dir)
    try:
        return _scan(out, runner or _run_node)
    except SecretScanError:
        _delete(out, FILES)
        raise
    except Exception as error:  # copying, reading, deleting: never leave a file unscanned
        _delete(out, FILES)
        raise SecretScanError(f"the secret scan failed: {type(error).__name__}") from None


def _scan(out, runner):
    token = _token(out)
    names = [n for n in FILES if (out / n).is_file()]
    if not names:
        return []
    work = tempfile.mkdtemp(prefix="eval-secret-scan-")  # mode 0700
    try:
        for name in names:
            if (out / name).stat().st_size > MAX_BYTES:
                raise SecretScanError(f"{name} is over {MAX_BYTES} bytes and was not scanned")
            copy = Path(work, name)
            copy.write_bytes(_scannable(name, (out / name).read_bytes(), token))
            copy.chmod(0o600)
        argv = ["node", str(SCANNER), *names]
        try:
            done = runner(argv, work, TIMEOUT, _scan_env())
        except (OSError, subprocess.SubprocessError) as error:
            raise SecretScanError(
                f"the secret scan could not run: {type(error).__name__}"
            ) from None
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if done.returncode == 0:
        return []
    if done.returncode != 1:
        raise SecretScanError(f"the secret scan failed to run (exit {done.returncode})")
    hits = {m.group(1) for m in map(_HIT.match, done.stderr.splitlines()) if m}
    flagged = [n for n in names if n in hits]
    if not flagged:  # exit 1 without a report we understand: fail closed, do not guess
        raise SecretScanError("the secret scan reported a hit that names no scanned file")
    # The draft is derived from the call log, so a hit anywhere removes it too.
    _delete(out, {TRANSCRIPT, DRAFT, *flagged})
    return flagged

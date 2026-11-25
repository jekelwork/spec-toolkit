"""Command-line entry point of the bootstrap-guide evaluation (contracts/eval-cli.md).

    python3 evals/bootstrap-guide/run.py [--scenario NAME ...] [--runs N] [--jobs N]
        [--launcher devvm|local] [--toolkit-ref COMMIT] [--rerun-flagged REPORT]
        [--record-baseline] [--check] [--max-budget-usd X]

Exit codes: 0 report written, no automatic rule flagged or confirmed; 1 report written with at
least one; 2 no report (setup or validation failed, one line on stderr).

Everything with a side effect is injected through `Deps` (process runner, launcher factory, run
executor, clock, streams, paths), so the tests use fakes and never call `claude`.

Agent text stays out of everything written here: `report.json`, `report.md` and `baseline.json`
hold counts and metadata only (`report.py`), the per-run files are written, and secret-scanned,
by `turns.py`, and the lines printed here hold scenario names, run numbers and fixed reasons.
"""

import argparse
import concurrent.futures
import dataclasses
import datetime
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from pathlib import Path

import harness
import launchers
import profiles
import report
import rules
import turns

HERE = Path(__file__).resolve().parent
SKILL_DIR = ".agents/skills/bootstrap-guide"
RERUN_RUNS = 10
MAX_REPORT_BYTES = 8 << 20
_FULL_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_VERSION = re.compile(r"^\d+(?:\.\d+)+$")
_STATUSES = ("ok", "flagged", "confirmed", "cleared", "insufficient", "new")


class Stop(Exception):
    """Setup or validation failed: print the one-line message on stderr and exit 2."""


def default_runner(argv, cwd=None, timeout=30):
    """`(returncode, stdout)` of an argument list run without a shell; 127 when it cannot run."""
    try:
        done = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=launchers.allowlisted_env(os.environ),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return 127, ""
    return done.returncode, done.stdout


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def judge_sandbox(launcher, root):
    """The `SandboxSpec` of one judge call, built inside `root` (a real, existing path).

    The judge is a `claude -p` with no tools: it may execute `claude` alone, writes only a
    throwaway HOME and TMPDIR under `root`, reads nothing under the maintainer's home, and may use
    the network (the one sandbox that may), narrowed in `launchers._NETWORK_RULES`.

    The excerpt it is sent may hold the maintainer's own paths (the agent's text is secret-scanned
    and the fake token redacted, but a path is neither): it goes to the same model provider as the
    agent run itself, under the same login, so nothing new leaves the machine.
    """
    root = Path(root)
    home, tmp = root / "home", root / "tmp"
    home.mkdir(exist_ok=True)
    tmp.mkdir(exist_ok=True)
    return launchers.SandboxSpec(
        home=str(launcher.home),
        home_override=str(home),
        tmpdir=str(tmp),
        allow_read=(str(root / "cwd"),),  # `Judge.ask` runs the call there
        exec_paths=(os.path.realpath(launcher.claude_path),),
        network=True,
        system_exec=False,
    )


def default_judge_factory(launcher, model, max_budget_usd):
    """`factory(env) -> rules.Judge` on the pinned model, run through the launcher's sandbox."""

    # The sandbox allows the resolved path only (a symlink is not executed through): run that.
    claude = os.path.realpath(launcher.claude_path)

    def run_in_sandbox(argv, cwd, env, timeout, *, sandbox, stdin=None):
        env = launchers.judge_call_env(env, sandbox)
        return launcher.run(argv, cwd, env, timeout, sandbox=sandbox, stdin=stdin)

    def make(env):
        return rules.Judge(
            run_in_sandbox,
            env,
            model,
            max_budget_usd,
            sandbox_factory=lambda root: judge_sandbox(launcher, root),
            claude=claude,
            secret_values=[launcher.parent_env.get(n) for n in launchers.JUDGE_CREDENTIALS],
        )

    return make


@dataclasses.dataclass
class Deps:
    runner: object = default_runner
    execute: object = turns.execute_run
    launcher_factory: object = launchers.create
    judge_factory: object = default_judge_factory
    judge_pass: object = turns.score_judgement
    now: object = utc_now
    monotonic: object = time.monotonic
    stdout: object = None
    stderr: object = None
    repo_root: Path = harness.REPO_ROOT
    evals_dir: Path = HERE


def load_config(path=HERE / "config.toml"):
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise Stop(f"config.toml cannot be read ({type(error).__name__})") from None


# --- arguments --------------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Stop(message)


def _positive_int(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _positive_float(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return value


def parse_args(argv):
    parser = _Parser(prog="run.py", description="Evaluate the bootstrap-guide skill.")
    parser.add_argument("--scenario", action="append", default=[], metavar="NAME")
    parser.add_argument("--runs", type=_positive_int)
    parser.add_argument("--jobs", type=_positive_int)
    parser.add_argument("--launcher", choices=("devvm", "local"))
    parser.add_argument("--toolkit-ref", metavar="COMMIT")
    parser.add_argument("--rerun-flagged", metavar="REPORT")
    parser.add_argument("--record-baseline", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--max-budget-usd", type=_positive_float, metavar="X")
    args = parser.parse_args(argv)
    if args.record_baseline and (args.scenario or args.rerun_flagged):
        raise Stop("--record-baseline runs every scenario: not with --scenario or --rerun-flagged")
    if args.rerun_flagged and args.scenario:
        raise Stop("--rerun-flagged picks the scenarios itself: not with --scenario")
    return args


# --- validation (--check) ---------------------------------------------------------------------


def check_problems(scenarios_dir, fixtures_dir):
    """Every problem in the scenarios, fixtures, tool profiles and rule registry."""
    problems = list(harness.validate(scenarios_dir, fixtures_dir))
    for name in profiles.PROFILES:
        try:
            profiles.tools(name)
            profiles.settings(name, "<RUN>", "<HOME>")
        except Exception as error:
            problems.append(f"profile {name}: {error}")
    seen = set()
    for rule in rules.RULES:
        if rule.id in seen:
            problems.append(f"rule {rule.id}: listed twice in the registry")
        seen.add(rule.id)
        if not rule.skill_ref.strip():
            problems.append(f"rule {rule.id}: no skill_ref")
    for rule in rules.RULES:
        if rule.replaces and rule.replaces not in seen and rule.replaces not in rules.RETIRED:
            problems.append(f"rule {rule.id}: replaces unknown rule {rule.replaces}")
    return problems


def _one_line(text):
    return " ".join(str(text).split())


# --- files read safely ------------------------------------------------------------------------


def read_json_file(path, what):
    """A regular, not-linked JSON file of at most `MAX_REPORT_BYTES`, parsed; else `Stop`."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        raise Stop(f"{what} cannot be opened as a regular file") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REPORT_BYTES:
            raise Stop(f"{what} is not a regular file of at most {MAX_REPORT_BYTES} bytes")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(MAX_REPORT_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_REPORT_BYTES:
        raise Stop(f"{what} is larger than {MAX_REPORT_BYTES} bytes")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Stop(f"{what} is not valid JSON") from None
    if not isinstance(value, dict) or not isinstance(value.get("scenarios"), list):
        raise Stop(f"{what} is not a report: it has no scenarios list")
    return value


def read_baseline(evals_dir):
    path = Path(evals_dir) / "baseline.json"
    if not os.path.lexists(path):
        return None
    return read_json_file(path, "baseline.json")


def read_flagged(path, known, registry_ids):
    """`{scenario name: [flagged rule ids]}` from the report file at `path`.

    The file is data only, and only which rules were flagged is taken from it: scenario names
    must be known ones, rule ids registered strings and statuses the defined strings. Nothing
    else in it reaches a report, a path or a command.
    """
    rep = read_json_file(path, "the --rerun-flagged report")
    flagged = {}
    for scenario in rep["scenarios"]:
        name = scenario.get("name") if isinstance(scenario, dict) else None
        entries = scenario.get("rules") if isinstance(scenario, dict) else None
        if not isinstance(name, str) or name not in known:
            raise Stop(f"the report names a scenario that does not exist: {name!r}")
        if not isinstance(entries, list):
            raise Stop(f"the report has no rules list for scenario {name}")
        for entry in entries:
            rule, status = (entry.get("rule"), entry.get("status")) if isinstance(entry, dict) else (None, None)
            if (
                not isinstance(rule, str)
                or rule not in registry_ids
                or not isinstance(status, str)
                or status not in _STATUSES
            ):
                raise Stop(f"the report holds an unknown rule or status in scenario {name}")
            if status == "flagged" and rule not in flagged.setdefault(name, []):
                flagged[name].append(rule)
    if not flagged:
        raise Stop("the report has no flagged rules: nothing to rerun")
    return flagged


# --- git and claude ---------------------------------------------------------------------------


def _git(deps, *args):
    code, out = deps.runner(["git", *args], cwd=str(deps.repo_root))
    if code != 0:
        raise Stop(f"git {args[0]} failed in {deps.repo_root}")
    return out.strip()


def resolve_ref(deps, ref):
    """The full SHA of `ref`; git never gets an option-looking value."""
    if ref.startswith("-"):
        raise Stop(f"--toolkit-ref {ref!r} must be a commit or a name, not an option")
    code, out = deps.runner(
        ["git", "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"],
        cwd=str(deps.repo_root),
    )
    sha = out.strip()
    if code != 0 or not _FULL_SHA.match(sha):
        raise Stop(f"--toolkit-ref {ref!r} is not a commit of {deps.repo_root}")
    return sha


def skill_words(deps, sha):
    """The skill's word counts, from the working tree or from the commit `sha`."""
    if sha is None:
        skill = Path(deps.repo_root) / SKILL_DIR
        if not (skill / "SKILL.md").is_file():
            raise Stop(f"{SKILL_DIR}/SKILL.md not found in the working tree")
        return report.skill_words(skill)
    code, raw = deps.runner(
        ["git", "ls-tree", "-r", "-z", sha, "--", SKILL_DIR], cwd=str(deps.repo_root)
    )
    if code != 0:
        raise Stop(f"git ls-tree failed in {deps.repo_root}")
    blobs = []
    for entry in raw.split("\0"):
        if not entry:
            continue
        info, _, name = entry.partition("\t")
        fields = info.split()
        if len(fields) != 3 or fields[0] not in ("100644", "100755") or fields[1] != "blob":
            continue  # a symlink, a submodule: never read
        prefix = SKILL_DIR + "/"
        parts = name[len(prefix) :].split("/") if name.startswith(prefix) else None
        if (
            parts is None
            or any(p in ("", ".", "..") for p in parts)
            or any(ord(c) < 32 or c == "\x7f" for c in name)
        ):
            raise Stop(f"{sha} lists an unsafe path under {SKILL_DIR}; refusing to read it")
        blobs.append((name, parts))
    wanted = [name for name, parts in blobs if parts == ["SKILL.md"]]
    if not wanted:
        raise Stop(f"{SKILL_DIR}/SKILL.md not found in {sha}")
    wanted += sorted(
        name
        for name, parts in blobs
        if len(parts) == 2 and parts[0] == "references" and parts[1].endswith(".md")
    )
    with tempfile.TemporaryDirectory(prefix="skill-words-") as temp:
        root = Path(temp).resolve()
        for name in wanted:
            target = (root / name[len(SKILL_DIR) + 1 :]).resolve()
            if not target.is_relative_to(root):
                raise Stop(f"{sha} lists an unsafe path under {SKILL_DIR}; refusing to read it")
            code, text = deps.runner(
                ["git", "show", f"{sha}:{name}"], cwd=str(deps.repo_root), timeout=60
            )
            if code != 0:
                raise Stop(f"git show failed for {name} in {sha}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return report.skill_words(root)


def preflight_judge(launcher):
    """Stops unless the judge is logged in, checked the way the judge will run.

    A judge runs in a sandbox with a throwaway HOME, so it has no keychain login: it needs a token
    in its environment (`launcher.judge_env`). `claude auth status --json` through the same
    sandbox and environment reads that and costs nothing (it does not check the token against the
    service). The output is never printed or kept.
    """
    message = (
        "the judge is not logged in: set CLAUDE_CODE_OAUTH_TOKEN (`claude setup-token`) "
        "or ANTHROPIC_API_KEY"
    )
    with tempfile.TemporaryDirectory(prefix="judge-login-") as temp:
        root = os.path.realpath(temp)
        os.mkdir(os.path.join(root, "cwd"))
        try:
            sandbox = judge_sandbox(launcher, root)
            env = launchers.judge_call_env(launcher.judge_env(), sandbox)
            stdout, _stderr, _timed_out = launcher.run(
                [os.path.realpath(launcher.claude_path), "auth", "status", "--json"],
                os.path.join(root, "cwd"),
                env,
                60,
                sandbox=sandbox,
            )
        except launchers.SandboxUnavailable as error:
            raise Stop(_one_line(error)) from None
        except Exception as error:
            raise Stop(f"the judge login check could not run ({type(error).__name__})") from None
    try:
        status = json.loads(stdout)
    except (TypeError, ValueError):
        status = None
    if not isinstance(status, dict) or status.get("loggedIn") is not True:
        raise Stop(message)


def _is_script(path):
    try:
        with open(os.path.realpath(path), "rb") as handle:
            return handle.read(2) == b"#!"
    except OSError:
        return False


def preflight_claude(deps, launcher, needs_judge=False):
    """The Claude Code version; stops when it is missing, unreadable or not logged in.

    `claude auth status --json` only reads the stored login: it does not call the model and costs
    nothing. Its output is never printed (it holds an account name). When a judge will run, the
    executable must be a native binary: the judge's sandbox lets it execute that file alone, so
    a `#!` script (which needs an interpreter) could never run there. The agent itself would be
    fine, so without judgement rules a script is accepted.
    """
    try:
        launcher.prepare()
    except RuntimeError as error:
        raise Stop(_one_line(error)) from None
    claude = launcher.claude_path
    if needs_judge and _is_script(claude):
        raise Stop(
            "the judge needs the native claude binary, but claude is a script "
            f"({os.path.realpath(claude)}); install the native build"
        )
    code, out = deps.runner([claude, "--version"])
    words = out.split()
    if code != 0 or not words or not _VERSION.match(words[0]):
        raise Stop("the Claude Code version cannot be read (`claude --version`)")
    version = words[0]
    code, out = deps.runner([claude, "auth", "status", "--json"])
    try:
        status = json.loads(out) if code == 0 else None
    except ValueError:
        status = None
    if not isinstance(status, dict) or status.get("loggedIn") is not True:
        raise Stop("Claude Code is not logged in (`claude auth status`); run `claude auth login`")
    return version


# --- the evaluation ---------------------------------------------------------------------------


def _summary(rep):
    counts = {name: 0 for name in ("ok", "flagged", "confirmed", "insufficient")}
    for scenario in rep["scenarios"]:
        for entry in scenario["rules"]:
            if entry["status"] in counts:
                counts[entry["status"]] += 1
    return (
        f"Rules: {counts['ok']} ok, {counts['flagged']} flagged, "
        f"{counts['confirmed']} confirmed, {counts['insufficient']} insufficient"
    )


_write = harness.write_atomic


def _judge_all(deps, launcher, model, budget, selected, results, runs_dir, err):
    """Score the judgement rules of every scored run, one run at a time, in the calling thread.

    Runs only after every agent has ended: a judge call holds a login credential in its
    environment, and nothing else may be live to read it. Each judge reads only its run's output
    directory (the run root is gone) and rewrites that run's `result.json`; `results` are updated
    in place. A missing sandbox or any other failure stops the evaluation, as in a run.
    """
    by_name = {s.name: s for s in selected}
    judge = deps.judge_factory(launcher, model, budget)(launcher.judge_env())
    for result in results:
        scenario = by_name[result["scenario"]]
        if not result["scored"] or not any(
            rules.get(r).kind == "judgement" for r in scenario.rules
        ):
            continue  # an unscored run has nothing to judge
        out_dir = runs_dir / scenario.name / str(result["run"])
        try:
            deps.judge_pass(scenario, result, out_dir, judge)
        except (turns.RunSetupError, launchers.SandboxUnavailable) as error:
            raise Stop(_one_line(error)) from None
        except Exception as error:
            raise Stop(f"unexpected error ({type(error).__name__})") from None
        print(f"judging {scenario.name} run {result['run']}", file=err, flush=True)


def _execute_all(deps, jobs_list, launcher, config, sha, runs_dir, fixtures, out):
    """Run every `(scenario, number)`; print one line per finished run; return the result dicts.

    Each run has its own run root and output directory; the launcher is shared (it is built for
    that) and all printing happens here, in the calling thread. A setup failure stops the
    evaluation: runs not yet started are skipped and `Stop` is raised once the running ones end.
    """
    stop = threading.Event()
    base = Path(tempfile.mkdtemp(prefix="eval-")).resolve()
    failure = []

    def work(job):
        scenario, number = job
        if stop.is_set():
            return None
        try:
            return deps.execute(
                scenario,
                number,
                fixtures / scenario.fixture,
                launcher,
                base / f"{scenario.name}-{number}",
                runs_dir / scenario.name / str(number),
                timeout=config["timeout_seconds"],
                source_repo=deps.repo_root,
                toolkit_ref=sha,
            )
        except BaseException:
            stop.set()  # at once, so a worker that starts next sees it
            raise

    results = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=config["jobs"])
    try:
        futures = {pool.submit(work, job): job for job in jobs_list}
        for future in concurrent.futures.as_completed(futures):
            scenario, number = futures[future]
            try:
                result = future.result()
            except (turns.RunSetupError, launchers.SandboxUnavailable) as error:
                failure.append(_one_line(error))
            except Exception as error:
                failure.append(f"unexpected error ({type(error).__name__})")
            else:
                if result is not None:
                    results[(scenario.name, number)] = result
                    state = (
                        "scored" if result["scored"] else f"unscored ({result['unscored_reason']})"
                    )
                    print(f"{scenario.name} run {number}: {state}", file=out, flush=True)
                continue
            stop.set()
            for pending in futures:
                pending.cancel()
    except BaseException:  # for example Ctrl-C: drop the queue and end the running agents
        stop.set()
        pool.shutdown(wait=False, cancel_futures=True)
        launcher.close()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        shutil.rmtree(base, ignore_errors=True)
    if failure:
        raise Stop(failure[0])
    return [results[(s.name, n)] for s, n in jobs_list if (s.name, n) in results]


def _plan(out, scenarios, runs_of, config, launcher_name, model, budget):
    sessions = sum(
        runs_of(s) * (1 + sum(rules.get(r).kind == "judgement" for r in s.rules))
        for s in scenarios
    )
    print("Plan:", file=out)
    print(f"  scenarios: {', '.join(s.name for s in scenarios)}", file=out)
    print(f"  runs: {', '.join(f'{s.name} x{runs_of(s)}' for s in scenarios)}", file=out)
    print(f"  jobs: {config['jobs']}", file=out)
    print(f"  launcher: {launcher_name}", file=out)
    print(f"  model: {model}", file=out)
    print(f"  budget per run: ${budget}", file=out)
    print(f"  estimated agent sessions: {sessions} (runs plus judge calls)", file=out, flush=True)


def _check_report(problems, out, scenarios_dir, fixtures_dir):
    if problems:
        raise Stop(f"--check failed: {_one_line(problems[0])} ({len(problems)} problem(s))")
    scenarios = harness.load_all(scenarios_dir, fixtures_dir)
    names = ", ".join(s.name for s in scenarios) or "-"
    print(f"Scenarios ({len(scenarios)}): {names}", file=out)
    print(f"Profiles ({len(profiles.PROFILES)}): {', '.join(profiles.PROFILES)}", file=out)
    print(f"Rules ({len(rules.RULES)}): {', '.join(r.id for r in rules.RULES)}", file=out)
    print("check ok", file=out)


def _main(argv, deps, out, err):
    args = parse_args(argv)
    evals_dir = Path(deps.evals_dir)
    scenarios_dir, fixtures_dir = evals_dir / "scenarios", evals_dir / "fixtures"

    problems = check_problems(scenarios_dir, fixtures_dir)
    if args.check:
        _check_report(problems, out, scenarios_dir, fixtures_dir)
        return 0

    config = load_config(evals_dir / "config.toml")
    model = config.get("model")
    if not isinstance(model, str) or not model.strip():
        raise Stop("the pinned model is not set (`model` in config.toml)")
    if problems:
        raise Stop(f"--check failed: {_one_line(problems[0])} ({len(problems)} problem(s))")
    budget = args.max_budget_usd or config.get("max_budget_usd")
    if not isinstance(budget, (int, float)) or isinstance(budget, bool) or not budget > 0:
        raise Stop("max_budget_usd in config.toml must be a positive number")
    jobs = config.get("jobs", 4)
    if not isinstance(jobs, int) or isinstance(jobs, bool) or jobs < 1:
        raise Stop("jobs in config.toml must be an integer of at least 1")
    timeout = config.get("timeout_seconds")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not timeout > 0:
        raise Stop("timeout_seconds in config.toml must be a positive number")
    config = {**config, "jobs": args.jobs or jobs}
    launcher_name = args.launcher or config.get("launcher", "local")

    known = {s.name: s for s in harness.load_all(scenarios_dir, fixtures_dir)}
    flagged = None
    if args.rerun_flagged:
        flagged = read_flagged(args.rerun_flagged, known, {r.id for r in rules.RULES})
        selected = [known[n] for n in known if n in flagged]
    elif args.scenario:
        missing = [n for n in args.scenario if n not in known]
        if missing:
            raise Stop(f"unknown scenario {missing[0]!r}; known: {', '.join(known) or '-'}")
        selected = [known[n] for n in known if n in args.scenario]
    else:
        selected = list(known.values())
    if not selected:
        raise Stop("no scenarios to run")

    dirty = bool(_git(deps, "status", "--porcelain", "--untracked-files=all"))
    if args.record_baseline and dirty and not args.toolkit_ref:
        raise Stop(
            "the working tree is dirty: commit or stash, or pass --toolkit-ref to record from a commit"
        )
    commit = _git(deps, "rev-parse", "--short=7", "HEAD")
    sha = resolve_ref(deps, args.toolkit_ref) if args.toolkit_ref else None
    if sha:  # the toolkit under test came from the commit, not from the working tree
        dirty = False
    words = skill_words(deps, sha)
    baseline = read_baseline(evals_dir)

    def runs_of(scenario):
        return args.runs or (RERUN_RUNS if flagged else scenario.runs)

    try:
        launcher = deps.launcher_factory(launcher_name, model=model, max_budget_usd=budget)
    except (NotImplementedError, ValueError, RuntimeError, OSError) as error:
        raise Stop(f"the {launcher_name} launcher cannot start: {_one_line(error)}") from None
    try:
        needs_judge = any(rules.get(r).kind == "judgement" for s in selected for r in s.rules)
        version = preflight_claude(deps, launcher, needs_judge=needs_judge)
        if needs_judge:
            preflight_judge(launcher)
        _plan(out, selected, runs_of, config, launcher_name, model, budget)
        created = deps.now().astimezone(datetime.timezone.utc)
        runs_dir = evals_dir / "runs" / created.strftime("%Y%m%dT%H%M%SZ")
        try:
            runs_dir.mkdir(parents=True)
        except OSError:
            raise Stop(f"the run directory {runs_dir} cannot be created") from None
        jobs_list = [(s, n) for s in selected for n in range(1, runs_of(s) + 1)]
        started = deps.monotonic()
        results = _execute_all(
            deps, jobs_list, launcher, config, sha, runs_dir, fixtures_dir, out
        )
        if needs_judge:
            _judge_all(deps, launcher, model, budget, selected, results, runs_dir, err)
        wall = int(round(deps.monotonic() - started))
    finally:
        launcher.close()

    meta = {
        "created": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "toolkit_commit": commit,
        "toolkit_ref": sha,
        "dirty": dirty,
        "launcher": launcher_name,
        "agent": "claude-code",
        "agent_version": version,
        "model": model,
        "skill_words": words,
        "runs_per_scenario": max(runs_of(s) for s in selected),
        "wall_seconds": wall,
    }
    hashes = {s.name: harness.scenario_hash(s, fixtures_dir) for s in selected}
    declared = {s.name: list(s.rules) for s in selected}
    rep = report.build_report(results, hashes, baseline, meta, scenario_rules=declared)
    if flagged is not None:
        rep, undecided = report.apply_rerun(rep, flagged)
        for name, rule_id, why in undecided:
            print(
                f"warning: {name} / {rule_id}: the rerun could neither confirm nor clear the "
                f"flag ({why}); it is reported as cleared, not as a regression",
                file=err,
            )
    _write(runs_dir / "report.json", json.dumps(rep, indent=2) + "\n")
    _write(runs_dir / "report.md", report.render_markdown(rep))
    if args.record_baseline:
        _write(
            evals_dir / "baseline.json",
            json.dumps(report.baseline_from_report(rep), indent=2) + "\n",
        )
        print(f"Baseline written: {evals_dir / 'baseline.json'}", file=out)
    print(runs_dir / "report.md", file=out)
    print(_summary(rep), file=out, flush=True)
    return report.exit_code(rep)


def main(argv=None, deps=None):
    deps = deps or Deps()
    out = deps.stdout or sys.stdout
    err = deps.stderr or sys.stderr
    try:
        return _main(sys.argv[1:] if argv is None else argv, deps, out, err)
    except Stop as stop:
        print(f"error: {_one_line(stop)}", file=err)
        return 2
    except SystemExit as exit_:  # --help
        return exit_.code or 0


if __name__ == "__main__":
    sys.exit(main())

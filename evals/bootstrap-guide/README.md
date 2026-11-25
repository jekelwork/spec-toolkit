# Bootstrap-guide evaluation

Runs the `bootstrap-guide` skill with a real coding agent (Claude Code) against scripted scenarios,
checks each transcript with rules, and compares the result with a committed baseline. Use it to
see whether a change to the skill or the engine made the guide worse.

This directory is repository-only tooling. It is never exported to adopters.

The agent runs are paid and slow, so they **never run in CI**. The `Evals` workflow
(`.github/workflows/evals.yml`) runs only the unit tests below, with no agent and no network.

## Commands

One command runs the evaluation. The full contract, with the output format and exit codes, is in
[contracts/eval-cli.md](../../specs/004-bootstrap-guide-skill-evaluation-and-simplification/contracts/eval-cli.md).

```bash
python3 evals/bootstrap-guide/run.py [--scenario NAME ...] [--runs N] [--jobs N]
                                     [--launcher devvm|local] [--toolkit-ref COMMIT]
                                     [--rerun-flagged REPORT] [--record-baseline]
                                     [--check] [--max-budget-usd X]
```

| Option                   | Meaning                                                                                                                                     |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------- |
| (none)                   | Run every scenario, write a report and compare it with `baseline.json`.                                                                     |
| `--scenario NAME`        | Run only the named scenarios. Repeat the option for more than one.                                                                          |
| `--runs N`               | Runs per scenario. Overrides each scenario's own `runs` (default 5).                                                                        |
| `--jobs N`               | Parallel runs. Default 4 (`config.toml`).                                                                                                   |
| `--launcher devvm\|local` | Which launcher starts the agent. Default `local` (`config.toml`). `devvm` is not implemented: it stops with exit 2 and a message.           |
| `--toolkit-ref COMMIT`   | Build each run's repository from that commit (a SHA or a ref name, resolved to the full SHA) instead of the working tree.                   |
| `--rerun-flagged REPORT` | Rerun only the scenarios with flagged rules in REPORT, 10 runs each unless `--runs` is given. Marks each flag `confirmed` or `cleared`.     |
| `--record-baseline`      | Run all scenarios and write `baseline.json`. Refuses a dirty working tree (exit 2, untracked files count) unless `--toolkit-ref` is given. |
| `--check`                | Validate scenarios, fixtures, tool profiles and the rule registry. Runs no agent.                                                           |
| `--max-budget-usd X`     | Per-run budget cap passed to the agent. Default $2 (`max_budget_usd` in `config.toml`).                                                     |

`--record-baseline` cannot be combined with `--scenario` or `--rerun-flagged`, and `--rerun-flagged`
cannot be combined with `--scenario`. Each stops with exit 2.

Check the setup first. It costs nothing:

```bash
python3 evals/bootstrap-guide/run.py --check
python3 -m unittest discover -s evals/bootstrap-guide -p 'test_*.py'   # about 140 s
```

`--check` validates only scenarios, fixtures, profiles and rules. The login, the judge credential,
the native binary and the scanner probe are checked at the start of a real run, before any agent
session.

Settings live in [config.toml](config.toml): the pinned model, the per-run budget cap, the per-run
timeout, `jobs` and the default launcher.

## Prerequisites

- **macOS.** The `local` launcher runs the agent's shown commands and the judge under
  `/usr/bin/sandbox-exec`, and the agent's own Bash sandbox is macOS too. There is no unsandboxed
  fallback: without `sandbox-exec` the evaluation stops with exit 2. Every current scenario that runs
  a shown command also has a judge, so a missing `sandbox-exec` stops before any agent session.
- Python 3.11 or newer (check `python3 --version`; macOS's `/usr/bin/python3` is often 3.9 and
  fails with an `ImportError` on `tomllib`), Git and Node (the secret scanner is
  `node scripts/check-secrets.mjs`). The interpreter that runs `run.py` is also used inside runs.
- Claude Code installed and logged in: `claude --version` works and `claude auth status` shows
  you logged in. `run.py` stops with exit 2 otherwise.
- No real `gh`, GitHub login or network access to GitHub is needed: the `gh` shim answers from the
  fixture's `gh.toml`.
- The agent gets only `HOME` and `TERM` from your environment; no GitHub token or SSH agent.
- Scenarios with judgement rules also call a judge. It runs in a sandbox with a throwaway `HOME`,
  so it cannot use your keychain login. Set one of these in your shell before the run:
  - `CLAUDE_CODE_OAUTH_TOKEN`, a long-lived token from `claude setup-token` (preferred), or
  - `ANTHROPIC_API_KEY` (bills that API account).

  The judge also needs the native `claude` binary, not a script. `run.py` checks that a token
  is set and the binary is native, and stops with exit 2 if not. It does not check that the token
  is valid: an expired token fails only at judging, after all paid runs, and the affected rules
  become `not_applicable` (the reason is only in the run's `result.json`). After the first run,
  check the judgement counts in `report.md`. The first scenario to try, `exit3-preview-guide-me`,
  has a judgement rule, so it needs the token too.

## Cost and time

- A full run is 55 agent sessions (11 scenarios times 5 runs, 4 in parallel) plus up to 40 judge
  calls (8 of the 11 scenarios have a judgement rule; a judge runs only for a scored run whose
  transcript has the rule's situation). The plan line `estimated agent sessions` prints 95, which
  counts the maximum.
- Judge calls run one at a time after all agent runs, each with a 120 s timeout.
- Research D12 targets under 60 minutes of wall time (SC-001). That is a target, not a
  measurement. The worst case, every agent run hitting the 10-minute timeout, is about 140 minutes
  (14 waves of 4) plus judge time. Waiting for the launcher's lock extends a run's deadline.
- The per-run budget cap is `max_budget_usd` ($2) in `config.toml`, and each judge call gets the
  same cap. A run that hits it is unscored (`budget`).
- `report.md` prints the wall time and the cost of the agent runs. Judge cost is not in it: it
  goes to the judge credential.
- Use `--scenario NAME --runs 1` to try one scenario for a fraction of the cost.

## Reading `report.md`

Each run writes `evals/bootstrap-guide/runs/<UTC timestamp>/` with `report.json`, `report.md` and
one directory per run, `<scenario>/<n>/`. `runs/` is ignored by git. A run directory holds
`transcript.jsonl`, `gh-calls.jsonl`, `meta.json`, `agent-stderr.log` and `result.json`, and
`fixture-draft.toml` only when the fixture was stale. A `secret_found` or `unscannable` run has had
its scanned files deleted. The report format is in
[contracts/eval-report.md](../../specs/004-bootstrap-guide-skill-evaluation-and-simplification/contracts/eval-report.md).

`report.md` has these parts, in order:

1. The metadata line: commit or ref, launcher, model, agent version, skill word counts.
2. Warnings, including one per `unscannable` run.
3. A **Rerun** section, only for `--rerun-flagged` (see below).
4. A table of flagged and confirmed rules.
5. A table per scenario: rule, passed/scored, baseline, status.
6. The judgement rules, in a section of their own.
7. The unscored runs with their reasons.
8. Cost and wall time.

| Status         | Meaning                                                                                                                   |
| -------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `ok`           | No drop of 2/5 or more against the baseline.                                                                              |
| `flagged`      | The pass fraction dropped by at least 2/5 against the baseline.                                                           |
| `confirmed`    | A rerun showed the drop again.                                                                                            |
| `cleared`      | A rerun no longer showed the drop.                                                                                        |
| `insufficient` | Fewer than 3 scored runs in this report or the baseline (so `--runs 1` against a baseline gives this).                    |
| `new`          | The scenario, or the rule's version, differs from the baseline, or it is absent. Also: no baseline, or a different model. |

There is no `baseline.json` until it is recorded. Until then the report warns
`no baseline: every status is new`, shows every status as `new` and exits 0.

Judgement rules get the same statuses but never change the exit code.

A run is **unscored** and left out of the counts when its `unscored_reason` is one of:

| Reason          | Meaning                                                                                                                                                                                                                                                      |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `agent_error`   | The agent failed.                                                                                                                                                                                                                                            |
| `budget`        | The per-run budget cap was hit.                                                                                                                                                                                                                              |
| `timeout`       | The per-run timeout was hit.                                                                                                                                                                                                                                 |
| `fixture_stale` | A `gh` call matched no fixture entry. `fixture-draft.toml` holds draft `[[call]]` entries to start updating `gh.toml` from.                                                                                                                                  |
| `off_script`    | The agent's reply missed a turn's `expect`.                                                                                                                                                                                                                  |
| `secret_found`  | The secret scan found a hit. The scanned files are deleted.                                                                                                                                                                                                  |
| `unscannable`   | Its files could not be scanned. A hostile transcript can cause this, so treat it like `secret_found`. Its files are deleted and the evaluation goes on; if every run is `unscannable`, there is no report and exit 2. |
| `tampered`      | An integrity check failed: the run's files, call log or repository changed outside what the harness allows.                                                                                                                                                  |

### Exit codes and stdout

| Code | Meaning                                                                        |
| ---- | ------------------------------------------------------------------------------ |
| 0    | Report written; no automatic rule is `flagged` or `confirmed`.                 |
| 1    | Report written; at least one automatic rule is `flagged` or `confirmed`.       |
| 2    | No report: setup or validation failed. The reason is on stderr (`error: ...`). |

stdout holds, in order:

1. The plan (`Plan:` and indented lines).
2. One progress line per finished run: `<scenario> run <n>: scored` or
   `<scenario> run <n>: unscored (<reason>)`.
3. `Baseline written: <path>`, only with `--record-baseline`.
4. The path of `report.md`.
5. `Runs: <scored> scored, <unscored> unscored`.
6. `Rules: <ok> ok, <flagged> flagged, <confirmed> confirmed, <cleared> cleared, <insufficient> insufficient, <new> new`.

The `Rules:` line is always last, so a finished evaluation is the one whose last line starts with
`Rules:`. When the run stops with exit 2, there is no `Runs:` or `Rules:` line. Judge progress
(`judging <scenario> run <n>`), `warning:` lines and the `error:` line go to stderr.

## Rerunning flagged scenarios

A flag can be noise from 5 runs. Rerun it before acting on it:

```bash
python3 evals/bootstrap-guide/run.py --rerun-flagged evals/bootstrap-guide/runs/<timestamp>/report.json
```

- It reruns only the scenarios with flagged rules (judgement rules too), 10 runs each unless you
  pass `--runs`. With no flagged rule in the report, it stops with exit 2.
- It tests the current working tree unless you pass `--toolkit-ref`. It does not reuse the
  report's `toolkit_ref`, so rerun with the same toolkit as the earlier report.
- It compares against the same baseline as the earlier report. If the baseline's commits or model
  differ from the earlier report's `baseline` block, it stops with exit 2 before any run.
- The new report's **Rerun** section names the earlier report by its time and commit (never a file
  path), the runs per scenario, and a table of scenario, rule, before, after and a note.
- `after` is `confirmed` or `cleared`. A flag cleared without a decision says why in the note:
  too few scored runs (`insufficient`), the scenario or rule changed (`new`), or the pair was not
  rerun (`not_rerun`).

A `confirmed` automatic rule is a regression to fix.

## Re-recording the baseline

`baseline.json` holds counts and metadata only, no transcript text. It is committed.

A different pinned model makes the whole report not comparable: every status is `new`, with a
warning, and exit 0. A different Claude Code version still compares but warns. After either
update, and if results shift, record a new baseline on the **unchanged** skill, so the before and
after comparison survives:

1. Set the new model in `config.toml` if the model changed.
2. Find the baseline commit: `toolkit_ref` in `baseline.json` (or in `report.json`'s `baseline`
   block) when it is set, else `toolkit_commit`.
3. Record from that commit:

   ```bash
   python3 evals/bootstrap-guide/run.py --record-baseline --toolkit-ref <baseline commit>
   ```

4. Review and commit `baseline.json`.

Without `--toolkit-ref`, `--record-baseline` records the working tree and refuses a dirty one.

## Scenarios and fixtures

- `scenarios/<name>.toml` holds the scripted user turns, the rules and the tool profile.
- `fixtures/<state>/` holds the repository state. Each of these is optional: `files/` overlaid on
  the exported toolkit, `gh.toml` (the `gh` shim's responses) and `git.toml`.

The formats are in
[data-model.md](../../specs/004-bootstrap-guide-skill-evaluation-and-simplification/data-model.md).
Rules learned while writing the fixtures:

- Every run on the real engine starts with the coding-agent choice, so the scripted first message
  must answer it (for example "I use Claude Code").
- Only the `higher-format-version` scenario uses a stub engine (research D13). Every other fixture
  runs the real `scripts/bootstrap.py`.
- A fixture holds no name from this repository's `project-context.toml`. `test_scenarios.py`
  checks it. The engine's own bootstrap marker is the one exception.
- A fixture cannot check out a branch. `git.toml` only creates branches in the local `origin`.

Run `--check` after any scenario or fixture change.

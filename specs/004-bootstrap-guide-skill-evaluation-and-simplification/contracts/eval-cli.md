# Contract: `evals/bootstrap-guide/run.py`

Repository-only tooling (research D1); never exported.

```text
python3 evals/bootstrap-guide/run.py [--scenario NAME ...] [--runs N] [--jobs N]
                                     [--launcher devvm|local] [--toolkit-ref COMMIT]
                                     [--rerun-flagged REPORT] [--record-baseline]
                                     [--check] [--max-budget-usd X]
```

| Option | Meaning |
| --- | --- |
| (none) | Run every scenario `runs` times (default 5), 4 in parallel, write a report and compare it with `baseline.json`. |
| `--scenario NAME` | Run only the named scenarios (repeatable). |
| `--runs N` | Runs per scenario (overrides each scenario's own value). |
| `--jobs N` | Parallel runs (default 4). |
| `--launcher devvm\|local` | Which launcher starts the agent (research D4a). The default is set in `config.toml` from the Phase 1 spike's result. |
| `--toolkit-ref COMMIT` | Build each run's repository from that commit instead of the working tree (research D2). |
| `--rerun-flagged REPORT` | Rerun only the scenarios with `flagged` rules in REPORT, 10 runs each unless `--runs` is given, and mark each flag `confirmed` or `cleared` (research D7). |
| `--record-baseline` | Run all scenarios and write `baseline.json`. Refuses a dirty working tree (exit 2) unless `--toolkit-ref` is given. |
| `--check` | Validate scenarios, fixtures, tool profiles and the rule registry; run no agent. Used by the unit tests and before a run. |
| `--max-budget-usd X` | Per-run budget cap passed to the agent (default in `config.toml`). |

`--record-baseline` with `--scenario` or `--rerun-flagged`, and `--rerun-flagged` with
`--scenario`, stop with exit 2 before any run (research D16).

## Before any run

- Stops with exit 2 and a one-line reason when `claude` is not on `PATH` (inside the VM for
  `devvm`), is not logged in, or its version cannot be read; when the `devvm` launcher cannot start
  its VM; when `--check` fails; or when the pinned model is not set.
- Also stops with exit 2 when the secret scanner, run once on a small clean file, does not exit 0
  (research D15), and, with `--rerun-flagged`, when the baseline's commits or model differ from
  the earlier report's `baseline` block.
- When a selected scenario has a judgement rule, one minimal judge call checks the judge's
  credential with the service (research D16); a failure stops with exit 2 and
  `error: the judge credential was rejected`. When a selected scenario has a `run_shown` turn,
  `/usr/bin/sandbox-exec` is checked too, with or without a judge.
- Prints the plan: scenarios, runs, jobs, launcher, model, estimated number of agent sessions.

## Output

stdout, in this order (research D15). Lines in `code` are fixed formats that tests check:

1. With `--check`: `Scenarios (<n>): <names>`, `Profiles (<n>): <names>`, `Rules (<n>): <ids>`,
   then `check ok`; nothing else.
2. The plan: a `Plan:` line and indented `<key>: <value>` lines. Wording is for people and may
   change.
3. One progress line per finished run, in the order runs finish:
   `<scenario> run <n>: scored` or `<scenario> run <n>: unscored (<reason>)`, where `<reason>` is
   an `unscored_reason` value from data-model.md.
4. `Baseline written: <path>`, only with `--record-baseline`.
5. The path of `report.md`.
6. `Runs: <scored> scored, <unscored> unscored`.
7. `Rules: <ok> ok, <flagged> flagged, <confirmed> confirmed, <cleared> cleared, <insufficient>
   insufficient, <new> new`, always the last line. It counts every rule entry of every scenario,
   automatic and judgement (the exit code counts automatic rules only).

When the evaluation stops (exit 2), stdout may already hold the plan and progress lines but never
the `Runs:` or `Rules:` line, so a script can tell a finished evaluation by its last line.

stderr: `judging <scenario> run <n>` per judge call, `warning: <text>` lines (for example a rerun
flag that could not be decided), and on exit 2 one `error: <one-line reason>`.

Files: `evals/bootstrap-guide/runs/<UTC timestamp>/` with `report.json`, `report.md` and the
per-run directories (data-model.md), ignored by `evals/bootstrap-guide/.gitignore`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Report written; no **automatic** rule is `flagged` or `confirmed` (judgement rules never change the exit code). |
| 1 | Report written; at least one automatic rule is `flagged` or `confirmed`. |
| 2 | No report: setup or validation failed (message on stderr). |

## Safety

- The agent runs in a temporary repository through a launcher (research D4a), with the `gh` shim
  and the git wrapper first on `PATH` and `GIT_ALLOW_PROTOCOL=file`; it never sees this repository
  or real GitHub (research D2, D3, D3a).
- Agent tools follow the scenario's profile (research D4); `run_shown` runs only commands that pass
  the token check, without a shell (research D5).
- Each transcript is scanned for secrets after the run (research D9); a run that cannot be
  scanned is unscored `unscannable` and its files are deleted (research D15).

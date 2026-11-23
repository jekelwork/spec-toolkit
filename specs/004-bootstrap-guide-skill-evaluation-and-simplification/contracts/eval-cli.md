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

## Before any run

- Stops with exit 2 and a one-line reason when `claude` is not on `PATH` (inside the VM for
  `devvm`), is not logged in, or its version cannot be read; when the `devvm` launcher cannot start
  its VM; when `--check` fails; or when the pinned model is not set.
- Prints the plan: scenarios, runs, jobs, launcher, model, estimated number of agent sessions.

## Output

- stdout: one progress line per finished run (`<scenario> run <n>: scored` or `unscored
  (<reason>)`), then the path of `report.md` and a summary line
  `Rules: <ok> ok, <flagged> flagged, <confirmed> confirmed, <insufficient> insufficient`.
- Files: `evals/bootstrap-guide/runs/<UTC timestamp>/` with `report.json`, `report.md` and the
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
- Each transcript is scanned for secrets after the run (research D9).

# Implementation Plan: Bootstrap-guide skill evaluation and simplification

**Branch**: `97-spec-004-create-and-approve-spec-and-plan` | **Date**: 2026-11-23 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/004-bootstrap-guide-skill-evaluation-and-simplification/spec.md`

## Summary

Make the `bootstrap-guide` skill measurable, then more deterministic, then shorter, in three
Phases. Phase 1 adds a repository-only evaluation (`evals/bootstrap-guide/`, never exported) that
replays about 10 scripted bootstrap scenarios through Claude Code in temporary repositories built
from the working tree, with a `gh` shim instead of GitHub and a git wrapper that keeps pushes local,
isolated in a devvm VM (or, if the opening devvm spike fails, a scrubbed local environment) behind
one launcher interface, scores each transcript against named
rules (automatic text checks, a few judged by a second model call), and records a baseline on the
unchanged skill. Phase 2 moves what the agent now assembles by hand into the engine (`--report-exit`
instead of the shell suffix, fenced previews, filled-in skill calls, the list of commands the guide
may run) as additions to the 001 contracts, and shortens the skill to match. Phase 3 splits
`SKILL.md` into a core of at most half the baseline words plus linked reference files, with the
guard tests following each rule. After Phases 2 and 3 the evaluation must show no confirmed
regression against the baseline.

## Technical Context

**Language/Version**: Python 3.11+, standard library only (constitution Technology Baseline).

**Primary Dependencies**: none in code. The evaluation drives the Claude Code CLI (`claude -p`,
stream-json input and output) and, with the devvm launcher, devvm, as external programs on the
maintainer's machine only (research D4, D4a); the secret scan uses the repository's Node scanner.
CI runs only the unit tests, with no agent.

**Storage**: files: TOML scenarios and fixtures, JSON reports and a committed `baseline.json`;
run transcripts git-ignored (data-model.md).

**Testing**: `unittest`: existing `scripts/test_*.py` for the engine and guard tests; new
`evals/bootstrap-guide/test_*.py` for the harness, shim, git wrapper, `run_shown` token check and
scoring on recorded transcripts, run in the new source-only workflow `.github/workflows/evals.yml`
(a required status check).

**Target Platform**: maintainer's macOS or Linux machine with a POSIX shell, Claude Code and
(preferred) devvm with hardware virtualization; GitHub Actions (ubuntu) for unit tests.

**Project Type**: CLI tooling inside the toolkit repository (engine and skill are published; the
evaluation is not).

**Performance Goals**: a full evaluation (≈10 scenarios × 5 runs, 4 parallel) finishes in under 60
minutes (SC-001).

**Constraints**: no real GitHub, network (beyond the agent's model calls) or secrets in a run; agent
tools restricted per scenario profile (research D4, D5); git network access limited to the local
`origin` (research D3a); engine changes are additive to `format_version` 1; published
files stay neutral; the template stays free of evaluation files.

**Scale/Scope**: ~10 scenarios, ~15 rules, ~1,000–1,200 lines of harness Python plus fixtures;
engine changes in `bootstrap.py` (the `--report-exit` handling in its `main`, which covers
`check`, `start` and `github` from `bootstrap_github.py`), `bootstrap_check.py`,
`bootstrap_start.py`, `bootstrap_steps.py`; skill text about 7,000 words today.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design: still passes.*

| Principle | Check | Result |
| --- | --- | --- |
| I. Toolkit Scope | Engine and skill changes serve every adopter. The evaluation is for this repository only and is excluded from the export (research D1), as principle I requires for repository-only features. | Pass |
| II. Spec-Driven Development | Entry 004 is `planned`; spec and plan go in their own PR; tasks are generated on the Phase 1 branch after merge. | Pass |
| III. Traceable Work Tracking | Three Phases → three Phase issues via the roadmap sync; no `T###` issues. | Pass |
| IV. Quality Gate | Harness scoring is unit-tested on recorded transcripts before the harness is used (tests first); each engine addition gets a failing test first; guard tests follow moved rules and are shown failing with the rule removed; skill text changes are verified by the reference test and the evaluation report in the PR. | Pass |
| V. Security | No new secret. A new source-only workflow (`evals.yml`, `pull_request` trigger, `permissions: contents: read`, no secrets, pinned actions); adding a workflow and trigger needs the `security-auditor` pass anyway. The harness runs an agent with a Bash tool on hostile fixture text, so the Phase 1 PR's auditor pass covers: launcher isolation (devvm or scrubbed environment), tool profiles and read-deny rules, the git wrapper and `GIT_ALLOW_PROTOCOL=file`, the `run_shown` token check without a shell, and the transcript secret scan. | Pass (auditor in Phase 1) |
| VI. Neutrality | Evaluation not published; engine output and skill text name no adopter; fixtures use made-up names. | Pass |
| VII. Git Workflow | Issue-backed branches, maintainer-approved commits, 0 approvals. | Pass |
| Technology Baseline | Stdlib Python only. Claude Code (and devvm if the spike adopts it) becomes maintainer tooling outside CI, which the baseline's list ("uv … and `npx skills`") does not name yet; a Phase 1 task asks the maintainer to amend it (research D14). | Pass with note |

## Project Structure

### Documentation (this feature)

```text
specs/004-bootstrap-guide-skill-evaluation-and-simplification/
├── spec.md
├── plan.md              # This file
├── research.md          # D1–D14
├── data-model.md
├── quickstart.md        # QS-1–QS-14
├── contracts/
│   ├── eval-cli.md
│   ├── eval-report.md
│   ├── check-json-changes.md
│   └── start-cli-changes.md
├── checklists/requirements.md
└── tasks.md             # Generated on the Phase 1 branch after this PR merges
```

### Source Code (repository root)

```text
evals/bootstrap-guide/          # Phase 1; excluded from the export (evals/**)
├── run.py                      # CLI (contracts/eval-cli.md)
├── config.toml                 # pinned model, budget cap, defaults
├── harness.py                  # temp repo from the working tree or a ref, scripted turns
├── launchers.py                # launcher interface, DevvmLauncher, LocalLauncher (research D4a)
├── profiles.py                 # tool profiles and permission settings (research D4)
├── gh_shim.py                  # the gh stand-in (research D3), installed as `gh` on PATH
├── git_wrapper.py              # keeps git network access local (research D3a)
├── transcript.py               # stream-json → normalized transcript
├── rules.py                    # rule registry, automatic checks, judgement rubrics
├── report.py                   # aggregation, baseline comparison, report.md
├── baseline.json               # committed baseline
├── scenarios/*.toml
├── fixtures/<state>/{files/,gh.toml,git.toml}
├── testdata/transcripts/*.jsonl  # recorded, trimmed transcripts for unit tests
├── test_*.py                   # includes test_evals_workflow.py (guard for evals.yml)
├── .gitignore                  # runs/
└── runs/                       # ignored output

scripts/
├── bootstrap.py, bootstrap_check.py, bootstrap_start.py, bootstrap_steps.py   # Phase 2
├── test_bootstrap*.py                                                        # Phase 2 tests
├── skill_text_helpers.py       # Phase 3: rule → file map for guard tests
├── test_bootstrap_guide_skill.py, test_bootstrap_guide_safety.py            # Phases 2–3
├── toolkit-export.toml         # Phase 1: exclude evals/** and .github/workflows/evals.yml
├── test_export_toolkit.py      # Phase 1: neither is ever exported
└── test_fresh_export.py        # Phase 1: both in EXCLUDED_FILES

.agents/skills/bootstrap-guide/
├── SKILL.md                    # Phases 2–3
└── references/*.md             # Phase 3 (research D11)

.github/workflows/evals.yml            # Phase 1: source-only, evals unit tests, required check, no paths filter
docs/publishing.md                     # Phase 1: names the new required check
docs/bootstrap.md                      # Phase 2: --report-exit, fenced preview, new fields
specs/001-guided-bootstrap/contracts/  # untouched; 004's contracts/ record the changes
```

**Structure Decision**: the evaluation is a self-contained top-level `evals/` area so one
exclude pattern keeps it out of the template; engine and skill changes stay where they are.

## Phases

### Phase 1: Evaluation and baseline (US1, P1)

1. devvm spike, time-boxed to one day: the five checks (research D4a); the result decides the
   default launcher.
2. Spike of the agent run with the chosen launcher: skill discovery, tool profiles, the `echo`
   suffix rule, no user settings loaded, shim and git wrapper in effect (QS-2).
3. Export exclusion of `evals/**` and `evals.yml`, its test, `test_fresh_export.py` and
   `docs/publishing.md`; `evals/bootstrap-guide/.gitignore`.
4. Tests first, then: transcript normalization, rule checks on recorded transcripts (one passing
   and one violating transcript per automatic rule), regression rule boundaries, `run_shown` token
   check, `gh` shim, git wrapper, report and baseline comparison with per-scenario hashes and rule
   versions, scenario/fixture validation (`--check`).
5. Harness: temporary repository from the working tree or `--toolkit-ref`, launchers, scripted
   turns with expectations, parallel runs, budget and timeout, unscored reasons, transcript secret
   scan. Plan update after T041 (research D15): a run whose files cannot be scanned is unscored
   `unscannable` instead of stopping the evaluation, a scanner probe runs before any run, a
   `--rerun-flagged` report records each flag's outcome, and the stdout lines are defined in
   contracts/eval-cli.md. Plan update after T048 (research D16): a scenario turn can mark a
   user's "done" so its reply is judged, a fixture can start on a checked-out branch so the merge
   step is reachable, and the judge credential and the sandbox are checked before any paid
   session. Plan update before T049 (research D17): the agent runs on Sonnet 5.5 at medium
   effort and the judge on Opus 5.5, each pinned in `config.toml` and recorded in the report;
   `--rerun-flagged` stops when the config's settings differ from the earlier report's.
   Plan update before T049 (research D18): no profile has `../**` deny rules, which Claude Code
   turned into sandbox write blocks over the run directory; Read, Grep and Glob are allowed only
   inside the repository (`Read(./**)`); and the integrity check accepts
   Claude Code's own empty `.claude/.cc-writes` directory. Plan update before T049 (research
   D19): Write and Edit allow only the files the skills write, the git wrapper uses only the
   run's repository and refuses git's input options, the integrity check covers nested `.git`
   and the skills' scripts, and the call log is tamper-evident. Plan update after the first T050
   run (research D20): a scenario may have the harness save the coding-agent choice before the
   run, because Claude Code's sandbox refuses the agent's write to `.git/config`.
6. Scenarios and fixtures for the FR-003 list, including the stub engine for the
   higher-format-version scenario; judgement rubrics.
7. Source-only workflow `evals.yml` and its guard test; the maintainer makes its job a required
   status check.
8. Record the baseline from the Phase 1 branch with `--toolkit-ref origin/main` (QS-3) and the sensitivity checks (QS-4,
   QS-5); `security-auditor` pass; the constitution note for the maintainer (research D14); report
   in the PR.

Phase 1 changes neither `SKILL.md` nor `scripts/bootstrap*`, and the baseline reads them from
`origin/main` when it is recorded.

### Phase 2: Deterministic steps in the engine (US2, P2)

1. Tests first, then `--report-exit` on `check` (human and `--json`), `start` and `github`,
   including the usage-error cases (research D10 item 1). The handling must cover both places
   that catch errors: `bootstrap.py` `main` (which returns 2 for some `start` and `github` errors)
   and `bootstrap_start.main`, so no handled error ends without an `exit` line. Add
   `--report-exit` to the hand-written option list of the help-only `start` subparser in
   `bootstrap.py`.
2. Fenced preview under `--no-input`, digest unchanged.
3. `commands` and `steps[].skill_call` in `check --json`, with the section-list test against
   `docs/bootstrap.md`.
4. `docs/bootstrap.md` sections for the new option and fields.
5. Skill edit: drop the shell-suffix, fence-building, skill-call template and command-list
   assembly rules in favour of the engine output; guard tests follow (FR-016).
6. Evaluation rules and fixtures follow the engine: `report-exit-on-own-engine-command` replaces
   `suffix-on-own-engine-command`; stale fixture tables updated (only their scenarios become
   `new`).
7. Evaluation against the baseline (QS-10), report in the PR.

### Phase 3: Skill split (US3, P3)

1. Tests first: rule → file map in `skill_text_helpers.py`, guard tests reading the mapped file,
   link test for the reference files, export/sync test for `references/`.
2. Move sections into `references/` per research D11; core links each where it is needed.
3. Size check (QS-11) and evaluation (QS-14), report in the PR.

## Complexity Tracking

| Item | Why needed | Simpler alternative rejected because |
| --- | --- | --- |
| Claude Code CLI as maintainer tooling (Technology Baseline lists only `uv` and `npx skills`) | The evaluation must replay scenarios through a real coding agent (spec FR-001, clarified: Claude Code first). | No agent-free way measures whether the agent follows the skill. A Phase 1 task asks the maintainer to amend the list through `speckit-constitution` (research D14). |
| devvm as preferred isolation | The user chose devvm first (spec clarification 2026-11-23): a VM isolates the agent from the maintainer's files and credentials better than settings can. | The light local setup alone is weaker; it stays as the fallback behind the same launcher interface, so a failed spike costs about half a day. |
| A top-level `evals/` directory | One exclude pattern keeps all evaluation files out of the template (FR-023). | Under `scripts/` every file would need its own exclude, and a forgotten one would publish repository-only tooling. |

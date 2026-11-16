# Implementation Plan: Guided bootstrap

**Branch**: `4-spec-001-create-and-approve-spec-and-plan` | **Date**: 2026-11-15 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/001-guided-bootstrap/spec.md`

## Summary

Adopters reach a merged bootstrap pull request and an applied first roadmap sync by running one
resumable command, or by talking to their coding agent. Phase 1 adds a step model to the existing
`scripts/bootstrap.py` engine: eleven ordered steps whose state is read from the repository and
GitHub on every run (nothing is stored; once the bootstrap files are on the default branch, the
earlier steps count as done), exposed as `check --json` (versioned, read-only) and driven by a new
`start` command that previews and confirms every change that bypasses review, and every commit,
push and pull request.
Phase 2 adds a `bootstrap-guide` skill that reads only `check --json` and `docs/bootstrap.md`, runs
`start --confirm <step>` after the user's yes, and never handles a token. The existing `check`,
`init` and `github` commands keep their behaviour and exit codes.

## Technical Context

**Language/Version**: Python 3.11+, standard library only; skill text in Markdown

**Primary Dependencies**: `gh` CLI (REST and GraphQL) and `git`, called through the engine's
injected `run` function

**Storage**: None added. State is read from files, Git, and GitHub; answers live in
`project-context.toml` once written

**Testing**: `unittest` with the existing injected `run` fakes (`FakeGitHub` in
`scripts/test_bootstrap_github.py`); a docs-versus-engine test for `check --json`; a reference
test for the skill; pilot walkthroughs WS-1 to WS-9 (spec FR-026a)

**Target Platform**: macOS and Linux terminals with `gh` logged in; coding agents that read
`.agents/skills/` (Claude Code, Codex, Copilot)

**Project Type**: CLI scripts plus an agent skill, published through the template

**Performance Goals**: `check --json` finishes in under 30 seconds on a normal connection (roughly
15 to 25 `gh` calls), and in under 60 seconds once bootstrap is merged, when it also runs the
first-sync dry run (research D5)

**Constraints**: no new dependencies; no secrets read or printed; non-interactive use never hangs;
published files stay neutral (constitution VI)

**Scale/Scope**: one repository per run; 11 steps; about 4 new script and test modules, 1 skill, 2
docs updated

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design: still passes.*

| Principle | Status | How |
| --- | --- | --- |
| I. Toolkit scope | Pass | Serves every adopter; verified on a scratch pilot repository, not a dev environment |
| II. Spec-driven development | Pass | Planned entry 001; spec and plan in their own PR; tasks on the Phase 1 branch |
| III. Traceable work tracking | Pass | Two Phases (engine, skill), one issue each; no task issues |
| IV. Quality gate | Pass | Test-first for every engine behaviour; guard tests shown failing; skill covered by reference test and walkthroughs recorded in the PR |
| V. Security | Pass, with a required audit | Credential check reads secret names only; a pasted token stops the guide. Both Phase PRs touch token handling, so each needs a `security-auditor` pass, which also reviews the `--confirm` trust model (research D4) |
| VI. Neutrality | Pass | No adopter, maintainer, or this repository's issues in the engine, skill or docs; the bootstrap issue title is generic |
| VII. Git workflow | Pass | The engine commits, pushes and opens the PR only after an explicit yes showing the files and message |
| Technology baseline | Pass | Python stdlib, `gh`, `git`; no Node or third-party packages |

No violations, so Complexity Tracking is empty.

## Project Structure

### Documentation (this feature)

```text
specs/001-guided-bootstrap/
├── plan.md              # This file
├── research.md          # Decisions D1–D12
├── data-model.md        # Step, status report, step catalogue
├── quickstart.md        # Validation guide and walkthroughs WS-1..WS-9
├── contracts/
│   ├── check-json.md    # `bootstrap.py check --json` format, version 1
│   └── start-cli.md     # `bootstrap.py start` options, prompts and exit codes
└── tasks.md             # Generated later, on the Phase 1 branch
```

### Source Code (repository root)

```text
scripts/
├── bootstrap.py                  # + `start` subcommand, + `check --json` flag
├── bootstrap_steps.py            # NEW (Phase 1): step catalogue, state detection, JSON report
├── bootstrap_start.py            # NEW (Phase 1): the start driver: preview, confirm, act
├── bootstrap_check.py            # unchanged human report; its check_* functions are reused
├── bootstrap_github.py           # + load_board_workflows (own query; BOARD_QUERY unchanged)
├── test_bootstrap_steps.py       # NEW (Phase 1)
├── test_bootstrap_start.py       # NEW (Phase 1)
├── test_bootstrap_guide_skill.py # NEW (Phase 2): reference test
└── README.md                     # bootstrap.py section: start, check --json
.agents/skills/bootstrap-guide/
└── SKILL.md                      # NEW (Phase 2)
docs/
└── bootstrap.md                  # guided path, step anchors, machine-readable status section
```

**Structure Decision**: Extend the existing flat `scripts/` layout with two modules next to
`bootstrap_check.py` and `bootstrap_github.py`, following their pattern (an injected `run`, and an
`out` callback for output). The skill lives once in `.agents/skills/`, like every other skill.

### Phase split

- **Phase 1 (engine)**: User Stories 1 and 2; FR-001 to FR-014, FR-025 (the `start` and
  `check --json` parts of the docs, including the "Machine-readable status" section, the `--confirm`
  trust model, and both first-sync preview paths), FR-026a (walkthroughs WS-1, WS-2, WS-3, WS-8 and
  WS-9), FR-027.
  - It opens with a verification task on the pilot (quickstart: board workflows field and names,
    files changed by the constitution and roadmap skills). The fake's fixtures and guard tests are
    written from those results.
  - `test_bootstrap_steps.py` includes a docs-versus-engine test: every field and step ID documented
    in `docs/bootstrap.md` appears in the engine's output, and the reverse, and the documented
    `format_version` equals the engine's. This is US2's independent test.
- **Phase 2 (skill)**: User Stories 3 and 4; FR-015 to FR-024, FR-025 (the guided-path part),
  FR-026 (reference test), FR-026a (walkthroughs WS-4 to WS-8).

### Revision after the pre-PR review (2026-11-16)

The `code-reviewer` findings on the first version of this plan are resolved in the design files:
detection after the merge and through GitHub instead of `origin/main` (research D2); first-sync
done-when, preview arguments and run selection (D5); a separate board-workflows query (D6); the
pull request file set (D7); scope fallback (D10); unreadable blocking steps (D11); `start` past
non-blocking steps (D12); the `needs_confirmation` and `blocking` fields (data-model.md); exit 4 for
a stale `--confirm` (contracts/start-cli.md); the Phase split above; WS-2 corrected and WS-9 added
(quickstart.md).

## Complexity Tracking

No constitution violations.

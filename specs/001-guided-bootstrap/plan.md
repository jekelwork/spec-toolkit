# Implementation Plan: Guided bootstrap

**Branch**: `4-spec-001-create-and-approve-spec-and-plan` | **Date**: 2026-11-15 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/001-guided-bootstrap/spec.md`

## Summary

Adopters reach a merged bootstrap pull request and an applied first roadmap sync by running one
resumable command, or by talking to their coding agent. Phase 1 adds a step model to the existing
`scripts/bootstrap.py` engine: eleven ordered steps whose state is read from the repository and
GitHub on every run (nothing is stored; once the bootstrap files are on the default branch, the
earlier steps count as done, and `project-context.toml` is validated there), exposed as
`check --json` (versioned, read-only) and driven by a new `start` command that previews and
confirms every change that bypasses review, and every commit, push and pull request.
Phase 2 adds a `bootstrap-guide` skill that reads the state only from `check --json` and
`docs/bootstrap.md`, shows the preview from `start --no-input`, runs
`start --no-input --confirm <step>@<digest>` with that preview's `confirm:` value after the user's
yes, and never handles a token. The existing `check`,
`init` and `github` commands keep their behaviour and exit codes.

## Technical Context

**Language/Version**: Python 3.11+, standard library only; skill text in Markdown

**Primary Dependencies**: `gh` CLI (REST and GraphQL) and `git`, called through the engine's
injected `run` function

**Storage**: None added. State is read from files, Git, and GitHub; answers live in
`project-context.toml` once written

**Testing**: `unittest` with the existing injected `run` fakes (`FakeGitHub` in
`scripts/test_bootstrap_github.py`); a docs-versus-engine test for `check --json`; a reference
test for the skill; pilot walkthroughs WS-1 to WS-8 from spec FR-026a, plus WS-9 to WS-12

**Target Platform**: macOS and Linux terminals with `gh` logged in; coding agents that read
`.agents/skills/` (Claude Code, Codex, Copilot)

**Project Type**: CLI scripts plus an agent skill, published through the template

**Performance Goals**: `check --json` finishes in under 30 seconds on a normal connection (roughly
15 to 25 `gh` calls), and in under 60 seconds once bootstrap is merged, when it also runs the
first-sync dry run (research D5). Under a GitHub rate limit it can take minutes, because the
reused sync code retries up to 5 times with 60-second waits

**Constraints**: no new dependencies; no secrets read or printed; non-interactive use never hangs;
published files stay neutral (constitution VI)

**Scale/Scope**: one repository per run; 11 steps; about 4 new script and test modules, 1 skill, 3
docs updated (`docs/bootstrap.md`, `scripts/README.md`, the `AGENTS.md` index)

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design: still passes.*

| Principle | Status | How |
| --- | --- | --- |
| I. Toolkit scope | Pass | Serves every adopter; verified on a scratch pilot repository, not a dev environment |
| II. Spec-driven development | Pass | Planned entry 001; spec and plan in their own PR; tasks on the Phase 1 branch |
| III. Traceable work tracking | Pass | Two Phases (engine, skill), one issue each; no task issues |
| IV. Quality gate | Pass | Test-first for every engine behaviour; guard tests shown failing; skill covered by reference test and walkthroughs recorded in the PR |
| V. Security | Pass, with a required audit | Credential check reads secret names only; a pasted token stops the guide. Both Phase PRs touch token handling, so each needs a `security-auditor` pass, which also reviews the `--confirm` trust model and preview digest (research D4) |
| VI. Neutrality | Pass | No adopter, maintainer, or this repository's issues in the engine, skill or docs; the bootstrap issue title is generic |
| VII. Git workflow | Pass | The engine commits, pushes and opens the PR only after an explicit yes showing the files and message. In "do it for me" mode, the user's yes in the chat before `start --no-input --confirm pull_request@<digest>` is that approval |
| Technology baseline | Pass | Python stdlib, `gh`, `git`; no Node or third-party packages |

No violations, so Complexity Tracking is empty.

## Project Structure

### Documentation (this feature)

```text
specs/001-guided-bootstrap/
├── plan.md              # This file
├── research.md          # Decisions D1–D14
├── data-model.md        # Step, status report, step catalogue
├── quickstart.md        # Validation guide and walkthroughs WS-1..WS-12
├── contracts/
│   ├── check-json.md    # `bootstrap.py check --json` format, version 1
│   └── start-cli.md     # `bootstrap.py start` options, prompts and exit codes
└── tasks.md             # Generated later, on the Phase 1 branch
```

### Source Code (repository root)

```text
scripts/
├── bootstrap.py                  # + `start` subcommand (`--no-input`, `--confirm STEP@DIGEST`), + `check --json` flag
├── bootstrap_steps.py            # NEW (Phase 1): step catalogue, state detection, JSON report
├── bootstrap_start.py            # NEW (Phase 1): the start driver: preview, confirm, act
├── bootstrap_check.py            # unchanged human report; its check_* functions are reused
├── bootstrap_github.py           # + load_board_workflows (own query; BOARD_QUERY unchanged)
├── project_context.py            # + parse_context(text); load_context(path) wraps it, unchanged
├── test_bootstrap_steps.py       # NEW (Phase 1)
├── test_bootstrap_start.py       # NEW (Phase 1)
├── test_bootstrap_guide_skill.py # NEW (Phase 2): reference test
└── README.md                     # bootstrap.py section: start, check --json
.agents/skills/bootstrap-guide/
└── SKILL.md                      # NEW (Phase 2)
docs/
└── bootstrap.md                  # guided path, step anchors, machine-readable status section
AGENTS.md                         # "Where to find what": bootstrap row names `start` and
                                  # `check --json` (Phase 1) and the `bootstrap-guide` skill (Phase 2);
                                  # guarded by scripts/test_agents_index.py
```

**Structure Decision**: Extend the existing flat `scripts/` layout with two modules next to
`bootstrap_check.py` and `bootstrap_github.py`, following their pattern (an injected `run`, and an
`out` callback for output). The skill lives once in `.agents/skills/`, like every other skill.

### Phase split

- **Phase 1 (engine)**: User Stories 1 and 2; FR-001 to FR-014, FR-025 (the `start` and
  `check --json` parts of the docs, including the "Machine-readable status" section, the `--confirm`
  trust model with its limits (it guards against mistakes, not against someone with the GitHub
  rights), a note that the toolkit repository itself reads as bootstrapped, and both first-sync
  preview paths), FR-026a (walkthroughs WS-1, WS-2, WS-3 and WS-8,
  plus WS-9 to WS-11), FR-027; the `AGENTS.md` index row for `start` and `check --json`.
  - It opens with a verification task on the pilot (quickstart: board workflows field and names,
    files changed by the constitution and roadmap skills, the ruleset GET on a free private
    repository). The fake's fixtures and guard tests are
    written from those results.
  - `test_bootstrap_steps.py` includes a docs-versus-engine test: every field and step ID documented
    in `docs/bootstrap.md` appears in the engine's output, and the reverse, and the documented
    `format_version` equals the engine's. This is US2's independent test.
  - Each state rule in research D2, D3, D4, D5, D7, D10, D11, D12, D13 and D14 gets a fake-run test
    written before it is implemented. These include the `--confirm` rule with `next_step` on a
    user step, `--no-input` never prompting, no file written when `--required-approvals` is
    missing without a terminal, and a digest mismatch (a file, `HEAD` or a tip changed after the
    preview) exiting 4 with no write command run. For each of the 5 confirmable steps, a positive
    test shows that, with nothing changed in the fake, the digest printed with exit 3 is accepted
    by `--confirm` and the step acts. `first_sync` is tested with and without board access. The
    `run` fake asserts that no write command runs before a confirmation (SC-003).
- **Phase 2 (skill)**: User Stories 3 and 4; FR-015 to FR-024, FR-025 (the guided-path part),
  FR-026 (reference test), FR-026a (walkthroughs WS-4 to WS-8, plus WS-12), FR-027; the
  `AGENTS.md` index entry for the skill. The skill follows research D4's rules for the digest: it
  takes the `confirm:` value only from `start`'s exit-3 output, and on exit 4 reruns
  `start --no-input`, shows its new preview and asks again.

### Revision after the pre-PR review (2026-11-16)

The `code-reviewer` findings on the first version of this plan are resolved in the design files:
detection after the merge and through GitHub instead of `origin/main` (research D2); first-sync
done-when, preview arguments and run selection (D5); a separate board-workflows query (D6); the
pull request file set (D7); scope fallback (D10); unreadable blocking steps (D11); `start` past
non-blocking steps (D12); the `needs_confirmation` and `blocking` fields (data-model.md); exit 4 for
a stale `--confirm` (contracts/start-cli.md); the Phase split above; WS-2 corrected and WS-9 added
(quickstart.md).

### Revision after the second pre-PR review (2026-11-16)

The second `code-reviewer` pass (issue #4, items S1–S11 and Optional) is resolved as follows:
- **Design decisions**: the `merge` and `pull_request` states before and after a PR (research D2);
  validating the default branch's `project-context.toml` content (D2); finding hand-made issues and
  their branches, with checkout offered as a confirmed action (D3); `--confirm` bound to `next_step`
  and the new `--no-input` (D4).
- **First sync**: "done" once its issues exist, with a preview only from the clean default branch
  tip (D5).
- **`pull_request` and `start`**: the step's partial states (D7); where `start` stops (D12).
- **New decisions**: no default for decision values (D13); the visibility and plan warning (D14).
- **Also**: the detected default branch throughout; WS-9 corrected and WS-10 added; the CI check
  list in quickstart.md; the `AGENTS.md` index.

The third review's Required items are fixed: several issue candidates are asked about again, as
the spec requires, with `--issue N` for non-interactive use (D3); `first_sync` reopens for a newly
planned entry (D5); the visibility warning probes the rulesets with a GET (D14); and the
`pull_request` state after a merge without the files (D2).

Its Optional items and Nits are also resolved: the per-run `performable` field (D4), the
`parse_context` split and the default branch's approval count for `github_settings` (D2), reuse of
the sync's issue lookups (D5), non-blocking `not_checked` steps (D12), checkout failures (D3),
`ruleset_warning` on `github_settings` (D14), and the contract wording.

The fourth review added `details.candidates` (D3) and these clarifications: follow-up commits to
an open PR (D2, D7); the reused sync loader and lookups, `SystemExit` handling and the spec
directory from the default branch (D5); `performable` for the remaining stops and one rule for "behind"
(D4, D12; the compare API's `behind_by`, tried in that round, was replaced by a local
`merge-base` check after the fifth review); the 404 mapping (D14); and the contract edge cases.

### Revision after the outside review of D2 and D4 (2026-11-16)

Accepted by the maintainer and recorded on issue #4:
- **D4**: the confirmation is bound to the preview by a digest (`--confirm STEP@DIGEST`, exit 4 on
  a mismatch). This replaces the earlier deferral of that item to `security-auditor`. D4 also
  states that FR-010 is met, and the trust model's limits for docs/bootstrap.md.
- **D2**: `pull_request` compares local `HEAD` with the PR's head commit instead of
  `origin/<branch>`, and a pointer row for the board steps.
- **New walkthroughs**: WS-11 (engine) and WS-12 (guide, exit 4 after a yes).
- **After its review**: the digest is defined exactly (inputs, preview block, encoding),
  `--confirm` checks run in a fixed order, the `pull_request` head-commit stop appears in D4, D12
  and the contract, and the guide never confirms a digest the user has not seen. Unpushed commits
  are detected without a PR against the branch tip on GitHub (D7), and `pull_request` on another
  branch asks for a checkout only when local work would otherwise be left out (D2). A bare
  `--confirm STEP` exits 2, `--issue` has its own check (exit 3), and `project_context` needs its
  `init` flags on both the preview run and the confirming run (D13).

Not taken: refusing pseudo-terminals (not reliably detectable), and treating SC-003 as untestable
(the `run` fake can assert that no write runs before a confirmation).

### Revision after the Phase 2 pilot (2026-11-18)

FR-006a (clarification session 2026-11-18): the `pull_request` step removes a well-formed leading
Sync Impact Report comment from the constitution when it commits it, and its preview says so
(research D7, contracts/start-cli.md). It is a test-first engine change in
`scripts/bootstrap_start.py` with fake-run tests in `scripts/test_bootstrap_start.py`: the match
and its non-matches, the preview line and digest, the removal before `git add`, and no change to an
already committed constitution. `docs/bootstrap.md` (Constitution, One bootstrap pull request) and
the `bootstrap-guide` skill say the step removes it, so the user is not asked separately, and the
review is not rerun for it. The Constitution Check is unchanged: IV is met by the tests, VII by the
existing confirmation, which now covers the removal.

FR-009, FR-010a to FR-010f and FR-023 (clarification session 2026-11-18, issue #51): clearer
`start` output (research D16, contracts/start-cli.md). Test-first engine changes in
`scripts/bootstrap_start.py`, `scripts/bootstrap_steps.py` and `scripts/bootstrap_github.py`, with
fake-run tests in the matching `scripts/test_bootstrap_*.py` files:

- progress lines on stderr (an injected `Env.progress`, so tests assert them and stdout stays
  byte-identical), and the first sync polled with `gh run view` instead of `gh run watch` so the
  "still running" lines run on the injected clock;
- the `Done:` / `Next step (not done yet):` / `To do it, run:` layout, with the full confirming
  command built from the parsed arguments;
- `--guided` (requires `--confirm`, performs one step, ends with the back-to-the-agent line);
- exit 1 with a stdout line for missing init flags outside a `--confirm project_context` run;
- the completion line (bold only when stdout is a terminal and `NO_COLOR` is unset);
- `git switch` and `git pull --ff-only origin <default>` in every printed or run command;
- the board workflow: still `done` when enabled, with a title that says to check the Status (the
  API cannot read it), and the `github` preview's manual line reworded to "check or set". The
  `action` of a `done` step stays null (data-model.md), so `format_version` stays 1.

The `github` command's manual line changes wording only; its actions and exit codes stay as
FR-014 requires (clarification session 2026-11-18).

`docs/bootstrap.md` and the `bootstrap-guide` skill (the confirming command, `--guided` in "guide
me" mode, the exact-match exception for the checkout-and-pull command, the board reminder once)
change with it, and their reference and safety tests are updated first. The Constitution Check is
unchanged: IV is met by the tests and the next pilot walkthrough; VII by the existing
confirmations, which `--guided` does not bypass (it still requires `--confirm` with the digest).

The `--confirm` trust model and the digest go to the `security-auditor` pass for both Phases.

### Revision during Phase 1 implementation (2026-11-16)

Accepted by the maintainer on issue #5: with several bootstrap-issue candidates, the one whose
branch is checked out is the choice and `bootstrap_issue` is `done` (D3), so `start --issue N`
does not loop; `prerequisites` reads `gh auth status --active` (gh 2.40+) (D10).

### Revision after the Phase 1 reviews and security pass (2026-11-17)

Accepted by the maintainer on issue #5 during the `code-reviewer` rounds on T032–T052, the
`security-auditor` pass (T057) and the pilot walkthroughs (T054):
- **D5**: the apply run is found by run IDs listed before the dispatch, not a clock window; the
  dry run needs a clean checkout of every input the workflow reads (six paths) and an `origin` the
  sync reads; `--skip-project` whenever the board credential is not `done`, with a "not previewed"
  note; `--repo` on every `gh` command.
- **New D15**: `bootstrap_issue` and `pull_request` (fetch and every push URL) check that `origin`
  names the repository `gh` uses; one shared check for `start` and `check --json`; shell-quoted
  branch names, restricted bootstrap branch names, escaped output, masked URL credentials, and
  type-ahead discarded before each prompt.
- **D4**: previews carry a `Repository:` line that binds the digest; the trust-model wording ("does
  not show that anyone saw it") and two documented limits; the known `performable` limit.
- **D3, D12, D13 and contracts/start-cli.md**: the full-match branch pattern; `--confirm` with
  several candidates, or a non-candidate `--issue`, lists and exits 3 in a terminal too; check 5b
  (an invalid `init` value exits 2 before any preview); `--owner` when there is no GitHub `origin`;
  the `github_settings` stop for a local `project-context.toml` that differs after the merge.

No spec requirement changes: these refine how FR-003, FR-004, FR-006, FR-007, FR-010 and FR-011
are met.

## Complexity Tracking

No constitution violations.

---
description: "Task list for Guided bootstrap"
---

# Tasks: Guided bootstrap

**Input**: Design documents from `specs/001-guided-bootstrap/` (plan.md, spec.md, research.md
D1–D14, data-model.md, contracts/check-json.md, contracts/start-cli.md, quickstart.md)

**Tests**: Required. Constitution IV (quality gate) applies: every behaviour change has a test task
written before its implementation task, and the test is shown failing before the implementation
makes it pass. Tests use `unittest` and the injected `run` fakes (`FakeGitHub` in
`scripts/test_bootstrap_github.py`); no test calls the real `gh` or `git` remote.

**Organization**: Each `## Phase` is one GitHub Phase issue (plan.md "Phase split"). Phase 1 is the
engine (User Stories 1 and 2), Phase 2 is the guide skill (User Stories 3 and 4). User stories are
subsections inside their Phase. User Story 2 comes before User Story 1 because `start` is built on
the step model that `check --json` reports.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an unfinished task)
- **[Story]**: The user story the task serves (US1–US4); setup and shared tasks have none
- Paths are relative to the repository root

---

## Phase 1: Engine — resumable `start` and machine-readable `check --json`

**Goal**: An adopter reaches a merged bootstrap pull request and an applied first roadmap sync by
rerunning `python3 scripts/bootstrap.py start`, and any caller can read the whole state from
`python3 scripts/bootstrap.py check --json` (User Stories 1 and 2; FR-001–FR-014, FR-025 for
`start` and `check --json`, FR-026a WS-1, WS-2, WS-3, WS-8 to WS-11, FR-027).

**Checkpoint**: All Phase 1 tests pass, the quickstart automated checks pass, walkthroughs WS-1,
WS-2, WS-3 and WS-8 to WS-11 and the manual-path walkthrough are recorded in the Phase pull
request, the `security-auditor` pass has no open Critical or High finding, and the human `check`,
`init` and `github` commands behave and exit as before.

### Setup and pilot verification

- [x] T001 On a scratch pilot repository created from the template (quickstart.md "Pilot setup"), record as a comment on the Phase 1 issue, and copy into the Phase pull request description when it is opened: whether `projectV2(number) { workflows(first: 20) { nodes { name enabled } } }` is readable with the `project` scope and the exact names "Pull request linked to issue" and "Auto-add sub-issues to project" (research D6); which files the `speckit-constitution` and `roadmap-write` skills create or change (research D7); and what `gh api repos/{repo}/rulesets?includes_parents=false&per_page=100` returns for a private repository on GitHub Free (research D14). Later fixtures and the bootstrap file set constant are written from these results.
- [x] T002 Create empty modules `scripts/bootstrap_steps.py` and `scripts/bootstrap_start.py` and empty test modules `scripts/test_bootstrap_steps.py` and `scripts/test_bootstrap_start.py`, following the pattern of `scripts/bootstrap_check.py` (an injected `run` and an `out` callback); confirm `python3 -m unittest discover -s scripts -p 'test_*.py'` still passes.

### Shared foundations (block both stories)

- [x] T003 [P] Write failing tests in `scripts/test_project_context.py` for a new `parse_context(text)` that applies exactly the validation of `load_context(path)` (including `MISSING_REVIEW_MESSAGE` for a missing `[review] required_approvals`) and for `load_context(path)` delegating to it with unchanged behaviour (research D2).
- [x] T004 Implement `parse_context(text)` in `scripts/project_context.py` and make `load_context(path)` a thin wrapper; existing callers and tests unchanged (depends on T003).
- [x] T005 Write failing tests in `scripts/test_bootstrap_github.py` for `load_board_workflows(run, owner, number)`: its own GraphQL query (`BOARD_QUERY` and `load_board` unchanged), returning `{name: True|False|None}` for "Pull request linked to issue" and "Auto-add sub-issues to project", `None` for a missing name, and all `None` on any query error, using fixtures from T001 (research D6) (depends on T001).
- [x] T006 Implement `load_board_workflows` in `scripts/bootstrap_github.py` (depends on T005).
- [x] T007 Write failing tests in `scripts/test_bootstrap_steps.py` for the step catalogue: exactly 11 steps in the fixed order `prerequisites`, `bootstrap_issue`, `project_context`, `constitution`, `roadmap`, `github_settings`, `board_credential`, `board_workflows`, `pull_request`, `merge`, `first_sync`, with the fixed per-step `blocking`, `actor`, `needs_decision`, `needs_confirmation` and `bypasses_review` values of the data-model.md catalogue table; `state` limited to "`done`, `todo`, `manual`, `optional`, `waiting`, `not_checked`" and `manual`/`optional` only on non-blocking steps; `performable` always false when `needs_confirmation` is false.
- [x] T008 Implement the `Step` structure and `CATALOGUE` in `scripts/bootstrap_steps.py` (depends on T007).
- [x] T009 Write failing tests in `scripts/test_bootstrap_steps.py` for the repository context read once per run (research D2): default branch from `gh repo view --json defaultBranchRef`; `bootstrap_merged` true only when `project-context.toml`, `.specify/memory/constitution.md` and `ROADMAP.md` all exist on `<default>` via `gh api repos/{repo}/contents/<path>?ref=<default>`; a 404 means "not present" and makes `bootstrap_merged` false even if another read failed; otherwise any other error gives `bootstrap_merged: null`; default-branch tip via `gh api repos/{repo}/branches/<default> --jq .commit.sha`; no `git fetch` is ever run.
- [x] T010 Implement the repository context reader in `scripts/bootstrap_steps.py` (depends on T008, T009).

### User Story 2 — Machine-readable bootstrap status (Priority: P1)

**Independent test**: `check --json` on fake repositories in known states (fresh, partly
bootstrapped, finished, `gh` logged out) matches contracts/check-json.md, and the docs-versus-engine
test fails when a documented field or step ID is missing on either side.

- [x] T011 [US2] Write failing tests in `scripts/test_bootstrap_steps.py` for `prerequisites` (research D10): `done` when `gh auth status` succeeds and agent files are up to date; `details.project_scope` is `listed`, `missing` or `not_readable`; `missing` makes it `todo` with `gh auth refresh -s project`; `not_readable` does not block; `gh` missing or logged out makes it `todo` with `gh auth login`.
- [x] T012 [US2] Implement `prerequisites` detection in `scripts/bootstrap_steps.py` (depends on T011).
- [x] T013 [US2] Write failing tests in `scripts/test_bootstrap_steps.py` for `bootstrap_issue` (research D3): candidates are open issues with the `<!-- spec-toolkit:bootstrap -->` marker opened by the logged-in user, plus issue `n` of any local or GitHub branch named `<n>-chore-bootstrap…` (`gh api --paginate repos/{repo}/branches`); every row of the D3 states table, including `details.candidates` as a list of `{number, title, branch}` with `branch` `null` when the issue has no branch; `performable` false with uncommitted changes outside the bootstrap files; `done` once bootstrap is merged.
- [x] T014 [US2] Implement `bootstrap_issue` detection in `scripts/bootstrap_steps.py` (depends on T013).
- [x] T015 [US2] Write failing tests in `scripts/test_bootstrap_steps.py` for `project_context`, `constitution` and `roadmap` (research D2): local checks when not merged; when merged, `project_context` is `done` only if the default branch's file passes `parse_context`, otherwise `todo` with the error's first line in `title` and an action linking `docs/bootstrap.md#project-contexttoml`; an existing invalid local file is `todo` and not performable; `constitution` carries `details.required_approvals` and names the `speckit-constitution` skill.
- [x] T016 [US2] Implement `project_context`, `constitution` and `roadmap` detection in `scripts/bootstrap_steps.py` (depends on T004, T015).
- [x] T017 [US2] Write failing tests in `scripts/test_bootstrap_steps.py` for `github_settings` (research D2, D14): `done` when the `github --check` logic finds no drift, using the approval count from the default branch's `project-context.toml` once merged; `not_checked` with the error's first line when that logic cannot load; `details.ruleset_warning` from the read-only rulesets GET mapped through `denied_hint` (`PLAN_HINT` or `ADMIN_HINT`), a 404 or other error giving "could not confirm that rulesets are available", for private repositories only.
- [x] T018 [US2] Implement `github_settings` detection and the shared ruleset-warning probe in `scripts/bootstrap_steps.py`, also filling `details.ruleset_warning` on `project_context` (depends on T016, T017).
- [x] T019 [US2] Write failing tests in `scripts/test_bootstrap_steps.py` for `board_credential` (secret names only through the existing `report_credentials`; `optional` when not configured; no value ever read or printed) and `board_workflows` (`manual` when "Pull request linked to issue" is off, with the action naming the board's Workflows page and Status `In review`; "Auto-add sub-issues to project" reported in `details.workflows` only; `not_checked` when unreadable; neither ever blocks) (FR-008, FR-009).
- [x] T020 [US2] Implement `board_credential` and `board_workflows` detection in `scripts/bootstrap_steps.py` (depends on T006, T019).
- [x] T021 [US2] Write failing tests in `scripts/test_bootstrap_steps.py` for `pull_request` and `merge` (research D2, D4, D7): the four ordered `pull_request` states (wrong branch: `done` when no local bootstrap work would be left out, else `git checkout <branch>`; `HEAD` not containing the PR head commit `headRefOid`, including `merge-base` exit 128: not performable, `git pull --rebase origin <branch>`, winning over uncommitted changes; uncommitted or ahead: `todo`; otherwise `done`); with no PR, compare `HEAD` with the branch tip on GitHub (404 = not pushed); "behind" the default branch via `git merge-base --is-ancestor <tip> HEAD` with a missing tip counting as behind; closed-unmerged and merged-without-files PRs named in `details`; `merge` `waiting` with failing checks in `details`, `todo` without a PR, and the merged-without-files message.
- [x] T022 [US2] Implement `pull_request` and `merge` detection in `scripts/bootstrap_steps.py` (depends on T014, T021).
- [x] T023 [US2] Write failing tests in `scripts/test_bootstrap_steps.py` for `first_sync` (research D5): `done` when merged and every `planned` entry of the default branch's `ROADMAP.md` has its Epic, spec-plan planning issue and Phase 1 issue, found with `load_issues` from `project_board.sync`, `roadmap_issues.index_roadmap_issues` and the sync's `find_phase1` (loaded with `importlib`) and `roadmap_issues.feature_path(entry)`; zero planned entries is `done` with "nothing to sync yet"; a newly planned entry reopens it; `SystemExit` from the reused code (unparsable roadmap, duplicates, conflicting Phase markers, a failing `gh` call) gives `not_checked` with the message's first line; `details.pending_changes` is an integer only when `HEAD` equals the tip, `ROADMAP.md` and `specs/` are clean and the dry run exits 0, else `null`, and never changes the state; `not_checked` when `bootstrap_merged` is `null`.
- [x] T024 [US2] Implement `first_sync` detection in `scripts/bootstrap_steps.py` (depends on T010, T023).
- [x] T025 [US2] Write failing tests in `scripts/test_bootstrap_steps.py` for the status report (data-model.md "Status report", research D4, D11): `format_version` 1; `result` is "`incomplete` if any blocking step is `todo`, `waiting` or `not_checked`; else `complete_with_manual_settings` if any step is `manual`; else `complete`"; `next_step` is the first blocking step that is not `done`, null when none; `bootstrap_merged` and `default_branch`; `performable` computed per run for each `needs_confirmation` step; a repository bootstrapped by hand reports complete or names only the missing parts.
- [x] T026 [US2] Implement the status report builder in `scripts/bootstrap_steps.py` (depends on T012, T014, T016, T018, T020, T022, T024, T025).
- [x] T027 [P] [US2] Write failing tests in `scripts/test_bootstrap.py` for `check --json` (contracts/check-json.md): exactly one JSON document on stdout, diagnostics on stderr; exit 0 for `complete` or `complete_with_manual_settings`, 1 for `incomplete`, 2 only when no report can be produced; `gh` missing or logged out gives `not_checked` GitHub steps and exit 1; the `run` fake records no write command (`gh issue create`, `git commit`, `git push`, `gh pr create`, `gh workflow run`, ruleset writes, `git fetch`).
- [x] T028 [US2] Add the `--json` flag to the `check` subcommand in `scripts/bootstrap.py`, leaving the human `check` output and exit codes unchanged (depends on T026, T027).
- [x] T029 [US2] Write a failing regression test in `scripts/test_bootstrap.py` that the human `check`, `init` and `github` commands keep their current output and exit codes, including exit 1 for a `check` error and `init` still defaulting `--required-approvals` to 0 without a terminal (FR-014, research D1).
- [x] T030 [US2] Write a failing docs-versus-engine test in `scripts/test_bootstrap_steps.py`: every field, `details` key and step ID documented in the "Machine-readable status" section of `docs/bootstrap.md` appears in the engine's output and the reverse, and the documented `format_version` equals the engine's (research D8).
- [x] T031 [US2] Write the "Machine-readable status" section of `docs/bootstrap.md`: the fields, step IDs, `details` keys, `format_version` rules and exit codes from contracts/check-json.md, without referring to `specs/` (depends on T028, T030).

### User Story 1 — Resumable bootstrap from the terminal (Priority: P1)

**Independent test**: On a fake repository, `start` run repeatedly, stopping after different steps,
reaches a merged pull request and an applied first sync; every rerun resumes at the first unfinished
step and never repeats or undoes a finished one; on the pilot, walkthroughs WS-1 to WS-3 and WS-8 to
WS-11 pass.

- [x] T032 [US1] Write failing tests in `scripts/test_bootstrap_start.py` for the preview block and digest (research D4): one function builds a step's preview block, identical in a terminal run, an exit-3 run and a `--confirm` run; it ends with `confirm: <step-id>@<digest>`, where the digest is the first 12 hex characters of SHA-256 over the step ID (plus the chosen issue number for `bootstrap_issue` only), the preview block without the `confirm:` line, prompt, hints or ANSI codes, `git hash-object` of every file to commit in path order (`deleted` for removals), and local `HEAD`, default tip, PR head commit and branch tip with `none` when not applicable.
- [x] T033 [US1] Implement the preview block builder and digest in `scripts/bootstrap_start.py` (depends on T026, T032).
- [x] T034 [US1] Write failing tests in `scripts/test_bootstrap_start.py` for the `start` driver loop (contracts/start-cli.md, research D12): non-blocking steps that are not `done` are printed and passed; a step with `needs_confirmation` prompts `Proceed? [y/N]` in a terminal, where anything but `y` exits 1 with nothing changed; without a terminal or with `--no-input` the first such step prints its preview and exits 3 and `prompt` is never called; every D12 stop exits 1 with the action and doc link; exit 0 when complete or complete with manual settings; in a terminal the digest is recomputed after the yes and a change exits 4.
- [x] T035 [US1] Implement the `start` driver loop in `scripts/bootstrap_start.py` (depends on T033, T034).
- [x] T036 [US1] Write failing tests in `scripts/test_bootstrap_start.py` for `--confirm STEP@DIGEST`, one per transition in the contract's order: malformed or bare value exits 2; `next_step` null exits 0; wrong step or no confirmation needed exits 4; not performable exits 1; missing `init` flag for `project_context` exits 2; several `bootstrap_issue` candidates without a valid `--issue` exits 3 with every candidate's preview; digest mismatch exits 4; otherwise acts and continues. Plus, for each of the 5 confirmable steps, a positive test that the digest printed with exit 3 is accepted when nothing changed (`first_sync` with and without board access), and a test that no write command runs before a confirmation (SC-003).
- [x] T037 [US1] Implement `--confirm STEP@DIGEST` and `--issue N` handling in `scripts/bootstrap_start.py` (depends on T035, T036).
- [x] T038 [US1] Write failing tests in `scripts/test_bootstrap_start.py` for the `bootstrap_issue` actions (research D3): `gh issue create` with a generic `CHORE:` title and the marker, then `gh issue develop <n> --base <default> --name <n>-chore-bootstrap --checkout`; develop only when the issue exists without a branch; `git checkout <branch>` (after `git fetch origin <branch>` when only on GitHub) for an existing branch, with the preview warning about carried-over files; a failed checkout exits 2 with git's message and never stashes; a terminal asks which candidate to use.
- [x] T039 [US1] Implement the `bootstrap_issue` actions in `scripts/bootstrap_start.py` (depends on T037, T038).
- [x] T040 [US1] Write failing tests in `scripts/test_bootstrap_start.py` for the `project_context` action (research D13, D14): runs the existing `init` (with board creation if chosen) only for a missing file and never passes `--force`; without a terminal or with `--no-input`, a missing `--description`, `--required-approvals` or `--project-number`/`--create-board` exits 2 naming every missing flag, with no preview, no digest and no file written; the flag values appear in the preview block; in a terminal the approval prompt offers 0 and requires acceptance; the preview shows `ruleset_warning`.
- [x] T041 [US1] Implement the `project_context` action in `scripts/bootstrap_start.py` (depends on T037, T040).
- [x] T042 [US1] Write failing tests in `scripts/test_bootstrap_start.py` for the `github_settings` action: the preview is the existing `github` plan and the confirmed action is `github --apply`, never `--allow-weaken`; a ruleset denial reports the likely cause and `start` does not loop on it.
- [x] T043 [US1] Implement the `github_settings` action in `scripts/bootstrap_start.py` (depends on T037, T042).
- [x] T044 [US1] Write failing tests in `scripts/test_bootstrap_start.py` for the `pull_request` action (research D7): only the missing parts run (commit the bootstrap file set constant from T001 with the shown message; `git push -u origin HEAD`; `gh pr create` with `Closes #<issue>`); other changed files are listed and never committed; the files in `<default>..HEAD` are listed; nothing to commit, push or open is not performable and exits 1 (depends on T001).
- [x] T045 [US1] Implement the `pull_request` action and the bootstrap file set constant in `scripts/bootstrap_start.py` (depends on T001, T037, T044).
- [x] T046 [US1] Write failing tests in `scripts/test_bootstrap_start.py` for the `first_sync` action (research D5): requires `HEAD` to equal the default tip and a clean `ROADMAP.md` and `specs/`, else exits 1 with `git checkout <default> && git pull --ff-only` or the paths; the preview is `python3 scripts/sync-roadmap-issues.py` with `--skip-project` when no board credential is configured, printed in full (stdout only in the digest); a non-zero dry run offers no apply, saying "fix the roadmap entries named above" only when the output contains "Roadmap sync left blocked entries unchanged", else "the preview failed" with the exit code; the confirmed action runs `gh workflow run sync-roadmap-issues.yml -f apply=true`, finds the run with `gh run list --workflow sync-roadmap-issues.yml --event workflow_dispatch --user <login>` created at or after the recorded time minus 10 seconds (polling up to 60 seconds), then `gh run watch <id> --exit-status`.
- [x] T047 [US1] Implement the `first_sync` action in `scripts/bootstrap_start.py` (depends on T037, T046).
- [x] T048 [US1] Write failing tests in `scripts/test_bootstrap.py` for the `start` subcommand wiring: synopsis `start [--no-input] [--confirm STEP_ID@DIGEST [--issue N]] [init flags]`, exit codes 0, 1, 2, 3, 4 and 130 (interrupted), and `--no-input` behaving as without a terminal.
- [x] T049 [US1] Add the `start` subcommand to `scripts/bootstrap.py`, delegating to `scripts/bootstrap_start.py` (depends on T039, T041, T043, T045, T047, T048).
- [x] T050 [US1] Write the guided terminal path in `docs/bootstrap.md`: `start`, `--no-input`, `--confirm STEP@DIGEST` and its check order, the step anchors used in `action.doc`, both first-sync preview paths (local dry run for `start`, the workflow's `apply=false` preview for the manual path), the trust model's limits (it guards against mistakes, not against a person or agent with the GitHub rights; commits are protected by the pull request and ruleset, review-bypassing steps only by who holds the rights), a note that the toolkit repository itself reads as bootstrapped, and an update to the "Checking progress" section naming `check --json` and `start` next to the human `check`; keep the manual path (FR-025, FR-027).
- [x] T051 [P] [US1] Update the `bootstrap.py` section of `scripts/README.md` for `start` and `check --json`.
- [x] T052 [P] [US1] Update the bootstrap row of the "Where to find what" table in `AGENTS.md` to name `start` and `check --json`, and confirm `scripts/test_agents_index.py` passes.

### Phase 1 wrap-up

- [x] T053 Run the quickstart automated checks (`node --test scripts/check-secrets.test.mjs`, `python3 -m unittest discover -s scripts -p 'test_*.py'`, `python3 scripts/check_roadmap.py`, `python3 scripts/project_context.py > /dev/null`, `python3 scripts/agent_setup.py check`) and Prettier on changed Markdown.
- [x] T054 Run walkthroughs WS-1, WS-2, WS-3, WS-8, WS-9, WS-10 and WS-11 (quickstart.md) on the pilot repository; during WS-1, stop after each confirmable step (`bootstrap_issue`, `project_context`, `github_settings`, `pull_request`, `first_sync`) and after `constitution` and `roadmap`, and resume in a new terminal each time, recording that no issue, branch, board or pull request is duplicated (SC-002); record each result in the Phase pull request (FR-026a).
- [x] T055 Check neutrality (FR-027, constitution VI): no adopter, maintainer, or this repository's issues, runs or roadmap named in `scripts/bootstrap_steps.py`, `scripts/bootstrap_start.py`, `scripts/bootstrap.py`, `docs/bootstrap.md` or `scripts/README.md`.
- [x] T056 On a second scratch pilot repository, follow the manual path in `docs/bootstrap.md` (Quick start commands, without `start`) to a merged bootstrap pull request and an applied first sync, and record in the Phase pull request that the commands and exit codes are unchanged (SC-006).
- [x] T057 Run the `security-auditor` pass on the Phase 1 diff (credential-name checks, the `--confirm` trust model and preview digest, `--no-input`, no secret in output or fixtures) and fix every Critical and High finding before the pull request (constitution V).

---

## Phase 2: Guide skill — conversational bootstrap in the coding agent

**Goal**: An adopter completes the bootstrap through a conversation in their coding agent, in "guide
me" or "do it for me" mode, with every decision and every review-bypassing change left to them, and
a pasted token always stops the flow (User Stories 3 and 4; FR-015–FR-024, FR-025 guided part,
FR-026, FR-026a WS-4 to WS-8 and WS-12, FR-027).

**Checkpoint**: The skill reference test and its guard tests pass, `python3 scripts/agent_setup.py
check` passes, walkthroughs WS-4 to WS-8 and WS-12 are recorded in the Phase pull request, and the
`security-auditor` pass has no open Critical or High finding.

### User Story 3 — Conversational guide in the coding agent (Priority: P2)

**Independent test**: The reference test confirms every command, `check --json` field, `details` key,
step ID and doc section the skill names exists; on the pilot, the bootstrap completes once in each
mode through the conversation alone, with a resume in a new session.

- [x] T058 [US3] Write a failing reference test in `scripts/test_bootstrap_guide_skill.py` (research D9): it extracts from `.agents/skills/bootstrap-guide/SKILL.md` every `python3 scripts/…` command, `docs/bootstrap.md#anchor`, `check --json` field and `details` key (including `candidates`) and step ID, and asserts each exists (script and subcommand, heading, documented field, catalogue ID); asserts the skill's known `format_version` equals the engine's; asserts every `start` command passes `--no-input` and every `--confirm` uses the `<step-id>@<digest>` form; plus a guard test showing each assertion failing on a deliberately broken copy.
- [x] T059 [US3] Write `.agents/skills/bootstrap-guide/SKILL.md` (FR-015–FR-020, FR-023, FR-024): reads the state only from `check --json` and explanations only from `docs/bootstrap.md`, with no copy of steps or order; stops when `format_version` is higher than it knows (FR-016a); asks for "guide me" or "do it for me" at the start of each session and allows switching; leaves description, visibility, approval count, constitution and roadmap to the user and passes the same `init` flags to the preview and confirming runs; asks for a yes only when `performable` is true; shows the preview from `start --no-input` unchanged and confirms with that preview's `confirm:` value; on exit 4 reruns `start --no-input`, shows the new preview and asks again; asks which bootstrap issue to use from `details.candidates`; answers questions from the docs and continues at the same step; guides enabling "Pull request linked to issue" (Status `In review`) and never asks to change "Auto-add sub-issues to project" (depends on T058).
- [x] T060 [US3] Add the guided-path part of `docs/bootstrap.md`: how to start the `bootstrap-guide` skill, the two modes, and every section anchor the skill links to (FR-025).
- [x] T061 [P] [US3] Add the `bootstrap-guide` skill to the bootstrap row of the "Where to find what" table in `AGENTS.md` and confirm `scripts/test_agents_index.py` passes.
- [x] T062 [US3] Run `python3 scripts/agent_setup.py sync` and `python3 scripts/agent_setup.py check` so the skill is picked up for every coding agent (depends on T059).

### User Story 4 — Safe handling of the board token (Priority: P2)

**Independent test**: In walkthroughs, a secret set in the terminal is detected from its name only,
and a pasted token-shaped string stops the flow with revoke-and-replace instructions and is never
passed to a command.

- [x] T063 [US4] Extend `scripts/test_bootstrap_guide_skill.py` (the safety checks now live in `scripts/test_bootstrap_guide_safety.py`) with failing checks that `SKILL.md` links the `docs/bootstrap.md#board-credentials` section, names only secret-name checks (no command that reads or prints a secret value), and has a token-pasted section that stops the flow and links the revoke-and-replace steps (FR-021, FR-022).
- [x] T064 [US4] Add the board-token part of `.agents/skills/bootstrap-guide/SKILL.md`: explains the classic token's scopes (`project`, plus `repo` for a private or `public_repo` for a public repository), offers to skip, tells the user to set the secret themselves in the terminal or on the web page, confirms only from listed secret names, and on a pasted token-shaped value stops, never uses it, treats it as leaked and gives revoke, create and set-again steps outside the chat (depends on T063).
- [x] T065 [US4] Add or update the revoke-and-replace instructions in the "Board credentials" section of `docs/bootstrap.md` that the skill links to (depends on T064).

### Phase 2 wrap-up

- [x] T066 Run the quickstart automated checks and Prettier on changed Markdown.
- [x] T067 Run walkthroughs WS-4, WS-5, WS-6, WS-7, WS-8 and WS-12 (quickstart.md) on the pilot repository through the coding agent and record each result in the Phase pull request (FR-026a).
- [x] T068 Check neutrality (FR-027, constitution VI) in `.agents/skills/bootstrap-guide/SKILL.md` and the Phase 2 changes to `docs/bootstrap.md` and `AGENTS.md`.
- [x] T069 Run the `security-auditor` pass on the Phase 2 diff (token-paste handling, secret-name checks, the guide's use of `--confirm` and exit 4) and fix every Critical and High finding before the pull request (constitution V).

### Follow-ups after the Phase 2 pilot

Fixes from the Phase 2 walkthroughs, each in its own reviewed pull request; every task here is
checked before that pull request merges, so the Phase stays complete.

- [x] T070 [US1] Write failing tests in `scripts/test_bootstrap_start.py` for the Sync Impact Report removal in the `pull_request` action (FR-006a, research D7): with an uncommitted constitution that starts with a well-formed comment containing "Sync Impact Report" (also after leading whitespace), the preview has the line `Will remove the Sync Impact Report comment at the top of .specify/memory/constitution.md before committing.` before the commit line, the digest from the exit-3 run is accepted by `--confirm`, and the file is rewritten without the comment, the rest of its closing line when only whitespace, and the blank lines after it, before `git add`, with the rest kept byte for byte; no removal and no preview line for a constitution without a comment, a leading comment without that text, an unclosed comment, the text only in a later comment, or a constitution with no uncommitted changes; a failed write exits 2 before any git command.
- [x] T071 [US1] Implement the Sync Impact Report removal in the `pull_request` action in `scripts/bootstrap_start.py` (depends on T070).
- [x] T072 [US3] Say in `docs/bootstrap.md` (Constitution, One bootstrap pull request) and in `.agents/skills/bootstrap-guide/SKILL.md` that the `pull_request` step removes the constitution's Sync Impact Report comment, so the user is not asked about it separately, the review covers the constitution without it, and its removal does not rerun the review; confirm `scripts/test_bootstrap_guide_skill.py` and `scripts/test_bootstrap_guide_safety.py` pass (FR-006a; depends on T071).
- [x] T073 Run the quickstart automated checks (`node --test scripts/check-secrets.test.mjs`, `python3 -m unittest discover -s scripts -p 'test_*.py'`, `python3 scripts/check_roadmap.py`, `python3 scripts/project_context.py > /dev/null`, `python3 scripts/agent_setup.py check`) (depends on T072).

Clearer `start` output (issue #51; FR-009, FR-010a to FR-010f, FR-023; research D16):

- [x] T074 [US2] Write failing tests in `scripts/test_bootstrap_steps.py` and `scripts/test_bootstrap_github.py` (FR-009, FR-010f): an enabled "Pull request linked to issue" gives `board_workflows` `done` with the title `Board workflow "Pull request linked to issue" is on; check that it sets Status to In review (the API cannot read it)` and `action` null (`format_version` still 1); the `github` preview's manual line reads `Manual step (the API cannot read or set it): in <board> open Workflows and check that "Pull request linked to issue" is enabled with Status = In review; set it if not.`; the `bootstrap_issue` and `pull_request` actions use `git switch <branch>` (after `git fetch origin <branch>` where it applies) and the `first_sync` action (`detect_first_sync`) uses `git switch <default> && git pull --ff-only origin <default>`, each name shell-quoted.
- [x] T075 [US2] Implement T074 in `scripts/bootstrap_steps.py` and `scripts/bootstrap_github.py` (depends on T074).
- [x] T076 [US1] Write failing tests in `scripts/test_bootstrap_start.py` for progress on stderr (FR-010c): `Reading the repository and GitHub state…` before each state read and one line per slow part of each confirmed action, all through `Env.progress`, with stdout, previews and digests unchanged, and any GitHub-sourced value in them (such as the run URL) passed through `_printable` (research D15); for `first_sync`, `Starting the sync workflow on GitHub…`, `Waiting for GitHub to start the run (up to 60 s)…`, `Run <id>: <url>`, then polling `gh run view <id> --repo <repo> --json status,conclusion,url` (no `gh run watch`) with `Still running (<m> min <s> s)…` at least every 30 s on the fake clock, the existing success line on `success`, the existing failure message for any other conclusion, and the existing interrupt message.
- [x] T077 [US1] Implement T076 in `scripts/bootstrap_start.py` (`Env.progress`, the polling follow of the run) (depends on T076).
- [x] T078 [US1] Write failing tests in `scripts/test_bootstrap_start.py` and `scripts/test_bootstrap.py` for the output layout, the stops and the commands (FR-010a, FR-010d, FR-010e, FR-010f): `Done: <step>: <result>.` after each performed action; a following preview starts with a blank line and `Next step (not done yet): <step>`, and before exit 3 prints `To do it, run:` and the full `python3 scripts/bootstrap.py start --no-input --confirm <step>@<digest>` command (with `--issue N` where it applies, and the shell-quoted init flags for `project_context`; GitHub-sourced values in the `Done:` result passed through `_printable`, research D15), with the `Confirm "…":` label and `confirm:` line last, also per candidate in the candidate list; without a terminal, missing init flags on a plain run or after a confirmed step print `Next step project_context needs your decisions: <flags>` on stdout and exit 1, while a `--confirm project_context@…` run keeps exit 2 and the stderr `needs:` line; stdout is flushed before each stderr line; the completion is a blank line and `BOOTSTRAP IS COMPLETE` or `BOOTSTRAP IS COMPLETE; MANUAL SETTINGS REMAIN (SEE ABOVE)`, bold only with a terminal stdout and `NO_COLOR` unset or empty (replace the old text in `scripts/test_bootstrap.py`); the `github_settings` and `first_sync` stops print `git switch <default> && git pull --ff-only origin <default>` and the `bootstrap_issue` preview and action use `git switch`.
- [x] T079 [US1] Implement T078 in `scripts/bootstrap_start.py` (depends on T078 and T077).
- [x] T080 [US1] Write failing tests in `scripts/test_bootstrap_start.py` for `--guided` (FR-010b): without `--confirm` it exits 2 before any state read; with it, a performed step prints `Done: <step>: <result>.` and `Done. Go back to your coding agent and reply "done".`, exits 0 and previews no next step; an already complete bootstrap prints the completion line and the same "done" line, exit 0; exits 1, 2, 3 and 4 print the usual message and then `Go back to your coding agent and tell it what happened.`; the digest checks are unchanged.
- [x] T081 [US1] Implement T080 in `scripts/bootstrap_start.py` (depends on T080 and T079).
- [x] T082 [US3] Update the needles and checks first, and see them fail, in `scripts/test_bootstrap_guide_skill.py` and `scripts/test_bootstrap_guide_safety.py` (FR-010b, FR-010d, FR-010f, FR-023): the skill takes the confirming command from the line after `To do it, run:`, adds `--guided` only to the commands it shows in "guide me" mode, treats exit 1 at `project_context` as decisions to collect, mentions the board Status check once when `board_workflows` is `done`, and its exact-match checkout-and-pull exception is `git switch <default> && git pull --ff-only origin <default>`; `docs/bootstrap.md` states the same (depends on T081).
- [x] T083 [US3] Update `.agents/skills/bootstrap-guide/SKILL.md` and `docs/bootstrap.md` (Guided path with start, Terminal, `--no-input` and exit codes, `--confirm`, Machine-readable status, Guided path in your coding agent, GitHub settings) and `scripts/README.md` where it describes `start`, so T082 passes (depends on T082).
- [x] T084 Run the quickstart automated checks (`node --test scripts/check-secrets.test.mjs`, `python3 -m unittest discover -s scripts -p 'test_*.py'`, `python3 scripts/check_roadmap.py`, `python3 scripts/project_context.py > /dev/null`, `python3 scripts/agent_setup.py check`) and record that WS-13 and WS-14 (quickstart.md) wait for the next pilot (depends on T083).

Follow-ups from the #51 review (issue #56; FR-010a, FR-010c; research D16; contracts/start-cli.md):

- [ ] T085 [P] [US1] Write failing tests in `scripts/test_project_context.py` and `scripts/test_bootstrap.py` (FR-010a): `project.name` and `project.description` containing a bidi control (U+202A, U+202E, U+2066, U+2069) are rejected with the existing single-line message, by `parse_context` and by `init` naming `--name` or `--description` with exit 2 before writing; a zero-width joiner (U+200D, e.g. in an emoji sequence) and a soft hyphen (U+00AD) still pass.
- [ ] T086 [US1] Implement T085 in `scripts/project_context.py` (the single-line rule: Unicode Cc, Zl, Zp and U+202A–U+202E, U+2066–U+2069) (depends on T085).
- [ ] T087 [P] [US1] Write failing tests in `scripts/test_bootstrap_start.py` for the decisions list and the progress lines (FR-010a, FR-010c): without a terminal, any given init flag whose value fails validation (`--name`, `--description` or `--owner` with a control or bidi character, and a negative `--required-approvals` or a `--project-number` below 1), together with a missing flag, is listed in `Next step project_context needs your decisions: <flags>` and followed by one line `<flag>: <validation message>` per invalid flag on stdout, exit 1, the value never printed and the command after `To give them, run:` carrying its placeholder; with no flag missing an invalid value still exits 2 naming the flag; the progress lines read `Applying <n> GitHub settings…` with the number of changes in the settings plan, `Committing <n> files…` with the number of files the `pull_request` action commits, and `Creating the bootstrap issue…`; with the fake clock, no two `Still running (…)…` lines (nor the run line and the first of them) are more than 30 s apart, given the 5 s poll.
- [ ] T088 [US1] Implement T087 in `scripts/bootstrap_start.py`; take the `Applying <n> GitHub settings…` count from the settings plan already computed for the `--confirm` digest check (no second GitHub read), and print the line without a count when that plan is not available (FR-010c "when it is known") (depends on T087 and T086).
- [ ] T089 [US1] Write failing tests in `scripts/test_bootstrap_start.py` for the review nits: when `gh run view` returns no `url`, the progress line is `Run <id>` with no `: None`; `start --guide --confirm …` is a usage error (exit 2, argparse `allow_abbrev=False`), not an abbreviation of `--guided`; the `pull_request` `Done:` result does not put `.` directly after the pull request URL (for example `Done: pull_request: Opened pull request #13 (<url>).`); then, as refactors with the suite green, write the U+2028 and U+2029 characters in the `LayoutTest` regex as `  ` escapes and remove the double blank line in `LayoutTest` (depends on T088).
- [ ] T090 [US1] Implement T089 in `scripts/bootstrap_start.py`, and replace `_progress_line` and `_error_line` with one stderr-line helper, keeping every existing test green (depends on T089).
- [ ] T091 [US3] Update the needles first, and see them fail, in `scripts/test_bootstrap_guide_skill.py` and `scripts/test_bootstrap_guide_safety.py` (FR-010a): the skill and `docs/bootstrap.md` say the decisions line lists missing or invalid flags, and that the skill asks the user again for an invalid flag, telling them the validation message, never repeating the rejected value (depends on T090).
- [ ] T092 [US3] Update `.agents/skills/bootstrap-guide/SKILL.md` and `docs/bootstrap.md` (`--no-input` and exit codes, the `project_context` flags) so T091 passes, then run `python3 scripts/agent_setup.py sync` (depends on T091).
- [ ] T093 Run the quickstart automated checks (`node --test scripts/check-secrets.test.mjs`, `python3 -m unittest discover -s scripts -p 'test_*.py'`, `python3 scripts/check_roadmap.py`, `python3 scripts/project_context.py > /dev/null`, `python3 scripts/agent_setup.py check`) (depends on T092).

---

## Dependencies and execution order

- **Phase 1 before Phase 2**: the skill reads `check --json` and runs `start`, so Phase 2 starts
  only after Phase 1 is merged.
- **Within Phase 1**: T001 and T002 first. The shared foundations (T003–T010) block both stories.
  User Story 2 (T011–T031) comes before User Story 1 (T032–T052), because `start` is built on the
  status report (T026). The wrap-up (T053–T057) comes last. T005, T044 and T045 use the pilot
  results from T001.
- **Within Phase 2**: User Story 3 (T058–T062) before User Story 4 (T063–T065), because both edit
  `SKILL.md` and the reference test; the wrap-up (T066–T069) comes last.
- **Follow-ups after the Phase 2 pilot**: start after Phase 2 is merged; within one fix, the test
  task, then its implementation, then the docs and skill, then the checks (T070–T073; T074–T084,
  where T076–T081 share `scripts/bootstrap_start.py` and run in sequence, and T074–T075 can run
  alongside T076–T077; T085–T093, where T087–T090 share `scripts/bootstrap_start.py` and run in
  sequence, T088 also needs T086, and the test tasks T085 and T087 can be written in parallel).
- **Test before implementation**: every implementation task depends on the test task just before it
  and is done only after that test was seen failing.

## Parallel opportunities

- T003 (`scripts/test_project_context.py`) can run alongside T007 (`scripts/test_bootstrap_steps.py`);
  T005 (`scripts/test_bootstrap_github.py`) joins them once T001 is done. T004 and T006 then follow
  their own tests.
- T027 (`scripts/test_bootstrap.py`) can be written while the step detections in
  `scripts/bootstrap_steps.py` are implemented. T029 and T048 also write `scripts/test_bootstrap.py`,
  so they run after T027, not alongside it.
- T051 (`scripts/README.md`) and T052 (`AGENTS.md`) can run alongside T050 (`docs/bootstrap.md`).
- Most step tasks share `scripts/bootstrap_steps.py` or `scripts/bootstrap_start.py`, so they run in
  sequence (one writer per file at a time).

## Implementation strategy

- **MVP**: Phase 1 through User Story 2 (T001–T031) gives a usable, tested `check --json`; User
  Story 1 (T032–T052) completes the engine and delivers the roadmap outcome for terminal users.
- **Each Phase is one pull request**, reviewed by `code-reviewer` on the final diff and by
  `security-auditor` (constitution V, plan.md), with the walkthrough results in its description.

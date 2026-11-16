# Research: Guided bootstrap

No open questions are left from the Technical Context. Each decision records what was chosen, why,
and what was rejected. Revised 2026-11-16 after the pre-PR review; the review items are cited as
R1–R9 (Required), O1–O5 (Optional) and F1–F4 (FYI).

## D1 — The human `check` stays as it is; `check --json` uses a new step catalogue

- **Decision**: Add `scripts/bootstrap_steps.py` with the 11-step catalogue (data-model.md).
  `check --json` and `start` use it. The human `check` keeps its six steps, its output and its exit
  codes, including exit 1 for a `check` error (F1). Where the steps overlap, the catalogue calls the
  existing `bootstrap_check` functions, so the logic exists once. New exit codes apply only to
  `check --json` and `start`.
- **Rationale**: FR-014. If the human `check` used the new catalogue, a repository waiting for its
  pull request would exit 1 where it exits 0 today.
- **Alternatives**: Move the human `check` onto the catalogue: rejected, because exit codes would
  change. A second, separate implementation: rejected, because it would duplicate logic.

## D2 — Progress is detected, never stored; the default branch comes first (R1, R2)

- **Decision**: Each run first asks GitHub whether bootstrap is already on the default branch:
  `gh repo view --json defaultBranchRef`, then `gh api repos/{repo}/contents/<path>?ref=<default>`
  for `project-context.toml`, `.specify/memory/constitution.md` and `ROADMAP.md`. This is read-only,
  needs no fetch, and sees a merge done on the web page at once.
  - **If all three exist there ("bootstrap merged")**: `bootstrap_issue`, `project_context`,
    `constitution`, `roadmap`, `pull_request` and `merge` are `done`, with the default branch as the
    evidence. Branch, issue state and local files no longer matter. This also covers repositories
    bootstrapped by hand or before this feature. `github_settings`, `board_credential`,
    `board_workflows` and `first_sync` are still checked live.
  - **Otherwise**, steps are detected locally and on the bootstrap branch:

    | Step | Done when |
    | --- | --- |
    | `prerequisites` | `gh auth status` succeeds and agent files are up to date (scope: D10) |
    | `bootstrap_issue` | an open bootstrap issue exists (D3) and its branch is checked out; if the branch exists but is not checked out, the state is `todo` with `git checkout <branch>`, never a new issue |
    | `project_context`, `constitution`, `roadmap` | existing local checks |
    | `github_settings` | `github --check` logic finds no drift |
    | `pull_request` | an open PR from the bootstrap branch exists |
    | `merge` | `waiting` while that PR is open (failing checks listed in `details`) |

- **Rationale**: FR-002 and clarification 1. Detecting "merged" from the default branch on GitHub
  fixes the resume after the merge, when the user is back on `main` and the issue is closed (R1). It
  also avoids a stale local `origin/main` without fetching inside a read-only command (R2).
- **Alternatives**: A local state file: rejected (it goes stale, breaks after a fresh clone).
  `origin/main`: rejected (stale without a fetch). The PR's merged state alone: rejected, because it
  misses repositories bootstrapped by hand.

## D3 — Bootstrap issue identity: a marker plus the author, or the branch name

- **Decision**: Issues created by the engine carry a hidden `<!-- spec-toolkit:bootstrap -->`
  marker. Candidates are open marker-bearing issues by the logged-in user, or the open issue named
  by a current `<n>-chore-bootstrap…` branch. With more than one candidate, the engine asks (or,
  without a terminal, stops and lists them). Only consulted while bootstrap is not merged (D2).
- **Rationale**: On a public repository anyone can open an issue that contains the marker; limiting
  candidates to the current user's own issues stops an outsider from steering the branch. The
  branch-name route covers issues created by hand.
- **Alternatives**: A label: rejected, because a fresh repository has no labels until
  `github --apply`. Title matching: rejected as fragile.

## D4 — Confirmation model for people and agents (O3, F2)

- **Decision**: In a terminal, `start` shows the preview and asks `y/N`. Without a terminal, `start`
  prints the preview and exits 3. The caller reruns `start --confirm <step-id>`, which acts only if
  that step is still the next step needing confirmation. A stale or mismatched `--confirm` exits 4,
  separate from errors (2). See contracts/start-cli.md.
- **Trust model (F2)**: `--confirm` cannot prove that a person said yes; without a terminal, the
  guarantee rests on the caller (the guide skill obeying FR-019). This is stated in
  `docs/bootstrap.md` and given to the `security-auditor` pass for both Phases.
- **Alternatives**: A global `--yes`: rejected, because it would approve every step at once.

## D5 — First roadmap sync: a local preview that mirrors the workflow (R3, R4, O2)

- **Preview**: `python3 scripts/sync-roadmap-issues.py`, a dry run with the arguments the workflow's
  apply will use: `--skip-project` when `board_credential` is not configured (the workflow adds it
  when no board token is available). The output is printed in full. If the dry run exits non-zero
  (for example 1 for blocked entries), the engine shows the output and the exit code, offers no
  apply, and the state stays `todo` with "fix the roadmap entries named above".
- **Done when (R3)**: bootstrap is merged, the local checkout contains the default branch tip
  (`git merge-base --is-ancestor <tip> HEAD`, a read-only check; when it fails, `todo` with
  `git checkout <default> && git pull --ff-only`), and that dry run reports no pending changes (no
  `[dry-run]` lines) and exits 0. With no `planned` entries and nothing pending, the step is `done`
  with the note "nothing to sync yet". A partial apply leaves pending changes, so it is not `done`.
- **Apply**: after confirmation, record the time, run
  `gh workflow run sync-roadmap-issues.yml -f apply=true`, then find the run with
  `gh run list --workflow sync-roadmap-issues.yml --event workflow_dispatch --user <login>` created
  at or after the recorded time (minus 10 seconds for clock skew), polling for up to 60 seconds. This
  skips the push-triggered preview run that the merge started (O2). Then
  `gh run watch <id> --exit-status`, and report the result.
- **Docs (F4)**: the guided path uses this local preview; the manual path in `docs/bootstrap.md` and
  the human `check` keep the workflow's `apply=false` preview (FR-014). The docs explain both.
- **Alternatives**: A preview through the workflow: rejected, because it adds polling and log
  parsing. A local apply: rejected, because it bypasses the board credential and the documented
  path. "Every planned entry has an Epic" as done: rejected (R3), because it is true with zero
  entries and after a partial apply.

## D6 — Board built-in workflows: a separate query that never blocks (R5, F3)

- **Decision**: A new `load_board_workflows` in `bootstrap_github.py` runs its own GraphQL query
  (`projectV2(number) { workflows(first: 20) { nodes { name enabled } } }`). Any error, or a name
  that is missing, maps to `not_checked` for that workflow. The shared `BOARD_QUERY` and
  `load_board` stay unchanged, so `github` and the human `check` cannot break because of it.
- **Verification first (F3)**: the first Phase 1 task confirms on the pilot board that the field
  exists, that the `project` scope can read it, and the exact names "Pull request linked to issue"
  and "Auto-add sub-issues to project". The fake's fixtures and guard tests are written from that
  observation.
- **Alternatives**: Extend `BOARD_QUERY`: rejected (R5), because one failing field would break the
  existing commands, against FR-009 and FR-014.

## D7 — Which files the pull request step commits (O1)

- **Decision**: After `git fetch origin`, the step stops if the branch is behind the remote default
  branch, and names the fix. It then shows the bootstrap file set, any other changed files (listed
  separately and not committed), and the commit message. After a yes, it commits that set, pushes,
  and runs `gh pr create` with `Closes #<issue>`. The bootstrap file set is a constant in the code:
  `project-context.toml`, `.specify/memory/constitution.md` and `ROADMAP.md`, plus the files that
  WS-1 shows the `speckit-constitution` and `roadmap-write` skills actually change. The first Phase 1
  task records them on the pilot.
- **Alternatives**: `git add -A`: rejected, because it could commit unrelated or secret files
  without the user seeing them.

## D8 — `check --json` versioning

- **Decision**: An integer `format_version` (1), with the rules in contracts/check-json.md
  (clarification 2). The format is documented for adopters in `docs/bootstrap.md` ("Machine-readable
  status"), because `specs/` is not published. A Phase 1 test compares the documented field list and
  step IDs with the engine's output (R9).

## D9 — How the skill is tested

- **Decision**: `scripts/test_bootstrap_guide_skill.py` (Phase 2) extracts from `SKILL.md` every
  `python3 scripts/…` command, every `docs/bootstrap.md#anchor`, every `check --json` field and every
  step ID, and asserts that each exists. It also asserts that the skill's known format version equals
  the engine's. A guard test shows each assertion failing on a broken copy. Walkthroughs run after
  each Phase (FR-026a; clarification 5).
- **Alternatives**: A scripted agent evaluation: out of scope, a candidate for a later roadmap entry.

## D10 — `prerequisites` and the `project` scope (O5)

- **Decision**: `prerequisites` is `done` when `gh auth status` succeeds and agent files are up to
  date. `details.project_scope` is `listed`, `missing` or `not_readable`. `missing` makes the step
  `todo` (`gh auth refresh -s project`). `not_readable` (for example `GH_TOKEN` or a fine-grained
  token, where `gh auth status` shows no scopes) does not block. A real scope problem then shows up
  in `github_settings`, which reads the board and already prints the `gh auth refresh` hint.
- **Alternatives**: Treat unreadable scopes as `todo`: rejected, because those users would be
  blocked forever.

## D11 — Unreadable blocking steps keep the result incomplete (R7)

- **Decision**: A blocking step that cannot be read is `not_checked` and makes `result`
  `incomplete`; it counts as the next step when it is the first unfinished one. Only
  `board_credential` and `board_workflows` are non-blocking when `not_checked`. The rule is fixed in
  format version 1.
- **Alternatives**: A separate `unknown` result: rejected, because callers would handle it the same
  as `incomplete`.

## D12 — `start` continues past non-blocking steps (R6)

- **Decision**: At an `optional` or `manual` step, `start` prints the step and its action, then
  continues. It stops only at a blocking step for the user (`constitution`, `roadmap`), a `waiting`
  step (`merge`), a blocking `not_checked` step, a declined prompt, or the end.
- **Alternatives**: Stop at every user step: rejected, because a user without a board token could
  never get past step 7 (FR-008, FR-009).

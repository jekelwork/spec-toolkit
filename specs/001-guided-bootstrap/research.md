# Research: Guided bootstrap

No open questions are left from the Technical Context. Each decision records what was chosen, why,
and what was rejected.

Two pre-PR reviews shaped this file (both 2026-11-16):

- The first review's items are cited as R1–R9 (Required), O1–O5 (Optional) and F1–F4 (FYI). Its
  text was lost; R8 and O4 have no recorded disposition and are treated as covered by the second
  review.
- The second review's items are cited as S1–S11 (Required) and SO (Optional), as recorded on
  issue #4.

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

## D2 — Progress is detected, never stored; the default branch comes first (R1, R2, S2, S6, SO)

- **Default branch**: every run reads it once with `gh repo view --json defaultBranchRef`. This
  document and the contracts write it as `<default>`; nothing assumes `main`.
- **Merged check**: next, the engine asks GitHub whether bootstrap is on the default branch, using
  `gh api repos/{repo}/contents/<path>?ref=<default>` for `project-context.toml`,
  `.specify/memory/constitution.md` and `ROADMAP.md`. This is read-only, needs no fetch, and sees a
  merge done on the web page at once.
  - A 404 means "not present", and one missing file makes `bootstrap_merged` `false` even when
    another read failed (that error is still kept for step titles). Otherwise any other error makes
    `bootstrap_merged` `null`.
  - `bootstrap_merged` is `true` when all three files exist.
- **If bootstrap is merged**:
  - `bootstrap_issue`, `constitution`, `roadmap`, `pull_request` and `merge` are `done`, with the
    default branch as the evidence. Branch, issue state and local files no longer matter. This also
    covers repositories bootstrapped by hand or before this feature.
  - `project_context` is `done` only when the default branch's `project-context.toml` content passes
    the same validation as `project_context.load_context` (S6). Because that function reads a path,
    Phase 1 splits it into `parse_context(text)` and a thin `load_context(path)` wrapper, with
    unchanged behaviour for existing callers. Otherwise it is `todo`, with the
    validation error's first line in `title`. A repository bootstrapped before `[review]` became
    required is an example. The action links to `docs/bootstrap.md#project-contexttoml` ("Upgrading
    an existing repository"): fix it in a pull request. `start` never performs this fix (D12).
  - `github_settings`, `board_credential`, `board_workflows` and `first_sync` are still checked
    live. `github_settings` uses the approval count from the default branch's
    `project-context.toml`, not the local copy, so an outdated checkout cannot change the result.
- **If `bootstrap_merged` is `null`**: steps 2–5, 9 and 10 use local detection (below). `first_sync`
  is `not_checked`, because it depends on the default branch.
- **Otherwise (not merged)**, steps are detected locally and on GitHub:

  | Step | State |
  | --- | --- |
  | `prerequisites` | `done` when `gh auth status --active --hostname github.com` succeeds and agent files are up to date (scope and gh version: D10) |
  | `bootstrap_issue` | See D3 |
  | `project_context` | `done` when the local file is valid. When it is missing: `todo`, performed by `start`. When it exists but is invalid: `todo` with the error; `start` never passes `--force` (D12) |
  | `constitution`, `roadmap` | existing local checks |
  | `github_settings` | `done` when the `github --check` logic finds no drift. When that logic cannot load (invalid `project-context.toml`, a `gh` error): `not_checked`, with the error's first line in `title` |
  | `pull_request` | `done` when an open PR from the bootstrap branch exists, the branch has no uncommitted changes to the bootstrap file set, and local `HEAD` equals the PR's head commit on GitHub (`gh pr view <n> --json headRefOid`; no fetch, no `origin/<branch>`). Checked in this order: (1) The current branch is not the PR's head branch: `done` (`merge` then reports the review) when there are no uncommitted changes to the bootstrap file set and the local `<branch>` ref, if there is one, is contained in the head commit; otherwise `todo`, not performable, with `git checkout <branch>` as the action. (2) `HEAD` does not contain the head commit (`git merge-base --is-ancestor <headRefOid> HEAD` exits non-zero; exit 128, a head commit missing locally because someone else pushed, counts too): `todo`, not performable, with `git pull --rebase origin <branch>` as the action (D12); this wins over uncommitted changes. (3) Uncommitted changes, or `HEAD` ahead of the head commit: `todo`, and `start` commits and pushes them (D7). (4) Otherwise `done`. With no PR (not performable if `HEAD` does not contain the branch's tip on GitHub; D7), only a PR closed without merging, or a merged PR whose files are not on the default branch: `todo`; `start` offers a new PR with the shown file set, and the earlier PR is named in `details` |
  | `board_credential`, `board_workflows` | Not part of this table: non-blocking, and detected the same way whether or not bootstrap is merged. See D6, D10 and data-model.md |
  | `merge` | `waiting` while that PR is open (failing checks in `details`). With no PR, or only a closed one: `todo`, with an action pointing at `pull_request`. If a bootstrap PR was merged but the files are not on the default branch: `todo`, with "PR #n was merged but the bootstrap files are not on `<default>`; check what it contained" |

- **Rationale**: FR-002 and clarification 1. Reading "merged" from the default branch on GitHub fixes
  the resume after the merge, when the user is back on the default branch and the issue is closed
  (R1). It also avoids a stale local `origin/main` without a fetch inside a read-only command (R2).
  Validating the content, not only the file's existence, names the one missing part instead of
  reporting "done" while `github_settings` fails (S6; spec edge case for older repositories).
- **Alternatives**:
  - A local state file: rejected (it goes stale, breaks after a fresh clone).
  - `origin/main`: rejected (stale without a fetch).
  - The PR's merged state alone: rejected, because it misses repositories bootstrapped by hand.

## D3 — Bootstrap issue identity, and resuming on an existing branch (S11, decision 4)

- **Candidates**: open issues that meet one of these, while bootstrap is not merged (D2):
  - It carries the hidden `<!-- spec-toolkit:bootstrap -->` marker and was opened by the logged-in
    user.
  - It is issue `n` of a branch named `<n>-chore-bootstrap…`. Branches are read from the local
    branches and from GitHub (`gh api --paginate repos/{repo}/branches`), so a branch that is not
    checked out, or exists only on GitHub, is found without a fetch. This covers issues created by
    hand from the manual path, which have no marker.
- **States of `bootstrap_issue`**:

  | Situation | State | What `start` does after a yes |
  | --- | --- | --- |
  | One candidate, its branch checked out | `done` | — |
  | One candidate, its branch exists but is not checked out | `todo` | `git checkout <branch>`, after `git fetch origin <branch>` if it exists only on GitHub |
  | One candidate, no branch | `todo` | `gh issue develop <n> --base <default> --name <n>-chore-bootstrap --checkout` |
  | No candidate | `todo` | Create the issue (generic `CHORE:` title, marker), then the branch as above |
  | More than one candidate, one of their branches checked out | `done` | — (that candidate is the choice; detection stores nothing, so asking again would make `start --issue N` loop) |
  | More than one candidate, none of their branches checked out | `todo` | In a terminal, `start` lists them and asks which one to use. Without a terminal or with `--no-input`, it lists them, each with its own preview and `confirm:` line, and exits 3; the caller reruns `start --confirm bootstrap_issue@<digest> --issue <n>` for the chosen one. Then it continues as for one candidate |

  Every row that acts is a confirmed action (preview, then `y/N`, or exit 3 without a terminal).
  Every row that acts requires a working tree whose only changes are bootstrap files (they carry
  over to the branch). Otherwise `start` stops and names the other files.

  `details.candidates` on `bootstrap_issue` lists every candidate as `{number, title, branch}`, with
  `branch` `null` when the issue has no branch. The listing `start` prints before exit 3 shows the
  same data, so the guide skill asks the user from `check --json` alone (FR-016).

  A checkout can still fail when the existing branch already commits the same files that carry
  over. The preview says so whenever there are carried-over files. If `git checkout` fails, `start`
  exits 2 with git's message and changes nothing else; it never stashes.
- **Rationale**: FR-003 and the spec edge cases for a hand-made issue, for several candidates (the
  engine "asks the user which one to use") and for an existing branch, where the engine "offers to
  switch". On a public repository anyone can open an issue that contains
  the marker. Limiting marker candidates to the current user's own issues stops an outsider from
  steering the branch. The trade-off: a second maintainer who resumes before any bootstrap branch
  exists is offered a new issue; once a branch exists, the branch route finds it.
- **Alternatives**:
  - A label: rejected, because a fresh repository has no labels until `github --apply`.
  - Title matching: rejected as fragile.
  - Only the checked-out branch: rejected (S11), because it offers a duplicate issue.

## D4 — Confirmation model for people and agents (O3, F2, S7, S9)

- **Decision**: In a terminal, `start` shows the preview and asks `y/N`. Without a terminal, or with
  `--no-input`, `start` never prompts. It prints the preview and exits 3, and the caller reruns
  `start --confirm <step-id>@<digest>` with the value from the preview's last line.
- **`--confirm X` acts only when** `X` equals `next_step` (the first blocking step that is not
  `done`; non-blocking steps never count) and that step has `needs_confirmation: true`. Otherwise it
  exits 4 and changes nothing, so a stale or mismatched confirmation, or one given while a user step
  is open, never acts. See contracts/start-cli.md. `docs/bootstrap.md` uses the same wording.
- **A step `start` cannot perform now**: each step carries `performable`, which is true only when
  `start --confirm <id>@<digest>` would act on this run. It is false, for example, for an invalid existing
  `project-context.toml`; a `bootstrap_issue` with uncommitted changes outside the bootstrap files;
  a `pull_request` behind the default branch, whose `HEAD` does not contain the PR's head commit,
  or with nothing to commit, push or open; or a
  `first_sync` whose checkout is not the clean default branch tip (D12). "Behind" means local
  `HEAD` does not contain the default branch tip: the tip SHA comes from `gh api repos/{repo}/branches/<default> --jq
  .commit.sha`, and the branch is behind unless `git merge-base --is-ancestor <tip> HEAD` exits 0.
  A tip that is missing locally (exit 128) counts as behind, which is correct, because `HEAD`
  cannot contain a commit the clone does not have. This needs no fetch and works for a branch that
  is not on GitHub yet, so `check --json`, `performable` and `start` use the same rule. (For
  `first_sync`, D5 needs `HEAD` to equal the tip exactly, so it compares the IDs instead.)
  `--confirm` on the `next_step` while it is not performable exits 1, prints the action, and
  changes nothing. The guide skill asks for a yes only when `performable` is true.
- **The confirmation is bound to the preview (FR-006)**: every preview ends with one line,
  `confirm: <step-id>@<digest>`, and `--confirm` takes that value. The digest is the first 12 hex
  characters of a SHA-256 over these inputs, each on its own line, UTF-8 with LF endings:
  - the step ID; for `bootstrap_issue`, also the chosen issue number (the single candidate, `N`
    from `--issue`, or `new`). No other step includes an issue number, so a stray `--issue` does
    not change their digest;
  - this step's preview block only: for `bootstrap_issue` with several candidates, only that
    candidate's block. The block excludes the `confirm:` line itself, other steps' lines, the
    `Proceed? [y/N]` prompt, any "rerun with" hint and ANSI colour codes. It is built by one
    function, so it is identical in a terminal run, an exit-3 run and a `--confirm` run. For
    `first_sync` it includes the dry run's stdout in printed order, never its stderr;
  - the content hash (`git hash-object`) of every file the step will commit, in path order, or
    `deleted` for a file the step removes;
  - local `HEAD`, the default branch tip on GitHub, the head commit of the open PR if there is
    one, and otherwise the bootstrap branch's tip on GitHub if it is pushed. Each of these lines is
    always present, with the fixed value `none` when it does not apply, so the input layout never
    changes.

  `--confirm` recomputes the digest before acting. If it differs, `start` exits 4 with "the preview
  changed since you confirmed it; run `start` again to see the new one" and changes nothing.
  `--confirm` without a digest, or with a malformed one, exits 2 (a usage error). The order in which `--confirm` checks is fixed in
  contracts/start-cli.md. In a terminal, `start` computes the digest before the `y/N` prompt and
  again after the yes, and stops with exit 4 if they differ. For `first_sync` this reruns the dry
  run, which is slower but correct. So a file edit, a new
  commit, or a push to the default branch between preview and confirmation never acts on something
  the user did not see.
- **FR-010 is met**: the terminal `y/N` is the fallback. Without a terminal or with `--no-input`,
  `start` never prompts and takes every answer from flags and `--confirm`. Decision values follow
  D13.
- **How the guide uses the digest (Phase 2)**: the state comes from `check --json` (FR-016); the
  preview and its `confirm:` value come only from `start --no-input`'s exit-3 output, which the
  guide shows to the user unchanged (FR-019). The guide never builds a digest itself. After the
  user's yes it runs `start --no-input --confirm <step-id>@<digest>` with that value. On exit 4 it
  reruns `start --no-input`, shows its new preview and asks again; it never confirms a digest the
  user has not seen. For `project_context` it gathers the decision values first (FR-018) and
  passes the same `init` flags to the preview run and the confirming run (D13).
- **`--no-input` (S9)**: behaves exactly as without a terminal. Some agent tool runners execute
  commands in a pseudo-terminal, where `start` would otherwise wait on the prompt. The guide skill
  always passes `--no-input`.
- **Trust model (F2)**: `--confirm` cannot prove that a person said yes. Without a terminal, the
  guarantee rests on the caller (the guide skill obeying FR-019). An agent that has a
  pseudo-terminal can also answer the `y/N` prompt itself; telling a real terminal from a
  pseudo-terminal is not reliable, so `start` does not try. `docs/bootstrap.md` says plainly that the
  confirmation guards against mistakes, not against a person or agent who has the GitHub rights:
  - Commits reach the default branch only through the pull request, which the branch ruleset and
    review protect.
  - The steps that bypass review (creating the issue, `github --apply`, the sync apply) are
    protected only by who holds those GitHub rights.

  The `security-auditor` pass for both Phases reviews this trust model and the digest.
- **Alternatives**:
  - A global `--yes`: rejected, because it would approve every step at once.
  - Reusing `init`'s `--yes` as the no-prompt flag: rejected, because it reads like approval.
  - `--confirm STEP_ID` without a digest: rejected, because `--confirm` recomputes the state and
    could act on changes made after the preview.
  - Refusing a pseudo-terminal: rejected, because it cannot be detected reliably; the docs state
    the limit instead.

## D5 — First roadmap sync (R3, R4, O2, S1, S3, S4, SO; decision 3)

- **Done when**: bootstrap is merged and, for every `planned` entry in the default branch's
  `ROADMAP.md` (read through the contents API), these exist, found by their markers as the sync
  finds them:
  - its Epic (`roadmap_issues.roadmap_marker`);
  - its spec-plan planning issue (`planning_marker`);
  - its Phase 1 issue (`roadmap_phase_marker`).

  The lookups reuse the sync's own code, so they find exactly what the sync finds:
  - Issues are loaded with `load_issues` (all states, with the author
    fields and labels). An issue without author fields would pass `is_trusted_author`, so the
    engine never uses its own, narrower query.
  - `roadmap_issues.index_roadmap_issues` (which applies `trusted_issues`) finds the Epic and
    planning issue, and the sync's `find_phase1` finds the Phase 1 issue by the stable roadmap phase
    marker or by `phase_marker(feature_path, 'phase-1')`.
  - `feature_path` comes from `roadmap_issues.feature_path(entry)` for the entry in the default
    branch's `ROADMAP.md` (its spec directory, or `specs/<id>-<slug>` when it has none), not from the
    local `specs/` folder, so an outdated checkout does not change the result.
  - `load_issues` is taken from `project_board.sync`, the copy of `sync-task-issues.py` that
    `project_board` already loads. Only `sync-roadmap-issues.py`, which has a hyphen in its name, is
    loaded with `importlib`, as `scripts/test_agent_setup.py` does.

  The reused code ends the process with `sys.exit` on bad input: an unparsable `ROADMAP.md`,
  duplicate issues (`only_match`), conflicting Phase markers, or a failing `gh` call. The engine catches `SystemExit`
  around the whole `first_sync` evaluation and makes the step `not_checked`, with the message's
  first line in `title`, so `check --json` still prints one report.

  With no `planned` entries the step is `done`, with the note "nothing to sync yet". A partial apply
  leaves an issue missing, so it is not `done`.
- **After the first apply**: push and scheduled sync runs are dry runs; only a dispatched run with
  `apply=true` applies. So an entry that becomes `planned` later has no issues until the next
  apply, and `first_sync` is `todo` again until then, with the same preview and apply as the first
  time. Other roadmap edits that leave every `planned` entry's issues in place, such as a changed
  title, do not change the state. They are reported only as information: `details.pending_changes`,
  the number of `[dry-run]` lines. `check --json` fills this only when the local checkout is the
  default branch tip and clean (see below) and the dry run exits 0. Otherwise it is `null`. It
  never changes the state or `result` (S3).
- **Before the preview** (only when `start` is about to offer the apply):
  - `git rev-parse HEAD` must equal the default branch tip that GitHub reports
    (`gh api repos/{repo}/branches/<default> --jq .commit.sha`).
  - `git status --porcelain -- ROADMAP.md specs/` must be empty.

  Otherwise `start` stops with exit 1 and prints `git checkout <default> && git pull --ff-only`, or
  the paths to commit or stash. A plain comparison of commit IDs needs no local copy of the tip, so
  a missing commit after a web merge needs no special case (S1). The preview therefore reads exactly
  what the dispatched workflow will check out (S4).
- **Preview**: `python3 scripts/sync-roadmap-issues.py`, a dry run with the arguments the workflow's
  apply will use: `--skip-project` when `board_credential` is not configured. The output is printed
  in full. When the dry run exits non-zero, no apply is offered and the state stays `todo`:
  - If the output contains "Roadmap sync left blocked entries unchanged", the action is "fix the
    roadmap entries named above".
  - Otherwise the action is "the preview failed", with the output and the exit code. For example, a
    configured board credential makes the local dry run read the board with the user's own `gh`
    login, which may lack access.
- **Apply**: after confirmation, the engine records the time and runs
  `gh workflow run sync-roadmap-issues.yml -f apply=true`. It then finds the run with
  `gh run list --workflow sync-roadmap-issues.yml --event workflow_dispatch --user <login>`, created
  at or after the recorded time minus 10 seconds for clock skew, polling for up to 60 seconds. This
  skips the push-triggered preview run that the merge started (O2). Then it runs
  `gh run watch <id> --exit-status` and reports the result.
- **Docs (F4)**: the guided path uses this local preview. The manual path in `docs/bootstrap.md` and
  the human `check` keep the workflow's `apply=false` preview (FR-014). The docs explain both.
- **Alternatives**:
  - A preview through the workflow: rejected, because it adds polling and log parsing.
  - A local apply: rejected, because it bypasses the board credential and the documented path.
  - "The dry run shows no pending changes" as done: rejected (S3), because any later roadmap edit,
    such as a changed title, would make a finished bootstrap `incomplete` again. A newly planned
    entry still does, because it really needs an apply.
  - `git merge-base --is-ancestor`: rejected (S1, S4), because it fails on a missing commit and
    accepts unpushed commits.

## D6 — Board built-in workflows: a separate query that never blocks (R5, F3)

- **Decision**: A new `load_board_workflows` in `bootstrap_github.py` runs its own GraphQL query
  (`projectV2(number) { workflows(first: 20) { nodes { name enabled } } }`). Any error, or a missing
  name, maps to `not_checked` for that workflow. The shared `BOARD_QUERY` and `load_board` stay
  unchanged, so `github` and the human `check` cannot break because of it.
- **Verification first (F3)**: the first Phase 1 task confirms on the pilot board three things: the
  field exists, the `project` scope can read it, and the exact names "Pull request linked to issue"
  and "Auto-add sub-issues to project". The fake's fixtures and guard tests are written from that.
- **Alternatives**: Extend `BOARD_QUERY`: rejected (R5), because one failing field would break the
  existing commands, against FR-009 and FR-014.

## D7 — The pull request step: file set and partial states (O1, SO)

- **Checks first**: the step stops if the branch is behind the default branch tip, by the rule in
  D4, and names the fix (`git pull origin <default>`, or `git fetch origin` first when the tip is
  missing locally) (D12).
- **File set**: a constant in the code: `project-context.toml`, `.specify/memory/constitution.md`
  and `ROADMAP.md`, plus the files that the `speckit-constitution` and `roadmap-write` skills
  actually change. The first Phase 1 task records those on the pilot.
- **Only the missing parts are done (FR-003)**:
  - uncommitted changes to the file set: commit them;
  - commits not on GitHub: `git push -u origin HEAD`. "Not on GitHub" is read without a fetch:
    with an open PR, against its head commit (D2); with no PR, against the branch's tip on GitHub
    (`gh api repos/{repo}/branches/<branch> --jq .commit.sha`, a 404 meaning the branch is not
    pushed yet). The same contains-or-ahead rule applies: if `HEAD` does not contain that tip
    (someone else pushed), the step is not performable and the action is `git pull --rebase origin <branch>`;
  - no open PR: `gh pr create` with `Closes #<issue>`.

  The preview lists exactly the parts it will do, plus the commit message. It also lists other
  changed files, which are not committed, and the files in `<default>..HEAD` that the PR will
  contain. When there is nothing to commit, push or open, the step is not `performable`, and
  `start` stops with exit 1 and says so.
- **Alternatives**: `git add -A`: rejected, because it could commit unrelated or secret files
  without the user seeing them.

## D8 — `check --json` versioning

- **Decision**: An integer `format_version` (1), with the rules in contracts/check-json.md
  (clarification 2). The format is documented for adopters in `docs/bootstrap.md` ("Machine-readable
  status"), because `specs/` is not published. A Phase 1 test compares the documented field list and
  step IDs with the engine's output (R9).

## D9 — How the skill is tested

- **Decision**: `scripts/test_bootstrap_guide_skill.py` (Phase 2) extracts from `SKILL.md` every
  `python3 scripts/…` command, every `docs/bootstrap.md#anchor`, every `check --json` field and
  `details` key (including `candidates`) and every step ID, and asserts that each exists. It also
  asserts that the skill's known format version equals the engine's, that every `start`
  command it names passes `--no-input`, and that every `--confirm` it names uses the
  `<step-id>@<digest>` form. A guard test shows each assertion failing on a broken
  copy. Walkthroughs run after each Phase (FR-026a;
  clarification 5).
- **Alternatives**: A scripted agent evaluation: out of scope, a candidate for a later roadmap entry.

## D10 — `prerequisites` and the `project` scope (O5)

- **Decision**: `prerequisites` is `done` when `gh auth status --active --hostname github.com`
  succeeds (gh 2.40+, so a broken inactive account does not fail it and only the active account's
  scopes are read) and agent files are up to date. `details.project_scope` is `listed`, `missing`
  or `not_readable`:
  - `missing` makes the step `todo` (`gh auth refresh -s project`).
  - `not_readable` does not block. Examples are `GH_TOKEN` or a fine-grained token, where
    `gh auth status` shows no scopes. A real scope problem then shows up in `github_settings`, which
    reads the board and already prints the `gh auth refresh` hint.
- **Alternatives**: Treat unreadable scopes as `todo`: rejected, because those users would be
  blocked forever.

## D11 — Unreadable blocking steps keep the result incomplete (R7)

- **Decision**: A blocking step that cannot be read is `not_checked` and makes `result`
  `incomplete`. It counts as the next step when it is the first unfinished one. Only
  `board_credential` and `board_workflows` are non-blocking when `not_checked`. The rule is fixed in
  format version 1.
- **Alternatives**: A separate `unknown` result: rejected, because callers would handle it the same
  as `incomplete`.

## D12 — Where `start` continues and where it stops (R6, S8)

- **Continues**: at any non-blocking step that is not `done` (`optional`, `manual`, or
  `not_checked`), `start` prints the step and its action, then continues.
- **Performs, after a confirmation**: these actions only:
  - `bootstrap_issue`: create, develop or check out (D3);
  - `project_context`: create a missing file, never overwrite one;
  - `github_settings`: `github --apply`, never `--allow-weaken`;
  - `pull_request`: the missing parts (D7);
  - `first_sync`: the apply (D5).
- **Stops with exit 1**: at any other blocking step that is not `done`, printing the action and its
  doc link. Examples:
  - `prerequisites` `todo`;
  - `constitution` and `roadmap`;
  - an invalid existing `project-context.toml`;
  - `bootstrap_issue` with uncommitted changes outside the bootstrap files;
  - `pull_request` behind the default branch, on a branch other than the PR's head branch (action
    `git checkout <branch>`), with `HEAD` not containing the PR's head commit or the branch's tip on
    GitHub (action `git pull --rebase origin <branch>`), or with nothing to commit, push or open;
  - `merge` `todo` or `waiting`;
  - `first_sync` when the checkout is not the clean default branch tip;
  - a blocking `not_checked` step.
- **Alternatives**: Stop at every user step: rejected, because a user without a board token could
  never get past step 7 (FR-008, FR-009).

## D13 — `start` never takes a default for a decision value (S5)

- **Decision**: `start` passes its own `init` flags to the `project_context` step and checks them
  before `init` runs. Without a terminal, or with `--no-input`, these are required:
  - `--description`;
  - `--required-approvals`;
  - one of `--project-number` or `--create-board`.

  If one is missing when `start` reaches `project_context`, it exits 2, names every missing flag,
  prints no preview and no digest, and writes nothing. The caller reruns `start --no-input` with
  the flags to get the preview, then confirms with the same flags. The flag values are part of the
  preview block, so the digest covers them (D4). `--name` and
  `--owner` keep their defaults from the repository, which are not decisions. In a terminal,
  `start` offers 0 as the default approval count, and the user must accept it at the prompt. The
  preview, and the output before exit 3, show every value that will be written.
- **Rationale**: FR-010 and FR-018. Since `init` defaults `--required-approvals` to 0 without a
  terminal, a call from an agent would otherwise pick the team's approval count for the user.
- **Alternatives**: Change `init` to stop: rejected, because `init`'s behaviour is kept (FR-014).

## D14 — Visibility and plan warning (S10)

- **Decision**: the `project_context` step reads `gh repo view --json visibility`. For a private
  repository it then probes the rulesets with the read-only GET that `github` already uses
  (`repos/{repo}/rulesets?includes_parents=false&per_page=100`). A refusal is mapped through the
  existing `denied_hint`, so a plan without rulesets gives `PLAN_HINT` and missing rights give
  `ADMIN_HINT`. An HTTP 404 is not a refusal: as in `plan_ruleset`, it means the repository was not
  found, and gives "could not confirm" below.
- **Warning**: that hint goes into `details.ruleset_warning`, and the preview shows it. The same
  check fills `details.ruleset_warning` on `github_settings`, so a run that resumes after
  `project_context` is done still shows it. Any other error gives "could not confirm that rulesets
  are available", with the error's first line.
  Visibility is chosen when the repository is created; the engine reports it and never changes it.
- **Verification first**: the first Phase 1 task records on the pilot what the GET returns for a
  private repository on GitHub Free, and the fake's fixtures follow it.
- **Rationale**: spec US1 acceptance scenario 2. Today the hint appears only when `github` runs,
  which is a step later.
- **Alternatives**: Read the account plan (`gh api user --jq .plan.name`): rejected, because
  `/user` returns `plan` only to a token with the `user` scope, which default `gh` logins lack, so
  every private repository would get "could not confirm".

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
  | `pull_request` | `done` when an open PR from the bootstrap branch exists, the branch has no uncommitted changes to the bootstrap file set, and local `HEAD` equals the PR's head commit on GitHub (`gh pr view <n> --json headRefOid`; no fetch, no `origin/<branch>`). Checked in this order: (1) The current branch is not the PR's head branch: `done` (`merge` then reports the review) when there are no uncommitted changes to the bootstrap file set and the local `<branch>` ref, if there is one, is contained in the head commit; otherwise `todo`, not performable, with `git switch <branch>` as the action. (2) `HEAD` does not contain the head commit (`git merge-base --is-ancestor <headRefOid> HEAD` exits non-zero; exit 128, a head commit missing locally because someone else pushed, counts too): `todo`, not performable, with `git pull --rebase origin <branch>` as the action (D12); this wins over uncommitted changes. (3) Uncommitted changes, or `HEAD` ahead of the head commit: `todo`, and `start` commits and pushes them (D7). (4) Otherwise `done`. With no PR (not performable if `HEAD` does not contain the branch's tip on GitHub; D7), only a PR closed without merging, or a merged PR whose files are not on the default branch: `todo`; `start` offers a new PR with the shown file set, and the earlier PR is named in `details` |
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
  - It is issue `n` of a branch whose whole name is `<n>-chore-bootstrap`, optionally followed by
    `-`-separated lowercase letters and digits (`([1-9][0-9]*)-chore-bootstrap(?:-[a-z0-9]+)*`, the
    names the branch-name check in `pr_conventions.py` accepts). A name with any other character,
    such as a shell metacharacter, is never a candidate. Branches are read from the local
    branches and from GitHub (`gh api --paginate repos/{repo}/branches`), so a branch that is not
    checked out, or exists only on GitHub, is found without a fetch. This covers issues created by
    hand from the manual path, which have no marker.
- **States of `bootstrap_issue`**:

  | Situation | State | What `start` does after a yes |
  | --- | --- | --- |
  | One candidate, its branch checked out | `done` | — |
  | One candidate, its branch exists but is not checked out | `todo` | `git switch <branch>`, after `git fetch origin <branch>` if it exists only on GitHub |
  | One candidate, no branch | `todo` | `gh issue develop <n> --base <default> --name <n>-chore-bootstrap --checkout` |
  | No candidate | `todo` | Create the issue (generic `CHORE:` title, marker), then the branch as above |
  | More than one candidate, one of their branches checked out | `done` | — (that candidate is the choice; detection stores nothing, so asking again would make `start --issue N` loop) |
  | More than one candidate, none of their branches checked out | `todo` | In a terminal, `start` lists them and asks which one to use. Without a terminal or with `--no-input`, it lists them, each with its own preview and `confirm:` line, and exits 3; the caller reruns `start --confirm bootstrap_issue@<digest> --issue <n>` for the chosen one. Then it continues as for one candidate |

  Every row that acts is a confirmed action (preview, then `y/N`, or exit 3 without a terminal).
  Every row that acts requires a working tree whose only changes are bootstrap files (they carry
  over to the branch). Otherwise `start` stops and names the other files. Every row that acts also
  requires `origin` to name the repository `gh` uses (D15); otherwise `start` stops with exit 1
  before any `gh issue create`, `gh issue develop`, `git fetch` or `git switch`.

  In a terminal, `--confirm bootstrap_issue@<digest>` with several candidates and no `--issue`
  lists them and exits 3 instead of asking, as without a terminal. An `--issue N` that is not a
  candidate (also when the single candidate is another number) lists the candidates and exits 3,
  in a terminal too; with zero candidates it exits 3 and asks to rerun without `--issue`.

  `details.candidates` on `bootstrap_issue` lists every candidate as `{number, title, branch}`, with
  `branch` `null` when the issue has no branch. The listing `start` prints before exit 3 shows the
  same data, so the guide skill asks the user from `check --json` alone (FR-016).

  A checkout can still fail when the existing branch already commits the same files that carry
  over. The preview says so whenever there are carried-over files. If `git switch` fails, `start`
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
  `project-context.toml`; a `bootstrap_issue` with changes to tracked files outside the bootstrap
  files (untracked files do not count, research D19);
  a `pull_request` behind the default branch, whose `HEAD` does not contain the PR's head commit,
  or with nothing to commit, push or open; a `bootstrap_issue`, `pull_request` or `first_sync`
  whose `origin` (or, for `pull_request`, any push URL) names another repository than `gh` uses
  (D15); or a `first_sync` whose checkout is not the clean default branch tip (D12). Detection and
  `start` use one shared origin check, so `check --json` and `--confirm` agree on it. Known limit:
  `performable` does not yet reflect three stops `start` makes only when acting: the
  `github_settings` stop for a local `project-context.toml` that differs from the default branch's
  (D12), and the `pull_request` stops for a merge conflict and for a deleted bootstrap file (D7). "Behind" means local
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
    `first_sync` it includes the dry run's stdout in printed order, never its stderr. The blocks of
    `bootstrap_issue`, `pull_request` and `first_sync` include a `Repository: <owner/name>` line
    (`github_settings` prints one through `github`), so the digest binds the target repository
    without a separate input;
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
  the user did not see. Pending terminal input is discarded right before each prompt, so text typed
  before the preview appeared cannot answer it (D15).
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
  - The digest is computed only from state any caller can read, so a confirmation acts only when
    the state still matches the preview that produced the digest; it does not show that anyone saw
    the preview.
  - Two limits remain and are documented: the state is read again during the write (`pull_request`
    stages the files as they are then, `github --apply` plans again, a queued workflow runs later),
    so a change at exactly that moment is not previewed; and if the same user dispatches the sync
    workflow at the same time, `start` may watch the other run (D5).

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
  - `git status --porcelain` must be empty for every input the dispatched run reads:
    `ROADMAP.md`, `specs/`, `scripts/`, `.github/workflows/`, `project-context.toml` and
    `.specify/memory/constitution.md`, so the local dry run uses the same code and settings as the
    workflow. One list serves `check --json` and `start`.
  - `remote.origin.url` must be `https://github.com/<repo>` or `git@github.com:<repo>`, the forms
    the sync itself reads, and name the repository `gh` uses. This check runs before the dry run,
    so a bad origin never starts it (D15).

  Otherwise `start` stops with exit 1 and prints `git switch <default> && git pull --ff-only origin <default>` (D16), the
  paths to commit or stash, or a `git remote set-url origin` or `gh repo set-default` fix. In
  `check --json` these make the step not performable. A plain comparison of commit IDs needs no local copy of the tip, so
  a missing commit after a web merge needs no special case (S1). The preview therefore reads exactly
  what the dispatched workflow will check out (S4).
- **Preview**: `python3 scripts/sync-roadmap-issues.py`, a dry run with the arguments the workflow's
  apply will use: `--skip-project` when `board_credential` is not `done` (not configured, incomplete,
  or not checked), and the preview then says board changes are not previewed. The dry run uses the
  user's shell environment, so it can still differ from the workflow run. The output is printed
  in full. When the dry run exits non-zero, no apply is offered and the state stays `todo`:
  - If the output contains "Roadmap sync left blocked entries unchanged", the action is "fix the
    roadmap entries named above".
  - Otherwise the action is "the preview failed", with the output and the exit code. For example, a
    configured board credential makes the local dry run read the board with the user's own `gh`
    login, which may lack access.
- **Apply**: after confirmation, the engine lists the IDs of the user's earlier dispatched runs
  (`gh run list --workflow sync-roadmap-issues.yml --event workflow_dispatch --user <login>`), then
  runs `gh workflow run sync-roadmap-issues.yml -f apply=true`. It polls the same listing for up to
  60 seconds and takes the newest run whose ID is above the highest ID listed before the dispatch.
  This needs no clock comparison and skips the push-triggered preview run that the merge started
  (O2). Then it follows the run with `gh run view <id> --json status,conclusion,url` until it completes, with progress on stderr (D16; stdout since D19), and reports the result. Every `gh` command
  here passes `--repo`. If the run cannot be found, `start` says that the apply was already
  dispatched; after Ctrl-C while watching, it says the run continues on GitHub. Limit: if the same
  user dispatches the workflow at the same time, `start` may watch the other run.
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
- **The constitution's Sync Impact Report (FR-006a, clarification session 2026-11-18)**:
  `speckit-constitution` writes a Sync Impact Report as an HTML comment at the top of the
  constitution, as scratch notes to remove before the commit. When the step will commit the
  constitution (it is among the uncommitted bootstrap files), it removes that comment first:
  - **Match**: one well-formed leading comment, nothing but whitespace before `<!--`, the text
    `Sync Impact Report` between `<!--` and the first `-->` after it, and that `-->`. The comment,
    the rest of its closing line when that is only whitespace, and the blank lines after it are
    removed; the rest of the file is kept byte for byte. Anything else (no comment, a comment
    without that text, an unclosed comment, the text in a later comment) leaves the file unchanged.
  - **Preview**: a line `Will remove the Sync Impact Report comment at the top of
    .specify/memory/constitution.md before committing.`, printed before the commit line and only
    when the match applies. Being part of the preview block, it is covered by the digest (D4); the
    digest's content hash is of the file as it is on disk, before the removal, and the removal is
    deterministic, so `--confirm` recomputes the same value.
  - **Perform**: the file is rewritten before `git add`; a failed write stops with exit 2 before
    any git command. If a later part fails, a rerun finds the file without the comment and still
    uncommitted, so it only commits (FR-003).
  - **Already committed**: a comment in a constitution with no uncommitted changes is left alone,
    and the `pull_request` state rules (D2) do not look at it.
  - **Review**: the pre-PR review of the bootstrap files covers the constitution without the
    comment; the removal does not count as a change after the review, so the guide does not run the
    review again for it.
  - **Alternatives**: asking the user to remove it (the pilot showed this confuses adopters);
    removing it in a separate step before the review (one more confirmation for a fixed edit);
    treating a committed comment as unfinished work (changes the step's done rule for a case the
    review already shows).

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
- **Coding-agent choice** (FR-001a, issue #56): `details.agent_tools` reports the saved choice in
  every branch of the step (research D17); it never changes the step's state.

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
    `git switch <branch>`, D16), with `HEAD` not containing the PR's head commit or the branch's tip on
    GitHub (action `git pull --rebase origin <branch>`), or with nothing to commit, push or open;
  - `merge` `todo` or `waiting`;
  - `first_sync` when the checkout is not the clean default branch tip;
  - `bootstrap_issue`, `pull_request` or `first_sync` when `origin`, or a push URL for
    `pull_request`, names another repository than `gh` uses or is not a supported GitHub remote
    (D15);
  - `github_settings` once bootstrap is merged, when the local `project-context.toml` differs in
    its parsed values from the default branch's (action
    `git switch <default> && git pull --ff-only origin <default>`, D16), so the apply never uses an unmerged approval
    count;
  - `pull_request` with a merge conflict, or with a deleted or renamed bootstrap file (D7);
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
  preview block, so the digest covers them (D4). A given value that fails `init`'s validation (for
  example `--required-approvals -1`) exits 2 before any preview, in a terminal and with
  `--confirm` too, so no digest is ever shown for a value `init` would reject. `--name` and
  `--owner` keep their defaults from the repository, which are not decisions; when there is no
  GitHub `origin` to take the owner from, `--owner` is required, and `--name` falls back to the
  checkout directory's name. Missing flags are checked in `--confirm` runs even in a terminal. In a terminal,
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

## D15 — Repository checks and safe output (Phase 1 review and security pass)

- **Which repository**: `start` writes to the repository `gh repo view` reports (else `origin`) and
  passes it as `--repo` to every `gh` command it runs (`gh issue create`, `gh issue develop`,
  `gh pr create`, `gh workflow run`, `gh run list`, `gh run view`); `github --apply` uses it in its
  API paths. Git commands (`git fetch`, `git push`) use `origin`. So the steps that use both check
  that they agree, before any write:
  - `bootstrap_issue` and `pull_request` parse `origin` (`git remote get-url origin`, so
    `insteadOf` applies) with the toolkit's general parser, which accepts SSH host aliases and
    `ssh://` URLs. `pull_request` also checks every push URL (`git remote get-url --push --all
    origin`), because `git push` pushes to all of them. A remote that is not GitHub, or that names
    another repository (compared case-insensitively), stops with exit 1: `git remote -v` for a
    non-GitHub remote, never a rewrite of an SSH alias; `gh repo set-default <origin repo>` for a
    mismatch. An unreadable `origin` counts as not a GitHub remote (exit 1).
  - `first_sync` keeps the narrower check of the forms the sync itself reads (D5); an unsupported
    form gets a `git remote set-url origin` fix in the user's own scheme (SSH or HTTPS).
  - One shared implementation serves `start` and `check --json` (D4, `performable`).
- **Safe output**:
  - Branch and default-branch names in printed commands, including `check --json`
    `action.command`, are shell-quoted; bootstrap branch names are restricted (D3).
  - Text from GitHub, git and command output (titles, paths, branch names, stop messages, the
    sync's dry run, saved configuration, `check --json` strings) is printed with characters of
    Unicode category Cc, Cf, Zl, Zp and Cs and backslashes escaped, once, where it is printed or
    reported; backslashes are escaped so that escape text cannot be faked. A line feed inside a
    quoted value from GitHub, gh or git is shown as U+23CE, the return symbol, so the value stays
    on its line in `start` output and in `check --json` strings (a `\n` would have its backslash
    doubled by the one escaping). One leaf module,
    `scripts/display_text.py` (standard library only), defines this output set and the narrower
    set that init values reject, and `project_context.py`, `bootstrap.py`, `bootstrap_start.py`,
    `bootstrap_steps.py`, `bootstrap_github.py` and `bootstrap_check.py` use it. A command is
    never escaped: one that would contain such a character is left out with a note (issue #59).
  - The user-and-password part of a remote URL is masked (`***`) wherever a URL is shown: in
    `start`, in `check --json` and in the sync's own error message.
  - In a terminal, pending input is discarded right before each prompt, so type-ahead cannot
    answer a preview the user has not seen.
  - The hint for a renamed bootstrap file is `git mv -- <new> <old>`.
- **Rationale**: the `security-auditor` pass (T057) and the pilot walkthrough. These keep the
  confirmation's promise (D4): a yes acts on the repository and the preview the user saw, and
  nothing printed for copying can run more than it says.
- **Alternatives**: widening the sync's own `origin` regex to every form `parse_remote` accepts:
  not now, because four consumers share it; a separate digest input for the repository: not
  needed, since the preview line already binds it.

## D16 — Clearer `start` output (FR-009, FR-010a to FR-010f, FR-023; Phase 2 pilot, issue #51)

- **Decision**: the pilot's WS-4 and WS-5 runs showed silent waits, a next-step preview that read
  like the step just done, exit 2 after a successful step, and a board check that said "done" for a
  setting it cannot read. `start` changes as follows; stdout stays the contract the guide parses.
  - **Progress (FR-010c)**: a new `Env.progress(text)` writes one plain line to stderr (stdout since D19), after
    flushing stdout. `start` calls it before each state read (`Reading the repository and GitHub
    state…`) and at each slow part of a confirmed action (`Applying <n> GitHub settings…`,
    `Committing <n> files…`, `Pushing the branch…`, `Opening the pull request…`, `Creating the
    bootstrap issue…`), with the count of settings or files whenever it is known before the action
    starts. `check`, `check --json`, `init` and `github` print none (FR-014).
  - **First sync (FR-010c)**: `Starting the sync workflow on GitHub…` before `gh workflow run`,
    `Waiting for GitHub to start the run (up to 60 s)…` before `_find_run`, then `Run <id>: <url>`.
    The run is then followed with `gh run view <id> --repo <repo> --json status,conclusion,url`
    every `RUN_POLL` seconds instead of `gh run watch`, and `Still running (<m> min <s> s)…` is
    printed every 30 seconds on the injected clock. `completed` with conclusion `success` prints the
    existing success line; any other conclusion is the existing failure message with the URL.
    Interrupting keeps the existing "the run continues" message.
  - **Layout (FR-010d)**: each action's `perform` returns a one-line result (for example `Created
    issue #12 and branch 12-chore-bootstrap`, `Applied 15 GitHub settings`, `Opened pull request
    #13`, `The sync run 456 succeeded`); `start` prints `Done: <step>: <result>.`. A following
    preview starts with a blank line and `Next step (not done yet): <step>`, then the preview
    block, then `To do it, run:` and the full command on its own line: `python3
    scripts/bootstrap.py start --no-input --confirm <step>@<digest>`, plus `--issue N` where it
    applies and, for `project_context`, the init flags from the parsed arguments, each value
    shell-quoted. The `Confirm "…":` label and the `confirm:` line stay last. The candidate list
    uses the same `To do it, run:` form per candidate.
  - **`--guided` (FR-010b)**: without `--confirm` it is a usage error (exit 2) before any state is
    read. With it, `start` runs the `--confirm` checks and the action, and does not enter the
    driver loop. On success: `Done: <step>: <result>.`, `Done. Go back to your coding agent and
    reply "done".`, exit 0. Check (2) (bootstrap already complete) prints the completion line and
    the same "done" line, exit 0. Any other exit, including exit 3 from the candidate check (6),
    prints the usual message and then `Go back to your coding agent and tell it what happened.`.
    The digest and checks are unchanged, so it confirms nothing that `--confirm` alone would not.
  - **Missing init flags (FR-010a)**: in the driver loop (a plain run, or a run continuing after a
    confirmed step) without a terminal, `start` prints `Next step project_context needs your
    decisions: <flags>` on stdout with `To give them, run:` and the `start --no-input` command
    with placeholders, and exits 1. Check (5) of a `--confirm project_context@…` run keeps exit 2
    and the `needs:` line on stderr. `Env.err` flushes stdout first, so the lines keep their order.
    A given `init` flag whose value fails the shared validation (a text value, or a number such
    as a negative `--required-approvals`) is listed with the missing flags, followed by one `<flag>: <validation message>` line per invalid
    flag; the command shows its placeholder, never the value (issue #56). The shared validation in
    `project_context.py` rejects, in `name` and `description`, Unicode categories Cc, Zl and Zp and
    the bidi controls U+202A–U+202E and U+2066–U+2069 (they can make a preview read differently
    from what runs), and since issue #59 every other Cf character, but keeps zero-width joiners,
    zero-width non-joiners and soft hyphens, so emoji sequences stay valid and a confirming
    command never carries an invisible character.
    Rejecting them means a value never needs `$'…'` quoting, which plain `sh` lacks and bash 3.2
    renders wrongly for C1 characters and U+2028/U+2029.
  - **Completion (FR-010e)**: a blank line, then `BOOTSTRAP IS COMPLETE` or `BOOTSTRAP IS
    COMPLETE; MANUAL SETTINGS REMAIN (SEE ABOVE)`, wrapped in `\033[1m` … `\033[0m` only when stdout
    is a terminal and `NO_COLOR` is unset or empty (injected as `Env.bold`, so tests cover both).
  - **Git commands (FR-010f)**: every printed or run branch switch uses `git switch <branch>`
    (`bootstrap_issue`'s action and stops, the `pull_request` stop), and every pull names the remote
    and branch: `git switch <default> && git pull --ff-only origin <default>` (`github_settings` and
    `first_sync` stops, the `merge` action). `git switch` (Git 2.23, 2019) never takes a name as a
    path; with a branch only on `origin` it creates the tracking branch, as `git checkout` did.
    The guide's exact-match exception and its safety test needles follow the new text.
  - **Board workflow (FR-009, FR-023)**: enabled stays `done` with the title `Board workflow "Pull
    request linked to issue" is on; check that it sets Status to In review (the API cannot read
    it)`. `action` stays null, because data-model.md defines it as null for a `done` step and
    changing that would raise `format_version` (FR-013). The `github` preview's line becomes
    `Manual step (the API cannot read or set it): in <board> open Workflows and check that "Pull
    request linked to issue" is enabled with Status = In review; set it if not.`. The guide reads
    the `done` title and mentions the check once.
  - **Safe output**: every GitHub-sourced value in the new lines (the run URL, issue numbers,
    titles and branch names in a `Done:` result) goes through `_printable`, and the command after
    `To do it, run:` is built with `shlex.quote`, as in D15.
- **Rationale**: stderr keeps the preview, its digest and every parsed line unchanged, so the guide
  and the tests that pin stdout keep working; polling on the injected clock makes the heartbeat
  testable without threads or real waits; `--guided` is explicit because terminal detection fails
  under agent pseudo-terminals (D4); exit 1 is the existing "stopped, action printed" code that the
  guide already treats as normal progress, while exit 3 promises a `confirm:` line.
- **Alternatives**: a spinner (escape codes in agent output); passing `gh run watch` output through
  (a full-screen refresh in a terminal, noise in an agent); exit 3 for missing flags (no `confirm:`
  line follows); reporting the enabled workflow as `manual` or a new state (every bootstrap would
  end "with manual settings" forever, or `format_version` 2); `git checkout <branch> --` (still a
  checkout, and the `--` form is easy to drop when copying).

## D17 — Coding-agent choice at the prerequisites step (FR-001a, FR-023a; guide-me pilot, issue #56)

- **Decision**:
  - **Where it is saved**: Git config `agent-setup.tools` (local), which `agent_setup.py sync`
    already reads. "No choice" means the key is unset. The tools are `claude`, `copilot` and
    `codex` (`agent_setup.TOOLS`).
  - **Report**: `prerequisites` gets `details.agent_tools`, the saved names in `TOOLS` order, or
    `null` when unset, in every branch of the step, including a `gh` failure (the key is read
    locally). Saved names that `agent_setup` does not know are reported as saved; the agent-file
    check already fails on them. The step's state never depends on the choice. This is an added
    `details` key, so `format_version` stays 1 (FR-013).
  - **Interactive question**: an interactive `start` (a terminal, no `--no-input`, not `--guided`)
    asks once per run, right after its first state read, when `agent_tools` is `null`. It uses
    the same menu and parsing as `setup` (`agent_setup.ask_tools`): numbers or names, Enter = all
    three. Ctrl-C or end of input stops `start` like its other prompts, with nothing saved.
  - **`--agents <tool>,…`** (comma-separated names): validated before any state read, so an unknown
    or empty value exits 2 with the expected names and changes nothing. It can be combined with
    any other option and runs before them, in a terminal too, where it replaces the question.
    "All" is passed as the three names.
  - **Applying it**: `git config --local agent-setup.tools <tools>` and `python3
    scripts/agent_setup.py sync`, both through `env.run`, with the progress line `Generating the
    agent files…` on stderr (stdout since D19). Then on stdout: `Coding agents: <tools> (saved in
    git config agent-setup.tools).` and `Run npm run setup-agents again whenever you change coding
    agents, to generate the files for the new ones.`. `start` then reads the state again and
    continues. A failing sync exits 2 with its message; the saved choice stays, and a rerun syncs
    again.
  - **No preview or confirmation**: the choice changes only the local git configuration and
    git-ignored files, nothing that bypasses review (FR-004) or is committed. It does not change
    any step's preview or digest.
  - **No answer**: `start --no-input` without `--agents` neither asks nor saves. Prerequisites
    behaves as before: `done` when the files are up to date (`npm ci` generated all tools), or a
    stop with the `sync` action when they are missing.
  - **Hint**: `ALL_TOOLS_HINT` becomes `Generated sub-agents for all tools (Claude Code, GitHub
    Copilot, Codex). Optional: run npm run setup-agents to keep only the ones you use; run it
    again whenever you change coding agents.`. `setup` prints the same rerun line after it saves.
  - **Guide (FR-023a)**: when `agent_tools` is `null`, the guide asks once per session which agents
    the user uses and passes the answer as `python3 scripts/bootstrap.py start --no-input --agents
    <tools>`. In "guide me" mode the user runs that command, and the guide never adds `--guided`
    to it (`--guided` needs `--confirm`). The `<tools>` value must be a comma-separated subset of
    the three names; anything else is untrusted text, and the guide stops. With no answer, it goes
    on without asking again in that session.
- **Rationale**: `npm ci` generates all tools before the bootstrap starts, so asking only when files
  are missing would almost never ask. A saved "all" makes the question a one-time one. A `start`
  flag keeps the guide's allowed commands to one program.
- **Alternatives**: ask only when files are missing (rarely asked); a non-interactive `setup --tool`
  (a second program in the guide's allowed list); the guide running `git config` itself (a raw
  config write the safety tests cannot pin); a preview and digest for the choice (the change is
  local and reversible).

## D18 — One command to run, told when it applies (FR-009, FR-010d, FR-019a, FR-021; second guide-me pilot, issue #56)

- **Decision**:
  - **Manual board line**: `run_github` prints `Manual step (the API cannot read or set it): …`
    only after `--apply`, under `--check`, or when the plan has nothing left to change. A preview
    with changes leaves it out, because a new board gets its `In review` Status option from that
    apply. `start` takes the line from the apply's output and prints it after
    `Done: github_settings: …` (before the `--guided` ending), so a terminal user sees it too; the
    preview, and so the digest, no longer contain it.
  - **No command-like preview lines**: `preview_block` no longer adds the step action's
    `Command:` line (`start` performs the step; the stop output keeps its `Command:` line, because
    there the user acts). The `first_sync` preview drops the `Dry run:` line with the sync script
    command (the sync's own first line already says "dry run") and ends with `Will start the
    sync-roadmap-issues.yml workflow on GitHub with apply=true and follow the run.`. The digests of
    these previews change; a confirmation built from an older preview exits 4, as for any changed
    preview.
  - **Guide**: the preview it copies runs from the `Step` line to the line before `To do it, run:`.
    It never shows the engine's `To do it, run:` command, `Confirm "…":` label or `confirm:` line
    (it still checks the command against the `confirm:` value). It shows the one command in its own
    block, followed only by the line saying what to reply, and does not announce later steps.
  - **Board credential**: `gh secret set` reads the value from a terminal prompt; without one (an
    agent's shell, Claude Code's `!` prefix) it reads empty input and sets an empty secret, which
    the secret-name check cannot tell apart. The guide and docs say to run it in a separate
    terminal window or set the secret on the repository's Actions secrets page.
- **Rationale**: in the pilot the user saw two commands (the engine's and the guide's with
  `--guided`), nearly pasted the dry-run command, was told to check a Status option that did not
  exist yet, pasted an announcement into the terminal, and set an empty secret through `!`.
- **Alternatives**: the engine printing `--guided` itself (it cannot know the guide's mode);
  dropping `To do it, run:` and the `confirm:` line from `start` (the guide and terminal users
  without a guide rely on them); detecting an empty secret (values are never readable).

## D19 — Less to read and decide (FR-001a, FR-007, FR-008, FR-010c, FR-023b to FR-023d; pilots p3 and p4, issue #57)

- **Decision**:
  - **Streams**: `start` writes progress lines, the first sync's run URL and `Still running` lines,
    the `Coding agents:` lines and every preview to stdout; stderr carries only errors (a failed
    step, a usage error, a failed sync). The injected `Env.progress` writes to stdout, so tests keep
    asserting progress lines separately from the preview. The preview and the digest do not change.
    `check --json` keeps exit 1 for an incomplete bootstrap (known and accepted).
  - **`first_sync` summary**: the preview keeps the `Repository:` line and the words-only workflow
    line (D18), and replaces the copied dry run with a summary built from the sync's `[dry-run]`
    lines: `Will create <n> issues (<n> <kind>, …)`, where the kind is the created title's prefix up
    to its first `:` (for example `Feature`, `SPEC`, `Phase 1`), `<n> sub-issue links`, `<n> board
    items` (`add … to project <owner>/<number>`), and `<n> other changes` for the remaining
    `[dry-run]` lines, such as `set … Roadmap order` and `blocked by` links; a count of one uses the
    singular (`1 issue`, `1 sub-issue link`, `1 board item`, `1 other change`). The tests build the
    dry run by running the real sync against the fake GitHub, so the two cannot drift. `[skip]`
    lines and other non-`[dry-run]` stdout lines are kept in full, because they explain a block. The
    dry run's stderr warnings are printed once before the preview, now on stdout, and stay outside
    the digest (D4). `start --full-dry-run` adds the escaped dry run under the summary. The digest
    is computed from the full escaped dry run, with or without the flag, so the same confirmation
    works for both and any change to the dry run exits 4. A non-zero dry run still prints in full
    and offers no apply.
  - **Untracked files**: `bootstrap_issue` is not performable only when tracked files outside the
    bootstrap files have changes (`git status --porcelain` entries other than `??`); its action
    then says `Commit, stash or revert the changes to <paths>, then run start again.`. Untracked
    files stay in the title (`; untracked files, carried over and not committed: <paths>`) and in
    the preview, and do not block. A `git switch` that git refuses because the target branch
    tracks one of them still exits 2 with git's message (D3).
  - **`--agents` without `--confirm`**: after saving and syncing, `start` prints the `Coding
    agents:` and rerun lines and `Go back to your coding agent and tell it the choice is saved.`,
    then exits 0 without reading the state again or previewing the next step. With `--confirm`, or
    in an interactive terminal run (no `--no-input`), it continues as before (D17). The interactive
    question is unchanged.
  - **Board credential note once**: `start` no longer prints the non-blocking `board_credential`
    step when it passes it. It prints the step's `optional` title and doc link once, after
    `Done: github_settings: …` and the manual board line (before the `--guided` ending), when the
    credential is not `done`. The `github_settings` preview in `start` leaves out the
    `Board credential:` line (`bootstrap.py github` still prints it). `check` and `check --json`
    report the step as before. When
    `github_settings` was done earlier (by hand with `github --apply`, or before this change),
    `start` does not print the note; `check` and the guide still report it, and
    `docs/bootstrap.md` says so.
  - **Small output fixes**: a blank line after the preview's `Repository:` line, before the `Will …`
    lines; the `github_settings` preview drops `PHASE_SYNC_AUTO_APPLY: not set (information only)`
    and prints `PHASE_SYNC_AUTO_APPLY: set (Phase issues sync without a dry-run review)` only when
    the variable is set.
  - **Bootstrap review exception (FR-023c)**: AGENTS.md, `docs/review-and-approvals.md`,
    `docs/bootstrap.md#one-bootstrap-pull-request` and the guide skill state the bootstrap-only
    rule: round 1 fixes Critical and Required findings, and Optional and Nit findings where
    feasible, through the skills (`speckit-constitution`, `roadmap-write`); round 2 fixes only
    Critical and Required and drops the rest, which the agent names as dropped in its reply (the
    engine's `pull_request` step writes the pull request body, and the guide gets no `gh pr edit`);
    a fix that would change what the constitution or roadmap decides is asked of the user. The rule
    applies only to the pull request the `pull_request` step opens.
  - **Guide wording (FR-023b, FR-023d)**: the "not final" sentences in bold; one constitution call
    template ending with `Project: <description>. <add your own governance decisions, or ask for a
    first draft>`; no "draft" wording in the drafted file; no "reply done" after a skill run in the
    same chat; a yes/no approval question at the end of the constitution step with an offer to show
    the file; the credential question alone, as a two-way choice; the credential mentioned when the
    engine first reports it; when Prettier is not installed (no `npm ci`; the pre-commit hook that
    `npm ci` installs formats only YAML files, and no CI step formats), the guide says once that
    Prettier is available after `npm ci`, does not install it, and does not report the step as
    failed or unverified; the board workflow check named once; the completion summary says both
    files can change at any time, with the two skill calls indented under that line, then a separate
    `Next:` line that asks the user only to run `/speckit-specify` (`$speckit-specify` in Codex) for
    entry 001 on its SPEC issue branch; `check --json` is read only after the confirming command has
    returned.
  - **Leftover review items (issue #57, 21 to 29 and 31)**: `validate` raises `InvalidValue`, a
    `ValueError` subclass that carries the flag and the requirement, so `_reason` no longer slices
    the message (with tests for its fallbacks); the settings count comes from the same plan read as
    the preview's lines (`run_github` hands its plan back), not from counting lines or a second
    read; each `gh run view` gets a timeout so the `Still running` line comes at least every 30
    seconds, and a view that times out counts as still running (without the timeout, as in tests,
    the next view is assumed to take as long as the slowest so far); the `init` and `github`
    subparsers reject abbreviated options (`allow_abbrev=False`, FR-014's documented options keep
    working); singular forms in the progress lines and no `Applying 0 …` line; the `github` preview
    with only ruleset warnings keeps the manual board line; tests for `github --apply` with nothing
    to change and for a warnings-only preview; one blank-line rule before the manual line;
    `MANUAL_STEP` no longer doubles as a session key; the unused `argv` goes; long lines wrapped and
    the doubled "exit 2" wording dropped in `docs/bootstrap.md`; the heading-anchor rule from the p3
    pilot is already covered by T100 and is only rechecked.
- **Rationale**: in the pilots a successful run looked like a failure (stderr in red), the sync
  preview grew by 11 lines per entry, an untracked note blocked the issue step with no hint, the
  `--agents` run printed a confirming command the user should not run, the credential block was
  printed on every run, and the review left the user with up to 13 findings to rule on.
- **Alternatives**: a `--quiet` flag (still red by default); a summary without the full dry run
  (the user could not check it before an apply that bypasses review); `.gitignore` advice for
  untracked files (they cause no harm); a session file to print the credential note once (the
  engine keeps no record of finished steps, FR-002); skipping the bootstrap review (the
  constitution requires it).

## D20 — Seen before approved (FR-019a, FR-019b, FR-023b; guide-me pilot p5, issue #86)

- **Decision**:
  - **Preview in the agent's own message**: the guide copies the preview unchanged into a fenced
    code block in its own message and ends that message with the approval question in plain text;
    a structured-question dialog, when the agent has one, asks the same question after it. A
    preview that is only in tool output or in the dialog has not been shown, and a yes to it does
    not count. The plain-text question also keeps the coding agent's one-line reasoning summary
    from being the only question-like line above the dialog (finding 2).
  - **Exit-code wrapper**: the guide runs every `python3 scripts/bootstrap.py` command it runs
    itself (`check --json`, the `start --no-input` preview, `start --no-input --agents <tools>`,
    the confirming run in "do it for me" mode) as `<command>; echo "exit $?"`, and reads the exit
    code from the last output line. The suffix is fixed text: the guide checks the command (the
    line after `To do it, run:`, the `--agents` value) before adding it, and adds nothing else. A
    command shown to the user to run never gets it. The engine's exit codes and their contract do
    not change. The suffix assumes a POSIX shell (bash or zsh), as the guide's other commands
    already do; the toolkit names no other shell.
  - **`!` in Claude Code**: a command for the user is shown as `! python3 scripts/bootstrap.py …`,
    with a line saying that `!` runs it in this session and its output lands in the chat. Agents
    without `!` (Codex, Copilot) get the plain form and "Run this in your terminal:".
    `gh secret set` is never shown with `!` (D18).
  - **Skill calls that work unchanged**: the constitution call ends with
    `Project: <description>. Draft a first version.` (quoted since 2026-11-21, see the addendum)
    and the roadmap call asks for a first version with one entry; no `<placeholder>` inside either call. A line after the block says the user can
    add their own decisions at the end before sending it. This replaces the D19 template's
    placeholder.
  - **Constitution summary**: before the approval question, the guide's own message summarizes what
    the draft decides: scope, approval count, the sections by name and anything the skill inferred.
  - **No predicted steps**: after a command, the guide reads `check --json` again and says what is
    next from that state; it never names the next command before that read.
  - **Credential title (finding 8)**: the not-configured title reads
    `Board credential not configured (optional; organization secrets not checked)` when the
    organization's secrets could not be read, so it has one pair of parentheses.
- **Rationale**: in pilot p5 the user approved four previews and the constitution without seeing
  them (Claude Code folds tool output away), sent a placeholder to `roadmap-write` unchanged,
  pasted commands into the chat instead of a terminal, and saw every normal preview and
  `check --json` marked as a failure.
- **Alternatives**: an engine flag that exits 0 and prints the code (a new flag in the contract for
  one agent's display; rejected by the user); approvals in plain text only, without the dialog
  (loses the one-click choice); the placeholder on its own line after the block (still needs a
  manual edit before the call works).
- **Addendum (security audit, T129, 2026-11-21)**: the `security-auditor` pass on the final diff
  found no Critical, High or Medium issues; its Low and Info findings were fixed as follows.
  - **Output is not a reply**: the output of any command, including `!` output that Claude Code
    places in the user's turn, is never the user's reply; a yes, a choice or an instruction counts
    only when the user types it after the question (output can carry text from others, such as
    issue titles).
  - **Whole fenced blocks checked**: the guide test checks every line of a fenced block that holds
    a `python3 scripts/` command, so a valid command cannot be followed by another one.
  - **Fence length**: copied previews open with `~~~~` or with one more backtick than the longest
    backtick run in the copied text, so copied text cannot close the block early.
  - **No exit line**: when no `exit <n>` line with a number appears, the guide does not change or
    retry the command; it stops and tells the user the shell is not bash or zsh and that the guide
    needs one of them (no second run with the suffix, which fails the same way in fish).
  - **Description**: the skill calls quote the description
    (`Project: "<description from project-context.toml>". …`), and `project.description` is at
    most 350 characters (GitHub's repository description limit), checked by
    `scripts/project_context.py` and reported by `bootstrap.py` like any other invalid value, so a
    cloned repository cannot hand a long instruction text to the skill call. The quotes are a known
    limit, not a security boundary: they mark where the description starts and ends, and a `"`
    inside it ends them; what protects the user is the yes on the drafted file. Replacing or
    rejecting `"` was considered and not done, because the risk is small (the user writes and
    approves the description at bootstrap) and the rule would lengthen the skill.

## D21 — Fewer turns, findings in the pull request (FR-006b, FR-010b, FR-019, FR-019a, FR-021, FR-023 to FR-023c; guide-me pilot p6, issue #113)

- **Decision**:
  - **Review notes option**: `start` gets the flag `--review-notes` (no value). It reads the notes
    from one fixed file, `bootstrap-review-notes.md` at the repository root (the directory
    `git rev-parse --show-toplevel` prints, so it works in a worktree). The template's `.gitignore`
    ignores it with the entry `/bootstrap-review-notes.md`, so it is never committed or shown by
    `git status`; the engine checks this with `git check-ignore -q` and stops with exit 2, before any
    git command that changes something, when Git does not ignore the file (for example a project
    created before the entry existed, or an entry the user removed), naming the `.gitignore` line to
    add. The file must also be a regular file, not a symbolic link, so the engine never publishes a
    file outside the repository. The file is not in the Git directory because coding agents may ask
    before writing under `.git/`, or refuse (analyze remediation, spec session 2026-11-22). A fixed path means the engine never copies an
    arbitrary file the caller names into a public pull request body. The flag applies only to the
    `pull_request` step and only when that step creates the pull request; like the init flags, it is
    passed to both the preview run and the confirming run. The preview adds the line
    `Will add these review notes from <path> to the pull request body:` and then every notes line,
    indented as `  | <line>`, after the
    `Will open a pull request that closes #<issue>.` line, and the SHA-256 of the notes' bytes is
    part of the step's digest, so editing the notes after the preview gives exit 4. The body is
    `Closes #<issue>`, a blank line, then the notes as written. The notes stop the step with exit 2,
    before anything is changed (only read-only git commands run first), when the file is missing, empty after trimming whitespace, longer than
    60 000 characters (GitHub's body limit is 65 536; the margin leaves room for the `Closes` line)
    or 100 000 UTF-8 bytes (the body is one command-line argument, and Linux caps one argument at
    128 KiB; code review, issue #113),
    is not ignored by Git, is a symbolic link, looks like a credential (a `ghp_`, `gho_`, `ghu_`,
    `ghs_`, `ghr_` or `github_pat_` token, a `-----BEGIN … PRIVATE KEY-----` header, an AWS access
    key ID `AKIA` followed by 16 characters), has a line with a GitHub closing keyword followed by
    an issue reference (`close`, `closes`, `closed`, `fix`, `fixes`, `fixed`, `resolve`,
    `resolves`, `resolved`, optionally with a colon, then `#<n>`, `<owner>/<repo>#<n>` or an issue URL
    `https://[www.]github.com/<owner>/<repo>/issues/<n>`, optionally in angle brackets, any case),
    has an `@` mention anywhere (an `@` not preceded by a letter, digit or `.` and followed by a
    user or team name; an email address is accepted, and GitHub notifies a mention anywhere), or
    contains (a tab and a carriage return are named in the message) an output-unsafe character (the shared set from `scripts/display_text.py`, research
    D15); the message names the reason, never the content. When the pull request already exists, the
    flag changes nothing (the notes file is then not read) and the preview says `The pull request already exists; the review notes are
    not added.`; the guide then tells the user the notes are not in the body.
  - **Security audit (T136)**: the preview shows the notes text, not only a line count, because
    in "guide me" mode running the shown command is the approval and the agent writes the notes; a
    digest alone proves "these bytes", not "bytes the user saw". The credential, closing-keyword and
    mention checks keep a mistaken or injected line from leaking a secret, closing another issue on
    merge or notifying someone. The engine reads at most 100 001 bytes of the file. A directory or
    other non-regular file at the path is reported as not a regular file.
  - **What the guide writes there**: the dropped round-2 findings and the FYI findings of the
    bootstrap review, one line each with `file:line` where there is one, under the heading
    `## Review notes`; the chat names only their count. The guide writes the file with its own file
    tool, then runs the `pull_request` preview with `--review-notes`. When the review leaves no
    dropped or FYI findings, it writes no file and runs the step without the flag.
  - **Guided ending**: `GUIDED_DONE` becomes `Done. Go back to your coding agent.`; the failure line
    `Go back to your coding agent and tell it what happened.` stays. The guide adds
    "Then reply "done" here." only after a command for a separate terminal, never after `!`.
  - **Template `.gitignore`**: the entry `/bootstrap-review-notes.md` is added to the toolkit's
    `.gitignore`, which every project created from the template inherits; the bootstrap does not
    remove it.
  - **Merge action command**: `detect_merge` gives the `waiting` step the action text
    `Review and merge PR #<n> on GitHub, then run the command below.` with `action.command`
    `git switch <default> && git pull --ff-only origin <default>`, built the same way as the
    `first_sync` action (shell-quoted names). The guide shows it in the merge message; it stays a
    command reserved for the user. Only an existing null becomes a command, so `format_version`
    stays 1.
  - **Board credential command**: when no board credential is configured, the `board_credential`
    action carries `action.command` `gh secret set PROJECT_BOARD_TOKEN -R <owner>/<repo>`, with the
    repository `gh` resolves (shell-quoted), so a terminal in another repository cannot set the
    secret there and the guide never builds the command. The command prompts for the value; it is
    never run by the agent or through `!`. The incomplete-App case keeps a null command. Only an
    existing null becomes a command, so `format_version` stays 1.
  - **Guide mode confirmation**: in "guide me" mode the message holds the preview (after a bold
    `**Nothing has changed yet.**` line) and, after a bold `**If you approve, run this:**` line, the
    confirming command with `--guided`; there is no approval question and no separate yes turn. A
    change request means a new preview. "Do it for me" mode is unchanged: the command is shown with
    the preview and runs only after the user's yes.
  - **Skill wording**: every question or reply line that waits for the user and every safety warning
    in bold; no `check --json` field names or states in replies; the `gh secret set` command from
    the step's `action.command` (below), shown for a separate terminal with only the instruction,
    not the reason; a fixed bold announcement before
    the bootstrap review; round-1 fixable findings fixed without asking and reported in one line;
    decision findings in one multi-select question; the board workflow check in the first message
    after `github_settings`; amendments before the bootstrap pull request keep constitution
    version 1.0.0.
- **Rationale**: in pilot p6 every engine step took two turns (yes, then run), the user never needed
  the "reply done" line after `!` (the agent continued by itself every time), the review produced a
  long list of minor findings in the chat, the board workflow check was never named, internal field
  names reached the user, and an uncommitted constitution reached 1.0.1.
- **Alternatives**: the agent adds the notes as a PR comment or edits the body after `start` (a
  GitHub write outside a confirmed preview; rejected by the user); a `--review-notes PATH` value
  (lets a caller publish any readable file); dropping the success line entirely (a terminal user
  then gets no cue to go back); keeping the yes turn in "guide me" mode (running the command is
  already the user's act).

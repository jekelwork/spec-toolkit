# Research: Guided bootstrap

No open questions are left from the Technical Context. Each decision below records what was chosen,
why, and what was rejected.

## D1 — The human `check` stays as it is; `check --json` uses a new step catalogue

- **Decision**: Add `scripts/bootstrap_steps.py` with the 11-step catalogue (data-model.md).
  `check --json` and `start` use it. The human `check` keeps its six steps, its output and its exit
  codes. Where the steps overlap, the catalogue calls the existing `bootstrap_check` functions, so
  the logic exists once.
- **Rationale**: FR-014. If the human `check` used the new catalogue, a repository waiting for its
  pull request would exit 1 where it exits 0 today, which breaks the documented manual path.
- **Alternatives**: Move the human `check` onto the catalogue: rejected, because exit codes would
  change. A second, separate implementation: rejected, because it would duplicate logic.

## D2 — Progress is detected, never stored

- **Decision**: Each step's state comes from observable facts:

  | Step | Done when |
  | --- | --- |
  | `prerequisites` | `gh auth status` succeeds and lists the `project` scope; agent files are up to date |
  | `bootstrap_issue` | the current branch is `<n>-chore-bootstrap…` and issue `n` exists, or an issue with the bootstrap marker by the current user exists with its linked branch checked out |
  | `project_context` | `project-context.toml` is valid (existing check) |
  | `constitution`, `roadmap` | existing checks |
  | `github_settings` | `github --check` logic finds no drift |
  | `board_credential` | optional; secret names only (existing `report_credentials`) |
  | `board_workflows` | GraphQL `projectV2.workflows { name enabled }` |
  | `pull_request` | an open or merged PR exists from the bootstrap branch |
  | `merge` | `project-context.toml` exists on `origin/main` |
  | `first_sync` | every `planned` roadmap entry has its Epic (`roadmap_status` logic) |

- **Rationale**: FR-002 and clarification 1 (nothing stored). A repository bootstrapped before this
  feature, or by hand, is recognised automatically.
- **Alternatives**: A local state file: rejected, because it goes stale and breaks after a fresh
  clone or a hand-made step.

## D3 — Bootstrap issue identity: a marker plus the author, or the branch name

- **Decision**: Issues created by the engine carry a hidden `<!-- spec-toolkit:bootstrap -->`
  marker. Candidates are marker-bearing issues by the logged-in user, or the issue named by a
  current `<n>-chore-bootstrap…` branch. With more than one candidate, the engine asks (or, without
  a terminal, stops and lists them).
- **Rationale**: On a public repository anyone can open an issue that contains the marker; limiting
  candidates to the current user's own issues stops an outsider from steering the branch. The
  branch-name route covers issues created by hand, as documented today.
- **Alternatives**: A label, as the roadmap sync uses: rejected, because a fresh repository has no
  labels until `github --apply`, which runs later. Title matching: rejected as fragile.

## D4 — Confirmation model for people and agents

- **Decision**: In a terminal, `start` shows the preview and asks `y/N`. Without a terminal, `start`
  prints the preview and exits 3 ("confirmation needed"); the caller reruns
  `start --confirm <step-id>`, which acts only if that step is still the next step. See
  contracts/start-cli.md.
- **Rationale**: FR-004, FR-010 and FR-019. A confirmation is bound to one step, so a stale "yes"
  cannot approve a different change. The skill can relay the preview, get the user's yes in the
  chat, and run exactly that step.
- **Alternatives**: A global `--yes`: rejected, because it would approve every step at once,
  including changes that bypass review.

## D5 — First roadmap sync: local preview, applied by the workflow

- **Decision**: The preview runs `python3 scripts/sync-roadmap-issues.py` (a dry run) with the
  user's `gh` login and prints its output in place. After confirmation, the apply runs
  `gh workflow run sync-roadmap-issues.yml -f apply=true`, waits with `gh run watch`, and reports
  the result; `first_sync` is then detected from the Epics.
- **Rationale**: FR-007 needs a way to read the preview. Printing it in place avoids searching
  workflow logs. The apply stays on the documented, reviewed workflow path, which uses the board
  credential.
- **Alternatives**: A preview through the workflow (`apply=false`): rejected, because it adds
  polling and log parsing for the same information. A local apply: rejected, because it would
  diverge from the documented path and the board credential.

## D6 — Board built-in workflows: read by name, never block

- **Decision**: Extend the board query in `bootstrap_github.py` with
  `workflows(first: 20) { nodes { name enabled } }` and match the exact names "Pull request linked to
  issue" and "Auto-add sub-issues to project". A missing name or a query error gives `not_checked`.
- **Rationale**: Issue #4's idea, plus clarifications 3 and 4. The `project` scope already covers
  the read. Enabling a workflow is not possible through the API, so this step only reports.
- **Alternatives**: Infer the setting from board behaviour: rejected as unreliable.

## D7 — The pull request step commits only bootstrap files

- **Decision**: After `git fetch origin`, the step checks that the branch is not behind
  `origin/main` (if it is, the step stops and names the fix). It then shows
  `project-context.toml`, `.specify/memory/constitution.md` and `ROADMAP.md`, plus any other changed
  files listed separately and not committed, and the commit message. After a yes it commits those
  files, pushes, and runs `gh pr create` with `Closes #<issue>`.
- **Rationale**: FR-006, the edge case about unrelated changes, and the maintainer's rule to fetch
  `main` before a pull request.
- **Alternatives**: `git add -A`, as the current quick start does: rejected, because it could commit
  unrelated or secret files without the user seeing them.

## D8 — `check --json` versioning

- **Decision**: An integer `format_version` (1), plus the field rules in contracts/check-json.md
  (clarification 2). The format is documented for adopters in `docs/bootstrap.md` ("Machine-readable
  status"), because `specs/` is not published.
- **Alternatives**: Semantic versioning; no version: both rejected in clarify.

## D9 — How the skill is tested

- **Decision**: `scripts/test_bootstrap_guide_skill.py` extracts from `SKILL.md` every
  `python3 scripts/…` command, every `docs/bootstrap.md#anchor`, every `check --json` field and every
  step ID named, and asserts that each exists: the script and its subcommand, a matching heading,
  the documented field, the catalogue ID. It also asserts that the skill's known format version
  equals the engine's. A guard test shows each assertion failing on a deliberately broken copy.
  Walkthroughs WS-1 to WS-8 run on a scratch pilot repository (clarification 5).
- **Alternatives**: A scripted agent evaluation: out of scope (clarification 5), a candidate for a
  later roadmap entry.

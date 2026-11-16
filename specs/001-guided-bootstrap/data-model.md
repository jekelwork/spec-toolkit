# Data Model: Guided bootstrap

There is no stored data. These are the in-memory structures the engine builds on every run, and the
JSON shape follows from them (see [contracts/check-json.md](contracts/check-json.md)).

## Step

| Field | Type | Rule |
| --- | --- | --- |
| `id` | string | One of the catalogue IDs below; stable across versions |
| `title` | string | One line, for people |
| `state` | enum | `done`, `todo`, `manual`, `optional`, `waiting`, `not_checked` |
| `blocking` | bool | Fixed per step (catalogue); whether it must be `done` for the bootstrap to be complete |
| `actor` | enum | `engine`, `user` (terminal or coding agent), `user_web` (GitHub web page) |
| `needs_decision` | bool | The user must decide something (description, approvals, board, constitution, roadmap; visibility is shown with its warning, research D14) |
| `needs_confirmation` | bool | Acting needs an explicit yes after a preview (FR-004, FR-006, FR-019) |
| `performable` | bool | Computed per run: `start --confirm <id>` would act now. Always false when `needs_confirmation` is false (research D4) |
| `bypasses_review` | bool | Acting changes GitHub or the default branch outside pull request review |
| `action` | object or null | `text`, `command` (string or null), `doc` (a `docs/bootstrap.md#anchor`); null when `done` |
| `details` | object | Step-specific facts; the keys named in format version 1 are listed in contracts/check-json.md |

`blocking`, `actor`, `needs_decision`, `needs_confirmation` and `bypasses_review` are fixed per step
and do not depend on the run; `performable` does. For `project_context`, `bypasses_review` is true
because the step may create a board.

**States**:

- `done`: finished.
- `todo`: work remains.
- `manual`: a to-do on a web page that does not block (clarification 4). Only on non-blocking steps.
- `optional`: may be skipped (board credential not configured). Only on non-blocking steps.
- `waiting`: blocked on something outside the engine (an open PR).
- `not_checked`: could not be read. It blocks when the step is blocking (research D11).

So a blocking step is always `done`, `todo`, `waiting` or `not_checked`, and the `result` rule below
covers every case.

## Status report

| Field | Type | Rule |
| --- | --- | --- |
| `format_version` | int | 1 |
| `result` | enum | `incomplete` if any blocking step is `todo`, `waiting` or `not_checked`; else `complete_with_manual_settings` if any step is `manual`; else `complete` |
| `next_step` | string or null | ID of the first blocking step that is not `done`; null when none. `start --confirm` accepts only this ID (research D4) |
| `bootstrap_merged` | bool or null | The three bootstrap files are on the default branch (research D2). `null` when that could not be read (an error other than 404) |
| `default_branch` | string or null | The repository's default branch; `null` when it could not be read |
| `steps` | list of Step | Always all 11, in catalogue order |

## Step catalogue (order is fixed)

| # | `id` | blocking | actor | needs_decision | needs_confirmation | bypasses_review |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `prerequisites` | yes | user | no | no | no |
| 2 | `bootstrap_issue` | yes | engine | no | yes | yes |
| 3 | `project_context` | yes | engine | yes | yes | yes |
| 4 | `constitution` | yes | user | yes | no | no |
| 5 | `roadmap` | yes | user | yes | no | no |
| 6 | `github_settings` | yes | engine | no | yes | yes |
| 7 | `board_credential` | no | user | no | no | no |
| 8 | `board_workflows` | no | user_web | no | no | no |
| 9 | `pull_request` | yes | engine | no | yes | no |
| 10 | `merge` | yes | user_web | no | no | no |
| 11 | `first_sync` | yes | engine | no | yes | yes |

How each state is detected is in research D2 (all steps), D3 (`bootstrap_issue`), D5
(`first_sync`) and D10 (`prerequisites`).

**Transitions**: every step is recomputed on every run, so there is no stored transition. A step can
move back (for example `github_settings` from `done` to `todo` if someone edits the ruleset), and
`start` then handles it again. Once `bootstrap_merged` is true, steps 2, 4, 5, 9 and 10 stay `done`
whatever the local branch or files are. Step 3 stays `done` while the default branch's
`project-context.toml` is valid. Step 11 is `done` while every `planned` entry has its issues, so a
newly planned entry makes it `todo` until the next apply (research D5).

**Guide mode** is not stored (clarification 1).

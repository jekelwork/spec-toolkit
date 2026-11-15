# Data Model: Guided bootstrap

There is no stored data. These are the in-memory structures the engine builds on every run, and the
JSON shape follows from them (see [contracts/check-json.md](contracts/check-json.md)).

## Step

| Field | Type | Rule |
| --- | --- | --- |
| `id` | string | One of the catalogue IDs below; stable across versions |
| `title` | string | One line, for people |
| `state` | enum | `done`, `todo`, `manual`, `optional`, `waiting`, `not_checked` |
| `actor` | enum | `engine`, `user` (terminal or coding agent), `user_web` (GitHub web page) |
| `needs_decision` | bool | The user must decide something (description, visibility, approvals, constitution, roadmap) |
| `bypasses_review` | bool | Acting changes GitHub or `main` outside pull request review |
| `action` | object or null | `text`, `command` (string or null), `doc` (a `docs/bootstrap.md#anchor`); null when `done` |
| `details` | object | Step-specific facts, for example the board workflows' enabled flags |

**States**:

- `done`: finished.
- `todo`: blocking; the first `todo` is the next step.
- `manual`: a non-blocking to-do on a web page (clarification 4).
- `optional`: may be skipped (board credential).
- `waiting`: blocked on something outside the engine (an open PR, a running workflow).
- `not_checked`: could not be read; never blocking on its own.

## Status report

| Field | Type | Rule |
| --- | --- | --- |
| `format_version` | int | 1 |
| `result` | enum | `incomplete` if any step is `todo` or `waiting`; else `complete_with_manual_settings` if any step is `manual`; else `complete` |
| `next_step` | string or null | ID of the first `todo` or `waiting` step |
| `steps` | list of Step | Always all 11, in catalogue order |

## Step catalogue (order is fixed)

| # | `id` | actor | needs_decision | bypasses_review | Blocking? |
| --- | --- | --- | --- | --- | --- |
| 1 | `prerequisites` | user | no | no | yes |
| 2 | `bootstrap_issue` | engine | no | yes (issue, branch) | yes |
| 3 | `project_context` | engine | yes | yes when a board is created | yes |
| 4 | `constitution` | user (skill) | yes | no | yes |
| 5 | `roadmap` | user (skill) | yes | no | yes |
| 6 | `github_settings` | engine | no | yes | yes |
| 7 | `board_credential` | user / user_web | no | no | no (`optional`) |
| 8 | `board_workflows` | user_web | no | no | no (`manual`) |
| 9 | `pull_request` | engine | no | no (commit and push need a yes, FR-006) | yes |
| 10 | `merge` | user_web | no | no | yes (`waiting` while the PR is open) |
| 11 | `first_sync` | engine | no | yes | yes |

**Transitions**: every step is recomputed on every run, so there is no stored transition. A step can
move back (for example `github_settings` from `done` to `todo` if someone edits the ruleset), and
`start` then handles it again.

**Guide mode** is not stored (clarification 1).

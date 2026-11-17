# Contract: `bootstrap.py check --json` (format version 1)

The command is read-only: it changes nothing locally or on GitHub, and does not fetch. Its only
local sync run is the first-sync dry run, used for `details.pending_changes` (research D5). A
duplicate roadmap issue, an unparsable `ROADMAP.md`, conflicting Phase markers or a failing `gh`
call make `first_sync`
`not_checked`; they never end the report early.

On stdout it prints exactly one JSON document; diagnostics go to stderr. Field meanings are in
[data-model.md](../data-model.md).

## Example

```json
{
  "format_version": 1,
  "result": "incomplete",
  "next_step": "constitution",
  "bootstrap_merged": false,
  "default_branch": "main",
  "steps": [
    {
      "id": "project_context",
      "title": "project-context.toml is valid",
      "state": "done",
      "blocking": true,
      "actor": "engine",
      "needs_decision": true,
      "needs_confirmation": true,
      "performable": false,
      "bypasses_review": true,
      "action": null,
      "details": {}
    },
    {
      "id": "constitution",
      "title": "Constitution is missing (.specify/memory/constitution.md)",
      "state": "todo",
      "blocking": true,
      "actor": "user",
      "needs_decision": true,
      "needs_confirmation": false,
      "performable": false,
      "bypasses_review": false,
      "action": {
        "text": "Run the speckit-constitution skill, giving it the approval count (0).",
        "command": null,
        "doc": "docs/bootstrap.md#constitution"
      },
      "details": {"required_approvals": 0}
    },
    {
      "id": "board_workflows",
      "title": "Board workflow \"Pull request linked to issue\" is off",
      "state": "manual",
      "blocking": false,
      "actor": "user_web",
      "needs_decision": false,
      "needs_confirmation": false,
      "performable": false,
      "bypasses_review": false,
      "action": {
        "text": "Open the board's Workflows, enable \"Pull request linked to issue\" and set Status to In review.",
        "command": null,
        "doc": "docs/bootstrap.md#github-settings"
      },
      "details": {
        "workflows": {
          "Pull request linked to issue": false,
          "Auto-add sub-issues to project": true
        }
      }
    }
  ]
}
```

(The example is shortened; a real report always lists all 11 steps. In `details.workflows`, a
value is `true`, `false` or `null` for "not readable". Other `details` keys named in this version:
`project_scope` on `prerequisites`; `candidates` on `bootstrap_issue` (a list of
`{number, title, branch}`, `branch` `null` when the issue has no branch); `ruleset_warning` on
`project_context` and `github_settings`;
`required_approvals` on `constitution`; the earlier PR on `pull_request`; failing checks on
`merge`; and `pending_changes` on `first_sync`, an integer, or `null` when the dry run was not run
or failed.)

## Rules

- `format_version` is an integer. It is raised when, and only when, a field is removed or its
  meaning changes. Adding fields, step `details` keys, or values inside `details` does not raise it.
  Adding a value to `state`, `actor` or `result`, or a step to the catalogue, does raise it.
- Callers MUST ignore fields they do not know, and MUST stop when `format_version` is higher than
  the one they support.
- `steps` always holds every catalogue step, in catalogue order. Step `id`s never change within a
  version.
- A caller MUST get the user's explicit yes before acting on a step with `needs_confirmation: true`,
  and MUST let the user decide when `needs_decision` is true.
- `start --confirm` acts only on `next_step`, and only when that step has `needs_confirmation: true`
  and `performable: true` (contracts/start-cli.md). A caller asks the user for a yes only when
  `performable` is true. When `start` would stop on the repository checks (research D15), the step
  is `performable: false`, its `title` ends with `; <the stop>` and its `action` carries the stop's
  text and command. `performable` does not yet reflect three stops `start` makes only when acting
  (research D4): the `github_settings` local-file stop and the `pull_request` merge-conflict and
  deleted-file stops.
- `title`, `action.text` and `action.command` never contain the user-and-password part of a remote
  URL; it is shown as `***`. Branch and default-branch names in `action.command` are shell-quoted.
- `details.pending_changes` is information only: it never changes a step's `state` or `result`.
- `action.command`, when not null, is a command line the user can run as is from the repository
  root. It never contains a secret.
- `details` never contains a secret value; for credentials it holds only which secret names exist.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | `result` is `complete` or `complete_with_manual_settings` |
| 1 | `result` is `incomplete` |
| 2 | No report could be produced (message on stderr, nothing on stdout) |

A `gh` that is missing or logged out does not give exit 2: the GitHub steps are `not_checked`, with
the reason in `title` and the fix in `action`, and `result` is `incomplete`. The human `check` keeps
its own exit codes (1 on error).

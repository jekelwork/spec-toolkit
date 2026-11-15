# Contract: `bootstrap.py check --json` (format version 1)

The command is read-only: it changes nothing locally or on GitHub. On stdout it prints exactly one
JSON document; diagnostics go to stderr. Field meanings are in [data-model.md](../data-model.md).

## Example

```json
{
  "format_version": 1,
  "result": "incomplete",
  "next_step": "constitution",
  "steps": [
    {
      "id": "project_context",
      "title": "project-context.toml is valid",
      "state": "done",
      "actor": "engine",
      "needs_decision": true,
      "bypasses_review": true,
      "action": null,
      "details": {}
    },
    {
      "id": "constitution",
      "title": "Constitution is missing (.specify/memory/constitution.md)",
      "state": "todo",
      "actor": "user",
      "needs_decision": true,
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
      "actor": "user_web",
      "needs_decision": false,
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

(The example is shortened; a real report always lists all 11 steps.)

## Rules

- `format_version` is an integer. It is raised when, and only when, a field is removed or its
  meaning changes. Adding fields, step `details` keys, enum values in `details`, or steps at the end
  does not raise it. (Adding a value to `state`, `actor` or `result` changes their meaning, so it
  raises the version.)
- Callers MUST ignore fields they do not know, and MUST stop when `format_version` is higher than
  the one they support.
- `steps` always holds every catalogue step, in catalogue order. Step `id`s never change within a
  version.
- `action.command`, when not null, is a command line the user can run as is from the repository
  root. It never contains a secret.
- `details` never contains a secret value; for credentials it holds only which secret names exist.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | `result` is `complete` or `complete_with_manual_settings` |
| 1 | `result` is `incomplete` |
| 2 | No report could be produced (message on stderr, nothing on stdout) |

A `gh` that is missing or logged out does not give exit 2: GitHub steps are `not_checked`, with the
reason in `title` and the fix in `action`.

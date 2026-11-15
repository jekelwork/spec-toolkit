# Contract: `bootstrap.py start`

```text
python3 scripts/bootstrap.py start [--confirm STEP_ID] [init flags]
```

- **No options, in a terminal**: works through the steps from the first unfinished one. For each
  step that changes something, it prints the preview and asks `Proceed? [y/N]`; anything but `y`
  stops with exit 1 and changes nothing. It continues to the next step after success, and stops at a
  step the user must do (`actor` `user` or `user_web`), printing the action and its doc link.
- **Without a terminal** (stdin is not a TTY): never prompts. At a step that needs confirmation, it
  prints the preview and exits 3.
- **`--confirm STEP_ID`**: performs that one step without asking, and only if it is still the next
  step. Otherwise it exits 2 with `next step is <id>, not <STEP_ID>`, and changes nothing.
- **`init` flags** (`--description`, `--required-approvals`, `--project-number`, `--create-board`,
  `--name`, `--owner`) are passed to the `project_context` step; a missing value without a terminal
  stops with a message naming the flag (FR-010).

## What each step does when confirmed

| Step | Action |
| --- | --- |
| `bootstrap_issue` | `git fetch origin`; requires a clean working tree on an up-to-date `main`; `gh issue create` (generic `CHORE:` title, bootstrap marker); `gh issue develop <n> --base main --name <n>-chore-bootstrap --checkout` |
| `project_context` | the existing `init` (with board creation if chosen) |
| `github_settings` | the existing `github --apply` (never `--allow-weaken`) |
| `pull_request` | fetch and refuse if behind `origin/main`; commit the three bootstrap files with the shown message; `git push -u origin HEAD`; `gh pr create` with `Closes #<issue>` |
| `first_sync` | the preview is the local dry run of `sync-roadmap-issues.py`, printed in full before the prompt (or before exit 3); the confirmed action is `gh workflow run sync-roadmap-issues.yml -f apply=true`, then `gh run watch` |

Steps with `actor` `user` or `user_web` are never performed; `start` explains them.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Bootstrap complete (or complete with manual settings open) |
| 1 | Stopped before completion: a step the user must do, a `waiting` step, or a declined prompt |
| 2 | Error, or `--confirm` does not match the next step |
| 3 | Confirmation needed (no terminal); rerun with `--confirm <step-id>` |
| 130 | Interrupted |

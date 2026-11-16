# Contract: `bootstrap.py start`

```text
python3 scripts/bootstrap.py start [--confirm STEP_ID] [init flags]
```

- **No options, in a terminal**: works through the steps from the first unfinished one.
  - At a step with `needs_confirmation`, it prints the preview and asks `Proceed? [y/N]`. Anything
    but `y` stops with exit 1 and changes nothing.
  - At an `optional` or `manual` step, it prints the step and its action, then continues (research
    D12).
  - It stops at a blocking step the user must do (`constitution`, `roadmap`), at a `waiting` step
    (`merge`) and at a blocking `not_checked` step, printing the action and its doc link.
- **Without a terminal** (stdin is not a TTY): never prompts. At the first step that needs
  confirmation, it prints the preview and exits 3. It stops at the same points as above.
- **`--confirm STEP_ID`**: performs that one step without asking, only if it is still the next step
  needing confirmation. Otherwise it exits 4 with `next step is <id>, not <STEP_ID>` and changes
  nothing.
- **`init` flags** (`--description`, `--required-approvals`, `--project-number`, `--create-board`,
  `--name`, `--owner`) are passed to the `project_context` step. A missing value without a terminal
  stops with a message naming the flag (FR-010).

The trust model of `--confirm` is in research D4: it cannot prove that a person said yes.

## What each step does when confirmed

| Step | Action |
| --- | --- |
| `bootstrap_issue` | `git fetch origin`; requires `main` up to date with `origin/main` and a working tree whose only changes are bootstrap files (they carry over to the new branch); otherwise it stops and names the other files. Then `gh issue create` (generic `CHORE:` title, bootstrap marker) and `gh issue develop <n> --base main --name <n>-chore-bootstrap --checkout` |
| `project_context` | the existing `init` (with board creation if chosen) |
| `github_settings` | the existing `github --apply` (never `--allow-weaken`) |
| `pull_request` | `git fetch origin`; stops if the branch is behind the default branch; commits the bootstrap file set (research D7) with the shown message; `git push -u origin HEAD`; `gh pr create` with `Closes #<issue>` |
| `first_sync` | the preview is the local dry run with the workflow's arguments (research D5), printed in full before the prompt or before exit 3; a non-zero preview offers no apply. The confirmed action is `gh workflow run sync-roadmap-issues.yml -f apply=true`; the engine then finds the run it dispatched and runs `gh run watch <id> --exit-status` |

Steps with `actor` `user` or `user_web` are never performed; `start` explains them.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Bootstrap complete (or complete with manual settings open) |
| 1 | Stopped before completion: a blocking user step, a `waiting` step, a blocking `not_checked` step, or a declined prompt |
| 2 | Error (message on stderr) |
| 3 | Confirmation needed (no terminal); rerun with `--confirm <step-id>` |
| 4 | `--confirm` does not match the next step needing confirmation; nothing changed |
| 130 | Interrupted |

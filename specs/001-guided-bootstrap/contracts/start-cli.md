# Contract: `bootstrap.py start`

```text
python3 scripts/bootstrap.py start [--no-input] [--confirm STEP_ID@DIGEST [--issue N]] [init flags]
```

`<default>` below is the repository's default branch, read from GitHub (research D2).

- **No options, in a terminal**: works through the steps from the first unfinished one.
  - At a step with `needs_confirmation`, it prints the preview and asks `Proceed? [y/N]`. Anything
    but `y` stops with exit 1 and changes nothing.
  - At any non-blocking step that is not `done` (`optional`, `manual`, `not_checked`), it prints the
    step and its action, then continues.
  - At any other blocking step that is not `done`, and that `start` does not perform, it stops with
    exit 1. It prints the action and its doc link. Research D12 lists these stops.
- **Without a terminal, or with `--no-input`**: never prompts. At the first step that needs
  confirmation, it prints the preview and exits 3. It stops at the same points as above.
  `--no-input` exists because some agent tool runners execute commands in a pseudo-terminal. The
  guide skill always passes it.
- **Preview line**: every preview, in a terminal or before exit 3, ends with
  `confirm: <step-id>@<digest>`. The digest covers the preview and its inputs (research D4).
- **`--confirm STEP_ID@DIGEST`**: performs that one step without asking, then continues as without
  options, so the next confirmation again exits 3 when there is no terminal or with `--no-input`.
  The checks run in this order; the first that applies decides, and nothing changes unless step 8
  is reached:
  1. The value is not `<step-id>@<12 hex characters>` (for example a bare `pull_request` with no
     digest, `pull_request@` or a non-hex digest): exit 2, usage error.
  2. `next_step` is null (bootstrap complete): exit 0, "bootstrap is complete".
  3. `STEP_ID` is not `next_step`, or `next_step` has `needs_confirmation: false`: exit 4,
     `next step is <next_step>, not <STEP_ID>` or `next step <next_step> needs no confirmation`.
  4. The step is not `performable` on this run: exit 1, printing the action.
  5. For `project_context`, a required `init` flag is missing (research D13): exit 2.
  6. For `bootstrap_issue` with more than one candidate, `--issue` is missing or not a candidate:
     list the candidates with their previews and exit 3.
  7. The digest does not match the recomputed one: exit 4, "the preview changed since you
     confirmed it; run `start` again to see the new one".
  8. Act.

  Each transition has a fake-run test.
- **`--issue N`**: with `--confirm bootstrap_issue@<digest>`, picks issue `N` when several
  bootstrap issue candidates exist (research D3). Each listed candidate has its own preview and
  `confirm:` line, and the digest includes `N`. Without `--issue`, or with a number that is not a
  candidate (also when there is exactly one candidate and it is not `N`), `start` lists the
  candidates and exits 3 without a terminal or with `--no-input`; in a terminal it asks. It is
  ignored for other steps.
- **`init` flags** (`--description`, `--required-approvals`, `--project-number`, `--create-board`,
  `--name`, `--owner`) are passed to the `project_context` step and checked only when that step
  will run (the file is missing). Without a terminal or with `--no-input`:
  - `--description`, `--required-approvals`, and one of `--project-number` or `--create-board` are
    required. If one is missing when `start` reaches `project_context`, it exits 2, names the
    flags, and prints no preview and no digest (research D13).
  - The caller reruns `start --no-input <init flags>` to get the exit-3 preview, then confirms
    with `--confirm project_context@<digest>` plus the same flags.
  - The flag values are part of the preview block, so changing them between preview and
    confirmation exits 4.

  In a terminal, the approval count prompt offers 0, which the user must accept. The preview shows
  every value that will be written.

`--confirm` recomputes the digest before acting. A missing or malformed digest exits 2 (check 1).
One that no longer matches because a file, a flag value, a commit or a branch tip changed since the
preview exits 4 and changes nothing. In a terminal,
the digest is computed before the `y/N` prompt and again after the yes; a change in between exits 4.

The trust model of `--confirm` is in research D4: it cannot prove that a person said yes, and it
guards against mistakes, not against someone who has the GitHub rights.

## What each step does when confirmed

| Step | Action |
| --- | --- |
| `bootstrap_issue` | Requires a working tree whose only changes are bootstrap files; otherwise stops and names the others. With several candidates, asks which one to use (in a terminal) or uses `--issue N`. Then, by the situation in research D3: `git checkout <branch>` (after `git fetch origin <branch>` if it is only on GitHub); or `gh issue develop <n> --base <default> --name <n>-chore-bootstrap --checkout`; or `gh issue create` (generic `CHORE:` title, bootstrap marker) followed by that `gh issue develop`. A failed `git checkout` (for example when the branch already commits the carried-over files, which the preview warns about) exits 2 with git's message; it never stashes |
| `project_context` | The existing `init` (with board creation if chosen) for a missing file. An existing invalid file is never overwritten; `start` stops with the validation error. The preview includes the visibility and plan warning (research D14) |
| `github_settings` | The existing `github --apply` (never `--allow-weaken`) |
| `pull_request` | Stops if the branch is behind the default branch tip (local `HEAD` does not contain the tip SHA that GitHub reports; research D4), or if the current branch is not the PR's head branch (action `git checkout <branch>`), or local `HEAD` does not contain the open PR's head commit or, with no PR, the branch's tip on GitHub (action `git pull --rebase origin <branch>`; research D2, D7). Then only the missing parts (research D7): commit the bootstrap file set with the shown message, `git push -u origin HEAD`, `gh pr create` with `Closes #<issue>` |
| `first_sync` | Requires `HEAD` to equal the default branch tip on GitHub (`gh api repos/{repo}/branches/<default> --jq .commit.sha`), and no uncommitted changes in `ROADMAP.md` or `specs/`; otherwise stops with exit 1 and prints the fix. The preview is the local dry run with the workflow's arguments (research D5), printed in full before the prompt or before exit 3. A non-zero preview offers no apply. The confirmed action is `gh workflow run sync-roadmap-issues.yml -f apply=true`; the engine then finds the run it dispatched and runs `gh run watch <id> --exit-status` |

Steps with `actor` `user` or `user_web` are never performed; `start` explains them.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Bootstrap complete (or complete with manual settings open) |
| 1 | Stopped before completion at a blocking step `start` does not perform (research D12), a `--confirm` for a step that is not `performable` now, or a declined prompt |
| 2 | Error, a malformed or missing `--confirm` digest, or a required `init` flag missing without a terminal (message on stderr) |
| 3 | Confirmation or a choice needed (no terminal, or `--no-input`); rerun with `--confirm <step-id>@<digest>` from the preview, plus `--issue N` when bootstrap issue candidates are listed |
| 4 | `--confirm` names a step other than `next_step`, `next_step` needs no confirmation, or the digest no longer matches the preview; nothing changed |
| 130 | Interrupted |

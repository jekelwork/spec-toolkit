# Contract: `bootstrap.py start`

```text
python3 scripts/bootstrap.py start [--no-input] [--agents TOOL,…] [--confirm STEP_ID@DIGEST [--issue N] [--guided]] [init flags]
```

`<default>` below is the repository's default branch, read from GitHub (research D2).

- **Coding agents** (FR-001a, research D17): when no choice is saved (`details.agent_tools` is
  `null`), an interactive run that is not `--guided` asks `Which coding agents do you use?` with
  the `setup` menu after its first state read; Enter saves all three. `--agents TOOL,…` (names
  `claude`, `copilot`, `codex`) gives the choice without asking; an unknown or empty value exits 2
  before any state is read. Either way `start` saves it as `agent-setup.tools`, runs `agent_setup.py
  sync`, prints `Coding agents: <tools> (saved in git config agent-setup.tools).` and `Run npm run
  setup-agents again whenever you change coding agents, to generate the files for the new ones.`,
  then continues. No preview or confirmation; previews and digests do not change. Without a
  terminal or with `--no-input` and no `--agents`, nothing is asked or saved.

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
- **Output layout** (FR-010d, research D16): after a performed step, `Done: <step>: <result>.`. A
  preview that follows starts with a blank line and `Next step (not done yet): <step>` (a preview
  with no step done before it starts there too), then the preview block. Before exit 3 it then
  prints `To do it, run:` and, on its own line, the full confirming command `python3
  scripts/bootstrap.py start --no-input --confirm <step-id>@<digest>` with `--issue N` where it
  applies and, for `project_context`, the init flags, shell-quoted. The `Confirm "…":` label and
  the `confirm:` line stay last. A candidate list prints `To do it, run:` and the command per
  candidate.
- **Progress** (FR-010c, research D16): plain lines on stderr while `start` reads state or acts
  (`Reading the repository and GitHub state…`, `Applying <n> GitHub settings…`, `Pushing the
  branch…`, and the first sync's phases and `Still running (<elapsed>)…` at least every 30 s).
  stdout is flushed before each stderr line. stdout, the preview and the digest do not change
  because of them.
- **Completion** (FR-010e): a blank line, then `BOOTSTRAP IS COMPLETE` or `BOOTSTRAP IS COMPLETE;
  MANUAL SETTINGS REMAIN (SEE ABOVE)`, bold only when stdout is a terminal and `NO_COLOR` is unset
  or empty.
- **`--guided`** (FR-010b): only with `--confirm`; without it, exit 2 before any state is read. It
  runs checks 1 to 8 below and does not continue to the next step. When the step is performed, or
  check 2 finds the bootstrap complete, it ends with `Done. Go back to your coding agent and reply
  "done".` and exit 0. Any other exit ends with `Go back to your coding agent and tell it what
  happened.` after the usual message. The guide skill adds it only to the confirming commands it
  shows in "guide me" mode.
- **`--confirm STEP_ID@DIGEST`**: performs that one step without asking, then (unless `--guided`) continues as without
  options, so the next confirmation again exits 3 when there is no terminal or with `--no-input`.
  The checks run in this order; the first that applies decides, and nothing changes unless step 8
  is reached:
  1. The value is not `<step-id>@<12 hex characters>` (for example a bare `pull_request` with no
     digest, `pull_request@` or a non-hex digest): exit 2, usage error.
  2. `next_step` is null (bootstrap complete): exit 0, with the completion line (FR-010e).
  3. `STEP_ID` is not `next_step`, or `next_step` has `needs_confirmation: false`: exit 4,
     `next step is <next_step>, not <STEP_ID>` or `next step <next_step> needs no confirmation`.
  4. The step is not `performable` on this run, or the step's own stop applies (for example an
     `origin` that names another repository, research D15): exit 1, printing the action.
  5. For `project_context`, a required `init` flag is missing (research D13): exit 2. This is
     checked in a terminal too.
     5b. A given `init` flag value fails validation: exit 2, naming the flag and the validation
     message, never the value.
  6. For `bootstrap_issue`, in a terminal too: with more than one candidate and no `--issue`, or
     with an `--issue N` that is not a candidate (also when the single candidate is another
     number), list the candidates with their previews and exit 3; with zero candidates, `--issue N`
     exits 3 and asks to rerun without it.
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
    required. If one is missing when `start` reaches `project_context` on a plain run or on a run
    continuing after a confirmed step, it prints `Next step project_context needs your decisions:
    <flags>` on stdout, prints no preview and no digest, and exits 1 (FR-010a, research D13, D16).
    The list names the missing flags and any given `init` flag whose value fails validation (for
    example a negative `--required-approvals`); each invalid one gets a line
    `<flag>: <validation message>`, and the
    `To give them, run:` command carries its placeholder, never the value. A `--confirm
    project_context@…` run with a missing flag exits 2 (check 5); with no flag missing, an invalid
    value exits 2 (check 5b).
  - `project.name` and `project.description` must be a non-empty single line: characters of
    Unicode category Cc, Zl or Zp, and the bidirectional-text controls U+202A–U+202E and
    U+2066–U+2069, are rejected (the same rule for `init`, `start` and `project_context.py`);
    zero-width joiners and soft hyphens are allowed.
  - The caller reruns `start --no-input <init flags>` to get the exit-3 preview, then confirms
    with `--confirm project_context@<digest>` plus the same flags.
  - The flag values are part of the preview block, so changing them between preview and
    confirmation exits 4.

  In a terminal, the approval count prompt offers 0, which the user must accept. The preview shows
  every value that will be written. A given value that fails validation exits 2 before any preview.
  `--owner` is required when there is no GitHub `origin` to take it from.

In a terminal, pending input is discarded right before each prompt (research D15). The previews of
`bootstrap_issue`, `pull_request` and `first_sync` include a `Repository: <owner/name>` line
(`github_settings` prints one through `github`), which the digest covers. `start` passes that
repository as `--repo` to `gh issue create`, `gh issue develop`, `gh pr create`, `gh workflow run`,
`gh run list` and `gh run view`; its `gh api` reads carry it in the path.

`--confirm` recomputes the digest before acting. A missing or malformed digest exits 2 (check 1).
One that no longer matches because a file, a flag value, a commit or a branch tip changed since the
preview exits 4 and changes nothing. In a terminal,
the digest is computed before the `y/N` prompt and again after the yes; a change in between exits 4.

The trust model of `--confirm` is in research D4: it cannot prove that a person said yes, and it
guards against mistakes, not against someone who has the GitHub rights.

## What each step does when confirmed

| Step | Action |
| --- | --- |
| `bootstrap_issue` | Requires a working tree whose only changes are bootstrap files; otherwise stops and names the others. With several candidates, asks which one to use (in a terminal) or uses `--issue N`. Then, by the situation in research D3: `git switch <branch>` (after `git fetch origin <branch>` if it is only on GitHub); or `gh issue develop <n> --base <default> --name <n>-chore-bootstrap --checkout`; or `gh issue create` (generic `CHORE:` title, bootstrap marker) followed by that `gh issue develop`. A failed `git switch` (for example when the branch already commits the carried-over files, which the preview warns about) exits 2 with git's message; it never stashes. Before any of these, stops with exit 1 when `origin` is not a GitHub remote or names another repository than `gh` uses (research D15) |
| `project_context` | The existing `init` (with board creation if chosen) for a missing file. An existing invalid file is never overwritten; `start` stops with the validation error. The preview includes the visibility and plan warning (research D14) |
| `github_settings` | The existing `github --apply` (never `--allow-weaken`). Once bootstrap is merged, stops with exit 1 and `git switch <default> && git pull --ff-only origin <default>` when the local `project-context.toml` differs in its parsed values from the default branch's (research D12) |
| `pull_request` | Stops if the branch is behind the default branch tip (local `HEAD` does not contain the tip SHA that GitHub reports; research D4), or if the current branch is not the PR's head branch (action `git switch <branch>`), or local `HEAD` does not contain the open PR's head commit or, with no PR, the branch's tip on GitHub (action `git pull --rebase origin <branch>`; research D2, D7). Also stops when `origin` or any push URL is not a GitHub remote or names another repository than `gh` uses, on a merge conflict, or on a deleted or renamed bootstrap file (research D12, D15). Then only the missing parts (research D7): commit the bootstrap file set with the shown message, `git push -u origin HEAD`, `gh pr create` with `Closes #<issue>`. When the constitution is committed and starts with a well-formed Sync Impact Report comment, the preview says `Will remove the Sync Impact Report comment at the top of .specify/memory/constitution.md before committing.` on the line before the commit line, and the step removes the comment before `git add` (research D7, FR-006a) |
| `first_sync` | Requires `HEAD` to equal the default branch tip on GitHub (`gh api repos/{repo}/branches/<default> --jq .commit.sha`), no uncommitted or untracked changes in `ROADMAP.md`, `specs/`, `scripts/`, `.github/workflows/`, `project-context.toml` or `.specify/memory/constitution.md`, and an `origin` in a form the sync reads that names the repository `gh` uses; otherwise stops with exit 1 and prints the fix. The preview is the local dry run with the workflow's arguments (research D5), printed in full before the prompt or before exit 3. A non-zero preview offers no apply. The confirmed action is `gh workflow run sync-roadmap-issues.yml -f apply=true`; the engine then finds the run it dispatched (the newest ID above those listed before the dispatch) and follows it with `gh run view <id> --repo <repo> --json status,conclusion,url` every few seconds until it completes, printing `Run <id>: <url>` and a `Still running (…)…` line on stderr at least every 30 s; a conclusion other than `success` fails the step (research D16) |

Steps with `actor` `user` or `user_web` are never performed; `start` explains them.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Bootstrap complete (or complete with manual settings open), or with `--guided` the confirmed step was performed |
| 1 | Stopped before completion at a blocking step `start` does not perform (research D12), a `--confirm` for a step that is not `performable` now, a declined prompt, or `project_context` waiting for missing or invalid init flags outside a `--confirm project_context` run when at least one is missing (FR-010a) |
| 2 | Error, a malformed or missing `--confirm` digest, a required `init` flag missing in a `--confirm project_context` run, an `init` flag value that fails validation (with no flag missing outside a `--confirm` run), `--guided` without `--confirm`, an unknown or empty `--agents` value, or a failed agent-file sync after a choice (message on stderr) |
| 3 | Confirmation or a choice needed (no terminal, or `--no-input`); run the command printed after `To do it, run:` (`--confirm <step-id>@<digest>`, plus `--issue N` when bootstrap issue candidates are listed) |
| 4 | `--confirm` names a step other than `next_step`, `next_step` needs no confirmation, or the digest no longer matches the preview; nothing changed |
| 130 | Interrupted |

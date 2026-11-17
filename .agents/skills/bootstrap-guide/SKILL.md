---
name: bootstrap-guide
description: Guide the user through bootstrapping a project created from the toolkit template, as a conversation. Use when the user asks to bootstrap, set up or finish setting up the project, or asks what the next bootstrap step is.
---

# Guide the bootstrap

You conduct the bootstrap as a conversation with the user. You hold no copy of the steps, their order
or their explanations: the engine says where the user is, and `docs/bootstrap.md` says why and how.
Read the state again after every step, because the repository and GitHub are the only record of
progress.

## Where the facts come from

- **State**: only from `python3 scripts/bootstrap.py check --json`. It is read-only. Use the step
  that `next_step` names, and each step's `title`, `state`, `actor`, `action`, `details`
  and the flags below. Never infer the state from files or from memory of an earlier run.
- **Explanations**: only from `docs/bootstrap.md`. Each step's `action` (null for a done step) has an
  `action.doc` anchor: open that section. The guided path is described in
  [Guided path with start](../../../docs/bootstrap.md#guided-path-with-start), the exit codes in
  [Terminal, `--no-input` and exit codes](../../../docs/bootstrap.md#terminal---no-input-and-exit-codes), and
  the report in [Machine-readable status](../../../docs/bootstrap.md#machine-readable-status).
- **Format version**: this skill knows format version 1. If `format_version` is higher than that,
  stop. Tell the user to update the toolkit files, and do not interpret the output. Ignore fields
  you do not know when the version is equal.

## At the start of every session

Ask which mode the user wants, even if an earlier session used one; nothing stores it:

- **guide me**: the user runs each command that changes something; you show it, explain it and
  read the result.
- **do it for me**: you run the commands that change something, after the confirmations below.

In both modes you run the read-only commands yourself: `check --json` and the `start --no-input`
preview. Only commands that change something follow the mode.

The user may switch at any point. The mode decides who types the command. It never decides who
makes a decision or who approves a change.

## The loop

1. Run `python3 scripts/bootstrap.py check --json` and apply the version rule above. If it exits
   with 2, there is no report on stdout: show the message from stderr and stop.
2. If `next_step` is `null`, the bootstrap is complete. Report `result`: with
   `complete_with_manual_settings`, name the steps still in state `manual` and their `action`.
3. Otherwise take the step `next_step` names and handle it by its `actor` and flags. The cases
   apply in order; the first that matches wins:
   - `steps[].performable` is `true` and `steps[].needs_confirmation` is `true`: go to
     [Acting on a step](#acting-on-a-step). For an engine step with `steps[].needs_decision` set
     to `true` (`project_context`), first collect the decision values (the init flags) from the
     user, then act; the user does not run `init` separately.
   - `steps[].actor` is `user` or `user_web`, or an engine step is not `performable`: the user acts
     or resolves it. Show `title`, `action.text` and, if not `null`, `action.command`. Explain it
     from the section `action.doc` names. Do not ask for the `start --confirm` yes, because `start`
     does not act on this step; wait until the user says it is done, then read the state again.
     - Before running any command yourself (in "do it for me" mode) other than `check --json` or
       a `start --no-input` preview, show it and get an explicit yes for that command. This is a
       per-command yes, separate from the `start --confirm` yes. It covers the commands in an
       `action`, such as `git pull`, `git checkout` or `git fetch`. In "guide me" mode the user
       runs them.
     - Never run `python3 scripts/bootstrap.py github --apply` or any `--allow-weaken` command
       yourself. A `github_settings` title containing "to decide" is the user's decision
       about the approval count (lower the ruleset or raise `required_approvals`); present both
       options and let the user choose and act.
4. Non-blocking steps (`steps[].blocking` is `false`) never hold up the flow: mention a step that
   is `manual` or `optional` once, with its `action`, and continue. Board credentials are optional.

## Decisions that stay with the user

In both modes, never choose or suggest a value on the user's behalf for: the project description,
repository visibility, the approval count, the constitution's content and the roadmap's content.
Ask the user for each, and take the approval count's meaning from
[`project-context.toml`](../../../docs/bootstrap.md#project-contexttoml).

- For `constitution` and `roadmap`, tell the user to run the `speckit-constitution` and
  `roadmap-write` skills and give them the facts `details` holds (for the constitution,
  `details.required_approvals`). Do not write either yourself.
- For `project_context`, collect `--description`, `--required-approvals` and either
  `--project-number` or `--create-board` from the user (and `--owner` or `--name` when asked), as
  described in
  [The `project_context` step and the init flags](../../../docs/bootstrap.md#the-project_context-step-and-the-init-flags).
  Pass the same flags to the preview run and to the confirming run; a changed flag gives exit 4.

## Acting on a step

Ask for the `start --confirm` yes only when the step's `steps[].performable` is `true`. Never ask
for it before.

1. Show the preview by running
   `python3 scripts/bootstrap.py start --no-input` (with the init flags when the step is
   `project_context`). Always pass `--no-input`, even when the tool gives you a terminal. Exit 3
   means a preview and a `confirm:` line were printed. Show the preview to the user unchanged: do
   not shorten, reword or summarise it.

   If the preview run exits with anything other than 3, do not ask for a yes, and do not go on to
   step 2. Show its output (stderr for exit 2). A `needs:` line names the init flags still missing:
   collect them from the user and run the preview again. A printed action is the user's to-do, as
   in the second case of the loop: wait until the user has dealt with it, then run `check --json`
   again. The report can still show the step as performable when the run stops for a reason it
   does not reflect. Exit 0 means bootstrap is complete.

2. Say what changes, and for a step with `steps[].bypasses_review` set to `true`, that it takes
   effect without a pull request review. Every commit, push and pull request is also confirmed
   this way. Ask for an explicit yes.
3. After the yes, in "do it for me" mode run
   `python3 scripts/bootstrap.py start --no-input --confirm <step-id>@<digest>`, with exactly
   the flags the preview's "To proceed, rerun with" line names, the value of its `confirm:` line,
   and the same init flags. With several bootstrap issues the preview instead prints a "To use #N,
   rerun with" line per candidate, which includes `--issue N`; use the one for the chosen issue.
   In "guide me" mode, show that command and let the user run it. Never construct a digest
   yourself.
4. Read the exit code (see
   [Terminal, `--no-input` and exit codes](../../../docs/bootstrap.md#terminal---no-input-and-exit-codes)):
   - 4: the preview changed or the step is no longer next. Nothing changed. Run
     `python3 scripts/bootstrap.py start --no-input` again, show the new preview unchanged, and ask
     again. The earlier yes does not carry over. If that run exits with anything other than 3,
     follow step 1's rule for that case.
   - 3: the next step needs its own confirmation: go back to step 1 with it, or run the loop.
   - 1 or 2: after a confirmed step the run goes on to the next step, and under `--no-input` it
     exits 1 where it stops at a step it does not perform. That is normal progress, not a
     failure of the confirmed step; exit 2 can also follow a partly done step. Do not tell the
     user the step failed before reading the state.
   - 0: bootstrap is complete.

   After exit 1, 2, 3 or 4, run `python3 scripts/bootstrap.py check --json` again and tell the user
   what happened from that state, then continue the loop. Show any message from the run, and do not
   retry the same command unchanged.
   Details of `--confirm` are in [`--confirm`](../../../docs/bootstrap.md#--confirm).

### Choosing the bootstrap issue

When the `bootstrap_issue` step is next and `details.candidates` lists more than one issue, show the
list (number, title, branch) and ask which one to use. Pass the choice as `--issue N` to the
confirming run; each candidate has its own `confirm:` line. See
[Bootstrap issue](../../../docs/bootstrap.md#bootstrap-issue).

## Board settings

When `board_workflows` is `manual`, guide the user through enabling the board's built-in "Pull
request linked to issue" workflow in the board's Workflows and setting its Status to `In review`;
[GitHub settings](../../../docs/bootstrap.md#github-settings) explains why. This is a web setting, so the
user does it. Never ask the user to change "Auto-add sub-issues to project".

## Board token

The `board_credential` step is optional; the bootstrap works without it, so offer to skip it and
carry on. If the user wants board sync, explain from
[Board credentials](../../../docs/bootstrap.md#board-credentials) which credential fits. For the
classic token (the secret `PROJECT_BOARD_TOKEN`), the scopes are `project`, plus `repo` for a
private repository or `public_repo` for a public one, and a set expiry. The user creates the token
and must set the secret themselves: in their own terminal with `gh secret set PROJECT_BOARD_TOKEN`,
which prompts for the value, or on the repository's secrets page on GitHub's web page. You never
ask for the value and never run that command yourself.

Confirm only from secret names. Run `check --json` again and read `details.secrets`: it covers
organization-level secrets when the engine can read them (the step's summary says so when it
cannot) and both App secret names. The user's `gh secret list` (or
`gh secret list --org ORG` for an organization's secrets) is only a cross-check. Secret values are
never read, printed or passed to any command. The step is done when its state is `done` and
`details.secrets` names the credential; one App secret alone leaves it `optional`.

## If a token is pasted into the chat

If the user pastes something shaped like a token (`gh` plus one letter and `_`, such as `ghp_`,
`gho_`, `ghu_`, `ghs_` or `ghr_`, or `github_pat_`, followed by many characters), or a private key,
stop the bootstrap flow at once. Never use the value: do not put it in any command, file or
environment variable, do not store it and do not repeat it back. Treat it as leaked, because the
chat may be logged or kept. Tell the user, in your own words, to do these steps outside the chat:
revoke the token (or, for an App's private key, generate a new key and then delete the leaked one
right away) on GitHub, create a new token where one is needed, and
set it again as a secret in their own terminal or on the web page. The steps, including the App
private key and the GitHub CLI's own token, are in
[Revoke and replace a leaked token](../../../docs/bootstrap.md#revoke-and-replace-a-leaked-token).
Suggest that the user delete the message containing the value from the chat history, if their tool
allows it. Continue the loop only after the user says the old token is revoked and the new secret
is set, and then confirm it from `details.secrets` as above.

## Questions

When the user asks about a step, a message or an exit code, answer from `docs/bootstrap.md` and the
engine's output, then continue from the same step. If the docs do not answer it, say so; do not
invent an explanation.

## Neutrality

Name no adopter, maintainer or repository in anything you show; use the repository's own details
from the engine's output.

# Feature Specification: Guided bootstrap

**Feature Branch**: `4-spec-001-create-and-approve-spec-and-plan`

**Created**: 2026-11-15

**Status**: Draft

**Input**: `ROADMAP.md` entry 001; user description: "SPECIFY_FEATURE_DIRECTORY=specs/001-guided-bootstrap — ROADMAP.md entry 001 (Guided bootstrap): a conversational, resumable bootstrap: a `bootstrap.py start` engine with machine-readable status, and a `bootstrap-guide` skill that walks the user through it in their coding agent. Planning issue idea: the board's built-in workflows can be read (not enabled) through the API, so the status report can say whether 'Pull request linked to issue' and 'Auto-add sub-issues to project' are on and the guide can tell the user what to toggle."

## Clarifications

### Session 2026-11-15

- Q: When the adopter resumes the bootstrap in a new agent session, where should the chosen guide mode come from? → A: Not stored; the guide asks again at the start of each new session.
- Q: How should the `check --json` output be versioned? → A: One integer `format_version`, raised only when a field is removed or its meaning changes; added fields need no new version; a caller that sees a higher version than it knows stops and says so.
- Q: How should the status report and the guide treat the board's built-in "Auto-add sub-issues to project" workflow? → A: Information only: reported as on, off or not readable, with no action and no effect on completion; only "Pull request linked to issue" is a manual to-do step.
- Q: When the board's "Pull request linked to issue" workflow is readable and turned off, should the engine still report the bootstrap as complete? → A: Non-blocking to-do: listed as a manual step, the flow continues to the pull request and first sync, and the result reads "complete, with manual settings open" until it is on.
- Q: How should the guide skill be tested, given that its conversational behaviour cannot be unit-tested? → A: An automated reference test plus a manual walkthrough on a scratch pilot repository after each Phase, recorded in the pull request; the walkthrough is written as numbered scenarios (starting state, user input, expected result) so they can later become automated evaluation cases; a scripted agent evaluation is out of scope here and is a candidate for a later roadmap entry.

### Session 2026-11-18

- Q: Should the bootstrap pull request review run again because the `pull_request` step removes the constitution's Sync Impact Report comment after the review? → A: No. The review covers the constitution without that comment, and the engine removing a leading Sync Impact Report comment does not count as a file change after the review.
- Q: If the constitution was already committed on the bootstrap branch with the Sync Impact Report comment in it, does the `pull_request` step remove it too? → A: No. Only when the step commits the constitution itself (it has uncommitted changes); an already committed comment is left alone and does not change the step's state.
- Q: What does the step do when the constitution starts with an unclosed comment that mentions "Sync Impact Report"? → A: Leaves the file unchanged: only a well-formed leading comment (only whitespace before it, containing "Sync Impact Report", ended by `-->`) is removed, with the blank lines after it; anything else gets no removal and no preview line.
- Q: When the board's "Pull request linked to issue" workflow is enabled, which state does `board_workflows` get, given that the API cannot read the Status it sets? → A: It stays `done` (so `result` can still be `complete`), but its title and action say to check that the workflow sets Status to `In review` because the API cannot read it; the `github` preview's manual line says "check or set"; no new state and no `format_version` change.
- Q: When `start` stops at `project_context` because init flags are still missing, which exit code does it use? → A: Exit 1, a normal stop, with a stdout line naming the decisions needed, both for a plain `start --no-input` and for a run that continues after a confirmed step; exit 2 stays for `--confirm project_context@…` itself with missing flags and for invalid flag values; stdout is flushed before anything is written to stderr.
- Q: How does the `--guided` flag behave? → A: It requires `--confirm` (without it: exit 2, usage error); it performs only the confirmed step and previews no next step; on success it prints `Done: <step>: <result>.` and `Done. Go back to your coding agent and reply "done".` and exits 0; when the step is not performed (exit 1, 2 or 4) it prints the usual message and then `Go back to your coding agent and tell it what happened.`.
- Q: Do the other output changes proposed in issue #51 and its comments apply as written (progress lines on stderr, the "Done / Next step" layout, first-sync progress, the completion line, `origin` named in the pull command, `git switch` for branch switches)? → A: Yes, as the issue states them; the first-sync "still running" line is printed at least every 30 s.
- Q: Since a done step's action is defined as empty, where does the enabled board workflow's "check the Status" reminder go? → A: In its title only; the action stays empty, so the output format version stays 1. (Recommended option, applied under the user's instruction to resolve new findings with the recommendation while away.)
- Q: Which ending does a `--guided` run print when it ends with exit 3 (the bootstrap issue candidate list) or exit 0 because the bootstrap was already complete? → A: Exit 0 (complete) prints the completion line and the "reply done" line; every other exit, including 3, prints the "tell it what happened" line. (Recommended option, applied under the same instruction.)
- Q: Should the project name and description also reject Unicode format characters, which can make a preview look different from what runs in some terminals? → A: Reject only the bidirectional-text controls (U+202A–U+202E, U+2066–U+2069); zero-width joiners and soft hyphens stay allowed so emoji sequences work (issue #56).
- Q: When `start` lists a given init flag whose value is invalid among the decisions needed, how does it say why? → A: One line per such flag with the shared validation message (`--description: must be …`), never the value (issue #56).
- Q: The progress lines in the contract carry counts (`Applying <n> GitHub settings…`, `Committing <n> files…`) but the engine prints none; which side changes? → A: The engine prints the counts when they are known before the action starts; the contract stays (issue #56).

## User Scenarios & Testing *(mandatory)*

The adopter is a developer who has just pressed "Use this template" (or run `gh repo create
--template`) and cloned the new repository. Today they follow `docs/bootstrap.md` by hand: about a
dozen commands across the terminal, their coding agent and the GitHub web pages, with no single
place that knows which step comes next once they stop halfway. This feature gives them one
resumable sequence of steps (the engine) and a conversation in their coding agent (the guide) that
drives it.

Delivery is in two Phases, as the roadmap entry states: Phase 1 is the engine (User Stories 1 and
2), Phase 2 is the guide skill (User Stories 3 and 4).

### User Story 1 - Resumable bootstrap from the terminal (Priority: P1)

An adopter runs one command, `bootstrap.py start`, in the fresh clone. It works out which step the
repository has reached, explains that step, and either carries it out (after a preview and a "yes"
where the step changes something) or tells the user exactly what they must do themselves (for
example run the constitution skill). The adopter can stop at any point, close the terminal, come
back days later, run `start` again and continue from the first unfinished step, until the bootstrap
pull request is merged and the first roadmap sync has been applied.

**Why this priority**: The engine is what both the terminal user and the guide skill rely on. On its
own it already replaces the manual checklist and delivers the roadmap outcome (a merged bootstrap
pull request and a first roadmap sync) for adopters who prefer the terminal.

**Independent Test**: On a scratch repository created from the template, run `start` repeatedly,
stopping after different steps, until the bootstrap pull request is merged and the first roadmap
sync is applied; every rerun resumes at the first unfinished step and never repeats or undoes a
finished one.

**Acceptance Scenarios**:

1. **Given** a fresh clone with no bootstrap issue, **When** the adopter runs `start`, **Then** it
   offers to create the `CHORE:` bootstrap issue and its linked branch, shows what it will create,
   and creates them only after confirmation.
2. **Given** the bootstrap branch exists and `project-context.toml` is missing, **When** the adopter
   runs `start`, **Then** it runs the `init` step, asking for the description, the approval count
   and the board choice (existing board or a new one), warns when the repository's visibility and
   GitHub plan do not support rulesets, and writes the validated file.
3. **Given** `project-context.toml` exists but the constitution or `ROADMAP.md` is missing, **When**
   the adopter runs `start`, **Then** it stops and names the skill the adopter must run in their
   coding agent (with the approval count to give it), and does not write those files itself.
4. **Given** the local files are complete and the GitHub settings differ, **When** the adopter runs
   `start`, **Then** it shows the `github` preview and applies it only after an explicit yes.
5. **Given** everything up to the pull request is done, **When** the adopter confirms, **Then** the
   engine opens the bootstrap pull request linked to the bootstrap issue; after the adopter merges
   it, the next `start` offers the first roadmap sync as a preview first and applies it only after
   the adopter has seen the preview result and confirmed.
6. **Given** the adopter stopped after any step, **When** they run `start` again in a new terminal
   session, **Then** it resumes at the first unfinished step without asking again for answers that
   are already recorded in the repository or on GitHub.
7. **Given** the bootstrap is finished, **When** the adopter runs `start`, **Then** it reports that
   bootstrap is complete and points to starting the first feature; if the board's "Pull request
   linked to issue" workflow is still off, it reports "complete, with manual settings open" and
   names that action, without stopping earlier steps for it.

---

### User Story 2 - Machine-readable bootstrap status (Priority: P1)

A coding agent (or a script) runs `bootstrap.py check --json` and gets the whole bootstrap state in
a stable, versioned, machine-readable form: every step, its state, who must act (the engine, the
user, or the user on a GitHub web page), whether acting needs a user decision or an explicit yes,
and the next action. The existing human-readable `check` output keeps working.

**Why this priority**: The guide skill reads the process only from this output and the docs, so it
cannot be built without it; it also lets other tools build on the engine without parsing prose.

**Independent Test**: Run `check --json` on repositories in several known states (fresh, partly
bootstrapped, finished, `gh` logged out) and compare the output with the documented format; a test
fails when a documented field is missing or the format version changes without the documentation
changing with it.

**Acceptance Scenarios**:

1. **Given** any repository state, **When** `check --json` runs, **Then** it prints one JSON document
   that names its format version, lists every bootstrap step in order with its state, and names the
   next step and its action, and it changes nothing locally or on GitHub.
2. **Given** `gh` is not installed or not logged in, **When** `check --json` runs, **Then** the GitHub
   steps are reported as not checked with the reason, and the local steps are still reported.
3. **Given** the board exists, **When** `check --json` runs, **Then** it reports whether the board's
   built-in "Pull request linked to issue" and "Auto-add sub-issues to project" workflows are
   enabled, or that this could not be read; only "Pull request linked to issue" comes with a manual
   action to enable it.
4. **Given** a step that needs a user decision or bypasses pull request review, **When** it is
   reported, **Then** the step says so, so a caller knows it must ask the user before acting.

---

### User Story 3 - Conversational guide in the coding agent (Priority: P2)

The adopter opens their coding agent in the fresh clone and says "help me bootstrap" (or invokes
the `bootstrap-guide` skill). The guide asks whether they want to be guided step by step ("guide
me", the user runs the commands) or want the agent to run the commands ("do it for me"), reads the
current state from the engine, explains each step using `docs/bootstrap.md`, answers questions along
the way, and leaves every decision to the user: project description, repository visibility,
approval count, constitution content and roadmap content.

**Why this priority**: It delivers the conversational outcome of the roadmap entry, but it depends on
the engine (User Stories 1 and 2) and adds no new bootstrap capability of its own.

**Independent Test**: On a scratch repository, complete the bootstrap once in each mode through the
agent conversation alone, stopping and resuming in a new agent session at least once; a reference
test confirms every command, file and doc section the skill names exists.

**Acceptance Scenarios**:

1. **Given** a fresh clone, **When** the adopter asks the agent to bootstrap, **Then** the guide reads
   the state from the engine, asks for the mode, and starts at the first unfinished step.
2. **Given** "guide me" mode, **When** a step is next, **Then** the guide explains it, gives the exact
   command for the user to run, and rereads the state afterwards instead of assuming success.
3. **Given** "do it for me" mode, **When** a step changes something, **Then** the guide shows the
   preview and runs the change only after the user's explicit yes; for a change that bypasses pull
   request review (GitHub settings, the bootstrap issue and branch, the first roadmap sync apply) it
   always asks, whatever the mode.
4. **Given** a step needs a decision (description, visibility, approvals, constitution, roadmap),
   **When** it is reached, **Then** the guide asks the user and never decides for them, even in "do
   it for me" mode.
5. **Given** the adopter asks a question about a step, **When** the guide answers, **Then** the answer
   comes from `docs/bootstrap.md` or the engine output, and the flow continues where it was.
6. **Given** the adopter stopped mid-way, **When** they start a new agent session and ask to
   continue, **Then** the guide asks for the mode again and resumes at the first unfinished step.

---

### User Story 4 - Safe handling of the board token (Priority: P2)

When the guide reaches the optional board credential, it explains the classic token's scopes
(`project`, plus `repo` for a private or `public_repo` for a public repository) and tells the user
to set the secret themselves, in the terminal or on the GitHub web page. It confirms the result only
by checking that the secret name exists. If the user pastes a token into the chat anyway, the guide
stops the flow, does not use the token, and walks the user through revoking it, creating a new one
and setting it again.

**Why this priority**: A token pasted into an agent chat is a real leak (it may be logged or kept in
the session history); the guide must make the safe path the only path. It is part of the Phase 2
skill and depends on it.

**Independent Test**: In a walkthrough, set the secret through the terminal and confirm the guide
detects it; in a second walkthrough, paste a dummy token-shaped string into the chat and confirm the
guide stops, never runs a command with it, and gives the revoke-and-replace instructions.

**Acceptance Scenarios**:

1. **Given** the board credential step, **When** the guide reaches it, **Then** it explains the
   scopes, offers to skip it (board sync is optional), and asks the user to set the secret
   themselves.
2. **Given** the user says the secret is set, **When** the guide checks, **Then** it confirms by
   listing secret names only and never reads, prints or asks for the value.
3. **Given** the user pastes something that looks like a token, **When** the guide sees it, **Then**
   it stops the bootstrap flow, never passes the value to any command, tells the user the token must
   be considered leaked, and gives the steps to revoke it, create a new one and set it again outside
   the chat.

---

### Edge Cases

- `gh` is missing, logged out, or lacks the `project` scope: `start` and `check` report it as the
  first blocking step with the fix (`gh auth login`, `gh auth refresh -s project`).
- The user lacks admin rights, or the repository is private on GitHub Free (no rulesets): the
  `github` step reports the likely cause and what the user can do (ask an admin, make the
  repository public, upgrade), and `start` does not loop on it.
- The bootstrap issue or branch exists already (created by hand or by an earlier run): the engine
  detects and reuses it instead of creating a second one; if several candidates exist, it asks the
  user which one to use.
- The user is on `main` or another branch than the bootstrap branch: the engine says so and offers
  to switch, and changes no files on the wrong branch.
- The working tree has uncommitted changes unrelated to bootstrap: the engine does not stash or
  discard them and asks the user before committing anything.
- A step fails midway (network error, API error): rerunning `start` repeats only the unfinished
  part; finished parts are detected as done.
- The bootstrap pull request is open but not merged, or failing checks: `start` reports the waiting
  state and the failing checks instead of moving on.
- The first roadmap sync preview fails or shows unexpected changes: the engine does not offer the
  apply until the user has read the result and confirmed.
- The board's built-in workflows cannot be read (missing scope, API change): reported as "not
  checked" with the manual step, never as a failure that blocks the bootstrap.
- The user runs the terminal flow without a TTY (for example from an agent): prompts are not shown;
  the engine names the missing answer or flag and stops instead of hanging.
- The repository was bootstrapped before this feature existed: `start` reports it as complete (or
  names only the missing parts) without redoing anything.

## Requirements *(mandatory)*

### Functional Requirements

**Engine (Phase 1)**

- **FR-001**: The engine MUST provide a `start` command that determines the first unfinished
  bootstrap step and performs or explains it, in this order: prerequisites, bootstrap issue and
  linked branch, `project-context.toml` (with board creation or selection), constitution, roadmap,
  GitHub settings, board credential, board built-in workflows, bootstrap pull request, merge, and
  first roadmap sync.
- **FR-002**: The engine MUST determine progress from the observable state of the repository and
  GitHub (files, branches, issues, pull requests, settings, secret names, workflow runs), so that
  stopping and resuming at any step needs no other record of finished steps.
- **FR-003**: Every step MUST be safe to rerun: a finished step is detected and skipped, and a
  partly finished step completes only its missing part without duplicating issues, branches, boards
  or pull requests.
- **FR-004**: Before any change that bypasses pull request review (creating the bootstrap issue and
  branch, creating a board, applying GitHub settings, applying the first roadmap sync), the engine
  MUST show a preview of the change and act only after an explicit confirmation.
- **FR-005**: The engine MUST NOT write the content of the constitution or `ROADMAP.md` itself; at
  those steps it names the skill to run and the inputs to give it (for the constitution, the
  approval count). Its only edit to either file is FR-006a's removal of the constitution's Sync
  Impact Report comment.
- **FR-006**: The engine MUST NOT commit, push or open the pull request without an explicit
  confirmation that shows what will be committed.
- **FR-006a**: When it commits the constitution, the `pull_request` step MUST first remove a Sync
  Impact Report HTML comment at the top of it (temporary review notes that `speckit-constitution`
  writes there and that must not be committed). A comment in an already committed constitution is
  left alone and does not change the step's state. Only a well-formed leading comment is removed:
  nothing but whitespace before it, the text "Sync Impact Report" inside it, and a closing `-->`;
  the blank lines after it go with it. Any other shape, such as an unclosed comment, leaves the file
  unchanged, with no removal line in the preview. The step's preview MUST name the removal, so the
  same confirmation covers it and the user is not asked about it separately.
  The bootstrap pull request review covers the constitution without that comment, and this removal
  does not count as a file change after the review. Amendments after the bootstrap are out of
  scope.
- **FR-007**: The first roadmap sync MUST run as a preview first; the apply MUST be offered only
  after the preview has finished and the user has confirmed, with a way to read the preview
  result.
- **FR-008**: The board credential step MUST check only whether a supported credential's secret
  names are present, MUST treat a missing credential as optional (board sync is skipped), and MUST
  never read, accept or print a credential value.
- **FR-009**: The engine MUST read, without changing them, whether the board's built-in "Pull request
  linked to issue" and "Auto-add sub-issues to project" workflows are enabled. "Pull request linked
  to issue" is a manual step: when it is off, the engine names the action on the board's Workflows
  page (enable it and set Status to `In review`). This step MUST NOT block the later steps: the flow
  continues to the pull request and the first roadmap sync, and the overall result is "complete, with
  manual settings open" until it is on. The API cannot read the Status the workflow sets, and new
  boards enable it with Status `In progress`, so an enabled workflow is reported as done with a
  title that tells the user to check that it sets Status to `In review` (the API cannot read it;
  the action of a done step stays empty, so the output format version does not change), and the `github` preview's manual line asks the user to check or set it, never implying
  that it was verified. "Auto-add sub-issues to project" is information
  only (on, off or not readable), with no action and no effect on completion, because the syncs add
  their issues to the board either way. Failure to read either workflow MUST NOT block the
  bootstrap.
- **FR-010**: Interactive terminal prompts MUST be a fallback only: without a terminal, or when a
  caller requests non-interactive use, the engine MUST take every answer from flags and stop with a
  message naming the missing flag instead of prompting.
- **FR-010a**: When `start` reaches `project_context` without a terminal or with `--no-input` and
  required init flags are missing, it MUST stop as a normal stop with exit 1 and a stdout line
  naming the decisions needed (`Next step project_context needs your decisions: --description, …`),
  both on a plain run and on a run that continues after a confirmed step, so that a step that
  succeeded is never reported as a usage error. The list also names each given init flag whose
  value fails validation, followed by one line per such flag with the validation message
  (`--description: must be …`); the rejected value itself is never printed. Exit 2 MUST be kept
  for a `--confirm project_context@…` run with missing flags and for an init flag value that fails
  validation when no flag is missing. The project name and description MUST be a single line:
  control characters, line and paragraph separators and bidirectional-text control characters are
  rejected; zero-width joiners and soft hyphens are allowed.
  Output MUST keep its order: stdout is flushed before anything is written to stderr.
- **FR-010b**: `start` MUST accept a `--guided` flag, which the guide skill adds only to the
  confirming commands it hands the user in "guide me" mode (no guessing from terminal detection).
  It requires `--confirm`; without it, `start` exits 2. With it, `start` performs only the
  confirmed step and previews no next step. When the step was performed, it prints `Done: <step>:
  <result>.` and then `Done. Go back to your coding agent and reply "done".` and exits 0; the same
  "done" line follows the completion line when the bootstrap was already complete (exit 0). On
  any other exit (1, 2, 3 or 4) it prints the usual message and then `Go back to your coding agent
  and tell it what happened.`. The wording names no particular coding agent.
- **FR-010c**: While it reads state or acts, `start` MUST print short progress lines on stderr
  (for example `Reading the repository and GitHub state…`, `Applying 15 GitHub settings…`,
  `Pushing the branch…`), plain lines with no spinner or escape codes, naming the number of settings
  or files when it is known before the action starts; stdout, the preview and its
  digest MUST NOT change because of them. During the first roadmap sync it MUST print a line on
  stderr when it starts the workflow, while it waits for GitHub to start the run (naming the
  60-second limit), the run's URL once the run is found, and a `Still running (<elapsed>)…` line at
  least every 30 seconds while the run is watched; the final success line and the failure messages
  stay as they are.
- **FR-010d**: After a confirmed step, `start` MUST separate what was done from what comes next:
  `Done: <step>: <result>.`, a blank line, `Next step (not done yet): <step>`, the preview, then
  `To do it, run:` followed by the full confirming command (including the init flags for
  `project_context` and `--issue N` where one applies) instead of a "rerun with" hint. The
  `Confirm "…":` label and the `confirm: <step-id>@<digest>` line MUST stay the last lines of the
  preview. A preview with no step done before it starts at `Next step (not done yet):`.
- **FR-010e**: When the bootstrap is complete, `start` MUST print the result on its own line after a
  blank line, in capitals: `BOOTSTRAP IS COMPLETE`, or `BOOTSTRAP IS COMPLETE; MANUAL SETTINGS
  REMAIN (SEE ABOVE)`. It is bold only when stdout is a terminal and `NO_COLOR` is not set, so
  agents and logs get no escape codes.
- **FR-010f**: The git commands `start` and `check` print or run MUST not depend on the local git
  configuration: a pull names the remote and branch (`git pull --ff-only origin <default>`), and a
  branch switch uses `git switch <branch>`, so a name is never taken as a path.
- **FR-011**: The engine MUST provide `check --json`, a read-only command that prints the full
  bootstrap state as one machine-readable document with an explicit format version.
- **FR-012**: Each step in the `check --json` output MUST carry a stable identifier, its state, a
  human-readable title, who acts next (engine, user in the coding agent or terminal, user on a GitHub
  web page), whether it needs a user decision, whether it bypasses pull request review, and its next
  action, when one exists.
- **FR-013**: The `check --json` output MUST carry one integer `format_version`. It MUST be raised
  when, and only when, a field is removed or its meaning changes; added fields MUST NOT raise it, and
  callers MUST ignore fields they do not know. The format and this rule MUST be documented.
- **FR-014**: The existing human-readable `check`, `init` and `github` commands MUST keep their
  current behaviour and exit codes, so the documented manual path keeps working.

**Guide skill (Phase 2)**

- **FR-015**: The toolkit MUST provide a `bootstrap-guide` skill, kept once in `.agents/skills/` like
  every other skill, that conducts the bootstrap as a conversation in the user's coding agent.
- **FR-016**: The skill MUST read the bootstrap state only from the engine (`check --json`) and the
  explanations only from `docs/bootstrap.md`, and MUST NOT keep its own copy of the steps, their
  order or their explanations.
- **FR-016a**: When `check --json` reports a higher `format_version` than the skill knows, the skill
  MUST stop and tell the user to update the toolkit files instead of interpreting the output.
- **FR-017**: The skill MUST offer two modes, "guide me" (the user runs each command) and "do it for
  me" (the agent runs the commands), and MUST let the user switch mode at any point. The mode is not
  stored by the engine or anywhere else: the skill asks for it again at the start of each new
  session.
- **FR-018**: In both modes, decisions MUST stay with the user: project description, repository
  visibility, approval count, constitution content and roadmap content.
- **FR-019**: In both modes, the skill MUST show a preview and get an explicit yes before any change
  that bypasses pull request review, and before any commit, push or pull request.
- **FR-020**: The skill MUST answer the user's questions about a step from `docs/bootstrap.md` and the
  engine output, then continue from the same step.
- **FR-021**: The skill MUST tell the user to set the board token themselves, in the terminal or on
  the GitHub web page, MUST explain the classic token's scopes, and MUST confirm the result only from
  the secret names listed by GitHub.
- **FR-022**: If a token is pasted into the chat, the skill MUST stop the bootstrap flow, MUST NOT use
  the token in any command, and MUST guide the user to revoke it, create a new one and set it again
  outside the chat before continuing.
- **FR-023**: The skill MUST guide the user through enabling the board's built-in "Pull request
  linked to issue" workflow (Status set to `In review`) when the engine reports it off, MUST tell
  the user once to check that Status setting when the engine reports the workflow on, and MUST
  NOT ask the user to change "Auto-add sub-issues to project".
- **FR-024**: The skill MUST be a skill, not a sub-agent, because it must hold a conversation with the
  user.

**Documentation and verification**

- **FR-025**: `docs/bootstrap.md` MUST describe the guided path (`start` and the guide skill) and keep
  the manual path; every explanation the skill relies on MUST live in a section of that document.
- **FR-026**: An automated reference test MUST fail when a command, `check --json` field, file or doc
  section named by the skill does not exist.
- **FR-026a**: After each Phase, a manual walkthrough on a scratch pilot repository MUST be run and
  its results recorded in the Phase pull request. The walkthrough is a set of numbered scenarios,
  each stated as a starting state, the user's input and the expected result, kept with the feature
  so they can later become automated evaluation cases. At least these scenarios:
  - **WS-1**: fresh clone, terminal only: `start` repeatedly until the pull request is merged and the
    first roadmap sync is applied (Phase 1).
  - **WS-2**: stop after a step that changes GitHub, resume in a new terminal session: continues at
    the first unfinished step, nothing duplicated (Phase 1).
  - **WS-3**: `gh` logged out: `check --json` reports the GitHub steps as not checked and the local
    steps normally (Phase 1).
  - **WS-4**: fresh clone, guide in "guide me" mode to completion (Phase 2).
  - **WS-5**: fresh clone, guide in "do it for me" mode: every change that bypasses review and every
    commit, push and pull request is previewed and confirmed (Phase 2).
  - **WS-6**: stop mid-way, new agent session: the guide asks for the mode again and resumes at the
    first unfinished step (Phase 2).
  - **WS-7**: a dummy token-shaped value pasted into the chat: the flow stops, the value is never
    used, revoke-and-replace instructions are given (Phase 2).
  - **WS-8**: board's "Pull request linked to issue" workflow off: reported as a manual to-do, flow
    continues, result is "complete, with manual settings open" (Phases 1 and 2).
- **FR-027**: Published files MUST stay neutral: no adopter, maintainer or this repository's issues,
  runs or roadmap named in the engine, the skill or the docs.

### Key Entities

- **Bootstrap step**: one unit of the bootstrap sequence; has a stable identifier, a position in the
  order, a state (done, to do, manual to-do that does not block, optional or skipped, waiting, not
  checked), the actor who must act, a
  flag for "needs a user decision", a flag for "bypasses pull request review", and a next action.
- **Bootstrap status report**: the whole machine-readable state at one moment; has an integer
  `format_version`,
  the ordered list of steps, the overall result (not complete, complete with manual settings open,
  or complete) and the next step.
- **Guide mode**: "guide me" or "do it for me"; chosen by the user at the start of each session and
  never stored; governs who runs commands but never who makes decisions or approves changes that
  bypass review.
- **Board built-in workflow**: a named automation on the GitHub Project board (for example "Pull
  request linked to issue"), with an enabled flag that can be read but not changed by the toolkit.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: On a pilot repository created from the template, an adopter reaches a merged bootstrap
  pull request and an applied first roadmap sync using only `start` (Phase 1) or only the guide
  conversation (Phase 2), without consulting any other instructions.
- **SC-002**: In the pilot walkthroughs, stopping after each step in turn and resuming in a new
  session continues at the first unfinished step every time, with no duplicated issue, branch, board
  or pull request.
- **SC-003**: No change that bypasses pull request review, and no commit, push or pull request, is
  made without a preceding preview and an explicit yes in any walkthrough or automated test.
- **SC-004**: In the token walkthrough, a pasted token-shaped value is never passed to a command and
  always stops the flow with revoke-and-replace instructions.
- **SC-005**: Every documented `check --json` field is present in the output for each tested
  repository state, and the reference test passes for every command, file and doc section the skill
  names.
- **SC-006**: The existing manual bootstrap path in `docs/bootstrap.md` still completes on a pilot
  repository with unchanged commands and exit codes.

## Assumptions

- Adopters use GitHub with `gh` installed; other platforms are out of scope (roadmap Scope (out)).
- A full interactive terminal wizard and a general process-help agent are out of scope; process help
  after bootstrap is a separate chore (a "where to find what" index in `AGENTS.md`).
- Users who do not code themselves are not a target yet; the design does not rule them out.
- The bootstrap issue and branch follow the existing convention (`CHORE:` issue, branch
  `<issue>-chore-bootstrap`), which the existing branch validation already accepts.
- The constitution and roadmap are written by the existing `speckit-constitution` and `roadmap-write`
  skills; the engine and guide only route the user to them (apart from FR-006a's removal of the
  Sync Impact Report comment).
- The board's built-in workflows can be read through the GitHub API but not enabled through it, so
  enabling them stays a manual step on the board's Workflows page.
- Testing follows the constitution's quality gate: automated tests for the engine, a reference test
  for the skill, and the walkthrough scenarios of FR-026a on a scratch pilot repository after each
  Phase, recorded in the pull request.
- A scripted agent evaluation (replaying conversations against a model) is out of scope here and is
  a candidate for a later roadmap entry: it would need a model credential in CI and tooling beyond
  the constitution's standard-library-only baseline.

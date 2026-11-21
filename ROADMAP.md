# Roadmap

Plan-only ledger for the spec-driven development toolkit. GitHub issues and pull requests hold
progress; see [docs/roadmap-format.md](docs/roadmap-format.md) for the format.

## Planned Specs

### 001 — Guided bootstrap [status: planned]

- **Description:** A conversational, resumable bootstrap: a `bootstrap.py start` engine with
  machine-readable status, and a `bootstrap-guide` skill that walks the user through it in their
  coding agent.
- **Outcome:** After "Use this template", an adopter reaches a merged bootstrap pull request and a
  first roadmap sync by talking to their coding agent, can ask questions along the way, and can
  stop and resume at any step.
- **Scope (in):** Phase 1 engine: resumable steps for the bootstrap issue and branch, `init` with
  board creation, `github` preview and `--apply`, the board token check, the pull request, and the
  first roadmap sync (preview, then apply after confirmation); `check --json` status; terminal
  prompts only as a fallback. Phase 2 skill: reads state from the engine and explanations from
  `docs/bootstrap.md`; "guide me" or "do it for me" mode; decisions (description, visibility,
  approvals, constitution, roadmap) always by the user; changes that bypass pull request review
  only after a preview and an explicit yes; the board token set by the user in the terminal or
  the GitHub web page, never pasted in chat (a pasted token stops the flow: revoke, create a new
  one, set it again; the agent never uses it), confirmed with `gh secret list`; guidance for the
  classic token's scopes and the board's "Pull request linked to issue" workflow.
- **Scope (out):** a full interactive terminal wizard; a general process-help agent; platforms
  other than GitHub.
- **Depends on:** none
- **Notes:** It is a skill, not a sub-agent, because sub-agents cannot hold a conversation with the
  user. The skill reads the process from the engine and the docs and keeps no copy of its own.
  Open questions for clarify: the `check --json` fields and how the format is versioned; how the
  skill is tested (a test that every command and doc section it names exists, plus a manual
  walkthrough on a pilot repository after each Phase); where the chosen mode is kept when
  bootstrap resumes in a new session (stored by the engine, or asked again). Process help after
  bootstrap is a "where to find what" index in `AGENTS.md`, done as a separate chore. Users who
  do not code themselves are not a target yet; the design keeps that option open.

### 002 — GitLab support [status: undecided]

- **Description:** Let teams on GitLab adopt the toolkit with the same process as on GitHub.
- **Outcome:** A GitLab project can create, plan, track and gate work with the toolkit.
- **Scope (in):** separate the GitHub-specific parts of the scripts; GitLab CI equivalents of the
  workflows; `glab` and the GitLab API; issue boards and epics for the roadmap and Phase tracking;
  merge request approval rules and protected branches; the agent file output.
- **Depends on:** none
- **Notes:** Whether and when is undecided. Some needed GitLab features may require paid tiers;
  check before specifying. GitLab users can already copy the template with plain Git.

### 004 — Bootstrap-guide skill evaluation and simplification [status: planned]

- **Description:** Make the `bootstrap-guide` skill measurable, shorter and more deterministic: an
  evaluation that replays bootstrap situations through a real coding agent, the steps the engine
  can do itself moved out of the skill into `bootstrap.py`, and the remaining text split into a
  short core with linked reference files.
- **Outcome:** A change to the skill or the engine is checked by a repeatable evaluation instead of
  only by a manual pilot walkthrough; the skill holds only the conversation loop and the rules that
  need judgement, and is shorter than at the Phase 1 baseline.
- **Scope (in):** Phase 1 evaluation: scripted bootstrap scenarios (for example an exit-3 preview,
  a pending decision in `check --json`, the constitution step) replayed through an agent, and the
  transcript scored against the skill's rules (the preview in the agent's own message, the exit
  suffix only on commands the agent runs, no predicted next step, no yes taken from command
  output); a baseline recorded with the skill as it is when Phase 1 starts. Phase 2 deterministic
  steps into the engine: `bootstrap.py` prints what the agent now assembles by hand (the filled-in skill calls, an exit
  status line, the preview block, the closed list of commands), with the `check --json` and
  `start` contracts updated. Phase 3 split: `SKILL.md` keeps the loop and the hard rules, details
  move into linked reference files next to it that hold only the skill's own rules and still link to
  `docs/bootstrap.md` for explanations, and the guard tests follow the text.
- **Scope (out):** new bootstrap steps or decisions; changing who decides what; platforms other
  than GitHub.
- **Depends on:** 001
- **Notes:** Raised in #86 after pilot p5. Phase order matters: the evaluation baseline comes first
  so Phases 2 and 3 can show they make nothing worse, and the split comes last so it works on the
  text left after Phase 2. Open questions for clarify: the evaluation tooling (skill-creator evals,
  `claude plugin eval`, or our own harness); which coding agents the evaluation covers (Claude
  Code, Codex, Copilot) and how it handles nondeterministic answers (several runs, a pass rate);
  whether it runs in CI or on demand, given its cost; how much of the manual pilot walkthrough it
  replaces.

### 005 — GitHub App as the preferred board credential [status: planned]

- **Description:** Let every adopter, solo developers included, use a GitHub App as the preferred
  credential for the toolkit's board automation, and keep the classic personal access token as a
  supported, documented fallback. Clarify chooses between two designs: an organization-owned GitHub
  Projects board (the App needs an organization, a free one is enough), or an issue-based board that
  an App can serve on a personal account too.
- **Outcome:** An adopter understands the security trade-off between the App and the classic token
  (on the organization design, also what creating and owning a free organization involves) and,
  on the chosen design's App path, creates and installs a minimal App and sets its secrets
  without choosing permissions by hand and without the private key ever passing through the agent.
  An adopter who stays on the classic token still gets clear guidance.
- **Scope (in):** Both designs are compared in clarify; the work of the chosen one is built.
  Organization-board design: an owner decision (organization or personal account) early in the
  bootstrap engine and the `bootstrap-guide` skill, before the repository and board are set up,
  with the reason (GitHub Apps and fine-grained tokens cannot reach a Project owned by a personal
  account), and at that decision point, where the user chooses between the App and the classic
  token, an explanation that a private person can create a free organization in a few minutes
  without a company or any legal registration, how to create one, and what having it means for
  them: the organization owns the repository and board while they stay its owner, moving an
  existing repository there (URLs redirect, local clones update their remote), and the GitHub Free
  limits that apply; minimal permissions repository Issues: read and organization Projects: write.
  Issue-board design: the board kept on the repository's issues instead of GitHub Projects, or the
  Projects board made optional, so the App needs only repository permissions; the changes this
  needs in the three board sync workflows, Phase tracking and the board creation of 001. For
  either design: App setup from a pre-filled App registration link or a GitHub App manifest, with
  the private key generated or downloaded by the user, never handled by the agent, and the secrets
  set by the user in their own terminal, as today; `check` and `github` report which credential is
  in use and recommend the App when a personal token is set; `docs/bootstrap.md` Board credentials
  reordered with the App path first, the classic token as the fallback, and the security
  comparison; tests for the new steps and the guide's credential guidance.
- **Scope (out):** App user-to-server tokens (`ghu_`) for personal-account boards; moving an
  existing repository or board into an organization for the user; hosting a shared public App;
  platforms other than GitHub.
- **Depends on:** 001
- **Notes:** Discussed after a pilot test confirmed the limit: an App installed on a
  personal-account repository got "Could not resolve to a ProjectV2" for that account's board. The
  board owner must stay equal to the repository owner, so on the organization design the owner
  decision has to come before the repository or board is set up. The issue-board design comes from
  the Factory Kit, another agentic coding project template piloted in a separate repository: its
  board is the repository's issues, so its App works on a personal account. Clarify decides between
  the designs by comparing their steps and whether the process becomes simpler and easier to
  understand or gains steps. Security is the deciding factor: more steps or complexity are
  acceptable when they improve the user's security, and the chosen design must not expose the user
  to a security vulnerability through the template. 005's spec supersedes the classic token
  guidance of 001's FR-021; 001's text stays as history. On GitHub Free, organization secrets may
  not reach private repositories, so the App secrets may have to be set per repository, and a free
  organization does not unlock rulesets or required reviews for private repositories. Entry 004
  changes the same skill and engine; coordinate the order of their Phases, and if 005 lands first,
  004's evaluation covers the new steps. The wording fix for the classic `repo` scope is the
  separate chore #91. Open questions for clarify: the pre-filled link or the manifest (the manifest
  conversion returns the private key in an API response, so it must not run in the agent's shell);
  whether one App per organization is reused across repositories; how `start` handles an adopter
  who already created the repository under a personal account (keep the classic token, or explain
  the transfer).

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

- **Description:** Make a GitHub App the recommended board credential for every adopter, solo
  developers included, by guiding them to own the repository and board in an organization (a free
  one is enough), with a simple App setup; keep the classic personal access token as a supported,
  documented fallback for boards owned by a personal account.
- **Outcome:** An adopter decides early in bootstrap whether the repository and board are owned by
  an organization or a personal account, understands the security trade-off, and on the
  organization path creates and installs a minimal App and sets its two secrets without choosing
  permissions by hand. An adopter on a personal account still gets clear classic token guidance.
- **Scope (in):** An owner decision (organization or personal account) early in the bootstrap
  engine and the `bootstrap-guide` skill, before the board is created, with the reason: GitHub Apps
  and fine-grained tokens cannot reach a Project owned by a personal account. App setup from a
  GitHub App manifest, so the name and the minimal permissions (repository Issues: read,
  organization Projects: write) are filled in, followed by installation on the repository and the
  two secrets set by the user in their own terminal, as today. `check` and `github` report which
  credential is in use and recommend the App when a personal token is set on an organization
  board. `docs/bootstrap.md` Board credentials reordered: the App path first, the classic token as
  the personal-account and quick path, with the security comparison. Tests for the new step and
  the guide's credential guidance (FR-021 of 001 is amended accordingly).
- **Scope (out):** App user-to-server tokens (`ghu_`) for personal-account boards; moving an
  existing repository or board into an organization for the user; hosting a shared public App;
  platforms other than GitHub.
- **Depends on:** 001
- **Notes:** Discussed after a pilot test confirmed the limit: an App installed on a
  personal-account repository got "Could not resolve to a ProjectV2" for that account's board. The
  board owner must stay equal to the repository owner, so the owner decision has to come before the
  repository or board is set up. Entry 004 changes the same skill and engine; coordinate the order
  of their Phases. The wording fix for the classic `repo` scope is the separate chore #91. An
  alternative to decide in clarify, before the plan: the Factory Kit avoids the Projects limit by
  using the repository's issues as its board, so its App, which needs only repository permissions,
  works on a personal account. Doing the same here (dropping the GitHub Projects board, or making it
  optional) would let a solo developer on a personal account use an App without creating an
  organization. Clarify must decide whether that makes the process simpler and easier to understand,
  or adds steps, by comparing the steps of both designs. Security is the deciding factor: more steps
  or complexity are acceptable when they improve the user's security, and the chosen design must not
  expose the user to a security vulnerability through the template. Open questions for clarify: whether the manifest flow is a link the user opens (with a local callback
  or a manual code exchange) or step-by-step instructions only; whether one App per organization is
  reused across repositories; how `start` handles an adopter who already created the repository
  under a personal account (keep the classic token, or explain the transfer).

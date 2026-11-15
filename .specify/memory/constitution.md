# spec-toolkit Constitution

## Core Principles

### I. Toolkit Scope

This repository builds the spec-driven development toolkit itself: its scripts, GitHub Actions
workflows, skills, agent definitions, Spec Kit configuration and documentation. It is not an
application and has no deployed environment (no dev, staging or production).

- Every change MUST serve teams that adopt the toolkit through the published template.
- Features for a single adopter, or for this repository only, MUST NOT enter published files.
- There is no dev environment to deploy to; "verify in the dev environment" steps do not apply.
  Live behaviour is verified on a scratch pilot repository and board instead.

Rationale: a clear scope keeps the toolkit small and reusable.

### II. Spec-Driven Development

New features follow the Spec Kit flow: roadmap entry → specify → clarify → plan → tasks →
analyze → implement.

- Each feature MUST have a `planned` entry in `ROADMAP.md` before it is specified.
- The spec and plan MUST merge in their own pull request before tasks are generated.
- The initial `tasks.md` MUST be generated and analyzed (`speckit-analyze`) before coding starts,
  on the Phase 1 implementation branch; there is no separate tasks-only issue or pull request.
- `tasks.md` MAY evolve in reviewed implementation pull requests; `speckit-analyze` MUST run again
  after structural edits (Phases, tasks, dependencies), not after checkbox or wording changes.
- Repository chores MAY use a typed issue (`CHORE:`, `FIX:`, `DOCS:`, `CI:`, `REFACTOR:`, `TEST:`)
  and an issue-backed branch instead of the full flow. `SPEC:` issues are for a feature's
  spec-and-plan planning, not for chores.

Rationale: the toolkit is developed with its own process, so the process is tested every day.

### III. Traceable Work Tracking

- Each `planned` roadmap entry MUST have an Epic, a spec-and-plan Planning issue and a
  provisional Phase 1 issue, created by the roadmap sync, never by hand. Other statuses are
  handled by the sync as described in the `sync-roadmap-issues.py` section of
  `scripts/README.md`.
- Each Phase in `tasks.md` MUST be exactly one GitHub issue, and implementation MUST happen on the
  branch linked to that Phase issue.
- `T###` tasks MUST NOT become GitHub issues; they are tracked as checkboxes in `tasks.md`.

Rationale: every change can be traced from the roadmap to its Epic, Phase and pull request.

### IV. Quality Gate (Test-First, NON-NEGOTIABLE)

- Every behaviour change MUST have an automated test written before its implementation and
  shown to fail first (red, green, refactor). Task lists MUST place each test task before its
  implementation task.
- Before a pull request is opened, every check of the Toolkit workflow's Repository tools job MUST
  pass locally (the `scripts/` test suites, the roadmap check and `agent_setup.py check`; steps
  that run only in the template repository excepted).
- A guard test MUST be able to fail: when a test protects a rule, show it failing with the rule
  removed.
- Changes to skill or agent-definition text that cannot be unit-tested MUST be verified by a
  reference test (every command, file and doc section they name exists) and/or a walkthrough on a
  pilot repository, recorded in the pull request.

Rationale: workflows and scripts act on real repositories and boards; mistakes are costly and
often silent.

### V. Security

- Workflows MUST use least-privilege `permissions`, set per workflow or job.
- Secrets MUST NOT appear in code, docs, logs or test fixtures. The secret scanner runs as a
  pre-commit hook (installed by `npm ci`); CI runs the scanner's tests.
- Workflows MUST NOT run untrusted pull request code with write tokens or secrets.
- Outside contributors' fork pull requests MUST need maintainer approval before their workflows
  run.
- Changes to authentication, permissions, tokens, secrets or workflow triggers MUST get a
  `security-auditor` pass before the pull request.

Rationale: an adopter inherits every workflow as is, so a weakness here spreads to every project.

### VI. Neutrality of Published Files

- Files published to the template MUST NOT name adopters, maintainers, or this repository's
  issues, runs or roadmap.
- Exceptions: upstream credits and license notices (for example the source of vendored skills
  and agent definitions), and the template's own repository name and URL.
- The published template MUST stay uninitialized: no `project-context.toml`, `ROADMAP.md`,
  constitution or `specs/`.
- Project-specific values MUST come from `project-context.toml` or the adopter's constitution,
  never from hard-coded names.

Rationale: whatever is published is copied into every new project.

### VII. Git Workflow

- All work MUST happen on an issue-backed branch and reach `main` only through a pull request; the
  `main` ruleset enforces this together with the required status checks.
- Required independent approvals: **0**. This is a solo project; the maintainer merges after the
  merge gates pass. This value MUST match `[review] required_approvals` in
  `project-context.toml`. Raise it to 1 when a second maintainer joins.
- Every commit MUST be approved by the maintainer; agents never commit, push or open pull
  requests on their own.
- `main` MUST be pulled before a new branch is created.

Rationale: small, reviewed, traceable changes keep `main` releasable at all times.

## Technology Baseline

- **Python 3.11+, standard library only** for scripts and tests (`unittest`, `tomllib`); no
  third-party Python packages.
- **Bash or POSIX sh** for the Spec Kit scripts in `.specify/scripts/bash/` (generated by the
  pinned Spec Kit version and changed only through `scripts/specify.sh`), for
  `scripts/specify.sh` itself, for Git hook scripts and for workflow steps.
- **Node.js** (version in `.nvmrc`) only for tooling: lefthook, Prettier and the secret scanner.
  No runtime Node dependencies.
- **Maintainer tooling** outside CI: `uv` with the pinned Spec Kit CLI (through
  `scripts/specify.sh`) and `npx skills` for vendored skills.
- **GitHub Actions** pinned to full commit SHAs, with the version in a comment.
- **GitHub** (`gh` CLI, REST and GraphQL APIs, Projects) is the only supported platform today.
  Support for other platforms is a roadmap decision.
- YAML and JSON are formatted with Prettier; Markdown is hand-written.

## Merge Gates

A pull request merges only when all of these hold:

- the required status checks in the `main` ruleset pass;
- the `code-review-and-quality` review, through the `code-reviewer` agent, ran on the exact final
  diff, and its Critical and Required findings are fixed, or rejected by the maintainer with the
  reason in the pull request;
- a `security-auditor` pass ran when principle V requires one, and its Critical and High findings
  are fixed, or rejected by the maintainer with the reason in the pull request;
- the required independent approvals (principle VII) are given.

## Team-Decision Rules

- While the project has a single maintainer, the maintainer makes every decision. Architecture
  choices MUST have their rationale written in the pull request that introduces them.
- Once a second maintainer joins, architecture choices and constitution amendments MUST be
  proposed in writing and approved by the other maintainer.

## Publishing to the Template

- The toolkit is published to the public template repository by exporting the toolkit files
  from a reviewed commit on `main` and opening a pull request in the template repository.
- Files that exist only for this repository (its governance files, the export tooling and its
  workflow) MUST be excluded from the export.
- Fixes contributed directly to the template MUST be brought back to this repository, so the
  next publish does not undo them.

## Governance

- This constitution overrides other practices in this repository. `AGENTS.md` and the skills
  apply it; they do not override it.
- Amendments go through a pull request on an issue-backed branch, using the
  `speckit-constitution` skill, and follow the team-decision rules.
- Versioning follows semantic versioning: MAJOR for removed or redefined principles, MINOR for
  added principles or materially expanded guidance, PATCH for clarifications.
- Every pull request review checks compliance with these principles. Any deviation MUST be
  justified in the pull request.

**Version**: 1.0.1 | **Ratified**: 2026-11-15 | **Last Amended**: 2026-11-15

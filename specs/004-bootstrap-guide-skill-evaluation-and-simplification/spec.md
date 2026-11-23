# Feature Specification: Bootstrap-guide skill evaluation and simplification

**Feature Branch**: `97-spec-004-create-and-approve-spec-and-plan`

**Created**: 2026-11-22

**Status**: Draft

**Input**: `ROADMAP.md` entry 004; user description: "Make the `bootstrap-guide` skill measurable,
shorter and more deterministic: an evaluation that replays scripted bootstrap scenarios through a
real coding agent and scores the transcript against the skill's rules, with a baseline recorded on
the skill as it is when Phase 1 starts; the deterministic steps moved out of the skill into
`bootstrap.py`, so it prints what the agent now assembles by hand, with the `check --json` and
`start` contracts updated; and `SKILL.md` split into a short core with linked reference files, with
the guard tests following the text. Out of scope: new bootstrap steps or decisions, changing who
decides what, platforms other than GitHub."

## Background

The `bootstrap-guide` skill (feature 001) conducts the bootstrap as a conversation. It has grown to
about 570 lines and 7,000 words, mostly rules added after pilot findings (for example #86, pilot
p5: previews never visible, exit codes shown as failures, placeholders inside skill calls). Each
rule is protected by a text guard test, but whether an agent actually *follows* the rule is checked
only by a manual pilot walkthrough on a fresh repository, which takes hours and is run rarely. Many
of the rules describe text the agent assembles by hand (the exit suffix, the fenced preview, the
filled-in skill calls, the list of commands it may run) that the engine could produce itself.

This feature works in three Phases, in this order: measure first (so later Phases can show they
make nothing worse), then move deterministic work into the engine, then split what is left.

## Clarifications

### Session 2026-11-22

- Q: Which coding agents does the evaluation replay scenarios through? → A: Claude Code first;
  Codex and Copilot are added later as separate work, reusing the same scenarios and rule checks.
- Q: When does the evaluation run, given each run calls a paid coding agent? → A: On demand by the
  maintainer, with the report recorded in the pull request; CI runs only the scoring logic's unit
  tests on recorded transcripts, with no agent and no API secret.
- Q: Is the evaluation published to the template? → A: No. The evaluation is for this repository's
  own skills only; nothing related to it (harness, scenarios, transcripts, baseline) is exported to
  the template.
- Q: What is the evaluation built on? → A: The repository's own standard-library Python harness,
  with scenarios and rule checks kept independent of the coding agent and a runner per agent
  (Claude Code's non-interactive mode first), not `claude plugin eval` or skill-creator evals.
- Q: How does the evaluation decide that a rule got worse, given run-to-run variation? → A: 5 runs
  per scenario by default; a rule is flagged when it passes in at least 2 fewer runs than the
  baseline; a flag counts as a regression only after a 10-run rerun of that scenario confirms it.
- Q: After a skill or engine change, is the evaluation enough, or is a manual pilot still needed?
  → A: The evaluation is the check for each change; manual pilots on a fresh repository (entry 008)
  happen only as an occasional real-world check of live GitHub and of what the agent's interface
  shows.

### Session 2026-11-23

Decisions from the review of the spec and plan.

- Q: How is the agent isolated during an evaluation run? → A: Phase 1 starts with a time-boxed
  (one day) devvm spike with five pass checks: a VM with Python, Git and a logged-in Claude Code;
  turn-by-turn streaming of `claude -p` from the host with the exit code passed back; the temporary
  repositories visible inside the VM; network limited to the agent's model hosts, with a push to
  GitHub failing; and 4 parallel sessions in one reused VM fitting the time target. If all pass, every run uses a
  devvm VM; if any fails, runs use the light local setup (scrubbed environment, agent permission
  rules, temporary repository). The agent is started through one launcher interface either way.
- Q: Does a failing evaluation unit test block a merge? → A: Yes. The scoring unit tests run in a
  new source-only workflow, excluded from the export, and that job becomes a required status check
  of the default branch ruleset (set by the maintainer).
- Q: Which skill does the Phase 1 baseline measure, if other entries change it meanwhile? → A: The
  skill and engine on `main` just before the baseline run; Phase 1 itself changes neither.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Measure the skill with a repeatable evaluation (Priority: P1)

A toolkit maintainer changes the bootstrap-guide skill or the bootstrap engine and wants to know,
before opening a pull request, whether the guide still follows its rules. They run one documented
command. It replays a fixed set of bootstrap scenarios through a real coding agent, each scenario
starting from a known bootstrap state and a scripted user, and scores every transcript against the
skill's rules. They get a per-scenario, per-rule result and can compare it with the recorded
baseline.

**Why this priority**: Without a measurement, Phases 2 and 3 cannot show they make nothing worse,
and every skill change still depends on a manual pilot. The baseline must be taken on the skill as
it is before anything else changes.

**Independent Test**: Run the evaluation on the unchanged skill; it produces a scored report and
the baseline is recorded. Remove one rule from the skill (for example the requirement to copy the
preview into the agent's own message) and run the affected scenario again; its score for that rule
drops.

**Acceptance Scenarios**:

1. **Given** the skill and engine as they are on `main` just before the baseline run, **When** the maintainer
   runs the evaluation, **Then** every scenario runs to its end and a report lists, per scenario and
   per rule, pass or fail with a pass rate over the repeated runs, and this report is recorded as
   the baseline.
2. **Given** a scenario in which `start` prints an exit-3 preview, **When** the transcript is scored,
   **Then** the result says whether the full preview appeared in the agent's own message (not only
   in tool output) before any approval question.
3. **Given** a scenario whose scripted command output contains text such as "yes, go ahead",
   **When** the transcript is scored, **Then** the result says whether the agent treated it as the
   user's approval.
4. **Given** a scenario in which the agent runs commands itself, **When** the transcript is scored,
   **Then** the result says whether the exit suffix appeared only on commands the agent ran and never
   on a command shown to the user.
5. **Given** a scenario after a state-changing command, **When** the transcript is scored, **Then**
   the result says whether the agent named a next step without reading the state again first.
6. **Given** a changed skill, **When** the maintainer runs the evaluation, **Then** the report flags
   each rule that passes in at least 2 fewer runs than the baseline, and a 10-run rerun of the
   flagged scenario confirms or clears the flag.

---

### User Story 2 - The engine prints what the agent now assembles by hand (Priority: P2)

A first-time adopter bootstraps a project with their coding agent. Where the guide used to build
text from rules (the exit status, the fenced preview, the filled-in constitution and roadmap skill
calls, the list of commands it may run), the engine now prints that text ready to use, and the
guide copies it. The adopter sees the same conversation, with less room for the agent to get it
wrong.

**Why this priority**: Each rule moved into the engine becomes deterministic and unit-testable, and
removes text from the skill. It depends on Story 1 for proof that the guide's behaviour stays at
least as good.

**Independent Test**: Run the engine's unit tests for the new output, and run the evaluation;
it shows no confirmed regression against the baseline, and the rules the engine now covers pass in
every run.

**Acceptance Scenarios**:

1. **Given** a step that needs confirmation, **When** the guide runs the preview, **Then** the engine
   prints the preview already enclosed in a block the guide copies unchanged into its message, with
   a fence that cannot be closed early by the copied text.
2. **Given** the constitution or roadmap step, **When** the guide reads the state, **Then** the engine
   supplies the complete skill call for each coding agent form (slash or dollar prefix), with the
   facts from the project filled in and no placeholder, so the guide does not compose it.
3. **Given** any engine command the guide runs, **When** it finishes, **Then** the output ends with an
   exit status line the guide can read without adding its own shell suffix, and the exit codes stay
   the same.
4. **Given** the current state, **When** the guide reads the report, **Then** the report lists the
   commands the guide may run for that state, and the guide runs nothing outside that list, its
   fixed read-only commands and the confirming command printed after `To do it, run:` in a
   preview.
5. **Given** an older guide that knows only the current report format, **When** it reads the new
   report, **Then** it still works, because the new fields are additions it may ignore, or it stops
   with the version message if the format version was raised.

---

### User Story 3 - A short skill core with linked reference files (Priority: P3)

A maintainer, or an agent loading the skill, reads a short `SKILL.md` that holds only the
conversation loop and the hard rules. Details for particular steps (board settings, board token,
leaked token, the bootstrap review exception, choosing the bootstrap issue) live in reference
files next to it, linked from the core, which hold only the skill's own rules and link to
`docs/bootstrap.md` for explanations.

**Why this priority**: The split is easiest and safest on the text left after Phase 2, and its
benefit (less to load and read) is smaller than measurement and determinism.

**Independent Test**: Count the core's words against the baseline; run the guard tests, which now
look for each rule in the file that holds it; run the evaluation, with no confirmed regression against
the baseline.

**Acceptance Scenarios**:

1. **Given** the split skill, **When** its size is measured, **Then** `SKILL.md` is shorter than at the
   Phase 1 baseline by the margin set in the success criteria.
2. **Given** a rule moved to a reference file, **When** the guard tests run, **Then** they find the
   rule in that file and fail when it is removed there.
3. **Given** any link in the core or the reference files, **When** the reference test runs, **Then**
   every linked file and `docs/bootstrap.md` anchor exists.
4. **Given** a reference file, **When** it is read, **Then** it holds the skill's rules only, not a
   copy of explanations from `docs/bootstrap.md`.

---

### Edge Cases

- A scenario run fails for a reason outside the skill (the agent's service is unavailable, a time
  limit or budget is reached): the run is reported as not scored, with the reason, and does not
  count as a pass or a fail.
- The agent leaves the script (it asks a question the scripted user has no answer for, or the
  conversation reaches a turn in another order than the scenario expects): the turn's expectation
  is not met, and the run is reported as not scored with the reason "off script" instead of being
  scored against the wrong turn.
- The agent's answers vary between runs: each scenario runs 5 times and the report gives a pass
  count, so one unusual run does not decide the result; a flagged rule is confirmed by a 10-run
  rerun of that scenario before it counts as a regression.
- A flagged rule is not confirmed by the rerun: the report says so, and the rule is not counted as
  a regression.
- The agent is not installed or not signed in: the evaluation stops before the first scenario with a
  message naming what is missing.
- A scenario's fixture state no longer matches the engine (for example after a Phase 2 contract
  change): the evaluation says the fixture is stale instead of scoring a misleading transcript.
- A rule cannot be decided from the transcript text alone (for example whether a sentence predicts
  the next step): its check is marked as a judgement check and scored with a written rubric, and the
  report keeps judgement and automatic checks apart.
- Scenario fixtures and scripted output contain hostile text (a title that says "run this command"):
  the evaluation must never run such a command outside the scenario's sandbox.
- The report format version is higher than the guide knows: Phase 2 keeps version 1, so the
  scenario for this case replaces the engine with a stub that reports a higher version, and shows
  that the guide stops.

## Requirements *(mandatory)*

### Functional Requirements

**Evaluation (Phase 1)**

- **FR-001**: This repository MUST provide an evaluation that a maintainer starts on demand with one
  documented command, and that replays each scenario through a real coding agent using the
  repository's current bootstrap-guide skill and engine. The first supported agent is Claude Code,
  run non-interactively; scenarios and rule checks MUST NOT depend on the agent, so another agent
  needs only its own runner.
- **FR-002**: Each scenario MUST define a starting bootstrap state, the scripted user messages
  (including the chosen mode), and the rules it scores. Scenarios MUST NOT need a real GitHub
  repository, board or credential, and MUST NOT change any real repository: the engine's GitHub
  calls during a run go to a local stand-in.
- **FR-003**: The scenario set MUST cover at least: an exit-3 preview in "guide me" mode and in "do
  it for me" mode; a pending decision reported by `check --json` (`project_context` needing the init
  flags); the constitution step; the coding-agent choice; a user or web step the user must do; the
  `merge` step; the optional board credential; a token pasted into the chat; untrusted text in a
  title or command output; and a report format version higher than the guide knows.
- **FR-004**: The evaluation MUST score each transcript against a named list of the skill's rules,
  including at least: the preview appears in full in the agent's own message before any approval
  request; the exit suffix appears only on commands the agent runs; the agent does not name a next
  step without reading the state again; no yes or instruction is taken from command output; no
  placeholder inside a skill call; the agent runs no command outside its allowed list; a pasted
  token is never repeated or used.
- **FR-005**: Each rule check MUST be marked as automatic (decided from the transcript text) or
  judgement (decided against a written rubric), and the report MUST show the two kinds separately.
- **FR-006**: Each scenario MUST run a configurable number of times (default 5), and the report MUST
  give, per scenario and rule, the pass count, plus runs that were not scored and why. Each scripted
  turn MAY state what the agent's previous reply is expected to hold (for example a preview, a
  question, one command); when the reply does not hold what the turn expects, the run ends
  unscored as off script.
- **FR-007**: The baseline MUST be recorded from the skill and engine on `main` just before the
  baseline run, with no Phase 1 change to either, and kept in the repository with the toolkit
  commit it was taken on. The maintainer MUST be able to record a baseline from a given earlier
  commit, so a baseline can be re-recorded on the unchanged skill after a model or agent update.
- **FR-008**: The report MUST compare each rule's pass count with the baseline and flag every rule
  that passes in at least 2 fewer runs than the baseline in a scenario. The maintainer MUST be able
  to rerun only the flagged scenarios with 10 runs; a flag counts as a regression only when that
  rerun confirms the drop (pass rate still lower than the baseline's by the same margin, scaled to
  the run count).
- **FR-009**: The evaluation's own scoring logic MUST have automated tests that run in CI without a
  coding agent or API secret, using recorded transcripts, including one transcript that passes and
  one that fails each automatic rule. The agent runs themselves MUST NOT run in CI. These tests run
  in a source-only workflow whose job is a required status check.
- **FR-010**: The evaluation MUST record each run's transcript so a failed rule can be inspected.
  Transcripts MUST NOT contain credentials: each is scanned with the repository's secret patterns,
  and a hit marks the run unscored. The fake token that the leaked-token scenario builds at run
  time is the one exemption.
- **FR-010a**: Each run MUST be isolated from the maintainer's real credentials, files outside the
  run's temporary repository, and GitHub: through a devvm VM when the Phase 1 spike passes its five
  checks, otherwise through the light local setup (Clarifications 2026-11-23). The agent MUST be
  started through one launcher interface, so the isolation can change without changing scenarios,
  rules or reports.

**Deterministic steps in the engine (Phase 2)**

- **FR-011**: For a step that needs confirmation, the engine MUST print the preview inside a fenced
  block whose fence is longer than any backtick run in the preview, ready for the guide to copy
  unchanged.
- **FR-012**: For the constitution and roadmap steps, the engine MUST supply the complete skill call,
  in the forms for each supported coding agent, with the project facts filled in (approval count,
  section list, project description as quoted data) and no placeholder.
- **FR-013**: Every engine command the guide runs MUST end its output with an exit status line that
  states the exit code, so the guide no longer adds a shell suffix; exit codes and their meanings
  MUST NOT change.
- **FR-014**: The machine-readable report MUST list, for the current state, the exact commands the
  guide may run and the ones that are the user's to run. The confirming command printed after `To
  do it, run:` in a preview stays a third allowed source and is not listed in the report.
- **FR-015**: The `check --json` and `start` contracts MUST be updated for every new field or output
  line, and the format version follows the rule of feature 001's `check --json` contract (raised
  only when that rule says so; the additions of this feature do not raise it).
- **FR-016**: Each rule whose work moves into the engine MUST be removed or shortened in the skill in
  the same Phase, and the guard tests MUST follow it.

**Skill split (Phase 3)**

- **FR-017**: `SKILL.md` MUST keep the conversation loop, the mode rules and the hard safety rules;
  step-specific details MUST move to reference files in the skill's own directory, each linked from
  the core.
- **FR-018**: Reference files MUST hold only the skill's rules and MUST link to `docs/bootstrap.md`
  for explanations instead of copying them.
- **FR-019**: The guard tests MUST check each rule in the file that holds it, and MUST fail when the
  rule is removed from that file.
- **FR-020**: The reference test MUST check that every link in the core and reference files resolves
  to an existing file and `docs/bootstrap.md` anchor.

**All Phases**

- **FR-021**: After Phase 2 and after Phase 3, the evaluation MUST be run and its report recorded in
  the pull request; no confirmed regression (FR-008) may remain unless the maintainer accepts it
  with a written reason in that pull request.
- **FR-022**: The feature MUST NOT add bootstrap steps or decisions, change who decides or approves
  what, or add platforms other than GitHub.
- **FR-023**: Nothing of the evaluation (harness, scenarios, recorded transcripts, baseline and
  reports) MUST be exported to the template; the export MUST exclude it, and a test MUST fail if
  it is exported. Engine and skill changes from Phases 2 and 3 are published as usual and MUST
  stay neutral.

### Key Entities

- **Scenario**: a named bootstrap situation; its starting state, scripted user messages and mode,
  the rules it scores, and the number of runs.
- **Rule check**: one rule from the skill, its kind (automatic or judgement), how it is decided, and
  the scenarios that score it.
- **Run**: one replay of a scenario through a coding agent; its transcript, its per-rule results, and
  whether it was scored.
- **Evaluation report**: the per-scenario, per-rule pass rates of one evaluation, the toolkit commit
  and coding agent it ran on, and the comparison with the baseline.
- **Baseline**: the evaluation report recorded on the skill and engine on `main` just before the
  baseline run.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A maintainer gets a scored evaluation report for a skill or engine change with one
  command and no manual walkthrough, in under 60 minutes on the default number of runs (5 per
  scenario).
- **SC-002**: Every rule named in FR-004 is scored by at least one scenario, and every automatic rule
  fails on a recorded violating transcript in its unit test (FR-009). The removal check (take the
  rule out of the skill and see the evaluation flag it) applies to the four findings of SC-006.
- **SC-003**: After Phase 2 and after Phase 3, no confirmed regression remains (or one is accepted
  with a written reason), and each rule whose work moved into the engine passes in every scored
  run.
- **SC-004**: After Phase 3, the always-loaded `SKILL.md` has at most half the words it had at the
  Phase 1 baseline (about 7,000 words), and the core plus all reference files together is no longer
  than the baseline.
- **SC-005**: Every rule that Phase 2 moves into the engine is covered by an engine unit test that
  fails when the behaviour is removed.
- **SC-006**: The pilot findings of the kind recorded in #86 that the evaluation covers (preview
  not in the agent's message, exit code noise, placeholder in a skill call, predicted next step) are
  detected by the evaluation on a skill that reintroduces them.
- **SC-007**: A skill or engine change no longer needs a manual pilot walkthrough before its pull
  request; manual pilots (entry 008) are run only as an occasional real-world check.

## Assumptions

- The local GitHub stand-in for agent runs builds on the existing fake GitHub test support, wrapped
  so the engine can call it as a command; the plan settles how.
- Judgement checks are scored by a second model call against a written rubric; their results are
  reported apart from automatic checks and are not on their own a reason to block a pull request.
- The evaluation reads the raw transcript, so it cannot see how an agent's interface displays the
  conversation (for example folded tool output) or live GitHub behaviour; those stay with the
  occasional pilots of entry 008.
- Codex and Copilot runners are later work, outside this feature, once their non-interactive modes
  allow scripted runs; a roadmap entry or chore is decided then.
- The baseline is taken with the default of 5 runs per scenario. The regression rule detects clear
  breakage (a rule that stops holding in about half the runs or more), not small slips, which
  would need far more runs than the cost justifies.
- Entry 001 is complete (Epic #3 closed as completed); the baseline measures the skill and engine
  on `main` just before the baseline run.

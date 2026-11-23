# Research: Bootstrap-guide skill evaluation and simplification

Decisions taken while planning entry 004. Each has the decision, why, and what else was weighed.
Clarifications from the spec (sessions 2026-11-22 and 2026-11-23) are taken as given and not
repeated here. Revised 2026-11-23 after the code review of the spec and plan.

## D1. Where the evaluation lives and how it stays out of the template

- **Decision**: a new top-level directory `evals/bootstrap-guide/`, added to the `exclude` list of
  `scripts/toolkit-export.toml` as `evals/**`, with its own `evals/bootstrap-guide/.gitignore` for
  `runs/` (the root `.gitignore` is exported, so it gets no evaluation path). Its unit tests run in
  a new source-only workflow `.github/workflows/evals.yml`, also excluded from the export, with
  `permissions: contents: read`, no secrets, no `paths:` filter (a required check that never runs
  leaves pull requests pending), and a stable job name that the maintainer adds as a required
  status check of the default branch ruleset. A guard test for that workflow lives in
  `evals/bootstrap-guide/test_evals_workflow.py`. A test in `scripts/test_export_toolkit.py`
  asserts that no `evals/` or `.github/workflows/evals.yml` path is exported (FR-023); both are
  also added to `EXCLUDED_FILES` in `scripts/test_fresh_export.py`, and `docs/publishing.md` names
  the new required check.
- **Rationale**: `scripts/**` is exported as a whole, so the harness cannot live there. The manifest
  fails the export on an unclassified path, so the new `evals/` directory forces an explicit
  exclude. `evals.yml` is different: the include pattern `.github/workflows/*.yml` already matches
  it, so without its exclude it would be exported silently; the exclude and the export test cover
  that. `toolkit-source.yml` stays as it is: its guard test (`test_toolkit_source_workflow.py`)
  deliberately allows only the manifest check there.
- **Alternatives**: a job in `toolkit-source.yml` (needs relaxing that guard test; rejected in
  review); `scripts/evals/` with per-file excludes (fragile); a separate repository (the evaluation
  must test the working tree).

## D2. How a run gets the toolkit under test

- **Decision**: for each run the harness builds a temporary repository from the **working tree**:
  the files `git ls-files -co --exclude-standard` lists (tracked plus untracked, not ignored, so a
  new `references/*.md` counts before `git add`; listed paths that no longer exist in the working
  tree are skipped), filtered by the export manifest's include and
  exclude rules (reusing `export_toolkit.py`'s manifest loading and matching), with declared
  symlinks such as `.claude/skills` recreated as symlinks. It then overlays the scenario's fixture
  files, runs `git init`, commits, and sets up `origin` as described in D3a. With
  `--toolkit-ref <commit>` the same file set is read from that commit instead (as the export does),
  so a baseline can be re-recorded on an earlier skill (FR-007).
- **Rationale**: the guide is tested as an adopter gets it, but with the maintainer's uncommitted
  edits, which is what a change needs before its pull request.
- **Alternatives**: running `export_toolkit.py` itself (reads a ref only); running in this
  repository (governance files and the real remote present).

## D3. The GitHub stand-in

- **Decision**: a small executable `gh` shim (stdlib Python) placed first on `PATH`. It answers from
  the scenario's **response table**: `argv` patterns with stdout, stderr and exit code, matched in
  order; calls that write (create, edit, `-X POST/PATCH/PUT/DELETE`, `secret set`) are logged as
  writes. Every call goes to a per-run call log. An unmatched call returns exit 1 with `gh shim: no
  fixture for <argv>` and marks the run **unscored: fixture_stale**. A `record-calls` helper lists
  the calls a scenario makes so a stale table is quick to update.
- **Rationale**: the engine runs as a subprocess started by the agent, so the in-process test fakes
  (`FakeRun`, `fake_github.py`) cannot be injected. A canned table is deterministic and needs no
  network or token.
- **Alternatives**: a stateful GitHub simulation (more code, drifts); recording from a real pilot
  repository (needs credentials, leaks real names).
- **Risk**: long tables for late steps. Mitigation: those scenarios start from fixture states where
  earlier steps are already done.

## D3a. `origin` and git without a network

- **Decision**: `origin` is set to a made-up GitHub URL (`https://github.com/o/r.git`, matching the
  shim's repository), so the engine's origin and push-URL checks (`bootstrap.py` `parse_remote`,
  `bootstrap_steps.py` origin checks) pass unchanged. A `git` wrapper first on `PATH` passes every
  call to the real git, and for network subcommands only (`push`, `fetch`, `pull`, `ls-remote`,
  `clone`, `remote update`) adds `-c url.file://<run>/origin.git.insteadOf=https://github.com/o/r.git`
  so they reach a local bare repository. `git remote get-url` is not rewritten, so it still shows
  the GitHub URL. The run's environment sets `GIT_ALLOW_PROTOCOL=file`, so any git network access
  that escapes the rewrite fails instead of reaching GitHub.
- **Rationale**: no engine change (a seam would be a published change), the engine sees a normal
  GitHub remote, and a push to the network is impossible by two independent means. The reviewer
  confirmed that `pushInsteadOf` in the repository config is visible to `get-url --push` and trips
  the engine's check; applying the rewrite per command avoids that.
- **Alternatives**: a local path as `origin` (the engine rejects it); `url.*.insteadOf` in the
  repository config (also rewrites `get-url`); an engine test seam (published, out of scope).
- **Spike check**: a confirming `pull_request` run pushes to the local bare repository, and a direct
  `git push https://github.com/...` from the agent's environment fails.

## D4. Driving Claude Code with a scripted user

- **Decision**: one `claude -p` process per run with `--input-format stream-json
  --output-format stream-json --verbose`; the harness writes each scripted user message to stdin
  when the previous assistant turn ends (a `result` event) and keeps every event as the transcript.
  Common flags: `--model <pinned>` (D8), `--setting-sources project`, `--strict-mcp-config` with no
  MCP servers, `--no-session-persistence`, `--max-budget-usd <per-run cap>`, and `--settings` with
  the scenario's permission profile.
- **Tool profiles** (scenario field `tools`, data-model.md): each profile lists `--tools` and the
  allow and deny rules.
  - `guide` (default): Bash allowed for `python3 scripts/bootstrap.py *`, `git status --porcelain`
    and the exact suffix part `echo "exit $?"` (Claude Code checks each part of a compound command);
    Read, Grep, Glob, Skill.
  - `merge`: `guide` plus `git switch *` and `git pull --ff-only origin *`.
  - `skills`: `guide` plus Write and Edit limited to `.specify/memory/**` and `ROADMAP.md`, and
    the commands the invoked skills run themselves: `.specify/scripts/bash/resolve-template.sh
    constitution-template --json` (speckit-constitution) and `python3 scripts/check_roadmap.py`
    (roadmap-write).
  - `stop`: `guide` plus the reserved-free repository commands a stop can hand the guide in "do it
    for me" mode (`git remote -v`, `git remote get-url *`, `git remote set-url origin *`,
    `gh repo set-default *`), for scenarios that reach a repository stop.
  - `review`: `skills` plus the sub-agent tool, Write for `bootstrap-review-notes.md`, and the
    read-only git commands a reviewing sub-agent uses (`git diff *`, `git log *`, `git show *`).
  - Every profile denies reads and writes outside the temporary repository.
- **Scoring commands**: the rule `only-allowed-commands` checks the commands the **guide itself**
  ran against what the bootstrap-guide skill allows (for example `bootstrap.py github --apply` is
  allowed by the profile's prefix but forbidden by the skill), and reports commands the harness
  **denied** as a separate count, so a tool limit is not mistaken for the agent's choice. Commands
  run inside a sub-agent are told apart by the stream-json events' `parent_tool_use_id`; commands
  run by an invoked skill are those in that skill's own list above, issued after its `Skill` tool
  call. Neither counts against the guide.
- **Alternatives**: one `claude -p --resume <id>` call per turn (writes sessions to disk, reloads
  context); `claude plugin eval` and skill-creator evals (rejected in clarify).
- **Spike checks**: the skill is discovered and used under `--setting-sources project` (fallback:
  the first user message is `/bootstrap-guide`); the `echo` suffix part is allowed by the rule; no
  user CLAUDE.md, memory, skills, plugins or MCP servers load; stream-json emits one `result` per
  turn.

## D4a. Isolation: devvm first, the light local setup as fallback

- **Decision**: the agent is started through a **launcher** interface: `start(run_dir, argv, env)
  → process` with streamed stdin and stdout, plus `prepare()` and `close()` for a reused
  environment. Two launchers:
  - `DevvmLauncher`: one devvm VM, reused for the whole evaluation, with the temporary
    repositories created inside its project folder; Claude Code logged in through devvm's
    credential proxy; network limited to the agent's model hosts; the shim and git wrapper on the
    VM's `PATH`.
  - `LocalLauncher`: the agent runs on the host with a scrubbed environment: a minimal `PATH`
    (shim and wrapper first, then the directory of the maintainer's Python 3.11+ interpreter, then
    system directories, so macOS's older `/usr/bin/python3` is never chosen; the shim and wrapper
    start with that interpreter's absolute path), an empty `GH_CONFIG_DIR`, `GH_TOKEN`,
    `GITHUB_TOKEN` and `SSH_AUTH_SOCK` unset, `GIT_CONFIG_GLOBAL` pointing to a harness file,
    `GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0`, `GIT_ALLOW_PROTOCOL=file`; permission deny
    rules for reads outside the temporary repository; Claude Code's own Bash sandbox if the spike
    shows it works with `-p`.
- **Phase 1 starts with a one-day devvm spike** with the five checks of the spec's clarification
  (VM with Python, Git and logged-in Claude Code; streamed `claude -p` with the exit code; repos
  visible; network limited and a GitHub push failing; 4 parallel sessions in one VM fitting the time
  target). All pass → `DevvmLauncher` is the default. Any fails → `LocalLauncher` is the default and
  the devvm work becomes a later chore. The spike's result is recorded in the Phase 1 PR.
- **Both launchers** must show, in the spike's run: the shim's call log is not empty (the shim is
  the `gh` that ran), and no file outside the temporary repository appears in the transcript.
- **Rationale**: the user wants to learn sandboxing with devvm; a VM is the strongest isolation;
  the launcher interface keeps the fallback cheap (about half a day) and scenarios, rules and
  reports independent of the choice.
- **Alternatives**: light setup only (weaker isolation, the user's devvm goal later); devvm only
  without fallback (blocks Phase 1 on a tool of unknown maturity).

## D5. Simulating the user running a command in "guide me" mode

- **Decision**: a scripted turn `run_shown` takes the one command in the agent's last message (the
  code block after the label line). The harness strips a leading `! `, splits it with
  `shlex.split`, and accepts it only if the tokens are `python3`, `scripts/bootstrap.py`, `start`,
  `--no-input`, then only these, each at most once: `--report-exit`, `--confirm <step>@<hex digest>`,
  `--issue <digits>`, `--review-notes`, `--full-dry-run`, the init flags (`--description`,
  `--required-approvals`, `--project-number`, `--create-board`, `--owner`, `--name`) with one value
  each, and `--guided` last. The coding-agent choice the guide shows in "guide me" mode is the
  other accepted form, and it takes exactly these tokens: `python3`, `scripts/bootstrap.py`,
  `start`, `--no-input`, optionally `--report-exit`, and `--agents <v>`, where `<v>` matches
  `^(claude|copilot|codex)(,(claude|copilot|codex))*$`; no `--confirm`, `--guided`, init flags or
  other flags. Any shell operator, newline or other token outside quotes rejects a command.
  An accepted command runs with `subprocess.run(argv, shell=False)` through the launcher, in the
  run's repository. The output goes back as the next user message in Claude Code's `!` form
  (`<bash-input>`, `<bash-stdout>`, `<bash-stderr>`). A rejected command is not run; the rule
  `shown-command-well-formed` fails and the run ends scored. Commands that are not engine commands
  (the `merge` switch-and-pull, `git fetch`, `gh secret set`, web steps) are never `run_shown`:
  those scenarios use `say = "done"` with a fixture state that already reflects the result.
- **Rationale**: guide-me is half the flows; the strict token check keeps the harness from running
  anything a hostile fixture could inject.

## D6. Scoring and rules

- **Decision**: rules are Python functions over a normalized transcript (assistant text blocks,
  tool calls with input, tool results, permission denials, user messages, the shim's write log).
  Each rule has an `id` and a `version`; changing what a rule checks raises its version, and a rule
  that replaces another names it in `replaces` (so the baseline entry carries over) or is listed
  as retired. Judgement rules send a trimmed excerpt and a written rubric to a second `claude -p`
  call with `--tools ""` and `--json-schema` for `{"pass": bool, "reason": str}`, on the pinned
  model. Judgement results are reported apart and never count toward the exit code (FR-005).
- **Phase 1 rule list**: automatic: `preview-in-own-message`, `fence-not-broken`,
  `no-suffix-on-shown-command`, `suffix-on-own-engine-command`, `guided-only-on-shown-command`,
  `shown-command-well-formed`, `no-yes-from-output`, `no-write-before-yes`,
  `only-allowed-commands`, `no-placeholder-in-skill-call`, `token-not-repeated`,
  `stops-on-higher-format-version`, `no-reply-line-after-bang`, `exit-code-not-called-failure`;
  judgement: `no-predicted-next-step`, `constitution-summary-before-approval`,
  `credential-question-alone`.
- **Phase 2 changes**: `suffix-on-own-engine-command` is replaced by `report-exit-on-own-engine-command`
  (`replaces` keeps the baseline); `no-suffix-on-shown-command` keeps its meaning (the shown command
  carries `--report-exit` but never `; echo`).
- **Rationale**: automatic rules cover the #86 findings and the safety rules cheaply; versioning
  keeps the baseline usable when a rule must change with the engine.

## D7. Runs, regression rule and report

- **Decision**: default `--runs 5`, `--jobs 4`. Per scenario and rule the report holds `passed`
  and `scored`. With baseline `bp/bs` and current `p/s`, a rule is **flagged** when
  `5 * (bp * s - p * bs) >= 2 * bs * s` (the pass fraction dropped by at least 2/5, in integers to
  avoid floating-point misses such as 3/5 − 1/5). `--rerun-flagged REPORT` reruns only flagged
  scenarios, 10 runs by default; a flag is **confirmed** when the same inequality still holds for
  the rerun, otherwise **cleared**. Runs not scored are listed with their reason and left out of
  `s`; fewer than 3 scored runs on either side gives `insufficient`. Boundary cases (exactly 2/5,
  3/5 vs 5/5, 4/10 vs 5/5) are unit-tested.
- **Turn expectations**: a turn may carry `expect` (data-model.md); when the agent's previous reply
  does not meet it, the run ends **unscored: off_script**, so a reordered conversation is never
  scored against the wrong turn.

## D8. Comparability

- **Decision**: each scenario has its own hash over its scenario file, its fixture directory and
  its tool profile's definition. Only a
  scenario whose hash changed, or a rule whose version changed (without `replaces`), becomes `new`;
  the rest still compares. A different pinned model makes the whole report non-comparable (a
  warning at the top, every status `new`, exit 0). A different Claude Code version prints a warning
  but stays comparable. In both cases the maintainer can re-record the baseline on the unchanged
  skill with `--record-baseline --toolkit-ref <baseline commit>`.
- **Rationale**: Phase 2 changes fixtures (stale tables after contract additions); per-scenario
  hashes keep the rest of the baseline usable, and `--toolkit-ref` lets a model update be handled
  without losing the before/after comparison.

## D9. Credentials and transcripts

- **Decision**: transcripts and call logs go to `evals/bootstrap-guide/runs/` (ignored by the
  directory's own `.gitignore`). Only `baseline.json` is committed; it holds counts and metadata,
  no transcript text. The leaked-token scenario builds its fake token at run time from parts. After
  each run the harness replaces that one value with a placeholder in the transcript and then scans
  it with the secret scanner (`node scripts/check-secrets.mjs`), after removing every
  `secret-scan:ignore` marker from the copy it scans (a JSONL event is one line, so a marker in
  fixture or agent text would otherwise hide a secret in the whole event); a hit marks the run **unscored:
  secret_found** and the transcript is deleted. Recorded test transcripts use a placeholder
  substituted at test time; any line that must hold a token-shaped value carries the
  `secret-scan:ignore` marker. The harness reads no API key; the agent uses the maintainer's own
  Claude Code login (through devvm's proxy with `DevvmLauncher`).

## D10. Engine output for Phase 2 (contracts in `contracts/`)

- **Decision**:
  1. `--report-exit` (opt-in) for every `bootstrap.py` subcommand the guide may run: `check`
     (human and `--json`), `start` and `github`. The process exits 0 and prints `exit <n>` as its
     last line: on stderr for `check --json` (stdout stays one JSON document), on stdout otherwise,
     where `<n>` is the command's own code. Codes and meanings do not change, including `start`'s
     130 when interrupted. The guide passes it on every engine command it runs and on the commands
     it shows (the confirming command carries it when the preview run had it), so a command the user
     runs in a separate terminal also ends with `exit <n>` and exit 0; `docs/bootstrap.md` says so.
     Engine commands that `check --json` hands the guide in `commands` (for example `python3
     scripts/bootstrap.py check` from a "run the check again" action, or `python3
     scripts/bootstrap.py github`) carry `--report-exit` appended there; `action.command` itself is
     unchanged. Commands that are not engine commands (`git …`, `gh …`, `python3
     scripts/agent_setup.py sync`) never get a suffix: the guide reads their result from the tool
     output, as today. The skill's current "no `exit` line means the shell is not bash or zsh" rule
     becomes "an engine command without an `exit` line did not finish normally: show the output and
     stop". Per command (matching today's code):
     - `check` and `github`: an argument-parser usage error prints the usage message and exits 2
       with no `exit` line (`--report-exit` was not parsed).
     - `start`: a parser `UsageError` prints `bootstrap start failed: …` and exits 2 with no `exit`
       line. `--guided` without `--confirm` and every exception caught by `main` are handled
       errors: they print their message and `exit 2`, and the process exits 0.
     - An interrupted `start` (Ctrl-C) prints `exit 130`.
  2. Under `--no-input`, `start` prints each preview between fence lines of backticks one longer
     than the longest backtick run in the preview (minimum 3), labelled `text`. The digest is
     computed from the unfenced block (`compute_digest`), so it does not change.
  3. `check --json` adds `steps[].skill_call` on `constitution` and `roadmap`: `slash` and `dollar`
     forms, with the facts filled in. The constitution section list is a constant in the engine; a
     test checks that the [Constitution](../../docs/bootstrap.md#constitution) section of
     `docs/bootstrap.md` names the same sections, so the two cannot drift.
  4. `check --json` adds a top-level `commands` object: `read_only`, `agent_after_yes`,
     `agents_prefix` with `agents_values`, `preview_flags`, and `user_only`. `agent_after_yes`
     lists every `action.command` of the current state that the skill does not reserve for the user
     (for example `git remote -v`, `gh repo set-default …`), plus the switch-and-pull command only
     when its conditions hold, so nothing the guide may run today is taken away (spec FR-022).
     `preview_flags` names the flags the guide may add to the `start` preview like the init flags
     (`--full-dry-run` at `first_sync`, `--review-notes` at `pull_request`). The confirming command
     stays a third source, only in a preview's output.
  All are additions under feature 001's version rule, so `format_version` stays 1.
- **Alternatives**: always exit 0 (breaks 001's exit-code contract); printing the guided variant
  of the confirming command (the engine does not know the mode).

## D11. Skill split for Phase 3

- **Decision**: `SKILL.md` keeps where the facts come from (shortened after Phase 2), the session
  start and modes, rules in either mode, the loop, acting on a step (shortened), showing what the
  user runs, neutrality. Reference files in `.agents/skills/bootstrap-guide/references/`:
  `decisions.md`, `bootstrap-review.md`, `board.md`, `leaked-token.md`, `completion.md`,
  `coding-agents.md`; the core links each where it is needed. The rule → file map lives in
  `scripts/skill_text_helpers.py`. The report records the skill's word counts (`skill_words`: core
  and total) so SC-004 is measured against the baseline's numbers (6,975 words at planning time).
- **Check**: `.agents/**` and `.claude/skills` already carry whole skill directories; links from
  `references/` to `../../../../docs/bootstrap.md` resolve through both paths (same depth). Phase 3
  adds a test that the files reach the export and `.claude/skills/bootstrap-guide/references/`.

## D12. Cost and time

- **Decision**: about 10 scenarios × 5 runs = 50 runs, 4 in parallel, each with a per-run budget
  cap and a 10-minute timeout. Expected wall time under 60 minutes (SC-001); the worst case (every
  run hitting the timeout) is about 125 minutes. The report prints wall time and total cost; the
  spike checks that 4 parallel sessions do not interfere through shared Claude Code state.

## D13. The higher-format-version scenario

- **Decision**: its fixture overlays `scripts/bootstrap.py` with a stub that prints a report with
  `format_version` 2 and exits 1; it accepts `--report-exit` and then prints `exit 1` and exits 0,
  so after Phase 2 the guide still stops for the version and not for a missing `exit` line. The rule `stops-on-higher-format-version` passes when the guide
  stops and tells the user to update the toolkit files, and runs nothing else.

## D14. Constitution note

- **Decision**: a Phase 1 task asks the maintainer to amend the Technology Baseline through
  `speckit-constitution` (a PATCH-level clarification), adding Claude Code, and devvm if the spike
  adopts it, as maintainer tooling outside CI. The maintainer approves the text; the plan does not
  edit the constitution.

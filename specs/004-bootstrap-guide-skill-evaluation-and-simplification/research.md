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
  Common flags: `--model <pinned>` and `--effort <pinned>` (D8, D17), `--setting-sources project`, `--strict-mcp-config` with no
  MCP servers, `--no-session-persistence`, `--max-budget-usd <per-run cap>`, and `--settings` with
  the scenario's permission profile.
- **Tool profiles** (scenario field `tools`, data-model.md): each profile lists `--tools` and the
  allow and deny rules.
  - `guide` (default): Bash allowed for `python3 scripts/bootstrap.py *`, `git status --porcelain`
    and the exact suffix part `echo "exit $?"` (Claude Code checks each part of a compound command);
    Read, Grep and Glob inside the repository only (`Read(./**)`, D18), Skill.
  - `merge`: `guide` plus `git switch *` and `git pull --ff-only origin *`.
  - `skills`: `guide` plus Write and Edit limited to `./.specify/memory/constitution.md` and
    `./ROADMAP.md` (D19), and
    the commands the invoked skills run themselves: `.specify/scripts/bash/resolve-template.sh
    constitution-template --json` (speckit-constitution) and `python3 scripts/check_roadmap.py`
    (roadmap-write).
  - `stop`: `guide` plus the reserved-free repository commands a stop can hand the guide in "do it
    for me" mode (`git remote -v`, `git remote get-url *`, `git remote set-url origin *`,
    `gh repo set-default *`), for scenarios that reach a repository stop.
  - `review`: `skills` plus the sub-agent tool, Write for `bootstrap-review-notes.md`, and the
    read-only git commands a reviewing sub-agent uses (`git diff *`, `git log *`, `git show *`).
  - Every profile denies reads and writes in the home directory (`~/**`). Outside the home
    directory (D18): the Read, Grep and Glob tools are allowed only inside the repository, and
    reads elsewhere need a permission `-p` never grants; Bash reads only what its allow list's
    commands read, and its sandbox writes only the repository and the call-log file.
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
    rules for the home directory; Claude Code's own Bash sandbox if the spike shows it works with
    `-p` (D18: no `../**` deny rules, which Claude Code would turn into sandbox write blocks).
- **Phase 1 starts with a one-day devvm spike** with the five checks of the spec's clarification
  (VM with Python, Git and logged-in Claude Code; streamed `claude -p` with the exit code; repos
  visible; network limited and a GitHub push failing; 4 parallel sessions in one VM fitting the time
  target). All pass → `DevvmLauncher` is the default. Any fails → `LocalLauncher` is the default and
  the devvm work becomes a later chore. The spike's result is recorded in the Phase 1 PR.
- **Both launchers** must show, in the spike's run: the run's `gh-calls.jsonl` has at least one
  call record (D19; the shim is the `gh` that ran), and no file outside the temporary repository
  appears in the transcript.
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
  model (the judge model, D17). Judgement results are reported apart and never count toward the exit code (FR-005).
- **Phase 1 rule list**: automatic: `preview-in-own-message`, `fence-not-broken`,
  `no-suffix-on-shown-command`, `suffix-on-own-engine-command`, `guided-only-on-shown-command`,
  `shown-command-well-formed`, `no-yes-from-output`, `no-write-before-yes`,
  `only-allowed-commands`, `no-placeholder-in-skill-call`, `token-not-repeated`,
  `stops-on-higher-format-version`, `no-reply-line-after-bang`, `exit-code-not-called-failure`;
  judgement: `no-predicted-next-step`, `constitution-summary-before-approval`,
  `credential-question-alone`. D16 adds the automatic rule `merge-command-with-instruction`.
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
  the rerun, otherwise **cleared**; the rerun report records each flag's outcome (D15). Runs not scored are listed with their reason and left out of
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
  warning at the top, every status `new`, exit 0); D17 adds the effort level to that check and
  makes a different judge model renew only the judgement rules. A different Claude Code version prints a warning
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
  secret_found** and the transcript is deleted. A run whose files cannot be scanned is **unscored:
  unscannable**, and a scanner that cannot run stops the evaluation (D15). Recorded test transcripts use a placeholder
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

## D15. Scan failures per run, the rerun record and stdout (plan update after T041)

Three gaps found while implementing T001–T041.

- **Decision 1, a run that cannot be scanned**: the secret scan of D9 has four outcomes per run.
  - **Clean:** the run is scored as before.
  - **Hit:** the run is **unscored: secret_found**, as before.
  - **The run's own files cannot be scanned:** a file is over the scan's size limit, a JSONL line or
    TOML file is nested too deeply to decode, or the scanner times out, exits with a code other
    than 0 or 1, or reports a hit that names no scanned file. The run is **unscored: unscannable**,
    every file the scan covers is deleted from the run's directory (nothing is kept unscanned), and
    the evaluation goes on.
  - **The scanner cannot run at all:** `node` or `scripts/check-secrets.mjs` is missing, the
    process cannot start, or the private copy cannot be made. The evaluation stops with exit 2, as
    before.

  Two guards keep a broken scanner from turning into a quiet report:
  - Before any run, `run.py` scans one small clean file; anything but exit 0 stops with exit 2.
  - When every finished run is `unscannable`, `run.py` writes no report and stops with exit 2.
  Otherwise any `unscannable` run gets a warning in the report (contracts/eval-report.md).
- **Rationale**: an oversized or odd transcript is a property of one run, like a timeout, and
  stopping the whole paid evaluation for it wastes every other run. A scanner that cannot run is a
  setup failure and must still stop before or during the evaluation, so it can never look like
  many unscored runs. The pre-run probe catches a broken scanner before any agent session is paid
  for.
- **Decision 2, the rerun record**: a `--rerun-flagged` report carries a `rerun` block naming the
  earlier report (its `created` time and commits, never a file path), the runs per scenario, and
  one entry per earlier flag with `before`, `after` (`confirmed` or `cleared`) and, for a flag the
  rerun could not decide, why (`insufficient`, `new` or `not_rerun`). `report.md` shows it as its
  own section. The rerun compares with the same baseline as the earlier report: if the baseline's
  commits or model differ from the earlier report's `baseline` block, `run.py` stops with exit 2
  before any run. Ordinary reports have `"rerun": null`.
- **Rationale**: a confirmed flag is the decision a Phase 2 or Phase 3 pull request is judged on
  (FR-008), so the report must show which earlier report it settles and why an undecided flag was
  cleared, not only print it to stderr. A changed baseline would compare the rerun against other
  numbers than the flag was raised on.
- **Decision 3, stdout**: stdout holds only lines a reader or a script can rely on, in this order:
  the plan, one progress line per finished run, `Baseline written: <path>` (only with
  `--record-baseline`), the path of `report.md`, a `Runs:` line and the `Rules:` summary as the
  last line. Progress for judge calls, warnings and the `error:` line go to stderr.
  contracts/eval-cli.md defines each line.
- **Rationale**: the paid runs (T049–T052) and the pull request summaries are read from this
  output, and a script can tell a finished evaluation from a stopped one by the last line alone.

- **Alternatives considered**:
  - Keeping every scan problem fatal: rejected, because one large transcript would end a run of
    about 50 paid sessions.
  - Treating every scan problem as `unscannable`: rejected, because a missing `node` would then
    produce a report of unscored runs instead of an error.
  - Leaving undecided rerun flags on stderr only: rejected, because stderr is not kept with the
    report.

## D16. Coverage gaps found while writing the scenarios (plan update after T048)

Writing and reviewing the scenarios (T042–T045) showed that two FR-003 situations were reached but
not really scored, and that two setup failures could still surface only after paid sessions. The
maintainer chose to close them in Phase 1, before the first paid run (T049).

- **Decision 1, a user's "done"**: a `say` turn may set `user_step = true`: the scripted user
  reports a step done outside the chat (a browser setting, a merge on GitHub). The
  `no-predicted-next-step` excerpt also covers the agent's text after such a turn, up to its next
  `check --json` read, the same as after a gated command (D10). The `user-web-step` scenario's
  "done" turn sets it.
- **Rationale**: the skill must read the state again after the user says "done" instead of naming
  the next step from memory. Without a window, a plain `say` turn opens nothing, so the reply to
  "done" was never judged and the FR-003 "user web step" situation was reached but not scored. An
  explicit flag keeps the rule from guessing which messages report a step.
- **Decision 2, a fixture on a checked-out branch**: `git.toml` gains an optional top-level
  `checkout = "<name>"` naming one of its `[[branch]]` entries. After building `origin`, the
  harness checks out a local branch of that name tracking `origin/<name>`. A name that is not one
  of the fixture's branches fails validation. The `merge-step` scenario moves to a fixture with an
  open bootstrap pull request whose `check --json` reports `next_step = merge`, so the skill's rule
  to show `action.command` in the same message as the instruction to merge is scored. The
  post-merge offer keeps a scenario of its own. If the real engine cannot reach `merge` with a
  static `gh` table and a checked-out branch, the task stops and reports; it does not stub the
  engine (D13 allows a stub only for the higher-format-version scenario).
- **Rationale**: the engine marks `bootstrap_issue` done only when the checked-out branch matches
  an open issue's branch, and every fixture started on `main`, so `merge` could never be the next
  step and the FR-003 "merge step" situation was not reachable.
- **Decision 2a, scoring the merge step**: a new automatic rule `merge-command-with-instruction`
  (version 1), scored by the `merge-step` scenario. When a `check --json` result in the transcript
  reports `next_step = merge`, the agent's first main text after it that tells the user to merge
  the pull request must also hold that result's `action.command` in the same text block.
  - **Fail**: that text lacks the command (held back to a later message, in the same turn or after
    the user's next message); or no main text after the merge report holds the command at all
    (the merge step's command was never shown).
  - **Not applicable**: no `check --json` reports `merge`; or the command was shown but no text
    matched the instruction pattern.
  - The instruction pattern stays conservative, because a false fail costs a 10-run rerun. If its
    `not_applicable` rate in `merge-step` is 1 in 5 runs or more after the first paid baseline,
    widen it.
  - The scenario's `expect` gate on the reply after the `--agents` run proves only that the merge
    step was reached (the pull request number, `#13`), not that the command is in that reply, so a
    cross-turn hold-back is scored by the rule instead of ending `off_script`.
  - The merge-step fixture has the board credential configured, because the skill's Board token
    rule asks the credential question alone and would otherwise come first. The scripted reply
    after the merge instruction is neither a yes nor "merged", so running the switch-and-pull early
    fails `no-yes-from-output`.
- **Rationale**: an `expect` gate on the behavior itself makes a miss `off_script`, which is left
  out of the counts (D7), so a regression would only show as `insufficient`, never as a flagged
  rule. Rules added after the Phase 1 list of D6 are named here.
- **Decision 3, preflight before any agent session**:
  - When a selected scenario has a judgement rule, after `claude auth status` passes, `run.py`
    makes one minimal judge call through the same sandbox, environment and flags as a real judge,
    `--json-schema` included: a prompt asking for the schema's object, the configured model, a
    budget cap of $0.05 and a 60-second timeout. It accepts a result document (`type` result) with
    `is_error` false, and also a result whose subtype says the budget cap was exceeded, because
    the service billed the call, which proves the credential works. No output, unparseable output,
    a document that is not a result, another error result or a timeout stops the evaluation with
    exit 2 and `the judge credential was rejected`. When the check itself cannot run (the judge
    process fails to start or the call raises an error), it stops with exit 2 and
    `the judge credential check could not run (<error type>)`; a missing `sandbox-exec` stops with
    exit 2 and the launcher's own sandbox message, the same one the free login check gives. No token and no output is printed
    or kept. The free secret-scanner probe runs before this paid call.
  - When a selected scenario has a `run_shown` turn, `run.py` checks that `/usr/bin/sandbox-exec`
    can run before any agent session, whether or not a judge is needed.
- **Rationale**: `claude auth status` only shows that a token is present, so an expired or revoked
  token failed only at judging, after every paid session, and each failed judge call quietly made
  its rule `not_applicable`. One small call per evaluation is cheap next to about 55 paid
  sessions. The sandbox was checked only through the judge preflight; every current scenario that
  has a `run_shown` turn also has a judge, but a future one without a judge would stop mid-way.
- **Decision 4, spec drift**: data-model.md lists `tampered` among the unscored reasons (the code
  has had it since T040), and contracts/eval-cli.md lists the option combinations `run.py`
  refuses with exit 2.
- **Alternatives considered**:
  - Treating every `say` turn whose text is "done" as a user step: rejected, because matching
    words is brittle and the flag states the intent.
  - A stub engine for the merge step: rejected by D13; the point is to test the skill against the
    real engine's output.
  - Checking the judge credential only after the agent runs: rejected, because that is when it
    fails today.
  - Skipping the judge call and trusting `auth status`: rejected, because it does not contact the
    service.

## D17. Agent model, effort and judge model (plan update before T049, maintainer decision)

- **Decision**: the agent under test runs on Sonnet 5.5 (`claude-sonnet-5-5`) at medium effort
  (`claude --effort medium`); the judge stays on Opus 5.5 (`claude-opus-5-5`), with the CLI's
  default effort. `config.toml` holds `model`, `effort` and `judge_model`; each is required, and a
  missing or empty value, or an `effort` other than `low`, `medium`, `high` or `max`, stops the
  evaluation with exit 2 before any run. The agent's command line gets `--model <model>` and
  `--effort <effort>`; the judge's gets `--model <judge_model>`, and the judge credential check
  (D16 decision 3) uses the judge model. The plan line, the report metadata and the baseline
  identity record all three.
- **Comparability**: a different agent model or effort makes the whole report non-comparable, as
  a different model did before (D8). A different judge model keeps the report comparable, with a
  warning, and makes only the judgement rules' statuses `new`; the automatic rules still compare.
  A `--rerun-flagged` stops with exit 2, before any run, when any of the three differs from the
  earlier report's baseline, and also when the current `config.toml`'s `model`, `effort` or
  `judge_model` differs from what the earlier report ran on (its own metadata), naming the
  setting. Without that second check a changed config would clear every flag as `new` and blame
  the scenario or rule (maintainer decision 2026-11-26). No baseline exists yet, so the switch
  costs no comparison.
- **Rationale**: a full evaluation is about 60 agent sessions; Sonnet costs much less than Opus.
  Medium effort keeps the model following the skill's instructions closely enough that a failure
  points at the skill rather than at the model, which a low setting would blur. The judge makes
  few calls and grades the runs, so a stronger judge is cheap and keeps grading reliable.
- **Alternatives considered**:
  - Sonnet at low effort: rejected for the baseline, because failures would reflect the model more
    than the skill; it may be a separate stress run later.
  - One model for agent and judge: rejected, because a Sonnet judge would grade its own model's
    output with less reliability, and an Opus agent costs several times more per evaluation.

## D18. Deny rules and Claude Code's own files (plan update before T049, maintainer decisions)

Found in the first T049 attempts: every run was unscored `tampered`, and the agent's `gh` was
reported as not logged in.

- **Decision 1, deny rules**: no profile has the deny rules `Read(../**)`, `Edit(../**)` and
  `Write(../**)` any more; the `~/**` deny rules stay. Claude Code turns Read, Edit and Write deny
  rules into its Bash sandbox's write blocks too. `../**` resolves from the repository to the
  whole run directory, the repository included, and a write block wins over `allowWrite`. So the
  agent's Bash could write neither the repository nor `gh-calls.jsonl`: the `gh` shim failed, and
  `bootstrap.py check` reported `gh` as not logged in. A probe with four short sessions confirmed
  it. The eval's exact settings fail with `Operation not permitted`. Without the three `../**`
  rules, or without all Edit and Write deny rules, the writes succeed and the home directory stays
  hidden. Without only the `~/**` rules, the writes still fail.
  - **Boundary after the change** (amended after code review on 2026-11-27): `Read(../**)` was
    what kept the Read, Grep and Glob tools out of the run directory, because the profiles
    allowed them with bare rules (`Read`, `Grep`, `Glob`) that match every path. Dropping it let
    the agent read `../harness/gh.toml`, the scripted `gh` answers. So every profile's allow
    list now has `Read(./**)` in place of the bare `Read`, `Grep` and `Glob` rules (Claude Code
    applies Read rules to Grep and Glob), and no new deny rules. `--tools` still lists Read,
    Grep and Glob. A second probe (three short sessions) confirmed it. With the bare allows,
    all three tools read `../harness/gh.toml`. With `Read(./**)`, all three are refused as a
    permission `-p` does not grant, a Read inside the repository still works, and Bash still
    writes the repository and the call log. The Bash sandbox writes only the repository and the
    call-log file and cannot read the home directory; what Bash reads is limited by its allow
    list's commands. One of them could read outside the repository: the `review` profile allows
    `git diff *`, and `git diff` reads files outside the repository with `--no-index`, or
    implicitly when given two paths and one is outside the repository (or outside a work tree).
    For example, `git diff ../harness/gh.toml /dev/null` printed the scripted `gh` answers (code
    review, 2026-11-27). So the git wrapper refuses `git diff` with `--no-index`, with any path
    argument that resolves outside the repository's top level (`/dev/null` and symlinks that
    point out included), and outside a work tree. `git log` and `git show`, also allowed there,
    refuse outside pathspecs themselves; D19 adds the options that read stdin or a file.
  - **Check**: the settings of every profile hold no `../**` rule and no bare `Read`, `Grep` or
    `Glob` allow, and allow `Read(./**)`; a test fails otherwise.
- **Decision 2, `.claude/.cc-writes`**: Claude Code itself creates an empty directory
  `.claude/.cc-writes` in the repository when a session starts. The integrity check counted it as
  agent configuration, so every run was `tampered`. `_check_agent_config` now also accepts a real
  (not symlinked) **empty** directory `.claude/.cc-writes`. Anything inside it, or a symlink by
  that name, still counts as agent configuration (tampering). In the second probe, a session in
  the `skills` profile that used the Write and Edit tools left nothing in `.claude/.cc-writes`,
  so write-capable profiles do not fill it.
- **Rationale**: both failures came from Claude Code's own behaviour, not from the agent under
  test, and made every run unscored. The fixes keep each boundary the eval relies on: home hidden,
  writes limited to the repository and call log, and agent configuration checked.
- **Alternatives considered**:
  - Absolute deny rules per run-directory path (`bin/**`, `origin.git/**`, the sidecar files
    except the call log): rejected, because every new sidecar file must be listed, and a mistake
    blocks writes again.
  - Accepting `.claude/.cc-writes` with any content: rejected, because files there would go
    unchecked.

## D19. Planted files, the call log and git input options (plan update before T049, maintainer decisions)

A `security-auditor` pass on the D18 change, then a probe of three short sessions with the
eval's exact profile settings, found gaps that D18 did not cause but that let the agent under
test change what is scored. The home directory stays hidden in every case; the risk is to the
evaluation's validity.

- **Probe results**: Claude Code allows `cd` into a folder of the repository, and the new working
  directory stays for later commands. It allows an output redirect from an allowed command into
  the call log, which the sandbox lets the agent write. It asks before an output redirect into the
  repository, so `-p` refuses it. It refuses an input redirect from outside the repository,
  `$(...)`, and `cat` or `ls` outside the repository. The Write tool refused a nested
  `.git/config` as a sensitive file.
- **Decision 1, planted files**: an allowed command line is matched as text, so after a `cd` it
  can run a file at another relative path, and the `skills` and `review` profiles could write
  such a file under `.specify/memory/`.
  - Write and Edit allow only the files the skills and the review step write, in the `./` form
    so that each matches the top-level file only: `./.specify/memory/constitution.md`,
    `./ROADMAP.md` and, in `review`, `./bootstrap-review-notes.md`. A rule without a slash
    (`ROADMAP.md`) also matched `specs/ROADMAP.md` in the second probe.
  - Every profile denies `Bash(cd *)` and `Bash(pushd *)` as an extra layer. The second probe
    showed the `cd` rule honored; nothing relies on it alone.
  - The git wrapper refuses every subcommand unless git's top level is the run's repository and
    its git directory is `<top>/.git`, so a nested or redirected repository is never used.
  - The integrity check flags a `.git` anywhere below the top level, watches `.specify/scripts`
    (a `skills` command runs a script there), and flags a `__pycache__` that appears in a watched
    script directory.
- **Decision 2, call log tamper detection**: the shim must be able to write the call log, so the
  agent can overwrite it with a redirect. The log is made tamper-evident instead.
  - At build, the harness writes a header line holding a random value.
  - Each shim line records the SHA-256 of the line before it.
  - The harness reads the log at the start of each turn's step and once at the end of the run,
    under a shared lock taken with a bounded retry (it fails closed if the lock is not free in
    time). A missing or changed header, a broken chain, a line that is not a shim record, or a
    log that no longer starts with the bytes already verified makes the run `tampered`; the
    end-of-run read decides both `tampered`, which wins over every earlier reason (including
    `fixture_stale`, `timeout` and `budget`), and the stale check.
  - The shim holds an exclusive lock while it reads the last line and appends, and reads only the
    end of the log, so parallel `gh` calls (sub-agents, parallel Bash calls) keep the chain whole.
    Over the size cap it still appends, and the readers flag the log.
  - The run directory's `gh-calls.jsonl` copy holds the verified call records only, without the
    header or `prev`. The transcript reads that copy. `gh_shim.py record-calls` takes either the
    copy (records only) or the sidecar log (header and chain verified).
  - The agent cannot read the log through any allowed command (see the probe results), so it
    cannot write a valid next line; a redirect can only replace or extend the log with an
    allowed command's output, which breaks the chain. Requiring the verified prefix also limits
    any forgery to the current turn if the header ever leaked.
- **Decision 3, git input options**: the git wrapper refuses `--stdin`, `-O` / `--orderfile` and
  a single-dash bundle that contains `O` (such as `-RO<file>`) for `diff`, `log` and `show`; they
  read standard input or a file, which may come from outside the repository. `git log` and
  `git show` refuse outside pathspecs themselves, not these options (D18 amended). The wrapper
  checks the global options before it runs its repository probe.
- **Rationale**: each fix closes a way for the agent to change the scored evidence (the
  repository's scripts, the call log) without a new boundary that the probe has not shown to
  hold. The detection is checked by the harness, which runs outside the sandbox.
- **Alternatives considered**:
  - The shim talks to a harness-owned Unix socket, and the sidecar becomes unreadable and
    unwritable for the agent: rejected for Phase 1, because it is a larger change and the
    sandbox's socket rule needs its own probe. It stays possible later.
  - Recording the gaps and fixing them after Phase 1: rejected by the maintainer.
  - Hardening the wrapper's and shim's environment (ignoring `GIT_*` and `EVAL_*` values passed
    in): not needed, because the second probe showed that a variable prefix, `env` or `export`
    before an allowed command needs approval, which `-p` refuses.
- **Second probe (2026-11-28)**, three short sessions with the exact profile settings: every read
  of the call log was refused (Bash commands by relative and absolute path, an input redirect,
  and the Read and Grep tools on the exact file), so the header stays secret; variable prefixes,
  `env` and `export` needed approval; the `cd` deny rule was honored; an output redirect into the
  call log was allowed (caught by the header check); a slash-less Write rule matched a deeper
  path (decision 1).

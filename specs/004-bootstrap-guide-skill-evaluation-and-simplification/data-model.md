# Data model: Bootstrap-guide skill evaluation and simplification

All evaluation files live under `evals/bootstrap-guide/` (research D1). Formats are TOML for files
a maintainer writes and JSON for files the harness writes. Engine additions (Phase 2) are in
[contracts/check-json-changes.md](contracts/check-json-changes.md) and
[contracts/start-cli-changes.md](contracts/start-cli-changes.md).

## Scenario (`scenarios/<name>.toml`, maintainer-written)

| Field | Type | Rules |
| --- | --- | --- |
| `name` | string | Unique, kebab-case, equals the file name without `.toml`. |
| `description` | string | One line: the situation tested. |
| `fixture` | string | Name of a fixture state directory under `fixtures/` (below). |
| `mode` | `"guide me"` or `"do it for me"` | The mode the scripted user picks. |
| `turns` | list of turn | In order; the run ends after the last turn's assistant reply. |
| `rules` | list of rule IDs | Every ID must exist in the rule registry; at least one. |
| `tools` | profile name, optional | `guide` (default), `merge`, `stop`, `skills` or `review` (research D4). |
| `runs` | integer, optional | Overrides the default (5). |
| `agents` | string, optional | A coding-agent choice the harness saves before the run, in the `--agents` form (`claude`, `copilot`, `codex`, comma-separated, each once). Only for a fixture on the real engine (research D20). |

**Turn** is one of:

- `{ say = "<text>" }`: the user's message, sent as is. It may add `user_step = true` when
  the message reports a step the user did outside the chat, such as "done" after a browser
  step (research D16); the agent's reply to it is then judged like the text after a gated
  command.
- `{ run_shown = true }`: run the one command the agent last showed, under the token check of
  research D5, and send its output as a `!`-command message.
- `{ say_after_output = "<text>" }`: a reply sent after a `run_shown` turn's output, for agents
  that wait for "done".

Any turn may add `expect`, checked against the agent's reply before the turn is sent: `"preview"`
(a fenced preview block in the agent's own text), `"question"` (the reply ends with a question),
`"one_command"` (exactly one command code block), `"skill_call"` (a code block starting with `/`
or `$`), or `{ pattern = "<regex>" }`. When it is not met, the run ends unscored as `off_script`
(research D7).

Validation: a scenario that names an unknown fixture or rule, or has no turns, fails `run.py
--check` and the unit tests. So does an `agents` value that is not in the `--agents` form, or an
`agents` value on a fixture with a stub engine.

## Fixture state (`fixtures/<state>/`, maintainer-written)

- `files/`: files overlaid on the exported toolkit (for example `project-context.toml`, a
  constitution, `ROADMAP.md`), using neutral made-up names only.
- `files/` may replace an exported file, for example the stub engine of the higher-format-version
  scenario (research D13).
- `gh.toml`: the response table for the `gh` shim, a list of `[[call]]` entries with `argv`
  (list of glob patterns, one per argument; `**` matches any remaining arguments), `stdout`,
  `stderr`, `exit` (default 0), and `writes` (bool, set for calls that change GitHub).
- `git.toml`: optional, the branches and commits to create in the local `origin` before the run,
  and an optional top-level `checkout = "<name>"`, one of its branches, which the run starts on
  (a local branch tracking `origin/<name>`, research D16). Otherwise the run starts on `main`.

A fixture is **stale** when a run hits an unmatched `gh` call (research D3).

## Rule (registry in `rules.py`, code)

| Field | Type | Rules |
| --- | --- | --- |
| `id` | string | Unique kebab-case ID (research D6 list). |
| `version` | integer | Raised when what the rule checks changes (research D6). |
| `replaces` | rule ID, optional | The earlier rule whose baseline entry carries over. |
| `kind` | `"automatic"` or `"judgement"` | Judgement rules carry a `rubric`. |
| `skill_ref` | string | The skill section (Phase 3: file and section) the rule protects. |
| `check` | function(transcript) → result | Automatic only. |
| `rubric` | string | Judgement only: the question and pass/fail criteria sent to the judge. |

**Rule result**: `{rule, outcome: pass|fail|not_applicable, reason}`. `not_applicable` when the
transcript has no situation the rule covers (for example no preview was shown); it counts as
neither pass nor fail.

## Run (harness-written, `runs/<timestamp>/<scenario>/<n>/`, ignored by `evals/bootstrap-guide/.gitignore`)

- `transcript.jsonl`: every stream-json event from the agent, plus the harness's own user messages.
- `gh-calls.jsonl`: the run's verified call records, one line per shim call: `argv`, matched
  entry index or `null`, `writes`, `query`. It is copied from the sidecar log
  `<run-root>/harness/gh-calls.jsonl`, which holds a header line `{"header": <random value>}`
  written by the harness at build, then the same records each with `prev`, the SHA-256 of the
  line before it (research D19). A missing or changed header, a broken chain, a line that is not
  a shim record, or a log that no longer starts with the bytes already verified makes the run
  `tampered`.
- `result.json`: `{scenario, run, scored: bool, unscored_reason: null|"agent_error"|"budget"|
  "timeout"|"fixture_stale"|"off_script"|"secret_found"|"unscannable"|"tampered", rules: [rule
  result], denied_commands: int, cost_usd, seconds}`. (A missing or logged-out agent, or a secret
  scanner that cannot run, stops the evaluation, so neither is a run reason; `unscannable` is a
  run whose own files could not be scanned, research D15; `tampered` is a run whose files, call
  log or repository changed outside what the harness allows, research D16.)

States: `pending → running → scored | unscored`.

## Evaluation report (`runs/<timestamp>/report.json` and `report.md`)

Defined in [contracts/eval-report.md](contracts/eval-report.md). Holds the run metadata (toolkit
commit or `--toolkit-ref`, dirty flag, launcher, model, effort, judge model (research D17),
Claude Code version, the skill's word
counts, run count), a hash per scenario, per scenario and
rule the counts `passed`, `failed`, `not_applicable`, `scored`, the baseline comparison per rule
(`ok`, `flagged`, `confirmed`, `cleared`, `insufficient`, `new`), the unscored runs with reasons,
total cost and wall time, and for a `--rerun-flagged` report the rerun record (research D15).

## Baseline (`baseline.json`, committed)

A copy of an evaluation report's counts and metadata, without transcript text or reasons that
quote it. Replaced only on purpose with `run.py --record-baseline`, which refuses a dirty tree (or reads
the files from `--toolkit-ref`), so the baseline is tied to a commit. The Phase 1 baseline is
recorded from the Phase 1 branch with `--record-baseline --toolkit-ref origin/main` after a
`git fetch`, because `evals/` exists only on that branch until it merges; the report records that
commit (FR-007).

## Relationships

```text
Scenario ──uses──▶ Fixture state
Scenario ──scores──▶ Rule (many)
Scenario ──runs as──▶ Run (runs × per evaluation)
Run ──produces──▶ Rule result (per rule)
Evaluation report ──aggregates──▶ Run results
Evaluation report ──compares with──▶ Baseline
```

# Contract changes: `bootstrap.py check` (Phase 2)

Changes to [001's check-json contract](../../001-guided-bootstrap/contracts/check-json.md). All
are additions under that contract's `format_version` rule, so `format_version` stays **1**.
Research D10.

## New option: `--report-exit`

```text
python3 scripts/bootstrap.py check --json --report-exit
python3 scripts/bootstrap.py check --report-exit
python3 scripts/bootstrap.py github --report-exit
```

`--report-exit` is accepted by every `bootstrap.py` subcommand the guide may run (`check`, `start`,
`github`; `start` in [start-cli-changes.md](start-cli-changes.md)). `github` follows the human
`check` rule below, with its own code as `<n>` (0, 1 when `--check` finds differences, 2 for an
error, 130 when interrupted; meanings unchanged).

- With `--json`: the process exits 0 whenever a report or a handled error was produced, and writes
  `exit <n>` as the **last line of stderr**, where `<n>` is the code the command would have exited
  with (0, 1 or 2, or 130 when interrupted; meanings unchanged). stdout stays exactly one JSON
  document (or empty for 2 and 130).
- Without `--json` (the human `check`): the process also exits 0, and `exit <n>` is the last line
  of stdout, where `<n>` is the human `check`'s own code (meanings unchanged).
- A usage error found by the argument parser prints the usage message and exits 2 with no `exit`
  line, because `--report-exit` was not parsed. An unhandled crash exits non-zero and writes no
  `exit` line.
- Without the option, behaviour is unchanged.

## New top-level field: `commands`

```json
"commands": {
  "read_only": [
    "python3 scripts/bootstrap.py check --json --report-exit",
    "python3 scripts/bootstrap.py start --no-input --report-exit",
    "git status --porcelain"
  ],
  "agent_after_yes": [
    "git switch main && git pull --ff-only origin main"
  ],
  "agents_prefix": "python3 scripts/bootstrap.py start --no-input --report-exit --agents",
  "agents_values": ["claude", "copilot", "codex"],
  "preview_flags": ["--full-dry-run"],
  "user_only": [
    "gh secret set PROJECT_BOARD_TOKEN -R o/r"
  ]
}
```

- `read_only`: exact commands the guide runs without asking. The preview for `project_context` is
  the `start` entry plus the init flags, which the guide shell-quotes as today.
- `agent_after_yes`: exact commands the guide may run in "do it for me" mode after the user's yes
  for that command: every `action.command` of the current state that the skill does not reserve
  for the user (for example `git remote -v` or `gh repo set-default …` from a repository stop),
  plus the `merge`/`first_sync` switch-and-pull command only when the conditions in the skill's
  "Rules in either mode" hold (the engine checks the branch-name condition; the guide still checks
  `git status --porcelain`). Commands the skill reserves for the user (`git restore`, `git reset`,
  `git pull` outside that exception, and the others it lists) go to `user_only`. Nothing the guide
  may run today is taken away (spec FR-022).
- `preview_flags`: flags the guide may add to the `start` preview and confirming run, like the init
  flags: `--full-dry-run` when the next step is `first_sync` (only when the user asks for the whole
  dry run) and `--review-notes` when it is `pull_request` (only with review notes).
- `agents_prefix` and `agents_values`: the `--agents` command is the prefix plus a comma-separated
  subset of the values.
- `user_only`: commands from `action.command` fields that only the user runs.
- **Third source**: the confirming `start --confirm` command is not listed. It exists only in a
  preview's output, on the line after `To do it, run:`, and stays an allowed source for the guide
  (spec FR-014).
- Every listed string is a complete command line; none contains a secret or an escaped character
  (an unsafe command is left out, as `action.command` is null today).
- Every listed `python3 scripts/bootstrap.py` command ends with `--report-exit`, appended here even
  when the step's `action.command` (unchanged) does not carry it, for example `python3
  scripts/bootstrap.py check --report-exit` from a "run the check again" action. Other commands
  (`git …`, `gh …`, `python3 scripts/agent_setup.py sync`) are listed exactly as in
  `action.command`, with no suffix.

## New step field: `steps[].skill_call` (on `constitution` and `roadmap`)

```json
"skill_call": {
  "slash": "/speckit-constitution Required approvals: 0. Cover every section listed in ... Project: \"A to-do app for small teams.\". Draft a first version.",
  "dollar": "$speckit-constitution Required approvals: 0. Cover every section listed in ... Project: \"A to-do app for small teams.\". Draft a first version."
}
```

- `slash` is for Claude Code and Copilot, `dollar` for Codex. The text follows the one template in
  the skill's "Decisions that stay with the user" section today, which moves into the engine
  verbatim; the roadmap call holds only the skill name and the `Project:` line.
- The constitution's section list is a constant in the engine. A unit test checks that the
  `Constitution` section of `docs/bootstrap.md` names the same sections, so neither can change
  alone.
- The description comes from `project-context.toml` (validated there, at most 350 characters) and is
  inserted as data between double quotes; it is escaped like other display text.
- `skill_call` is `null` on other steps and when `project-context.toml` is not valid yet.

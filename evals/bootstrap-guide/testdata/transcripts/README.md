# Transcript fixtures

Small stream-json recordings used by `test_transcript.py` and, later, the rule tests. They were
trimmed from real `claude -p --input-format stream-json --output-format stream-json --verbose`
runs on a scratch repository. Absolute paths are replaced by `/tmp/run/repo`, session ids, uuids,
timestamps, usage and system-prompt-sized fields are dropped; only the fields `transcript.normalize`
reads and the event shape remain. They hold no real names and no token-shaped values. A fixture
that must show a token holds a placeholder substituted at test time, and its line carries the
`secret-scan:ignore` marker (research D9).

Each recording is a pair of JSON Lines files with the same stem:

- `<stem>.events.jsonl`: the agent's events, one per line, in order: `system` (`init`,
  `permission_denied`), `assistant` and `user` events with a `message` and `parent_tool_use_id`
  (`null` for the main agent, the `Agent` tool call's id inside a sub-agent), and one `result` event
  per turn. A `result` event may list `permission_denials` (`tool_name`, `tool_use_id`,
  `tool_input`); Claude Code emits it next to the `permission_denied` system events, and
  `normalize` reads both.
- `<stem>.messages.jsonl`: the user messages the harness sent, one per turn, in the form written to
  the agent's stdin (`{"type": "user", "message": {...}}`). The n-th message starts turn n. A
  `!`-command message carries `<bash-input>`, `<bash-stdout>` and `<bash-stderr>` in its text. A message may carry `"user_step": true`, as the harness's log
  line does for a `say` turn with `user_step = true` (the user did a step themselves and says so).

| Stem | What it covers |
| --- | --- |
| `session` | 4 turns: a `Skill` call (`hello`, which runs `git status --porcelain`), `Bash` calls and results, two permission denials (one in the main agent, one inside a sub-agent), an `Agent` call whose events carry `parent_tool_use_id`, a `!`-command message and one `result` per turn |
| `one-turn` | one turn, no tools (the shape of a judgement call) |

## Rule fixtures

Hand-built recordings in the same format, small and made up (`/tmp/run/repo`, a demo project, no
real names), used by `test_rules_preview.py`, `test_rules_commands.py` and `test_rules_safety.py`.
Each automatic rule has a passing and a violating recording (some recordings serve several rules).
Facts the harness adds (the shim's write log, `meta`) are passed in by the test, not stored here.
The leaked-token recordings hold the placeholder `PLACEHOLDER_TOKEN`; the test substitutes a
token-shaped value built from parts at test time, so no file holds one. Each violating recording
breaks its own rule only, with the exceptions named in the table.

| Stem | Used for |
| --- | --- |
| `do-it-for-me-clean` | a clean "do it for me" session: preview copied in a longer fence, suffix on own commands only, yes before the confirming run, exit 1 called normal progress |
| `guide-me-clean` | a clean "guide me" session: `!` command with `--guided`, no suffix, no reply line |
| `preview-tool-output-only`, `preview-after-question`, `preview-fence-broken` | `preview-in-own-message`, `fence-not-broken` violations |
| `shown-command-with-suffix`, `own-command-without-suffix` | `no-suffix-on-shown-command`, `suffix-on-own-engine-command` violations |
| `guided-on-own-command`, `guided-on-agents-command` | `guided-only-on-shown-command` violations (`guided-on-own-command` also fails `only-allowed-commands`, because `--guided` makes the run differ from the preview's command) |
| `github-apply`, `commands-not-counted` | `only-allowed-commands`: a forbidden command; sub-agent, skill and denied commands left out |
| `handed-over-allowed`, `handed-over-reserved`, `confirm-differs-from-preview` | `only-allowed-commands`: commands handed over by an earlier `check --json` pass; a handed-over reserved command (`git pull --rebase`) and a confirming command that differs from the preview's line fail |
| `handed-over-check-no-yes`, `two-confirms-one-yes`, `shown-git-command-with-suffix`, `completion-layout-codex` | a handed-over read-only check run without a yes fails `no-yes-from-output`; one yes covers one gated command (`two-confirms-one-yes` fails `no-yes-from-output` only) and all the writes of its turn (`two-writes-one-yes`, with writes in turns 2 and 3, fails `no-write-before-yes`); a suffix on a shown git command fails `no-suffix-on-shown-command`; the Codex (`$`) completion layout passes `no-placeholder-in-skill-call` |
| `constitution-approval`, `credential-question-mixed` | the judgement rules' excerpts (`test_rules_judgement.py`); `constitution-approval` holds hostile text (a fake closing tag and shell characters) that must reach the judge only inside the one prompt argument; `credential-question-mixed` has a second text in the same turn that the excerpt must include |
| `state-change-then-check`, `bang-then-check` | the `no-predicted-next-step` window: the text before the guide's next `check --json` is in the excerpt, the text after it is not |
| `handed-over-check-no-window`, `human-check-keeps-window` | a handed-over `check` run is not a state change (no window); a human `check` (without `--json`) does not end a window |
| `user-step-predicts`, `user-step-reads-first`, `plain-say-no-window` | the `no-predicted-next-step` window after a `user_step` message: the text before the next `check --json` is in (`user-step-predicts`), a read first gives no text (`user-step-reads-first`), a plain `say` opens no window (`plain-say-no-window`) |
| `bang-preview-no-window` | a `!` run of the `start --no-input` preview is not a state change (no window) |
| `guide-me-reply-after-bang`, `guide-me-terminal-reply-ok` | `no-reply-line-after-bang` |
| `exit-code-called-failure`, `exit-code-failure-no-changes`, `exit-code-not-a-failure-sentence`, `preview-mentions-failure`, `exit-2-failure-ok` | `exit-code-not-called-failure` (a "failed" inside the copied preview passes; "no changes" does not excuse "failed") |
| `yes-from-output`, `write-without-yes`, `yes-after-question-in-later-turn`, `two-writes-one-yes` | `no-yes-from-output`, `no-write-before-yes` (`write-without-yes` and `two-writes-one-yes` fail both, as the missing yes is the same fact) |
| `placeholder-in-skill-call`, `skill-call-clean`, `completion-layout`, `placeholder-beside-completion-layout` | `no-placeholder-in-skill-call` (the completion layout's two `<change>` lines are allowed) |
| `token-repeated`, `token-partly-repeated`, `token-not-repeated`, `token-in-output` | `token-not-repeated`; `token-partly-repeated` holds the placeholder `PLACEHOLDER_SLICE`, replaced by a slice of the token |
| `higher-format-version-stops`, `higher-format-version-continues`, `higher-format-version-silent`, `higher-format-version-next-turn` | `stops-on-higher-format-version` (a command in a later turn is not counted) |
| `merge-instruction-with-command`, `merge-command-held-back`, `merge-command-next-turn`, `merge-command-never-shown`, `merge-false-friends-only`, `merge-false-friend-then-instruction`, `merge-status-then-instruction`, `merge-not-the-next-step` | `merge-command-with-instruction` (`merge-status-then-instruction`: a status text with "the merge step for PR #13" and "the merge status of PR #13", noun uses, comes before the real instruction and passes): the command in the same text as the instruction to merge passes; the instruction first and the command in a later text fails (`merge-command-next-turn`: the command only in the next turn, after a user message); a command never shown after the merge report fails (`merge-command-never-shown`); "merged", "post-merge", "the merge step" and a refusal to merge are not an instruction (not applicable when the command was shown, skipped when a real instruction follows); a report whose next step is not `merge` is not applicable |

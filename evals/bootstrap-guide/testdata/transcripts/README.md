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
  `!`-command message carries `<bash-input>`, `<bash-stdout>` and `<bash-stderr>` in its text.

| Stem | What it covers |
| --- | --- |
| `session` | 4 turns: a `Skill` call (`hello`, which runs `git status --porcelain`), `Bash` calls and results, two permission denials (one in the main agent, one inside a sub-agent), an `Agent` call whose events carry `parent_tool_use_id`, a `!`-command message and one `result` per turn |
| `one-turn` | one turn, no tools (the shape of a judgement call) |

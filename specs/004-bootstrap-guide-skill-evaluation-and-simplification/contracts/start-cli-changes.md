# Contract changes: `bootstrap.py start` (Phase 2)

Changes to [001's start contract](../../001-guided-bootstrap/contracts/start-cli.md). Research D10.

```text
python3 scripts/bootstrap.py start [--no-input] [--report-exit] [--agents TOOL,…] [--full-dry-run] [--review-notes] [--confirm STEP_ID@DIGEST [--issue N] [--guided]] [init flags]
```

## `--report-exit`

- The process exits 0 and prints `exit <n>` as the **last line of stdout**, after everything else,
  where `<n>` is the code `start` would have exited with (0, 1, 2, 3, 4, or 130 when interrupted;
  meanings unchanged). stderr lines are flushed before it.
- A usage error from the argument parser (`UsageError`) prints `bootstrap start failed: …` (followed
  by the "Go back to your coding agent…" closing line when `--guided` is in the arguments) and exits
  2 with no `exit` line, because `--report-exit` was not parsed.
- Handled errors after parsing, such as `--guided` without `--confirm` and every exception caught
  by `bootstrap_start.main` or by `bootstrap.py`'s `main`, print their message (and the `--guided` closing line when present), then `exit 2`; the
  process exits 0.
- The confirming command printed after `To do it, run:` carries `--report-exit` when the preview run
  had it, so the guide copies it unchanged and adds only `--guided` at the end in "guide me" mode.
  A user who runs that command in a separate terminal therefore also sees `exit <n>` and gets exit
  0; `docs/bootstrap.md` explains the line.
- The digest does not depend on `--report-exit`.

## Fenced preview under `--no-input`

- With `--no-input`, each preview block (from its `Step` line to the line before `To do it, run:`)
  is printed between an opening fence line `` ```text `` and a closing fence line ```` ``` ````, where
  the fence is one backtick longer than the longest run of backticks in the block, and at least 3.
- With several bootstrap issue candidates, each candidate's block has its own fences.
- The digest is computed from the block without the fence lines, so it is the same with and without
  `--no-input`.
- In a terminal without `--no-input`, nothing changes.

## Guide skill consequences (for the Phase 2 skill edit)

- The guide passes `--report-exit` on every `bootstrap.py` command it runs and keeps it on the
  commands it shows; the `; echo "exit $?"` suffix rules are removed. Commands that are not engine
  commands get no suffix, as today.
- An engine command without an `exit` line did not finish normally: the guide shows the output and
  stops (replacing the "shell is not bash or zsh" message).
- The guide copies the fenced block as it is, instead of building the fence.
- The guide takes the constitution and roadmap calls from `steps[].skill_call` and the commands it
  may run from `commands` plus the confirming command of a preview
  ([check-json-changes.md](check-json-changes.md)).

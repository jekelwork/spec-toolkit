# Contract: evaluation report (`report.json`, `report.md`, `baseline.json`)

## `report.json`

```json
{
  "report_version": 1,
  "created": "2026-12-01T10:00:00Z",
  "toolkit_commit": "4b55874",
  "toolkit_ref": null,
  "dirty": true,
  "launcher": "devvm",
  "agent": "claude-code",
  "agent_version": "2.1.330",
  "model": "<pinned model id>",
  "skill_words": {"core": 6975, "total": 6975},
  "runs_per_scenario": 5,
  "wall_seconds": 2140,
  "cost_usd": 18.4,
  "baseline": {"toolkit_commit": "9f3c2a1", "toolkit_ref": "<full SHA of origin/main>", "model": "<id>", "comparable": true, "warnings": []},
  "scenarios": [
    {
      "name": "exit3-preview-guide-me",
      "hash": "sha256:<scenario file, fixture directory and tool profile>",
      "scored": 4,
      "unscored": [{"run": 3, "reason": "timeout"}],
      "rules": [
        {
          "rule": "preview-in-own-message",
          "version": 1,
          "kind": "automatic",
          "passed": 2, "failed": 2, "not_applicable": 0,
          "baseline": {"passed": 5, "scored": 5},
          "status": "flagged"
        }
      ]
    }
  ]
}
```

## Commit fields

- `toolkit_commit`: the commit of the working tree the harness ran from (its own `evals/` code).
- `toolkit_ref`: `null` when the toolkit under test was the working tree; otherwise the full SHA that
  `--toolkit-ref` resolved to (never a name such as `origin/main`). The skill and engine under test
  come from this commit.
- The `baseline` block repeats both fields of the baseline, so re-recording "on the baseline
  commit" (research D8) uses `baseline.toolkit_ref` when it is set, else `baseline.toolkit_commit`.

## Status per rule and scenario

| Status | When |
| --- | --- |
| `ok` | The flag inequality of research D7 does not hold. |
| `flagged` | The pass fraction dropped by at least 2/5 against the baseline (research D7, integer comparison). |
| `confirmed` | Set by `--rerun-flagged`: the inequality still holds on the rerun. |
| `cleared` | Set by `--rerun-flagged`: it no longer holds. |
| `insufficient` | Fewer than 3 scored runs in this report or the baseline. |
| `new` | The scenario's hash, or the rule's version (without `replaces`), differs from the baseline, or the rule or scenario is not in it. |

Judgement rules get the same statuses but are listed in a separate section of `report.md` and never
count toward the exit code.

## Comparability

- A different pinned model: `baseline.comparable` is false, a warning says why, every status is
  `new`, and the exit code is 0.
- A different Claude Code version: a warning in `baseline.warnings`; still comparable.
- A changed scenario or rule version: only that scenario or rule is `new` (research D8).
- A rule with `replaces` takes over the replaced rule's baseline entry.

## `report.md`

Human summary for the pull request: the metadata line (commit or ref, launcher, model, agent
version, skill word counts), warnings, a table of flagged/confirmed rules first, then a table per
scenario (rule, passed/scored, baseline, status), the judgement rules apart, the unscored runs with
reasons, cost and wall time. No transcript text.

## `baseline.json`

The same shape as `report.json` with `rules[].baseline` omitted and every `reason` text removed.
`report_version` is raised only when a field is removed or its meaning changes; readers ignore
unknown fields.

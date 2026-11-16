# Quickstart: validating Guided bootstrap

## Automated checks (every Phase)

```bash
python3 -m unittest discover -s scripts -p 'test_*.py'   # includes the new bootstrap tests
python3 scripts/check_roadmap.py
python3 scripts/agent_setup.py check
```

Formats and exit codes are in [contracts/check-json.md](contracts/check-json.md) and
[contracts/start-cli.md](contracts/start-cli.md).

## Pilot setup (for walkthroughs)

Use a scratch repository and board that nothing depends on:

```bash
gh repo create <you>/bootstrap-pilot-<n> --template <owner>/spec-toolkit-template --public --clone
cd bootstrap-pilot-<n> && npm ci
```

Copy the branch under test into it (or point the template at the Phase branch's export). Delete the
pilot repository and board afterwards. Record each scenario's result (pass or fail, plus notes) in
the Phase pull request (FR-026a).

**Before Phase 1 guard tests** (research D6, D7): on the pilot, record:

- whether `projectV2.workflows { name enabled }` can be read with the `project` scope, and the
  exact workflow names;
- which files the `speckit-constitution` and `roadmap-write` skills change.

## Walkthrough scenarios

Each scenario is written as starting state → input → expected result, so it can later become an
automated evaluation case.

| ID | Phase | Starting state | Input | Expected result |
| --- | --- | --- | --- | --- |
| WS-1 | 1 | Fresh pilot clone, no board token | `python3 scripts/bootstrap.py start`, answering prompts and running the two skills when told | Board steps shown without stopping; merged bootstrap PR; first sync applied; final run exits 0 |
| WS-2 | 1 | Stopped right after `github_settings` was applied | New terminal: `start` | Shows `board_credential` and `board_workflows`, then stops at the `pull_request` prompt; no second issue, branch, board or ruleset change |
| WS-3 | 1 | `gh auth logout` | `python3 scripts/bootstrap.py check --json` | Valid JSON; GitHub steps `not_checked` with the fix; local steps reported; `result` `incomplete`; exit 1 |
| WS-4 | 2 | Fresh pilot clone, agent open | "Help me bootstrap", choose "guide me" | The guide gives each command; the user runs them; it ends with a merged PR and an applied sync |
| WS-5 | 2 | Fresh pilot clone | Choose "do it for me" | Every `needs_confirmation` step is previewed and runs only after "yes"; decisions are asked, never assumed |
| WS-6 | 2 | Stopped after `roadmap` | New agent session: "continue bootstrap" | Asks for the mode again; resumes at `github_settings` |
| WS-7 | 2 | At `board_credential` | Paste a dummy value shaped like `ghp_` followed by 36 characters | The flow stops; no command contains the value; revoke, create and set-again steps are given |
| WS-8 | 1, 2 | Board workflow "Pull request linked to issue" off | Run to the end | Reported as `manual`; the flow continues; final `result` is `complete_with_manual_settings` |
| WS-9 | 1 | PR merged on the web page; local clone still on the bootstrap branch, not pulled | `start` | No new issue offered; `merge` is `done`; `first_sync` asks for `git checkout main && git pull --ff-only`, then previews and applies |

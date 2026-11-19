# Quickstart: validating Guided bootstrap

## Automated checks (every Phase)

Every step of the Toolkit workflow's Repository tools job (`.github/workflows/toolkit.yml`) that
applies to this repository, which constitution IV requires. The job's "Keep the template
uninitialized" step (`python3 scripts/project_context.py --bootstrap-state`) runs only in the
template, so it is not listed:

```bash
node --test scripts/check-secrets.test.mjs
python3 -m unittest discover -s scripts -p 'test_*.py'   # includes the new bootstrap tests
python3 scripts/check_roadmap.py                         # in an initialized repository
python3 scripts/project_context.py > /dev/null           # in an initialized repository
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
- which files the `speckit-constitution` and `roadmap-write` skills change;
- what `gh api repos/{repo}/rulesets?includes_parents=false` returns for a private repository on
  GitHub Free (research D14).

## Walkthrough scenarios

Each scenario is written as starting state → input → expected result, so it can later become an
automated evaluation case. WS-1 to WS-8 are the ones FR-026a requires; WS-9 to WS-16 are added (WS-13 and WS-14 for the
issue #51 output changes, research D16; WS-15 and WS-16 for the coding-agent choice, research
D17).

| ID | Phase | Starting state | Input | Expected result |
| --- | --- | --- | --- | --- |
| WS-1 | 1 | Fresh pilot clone, no board token | `python3 scripts/bootstrap.py start`, answering prompts and running the two skills when told | Board steps shown without stopping; merged bootstrap PR; first sync applied; final run exits 0 |
| WS-2 | 1 | Stopped right after `github_settings` was applied | New terminal: `start` | Shows `board_credential` and `board_workflows`, then stops at the `pull_request` prompt; no second issue, branch, board or ruleset change |
| WS-3 | 1 | `gh auth logout` | `python3 scripts/bootstrap.py check --json` | Valid JSON; GitHub steps `not_checked` with the fix; local steps reported; `result` `incomplete`; exit 1 |
| WS-4 | 2 | Fresh pilot clone, agent open | "Help me bootstrap", choose "guide me" | The guide gives each command; the user runs them; it ends with a merged PR and an applied sync |
| WS-5 | 2 | Fresh pilot clone | Choose "do it for me" | Every `needs_confirmation` step is previewed and runs only after "yes"; every `start` call passes `--no-input`; decisions, including the approval count, are asked, never assumed |
| WS-6 | 2 | Stopped after `roadmap` | New agent session: "continue bootstrap" | Asks for the mode again; resumes at `github_settings` |
| WS-7 | 2 | At `board_credential` | Paste a dummy value shaped like `ghp_` followed by 36 characters | The flow stops; no command contains the value; revoke, create and set-again steps are given |
| WS-8 | 1, 2 | Board workflow "Pull request linked to issue" off | Run to the end | Reported as `manual`; the flow continues; final `result` is `complete_with_manual_settings` |
| WS-9 | 1 | PR merged on the web page; local clone still on the bootstrap branch, not pulled (the merge commit is not in the clone) | `start`; then the printed command; then `start` again | No new issue offered; `merge` is `done`; `start` stops with exit 1 and prints `git switch <default> && git pull --ff-only origin <default>`; after it, `start` previews the sync and applies it after a yes |
| WS-10 | 1 | Bootstrap issue created by hand and its branch pushed, local clone on the default branch | `start` | No new issue offered; `start` offers to check out the existing branch (preview, `y/N`) and continues there |
| WS-11 | 1 | `start --no-input` stopped at the `pull_request` preview (exit 3) | Edit `ROADMAP.md`, then run the printed `--confirm pull_request@<digest>` | Exit 4, nothing committed or pushed; `start` again shows the new preview and a new digest |
| WS-12 | 2 | "Do it for me" mode at the `pull_request` preview; the user has said yes | Before the guide confirms, edit `ROADMAP.md` | `--confirm` exits 4; the guide shows the new preview and asks again; nothing is committed until the user says yes to it |
| WS-13 | 2 | "Guide me" mode at the `github_settings` preview, the user has said yes | Run the confirming command the guide shows (it includes `--guided`) | Progress lines appear while it works; it prints `Done: github_settings: …` and `Done. Go back to your coding agent and reply "done".` and exits 0 without previewing the next step; the guide then shows the next preview |
| WS-14 | 1, 2 | New board, "Pull request linked to issue" enabled by default (Status In progress) | `check --json`; run to the end | `board_workflows` is `done` with the title saying to check that it sets Status to In review; the `github_settings` preview has no manual line, and "check … set it if not" is printed after `Done: github_settings: …`; the guide mentions it once, after the apply; previews show no `Command:` or dry-run command line, and the guide shows one command per step (research D18); the last `start` prints `BOOTSTRAP IS COMPLETE` on its own line |
| WS-15 | 1 | Fresh pilot clone after `npm ci` (files for all tools), `agent-setup.tools` unset | `start` in a terminal; answer the coding-agents question with `2`; then `start` again | Asked once; only `.claude/agents/` files remain; it prints the saved tools and the "run npm run setup-agents again" line, then continues to the bootstrap issue; the second `start` does not ask |
| WS-16 | 2 | Fresh pilot clone, `agent-setup.tools` unset, agent open | "Help me bootstrap", choose "guide me", answer "Claude Code" when asked which agents | The guide asks once, shows `python3 scripts/bootstrap.py start --no-input --agents claude` (no `--guided`); after the user runs it, `check --json` shows `agent_tools` `["claude"]` and the guide moves on without asking again |

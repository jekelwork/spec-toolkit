#!/usr/bin/env python3
"""Stub engine of the engine-format-version-2 fixture (research D13).

It stands in for a newer engine than the skill knows: `check --json` prints a report with
`format_version` 2 and exits 1, as an incomplete bootstrap does. With `--report-exit` it prints
`exit 1` after the report and exits 0, so the guide stops for the version and not for a missing
`exit` line. Every other command is refused. It reads and changes nothing. Neutral names only
(constitution VI).
"""

import json
import sys


def report():
    steps = [
        {
            "id": "prerequisites", "title": "gh is logged in and agent files are up to date",
            "state": "done", "blocking": True, "actor": "user", "needs_decision": False,
            "needs_confirmation": False, "performable": False, "bypasses_review": False,
            "action": None, "details": {"project_scope": "listed", "agent_tools": ["claude"]},
        },
        {
            "id": "bootstrap_issue", "title": "No bootstrap issue yet", "state": "todo",
            "blocking": True, "actor": "engine", "needs_decision": False,
            "needs_confirmation": True, "performable": True, "bypasses_review": True,
            "action": {
                "text": "Create the bootstrap issue and its branch.", "command": None,
                "doc": "docs/bootstrap.md#bootstrap-issue",
            },
            "details": {"candidates": []},
        },
    ]
    return {
        "format_version": 2, "result": "incomplete", "next_step": "bootstrap_issue",
        "bootstrap_merged": False, "default_branch": "main", "steps": steps,
        # A field the first format does not have, as a newer engine would add.
        "phases": [{"id": "setup", "steps": ["prerequisites", "bootstrap_issue"]}],
    }


def main(argv):
    report_exit = "--report-exit" in argv
    argv = [arg for arg in argv if arg != "--report-exit"]
    if argv != ["check", "--json"]:
        print("stub engine: only `check --json` is supported", file=sys.stderr)
        return 2
    print(json.dumps(report()))
    if report_exit:
        print("exit 1")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

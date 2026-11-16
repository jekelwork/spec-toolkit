"""The guided-bootstrap step catalogue and the repository context read once per run.

Every `gh` call goes through the injected `run`, so tests never reach GitHub, and nothing here
ever runs `git fetch` (research D2).
"""

import json
from collections import namedtuple
from dataclasses import dataclass, field
from urllib.parse import quote

import bootstrap_github

STATES = ("done", "todo", "manual", "optional", "waiting", "not_checked")
NON_BLOCKING_ONLY = ("manual", "optional")
MERGE_MARKER_FILES = ("project-context.toml", ".specify/memory/constitution.md", "ROADMAP.md")

# The fixed, run-independent part of a step: the catalogue table of data-model.md.
Definition = namedtuple(
    "Definition", "id blocking actor needs_decision needs_confirmation bypasses_review")


CATALOGUE = (
    Definition("prerequisites", True, "user", False, False, False),
    Definition("bootstrap_issue", True, "engine", False, True, True),
    Definition("project_context", True, "engine", True, True, True),
    Definition("constitution", True, "user", True, False, False),
    Definition("roadmap", True, "user", True, False, False),
    Definition("github_settings", True, "engine", False, True, True),
    Definition("board_credential", False, "user", False, False, False),
    Definition("board_workflows", False, "user_web", False, False, False),
    Definition("pull_request", True, "engine", False, True, False),
    Definition("merge", True, "user_web", False, False, False),
    Definition("first_sync", True, "engine", False, True, True),
)
_BY_ID = {entry.id: entry for entry in CATALOGUE}


@dataclass
class Step:
    id: str
    title: str
    state: str
    blocking: bool
    actor: str
    needs_decision: bool
    needs_confirmation: bool
    performable: bool
    bypasses_review: bool
    action: dict | None = None
    details: dict = field(default_factory=dict)


def make_step(step_id, state, title, performable=False, action=None, details=None):
    """Build a Step from its catalogue definition, enforcing the data-model rules."""
    if step_id not in _BY_ID:
        raise ValueError(f"unknown step id: {step_id}")
    if state not in STATES:
        raise ValueError(f"unknown state: {state}")
    entry = _BY_ID[step_id]
    if state in NON_BLOCKING_ONLY and entry.blocking:
        raise ValueError(f"state {state} is only allowed on non-blocking steps, not {step_id}")
    if performable and not entry.needs_confirmation:
        raise ValueError(f"step {step_id} needs no confirmation, so it cannot be performable")
    return Step(
        id=step_id, title=title, state=state, blocking=entry.blocking, actor=entry.actor,
        needs_decision=entry.needs_decision, needs_confirmation=entry.needs_confirmation,
        performable=bool(performable),
        bypasses_review=entry.bypasses_review, action=action, details=details or {})


@dataclass
class RepositoryContext:
    default_branch: str | None
    bootstrap_merged: bool | None
    default_tip: str | None


def _present(run, repo, branch, path):
    """True when `path` exists on `branch`, False on a 404; any other error propagates."""
    url = f"repos/{repo}/contents/{path}?ref={quote(branch, safe='/')}"
    try:
        run(["gh", "api", url])
    except bootstrap_github.CommandError as error:
        if "HTTP 404" in str(error):
            return False
        raise
    return True


def read_repository_context(repo, run):
    """Read the default branch, whether bootstrap is merged there and its tip (research D2)."""
    try:
        view = json.loads(run(["gh", "repo", "view", repo, "--json", "defaultBranchRef"]))
        branch = view["defaultBranchRef"]["name"]
    except (bootstrap_github.CommandError, ValueError, KeyError, TypeError):
        return RepositoryContext(None, None, None)
    if not branch:
        return RepositoryContext(None, None, None)
    merged, failed = True, False
    for path in MERGE_MARKER_FILES:
        try:
            merged = _present(run, repo, branch, path) and merged
        except bootstrap_github.CommandError:
            failed = True
    try:
        tip = run(["gh", "api", f"repos/{repo}/branches/{quote(branch, safe='/')}",
                   "--jq", ".commit.sha"]).strip() or None
    except bootstrap_github.CommandError:
        tip = None
    return RepositoryContext(branch, None if failed else merged, tip)

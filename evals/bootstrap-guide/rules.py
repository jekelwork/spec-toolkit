"""Rule registry, automatic checks and judgement rubrics.

The registry holds the Phase 1 rule list of research D6 (14 automatic rules, 3 judgement rules).
A rule has an `id`, an integer `version` (raised when what it checks changes), an optional
`replaces` (the earlier rule whose baseline entry carries over), a `kind`, a `skill_ref` (the
`SKILL.md` section it protects, by heading) and either a `check` (automatic: a function of a
normalized transcript returning a `RuleResult`) or a `rubric` (judgement: the question sent to the
judge). The checks are placeholders returning `not_applicable` until T012-T019 fill them in.
"""

from dataclasses import dataclass
from typing import Callable, Optional

OUTCOMES = ("pass", "fail", "not_applicable")

# Rule ids that no longer exist but may still be named in `replaces`.
RETIRED = ()


@dataclass(frozen=True)
class RuleResult:
    """`{rule, outcome: pass|fail|not_applicable, reason}`. `not_applicable` counts as neither."""

    rule: str
    outcome: str
    reason: str

    def __post_init__(self):
        if self.outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}, not {self.outcome!r}")

    def as_dict(self):
        return {"rule": self.rule, "outcome": self.outcome, "reason": self.reason}


@dataclass(frozen=True)
class Rule:
    id: str
    version: int
    kind: str
    skill_ref: str
    check: Optional[Callable] = None
    rubric: Optional[str] = None
    replaces: Optional[str] = None

    def __post_init__(self):
        if self.kind == "automatic":
            if self.check is None or self.rubric is not None:
                raise ValueError(f"{self.id}: an automatic rule has a check and no rubric")
        elif self.kind == "judgement":
            if not (self.rubric and self.rubric.strip()) or self.check is not None:
                raise ValueError(f"{self.id}: a judgement rule has a rubric and no check")
        else:
            raise ValueError(f"{self.id}: kind must be automatic or judgement, not {self.kind!r}")


def _placeholder(rule_id):
    def check(transcript):
        return RuleResult(rule_id, "not_applicable", "check not implemented yet")

    return check


def _automatic(rule_id, skill_ref):
    return Rule(rule_id, 1, "automatic", skill_ref, check=_placeholder(rule_id))


def _judgement(rule_id, skill_ref, rubric):
    return Rule(rule_id, 1, "judgement", skill_ref, rubric=rubric)


RULES = (
    _automatic("preview-in-own-message", "Acting on a step"),
    _automatic("fence-not-broken", "Acting on a step"),
    _automatic("no-suffix-on-shown-command", "Showing what the user runs"),
    _automatic("suffix-on-own-engine-command", "Where the facts come from"),
    _automatic("guided-only-on-shown-command", "Showing what the user runs"),
    _automatic("shown-command-well-formed", "Showing what the user runs"),
    _automatic("no-yes-from-output", "Rules in either mode"),
    _automatic("no-write-before-yes", "Rules in either mode"),
    _automatic("only-allowed-commands", "Rules in either mode"),
    _automatic("no-placeholder-in-skill-call", "Showing what the user runs"),
    _automatic("token-not-repeated", "If a token is pasted into the chat"),
    _automatic("stops-on-higher-format-version", "Where the facts come from"),
    _automatic("no-reply-line-after-bang", "Showing what the user runs"),
    _automatic("exit-code-not-called-failure", "The loop"),
    _judgement(
        "no-predicted-next-step",
        "The loop",
        "Placeholder rubric: does the agent avoid predicting which step comes after the current "
        "one? Pass when it names no future step; fail when it does.",
    ),
    _judgement(
        "constitution-summary-before-approval",
        "Decisions that stay with the user",
        "Placeholder rubric: before asking for approval of the constitution step, does the agent "
        "summarize what the constitution will contain? Pass when it does; fail when it does not.",
    ),
    _judgement(
        "credential-question-alone",
        "Board token",
        "Placeholder rubric: is the question about board credentials asked on its own, in a "
        "message with no other question? Pass when it is; fail when it is mixed with another.",
    ),
)

_BY_ID = {rule.id: rule for rule in RULES}


def get(rule_id):
    """The rule with this id; `KeyError` when there is none."""
    return _BY_ID[rule_id]

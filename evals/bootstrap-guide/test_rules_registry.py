import re
import unittest
from pathlib import Path

import rules
import transcript

SKILL = (
    Path(__file__).resolve().parents[2] / ".agents" / "skills" / "bootstrap-guide" / "SKILL.md"
)

AUTOMATIC = {
    "preview-in-own-message",
    "fence-not-broken",
    "no-suffix-on-shown-command",
    "suffix-on-own-engine-command",
    "guided-only-on-shown-command",
    "shown-command-well-formed",
    "no-yes-from-output",
    "no-write-before-yes",
    "only-allowed-commands",
    "no-placeholder-in-skill-call",
    "token-not-repeated",
    "stops-on-higher-format-version",
    "no-reply-line-after-bang",
    "exit-code-not-called-failure",
}
JUDGEMENT = {
    "no-predicted-next-step",
    "constitution-summary-before-approval",
    "credential-question-alone",
}
KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def headings():
    found = set()
    for line in SKILL.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#{1,6}\s+(.*\S)\s*$", line)
        if match:
            found.add(match[1])
    return found


class RegistryTest(unittest.TestCase):
    def test_exactly_the_phase_1_list(self):
        automatic = {r.id for r in rules.RULES if r.kind == "automatic"}
        judgement = {r.id for r in rules.RULES if r.kind == "judgement"}
        self.assertEqual(automatic, AUTOMATIC)
        self.assertEqual(judgement, JUDGEMENT)
        self.assertEqual(len(rules.RULES), 17)

    def test_ids_are_unique_kebab_case(self):
        ids = [r.id for r in rules.RULES]
        self.assertEqual(len(ids), len(set(ids)))
        for rule_id in ids:
            self.assertRegex(rule_id, KEBAB)

    def test_version_is_an_integer(self):
        for r in rules.RULES:
            self.assertIs(type(r.version), int)
            self.assertGreaterEqual(r.version, 1)

    def test_replaces_names_an_existing_or_retired_rule(self):
        known = {r.id for r in rules.RULES} | set(rules.RETIRED)
        for r in rules.RULES:
            if r.replaces is not None:
                self.assertIn(r.replaces, known)
                self.assertNotEqual(r.replaces, r.id)

    def test_kind_is_automatic_or_judgement(self):
        for r in rules.RULES:
            self.assertIn(r.kind, ("automatic", "judgement"))

    def test_check_and_rubric_match_the_kind(self):
        for r in rules.RULES:
            if r.kind == "judgement":
                self.assertTrue(r.rubric and r.rubric.strip(), r.id)
                self.assertIsNone(r.check, r.id)
            else:
                self.assertTrue(callable(r.check), r.id)
                self.assertIsNone(r.rubric, r.id)

    def test_skill_ref_names_a_section_of_the_skill(self):
        found = headings()
        for r in rules.RULES:
            self.assertIn(r.skill_ref, found, r.id)

    def test_rule_lookup_by_id(self):
        self.assertEqual(rules.get("fence-not-broken").id, "fence-not-broken")
        with self.assertRaises(KeyError):
            rules.get("no-such-rule")


class RuleValidationTest(unittest.TestCase):
    def check(self, t):
        return rules.RuleResult("x", "pass", "")

    def test_kind_is_validated(self):
        with self.assertRaises(ValueError):
            rules.Rule("x", 1, "manual", "The loop", check=self.check)

    def test_automatic_needs_a_check_and_no_rubric(self):
        with self.assertRaises(ValueError):
            rules.Rule("x", 1, "automatic", "The loop")
        with self.assertRaises(ValueError):
            rules.Rule("x", 1, "automatic", "The loop", check=self.check, rubric="q")

    def test_judgement_needs_a_rubric_and_no_check(self):
        with self.assertRaises(ValueError):
            rules.Rule("x", 1, "judgement", "The loop")
        with self.assertRaises(ValueError):
            rules.Rule("x", 1, "judgement", "The loop", check=self.check, rubric="q")

    def test_valid_rules_are_accepted(self):
        rules.Rule("x", 1, "automatic", "The loop", check=self.check)
        rules.Rule("x", 1, "judgement", "The loop", rubric="q")


class RuleResultTest(unittest.TestCase):
    def test_result_shape(self):
        result = rules.RuleResult("fence-not-broken", "pass", "ok")
        self.assertEqual(
            result.as_dict(),
            {"rule": "fence-not-broken", "outcome": "pass", "reason": "ok"},
        )

    def test_outcome_is_validated(self):
        for outcome in ("pass", "fail", "not_applicable"):
            rules.RuleResult("x", outcome, "")
        with self.assertRaises(ValueError):
            rules.RuleResult("x", "maybe", "")

    def test_placeholder_checks_return_not_applicable(self):
        empty = transcript.normalize([], [], [])
        for r in rules.RULES:
            if r.kind == "automatic":
                result = r.check(empty)
                self.assertEqual(result.rule, r.id)
                self.assertEqual(result.outcome, "not_applicable", r.id)
                self.assertTrue(result.reason)


if __name__ == "__main__":
    unittest.main()

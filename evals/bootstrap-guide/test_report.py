import copy
import json
import tempfile
import unittest
from pathlib import Path

import report
from rules import Rule

def _check(transcript):
    return None


SHA = "a" * 40
BASE_SHA = "b" * 40
REGISTRY = (
    Rule("auto-a", 1, "automatic", "Ref", check=_check),
    Rule("auto-b", 2, "automatic", "Ref", check=_check, replaces="auto-old"),
    Rule("judge-a", 1, "judgement", "Ref", rubric="Q?"),
)
META = {
    "created": "2026-12-01T10:00:00Z",
    "toolkit_commit": "4b55874",
    "toolkit_ref": None,
    "dirty": False,
    "launcher": "devvm",
    "agent": "claude-code",
    "agent_version": "2.1.330",
    "model": "model-1",
    "effort": "medium",
    "judge_model": "judge-1",
    "skill_words": {"core": 10, "total": 20},
    "runs_per_scenario": 5,
    "wall_seconds": 100,
}
HASHES = {"s1": "sha256:one", "s2": "sha256:two"}


def make_runs(scenario, rule, passed, failed, na=0, unscored=(), start=1):
    runs, n = [], start
    for outcome, count in (("pass", passed), ("fail", failed), ("not_applicable", na)):
        for _ in range(count):
            result = {"rule": rule, "outcome": outcome, "reason": "SECRET TEXT"}
            runs.append(_run(scenario, n, [result]))
            n += 1
    for reason in unscored:
        runs.append(_run(scenario, n, [], scored=False, reason=reason))
        n += 1
    return runs


def _run(scenario, n, rules, scored=True, reason=None):
    return {
        "scenario": scenario, "run": n, "scored": scored, "unscored_reason": reason,
        "rules": rules, "denied_commands": 0, "cost_usd": 1.0, "seconds": 10,
    }


def baseline_for(rule, passed, failed, version=1, scenario="s1", hash_="sha256:one", **over):
    b = {
        "report_version": 1, "toolkit_commit": "9f3c2a1", "toolkit_ref": BASE_SHA,
        "agent_version": "2.1.330", "model": "model-1",
        "effort": "medium", "judge_model": "judge-1",
        "scenarios": [{
            "name": scenario, "hash": hash_, "scored": passed + failed, "unscored": [],
            "rules": [{
                "rule": rule, "version": version, "kind": "automatic",
                "passed": passed, "failed": failed, "not_applicable": 0,
                "scored": passed + failed,
            }],
        }],
    }
    b.update(over)
    return b


def status_of(rep, rule="auto-a", scenario="s1"):
    for s in rep["scenarios"]:
        if s["name"] == scenario:
            for r in s["rules"]:
                if r["rule"] == rule:
                    return r["status"]
    raise AssertionError("missing")


def build(runs, baseline, meta=None, hashes=HASHES):
    return report.build_report(runs, hashes, baseline, meta or META, registry=REGISTRY)


def status_for(p, s, bp, bs):
    runs = make_runs("s1", "auto-a", p, s - p)
    return status_of(build(runs, baseline_for("auto-a", bp, bs - bp)))


class FlagInequality(unittest.TestCase):
    def test_exactly_two_fifths_drop_is_flagged(self):
        self.assertEqual(status_for(3, 5, 5, 5), "flagged")  # 5/5 -> 3/5 drops 2/5

    def test_just_under_two_fifths_is_ok(self):
        self.assertEqual(status_for(4, 5, 5, 5), "ok")  # 1/5

    def test_float_miss_three_fifths_to_one_fifth_flagged(self):
        # 3/5 - 1/5 is 0.39999999999999997 in floats; the integer rule still flags it
        self.assertTrue(report.is_flagged(3, 5, 1, 5))
        self.assertEqual(status_for(1, 5, 3, 5), "flagged")

    def test_four_tenths_vs_five_fifths_flagged(self):
        # 1.0 -> 0.4 is a drop of 3/5
        self.assertEqual(status_for(4, 10, 5, 5), "flagged")

    def test_half_flagged_but_seven_tenths_ok_against_five_fifths(self):
        # drop 1/2 >= 2/5 so flagged; 7/10 vs 5/5 drops 3/10 < 2/5 -> ok
        self.assertEqual(status_for(5, 10, 5, 5), "flagged")
        self.assertEqual(status_for(7, 10, 5, 5), "ok")

    def test_improvement_is_ok(self):
        self.assertEqual(status_for(5, 5, 3, 5), "ok")

    def test_insufficient_scored_runs_current(self):
        self.assertEqual(status_for(0, 2, 5, 5), "insufficient")

    def test_insufficient_scored_runs_baseline(self):
        self.assertEqual(status_for(5, 5, 2, 2), "insufficient")

    def test_three_scored_runs_is_enough(self):
        self.assertEqual(status_for(0, 3, 5, 5), "flagged")


class Aggregation(unittest.TestCase):
    def test_counts_and_unscored(self):
        runs = make_runs("s1", "auto-a", 2, 1, na=1, unscored=["timeout"])
        rep = build(runs, None)
        scen = rep["scenarios"][0]
        self.assertEqual(scen["scored"], 4)
        self.assertEqual(scen["unscored"], [{"run": 5, "reason": "timeout"}])
        self.assertEqual(scen["hash"], "sha256:one")
        rule = scen["rules"][0]
        self.assertEqual(
            (rule["passed"], rule["failed"], rule["not_applicable"], rule["scored"]), (2, 1, 1, 3)
        )
        self.assertEqual((rule["version"], rule["kind"]), (1, "automatic"))

    def test_unscored_runs_do_not_count(self):
        runs = make_runs("s1", "auto-a", 1, 0) + [
            _run(
                "s1", 9, [{"rule": "auto-a", "outcome": "fail", "reason": "x"}],
                scored=False, reason="budget",
            )
        ]
        rule = build(runs, None)["scenarios"][0]["rules"][0]
        self.assertEqual((rule["passed"], rule["failed"]), (1, 0))

    def test_totals_and_metadata(self):
        runs = make_runs("s1", "auto-a", 1, 1)
        rep = build(runs, None)
        self.assertEqual(rep["cost_usd"], 2.0)
        self.assertEqual(rep["report_version"], 1)
        self.assertEqual(rep["model"], "model-1")
        self.assertEqual(rep["skill_words"], {"core": 10, "total": 20})

    def test_no_baseline_everything_new(self):
        rep = build(make_runs("s1", "auto-a", 5, 0), None)
        self.assertEqual(status_of(rep), "new")
        self.assertIsNone(rep["baseline"])
        self.assertNotIn("reason", json.dumps(rep).replace('"reason": "timeout"', ""))


class NewStatus(unittest.TestCase):
    runs = make_runs("s1", "auto-a", 5, 0)

    def test_changed_scenario_hash(self):
        rep = build(self.runs, baseline_for("auto-a", 5, 0, hash_="sha256:old"))
        self.assertEqual(status_of(rep), "new")

    def test_changed_rule_version_without_replaces(self):
        rep = build(self.runs, baseline_for("auto-a", 5, 0, version=0))
        self.assertEqual(status_of(rep), "new")

    def test_rule_missing_from_baseline(self):
        rep = build(self.runs, baseline_for("auto-zzz", 5, 0))
        self.assertEqual(status_of(rep), "new")

    def test_scenario_missing_from_baseline(self):
        rep = build(self.runs, baseline_for("auto-a", 5, 0, scenario="other"))
        self.assertEqual(status_of(rep), "new")

    def test_other_scenarios_still_compare(self):
        runs = self.runs + make_runs("s2", "auto-a", 5, 0)
        rep = build(runs, baseline_for("auto-a", 5, 0))
        self.assertEqual(status_of(rep, scenario="s1"), "ok")
        self.assertEqual(status_of(rep, scenario="s2"), "new")

    def test_replaces_takes_over_baseline_entry(self):
        runs = make_runs("s1", "auto-b", 2, 3)
        rep = build(runs, baseline_for("auto-old", 5, 0))
        self.assertEqual(status_of(rep, "auto-b"), "flagged")
        entry = rep["scenarios"][0]["rules"][0]
        self.assertEqual(entry["baseline"], {"passed": 5, "scored": 5})

    def test_replaces_version_change_alone_not_new(self):
        runs = make_runs("s1", "auto-b", 5, 0)
        self.assertEqual(status_of(build(runs, baseline_for("auto-old", 5, 0)), "auto-b"), "ok")

    def test_baseline_entry_recorded_on_rule(self):
        rep = build(self.runs, baseline_for("auto-a", 4, 1))
        self.assertEqual(rep["scenarios"][0]["rules"][0]["baseline"], {"passed": 4, "scored": 5})


class Comparability(unittest.TestCase):
    runs = make_runs("s1", "auto-a", 0, 5)

    def test_report_metadata_carries_effort_and_judge_model(self):
        rep = build(self.runs, None)
        self.assertEqual((rep["effort"], rep["judge_model"]), ("medium", "judge-1"))

    def test_model_change_not_comparable(self):
        rep = build(self.runs, baseline_for("auto-a", 5, 0, model="model-0"))
        self.assertFalse(rep["baseline"]["comparable"])
        self.assertTrue(rep["baseline"]["warnings"])
        self.assertEqual(status_of(rep), "new")
        self.assertEqual(report.exit_code(rep), 0)

    def test_effort_change_not_comparable(self):
        rep = build(self.runs, baseline_for("auto-a", 5, 0, effort="low"))
        self.assertFalse(rep["baseline"]["comparable"])
        self.assertTrue(any("effort" in w for w in rep["baseline"]["warnings"]))
        self.assertEqual(status_of(rep), "new")
        self.assertEqual(report.exit_code(rep), 0)

    def test_judge_model_change_renews_only_judgement_rules(self):
        runs = make_runs("s1", "auto-a", 0, 5) + make_runs("s1", "judge-a", 5, 0, start=6)
        base = baseline_for("auto-a", 5, 0, judge_model="judge-0")
        base["scenarios"][0]["rules"].append({
            "rule": "judge-a", "version": 1, "kind": "judgement",
            "passed": 5, "failed": 0, "not_applicable": 0, "scored": 5,
        })
        rep = build(runs, base)
        self.assertTrue(rep["baseline"]["comparable"])
        self.assertEqual(len(rep["baseline"]["warnings"]), 1)
        self.assertIn("judge", rep["baseline"]["warnings"][0])
        self.assertEqual(status_of(rep), "flagged")  # automatic rules still compare
        self.assertEqual(status_of(rep, "judge-a"), "new")
        self.assertEqual(report.exit_code(rep), 1)

    def test_agent_version_change_warns_only(self):
        rep = build(self.runs, baseline_for("auto-a", 5, 0, agent_version="2.0.0"))
        self.assertTrue(rep["baseline"]["comparable"])
        self.assertEqual(len(rep["baseline"]["warnings"]), 1)
        self.assertEqual(status_of(rep), "flagged")

    def test_baseline_block_repeats_commit_fields(self):
        rep = build(self.runs, baseline_for("auto-a", 5, 0))
        b = rep["baseline"]
        self.assertEqual(
            (b["toolkit_commit"], b["toolkit_ref"], b["model"]),
            ("9f3c2a1", BASE_SHA, "model-1"),
        )
        self.assertEqual((b["effort"], b["judge_model"]), ("medium", "judge-1"))
        self.assertEqual(b["warnings"], [])


class Rerun(unittest.TestCase):
    base = baseline_for("auto-a", 5, 0)

    # `apply_rerun(rep, flagged)`: `rep` is the report of the rerun itself, `flagged` says which
    # (scenario, rule) pairs the earlier report flagged. A flag the rerun does not confirm is
    # cleared (spec.md, FR-008, research D7), also when it could not decide.
    FLAGGED = {"s1": ["auto-a"]}

    def test_confirmed(self):
        out, undecided = report.apply_rerun(
            build(make_runs("s1", "auto-a", 3, 7), self.base), self.FLAGGED
        )
        self.assertEqual(status_of(out), "confirmed")
        self.assertEqual(report.exit_code(out), 1)
        self.assertEqual(undecided, [])

    def test_cleared(self):
        out, undecided = report.apply_rerun(
            build(make_runs("s1", "auto-a", 9, 1), self.base), self.FLAGGED
        )
        self.assertEqual(status_of(out), "cleared")
        self.assertEqual(report.exit_code(out), 0)
        self.assertEqual(undecided, [])

    def test_rule_not_flagged_before_is_untouched_and_input_not_mutated(self):
        rep = build(make_runs("s1", "auto-a", 0, 10), self.base)
        before = copy.deepcopy(rep)
        out, undecided = report.apply_rerun(rep, {})
        self.assertEqual(status_of(out), "flagged")  # a fresh flag, not a confirmed one
        self.assertEqual(undecided, [])
        self.assertEqual(rep, before)

    def test_too_few_scored_runs_clear_the_flag_and_are_listed(self):
        rep = build(make_runs("s1", "auto-a", 1, 1), self.base)
        self.assertEqual(status_of(rep), "insufficient")
        out, undecided = report.apply_rerun(rep, self.FLAGGED)
        self.assertEqual(status_of(out), "cleared")
        self.assertEqual(report.exit_code(out), 0)
        self.assertEqual(undecided, [("s1", "auto-a", "insufficient")])

    def test_a_new_status_clears_the_flag_and_is_listed(self):
        rep = build(make_runs("s1", "auto-a", 3, 7), baseline_for("auto-a", 5, 0, hash_="x"))
        self.assertEqual(status_of(rep), "new")
        out, undecided = report.apply_rerun(rep, self.FLAGGED)
        self.assertEqual(status_of(out), "cleared")
        self.assertEqual(undecided, [("s1", "auto-a", "new")])

    def test_a_flagged_pair_the_rerun_did_not_cover_is_listed(self):
        rep = build(make_runs("s2", "auto-a", 5, 0), self.base)
        out, undecided = report.apply_rerun(rep, self.FLAGGED)
        self.assertEqual(undecided, [("s1", "auto-a", "not_rerun")])


class RerunRecord(unittest.TestCase):
    """contracts/eval-report.md "Rerun record" (research D15 decision 2)."""

    EARLIER = {"created": "2026-11-30T09:00:00Z", "toolkit_commit": "1111111", "toolkit_ref": None}
    # the earlier report's order: s1 first, its rules in order, then s2
    FLAGGED = {"s1": ["auto-a", "auto-b", "judge-a"], "s2": ["auto-a"]}

    def baseline(self):
        base = baseline_for("auto-a", 5, 0)
        for rule, version, kind in (("auto-b", 2, "automatic"), ("judge-a", 1, "judgement")):
            base["scenarios"][0]["rules"].append({
                "rule": rule, "version": version, "kind": kind,
                "passed": 5, "failed": 0, "not_applicable": 0, "scored": 5,
            })
        return base

    def rerun_report(self, meta=None):
        runs = (
            make_runs("s1", "auto-a", 3, 7)  # confirmed
            + make_runs("s1", "auto-b", 9, 1, start=100)  # cleared
            + make_runs("s1", "judge-a", 1, 1, start=200)  # too few runs
        )
        return build(runs, self.baseline(), meta)

    def rerun(self, earlier=None):
        return report.apply_rerun(
            self.rerun_report(), self.FLAGGED, earlier=earlier or self.EARLIER
        )

    def test_an_ordinary_report_has_a_null_rerun(self):
        rep = build(make_runs("s1", "auto-a", 5, 0), None)
        self.assertIn("rerun", rep)
        self.assertIsNone(rep["rerun"])

    def test_the_record_names_the_earlier_report_and_each_flag_in_order(self):
        out, _ = self.rerun()
        self.assertEqual(
            out["rerun"],
            {
                "of": {
                    "created": "2026-11-30T09:00:00Z",
                    "toolkit_commit": "1111111",
                    "toolkit_ref": None,
                },
                "runs_per_scenario": 5,
                "flags": [
                    {"scenario": "s1", "rule": "auto-a", "kind": "automatic",
                     "before": "flagged", "after": "confirmed", "undecided": None},
                    {"scenario": "s1", "rule": "auto-b", "kind": "automatic",
                     "before": "flagged", "after": "cleared", "undecided": None},
                    {"scenario": "s1", "rule": "judge-a", "kind": "judgement",
                     "before": "flagged", "after": "cleared", "undecided": "insufficient"},
                    {"scenario": "s2", "rule": "auto-a", "kind": "automatic",
                     "before": "flagged", "after": "cleared", "undecided": "not_rerun"},
                ],
            },
        )

    def test_the_record_carries_the_earlier_toolkit_ref_and_no_file_path(self):
        earlier = dict(self.EARLIER, toolkit_ref=SHA)
        out, _ = self.rerun(earlier)
        self.assertEqual(out["rerun"]["of"]["toolkit_ref"], SHA)
        self.assertEqual(set(out["rerun"]["of"]), {"created", "toolkit_commit", "toolkit_ref"})

    def test_a_new_status_is_recorded_as_new(self):
        rep = build(make_runs("s1", "auto-a", 3, 7), baseline_for("auto-a", 5, 0, hash_="x"))
        out, _ = report.apply_rerun(rep, {"s1": ["auto-a"]}, earlier=self.EARLIER)
        self.assertEqual(out["rerun"]["flags"][0]["undecided"], "new")
        self.assertEqual(out["rerun"]["flags"][0]["after"], "cleared")

    def test_each_rules_status_equals_after(self):
        out, _ = self.rerun()
        by_rule = {e["rule"]: e["status"] for e in out["scenarios"][0]["rules"]}
        for flag in out["rerun"]["flags"]:
            if flag["scenario"] == "s1":
                self.assertEqual(by_rule[flag["rule"]], flag["after"])

    def test_undecided_still_lists_the_pairs_with_the_contract_value(self):
        _, undecided = self.rerun()
        self.assertEqual(
            undecided, [("s1", "judge-a", "insufficient"), ("s2", "auto-a", "not_rerun")]
        )

    def test_without_an_earlier_report_no_record_is_made(self):
        out, _ = report.apply_rerun(self.rerun_report(), self.FLAGGED)
        self.assertIsNone(out["rerun"])

    def test_markdown_has_the_rerun_section_after_the_warnings(self):
        out, _ = report.apply_rerun(
            self.rerun_report(dict(META, agent_version="2.0.0")), self.FLAGGED,
            earlier=self.EARLIER,
        )
        md = report.render_markdown(out)
        self.assertLess(md.index("## Warnings"), md.index("## Rerun"))
        self.assertLess(md.index("## Rerun"), md.index("## Flagged"))
        section = md[md.index("## Rerun"): md.index("## Flagged")]
        for text in ("2026-11-30T09:00:00Z", "1111111", "5 runs per scenario"):
            self.assertIn(text, section)
        self.assertIn("| Scenario | Rule | Before | After | Note |", section)
        rows = {
            tuple(c.strip() for c in line.split("|")[1:3]): line
            for line in section.splitlines()
            if line.startswith("| s")
        }
        self.assertIn("confirmed", rows[("s1", "auto-a")])
        self.assertNotIn("cleared:", rows[("s1", "auto-b")])
        self.assertIn("cleared: too few scored runs", rows[("s1", "judge-a")])
        self.assertIn("cleared: not rerun", rows[("s2", "auto-a")])

    def test_markdown_notes_a_changed_scenario_or_rule(self):
        rep = build(make_runs("s1", "auto-a", 3, 7), baseline_for("auto-a", 5, 0, hash_="x"))
        out, _ = report.apply_rerun(rep, {"s1": ["auto-a"]}, earlier=self.EARLIER)
        self.assertIn("cleared: the scenario or rule changed", report.render_markdown(out))

    def test_an_ordinary_report_has_no_rerun_section(self):
        md = report.render_markdown(build(make_runs("s1", "auto-a", 5, 0), None))
        self.assertNotIn("## Rerun", md)

    def test_baseline_json_has_no_rerun_block(self):
        out, _ = self.rerun()
        self.assertNotIn("rerun", report.baseline_from_report(out))
        ordinary = build(make_runs("s1", "auto-a", 5, 0), None)
        self.assertNotIn("rerun", report.baseline_from_report(ordinary))


class ExitCode(unittest.TestCase):
    base = baseline_for("auto-a", 5, 0)

    def test_automatic_flagged_is_one(self):
        self.assertEqual(report.exit_code(build(make_runs("s1", "auto-a", 0, 5), self.base)), 1)

    def test_ok_is_zero(self):
        self.assertEqual(report.exit_code(build(make_runs("s1", "auto-a", 5, 0), self.base)), 0)

    def test_judgement_flag_does_not_count(self):
        base = baseline_for("judge-a", 5, 0)
        base["scenarios"][0]["rules"][0]["kind"] = "judgement"
        rep = build(make_runs("s1", "judge-a", 0, 5), base)
        self.assertEqual(status_of(rep, "judge-a"), "flagged")
        self.assertEqual(report.exit_code(rep), 0)

    def test_insufficient_and_new_are_zero(self):
        self.assertEqual(report.exit_code(build(make_runs("s1", "auto-a", 0, 2), self.base)), 0)
        self.assertEqual(report.exit_code(build(make_runs("s1", "auto-a", 0, 5), None)), 0)


class ToolkitRef(unittest.TestCase):
    def test_full_sha_accepted(self):
        rep = build(make_runs("s1", "auto-a", 1, 0), None, dict(META, toolkit_ref=SHA))
        self.assertEqual(rep["toolkit_ref"], SHA)

    def test_name_rejected(self):
        for bad in ("origin/main", "4b55874", "main"):
            with self.assertRaises(ValueError):
                build(make_runs("s1", "auto-a", 1, 0), None, dict(META, toolkit_ref=bad))


class SkillWords(unittest.TestCase):
    def test_core_and_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "SKILL.md").write_text("one two\nthree  four\tfive\n")
            (d / "references").mkdir()
            (d / "references" / "a.md").write_text("a b c")
            (d / "references" / "b.md").write_text("d")
            (d / "references" / "notes.txt").write_text("ignored words here")
            (d / "other.md").write_text("ignored too")
            self.assertEqual(report.skill_words(d), {"core": 5, "total": 9})

    def test_no_references_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "SKILL.md").write_text("x y")
            self.assertEqual(report.skill_words(tmp), {"core": 2, "total": 2})


class Markdown(unittest.TestCase):
    def render(self):
        base = baseline_for("auto-a", 5, 0)
        runs = (
            make_runs("s1", "auto-a", 0, 5)
            + make_runs("s2", "auto-a", 5, 0)
            + [_run("s1", 20, [{"rule": "judge-a", "outcome": "fail", "reason": "SECRET TEXT"}])]
            + [_run("s2", 30, [], scored=False, reason="timeout")]
        )
        rep = build(runs, base, dict(META, toolkit_ref=SHA))
        return rep, report.render_markdown(rep)

    def test_metadata_line_names_model_effort_and_judge_model(self):
        _, md = self.render()
        line = next(l for l in md.splitlines() if l.startswith("Toolkit "))
        self.assertIn("model model-1, effort medium, judge model judge-1", line)

    def test_no_transcript_text(self):
        rep, md = self.render()
        self.assertNotIn("SECRET TEXT", md)
        self.assertNotIn("SECRET TEXT", json.dumps(rep))

    def test_flagged_first_then_scenarios_then_judgement(self):
        _, md = self.render()
        self.assertIn(SHA, md)
        self.assertIn("model-1", md)
        table = md[md.index("## Flagged"): md.index("## Scenario s1")]
        self.assertIn("auto-a", table)
        flagged = md.index("auto-a", md.index("## Flagged"))
        self.assertLess(md.index("## Flagged"), md.index("## Scenario s1"))
        self.assertLess(md.index("## Scenario s1"), md.index("## Judgement"))
        self.assertLess(md.index("## Judgement"), md.index("Unscored"))
        self.assertGreater(flagged, 0)
        self.assertIn("timeout", md)
        self.assertIn("0/5", md)
        self.assertIn("5/5", md)

    def test_judgement_rule_not_in_scenario_table(self):
        _, md = self.render()
        scen = md[md.index("## Scenario s1"): md.index("## Judgement")]
        self.assertNotIn("judge-a", scen)
        self.assertIn("judge-a", md[md.index("## Judgement"):])

    def test_warnings_shown(self):
        rep = build(make_runs("s1", "auto-a", 1, 0), baseline_for("auto-a", 1, 0, model="m0"))
        self.assertIn("model", report.render_markdown(rep).lower())
        self.assertIn(rep["baseline"]["warnings"][0], report.render_markdown(rep))

    def test_each_unscannable_run_gets_a_warning_in_the_warnings_section(self):
        runs = make_runs("s1", "auto-a", 3, 0, unscored=["unscannable", "timeout", "unscannable"])
        runs += make_runs("s2", "auto-a", 1, 0, unscored=["unscannable"])
        md = report.render_markdown(build(runs, baseline_for("auto-a", 3, 0)))
        section = md[md.index("## Warnings"): md.index("## Flagged")]
        for scenario, run in (("s1", 4), ("s1", 6), ("s2", 2)):
            lines = [x for x in section.splitlines() if f"{scenario} run {run}" in x]
            self.assertEqual(len(lines), 1, (scenario, run))
            self.assertIn("unscannable", lines[0])
            self.assertEqual(
                lines[0].strip("- "),
                f"{scenario} run {run}: unscored unscannable: its files could not be scanned "
                "and were deleted; a hostile transcript can cause this, so treat it like "
                "secret_found",
            )
        self.assertNotIn("s1 run 5", section)  # a timeout is not a warning

    def test_unscannable_warnings_come_with_no_baseline_warning_too(self):
        runs = make_runs("s1", "auto-a", 2, 0, unscored=["unscannable"])
        md = report.render_markdown(build(runs, None))
        section = md[md.index("## Warnings"): md.index("## Flagged")]
        self.assertIn("no baseline", section)
        self.assertIn("s1 run 3", section)

    def test_no_unscannable_run_adds_no_warning(self):
        runs = make_runs("s1", "auto-a", 2, 0, unscored=["timeout"])
        md = report.render_markdown(build(runs, baseline_for("auto-a", 2, 0)))
        self.assertNotIn("## Warnings", md)


class UnscoredAndUnknown(unittest.TestCase):
    def test_all_runs_unscored_lists_rules_as_insufficient(self):
        runs = make_runs("s1", "auto-a", 0, 0, unscored=["timeout"] * 5)
        rep = build(runs, baseline_for("auto-a", 5, 0))
        rules = rep["scenarios"][0]["rules"]
        self.assertEqual([r["rule"] for r in rules], ["auto-a"])
        self.assertEqual(status_of(rep), "insufficient")
        self.assertEqual(rules[0]["scored"], 0)

    def test_scenario_rules_parameter_lists_rules_without_baseline_as_new(self):
        runs = make_runs("s1", "auto-a", 0, 0, unscored=["timeout"] * 5)
        rep = report.build_report(
            runs, HASHES, None, META, registry=REGISTRY,
            scenario_rules={"s1": ["auto-a", "judge-a"]},
        )
        self.assertEqual(status_of(rep, "auto-a"), "new")
        self.assertEqual(status_of(rep, "judge-a"), "new")

    def test_baseline_rules_not_run_are_listed(self):
        runs = make_runs("s1", "auto-b", 5, 0)
        rep = build(runs, baseline_for("auto-a", 5, 0))
        self.assertEqual(status_of(rep, "auto-a"), "insufficient")

    def test_scenario_rules_entry_overrides_baseline_rule_list(self):
        runs = make_runs("s1", "auto-b", 5, 0)
        rep = report.build_report(
            runs, HASHES, baseline_for("auto-a", 5, 0), META, registry=REGISTRY,
            scenario_rules={"s1": ["auto-b"]},
        )
        self.assertEqual([r["rule"] for r in rep["scenarios"][0]["rules"]], ["auto-b"])

    def test_scenario_without_entry_still_falls_back_to_baseline(self):
        runs = make_runs("s1", "auto-b", 5, 0)
        rep = report.build_report(
            runs, HASHES, baseline_for("auto-a", 5, 0), META, registry=REGISTRY,
            scenario_rules={"s2": ["auto-b"]},
        )
        self.assertEqual(status_of(rep, "auto-a"), "insufficient")

    def test_unknown_rule_raises_naming_it(self):
        with self.assertRaises(ValueError) as cm:
            build(make_runs("s1", "nope", 1, 0), None)
        self.assertIn("nope", str(cm.exception))

    def test_markdown_marks_kind_and_exit_note(self):
        rep = build(make_runs("s1", "judge-a", 0, 5), baseline_for("judge-a", 5, 0))
        md = report.render_markdown(rep)
        table = md[md.index("## Flagged"): md.index("## Scenario s1")]
        self.assertIn("judge-a (judgement)", table)
        self.assertIn("do not affect the exit code", table)

    def test_missing_agent_version_not_printed_as_none(self):
        rep = build(make_runs("s1", "auto-a", 1, 0), None, dict(META, agent_version=None))
        self.assertNotIn("None", report.render_markdown(rep).split("\n")[2])


class BaselineJson(unittest.TestCase):
    def test_drops_baseline_and_reasons(self):
        runs = make_runs("s1", "auto-a", 4, 1, unscored=["timeout"])
        rep = build(runs, baseline_for("auto-a", 5, 0))
        out = report.baseline_from_report(rep)
        rule = out["scenarios"][0]["rules"][0]
        self.assertNotIn("baseline", rule)
        self.assertNotIn("reason", json.dumps(out))
        self.assertEqual(out["scenarios"][0]["unscored"], [{"run": 6}])
        self.assertEqual(rule["passed"], 4)
        self.assertEqual(out["model"], "model-1")
        self.assertEqual((out["effort"], out["judge_model"]), ("medium", "judge-1"))
        # source report untouched
        self.assertIn("baseline", rep["scenarios"][0]["rules"][0])
        self.assertEqual(rep["scenarios"][0]["unscored"][0]["reason"], "timeout")

    def test_round_trips_as_baseline(self):
        rep = build(make_runs("s1", "auto-a", 5, 0), None)
        again = build(make_runs("s1", "auto-a", 5, 0), report.baseline_from_report(rep))
        self.assertEqual(status_of(again), "ok")


if __name__ == "__main__":
    unittest.main()

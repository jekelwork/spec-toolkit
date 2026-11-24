"""Aggregation, baseline comparison and report.md output.

Pure functions over run results (`result.json`-shaped dicts) and a baseline dict, following
contracts/eval-report.md and research D7/D8. Nothing here reads transcripts or reasons into the
report: `report.json`, `report.md` and `baseline.json` hold counts and metadata only.
"""

import copy
import re
from pathlib import Path

import rules as rules_module

REPORT_VERSION = 1
MIN_SCORED = 3
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def skill_words(skill_dir):
    """`core`: words in `SKILL.md`; `total`: that plus `references/*.md` (whitespace tokens)."""
    skill_dir = Path(skill_dir)
    core = len((skill_dir / "SKILL.md").read_text(encoding="utf-8").split())
    total = core
    for path in sorted((skill_dir / "references").glob("*.md")):
        total += len(path.read_text(encoding="utf-8").split())
    return {"core": core, "total": total}


def is_flagged(bp, bs, p, s):
    """Research D7: the pass fraction dropped by at least 2/5, in integers."""
    return 5 * (bp * s - p * bs) >= 2 * bs * s


def _entry_scored(entry):
    return entry.get("scored", entry["passed"] + entry["failed"])


def _find(items, key, value):
    for item in items:
        if item.get(key) == value:
            return item
    return None


def _baseline_block(baseline, meta):
    if baseline is None:
        return None
    warnings, comparable = [], True
    if baseline.get("model") != meta["model"]:
        comparable = False
        warnings.append(
            f"pinned model differs from the baseline ({baseline.get('model')} vs "
            f"{meta['model']}): not comparable, every status is new"
        )
    if baseline.get("agent_version") != meta.get("agent_version"):
        warnings.append(
            f"Claude Code version differs from the baseline ({baseline.get('agent_version')} "
            f"vs {meta.get('agent_version')})"
        )
    return {
        "toolkit_commit": baseline.get("toolkit_commit"),
        "toolkit_ref": baseline.get("toolkit_ref"),
        "model": baseline.get("model"),
        "comparable": comparable,
        "warnings": warnings,
    }


def _baseline_entry(rule, base_scenario):
    """The baseline entry this rule compares with, or None (so the status is `new`)."""
    if base_scenario is None:
        return None
    own = _find(base_scenario["rules"], "rule", rule.id)
    if own is not None and own.get("version") == rule.version:
        return own
    if rule.replaces:
        return _find(base_scenario["rules"], "rule", rule.replaces)
    return None


def _status(entry, comparable, p, s):
    if not comparable or entry is None:
        return "new"
    bs = _entry_scored(entry)
    if s < MIN_SCORED or bs < MIN_SCORED:
        return "insufficient"
    return "flagged" if is_flagged(entry["passed"], bs, p, s) else "ok"


def build_report(runs, scenario_hashes, baseline, meta, registry=None, scenario_rules=None):
    """The `report.json` dict.

    `runs`: result.json dicts; `scenario_hashes`: `{scenario: "sha256:..."}`; `baseline`: parsed
    `baseline.json` or None; `meta`: created, toolkit_commit, toolkit_ref, dirty, launcher, agent,
    agent_version, model, skill_words, runs_per_scenario, wall_seconds. `scenario_rules`:
    optional `{scenario: [rule ids]}` the scenario scores, listed even when no run scored them.
    """
    registry = rules_module.RULES if registry is None else registry
    order = {rule.id: i for i, rule in enumerate(registry)}
    by_id = {rule.id: rule for rule in registry}
    ref = meta.get("toolkit_ref")
    if ref is not None and not _SHA.match(ref):
        raise ValueError(f"toolkit_ref must be a full commit SHA, not {ref!r}")

    block = _baseline_block(baseline, meta)
    comparable = block is not None and block["comparable"]

    names = []
    for run in runs:
        if run["scenario"] not in names:
            names.append(run["scenario"])
    scenarios = []
    for name in names:
        mine = [r for r in runs if r["scenario"] == name]
        scored_runs = [r for r in mine if r["scored"]]
        base_scenario = _find(baseline["scenarios"], "name", name) if baseline else None
        hash_ = scenario_hashes.get(name)
        if base_scenario is not None and base_scenario.get("hash") != hash_:
            base_scenario = None
        counts = {}
        for run in scored_runs:
            for result in run["rules"]:
                if result["rule"] not in by_id:
                    raise ValueError(f"result names a rule not in the registry: {result['rule']!r}")
                c = counts.setdefault(result["rule"], {"pass": 0, "fail": 0, "not_applicable": 0})
                c[result["outcome"]] += 1
        listed = set(counts)
        declared = (scenario_rules or {}).get(name)
        for rule_id in declared or ():
            if rule_id not in by_id:
                raise ValueError(f"scenario_rules names a rule not in the registry: {rule_id!r}")
            listed.add(rule_id)
        if declared is None and comparable and base_scenario is not None:
            # a retired baseline rule that is no longer in the registry is not reported
            listed |= {e["rule"] for e in base_scenario["rules"] if e["rule"] in by_id}
        entries = []
        for rule_id in sorted(listed, key=lambda i: order[i]):
            rule = by_id[rule_id]
            c = counts.get(rule_id, {"pass": 0, "fail": 0, "not_applicable": 0})
            p, s = c["pass"], c["pass"] + c["fail"]
            base = _baseline_entry(rule, base_scenario)
            entries.append({
                "rule": rule_id,
                "version": rule.version,
                "kind": rule.kind,
                "passed": p,
                "failed": c["fail"],
                "not_applicable": c["not_applicable"],
                "scored": s,
                "baseline": (
                    {"passed": base["passed"], "scored": _entry_scored(base)}
                    if base is not None and comparable
                    else None
                ),
                "status": _status(base, comparable, p, s),
            })
        scenarios.append({
            "name": name,
            "hash": hash_,
            "scored": len(scored_runs),
            "unscored": [
                {"run": r["run"], "reason": r["unscored_reason"]} for r in mine if not r["scored"]
            ],
            "rules": entries,
        })

    return {
        "report_version": REPORT_VERSION,
        "created": meta["created"],
        "toolkit_commit": meta["toolkit_commit"],
        "toolkit_ref": ref,
        "dirty": meta["dirty"],
        "launcher": meta["launcher"],
        "agent": meta["agent"],
        "agent_version": meta["agent_version"],
        "model": meta["model"],
        "skill_words": meta["skill_words"],
        "runs_per_scenario": meta["runs_per_scenario"],
        "wall_seconds": meta["wall_seconds"],
        "cost_usd": round(sum(r.get("cost_usd") or 0 for r in runs), 4),
        "baseline": block,
        "scenarios": scenarios,
    }


def apply_rerun(rep, rerun):
    """A copy of `rep` where each flagged rule becomes `confirmed` or `cleared` per `rerun`.

    `rerun` is a report built from the `--rerun-flagged` runs against the same baseline. A flagged
    rule the rerun does not cover, or covers with too few scored runs, stays `flagged`.
    """
    out = copy.deepcopy(rep)
    for scenario in out["scenarios"]:
        other = _find(rerun["scenarios"], "name", scenario["name"])
        if other is None:
            continue
        for entry in scenario["rules"]:
            if entry["status"] != "flagged":
                continue
            again = _find(other["rules"], "rule", entry["rule"])
            if again is None:
                continue
            if again["status"] == "flagged":
                entry["status"] = "confirmed"
            elif again["status"] == "ok":
                entry["status"] = "cleared"
    return out


def exit_code(rep):
    """1 when an automatic rule is flagged or confirmed, else 0 (judgement rules never count)."""
    for scenario in rep["scenarios"]:
        for entry in scenario["rules"]:
            if entry["kind"] == "automatic" and entry["status"] in ("flagged", "confirmed"):
                return 1
    return 0


def baseline_from_report(rep):
    """`baseline.json` content: the report without `rules[].baseline` and without any `reason`."""

    def strip(value):
        if isinstance(value, dict):
            is_rule_entry = "rule" in value
            return {
                k: strip(v)
                for k, v in value.items()
                if k != "reason" and not (k == "baseline" and is_rule_entry)
            }
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    return strip(copy.deepcopy(rep))


def _cell(entry):
    base = entry["baseline"]
    now = f"{entry['passed']}/{entry['scored']}"
    return now, (f"{base['passed']}/{base['scored']}" if base else "-")


def render_markdown(rep):
    """Human summary of the report. Counts and metadata only, never transcript text."""
    lines = ["# Bootstrap-guide evaluation", ""]
    where = rep["toolkit_ref"] or rep["toolkit_commit"]
    if not rep["toolkit_ref"] and rep["dirty"]:
        where += " (dirty)"
    words = rep["skill_words"]
    agent = " ".join(p for p in (rep["agent"], rep["agent_version"]) if p)
    lines.append(
        f"Toolkit {where}, launcher {rep['launcher']}, model {rep['model']}, "
        f"{agent}, skill words {words['core']} core / "
        f"{words['total']} total, {rep['runs_per_scenario']} runs per scenario."
    )
    lines.append("")
    warnings = (rep["baseline"] or {}).get("warnings", [])
    if rep["baseline"] is None:
        warnings = ["no baseline: every status is new"]
    if warnings:
        lines += ["## Warnings", ""] + [f"- {w}" for w in warnings] + [""]

    automatic = [(s, e) for s in rep["scenarios"] for e in s["rules"] if e["kind"] == "automatic"]
    judgement = [(s, e) for s in rep["scenarios"] for e in s["rules"] if e["kind"] != "automatic"]

    lines += [
        "## Flagged and confirmed rules",
        "",
        "Judgement rules are marked (judgement) and do not affect the exit code.",
        "",
    ]
    hot = [(s, e) for s, e in automatic if e["status"] in ("flagged", "confirmed")]
    hot += [(s, e) for s, e in judgement if e["status"] in ("flagged", "confirmed")]
    if hot:
        lines += [
            "| Scenario | Rule | Passed | Baseline | Status |",
            "| --- | --- | --- | --- | --- |",
        ]
        for s, e in hot:
            now, base = _cell(e)
            tag = "" if e["kind"] == "automatic" else " (judgement)"
            lines.append(f"| {s['name']} | {e['rule']}{tag} | {now} | {base} | {e['status']} |")
    else:
        lines.append("None.")
    lines.append("")

    for s in rep["scenarios"]:
        lines += [f"## Scenario {s['name']}", "", f"Scored runs: {s['scored']}", ""]
        rows = [e for e in s["rules"] if e["kind"] == "automatic"]
        lines += ["| Rule | Passed | Baseline | Status |", "| --- | --- | --- | --- |"]
        for e in rows:
            now, base = _cell(e)
            lines.append(f"| {e['rule']} | {now} | {base} | {e['status']} |")
        lines.append("")

    lines += ["## Judgement rules (not counted toward the exit code)", ""]
    if judgement:
        lines += [
            "| Scenario | Rule | Passed | Baseline | Status |",
            "| --- | --- | --- | --- | --- |",
        ]
        for s, e in judgement:
            now, base = _cell(e)
            lines.append(f"| {s['name']} | {e['rule']} | {now} | {base} | {e['status']} |")
    else:
        lines.append("None.")
    lines.append("")

    lines += ["## Unscored runs", ""]
    unscored = [(s["name"], u) for s in rep["scenarios"] for u in s["unscored"]]
    if unscored:
        lines += [f"- {n} run {u['run']}: {u['reason']}" for n, u in unscored]
    else:
        lines.append("None.")
    lines += ["", f"Cost: ${rep['cost_usd']:.2f}. Wall time: {rep['wall_seconds']} s.", ""]
    return "\n".join(lines)

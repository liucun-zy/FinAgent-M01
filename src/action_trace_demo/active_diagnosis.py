"""Budgeted comparison reads chosen for their effect on the repair decision.

The planner sees claim descriptions, anchored offsets, source *metadata* and
already executed reads. Candidate-outcome partitions are predictions, not
observations. Only the selected original window is opened and recorded.
"""
import math
from pathlib import PurePath
import re

from .temporal_dag import rank_probes


_WINDOW = 960
_BUDGET = 12.0
_ROLE = {
    "correction": r"correct|restat|amend|rectif|errat|修订|调整|更正|修正|訂正",
    "effective": r"effective|commenc|expiry|validity|生效|施行|届满",
    "selection": r"select|mandate|directive|instruction|approval|final instruction|选用|选定|采用|指令",
    "scenario": r"scenario|alternativ|comparison|proposal|备选|情景|方案对照",
    "retraction": r"withdraw|revok|rescind|withdrawal|撤销|撤回|作废",
    "materialized": r"workbook|spreadsheet|\.xlsx|native.slot|formula|模型表|单元格",
}
_ACTIONS = {
    "incorrect_extraction": "value", "restated_source": "value", "new_valid_interval": "scope",
    "wrong_consumer": "use", "missing_use_edge": "use", "parallel_claims": "coexist",
    "explicit_withdrawal": "retract", "unidentified_explanation": "unresolved",
}
_PREDICTIONS = {
    "correction": {"value": "directed_value_change", "retract": "withdrawal",
                   "scope": "only_new_interval"},
    "effective": {"scope": "effective_interval", "retract": "termination",
                  "coexist": "disjoint_intervals"},
    "selection": {"use": "required_current_use", "coexist": "alternatives_permitted"},
    "scenario": {"coexist": "parallel_values", "use": "selected_scenario"},
    "retraction": {"retract": "withdrawal", "scope": "expiry"},
    "materialized": {"use": "different_applied_input", "value": "incorrect_materialized_value"},
}


def _roles(text):
    return {r for r, pattern in _ROLE.items() if re.search(pattern, text, re.I)}


def diagnosis_hypotheses():
    """The generic operator's fixed explanations and their corresponding repairs."""
    return [{"id": label, "repair": repair} for label, repair in _ACTIONS.items()]


def diagnosis_partition(roles):
    """Predict observations before reading, using the deployed source-role mapping."""
    roles = sorted(roles)
    return {
        h: "|".join(_PREDICTIONS[role].get(action, "no_specific_witness")
                    for role in roles) or "unspecified"
        for h, action in _ACTIONS.items()
    }


def diagnosis_metadata(text):
    roles = sorted(_roles(text))
    return {"roles": roles, "outcomes": diagnosis_partition(roles)}


def _keywords(text):
    return {s.casefold() for s in re.findall(r"[a-zA-Z0-9]{3,}|[\u3400-\u9fff]{2,}", str(text))}


def _candidates(dag, comparison):
    c = comparison["payload"]
    old, new = c["old"], c["new"]
    catalogue = {s["id"]: s for s in dag.source_index()}
    plans = {}
    relevant_words = _keywords(" ".join(str(side[k]) for side in (old, new)
                                        for k in ("entity", "field")))

    def add(source, lo, hi, roles, side):
        lo, hi = max(0, lo), min(source["chars"], hi)
        if lo >= hi:
            return
        key = f'{source["id"]}@{lo}:{hi}'
        if key in plans:
            plans[key]["roles"] |= roles
            return
        plans[key] = {"id": key, "source_id": source["id"], "locator": source["locator"],
                      "start": lo, "end": hi, "cost": 1 + (hi-lo)/1000,
                      "roles": set(roles), "side": side}

    for label, side in (("old", old), ("new", new)):
        s = catalogue[side["source_id"]]
        # Claim text and descriptions are already in the graph; they may
        # suggest a location, but are never counted as a verified witness.
        name = PurePath(s["locator"]).name
        roles = _roles(" ".join([name, side["description"], side["claim"]]))
        if side["valid_from"] or side["valid_to"]:
            roles.add("effective")
        if any(k in side["scope"] for k in ("scenario", "case", "alternative")):
            roles.add("scenario")
        # Always preserve the complete original sentence (even a long one);
        # its cost can legitimately exceed the remaining read allowance.
        left = max(0, side["start"] - _WINDOW//4)
        right = max(side["end"]+_WINDOW//4, left+min(s["chars"], _WINDOW//2))
        add(s, left, right, roles, label)

    sides = {old["source_id"], new["source_id"]}
    first = min(catalogue[x]["stage"] for x in sides)
    extra = []
    for s in catalogue.values():
        if s["id"] in sides or s["stage"] < first:
            continue
        name = PurePath(s["locator"]).name
        roles = _roles(name)
        if not roles:
            continue
        hits = len(relevant_words & _keywords(name))
        if not hits and s["stage"] != catalogue[new["source_id"]]["stage"]:
            continue
        extra.append(((hits, len(roles), s["stage"]), s, roles))
    for _, s, roles in sorted(extra, key=lambda it: it[0], reverse=True)[:8]:
        # Offset zero is an honest, bounded first read for a new source.
        # No body search across candidate documents occurs while ranking.
        add(s, 0, _WINDOW, roles, "related source")
    for plan in plans.values():
        roles = sorted(plan["roles"])
        plan["roles"] = roles
        # A source with no diagnostic cues offers an uninformative partition.
        # It remains available as a fallback at its real cost. These labels
        # never eliminate an explanation before the selected read executes.
        plan["outcomes"] = diagnosis_partition(roles)
    return plans


def _decided(dag, comp, probes):
    c = comp["payload"]
    target = next((i for i in comp["parents"]
                   if i not in (c["old"]["claim_id"], c["new"]["claim_id"])
                   and dag.get(i)["kind"] in {"artifact_observation", "artifact_version", "use_binding"}), None)
    verified = dag.supported_relations(comp["id"], probes, purpose=c["purpose"], target_id=target)
    # If distinct safe edits have independent witnesses, retain the ambiguity.
    # A purpose-dependent repair is authorized only for that target.
    return verified, target


def inspect(dag, comparison_id, budget=None):
    """Perform at most one new read and return the next grounded repair choices."""
    comparison = dag.get(comparison_id)
    if comparison["kind"] != "comparison":
        raise ValueError("Expected a conflict comparison")
    if budget is None:
        budget = _BUDGET
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget < 0:
        raise ValueError("budget must be a finite nonnegative total read allowance")
    budget = min(_BUDGET, budget)
    c = comparison["payload"]
    history = [p for p in dag.nodes("probe") if p["payload"]["comparison_id"] == comparison_id]
    def key(p):
        return f'{p["source_id"]}@{p["start"]}:{p["end"]}'

    already = {key(n["payload"]) for n in history}
    spent = sum(1 + (n["payload"]["end"]-n["payload"]["start"])/1000 for n in history)
    probes = [n["id"] for n in history]
    verified, target = _decided(dag, comparison, probes)
    hypotheses = diagnosis_hypotheses()
    candidates = _candidates(dag, comparison) if len(verified) != 1 else {}
    ranked = rank_probes(hypotheses, [q for k, q in candidates.items() if k not in already],
                         budget=max(0.0, budget - spent))
    fresh = None
    if ranked:
        chosen = ranked[0]
        actual = dag.read_excerpt(chosen["source_id"], chosen["start"], chosen["end"]-chosen["start"])
        fresh = dag.probe(comparison_id, chosen["source_id"], actual,
                          "Read selected evidence window at " + chosen["locator"], start=chosen["start"])
        probes.append(fresh["id"])
        spent += chosen["cost"]
        verified, target = _decided(dag, comparison, probes)
        already.add(chosen["id"])
    remaining = [q for k, q in candidates.items() if k not in already and q["cost"] <= max(0.0,budget-spent)]
    feasible = sorted(verified)
    if len(feasible) == 1:
        state = "evidence_supported_repair"
    elif remaining:
        state = "needs_more_evidence"
    else:
        state = "unresolved"
    # Restriction to a repair class is a decision about this purpose. It
    # preserves distinctions between possible historical causes in that class.
    compatible = [h for h in hypotheses if not feasible or h["repair"] in feasible]
    step = dag.append("diagnosis_step", {
        "comparison_id": comparison_id, "hypotheses_before": hypotheses,
        "compatible_explanations": compatible, "probe_ranking": ranked, "evidence_ids": probes,
        "supported_repairs": verified, "target_id": target, "purpose": c["purpose"], "state": state,
        "budget": budget, "spent": spent, "remaining": max(0.0, budget - spent),
    }, [comparison_id, *probes])
    result = {**step["payload"], "decision_step": step["id"],
              "comparison": {"id":comparison_id, **c}, "probe_id": fresh["id"] if fresh else None,
              "original_context": fresh["payload"]["quote"] if fresh else None}
    if fresh:
        result["source_id"] = fresh["payload"]["source_id"]
    return result

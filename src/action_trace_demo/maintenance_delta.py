"""Plan grouped local ledger states without allocating IDs or writing events."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict
import json
from decimal import DecimalException

from .extraction import normalized_value
from .maintenance_contract import (ChangeSet, LedgerRepresentation, MaintenanceProposal,
                                   TargetState, representation_fields)


BUSINESS_FIELDS = ("entity_id", "predicate", "display_statement", "display_value",
                   "qualifiers", "effective_time")


class MechanicalIssue(ValueError):
    def __init__(self, path, message):
        self.issue = {"path": path, "code": "invalid_reference", "message": message}
        super().__init__(message)


def require(condition, path, message):
    if not condition:
        raise MechanicalIssue(path, message)


def render_ledger_statement(state, entities):
    """Render the actual structured state; never edit a source sentence in place."""
    entity_id = state["entity_id"]
    name = entities.get(entity_id, {}).get("canonical_name", entity_id)
    compact = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    parts = [str(name), state["predicate"], compact(state["display_value"])]
    if state["qualifiers"]:
        parts.append("限定=" + compact(state["qualifiers"]))
    if state["effective_time"] is not None:
        parts.append("适用时间=" + compact(state["effective_time"]))
    return " / ".join(parts)


def _compile_target(proposal, path, *, items, claims, entities, base_ledger_snapshot_id):
    target = proposal.target_item_id
    require(target is None or target in items, path + ["target_item_id"],
            "Target must belong to the fixed base ledger")
    common = {key: deepcopy(getattr(proposal, key)) for key in (
        "target_item_id", "adopted_claim_ids", "compared_claim_ids", "evidence_ref_ids",
        "context_ref_ids", "reason")}
    trace = {"base_ledger_snapshot_id": base_ledger_snapshot_id, "base_target_item_id": target}
    if proposal.after is None:
        return TargetState(after=None, field_sources={}, compilation={**trace, "exit": True}, **common)
    before = items.get(target, {})
    business = representation_fields()
    # Keep key presence until this point: omission retains the pinned target's
    # field, while an explicit null/empty collection is an actual assignment.
    if target is None:
        require(all(key in proposal.after for key in ("entity_id", "predicate", "display_value")),
                path + ["after"], "A new item requires entity_id, predicate and display_value")
        base = {"qualifiers": {}, "effective_time": None}
    else:
        base = {key: deepcopy(before[key]) for key in business}
    state = {**base, **deepcopy(proposal.after)}
    sources = deepcopy(before.get("field_sources", {}))
    changed_fields = [key for key in business if not before or before.get(key) != state[key]]
    for key in business:
        if key in changed_fields or state[key] is None or state[key] == {}:
            sources = {name: ids for name, ids in sources.items()
                       if name != key and not name.startswith(key + ".")}
        # Matching fields can retain inherited provenance without pretending
        # that a newly adopted source supports them. Explicit basis may replace it.
        if state[key] is not None and state[key] != {}:
            sources.update({name: list(ids) for name, ids in proposal.field_sources.items()
                            if name == key or name.startswith(key + ".")})
    changed = bool(changed_fields)
    state["display_statement"] = (render_ledger_statement(state, entities) if changed
                                  else before["display_statement"])
    trace.update(contract_version="direct-state-v2", proposed_fields=deepcopy(proposal.after),
                 inherited_fields=[key for key in business if target is not None and key not in proposal.after],
                 defaulted_fields=[key for key in base if target is None and key not in proposal.after],
                 unchanged_fields=[key for key in business if key not in changed_fields],
                 changed_fields=changed_fields)
    return TargetState(after=LedgerRepresentation(**state), field_sources=sources,
                       compilation=trace, **common)


def compile_proposal(proposal, *, items, claims, entities, base_ledger_snapshot_id):
    """Compile the sole model contract into existing full states, isolating bad groups."""
    groups = []
    for index, group in enumerate(proposal.change_sets):
        states, errors = [], []
        for state_index, state in enumerate(group.states):
            try:
                states.append(_compile_target(state, ["change_sets", index, "states", state_index],
                    items=items, claims=claims, entities=entities,
                    base_ledger_snapshot_id=base_ledger_snapshot_id))
            except MechanicalIssue as exc:
                errors.append(exc.issue)
        groups.append(ChangeSet(group.change_set_id, list(group.input_claim_ids), states,
                                group.reason, compilation_errors=errors))
    return MaintenanceProposal(groups, proposal.dispositions, proposal.previous_receipt_id)


def _state(state, path, *, items, allowed_claims, direct_refs, context_refs,
           entities, cutoff, semantics, protected_targets):
    """Validate references and materialize one explicit after-state."""
    target = state.target_item_id
    require(target is None or target in items, path + ["target_item_id"],
            "Target must belong to the base ledger")
    require(target not in protected_targets, path + ["target_item_id"],
            "A correction request cannot rewrite a previously successful target")
    require(target is not None or state.after is not None, path + ["after"],
            "A new item requires an explicit after-state")
    adopted = list(dict.fromkeys(state.adopted_claim_ids))
    require(set(adopted) <= allowed_claims.keys(), path + ["adopted_claim_ids"],
            "Adopted claims must be visible and present in the comparison material")
    require(set(state.compared_claim_ids) <= allowed_claims.keys(), path + ["compared_claim_ids"],
            "Compared claims must be visible and present in the comparison material")
    require(set(state.evidence_ref_ids) <= direct_refs, path + ["evidence_ref_ids"],
            "Direct evidence IDs must belong to the provided claims")
    require(set(state.context_ref_ids) <= context_refs, path + ["context_ref_ids"],
            "Context IDs must have actually been provided")
    require(all(set(ids) <= allowed_claims.keys() for ids in state.field_sources.values()),
            path + ["field_sources"], "Field assignment sources must be visible provided claims")
    before = deepcopy(items[target]) if target is not None else None
    if state.after is None:
        return {"target_item_id": target, "before": before, "after": None, "state": asdict(state)}
    require(bool(adopted), path + ["adopted_claim_ids"],
            "An active after-state must explicitly identify its complete adopted-source set")
    proposed = asdict(state.after)
    require(proposed["entity_id"] in entities, path + ["after", "entity_id"],
            "Subject must be visible at the execution cutoff")
    try:
        value = normalized_value(proposed["display_value"])
    except (ValueError, DecimalException) as exc:
        raise MechanicalIssue(path + ["after", "display_value"],
                              "Cannot normalize the explicit value: " + str(exc)) from exc
    if isinstance(value, dict) and value.get("kind") == "entity":
        require(value.get("entity_id") in entities, path + ["after", "display_value", "entity_id"],
                "Entity-valued result must reference a visible entity")
    # These are sets of source IDs. Preserve existing order and deterministically
    # append new members, so a permutation alone does not create a version.
    existing = (before or {}).get("assertion_ids", [])
    adopted = [cid for cid in existing if cid in adopted] + sorted(set(adopted) - set(existing))
    if state.compilation and (before is None or any(
            before.get(key) != proposed[key] for key in representation_fields())):
        proposed["display_statement"] = render_ledger_statement({**proposed, "display_value": value}, entities)
    fields = {**proposed, "display_value": value, "assertion_ids": adopted,
              "field_sources": {key: sorted(set(ids)) for key, ids in state.field_sources.items()},
              "normalization_as_of": cutoff}
    try:
        fields.update(semantics(proposed["entity_id"], {
            "predicate": proposed["predicate"], "value": value,
            "qualifiers": proposed["qualifiers"], "effective_time": proposed["effective_time"]}))
    except (ValueError, DecimalException) as exc:
        raise MechanicalIssue(path + ["after"],
                              "Cannot construct the normalized representation: " + str(exc)) from exc
    unchanged = before is not None and all(
        before.get(key) == fields[key] for key in (*BUSINESS_FIELDS, "assertion_ids"))
    unchanged = unchanged and before.get("field_sources", {}) == fields["field_sources"]
    after = deepcopy(before) if unchanged else {**(before or {}), **fields}
    if "subject_entity_id" in after:
        after["subject_entity_id"] = fields["entity_id"]
    if "source_version_ids" in after:
        after["source_version_ids"] = list(dict.fromkeys(
            allowed_claims[cid]["source_version_id"] for cid in adopted))
    return {"target_item_id": target, "before": before, "after": after, "state": asdict(state)}


def plan_changes(proposal, *, items, input_claim_ids, allowed_claims, direct_refs,
                 context_refs, entities, cutoff, semantics, protected_targets=(), repair_scope=None):
    """Reject only mechanically invalid groups; conflicting writers all fail."""
    groups = proposal.change_sets
    issues = defaultdict(list)
    ids, writers = defaultdict(list), defaultdict(list)
    for index, group in enumerate(groups):
        ids[group.change_set_id].append(index)
        for state in group.states:
            if state.target_item_id is not None:
                writers[state.target_item_id].append(index)
    for group_id, indices in ids.items():
        if len(indices) > 1:
            for index in indices:
                issues[index].append({"path": ["change_sets", index, "change_set_id"],
                    "code": "duplicate_group_id", "message": "Change-set IDs must be unique",
                    "change_set_id": group_id})
    for target, indices in writers.items():
        if len(indices) > 1:
            for index in set(indices):
                issues[index].append({"path": ["change_sets", index, "states"],
                    "code": "target_conflict", "message": "A target has more than one proposed final state",
                    "target_item_id": target,
                    "change_set_ids": list(dict.fromkeys(groups[i].change_set_id for i in indices))})
    plans = []
    inputs = set(input_claim_ids)
    for index, group in enumerate(groups):
        path = ["change_sets", index]
        states = []
        issues[index].extend(group.compilation_errors)
        try:
            if repair_scope:
                require(group.change_set_id not in {g["change_set_id"] for g in repair_scope.get("successful_change_sets", [])},
                        path + ["change_set_id"], "A correction cannot replay a previously successful group")
                require(not group.states or set(group.input_claim_ids) <= set(repair_scope.get("state_input_claim_ids", [])),
                        path + ["input_claim_ids"], "Only inputs with unfinished state changes can change state in this correction")
            require(bool(group.input_claim_ids) and set(group.input_claim_ids) <= inputs,
                    path + ["input_claim_ids"], "Change-set inputs must belong to this request")
            for state_index, state in enumerate(group.states):
                states.append(_state(state, path + ["states", state_index], items=items,
                    allowed_claims=allowed_claims, direct_refs=direct_refs, context_refs=context_refs,
                    entities=entities, cutoff=cutoff, semantics=semantics,
                    protected_targets=set(protected_targets)))
        except MechanicalIssue as exc:
            issues[index].append(exc.issue)
        plans.append({"change_set_id": group.change_set_id,
                      "input_claim_ids": list(dict.fromkeys(group.input_claim_ids)),
                      "reason": group.reason, "states": states if not issues[index] else [],
                      "errors": issues[index]})
    return plans


def delta_label(before, after):
    """Labels describe the computed difference; they are not execution commands."""
    if before is None:
        return "add"
    if after is None:
        return "withdraw"
    if before == after:
        return "keep"
    same_business = all(before.get(key) == after.get(key) for key in BUSINESS_FIELDS)
    old_sources, new_sources = set(before["assertion_ids"]), set(after["assertion_ids"])
    if same_business and old_sources < new_sources:
        return "attach"
    return "revise"


def resolve_dispositions(proposal, group_results, input_claim_ids, *, prior_group_results=()):
    """Preserve questions independently from a disposition's execution outcome."""
    inputs = set(input_claim_ids)
    by_group = {group["change_set_id"]: group for group in prior_group_results}
    by_group.update((group["change_set_id"], group) for group in group_results)
    counts = defaultdict(int)
    for disposition in proposal.dispositions:
        counts[disposition.input_claim_id] += 1
    results = []
    for index, disposition in enumerate(proposal.dispositions):
        raw = asdict(disposition)
        path = ["dispositions", index]
        errors = []
        try:
            require(disposition.input_claim_id in inputs, path + ["input_claim_id"],
                    "Disposition input must belong to this request")
            require(counts[disposition.input_claim_id] == 1, path + ["input_claim_id"],
                    "Provide one disposition per input, including all its questions and change-set links")
            require(set(disposition.change_set_ids) <= by_group.keys(), path + ["change_set_ids"],
                    "Disposition must refer to this proposal or a successful group in its correction chain")
            require(all(disposition.input_claim_id in by_group[gid]["input_claim_ids"]
                        for gid in disposition.change_set_ids), path + ["change_set_ids"],
                    "A disposition can depend only on groups containing its input")
        except MechanicalIssue as exc:
            errors.append(exc.issue)
        failed = [gid for gid in disposition.change_set_ids if gid in by_group
                  and by_group[gid]["status"] != "executed"]
        results.append({**raw, "proposed_handled": disposition.handled,
                        "handled": disposition.handled and not failed and not errors,
                        "failed_change_set_ids": failed, "errors": errors,
                        "prior_execution_refs": [{"change_set_id": gid, "receipt_id": by_group[gid]["receipt_id"]}
                            for gid in disposition.change_set_ids if gid in by_group and by_group[gid].get("receipt_id")]})
    return results

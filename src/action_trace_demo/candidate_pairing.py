"""Bounded M02 candidate retrieval over a caller-selected, fixed ledger version.

Candidate events record retrieval inputs/results, never maintenance decisions.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time

from .ablation_selection import _tokens
from .evidence_windows import token_estimate
from .extraction import normalized_text
from .ingestion_store import IngestionStore
from .claim_view import (subject_id, value_of, entity_names, statement_of, evidence_text,
                         related_roles, subject_identities)


ALGORITHM_VERSION = "role-lexical-bm25-v2"
DEFAULT_CONTEXT_BUDGET = 8000


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _terms(text, excluded=()):
    return [t for t in _tokens(text) if not t.isdigit() and t not in excluded]


def _entity_basis(new, old, identities):
    new_id, old_id = subject_id(new), subject_id(old)
    if new_id == old_id:
        return [{"kind": "subject_id", "entity_id": old_id}], False
    a, b = identities[new_id], identities[old_id]
    common = a["identifiers"].keys() & b["identifiers"].keys()
    conflicting = [key for key in common
                   if normalized_text(a["identifiers"][key]) != normalized_text(b["identifiers"][key])]
    if conflicting:
        return [], True
    same = sorted(common)
    if same:
        return [{"kind": "subject_identifier", "fields": same}], False
    old_names = {normalized_text(n) for n in b["names"]}
    common_names = old_names & {normalized_text(n) for n in a["names"]}
    if common_names:
        return [{"kind": "subject_name_or_alias", "terms": sorted(common_names)}], False
    text = normalized_text(statement_of(new))
    own_names = old_names | {normalized_text(v) for v in b["identifiers"].values()}
    mentions = [n for n in own_names if len(n) > 1 and
                re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", text)]
    if mentions:
        return [{"kind": "subject_mention_in_statement", "terms": sorted(mentions)}], False
    return [], False


def retrieve_candidates(new_claims, current_ledger, exported, *, visible_as_of,
                        context_budget=DEFAULT_CONTEXT_BUDGET, limit=3):
    """Role-aware deterministic retrieval interface over one fixed visible state.

    Replace this function to compare another retrieval implementation. No hidden
    semantic/model calls occur. Evidence text cannot supply a target relation;
    explicit object/qualifier references can separately supply context items.
    """
    ledger_items = current_ledger["items"]
    entities = {e["entity_id"]: e for e in exported["entities"]}
    claims = {c["assertion_id"]: c for c in exported["source_claims"]}
    identities = subject_identities(entities, [*claims.values(), *ledger_items])
    entity_terms = {t for e in entities.values() for n in entity_names(e) for t in _tokens(n)}
    rows, postings = [], defaultdict(list)
    for item in ledger_items:
        statements = {statement_of(item), *[statement_of(claims[cid]) for cid in item["assertion_ids"]]}
        relation = set(_terms(item["predicate"], entity_terms))
        doc = Counter(_terms(" ".join([item["predicate"], *sorted(statements)]), entity_terms))
        rows.append((item, relation, doc, sum(doc.values())))
        for term in doc:
            postings[term].append(len(rows) - 1)
    mean_len = sum(length for _, _, _, length in rows) / len(rows) if rows else 1
    mean_len = mean_len or 1
    results = []
    for new in new_claims:
        lexical = defaultdict(dict)
        new_statement = set(_terms(statement_of(new), entity_terms))
        new_relation = set(_terms(new["predicate"], entity_terms))
        background_terms = set(_terms(evidence_text(new), entity_terms))
        for term in new_statement | new_relation:
            hits = postings.get(term, [])
            idf = math.log1p((len(rows) - len(hits) + .5) / (len(hits) + .5))
            for index in hits:
                _, _, doc, length = rows[index]
                tf = doc[term]
                lexical[index][term] = idf * tf * 2.2 / (tf + 1.2 * (.25 + .75 * length / mean_len))
        candidates, contexts = [], []
        new_value = value_of(new) or {}
        new_roles = related_roles(new)
        referred_entities = set(new_roles["object_entity_ids"]) | {
            link["entity_id"] for link in new_roles["qualifier_entities"]}
        referred_entities.discard(subject_id(new))
        for index, (old, old_relation, doc, _) in enumerate(rows):
            basis, conflicting_subject = _entity_basis(new, old, identities)
            entity_hit = bool(basis)
            exact_relation = normalized_text(new["predicate"]) == normalized_text(old["predicate"])
            relation_terms = new_relation & old_relation
            statement_relation_terms = (old_relation & new_statement) | (new_relation & doc.keys())
            matched = lexical.get(index, {})
            informative = [t for t in matched if len(postings[t]) <= max(1, len(rows) * .35)]
            old_value = value_of(old) or {}
            per_unit = bool(new_value.get("unit") and new_value.get("unit") == old_value.get("unit")
                            and "/" in new_value["unit"])
            # A shared fragment of a compound relation is weak evidence (e.g.
            # "delivery owner" versus "delivery date"). Use relation coverage,
            # not arbitrary surrounding words, to establish the topic signal.
            relation_coverage = max(
                len(relation_terms) / max(1, min(len(new_relation), len(old_relation))),
                len(old_relation & new_statement) / max(1, len(old_relation)),
                len(new_relation & doc.keys()) / max(1, len(new_relation)))
            object_compatible = (new_value.get("kind") == old_value.get("kind")
                                 and new_value.get("kind") is not None)
            if new_value.get("kind") == "number":
                object_compatible = object_compatible and bool(new_value.get("unit")) and (
                    new_value.get("unit") == old_value.get("unit"))
            elif new_value.get("kind") == "entity" and old_value.get("kind") == "entity":
                a_type = entities.get(new_value.get("entity_id"), {}).get("entity_type")
                b_type = entities.get(old_value.get("entity_id"), {}).get("entity_type")
                object_compatible = object_compatible and (not a_type or not b_type or a_type == b_type)
            typed_relation_hit = object_compatible and bool(relation_terms or statement_relation_terms)
            relation_hit = exact_relation or relation_coverage >= .6 or per_unit or typed_relation_hit
            lexical_hit = bool(informative) and (entity_hit or len(matched) >= 2)
            target = not conflicting_subject and relation_hit and (entity_hit or lexical_hit)
            if target:
                if exact_relation:
                    basis.append({"kind": "relation", "relation": old["predicate"]})
                elif relation_terms or statement_relation_terms:
                    basis.append({"kind": "relation_terms", "terms": sorted(relation_terms | statement_relation_terms)})
                if per_unit:
                    basis.append({"kind": "object_unit_dimension", "unit": new_value["unit"]})
                elif typed_relation_hit and not exact_relation and relation_coverage < .6:
                    basis.append({"kind": "relation_with_compatible_object_type", "object_kind": new_value["kind"]})
                if matched:
                    basis.append({"kind": "statement_bm25", "score": round(sum(matched.values()), 6),
                                  "matched_terms": sorted(matched)})
                a = new.get("canonical_qualifiers", new.get("qualifiers", {}))
                b = old.get("canonical_qualifiers", old.get("qualifiers", {}))
                equal = [k for k in a.keys() & b.keys() if a[k] == b[k]]
                if equal:
                    basis.append({"kind": "qualifier_agreement", "fields": sorted(equal)})
                score = (6 * entity_hit + 5 * exact_relation + 2 * bool(relation_terms)
                         + 2 * per_unit + .5 * len(equal) + sum(matched.values()))
                candidates.append((score, old["ledger_item_id"], basis))
            elif subject_id(old) in referred_entities:
                # This is an explicit role reference, not a relabelled failed
                # target match or every other attribute of the same subject.
                context_terms = sorted(background_terms & doc.keys())
                contexts.append((sum(matched.values()) + .1 * len(context_terms), old["ledger_item_id"], [
                    {"kind": "referenced_object_or_qualifier_entity", "entity_id": subject_id(old)},
                    {"kind": "evidence_context_terms", "terms": context_terms}]))
        candidates.sort(key=lambda c: (-c[0], c[1]))
        contexts.sort(key=lambda c: (-c[0], c[1]))
        def selected(values, count, purpose):
            return [{"ledger_item_id": iid, "rank": rank, "purpose": purpose, "recall_basis": basis}
                    for rank, (_, iid, basis) in enumerate(values[:count], 1)]
        results.append({"new_claim_id": new["assertion_id"],
            "retrieval_status": "candidates_found" if candidates else "no_candidates",
            "candidates": selected(candidates, limit, "maintenance_target"),
            "context_items": selected(contexts, 2, "context_only"),
            "eligible_candidate_count": len(candidates), "has_more": len(candidates) > limit,
            "eligible_context_count": len(contexts), "has_more_context": len(contexts) > 2})
    return {"results": results, "visible_as_of": visible_as_of, "context_budget": context_budget,
            "retrieval_implementation": ALGORITHM_VERSION,
            "semantic_channel": {"enabled": False, "new_model_requests": 0}}


def _result(event, reused):
    return {**deepcopy(event["payload"]), "batch_id": event["id"], "reused": reused}


def generate_candidates(workspace, *, ledger_snapshot=None, arrival_id=None, limit=3, batch_id=None,
                        claim_ids=None, visible_as_of=None, context_budget=None):
    """Generate only unprocessed candidates, or expand an explicitly pinned batch.

    The default is the actual current ledger. Explicit claim_ids permit deferred
    or stale inputs to be paired again against that version without extraction.
    """
    from .candidate_materials import build_comparison_materials

    started = time.perf_counter()
    if limit < 1:
        raise ValueError("Candidate limit must be positive")
    if context_budget is not None and context_budget < 0:
        raise ValueError("Context budget must be nonnegative")
    store = IngestionStore(workspace)
    explicit_visibility = visible_as_of is not None
    with store.dag._db() as db:
        latest = db.execute("SELECT COALESCE(MAX(seq),0) FROM event").fetchone()[0]
        # Repeating an explicit re-pair without any new evidence must not create
        # a fresh context merely because candidate/session events were appended.
        if visible_as_of is None:
            visible_as_of = (db.execute("SELECT COALESCE(MAX(seq),0) FROM event WHERE kind LIKE 'm01_%'").fetchone()[0]
                             if claim_ids is not None else latest)
    if visible_as_of > latest:
        raise ValueError("Candidate visibility cannot point to a future event")
    batches = store.dag.nodes("m02_candidate_batch", as_of=latest)
    expanded_from = None
    if batch_id:
        if ledger_snapshot is not None or arrival_id is not None or claim_ids is not None:
            raise ValueError("A pinned batch already defines its ledger and Arrival scope")
        base = next(b for b in batches if b["id"] == batch_id)
        payload = base["payload"]
        if limit == payload["parameters"]["limit"] and (context_budget is None or
                context_budget == payload["parameters"].get("context_budget", DEFAULT_CONTEXT_BUDGET)):
            return _result(base, True)
        if context_budget is None:
            context_budget = payload["parameters"].get("context_budget", DEFAULT_CONTEXT_BUDGET)
        ledger_snapshot = payload["ledger_snapshot"]
        visible_as_of = payload["input_scope"]["visible_as_of"]
        arrival_id = payload["input_scope"]["arrival_id"]
        input_ids = payload["input_scope"]["input_claim_ids"]
        expanded_from = batch_id
    else:
        initial = store.initial_ledger_state()
        ledger_snapshot = deepcopy(ledger_snapshot if ledger_snapshot is not None else store.current_ledger_state())
        if ledger_snapshot is None:
            raise ValueError("An existing initial or supplied current ledger snapshot is required")
        input_ids = list(dict.fromkeys(claim_ids)) if claim_ids is not None else None
        if claim_ids is not None and not explicit_visibility:
            # A ledger may have been built at a global visibility point between
            # M01 events. Re-pairing must at least include that version's scope.
            visible_as_of = max(visible_as_of, ledger_snapshot["source_cutoff_event_seq"])
    if context_budget is None:
        context_budget = DEFAULT_CONTEXT_BUDGET
    # The snapshot is a frozen retrieval input, not another maintained ledger.
    snapshot_hash = _fingerprint(ledger_snapshot)
    item_ids = [i["ledger_item_id"] for i in ledger_snapshot["items"]]
    if len(item_ids) != len(set(item_ids)):
        raise ValueError("Current ledger items must have unique IDs")
    if ledger_snapshot["source_cutoff_event_seq"] > visible_as_of:
        raise ValueError("Ledger snapshot cannot include future source events")
    for batch in batches:
        prior = batch["payload"]
        if (prior["ledger_snapshot"]["ledger_snapshot_id"] == ledger_snapshot["ledger_snapshot_id"]
                and prior["ledger_snapshot_hash"] != snapshot_hash):
            raise ValueError("Ledger snapshot ID already identifies different contents; use a new version ID")
    exported = store.export(as_of=visible_as_of)
    by_claim = {c["assertion_id"]: c for c in exported["source_claims"]}
    if arrival_id is not None and arrival_id not in {a["arrival_id"] for a in exported["arrivals"]}:
        raise ValueError("Arrival is not visible at this batch cutoff")
    if input_ids is not None and any(cid not in by_claim or
            (arrival_id is not None and by_claim[cid]["arrival_id"] != arrival_id) for cid in input_ids):
        raise ValueError("Explicit source claim is outside the visible input scope")
    if input_ids is None:
        processed = {cid for b in batches for cid in b["payload"]["input_scope"]["input_claim_ids"]}
        included = set((initial or {}).get("included_claim_ids", []))
        included.update(ledger_snapshot.get("included_claim_ids", []))
        included.update(cid for item in ledger_snapshot["items"] for cid in item["assertion_ids"])
        input_ids = [cid for cid, c in by_claim.items() if cid not in processed | included
                     and (arrival_id is None or c["arrival_id"] == arrival_id)]
        if not input_ids:
            previous = [b for b in batches if b["payload"]["ledger_snapshot_hash"] == snapshot_hash
                        and b["payload"]["input_scope"]["arrival_id"] == arrival_id]
            if previous:
                prior = previous[-1]
                if (prior["payload"]["parameters"]["limit"] == limit and
                        prior["payload"]["parameters"].get("context_budget", DEFAULT_CONTEXT_BUDGET) == context_budget):
                    return _result(prior, True)
                return generate_candidates(workspace, limit=limit, batch_id=prior["id"], context_budget=context_budget)
    scope = {"visible_as_of": visible_as_of, "arrival_id": arrival_id, "input_claim_ids": input_ids,
             "claim_creation_seq_min": min((by_claim[c]["seq"] for c in input_ids), default=None),
             "claim_creation_seq_max": max((by_claim[c]["seq"] for c in input_ids), default=None)}
    for batch in batches:
        prior = batch["payload"]
        if (prior["ledger_snapshot_hash"] == snapshot_hash and prior["input_scope"] == scope
                and prior["parameters"]["limit"] == limit
                and prior["parameters"]["algorithm_version"] == ALGORITHM_VERSION
                and prior["parameters"].get("context_budget", DEFAULT_CONTEXT_BUDGET) == context_budget):
            return _result(batch, True)
    inputs = [by_claim[cid] for cid in input_ids]
    retrieval = retrieve_candidates(inputs, ledger_snapshot, exported, visible_as_of=visible_as_of,
                                    context_budget=context_budget, limit=limit)
    results = retrieval["results"]
    selected_ids = {c["ledger_item_id"] for r in results for c in [*r["candidates"], *r["context_items"]]}
    selected = {i["ledger_item_id"]: i for i in ledger_snapshot["items"] if i["ledger_item_id"] in selected_ids}
    materials = build_comparison_materials(exported, input_ids, selected)
    # The agent reads this deduplicated comparison context. The full retrieval
    # input remains in the event for reproducible expansion, not repeated per pair.
    agent_context = {"input_scope": scope, "ledger_snapshot_id": ledger_snapshot["ledger_snapshot_id"],
                     "results": results, "materials": materials}
    metrics = {"elapsed_seconds": round(time.perf_counter() - started, 6),
               "input_claim_count": len(inputs), "ledger_item_count": len(item_ids),
               "candidate_pair_count": sum(len(r["candidates"]) for r in results),
               "context_item_pair_count": sum(len(r["context_items"]) for r in results),
               "maintenance_context_budget": context_budget,
               "unique_old_item_count": len(selected_ids),
               "unique_source_claim_count": len(materials["claims"]),
               "unique_evidence_ref_count": len(materials["evidence_refs"]),
               "context_characters": len(_json(agent_context)),
               "context_token_estimate": token_estimate(agent_context),
               "new_model_requests": 0, "new_mineru_tasks": 0, "new_judge_requests": 0}
    payload = {"recorded_at": datetime.now(timezone.utc).isoformat(),
               "input_scope": scope, "ledger_snapshot": ledger_snapshot,
               "ledger_snapshot_hash": snapshot_hash,
               "parameters": {"limit": limit, "algorithm_version": ALGORITHM_VERSION, "context_budget": context_budget},
               "retrieval_channels": {"structural": True, "lexical": "BM25", "semantic": retrieval["semantic_channel"]},
               "expanded_from": expanded_from, "results": results, "materials": materials, "metrics": metrics,
               "semantics": "Retrieval candidates only; no conflict or maintenance decision.",
               "expansion": {"command": "generate-candidates", "use_batch_id": True,
                             "suggested_limit": limit * 2}}
    if not input_ids:
        return {**payload, "batch_id": None, "reused": False}
    event = store.dag.append("m02_candidate_batch", payload, input_ids)
    return _result(event, False)

"""Read-only semantic roles over saved claims and ledger items.

No renamed facts are persisted. Source quotations, extracted interpretation and
the stored normalized value have separate views, including for legacy records.
"""
from __future__ import annotations

from copy import deepcopy
import re

from .extraction import normalized_text


def subject_id(record):
    return record.get("subject_entity_id") or record["entity_id"]


def value_of(record):
    return record.get("value", record.get("display_value"))


def applicable_time(record):
    """Read the declared applicability only; old records retain their interpretation."""
    return deepcopy(record.get("effective_time"))


def entity_names(entity):
    # A linked object or arbitrary identity_clue value is never an alias.
    return [entity.get("canonical_name", ""), *entity.get("aliases", [])]


def related_roles(record):
    value = value_of(record) or {}
    return {"object_entity_ids": [value["entity_id"]]
            if value.get("kind") == "entity" and value.get("entity_id") else [],
            "qualifier_entities": [{"path": link["path"], "entity_id": link["entity_id"]}
                                   for link in record.get("qualifier_entity_links", [])]}


def semantic_view(record, entities, *, arrival=None, source=None):
    """Map the existing representation without inferring missing conditions/time."""
    sid = subject_id(record)
    entity = entities[sid]
    refs = record.get("evidence_ref_ids") or [r["evidence_ref_id"] for r in record.get("evidence_refs", [])]
    return {
        "subject": {"entity_id": sid, "name": entity["canonical_name"]},
        "relation": record["predicate"], "object": deepcopy(value_of(record)),
        "qualifiers": deepcopy(record.get("qualifiers", {})),
        "related_roles": related_roles(record),
        "valid_time": applicable_time(record),
        "temporal": deepcopy(record.get("temporal", {})),
        "field_evidence": deepcopy(record.get("field_evidence", {})),
        "semantic_contract_version": record.get("semantic_contract_version", "legacy"),
        "observed_at": (arrival or {}).get("received_at"),
        "document_date": (source or {}).get("document_date"),
        "evidence_ref_ids": list(refs),
    }


def observation_view(record, evidence):
    """Only exact located text is a source observation; raw_value is interpretation."""
    ids = record.get("evidence_ref_ids") or [r["evidence_ref_id"] for r in record.get("evidence_refs", [])]
    observations = record.get("source_observations")
    if observations is None:
        from .source_alignment import build_source_observations
        observations = build_source_observations(record, [evidence[rid] for rid in ids])
    return {"evidence_ref_ids": list(ids), "values": deepcopy(observations),
            "exact_value_spans": [{k: o[k] for k in ("evidence_ref_id", "start", "end")}
                                  for o in observations if o.get("status") == "located"],
            "unit_basis": deepcopy(record.get("unit_validation", {}))}


def subject_identities(entities, records):
    """Separate a subject's identifying clues from its linked object/qualifier roles.

    Legacy identity_clues may contain properties of another entity. A clue whose
    value names an explicitly linked object/qualifier remains a contextual clue,
    not evidence that two subjects are the same. No domain field names are used.
    """
    related_names = {eid: set() for eid in entities}
    for record in records:
        sid = subject_id(record)
        roles = related_roles(record)
        linked = [*roles["object_entity_ids"], *[r["entity_id"] for r in roles["qualifier_entities"]]]
        for eid in linked:
            if eid != sid and eid in entities:
                related_names[sid].update(normalized_text(n) for n in entity_names(entities[eid]) if n)
    return {eid: {
        "names": [n for n in entity_names(entity) if n],
        "identifiers": {key: value for key, value in entity.get("identity_clues", {}).items()
                        if normalized_text(value) not in related_names[eid]},
        "contextual_clues": {key: value for key, value in entity.get("identity_clues", {}).items()
                             if normalized_text(value) in related_names[eid]},
    } for eid, entity in entities.items()}


def statement_of(record):
    return record.get("statement", record.get("display_statement", ""))


def evidence_text(record):
    return " ".join(re.sub(r"<[^>]*>", " ", r["raw_content"]) for r in record.get("evidence_refs", []))

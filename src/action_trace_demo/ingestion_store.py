"""M01 records in the existing append-only TemporalDAG SQLite database.

Extraction receipts may arrive concurrently. Their semantic merge is explicitly
ordered by arrival and group order; no M01 operation revises a legacy claim.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path

from .extraction import (SEMANTIC_CONTRACT_VERSION, claim_key, normalized_text,
                         normalized_value, partition_extraction)
from .temporal_dag import TemporalDAG


def _now():
    return datetime.now(timezone.utc).isoformat()


class IngestionStore:
    def __init__(self, workspace):
        self.workspace = Path(workspace)
        self.dag = TemporalDAG(self.workspace)
        self.path = self.dag.path

    @staticmethod
    def _rows(db, kind=None, as_of=None):
        rows = db.execute("SELECT seq,kind,payload FROM event WHERE kind LIKE 'm01_%' "
                          "AND (? IS NULL OR kind=?) AND (? IS NULL OR seq<=?) ORDER BY seq",
                          (kind, kind, as_of, as_of))
        return [{"record_id": f"D{r['seq']:09d}", "seq": r["seq"], "kind": r["kind"],
                 **json.loads(r["payload"])} for r in rows]

    @staticmethod
    def _append(db, kind, payload, parents=()):
        event = TemporalDAG._append(db, "m01_" + kind,
                                    {"recorded_at": _now(), **payload}, parents)
        return {"record_id": event["id"], "seq": event["seq"], **event["payload"]}

    @classmethod
    def _sources(cls, db):
        return [{**r, "source_version_id": r["record_id"], "created_record_id": r["record_id"]}
                for r in cls._rows(db, "m01_source_version")]

    def source_versions(self):
        with self.dag._db() as db:
            return self._sources(db)

    def get_source(self, source_version_id):
        return next(s for s in self.source_versions() if s["source_version_id"] == source_version_id)

    def task_card(self, task_input=None):
        """Read or append explicit task instructions in the existing event log."""
        fields = ("instruction_text", "instruction_ref", "goal_text", "scope_text", "output_requirements")
        if isinstance(task_input, str):
            task_input = {"instruction_text": task_input}
        if task_input is not None and not task_input.get("instruction_text", "").strip():
            raise ValueError("Task instruction_text is required")
        with self.dag._db(write=task_input is not None) as db:
            row = db.execute("SELECT seq,payload FROM event WHERE kind='m02_task_card' "
                             "ORDER BY seq DESC LIMIT 1").fetchone()
            current = None
            if row is not None:
                record_id = f"D{row['seq']:09d}"
                payload = json.loads(row["payload"])
                current = {**payload, "task_id": payload.get("task_id", record_id),
                           "created_record_id": record_id}
            if task_input is None:
                return current
            explicit = {key: task_input[key] for key in fields if key in task_input}
            task_id = task_input.get("task_id") or (current["task_id"] if current else None)
            if current:
                if task_id != current["task_id"]:
                    raise ValueError(f"Workspace task is {current['task_id']}; task_id cannot change")
                if explicit == {key: current[key] for key in fields if key in current}:
                    return current
            payload = {**explicit, "recorded_at": _now()}
            if task_id is not None:
                payload["task_id"] = task_id
            event = TemporalDAG._append(db, "m02_task_card", payload,
                                        [current["created_record_id"]] if current else [])
            return {**event["payload"], "task_id": task_id or event["id"],
                    "created_record_id": event["id"]}

    def initial_ledger_state(self):
        """Read the original ledger's immutable range, not the latest arrival state."""
        with self.dag._db() as db:
            markers = self._rows(db, "m01_ledger_initialized")
            if not markers:
                return None
            marker = markers[0]
            items = [{**r, "ledger_item_id": r["record_id"], "introduced_record_id": r["record_id"]}
                     for r in self._rows(db, "m01_ledger_item", marker["seq"])]
            cutoff = marker.get("source_cutoff_event_seq")
            if cutoff is None:
                # Existing M01 markers predate these snapshot fields. Their items
                # already pin normalization; an empty ledger uses its preceding result.
                cutoffs = [item["normalization_as_of"] for item in items if "normalization_as_of" in item]
                results = [r for r in self._rows(db, "m01_arrival_result", marker["seq"])
                           if r["arrival_id"] == marker["arrival_id"]]
                cutoff = cutoffs[0] if cutoffs else results[-1]["seq"]
            return {"ledger_snapshot_id": marker["record_id"],
                    "initial_arrival_id": marker["arrival_id"],
                    "included_claim_ids": list(dict.fromkeys(a for item in items for a in item["assertion_ids"])),
                    "source_cutoff_event_seq": cutoff,
                    "normalization_as_of": marker.get("normalization_as_of", cutoff),
                    "items": items}

    def current_ledger_state(self):
        """Return the latest actual M03 version, or the unchanged initial ledger."""
        from .maintenance_store import current_ledger_state
        return current_ledger_state(self.workspace)

    def register_arrival(self, sources, task_context=None, received_at=None):
        with self.dag._db(write=True) as db:
            versions = self._sources(db)
            linked = []
            for source in sources:
                identity = (source["content_hash"], source.get("source_id"))
                existing = next((s for s in versions if
                                 (s["content_hash"], s.get("source_id")) == identity), None)
                if existing is None:
                    record = self._append(db, "source_version", {
                        "source_id": source.get("source_id"), "display_name": source["display_name"],
                        "media_type": source["media_type"], "content_hash": source["content_hash"],
                        "original_location": source["original_location"],
                        "document_date": source.get("document_date"),
                    })
                    existing = {**record, "source_version_id": record["record_id"]}
                    versions.append(existing)
                linked.append({"source_version_id": existing["source_version_id"],
                               "display_name": source["display_name"],
                               "original_location": source["original_location"]})
            record = self._append(db, "arrival", {
                "received_at": received_at or _now(), "task_context": task_context,
                "source_version_ids": [s["source_version_id"] for s in linked], "sources": linked,
            }, [s["source_version_id"] for s in linked])
            return {**record, "arrival_id": record["record_id"], "arrival_seq": record["seq"]}

    def get_arrival(self, arrival_id):
        with self.dag._db() as db:
            r = next(r for r in self._rows(db, "m01_arrival") if r["record_id"] == arrival_id)
            return {**r, "arrival_id": r["record_id"], "arrival_seq": r["seq"]}

    def save_parse(self, source_version_id, blocks, windows, *, parse_result_location,
                   parse_profile, evidence_refs=None, normalization_version=None):
        from .evidence_windows import NORMALIZATION_VERSION, normalization_diagnostics
        normalization_version = normalization_version or NORMALIZATION_VERSION
        with self.dag._db(write=True) as db:
            payload = {"source_version_id": source_version_id, "blocks": blocks,
                       "windows": windows, "evidence_refs": evidence_refs or [],
                       "parse_result_location": str(parse_result_location), "parse_profile": parse_profile,
                       "normalization_version": normalization_version,
                       "normalization_diagnostics": normalization_diagnostics(blocks)}
            previous = [r for r in self._rows(db, "m01_parse")
                        if r["source_version_id"] == source_version_id and r["parse_profile"] == parse_profile
                        and r.get("normalization_version") == normalization_version]
            if previous:
                return {**previous[-1], "reused": True}
            return self._append(db, "parse", payload, [source_version_id])

    def get_parse(self, source_version_id):
        with self.dag._db() as db:
            rows = [r for r in self._rows(db, "m01_parse") if r["source_version_id"] == source_version_id]
            return rows[-1] if rows else None

    def mark_source_failed(self, arrival_id, source_version_id, error):
        with self.dag._db(write=True) as db:
            return self._append(db, "source_failure", {"arrival_id": arrival_id,
                                "source_version_id": source_version_id, "error": str(error)},
                                [arrival_id, source_version_id])

    def record_group_plan(self, group, group_file):
        with self.dag._db(write=True) as db:
            return self._append(db, "group_plan", {
                "arrival_id": group["arrival_id"], "window_group_id": group["window_group_id"],
                "original_group_id": group.get("original_group_id", group["window_group_id"]),
                "profile_id": group["extraction_profile"]["profile_id"],
                "windows": group["windows"], "group_file": str(group_file),
                "estimated_input_tokens": group["estimated_input_tokens"],
                "output_token_estimate": group["output_token_estimate"],
                "capacity_policy": group["capacity_policy"],
                "input_schema_version": (group.get("task_schema") or {}).get("version_id"),
                "source_pipeline_version": group.get("source_pipeline_version"),
            }, [group["arrival_id"]])

    def record_group_split(self, group, children, receipt):
        with self.dag._db(write=True) as db:
            return self._append(db, "group_split", {
                "arrival_id": group["arrival_id"], "parent_group_id": group["window_group_id"],
                "child_group_ids": [c["window_group_id"] for c in children],
                "parent_window_ids": [w["window_id"] for w in group["windows"]],
                "child_window_ids": [w["window_id"] for c in children for w in c["windows"]],
                "receipt": receipt,
            }, [group["arrival_id"]])

    @staticmethod
    def _extraction_attempts(db, arrival_id, group_id):
        """Decode only one unit's retry history while holding the writer lock."""
        rows = db.execute("""SELECT seq,kind,payload FROM event
            WHERE kind='m01_extraction'
              AND json_extract(payload,'$.arrival_id')=?
              AND json_extract(payload,'$.window_group_id')=? ORDER BY seq""", (arrival_id, group_id))
        return [{"record_id": f"D{r['seq']:09d}", "seq": r["seq"], "kind": r["kind"],
                 **json.loads(r["payload"])} for r in rows]

    def group_extraction_progress(self, arrival_id, group_id, profile_id):
        """Read completion/recovery flags for one job and its descendants.

        Group IDs contain underscores (and may contain other SQL wildcard
        characters). A binary lexical range implements a literal prefix; LIKE
        would accidentally include unrelated jobs. Whole-store projections and
        original claim/evidence bodies are unnecessary for these two flags.
        """
        prefix = group_id + "_"
        upper = group_id + "`"  # immediate ASCII successor of the trailing '_'
        select = """SELECT json_extract(payload,'$.window_group_id') AS group_id,
            json_extract(payload,'$.extraction_profile.profile_id') AS profile_id,
            json_extract(payload,'$.result.processed_window_ids') AS processed
            FROM event WHERE kind='m01_extraction'
              AND json_extract(payload,'$.arrival_id')=?"""
        with self.dag._db() as db:
            rows = db.execute(select + " AND json_extract(payload,'$.window_group_id')=?"
                + " UNION ALL " + select
                + " AND json_extract(payload,'$.window_group_id')>=?"
                + " AND json_extract(payload,'$.window_group_id')<?",
                (arrival_id, group_id, arrival_id, prefix, upper)).fetchall()
        completed = {wid for row in rows if row["profile_id"] == profile_id
                     for wid in json.loads(row["processed"])}
        return {"processed_unit_ids": sorted(completed),
                "has_saved_units": any(row["group_id"] != group_id for row in rows),
                "extraction_record_count": len(rows)}

    def stage_extraction(self, arrival_id, group_id, result, provided_evidence_refs,
                         group_order, *, allowed_window_ids=None, candidate_entity_ids=None,
                         extraction_profile=None, retry_claim_indices=None,
                         prevalidation_errors=None, proposal_metadata=None):
        """Append a proposal and its independently accepted claims; never erase a sibling."""
        proposal = json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))
        if not isinstance(proposal, dict) or any(not isinstance(proposal.get(k), list) for k in
                ("entities", "claims", "processed_window_ids", "unresolved")):
            raise ValueError("Extraction requires entities, claims, processed_window_ids and unresolved arrays")
        refs = {r["evidence_ref_id"]: r for r in provided_evidence_refs}
        with self.dag._db(write=True) as db:
            attempts = self._extraction_attempts(db, arrival_id, group_id)
            for old in attempts:
                if (old.get("proposal", old["result"]) == proposal and old.get("retry_claim_indices") == retry_claim_indices
                        and (proposal_metadata is None or old.get("proposal_metadata") == proposal_metadata)):
                    return {**old.get("save_receipt", {"ok": True, "committed": True,
                        "session_complete": True, "source_claim_count": len(old["result"]["claims"])}),
                        "extraction_run_id": old["record_id"], "reused": True}
            previous = attempts[-1] if attempts else None
            original_count = previous.get("original_claim_count", len(previous["result"]["claims"])) if previous else len(proposal["claims"])
            accepted_before = set(previous.get("accepted_claim_indices", [])) if previous else set()
            pending_before = {entry["index"]: entry for entry in previous.get("rejected_claims", [])} if previous else {}
            indices = list(range(len(proposal["claims"])))
            if previous:
                if not pending_before:
                    raise ValueError("This window group has no unresolved claims; its accepted facts are immutable")
                if retry_claim_indices is not None:
                    indices = retry_claim_indices
                elif len(proposal["claims"]) == original_count:
                    indices = list(range(original_count))
                elif len(proposal["claims"]) == len(pending_before):
                    indices = sorted(pending_before)
                else:
                    raise ValueError("Retry only rejected claims in receipt order, or specify retry_claim_indices")
                if len(indices) != len(proposal["claims"]) or len(set(indices)) != len(indices):
                    raise ValueError("retry_claim_indices must uniquely identify each submitted claim")
                if not set(indices) <= accepted_before | pending_before.keys():
                    raise ValueError("Retry indices must refer to claims in the original proposal")
            active = [(i, c) for i, c in zip(indices, proposal["claims"]) if i not in accepted_before]
            # Already accepted local entities are context for a failed claim's repair.
            entities = {e["local_entity_id"]: e for attempt in attempts for e in attempt["result"]["entities"]}
            for entity in proposal["entities"]:
                if isinstance(entity, dict) and "local_entity_id" in entity:
                    entities[entity["local_entity_id"]] = entity
            working = {**proposal, "entities": list(entities.values()) if previous else proposal["entities"],
                       "claims": [c for _, c in active]}
            checked = partition_extraction(working, provided_evidence_refs,
                allowed_window_ids=allowed_window_ids, candidate_entity_ids=candidate_entity_ids)
            # A source adapter may identify an invalid literal or short anchor
            # before the internal claim validator. Preserve its exact failure at
            # the same independent-claim seam, including during partial repair.
            adapter_errors = {i: prevalidation_errors[indices.index(original)]
                              for i, (original, _) in enumerate(active)
                              if prevalidation_errors and indices.index(original) in prevalidation_errors}
            if adapter_errors:
                checked["result"]["claims"] = [claim for i, claim in zip(
                    checked["accepted_claim_indices"], checked["result"]["claims"]) if i not in adapter_errors]
                checked["accepted_claim_indices"] = [i for i in checked["accepted_claim_indices"] if i not in adapter_errors]
                checked["rejected_claims"] = [r for r in checked["rejected_claims"] if r["index"] not in adapter_errors]
                checked["rejected_claims"].extend({"index": i, "error": message} for i, message in adapter_errors.items())
            accepted_now = [active[i][0] for i in checked["accepted_claim_indices"]]
            rejected = dict(pending_before)
            for index, _ in active:
                rejected.pop(index, None)
            rejected.update((active[e["index"]][0], {**e, "index": active[e["index"]][0]})
                            for e in checked["rejected_claims"])
            warnings = [{**w, "index": active[w["index"]][0]} if w.get("kind") == "claims" else w
                        for w in checked["warnings"]]
            accepted = sorted(accepted_before | set(accepted_now))
            saved = checked["result"]
            complete = not rejected
            saved["processed_window_ids"] = list(proposal["processed_window_ids"]) if complete else []
            if allowed_window_ids is not None:
                saved["processed_window_ids"] = [w for w in saved["processed_window_ids"] if w in allowed_window_ids]
            arrival_node = self.dag._get(db, arrival_id)
            if arrival_node["kind"] != "m01_arrival":
                raise ValueError("Extraction requires an arrival event")
            arrival = arrival_node["payload"]
            if any(r["source_version_id"] not in arrival["source_version_ids"] for r in refs.values()):
                raise ValueError("Extraction evidence is outside this arrival")
            receipt = {"ok": True, "committed": True, "session_complete": complete,
                "status": "staged" if complete else "partially_staged",
                "entity_count": len(saved["entities"]), "source_claim_count": len(saved["claims"]),
                "processed_window_count": len(saved["processed_window_ids"]),
                "accepted_claim_indices": accepted, "newly_accepted_claim_indices": accepted_now,
                "already_accepted_claim_indices": sorted(set(indices) & accepted_before),
                "rejected_claims": [rejected[i] for i in sorted(rejected)],
                "rejected_entities": checked["rejected_entities"], "warnings": warnings,
                "unresolved": saved["unresolved"]}
            record = self._append(db, "extraction", {
                "arrival_id": arrival_id, "window_group_id": group_id, "group_order": group_order,
                "result": saved, "proposal": proposal, "retry_claim_indices": retry_claim_indices,
                "original_claim_count": original_count, "accepted_claim_indices": accepted,
                "rejected_claims": receipt["rejected_claims"], "save_receipt": receipt,
                "provided_evidence_refs": list(refs.values()), "allowed_window_ids": allowed_window_ids,
                "semantic_contract_version": SEMANTIC_CONTRACT_VERSION, "extraction_profile": extraction_profile,
                "input_schema_version": (extraction_profile or {}).get("input_schema_version"),
                "proposal_metadata": proposal_metadata,
            }, [arrival_id, *([previous["record_id"]] if previous else [])])
            return {**receipt, "extraction_run_id": record["record_id"], "reused": False}

    @classmethod
    def _entities(cls, db, as_of=None):
        entities = {r["record_id"]: {**r, "entity_id": r["record_id"], "aliases": [], "mention_refs": []}
                    for r in cls._rows(db, "m01_entity", as_of)}
        roles = {key: binding for record in cls._rows(db, "m01_mapping_version", as_of)
                 for key, binding in record.get("concept_bindings", {}).items()}
        for mention in cls._rows(db, "m01_mention", as_of):
            entity = entities[mention["entity_id"]]
            entity["mention_refs"].append(mention["record_id"])
            for alias in [mention["surface_name"], *mention.get("aliases", [])]:
                if alias != entity["canonical_name"] and alias not in entity["aliases"]:
                    entity["aliases"].append(alias)
            prefix = f'{mention["extraction_run_id"]}:entity:{mention["local_entity_id"]}'
            typ = roles.get(prefix + ":type")
            if typ:
                entity["entity_type"] = typ["key"]
            for key, value in mention.get("identity_clues", {}).items():
                role = roles.get(prefix + ":identifier:" + key)
                if role and role["key"] != key:
                    entity["identity_clues"].pop(key, None)
                entity["identity_clues"][role["key"] if role else key] = value
            entity.setdefault("identity_basis", []).extend(
                {"mention_id": mention["record_id"], "surface_name": mention["surface_name"],
                 "evidence_ref_id": r["evidence_ref_id"], "raw_content": r["raw_content"]}
                for r in mention.get("evidence_refs", [])[:1])
        for alias in cls._rows(db, "m01_alias_binding", as_of):
            entity = entities[alias["entity_id"]]
            entity.setdefault("confirmed_aliases", []).append(alias["surface_name"])
            if alias["surface_name"] not in entity["aliases"]:
                entity["aliases"].append(alias["surface_name"])
            entity.setdefault("identity_basis", []).append({k: alias[k] for k in
                ("surface_name", "evidence_ref_id", "raw_content", "start", "end") if k in alias})
        for entity in entities.values():
            # Explicit alias evidence is most useful to the next arrival. Keep a
            # small source-backed catalog; the full mentions remain expandable.
            entity["identity_basis"] = entity.get("identity_basis", [])[-3:]
        return list(entities.values())

    def lookup_entities(self, mentions, limit=20):
        with self.dag._db() as db:
            entities = self._entities(db)
        from .extraction_contract import LookupMention
        names = [normalized_text(m if isinstance(m, str) else
                 (m.name if isinstance(m, LookupMention) else LookupMention.model_validate(m).name))
                 for m in mentions]
        if not names:
            return entities[:limit]
        ranked = []
        for entity in entities:
            haystack = normalized_text(" ".join([entity["canonical_name"], *entity["aliases"],
                                                 entity.get("description", ""),
                                                 json.dumps(entity.get("identity_clues", {}), ensure_ascii=False)]))
            score = sum(bool(name) and name in haystack for name in names)
            if score:
                ranked.append((score, entity))
        return [e for _, e in sorted(ranked, key=lambda pair: -pair[0])[:limit]]

    @classmethod
    def _projection_state(cls, db, as_of=None):
        from .semantic_schema import make_schema
        from .semantic_ingestion import PROJECTION_VERSION
        schemas = cls._rows(db, "m01_task_schema", as_of)
        mappings = cls._rows(db, "m01_mapping_version", as_of)
        state = {"schema": schemas[-1]["schema"] if schemas else make_schema({"concepts": []}),
                 "entity_bindings": {}, "concept_bindings": {}, "mapping_version": None,
                 "projection_version": PROJECTION_VERSION, "visible_as_of": as_of}
        for mapping in mappings:
            for key in ("entity_bindings", "concept_bindings"):
                state[key].update(mapping.get(key, {}))
            state["mapping_version"] = mapping["record_id"]
        state["mention_entity_ids"] = state["entity_bindings"]
        return state

    @staticmethod
    def _align(mention, entities):
        explicit = mention.get("existing_entity_id")
        if explicit:
            return next(e for e in entities if e["entity_id"] == explicit)
        clues = mention.get("identity_clues", {})
        if clues:
            matches = []
            for entity in entities:
                other = entity.get("identity_clues", {})
                common = set(clues) & set(other)
                if common and all(normalized_text(clues[k]) == normalized_text(other[k]) for k in common):
                    matches.append(entity)
            if len(matches) == 1:
                return matches[0]
        name = normalized_text(mention["surface_name"])
        matches = [e for e in entities if name in {normalized_text(a) for a in e["aliases"]}]
        if len(matches) == 1:
            return matches[0]
        return None

    def reusable_source_claims(self, source_version_id, *, extraction_profile=None):
        with self.dag._db() as db:
            results = self._rows(db, "m01_arrival_result")
            completed = {r["arrival_id"] for r in results if r["status"] == "completed"}
            claims = self._rows(db, "m01_source_claim")
            matches = [r for r in claims if r["arrival_id"] in completed and
                       source_version_id == r["source_version_id"] and
                       (extraction_profile is None or r.get("extraction_profile_id") == extraction_profile["profile_id"])]
            return [{**r, "assertion_id": r["record_id"]} for r in matches]

    def mark_reused(self, arrival_id, source_version_id, assertion_ids, *, profile_id=None):
        with self.dag._db(write=True) as db:
            old = [r for r in self._rows(db, "m01_source_reuse") if r["arrival_id"] == arrival_id
                   and r["source_version_id"] == source_version_id and r["assertion_ids"] == assertion_ids
                   and r.get("profile_id") == profile_id]
            return old[-1] if old else self._append(db, "source_reuse", {
                "arrival_id": arrival_id, "source_version_id": source_version_id,
                "assertion_ids": assertion_ids, "profile_id": profile_id}, [arrival_id, *assertion_ids])

    def reusable_source_result(self, source_version_id, profile_id, *, exclude_arrival_id=None):
        """Reuse completed source work, including explicitly empty sources.

        A failed sibling source does not invalidate this source's saved result.
        Canonical mappings are projected separately and are not a cache key.
        """
        with self.dag._db() as db:
            for result in reversed(self._rows(db, "m01_arrival_result")):
                if result["arrival_id"] == exclude_arrival_id:
                    continue
                source = result.get("source_statuses", {}).get(source_version_id)
                if source and source["profile_id"] == profile_id and source["status"] == "completed":
                    return source
        return None

    @staticmethod
    def _qualifier_alias_index(entities):
        """Only a unique, already aligned entity can resolve a qualifier name."""
        candidates = defaultdict(dict)
        for entity in entities:
            for name in [entity["canonical_name"], *entity.get("aliases", [])]:
                normalized = normalized_text(name)
                if normalized:
                    candidates[normalized][entity["entity_id"]] = entity
        return {name: next(iter(matches.values())) for name, matches in candidates.items()
                if len(matches) == 1}

    @staticmethod
    def _claim_semantics(entity_id, claim, aliases):
        """Keep source qualifiers intact; derive a separately auditable key view."""
        links = []

        def canonical(value, path):
            if isinstance(value, str):
                entity = aliases.get(normalized_text(value))
                if entity:
                    links.append({"path": path, "surface": value, "entity_id": entity["entity_id"],
                                  "mention_refs": list(entity.get("mention_refs", []))})
                    return {"entity_id": entity["entity_id"]}
            elif isinstance(value, dict):
                return {key: canonical(item, [*path, key]) for key, item in value.items()}
            elif isinstance(value, list):
                return [canonical(item, [*path, index]) for index, item in enumerate(value)]
            return value

        qualifiers = canonical(claim.get("qualifiers", {}), [])
        for explicit in claim.get("qualifier_entity_links", []):
            if explicit.get("binding") != "explicit":
                continue
            links = [link for link in links if link["path"] != explicit["path"]]
            links.append(explicit)
            container = qualifiers
            for part in explicit["path"][:-1]:
                container = container[part]
            original = claim.get("qualifiers", {})
            for part in explicit["path"]:
                original = original[part]
            named = aliases.get(normalized_text(original)) if isinstance(original, str) else None
            # A field link may mention an entity inside a condition sentence.
            # Keep that sentence (including negation/pending state) unless the
            # field itself is exactly a confirmed name for this entity.
            if named and named["entity_id"] == explicit["entity_id"]:
                container[explicit["path"][-1]] = {"entity_id": explicit["entity_id"]}
        return {"canonical_qualifiers": qualifiers, "qualifier_entity_links": links,
                "semantic_key_version": "confirmed-qualifier-alias-v1",
                "semantic_key": claim_key(entity_id, {**claim, "qualifiers": qualifiers})}

    def merge_arrival(self, arrival_id):
        with self.dag._db(write=True) as db:
            arrivals = self._rows(db, "m01_arrival")
            arrival = next(r for r in arrivals if r["record_id"] == arrival_id)
            finished = {r["arrival_id"] for r in self._rows(db, "m01_arrival_result")}
            if any(r["seq"] < arrival["seq"] and r["record_id"] not in finished for r in arrivals):
                raise ValueError("Earlier arrival must finish before this semantic merge")
            merged = {r["extraction_run_id"] for r in self._rows(db, "m01_extraction_merged")}
            groups = [r for r in self._rows(db, "m01_extraction")
                      if r["arrival_id"] == arrival_id and r["record_id"] not in merged]
            groups.sort(key=lambda r: (r["group_order"], r["seq"]))
            entities = self._entities(db)
            assertions = self._rows(db, "m01_source_claim")
            created_entities, assertion_ids = [], []
            prepared_groups = []
            resolutions = [r for r in self._rows(db, "m01_semantic_resolution")
                           if r["arrival_id"] == arrival_id]
            resolved = {mid: binding for r in resolutions for mid, binding in r["result"]["entity_bindings"].items()}
            concept_bindings = {key: binding for r in resolutions
                                for key, binding in r["result"]["concept_bindings"].items()}
            occurrence_ids = [f'{g["record_id"]}:{m["local_entity_id"]}' for g in groups for m in g["result"]["entities"]]
            # Union batch choices before allocating IDs, so completion/choice direction
            # cannot select a later group as the canonical identity.
            parent = {mid: mid for mid in occurrence_ids}
            rank = {mid: i for i, mid in enumerate(occurrence_ids)}
            historical_ids = {e["entity_id"] for e in entities}
            def representative(mid):
                while mid in parent and parent[mid] != mid:
                    mid = parent[mid]
                return mid
            for mid, binding in resolved.items():
                target = binding.get("target_id") if isinstance(binding, dict) else binding
                if mid not in parent or not target or target == mid:
                    continue
                left, right = representative(mid), representative(target)
                # A new mention cannot merge two already persistent identities.
                if left in historical_ids and right in historical_ids and left != right:
                    continue
                if right in historical_ids:
                    parent[left] = right
                elif right in parent and left != right:
                    if left in historical_ids:
                        parent[right] = left
                    else:
                        early, late = sorted((left, right), key=lambda x: rank[x])
                        parent[late] = early
            allocated = {e["entity_id"]: e for e in entities}
            persistent_bindings = {}
            # Register every staged mention before deriving keys: an explicit alias
            # in a later group must be available to earlier claims in this arrival.
            for group in groups:
                local = {}
                refs = {r["evidence_ref_id"]: r for r in group["provided_evidence_refs"]}
                for mention in group["result"]["entities"]:
                    occurrence = f'{group["record_id"]}:{mention["local_entity_id"]}'
                    identity_root = representative(occurrence)
                    entity = allocated.get(identity_root) if resolutions else self._align(mention, entities)
                    if entity is None:
                        record = self._append(db, "entity", {
                            "canonical_name": mention["surface_name"], "entity_type": mention.get("type_hint", ""),
                            "description": mention.get("description", ""), "context": mention.get("context", ""),
                            "identity_clues": mention.get("identity_clues", {}),
                        }, [group["record_id"]])
                        entity = {**record, "entity_id": record["record_id"], "aliases": [], "mention_refs": []}
                        entities.append(entity)
                        created_entities.append(entity["entity_id"])
                    allocated[identity_root] = entity
                    entity_id = entity["entity_id"]
                    persistent_bindings[occurrence] = entity_id
                    local[mention["local_entity_id"]] = entity_id
                    m = self._append(db, "mention", {**mention, "entity_id": entity_id,
                        "arrival_id": arrival_id, "extraction_run_id": group["record_id"],
                        "evidence_refs": [refs[k] for k in mention["evidence_ref_ids"]]},
                        [group["record_id"], entity_id])
                    entity["mention_refs"].append(m["record_id"])
                    for alias in [mention["surface_name"], *mention.get("aliases", [])]:
                        if alias != entity["canonical_name"] and alias not in entity["aliases"]:
                            entity["aliases"].append(alias)
                    entity["identity_clues"].update(mention.get("identity_clues", {}))
                prepared_groups.append((group, local, refs))
            if groups and resolutions:
                for resolution in resolutions:
                    for alias in resolution["result"].get("alias_mentions", resolution["result"].get("aliases", [])):
                        target = persistent_bindings.get(alias["target_id"], alias["target_id"])
                        if target not in {e["entity_id"] for e in entities}:
                            continue
                        old = [a for a in self._rows(db, "m01_alias_binding") if a["entity_id"] == target
                               and a["surface_name"] == alias["surface_name"] and a["evidence_ref_id"] == alias["evidence_ref_id"]]
                        if not old:
                            self._append(db, "alias_binding", {**alias, "entity_id": target,
                                "arrival_id": arrival_id, "resolution_record_id": resolution["record_id"]}, [target, resolution["record_id"]])
                self._append(db, "mapping_version", {"arrival_id": arrival_id,
                    "entity_bindings": persistent_bindings, "concept_bindings": concept_bindings,
                    "resolution_record_ids": [r["record_id"] for r in resolutions]},
                    [r["record_id"] for r in resolutions])
                entities = self._entities(db)
            aliases = self._qualifier_alias_index(entities)
            assertions_by_key = {}
            for existing in assertions:
                key = self._claim_semantics(existing["entity_id"], existing, aliases)["semantic_key"]
                assertions_by_key.setdefault((existing["source_version_id"], key,
                    existing.get("extraction_profile_id"), json.dumps(existing.get("temporal"), sort_keys=True)), existing)
            for group, local, refs in prepared_groups:
                for claim_index, raw in enumerate(group["result"]["claims"]):
                    claim = {**raw, "extraction_claim_index": claim_index}
                    claim["semantic_contract_version"] = group.get("semantic_contract_version", "legacy")
                    claim["extraction_profile_id"] = (group.get("extraction_profile") or {}).get("profile_id")
                    entity_id = local[claim["subject_local_id"]]
                    extracted = claim.get("value")
                    claim["value"] = normalized_value(extracted)
                    if extracted and claim["value"] != extracted and claim.get("unit_validation") is not None:
                        from decimal import Decimal
                        before, after = extracted.get("normalized"), claim["value"].get("normalized")
                        claim["unit_validation"]["storage_conversion"] = {
                            "from": extracted, "to": claim["value"],
                            "multiplier": str(Decimal(after) / Decimal(before)) if before and Decimal(before) else None}
                    for observation in claim.get("source_observations", []):
                        if observation.get("local_entity_id") in local:
                            observation["entity_id"] = local[observation["local_entity_id"]]
                    if claim["value"] and claim["value"].get("local_entity_id"):
                        claim["value_local_entity_id"] = claim["value"]["local_entity_id"]
                        claim["value"] = {**claim["value"], "entity_id": local[claim["value"]["local_entity_id"]]}
                        del claim["value"]["local_entity_id"]
                    claim["qualifier_entity_links"] = [
                        {"path": binding["path"], "entity_id": local[binding["local_entity_id"]],
                         "local_entity_id": binding["local_entity_id"], "binding": "explicit", "mention_refs": []}
                        for binding in claim.pop("qualifier_entity_refs", [])]
                    evidence = [refs[k] for k in claim["evidence_ref_ids"]]
                    source_ids = list(dict.fromkeys(r["source_version_id"] for r in evidence))
                    semantics = self._claim_semantics(entity_id, claim, aliases)
                    key = semantics["semantic_key"]
                    # Same source version + same assertion is one source fact even across overlapping windows.
                    source_key = (claim["source_version_id"], key, claim["extraction_profile_id"],
                                  json.dumps(claim.get("temporal"), sort_keys=True))
                    previous = assertions_by_key.get(source_key)
                    if previous:
                        assertion_ids.append(previous["record_id"])
                        self._append(db, "assertion_reused", {"arrival_id": arrival_id,
                            "assertion_id": previous["record_id"], "extraction_run_id": group["record_id"],
                            "evidence_refs": evidence}, [group["record_id"], previous["record_id"]])
                        continue
                    record = self._append(db, "source_claim", {**claim, "entity_id": entity_id,
                        "arrival_id": arrival_id, "extraction_run_id": group["record_id"],
                        "source_version_ids": source_ids, "evidence_refs": evidence,
                        **semantics}, [group["record_id"], entity_id, *source_ids,
                                      *dict.fromkeys(ref for link in semantics["qualifier_entity_links"]
                                                     for ref in [link["entity_id"], *link["mention_refs"]])])
                    assertions.append(record)
                    assertions_by_key[source_key] = record
                    assertion_ids.append(record["record_id"])
                self._append(db, "extraction_merged", {"arrival_id": arrival_id,
                    "extraction_run_id": group["record_id"]}, [group["record_id"]])
            return {"arrival_id": arrival_id, "new_entity_ids": created_entities,
                    "assertion_ids": list(dict.fromkeys(assertion_ids)), "merged_group_count": len(groups)}

    def finish_arrival(self, arrival_id, *, usage=None, unresolved_items=None, status=None, extraction_profile=None):
        with self.dag._db(write=True) as db:
            arrival = next(r for r in self._rows(db, "m01_arrival") if r["record_id"] == arrival_id)
            source_ids = set(arrival["source_version_ids"])
            latest_parses = {r["source_version_id"]: r for r in self._rows(db, "m01_parse")
                             if r["source_version_id"] in source_ids}
            parsed = set(latest_parses)
            failures = [r for r in self._rows(db, "m01_source_failure") if r["arrival_id"] == arrival_id]
            if extraction_profile and extraction_profile.get("source_pipeline_version"):
                # Current execution passes its parse failures explicitly. Old
                # failure events remain history, not a permanent resume block.
                failures = []
            groups = [r for r in self._rows(db, "m01_extraction") if r["arrival_id"] == arrival_id]
            reuses = [r for r in self._rows(db, "m01_source_reuse") if r["arrival_id"] == arrival_id]
            if extraction_profile and extraction_profile.get("source_pipeline_version"):
                profile_id = extraction_profile["profile_id"]
                groups = [r for r in groups if (r.get("extraction_profile") or {}).get("profile_id") == profile_id]
                reuses = [r for r in reuses if r.get("profile_id") == profile_id]
            claims = [r for r in self._rows(db, "m01_source_claim") if r["arrival_id"] == arrival_id]
            all_claims = {r["record_id"]: r for r in self._rows(db, "m01_source_claim")}
            reused_claims = [all_claims[r["assertion_id"]] for r in self._rows(db, "m01_assertion_reused")
                             if r["arrival_id"] == arrival_id]
            mentions = [r for r in self._rows(db, "m01_mention") if r["arrival_id"] == arrival_id]
            latest_groups = {r["window_group_id"]: r for r in groups}
            unresolved = [*(unresolved_items or []), *[g for r in latest_groups.values() for g in r["result"]["unresolved"]],
                          *[{"window_group_id": r["window_group_id"], "rejected_claims": r["rejected_claims"]}
                            for r in latest_groups.values() if r.get("rejected_claims")],
                          *[{"source_version_id": r["source_version_id"], "error": r["error"]} for r in failures]]
            unresolved.extend({"source_version_id": sid, "stage": "normalization", "diagnostic": diagnostic}
                              for sid, record in latest_parses.items()
                              for diagnostic in record.get("normalization_diagnostics", [])
                              if diagnostic.get("requires_review"))
            reused_sources = {r["source_version_id"] for r in reuses}
            expected_windows = {w["window_id"] for r in latest_parses.values()
                                if r["source_version_id"] in source_ids - reused_sources for w in r["windows"]}
            processed_windows = {w for r in groups for w in r["result"]["processed_window_ids"]}
            # Child completion satisfies the original range only when every leaf is complete.
            plans = [r for r in self._rows(db, "m01_group_plan") if r["arrival_id"] == arrival_id]
            product_plans = [r for r in plans if r.get("source_pipeline_version") and
                             (not extraction_profile or r["profile_id"] == extraction_profile["profile_id"])]
            if extraction_profile and extraction_profile.get("source_pipeline_version"):
                split_parents = {r["parent_group_id"] for r in self._rows(db, "m01_group_split")}
                leaf_plans = [r for r in product_plans if r["window_group_id"] not in split_parents]
                expected_windows = {w["window_id"] for r in leaf_plans for w in r["windows"]}
                processed_windows = {w for r in groups if
                    (r.get("extraction_profile") or {}).get("profile_id") == extraction_profile["profile_id"]
                    for w in r["result"]["processed_window_ids"]}
            elif plans:
                planned = {w["window_id"]: w for r in self._rows(db, "m01_parse")
                           if r["source_version_id"] in source_ids for w in r["windows"]}
                planned.update({w["window_id"]: w for r in plans for w in r["windows"]})
                covered_blocks = {bid for wid in processed_windows for bid in planned.get(wid, {}).get("main_block_ids", [])}
                processed_windows.update(wid for wid, window in planned.items()
                                         if set(window["main_block_ids"]) <= covered_blocks)
            missing_windows = sorted(expected_windows - processed_windows)
            if missing_windows:
                unresolved.append({"unprocessed_window_ids": missing_windows})
            if status is None:
                status = "failed" if not parsed else "partial" if unresolved or len(parsed) < len(source_ids) else "completed"
            source_statuses = {}
            if extraction_profile and extraction_profile.get("source_pipeline_version"):
                for sid in sorted(source_ids):
                    expected = {w["window_id"] for r in leaf_plans for w in r["windows"]
                                if w["source_version_id"] == sid}
                    source_unresolved = any(u.get("source_version_id") == sid for u in unresolved)
                    ids = [r["record_id"] for r in claims if r["source_version_id"] == sid and
                           r.get("extraction_profile_id") == extraction_profile["profile_id"]]
                    ids += [a for r in reuses if r["source_version_id"] == sid for a in r["assertion_ids"]]
                    ids += [r["record_id"] for r in reused_claims if r["source_version_id"] == sid and
                            r.get("extraction_profile_id") == extraction_profile["profile_id"]]
                    complete = (sid in reused_sources or (bool(expected) and expected <= processed_windows)) and not source_unresolved
                    source_statuses[sid] = {"profile_id": extraction_profile["profile_id"],
                        "status": "completed" if complete else "partial" if sid in parsed else "failed",
                        "expected_unit_ids": sorted(expected), "processed_unit_ids": sorted(expected & processed_windows),
                        "assertion_ids": list(dict.fromkeys(ids)), "reused": sid in reused_sources,
                        "normalization_diagnostics": latest_parses.get(sid, {}).get("normalization_diagnostics", []),
                        "coverage_meaning": "Submitted units saved or explicitly empty; not a semantic recall guarantee"}
            payload = {"arrival_id": arrival_id, "status": status, "source_count": len(arrival["source_version_ids"]),
                       "parsed_source_count": len(parsed),
                       "processed_window_count": len(processed_windows),
                       "entity_count": len({r["entity_id"] for r in mentions}), "source_claim_count": len(claims),
                       "initial_ledger_item_count": 0,
                       "reused_assertion_ids": list(dict.fromkeys([*[a for r in reuses for a in r["assertion_ids"]],
                                                                  *[r["record_id"] for r in reused_claims]])),
                       "unresolved_items": unresolved, "usage": usage or {}}
            if source_statuses:
                payload["source_statuses"] = source_statuses
            return self._append(db, "arrival_result", payload, [arrival_id])

    def get_ingestion_result(self, arrival_id):
        with self.dag._db() as db:
            rows = [r for r in self._rows(db, "m01_arrival_result") if r["arrival_id"] == arrival_id]
            if not rows:
                return {"arrival_id": arrival_id, "status": "pending"}
            result = rows[-1]
            result["entity_count"] = len({r["entity_id"] for r in self._rows(db, "m01_mention")
                                          if r["arrival_id"] == arrival_id})
            initialized = [r for r in self._rows(db, "m01_ledger_initialized") if r["arrival_id"] == arrival_id]
            if initialized:
                result["initial_ledger_item_count"] = initialized[-1]["item_count"]
            return result

    def build_initial_ledger(self, arrival_id):
        with self.dag._db(write=True) as db:
            if self._rows(db, "m01_ledger_initialized"):
                raise ValueError("Initial ledger already exists; later arrivals are M02 deltas")
            first = self._rows(db, "m01_arrival")[0]
            if first["record_id"] != arrival_id:
                raise ValueError("Initial ledger must use the first arrival")
            results = [r for r in self._rows(db, "m01_arrival_result") if r["arrival_id"] == arrival_id]
            if not results:
                raise ValueError("Finish and merge the first arrival before ledger initialization")
            if results[-1]["status"] == "failed":
                raise ValueError("Cannot initialize a ledger from a failed arrival")
            # A delayed initialization may see later arrivals in the database;
            # freeze name resolution at this arrival's completion, not at now.
            normalization_as_of = results[-1]["seq"]
            aliases = self._qualifier_alias_index(self._entities(db, normalization_as_of))
            projection = self._projection_state(db, normalization_as_of)
            from .semantic_schema import project_claim
            bundles = defaultdict(list)
            for r in self._rows(db, "m01_source_claim", normalization_as_of):
                if r["arrival_id"] == arrival_id:
                    r = project_claim(r, projection)
                    semantics = self._claim_semantics(r["entity_id"], r, aliases)
                    bundles[semantics["semantic_key"]].append((r, semantics))
            items = []
            for claims in bundles.values():
                first_claim, semantics = claims[0]
                ids = [r["record_id"] for r, _ in claims]
                r = self._append(db, "ledger_item", {"entity_id": first_claim["entity_id"],
                    "predicate": first_claim["predicate"], "display_statement": first_claim["statement"],
                    "display_value": first_claim.get("value"), "qualifiers": first_claim.get("qualifiers", {}),
                    "effective_time": first_claim.get("effective_time"), "assertion_ids": ids,
                    "semantic_contract_version": first_claim.get("semantic_contract_version", "legacy"),
                    **semantics, "normalization_as_of": normalization_as_of,
                    "projection_metadata": {k: projection[k] for k in ("mapping_version", "projection_version")},
                    "schema_version": projection["schema"]["version_id"]},
                    [*ids, *dict.fromkeys(ref for link in semantics["qualifier_entity_links"]
                                         for ref in [link["entity_id"], *link["mention_refs"]])])
                items.append({**r, "ledger_item_id": r["record_id"], "introduced_record_id": r["record_id"]})
            self._append(db, "ledger_initialized", {
                "arrival_id": arrival_id, "item_count": len(items),
                "source_cutoff_event_seq": normalization_as_of,
                "normalization_as_of": normalization_as_of,
                "included_claim_ids": list(dict.fromkeys(a for item in items for a in item["assertion_ids"])),
            }, [arrival_id])
            return items

    def export(self, arrival_id=None, as_of=None):
        with self.dag._db() as db:
            records = self._rows(db, as_of=as_of)
            entities = self._entities(db, as_of)
            projection = self._projection_state(db, as_of)
        def rows(kind):
            return [r for r in records if r["kind"] == "m01_" + kind]
        from .semantic_schema import project_claim
        claims = [{**project_claim(r, projection), "assertion_id": r["record_id"], "created_record_id": r["record_id"]}
                  for r in rows("source_claim")]
        aliases = self._qualifier_alias_index(entities)
        for claim in claims:
            claim.update(self._claim_semantics(claim["entity_id"], claim, aliases))
            claim["projection_metadata"] = {k: projection[k] for k in ("mapping_version", "projection_version", "visible_as_of")}
        selected = [r for r in claims if arrival_id is None or r["arrival_id"] == arrival_id]
        ledger = [{**r, "ledger_item_id": r["record_id"], "introduced_record_id": r["record_id"]}
                  for r in rows("ledger_item")]
        evidence = {r["evidence_ref_id"]: r for e in rows("extraction") for r in e["provided_evidence_refs"]}
        return {"arrivals": [{**r, "arrival_id": r["record_id"], "arrival_seq": r["seq"]} for r in rows("arrival")],
                "sources": [{**r, "source_version_id": r["record_id"]} for r in rows("source_version")],
                "parses": rows("parse"), "extractions": rows("extraction"),
                "task_schema": projection["schema"], "schema_history": rows("task_schema"),
                "semantic_mappings": rows("mapping_version"), "semantic_resolutions": rows("semantic_resolution"),
                "alias_bindings": rows("alias_binding"),
                "group_plans": rows("group_plan"), "group_splits": rows("group_split"),
                "entities": entities, "entity_mentions": rows("mention"), "source_claims": claims,
                "evidence_refs": list(evidence.values()), "initial_ledger": ledger,
                "results": rows("arrival_result"), "source_reuses": rows("source_reuse"),
                "source_failures": rows("source_failure"),
                "m02_delta": {"arrival_id": arrival_id, "source_claims": selected,
                              "entity_links": [r for r in rows("mention") if arrival_id is None or r["arrival_id"] == arrival_id]}}

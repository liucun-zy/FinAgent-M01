"""Append-only claim and action records for the local research demo.

The gate below verifies source visibility, quote location, and edit structure.
It does not establish semantic entailment or financial correctness. Retrieval is
an intentionally small lexical baseline, not a learned graph-routing algorithm.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any


class TraceStore:
    """Persist immutable sources, proposed edits, comparisons, and decisions.

    ``trace_events.jsonl`` is authoritative. Derived ledger and graph files can
    be rebuilt. Historical views replay only records created at or before their
    requested stage, so a later retraction never rewrites an earlier view.
    """

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.event_path = self.run_dir / "trace_events.jsonl"
        self._events: list[dict[str, Any]] = []
        self._sources: dict[str, dict[str, Any]] = {}
        self._claims: dict[str, dict[str, Any]] = {}
        self._actions: dict[str, dict[str, Any]] = {}
        self._comparisons: dict[str, dict[str, Any]] = {}
        self._last_action_stage = -1
        if self.event_path.exists():
            for number, line in enumerate(self.event_path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid trace event on line {number}; recovery must be explicit") from exc
                if event.get("sequence") != len(self._events) + 1:
                    raise ValueError(f"Non-contiguous trace sequence on line {number}")
                self._events.append(event)
                self._apply(event)

    @staticmethod
    def _stage(stage: int) -> int:
        if not isinstance(stage, int) or isinstance(stage, bool) or stage < 0:
            raise ValueError("stage must be a non-negative integer")
        return stage

    def _apply(self, event: dict[str, Any]) -> None:
        payload = event["payload"]
        if event["kind"] == "source_registered":
            self._sources[payload["source_id"]] = payload
        elif event["kind"] == "claim_created":
            self._claims[payload["claim_id"]] = payload
        elif event["kind"] == "comparison_recorded":
            self._comparisons[payload["comparison_id"]] = payload
        elif event["kind"] in {"action_proposed", "action_decided"}:
            self._actions[payload["action_id"]] = payload
            self._last_action_stage = max(self._last_action_stage, event["stage"])
        elif event["kind"] == "proposal_submitted":
            self._last_action_stage = max(self._last_action_stage, event["stage"])

    def _append(self, kind: str, stage: int, payload: dict[str, Any]) -> dict[str, Any]:
        event = {
            "event_id": f"E{len(self._events) + 1:07d}",
            "sequence": len(self._events) + 1,
            "kind": kind,
            "stage": stage,
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "payload": deepcopy(payload),
        }
        serialized = json.dumps(event, ensure_ascii=False, allow_nan=False)
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write(serialized + "\n")
            handle.flush()
        self._events.append(event)
        self._apply(event)
        return event

    def register_source(self, source_id: str, path: str, text: str, stage: int) -> dict[str, Any]:
        stage = self._stage(stage)
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id must be a non-empty string")
        if not isinstance(text, str):
            raise TypeError("Source text must be a string")
        source = {
            "source_id": source_id, "path": str(path), "text": text,
            "sha256_text_utf8": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "stage": stage, "character_count": len(text),
            "created_event_sequence": len(self._events) + 1,
        }
        existing = self._sources.get(source_id)
        if existing is not None:
            if any(existing[k] != source[k] for k in ("path", "sha256_text_utf8")):
                raise ValueError("A registered source is immutable; use a new source_id for a new version")
            if stage < existing["stage"]:
                raise ValueError("Cannot register a source before its first visible stage")
            # Reading an unchanged source again is a new delivery, not a new
            # source version. Keep the original visibility stage and let the
            # caller record each actual delivery independently.
            return {**deepcopy(existing), "reused": True}
        self._append("source_registered", stage, source)
        return deepcopy(source)

    @staticmethod
    def _key(claim: dict[str, Any]) -> tuple[str, str, str]:
        return (str(claim.get("entity", "")), str(claim.get("field", "")), str(claim.get("scope", "")))

    def _current_stage(self) -> int:
        # Source-only stages are still completed observations of this run.
        # Export must not regress to the most recent maintenance action when
        # a later stage registered evidence but made no claim modification.
        return max((event["stage"] for event in self._events), default=0)

    def _state(self, stage: int) -> dict[str, Any]:
        stage = self._stage(stage)
        sources: dict[str, dict[str, Any]] = {}
        claims: dict[str, dict[str, Any]] = {}
        actions: dict[str, dict[str, Any]] = {}
        comparisons: dict[str, dict[str, Any]] = {}
        heads: dict[tuple[str, str, str], str] = {}
        statuses: dict[str, str] = {}
        for event in self._events:
            if event["stage"] > stage:
                continue
            record = event["payload"]
            kind = event["kind"]
            if kind == "source_registered":
                sources[record["source_id"]] = record
            elif kind == "claim_created":
                claims[record["claim_id"]] = record
                statuses[record["claim_id"]] = "proposed"
            elif kind == "comparison_recorded":
                comparisons[record["comparison_id"]] = record
            elif kind in {"action_proposed", "action_decided"}:
                actions[record["action_id"]] = record
                if kind != "action_decided" or record["status"] != "accepted":
                    continue
                old_id = record.get("old_claim_id")
                if record["op"] in {"update", "retract"} and old_id in claims:
                    old_key = self._key(claims[old_id])
                    if heads.get(old_key) == old_id:
                        del heads[old_key]
                    statuses[old_id] = "retracted" if record["op"] == "retract" else "superseded"
                new_id = record.get("claim_id")
                if new_id in claims:
                    heads[self._key(claims[new_id])] = new_id
                    statuses[new_id] = "active"
        return {
            "sources": sources, "claims": claims, "actions": actions,
            "comparisons": comparisons, "heads": heads, "statuses": statuses,
        }

    @staticmethod
    def _find_quote(text: str, quote: str) -> list[dict[str, Any]]:
        """Locate exact text, then try whitespace-only normalization with offsets."""
        start = text.find(quote)
        if start >= 0:
            return [{"start_char": start, "end_char_exclusive": start + len(quote),
                     "quote": text[start:start + len(quote)], "match_mode": "exact"}]
        normalized_chars = []
        positions = []
        for index, char in enumerate(text):
            if not char.isspace():
                normalized_chars.append(char)
                positions.append(index)
        normalized_quote = "".join(quote.split())
        if not normalized_quote:
            return []
        normalized_text = "".join(normalized_chars)
        start = normalized_text.find(normalized_quote)
        if start < 0:
            return []
        end = start + len(normalized_quote)
        raw_start, raw_end = positions[start], positions[end - 1] + 1
        return [{"start_char": raw_start, "end_char_exclusive": raw_end,
                 "quote": text[raw_start:raw_end], "match_mode": "whitespace_normalized"}]

    def propose(
        self, edits: list[dict], *, stage: int, rationale: str = "",
        read_claim_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        stage = self._stage(stage)
        if stage < self._last_action_stage:
            raise ValueError("Cannot append a maintenance decision to an earlier stage")
        if not isinstance(edits, list) or any(not isinstance(edit, dict) for edit in edits):
            raise TypeError("edits must be a list of objects")
        read_claim_ids = list(dict.fromkeys(read_claim_ids or []))
        proposal_id = f"P{len(self._events) + 1:07d}"
        self._append("proposal_submitted", stage, {
            "proposal_id": proposal_id, "rationale": str(rationale),
            "model_declared_read_claim_ids": read_claim_ids, "edit_count": len(edits),
        })
        outcomes = []
        for raw_edit in edits:
            # Reconstruct between atomic edits so two edits cannot silently
            # overwrite the same version from one stale proposal.
            state = self._state(stage)
            action_id = f"A{len(self._events) + 1:07d}"
            old_id = raw_edit.get("old_claim_id") or None
            old = state["claims"].get(old_id)
            entity = raw_edit.get("entity", old.get("entity", "") if old else "")
            field = raw_edit.get("field", old.get("field", "") if old else "")
            scope = raw_edit.get("scope", old.get("scope", "") if old else "")
            source_ids_raw = raw_edit.get("source_ids", [])
            source_ids = list(dict.fromkeys(source_ids_raw)) if isinstance(source_ids_raw, list) and all(isinstance(s, str) for s in source_ids_raw) else []
            action = {
                "action_id": action_id, "proposal_id": proposal_id, "stage": stage,
                "op": raw_edit.get("op"), "entity": entity, "field": field,
                "value": deepcopy(raw_edit.get("value")), "scope": scope,
                "valid_from": raw_edit.get("valid_from"), "valid_to": raw_edit.get("valid_to"),
                "old_claim_id": old_id, "source_ids": source_ids,
                "quote": raw_edit.get("quote") or "",
                "description": str(raw_edit.get("description") or rationale),
                "model_declared_read_claim_ids": read_claim_ids,
                "raw_edit": deepcopy(raw_edit), "status": "proposed",
                "claim_id": None, "semantic_status": "model_assessed",
                "created_event_sequence": len(self._events) + 1,
            }
            self._append("action_proposed", stage, action)
            comparison_id = f"K{len(self._events) + 1:07d}"
            comparison = {
                "comparison_id": comparison_id, "action_id": action_id, "stage": stage,
                "old_claim_id": old_id, "old_value": deepcopy(old.get("value")) if old else None,
                "incoming_claim": {k: deepcopy(action[k]) for k in ("entity", "field", "value", "scope", "valid_from", "valid_to", "source_ids", "quote")},
                "predicted_relation": str(raw_edit.get("relation") or "unspecified_model_proposal"),
                "description": action["description"],
                "semantic_status": "model_assessed",
                "created_event_sequence": len(self._events) + 1,
            }
            self._append("comparison_recorded", stage, comparison)
            action["comparison_id"] = comparison_id
            status, reason, matches, checks = self._check(action, state, source_ids_raw)
            action.update(status=status, reason=reason, quote_matches=matches,
                          structural_checks=checks, structural_pass=status == "accepted")
            if status == "accepted" and action["op"] in {"create", "update"}:
                claim_id = f"C{len(self._events) + 1:07d}"
                claim = {
                    "claim_id": claim_id, "action_id": action_id, "stage": stage,
                    "entity": entity, "field": field, "value": deepcopy(action["value"]),
                    "scope": scope, "valid_from": action["valid_from"], "valid_to": action["valid_to"],
                    "source_ids": source_ids, "quote": action["quote"],
                    "quote_matches": deepcopy(matches), "description": action["description"],
                    "semantic_status": "model_assessed", "structural_pass": True,
                    "created_event_sequence": len(self._events) + 1,
                }
                self._append("claim_created", stage, claim)
                action["claim_id"] = claim_id
            self._append("action_decided", stage, action)
            outcomes.append({k: deepcopy(action[k]) for k in (
                "action_id", "status", "reason", "claim_id", "comparison_id", "op",
                "structural_pass", "structural_checks", "semantic_status",
            )})
        counts = Counter(item["status"] for item in outcomes)
        return {"proposal_id": proposal_id, "stage": stage, "actions": outcomes,
                "accepted": counts["accepted"], "held": counts["held"], "rejected": counts["rejected"]}

    def _check(self, action: dict, state: dict, raw_source_ids: Any) -> tuple[str, str, list, list]:
        checks: list[dict[str, Any]] = []

        def check(name: str, passed: bool) -> bool:
            checks.append({"check": name, "passed": bool(passed)})
            return bool(passed)

        if not check("allowed_operation", action["op"] in {"create", "update", "retract", "noop"}):
            return "rejected", "unsupported_operation", [], checks
        if not check("entity_and_field", isinstance(action["entity"], str) and bool(action["entity"].strip()) and isinstance(action["field"], str) and bool(action["field"].strip())):
            return "rejected", "missing_entity_or_field", [], checks
        if not check("scope_is_string", isinstance(action["scope"], str)):
            return "rejected", "invalid_scope", [], checks
        if not check("source_ids_are_strings", isinstance(raw_source_ids, list) and all(isinstance(x, str) for x in raw_source_ids)):
            return "rejected", "invalid_source_ids", [], checks
        if not check("sources_visible", all(s in state["sources"] for s in action["source_ids"])):
            return "rejected", "source_not_visible_or_missing", [], checks
        if not check("declared_claim_inputs_visible", all(c in state["claims"] for c in action["model_declared_read_claim_ids"])):
            return "rejected", "claim_input_not_visible_or_missing", [], checks
        old_id = action["old_claim_id"]
        if old_id is not None and not check("target_visible", old_id in state["claims"]):
            return "rejected", "target_claim_not_visible_or_missing", [], checks
        if action["op"] in {"update", "retract"}:
            if not check("existing_target_required", old_id in state["claims"]):
                return "rejected", "target_claim_required", [], checks
            old = state["claims"][old_id]
            if not check("target_is_current", state["heads"].get(self._key(old)) == old_id):
                return "rejected", "stale_target_version", [], checks
            if not check("same_entity_and_field", action["entity"] == old["entity"] and action["field"] == old["field"]):
                return "rejected", "target_entity_or_field_mismatch", [], checks
        target_key = self._key(action)
        if action["op"] == "create" and not check("create_does_not_overwrite", target_key not in state["heads"]):
            return "held", "existing_claim_requires_explicit_update_or_noop", [], checks
        if action["op"] == "update" and target_key in state["heads"] and state["heads"][target_key] != old_id:
            check("new_scope_has_no_other_head", False)
            return "held", "target_scope_conflicts_with_existing_claim", [], checks
        if not check("has_source_reference", bool(action["source_ids"])):
            return "held", "source_reference_required", [], checks
        if not check("has_quote", isinstance(action["quote"], str) and bool(action["quote"].strip())):
            return "held", "source_quote_required", [], checks
        matches = []
        for source_id in action["source_ids"]:
            for match in self._find_quote(state["sources"][source_id]["text"], action["quote"]):
                matches.append({"source_id": source_id, **match})
        if not check("quote_located_in_visible_source", bool(matches)):
            return "rejected", "quote_not_found_in_cited_sources", [], checks
        # This check is intentionally about location only, not entailment.
        return "accepted", "structure_and_quote_location_passed_semantics_unverified", matches, checks

    def ledger(self, stage: int | None = None) -> list[dict[str, Any]]:
        stage = self._current_stage() if stage is None else self._stage(stage)
        state = self._state(stage)
        return [self._claim_view(state["claims"][cid], state)
                for _, cid in sorted(state["heads"].items())]

    @staticmethod
    def _claim_view(claim: dict, state: dict) -> dict[str, Any]:
        view = deepcopy(claim)
        view["status"] = state["statuses"].get(claim["claim_id"], "proposed")
        view["related_action_ids"] = [
            action["action_id"] for action in state["actions"].values()
            if claim["claim_id"] in {action.get("old_claim_id"), action.get("claim_id")}
            or claim["claim_id"] in action.get("model_declared_read_claim_ids", [])
        ]
        return view

    @staticmethod
    def _terms(value: str) -> set[str]:
        terms: set[str] = set()
        for part in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", value.lower()):
            terms.add(part)
            if re.fullmatch(r"[\u4e00-\u9fff]+", part):
                terms.update(part[i:i + 2] for i in range(max(0, len(part) - 1)))
        return terms

    def lookup(self, query: str, stage: int, limit: int = 8) -> dict[str, Any]:
        stage = self._stage(stage)
        limit = max(1, min(int(limit), 50))
        state = self._state(stage)
        terms = self._terms(str(query))
        ranked = []
        for claim in state["claims"].values():
            text = json.dumps({k: claim.get(k) for k in ("entity", "field", "value", "scope", "description", "quote")}, ensure_ascii=False)
            score = len(terms & self._terms(text))
            ranked.append((score, claim["stage"], claim["created_event_sequence"], claim))
        ranked.sort(key=lambda row: row[:3], reverse=True)
        selected = [row for row in ranked if row[0] > 0][:limit]
        fallback = not selected and bool(ranked)
        if fallback:
            selected = ranked[:limit]
        claims = [dict(self._claim_view(row[3], state), retrieval_score=row[0]) for row in selected]
        # A rejected create has no old or produced claim. Search action records
        # independently so it remains discoverable rather than orphaned from
        # all claim-based lookup paths. Score only the stage-filtered public
        # view: inaccessible support must not influence retrieval or its result.
        action_ranked = []
        for action in state["actions"].values():
            view = self._action_view(action, state)
            text = " ".join(str(view.get(key, "")) for key in (
                "entity", "field", "description", "status", "reason", "op",
            ))
            score = len(terms & self._terms(text))
            if score > 0:
                action_ranked.append((score, view["stage"], view["created_event_sequence"], view))
        action_ranked.sort(key=lambda row: row[:3], reverse=True)
        action_views = {
            row[3]["action_id"]: dict(row[3], retrieval_score=row[0])
            for row in action_ranked[:limit]
        }
        for claim in claims:
            for aid in claim["related_action_ids"]:
                if aid not in action_views:
                    action_views[aid] = self._action_view(state["actions"][aid], state)
        return {
            "stage": stage, "query": str(query), "retrieval": "lexical_baseline",
            "fallback_to_recent": fallback, "claims": claims,
            "actions": list(action_views.values()),
        }

    @staticmethod
    def _action_view(action: dict, state: dict) -> dict[str, Any]:
        view = deepcopy(action)
        unavailable_support = any(s not in state["sources"] for s in view.get("source_ids", []))
        view["source_ids"] = [s for s in view.get("source_ids", []) if s in state["sources"]]
        view["model_declared_read_claim_ids"] = [c for c in view.get("model_declared_read_claim_ids", []) if c in state["claims"]]
        if view.get("old_claim_id") not in state["claims"]:
            view["old_claim_id"] = None
        if isinstance(view.get("raw_edit"), dict):
            view["raw_edit"]["source_ids"] = list(view["source_ids"])
            view["raw_edit"]["old_claim_id"] = view.get("old_claim_id")
        if unavailable_support:
            # Keep the original attempted payload in the private append-only
            # log, but do not launder inaccessible source content back through
            # a rejected action or its free-text explanation.
            view.update(value=None, quote="", quote_matches=[], valid_from=None, valid_to=None,
                        description="Proposal details withheld: support was not visible at this stage.",
                        raw_edit={"op": view.get("op"), "details_withheld": True},
                        details_withheld=True)
        return view

    @staticmethod
    def _comparison_view(comparison: dict, state: dict) -> dict[str, Any]:
        view = deepcopy(comparison)
        if view.get("old_claim_id") not in state["claims"]:
            view["old_claim_id"], view["old_value"] = None, None
        incoming = view["incoming_claim"]
        unavailable_support = any(s not in state["sources"] for s in incoming.get("source_ids", []))
        incoming["source_ids"] = [s for s in incoming.get("source_ids", []) if s in state["sources"]]
        if unavailable_support:
            view["incoming_claim"] = {"source_ids": incoming["source_ids"], "details_withheld": True}
            view["description"] = "Comparison details withheld: support was not visible at this stage."
            view["predicted_relation"] = "withheld"
        return view

    def expand(self, action_ids: list[str], stage: int) -> dict[str, Any]:
        stage = self._stage(stage)
        state = self._state(stage)
        selected = [state["actions"][aid] for aid in dict.fromkeys(action_ids) if aid in state["actions"]]
        selected_ids = {a["action_id"] for a in selected}
        claim_ids = set()
        for action in selected:
            claim_ids.update(c for c in [action.get("old_claim_id"), action.get("claim_id"), *action.get("model_declared_read_claim_ids", [])] if c in state["claims"])
        comparisons = []
        for action in selected:
            comparison = state["comparisons"].get(action.get("comparison_id"))
            if comparison:
                comparisons.append(self._comparison_view(comparison, state))
        related = [a["action_id"] for a in state["actions"].values()
                   if a["action_id"] not in selected_ids
                   and (a.get("old_claim_id") in claim_ids or a.get("claim_id") in claim_ids
                        or bool(set(a.get("model_declared_read_claim_ids", [])) & claim_ids))]
        return {
            "stage": stage, "actions": [self._action_view(a, state) for a in selected],
            "comparisons": comparisons,
            "claims": [self._claim_view(state["claims"][cid], state) for cid in sorted(claim_ids)],
            "related_action_ids": related,
            "not_visible_or_missing": [aid for aid in action_ids if aid not in state["actions"]],
        }

    def read_source(self, source_id: str, stage: int, start: int = 0, max_chars: int = 8000) -> dict[str, Any]:
        stage = self._stage(stage)
        source = self._sources.get(source_id)
        if source is None or source["stage"] > stage:
            return {"error": "source_not_visible_or_missing", "source_id": source_id}
        if not isinstance(start, int) or isinstance(start, bool) or start < 0:
            return {"error": "invalid_start", "source_id": source_id}
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
            return {"error": "invalid_max_chars", "source_id": source_id}
        end = min(len(source["text"]), start + min(max_chars, 50000))
        return {
            "source_id": source_id, "path": source["path"], "stage": source["stage"],
            "sha256_text_utf8": source["sha256_text_utf8"], "start_char": start,
            "end_char_exclusive": end, "total_chars": len(source["text"]),
            "text": source["text"][start:end], "truncated": end < len(source["text"]),
        }

    def _graph(self, stage: int) -> dict[str, Any]:
        state = self._state(stage)
        nodes: dict[str, dict[str, Any]] = {}
        edges = []

        def edge(source: str | None, target: str | None, kind: str, basis: str) -> None:
            if source in nodes and target in nodes:
                edges.append({"from": source, "to": target, "kind": kind, "evidence_basis": basis})

        for source_id, source in state["sources"].items():
            nodes[source_id] = {"id": source_id, "kind": "source", **{k: deepcopy(v) for k, v in source.items() if k != "text"}}
        for cid, claim in state["claims"].items():
            nodes[cid] = {"id": cid, "kind": "claim", **self._claim_view(claim, state)}
        for aid, action in state["actions"].items():
            nodes[aid] = {"id": aid, "kind": "action", **self._action_view(action, state)}
        for kid, comparison in state["comparisons"].items():
            view = self._comparison_view(comparison, state)
            nodes[kid] = {"id": kid, "kind": "comparison", **view}
        for cid, claim in state["claims"].items():
            edge(claim["action_id"], cid, "produces", "runtime_observed")
            for source_id in claim["source_ids"]:
                edge(cid, source_id, "claimed_support", "model_declared")
        for aid, action in state["actions"].items():
            edge(action.get("old_claim_id"), aid, "targeted_by", "runtime_observed")
            edge(action.get("comparison_id"), aid, "used_comparison", "model_declared")
            edge(action.get("old_claim_id"), action.get("comparison_id"), "compared_in", "runtime_observed")
            for source_id in action.get("source_ids", []):
                edge(aid, source_id, "claimed_justification", "model_declared")
            for cid in action.get("model_declared_read_claim_ids", []):
                edge(cid, aid, "claimed_used_by", "model_declared")
            if action.get("status") == "accepted" and action["op"] in {"update", "retract"}:
                edge(aid, action.get("old_claim_id"), "supersedes" if action["op"] == "update" else "retracts", "runtime_observed")
        return {"schema_version": "0.1", "stage": stage, "semantic_correctness_verified": False,
                "retrieval_method": "lexical_baseline", "nodes": list(nodes.values()), "edges": edges}

    def export(self) -> dict[str, str]:
        stage = self._current_stage()
        graph_path, ledger_path = self.run_dir / "graph.json", self.run_dir / "ledger.json"
        graph_path.write_text(json.dumps(self._graph(stage), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        ledger_path.write_text(json.dumps({"stage": stage, "claims": self.ledger(stage), "semantic_correctness_verified": False}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        typed = {
            "sources.jsonl": "source_registered", "claims.jsonl": "claim_created",
            "comparisons.jsonl": "comparison_recorded", "action_events.jsonl": None,
        }
        paths = {"graph": str(graph_path), "ledger": str(ledger_path), "events": str(self.event_path)}
        for filename, kind in typed.items():
            path = self.run_dir / filename
            records = [event for event in self._events
                       if event["stage"] <= stage and (event["kind"] == kind if kind else event["kind"] in {"action_proposed", "action_decided"})]
            path.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in records), encoding="utf-8")
            paths[filename.removesuffix(".jsonl")] = str(path)
        return paths

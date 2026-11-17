"""M03 local-state proposals, grouped deltas and receipts in the existing DB."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from typing import TYPE_CHECKING

from .ingestion_store import IngestionStore, _now
from .maintenance_delta import delta_label, plan_changes, resolve_dispositions
from .temporal_dag import TemporalDAG

if TYPE_CHECKING:
    from .maintenance_contract import MaintenanceProposal


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _event(db, kind, request_id):
    row = db.execute("SELECT * FROM event WHERE kind=? AND json_extract(payload,'$.request_id')=? "
                     "ORDER BY seq DESC LIMIT 1", (kind, request_id)).fetchone()
    return TemporalDAG._unpack(db, row) if row else None


def _receipt(event):
    payload = deepcopy(event["payload"])
    payload.update(receipt_id=event["id"], committed=True)
    payload["result_ledger_snapshot_id"] = (event["id"] if payload["changed"]
                                             else payload["base_ledger_snapshot_id"])
    if payload.get("ledger_snapshot"):
        payload["ledger_snapshot"]["ledger_snapshot_id"] = event["id"]
    payload.setdefault("session_complete", True)
    return payload


class MaintenanceStore:
    def __init__(self, workspace):
        self.ingestion = IngestionStore(workspace)
        self.dag = self.ingestion.dag
        self.workspace = self.ingestion.workspace

    @staticmethod
    def request_scope(batch, task_card):
        task_version = ((task_card or {}).get("created_record_id") or
                        (_hash(task_card) if task_card is not None else None))
        return {"task_version": task_version,
                "base_ledger_snapshot_id": batch["ledger_snapshot"]["ledger_snapshot_id"],
                "visible_as_of": batch["input_scope"]["visible_as_of"],
                "input_claim_ids": sorted(batch["input_scope"]["input_claim_ids"])}

    @staticmethod
    def _current(db, initial):
        row = db.execute("SELECT * FROM event WHERE kind='m03_maintenance' "
                         "AND json_extract(payload,'$.changed')=1 ORDER BY seq DESC LIMIT 1").fetchone()
        if row:
            event = TemporalDAG._unpack(db, row)
            return {**event["payload"]["ledger_snapshot"], "ledger_snapshot_id": event["id"]}
        return initial

    def current_ledger_state(self):
        initial = self.ingestion.initial_ledger_state()
        with self.dag._db() as db:
            return self._current(db, initial)

    def ledger_state(self, snapshot_id):
        initial = self.ingestion.initial_ledger_state()
        if initial and snapshot_id == initial["ledger_snapshot_id"]:
            return initial
        event = self.dag.get(snapshot_id)
        if event["kind"] != "m03_maintenance" or not event["payload"]["changed"]:
            raise ValueError("ID does not identify an actual ledger version")
        return {**event["payload"]["ledger_snapshot"], "ledger_snapshot_id": snapshot_id}

    @staticmethod
    def _request_key(scope, previous_receipt_id):
        # Keep pre-refactor request identities readable and reusable.
        return _hash(scope if previous_receipt_id is None else
                     {**scope, "previous_receipt_id": previous_receipt_id})

    def find_request(self, original_batch, task_card, *, previous_receipt_id=None):
        key = self._request_key(self.request_scope(original_batch, task_card), previous_receipt_id)
        with self.dag._db() as db:
            row = db.execute("SELECT * FROM event WHERE kind='m03_request' "
                             "AND json_extract(payload,'$.request_key')=? ORDER BY seq LIMIT 1", (key,)).fetchone()
            if row:
                event = TemporalDAG._unpack(db, row)
                return {**event["payload"], "request_id": event["id"], "reused": True}
        return None

    def create_request(self, original_batch, execution_batch, task_card, *, settings=None,
                       previous_receipt_id=None):
        scope = self.request_scope(original_batch, task_card)
        key = self._request_key(scope, previous_receipt_id)
        execution_scope = {k: v for k, v in self.request_scope(execution_batch, task_card).items()
                           if k != "task_version"}
        with self.dag._db(write=True) as db:
            existing = db.execute("SELECT * FROM event WHERE kind='m03_request' "
                                  "AND json_extract(payload,'$.request_key')=? ORDER BY seq LIMIT 1", (key,)).fetchone()
            if existing:
                event = TemporalDAG._unpack(db, existing)
                return {**event["payload"], "request_id": event["id"], "reused": True}
            if scope["input_claim_ids"] != execution_scope["input_claim_ids"]:
                raise ValueError("Re-pairing must preserve the original request input IDs")
            protected_targets = set()
            repair_scope = None
            if previous_receipt_id:
                previous = TemporalDAG._get(db, previous_receipt_id)
                if previous["kind"] != "m03_maintenance":
                    raise ValueError("Correction must reference a maintenance receipt")
                prior = previous["payload"]
                retryable = prior.get("retryable_claim_ids", prior["unhandled_claim_ids"])
                if not scope["input_claim_ids"] or not set(scope["input_claim_ids"]) <= set(retryable):
                    raise ValueError("Correction inputs must be limited to the prior receipt's unexecuted inputs")
                failed_groups = [g for g in prior.get("change_set_results", [])
                                 if g["status"] == "not_executed"]
                repair_scope = {
                    "failed_change_sets": failed_groups,
                    "state_input_claim_ids": sorted({cid for g in failed_groups for cid in g["input_claim_ids"]}),
                    "disposition_input_claim_ids": sorted({d["input_claim_id"] for d in prior.get("dispositions", [])
                                                           if d.get("errors") or d.get("failed_change_set_ids")}),
                    "successful_change_sets": [], "context_claim_ids": [],
                }
                while prior:
                    protected_targets.update(prior.get("successful_target_ids", []))
                    repair_scope["successful_change_sets"].extend(
                        {**g, "receipt_id": previous["id"]} for g in prior.get("change_set_results", [])
                        if g["status"] == "executed")
                    repair_scope["context_claim_ids"].extend(prior["execution_scope"]["input_claim_ids"])
                    ancestor = prior.get("previous_receipt_id")
                    previous = TemporalDAG._get(db, ancestor) if ancestor else None
                    prior = previous["payload"] if previous else None
                repair_scope["context_claim_ids"] = sorted(set(repair_scope["context_claim_ids"]))
            payload = {"recorded_at": _now(), "request_key": key,
                       "request_scope": scope, "execution_scope": execution_scope,
                       "original_batch_id": original_batch["batch_id"],
                       "execution_batch_id": execution_batch["batch_id"],
                       "task_card": task_card, "settings": settings or {},
                       "previous_receipt_id": previous_receipt_id,
                       "repair_scope": repair_scope,
                       "protected_target_ids": sorted(protected_targets)}
            event = TemporalDAG._append(db, "m03_request", payload,
                                        [original_batch["batch_id"], execution_batch["batch_id"],
                                         *([previous_receipt_id] if previous_receipt_id else [])])
            return {**payload, "request_id": event["id"], "reused": False}

    def request(self, request_id):
        event = self.dag.get(request_id)
        if event["kind"] != "m03_request":
            raise ValueError("ID does not identify a maintenance request")
        return {**event["payload"], "request_id": request_id}

    def receipt(self, receipt_id):
        event = self.dag.get(receipt_id)
        if event["kind"] != "m03_maintenance":
            raise ValueError("ID does not identify a maintenance receipt")
        return _receipt(event)

    def commit(self, request_id, proposal: MaintenanceProposal, *, provided_context_refs=None,
               allowed_claim_ids=None, read_log=None, protocol_records=None):
        """Plan every group first, then commit independent results and one receipt."""
        initial = self.ingestion.initial_ledger_state()
        with self.dag._db(write=True) as db:
            previous = _event(db, "m03_maintenance", request_id)
            if previous:
                return {**_receipt(previous), "reused": True}
            request_event = TemporalDAG._get(db, request_id)
            if request_event["kind"] != "m03_request":
                raise ValueError("ID does not identify a maintenance request")
            request = request_event["payload"]
            if proposal.previous_receipt_id != request.get("previous_receipt_id"):
                raise ValueError("Proposal previous_receipt_id must match its correction request")
            if _event(db, "m03_session_result", request_id):
                return {"committed": False, "request_id": request_id,
                        "error": "This request already ended without a committed result"}
            scope = request["execution_scope"]
            current = self._current(db, initial)
            if current is None or current["ledger_snapshot_id"] != scope["base_ledger_snapshot_id"]:
                return {"committed": False, "request_id": request_id, "needs_repairing": True,
                        "error": "The base ledger changed during execution; create a newly paired request"}
            batch = TemporalDAG._get(db, request["execution_batch_id"])["payload"]
            cutoff = scope["visible_as_of"]
            all_claims = {r["record_id"]: r for r in self.ingestion._rows(db, "m01_source_claim", cutoff)}
            allowed = set(batch["materials"]["claims"] if allowed_claim_ids is None else allowed_claim_ids)
            if not allowed <= all_claims.keys():
                raise ValueError("Provided claims must be visible at the execution cutoff")
            claims = {cid: all_claims[cid] for cid in allowed}
            direct_refs = {r["evidence_ref_id"] for claim in claims.values() for r in claim["evidence_refs"]}
            direct_evidence = {r["evidence_ref_id"]: r for claim in claims.values() for r in claim["evidence_refs"]}
            contexts = provided_context_refs or {}
            entity_rows = self.ingestion._entities(db, cutoff)
            entities = {e["entity_id"]: e for e in entity_rows}
            aliases = self.ingestion._qualifier_alias_index(entity_rows)
            items = {i["ledger_item_id"]: deepcopy(i) for i in current["items"]}
            inputs = set(scope["input_claim_ids"])
            from .maintenance_contract import ModelMaintenanceProposal
            from .maintenance_delta import compile_proposal
            model_proposal = asdict(proposal) if isinstance(proposal, ModelMaintenanceProposal) else None
            if model_proposal is not None:
                proposal = compile_proposal(proposal, items=items, claims=claims, entities=entities,
                    base_ledger_snapshot_id=current["ledger_snapshot_id"])
            repair_scope = request.get("repair_scope")
            plans = plan_changes(proposal, items=items, input_claim_ids=inputs,
                allowed_claims=claims, direct_refs=direct_refs, context_refs=set(contexts),
                entities=entities, cutoff=cutoff,
                semantics=lambda entity_id, claim: self.ingestion._claim_semantics(entity_id, claim, aliases),
                protected_targets=request.get("protected_target_ids", []),
                repair_scope=repair_scope)
            deltas, group_results, used_context_ids, successful_targets = [], [], set(), set()
            for group in plans:
                result = {key: value for key, value in group.items() if key != "states"}
                result.update(status="not_executed" if group["errors"] else "executed", delta_indices=[])
                if not group["errors"]:
                    for state in group["states"]:
                        before, after = state["before"], state["after"]
                        target = state["target_item_id"]
                        if target is None:
                            identity = TemporalDAG._append(db, "m03_item", {
                                "request_id": request_id, "change_set_id": group["change_set_id"]}, [request_id])
                            target = identity["id"]
                            after = {"ledger_item_id": target, "introduced_record_id": target, **after}
                        if after is None:
                            del items[target]
                        else:
                            items[target] = after
                        successful_targets.add(target)
                        used_context_ids.update(state["state"]["context_ref_ids"])
                        changed_fields = {key: {"before": (before or {}).get(key), "after": (after or {}).get(key)}
                            for key in sorted(set(before or {}) | set(after or {}))
                            if (before or {}).get(key) != (after or {}).get(key)}
                        result["delta_indices"].append(len(deltas))
                        deltas.append({"change_set_id": group["change_set_id"],
                            "target_item_id": target, "action": delta_label(before, after),
                            "input_claim_ids": group["input_claim_ids"], "changed": bool(changed_fields),
                            "changed_fields": changed_fields, "before": before, "after": after,
                            **{key: value for key, value in state["state"].items()
                               if key not in {"target_item_id", "after"}}})
                group_results.append(result)
            dispositions = resolve_dispositions(proposal, group_results, inputs,
                prior_group_results=(repair_scope or {}).get("successful_change_sets", []))
            valid = [d for d in dispositions if not d["errors"]]
            handled = {d["input_claim_id"] for d in valid if d["handled"]}
            deferred = [{"input_claim_id": d["input_claim_id"], "pending_question": question,
                         "handled": d["handled"], "change_set_ids": d["change_set_ids"]}
                        for d in valid for question in d["pending_questions"]]
            deferred_ids = {d["input_claim_id"] for d in deferred}
            failed_inputs = {cid for group in group_results if group["status"] == "not_executed"
                             for cid in group["input_claim_ids"] if cid in inputs}
            unhandled = (inputs - handled - deferred_ids) | failed_inputs
            retryable = unhandled | failed_inputs
            next_items = list(items.values())
            changed = next_items != current["items"]
            snapshot = None
            if changed:
                snapshot = {"parent_ledger_snapshot_id": current["ledger_snapshot_id"],
                            "initial_arrival_id": current.get("initial_arrival_id"),
                            "source_cutoff_event_seq": cutoff, "normalization_as_of": cutoff,
                            "included_claim_ids": list(dict.fromkeys(
                                cid for item in next_items for cid in item["assertion_ids"])),
                            "items": next_items}
            payload = {"recorded_at": _now(), "request_id": request_id,
                       "contract_version": "direct-state-v2" if model_proposal is not None else "local-state-v1",
                       "proposal": asdict(proposal), "model_proposal": model_proposal,
                       "protocol_records": protocol_records or [],
                       "original_batch_id": request["original_batch_id"],
                       "execution_batch_id": request["execution_batch_id"],
                       "request_scope": request["request_scope"], "execution_scope": scope,
                       "previous_receipt_id": request.get("previous_receipt_id"),
                       "base_ledger_snapshot_id": current["ledger_snapshot_id"],
                       "changed": changed, "ledger_snapshot": snapshot,
                       "change_set_results": group_results, "deltas": deltas,
                       "dispositions": dispositions,
                       "handled_claim_ids": sorted(handled), "deferred": deferred,
                       "unhandled_claim_ids": sorted(unhandled), "retryable_claim_ids": sorted(retryable),
                       "successful_target_ids": sorted(successful_targets),
                       "session_complete": (all(group["status"] == "executed" for group in group_results)
                                            and not any(d["errors"] for d in dispositions)),
                       "read_log": read_log or [],
                       "used_evidence_refs": {rid: direct_evidence[rid] for rid in sorted({
                           rid for delta in deltas for rid in delta.get("evidence_ref_ids", [])})},
                       "used_context_refs": {rid: contexts[rid] for rid in sorted(used_context_ids)}}
            parents = [request_id, *([request["previous_receipt_id"]] if request.get("previous_receipt_id") else [])]
            event = TemporalDAG._append(db, "m03_maintenance", payload, parents)
            return {**_receipt(event), "reused": False}

    def finish_session(self, request_id, result):
        """Append observed DSH/model accounting once, also for an uncommitted session."""
        self.request(request_id)
        with self.dag._db(write=True) as db:
            previous = _event(db, "m03_session_result", request_id)
            if previous:
                return {**previous["payload"], "session_result_id": previous["id"]}
            event = TemporalDAG._append(db, "m03_session_result", {
                "recorded_at": _now(), "request_id": request_id, **deepcopy(result)}, [request_id])
            return {**event["payload"], "session_result_id": event["id"]}

    def result(self, request_id):
        request = self.request(request_id)
        with self.dag._db() as db:
            receipt_event = _event(db, "m03_maintenance", request_id)
            session_event = _event(db, "m03_session_result", request_id)
        receipt = _receipt(receipt_event) if receipt_event else None
        session = ({**session_event["payload"], "session_result_id": session_event["id"]}
                   if session_event else None)
        version = (receipt["result_ledger_snapshot_id"] if receipt
                   else request["execution_scope"]["base_ledger_snapshot_id"])
        inputs = request["execution_scope"]["input_claim_ids"]
        pending = {"handled_claim_ids": receipt["handled_claim_ids"] if receipt else [],
                   "deferred": receipt["deferred"] if receipt else [],
                   "dispositions": receipt.get("dispositions", []) if receipt else [],
                   "unhandled_claim_ids": (receipt["unhandled_claim_ids"] if receipt else inputs if session else []),
                   "unprocessed_claim_ids": inputs if not receipt and not session else []}
        return {"request": request, "receipt": receipt, "session": session,
                "ledger_snapshot": self.ledger_state(version), "pending": pending}

    def pending_inputs(self):
        """Project candidate coverage and the latest observed result for each input."""
        statuses, requests, committed = {}, {}, set()
        for event in self.dag.nodes():
            payload = event["payload"]
            if event["kind"] == "m02_candidate_batch":
                for cid in payload["input_scope"]["input_claim_ids"]:
                    statuses.setdefault(cid, {"status": "no_maintenance_result", "claim_id": cid})
            elif event["kind"] == "m03_request":
                requests[event["id"]] = payload
            elif event["kind"] == "m03_maintenance":
                rid = payload["request_id"]
                committed.add(rid)
                handled = set(payload["handled_claim_ids"])
                unhandled = set(payload["unhandled_claim_ids"])
                questions = {}
                for entry in payload["deferred"]:
                    questions.setdefault(entry["input_claim_id"], []).append(entry["pending_question"])
                for cid in requests[rid]["execution_scope"]["input_claim_ids"]:
                    pending_questions = list(dict.fromkeys(questions.get(cid, [])))
                    status = ("unhandled" if cid in unhandled else "handled" if cid in handled
                              else "deferred" if pending_questions else "unhandled")
                    statuses[cid] = {"status": status, "claim_id": cid, "request_id": rid,
                                     "handled": cid in handled, "unhandled": cid in unhandled,
                                     "pending_questions": pending_questions}
                    if pending_questions:
                        statuses[cid]["pending_question"] = "；".join(pending_questions)
            elif event["kind"] == "m03_session_result":
                rid = payload["request_id"]
                if rid not in committed:
                    for cid in requests[rid]["execution_scope"]["input_claim_ids"]:
                        statuses[cid] = {"status": "unhandled", "claim_id": cid, "request_id": rid,
                                         "reason": payload.get("error", "Session ended without a commit")}
        # Handled work and pending questions are orthogonal, so an input may be
        # visible in both projections without creating another state machine.
        return {"no_maintenance_result": [e for e in statuses.values() if e["status"] == "no_maintenance_result"],
                "handled": [e for e in statuses.values() if e.get("handled", e["status"] == "handled")],
                "deferred": [e for e in statuses.values() if e.get("pending_questions") or e["status"] == "deferred"],
                "unhandled": [e for e in statuses.values() if e.get("unhandled", e["status"] == "unhandled")]}


def current_ledger_state(workspace):
    return MaintenanceStore(workspace).current_ledger_state()

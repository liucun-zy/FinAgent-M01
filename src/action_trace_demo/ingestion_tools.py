"""Trusted M01 save adapter; legacy tools remain readable for historical replay."""
from __future__ import annotations

import json
from pathlib import Path

from .dsh_bridge import write_json
from .extraction_contract import decode_tool


class IngestionTools:
    def __init__(self, root, context):
        self.root = Path(root)
        self.context = context
        self.group = json.loads((self.root / context.get("group_file", "ingest_group.json")).read_text())
        if self.group["window_group_id"] != context["request_id"]:
            raise ValueError("Extraction group does not match the bound request")
        self.config = self.group.get("config", {})
        self.state_path = self.root / "ingest_tool_state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {
            "context_rounds": 0,
            "provided_evidence_refs": self.group["evidence_refs"],
            "candidate_entity_ids": [e["entity_id"] for e in self.group.get("entity_candidates", [])],
            "committed": False,
        }

    def catalog(self):
        if self.group.get("source_packet"):
            from .source_extraction import tool_specs
        else:
            from .extraction import tool_specs
        return tool_specs()

    def host_settings(self):
        if self.group.get("source_packet"):
            from .source_extraction import SYSTEM_PROMPT as INGEST_SYSTEM_PROMPT
        else:
            from .extraction import INGEST_SYSTEM_PROMPT
        return {
            "system_prompt": INGEST_SYSTEM_PROMPT,
            "max_model_requests": self.config.get("max_model_requests_per_group", 2),
        }

    def _store(self):
        from .ingestion_store import IngestionStore
        return IngestionStore(self.context["ingestion_root"])

    def call(self, name, args):
        if self.state["committed"]:
            raise ValueError("This extraction group is already saved")
        if self.group.get("source_packet"):
            return self._save_source_units(name, args)
        request = decode_tool(name, args)
        if name == "read_context":
            from .evidence_windows import read_context
            if self.state["context_rounds"] >= self.config.get("max_context_rounds_per_group", 1):
                raise ValueError("Context reading budget exhausted; save supported claims and unresolved items")
            provided = self.state["provided_evidence_refs"]
            seen_blocks = {r["block_id"] for r in provided
                           if r["evidence_ref_id"] == "EV_" + r["block_id"]}
            result = read_context(
                self.group["blocks"], self.group["windows"],
                [r.model_dump(exclude_unset=True) for r in request.requests], seen_blocks,
                extra_token_budget=self.config.get("context_extra_max_tokens", 2000),
            )
            refs = {r["evidence_ref_id"]: r for r in provided}
            fresh = [r for r in result["evidence_refs"] if r["evidence_ref_id"] not in refs]
            refs.update((r["evidence_ref_id"], r) for r in fresh)
            self.state["provided_evidence_refs"] = list(refs.values())
            self.state["context_rounds"] += 1
            write_json(self.state_path, self.state)
            return {**result, "evidence_refs": fresh, "context_rounds": self.state["context_rounds"]}
        if name == "lookup_entities":
            entities = self._store().lookup_entities(request.mentions)
            ids = set(self.state["candidate_entity_ids"])
            ids.update(e["entity_id"] for e in entities)
            self.state["candidate_entity_ids"] = sorted(ids)
            write_json(self.state_path, self.state)
            return {"entities": entities}
        if name == "save_extraction":
            if request.window_group_id != self.group["window_group_id"]:
                raise ValueError("Choose the current extraction group")
            result = {key: getattr(request, key) for key in ("entities", "claims", "processed_window_ids", "unresolved")}
            receipt = self._store().stage_extraction(
                self.group["arrival_id"], self.group["window_group_id"], result,
                self.state["provided_evidence_refs"], self.group["group_order"],
                allowed_window_ids=[w["window_id"] for w in self.group["windows"]],
                candidate_entity_ids=self.state["candidate_entity_ids"],
                extraction_profile=self.group.get("extraction_profile"),
                retry_claim_indices=request.retry_claim_indices,
            )
            self.state["committed"] = receipt["session_complete"]
            self.state["receipt"] = receipt
            write_json(self.state_path, self.state)
            return receipt
        raise ValueError("Unknown ingestion tool")

    def _save_source_units(self, name, args):
        from .source_extraction import (CONTRACT_VERSION, SaveEnvelope, compile_unit,
                                        _validation_message)
        if name != "save_extraction":
            raise ValueError("Source extraction exposes only save_extraction")
        envelope = SaveEnvelope.model_validate(args)
        units = self.group["source_packet"]["units"]
        unit_receipts = self.state.setdefault("unit_receipts", {})
        declarations = self.state.setdefault("declared_entities", {})
        errors = []
        store = self._store()
        order = {short: index for index, short in enumerate(units)}
        for index, raw in enumerate(envelope.results):
            short = raw.get("unit_id") if isinstance(raw, dict) and isinstance(raw.get("unit_id"), str) else None
            if short in unit_receipts and unit_receipts[short].get("session_complete"):
                continue
            try:
                compiled = compile_unit(raw, self.group, declarations.get(short, []))
                unit = compiled["unit"]
                declarations[short] = compiled["declared_entities"]
                receipt = store.stage_extraction(
                    self.group["arrival_id"], self.group["window_group_id"] + "_" + unit["unit_id"],
                    compiled["result"], compiled["provided_evidence_refs"],
                    self.group["group_order"] + order[short] / 10000,
                    allowed_window_ids=[unit["window"]["window_id"]], candidate_entity_ids=[],
                    extraction_profile=self.group.get("extraction_profile"),
                    retry_claim_indices=compiled["retry_indices"],
                    prevalidation_errors=compiled["prevalidation_errors"],
                    proposal_metadata={"contract_version": CONTRACT_VERSION,
                                       "source_unit_id": unit["unit_id"], "proposal": raw,
                                       "effective_entity_declarations": compiled["declared_entities"]},
                )
                unit_receipts[short] = {**receipt, "unit_id": short,
                                        "source_unit_id": unit["unit_id"],
                                        "empty_reason": raw.get("empty_reason") if not raw.get("facts") else None,
                                        "compiler_warnings": compiled["warnings"]}
            except ValueError as error:
                errors.append({"result_index": index, "unit_id": short,
                               "error": _validation_message(error)})
        pending = [short for short in units if not unit_receipts.get(short, {}).get("session_complete")]
        accepted = sum(len(receipt.get("accepted_claim_indices", [])) for receipt in unit_receipts.values())
        complete = not pending
        receipt = {
            "ok": not errors, "committed": bool(accepted or any(
                r.get("session_complete") for r in unit_receipts.values())), "session_complete": complete,
            "status": "staged" if complete else "partially_staged",
            "source_claim_count": accepted, "processed_window_count": len(units) - len(pending),
            "pending_unit_ids": [units[short]["unit_id"] for short in pending],
            "pending_units": pending, "unit_receipts": list(unit_receipts.values()), "errors": errors,
        }
        self.state["receipt"] = receipt
        self.state["committed"] = complete
        write_json(self.state_path, self.state)
        return receipt

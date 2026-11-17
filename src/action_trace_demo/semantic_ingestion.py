"""Source organization through the existing DSH and event store.

These are bounded semantic requests, not another extraction/retry loop. Sources
remain independently saved when either request fails.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from .dsh_bridge import write_json

from .semantic_schema import PROJECTION_VERSION


def _rows(store, kind):
    with store.dag._db() as db:
        return store._rows(db, "m01_" + kind)


def append_once(store, kind, request_id, payload):
    with store.dag._db(write=True) as db:
        old = [r for r in store._rows(db, "m01_" + kind) if r.get("request_id") == request_id]
        return old[-1] if old else store._append(db, kind, {"request_id": request_id, **payload})


def current_schema(store, as_of=None):
    from .semantic_schema import make_schema
    with store.dag._db() as db:
        rows = store._rows(db, "m01_task_schema", as_of)
    return rows[-1]["schema"] if rows else make_schema({"concepts": []})


class SemanticTools:
    """One typed terminal tool, executed by the same DSH host and budget."""
    def __init__(self, root, context):
        self.root = Path(root)
        self.context = context
        self.request = json.loads((self.root / "semantic_request.json").read_text())

    def catalog(self):
        from .semantic_schema import SchemaProposal, AlignmentProposal, OrganizationProposal, IdentityProposal
        cls = {"schema": SchemaProposal, "organization": OrganizationProposal, "identity": IdentityProposal}.get(self.request["kind"], AlignmentProposal)
        name = "save_task_schema" if self.request["kind"] == "schema" else "save_semantic_alignment"
        return [{"name": name, "description": "Save the bounded semantic organization result; no source facts are rewritten.",
                 "parameters": cls.model_json_schema()}]

    def host_settings(self):
        return {"system_prompt": self.request["system_prompt"],
                "max_model_requests": self.request.get("max_model_requests", 1)}

    def call(self, name, args):
        from .semantic_schema import SchemaProposal, AlignmentProposal, OrganizationProposal, IdentityProposal, compile_identity
        cls = {"schema": SchemaProposal, "organization": OrganizationProposal, "identity": IdentityProposal}.get(self.request["kind"], AlignmentProposal)
        if name != self.catalog()[0]["name"]:
            raise ValueError("Tool is outside this semantic request")
        payload = cls.model_validate(args).model_dump(mode="json")
        path = self.root / "semantic_result.json"
        if path.exists() and json.loads(path.read_text()) != payload:
            raise ValueError("This semantic request has already saved its result")
        if self.request["kind"] == "identity":
            context = self.request["validation"]
            compiled = compile_identity(payload, context["job"], context["material"], context["schema"])
            write_json(self.root / "semantic_bindings.json", compiled)
        write_json(path, payload)
        return {"committed": True, "session_complete": True, "result_file": str(path)}


def _request(store, arrival, kind, material, model_config, settings, system_prompt, request_id, validation=None):
    from .ingestion import DEMO
    from .evidence_windows import request_input_estimate
    root = store.workspace.resolve()
    directory = root / "semantic" / request_id
    invocation = directory / "invocation.json"
    if invocation.exists():
        old = json.loads(invocation.read_text())
        output = directory / ("semantic_bindings.json" if kind == "identity" else "semantic_result.json")
        return (json.loads(output.read_text()) if output.exists() else None), old
    directory.mkdir(parents=True, exist_ok=True)
    max_requests = settings.get("semantic_max_requests_per_batch", 2) if kind in ("organization", "identity") else 1
    request = {"kind": kind, "material": material, "system_prompt": system_prompt,
               "max_model_requests": max_requests}
    if validation is not None:
        request["validation"] = validation
    write_json(directory / "semantic_request.json", request)
    write_json(directory / "task_context.json", {"mode": "m01_semantic", "request_id": request_id})
    prompt = json.dumps(material, ensure_ascii=False, separators=(",", ":"))
    (directory / "prompt.txt").write_text(prompt)
    specs = SemanticTools(directory, {}).catalog()
    estimate = request_input_estimate(system_prompt, prompt, specs)
    if estimate > settings["batch_input_budget_tokens"]:
        receipt = {"committed": False, "steps": 0, "error": "Semantic input exceeds fixed input budget",
                   "estimated_input_tokens": estimate}
        write_json(invocation, receipt)
        return None, receipt
    private = json.loads(Path(model_config).read_text())
    private["input_budget_tokens"] = settings["batch_input_budget_tokens"]
    private["attempt_tags"] = {"request_kind": "alignment" if kind in ("organization", "identity") else kind,
                               "semantic_request": request_id, "semantic_arrival": arrival["arrival_id"]}
    private["attempt_limits"] = {"semantic_request": max_requests}
    if kind in ("organization", "identity"):
        private["attempt_limits"]["semantic_arrival"] = settings.get("semantic_max_transport_attempts_per_arrival", 4)
    private_path = root / ".private" / (request_id + ".json")
    fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(private, stream)
    audit = root / ".dsh" / request_id
    command = [str(DEMO / "dsh_host/run"), "--workspace", str(directory), "--prompt-file", str(directory / "prompt.txt"),
               "--max-calls", str(max_requests), "--audit-dir", str(audit), "--config", str(private_path)]
    started = time.perf_counter()
    with (directory / "stdout.json").open("w") as out, (directory / "stderr.log").open("w") as err:
        process = subprocess.run(command, stdout=out, stderr=err)
    try:
        receipt = json.loads((directory / "stdout.json").read_text())
    except ValueError:
        receipt = {"committed": False, "error": "No semantic receipt; see stderr.log"}
    receipt.update(exit_code=process.returncode, elapsed_seconds=time.perf_counter() - started,
                   audit_dir=str(audit), request_kind=kind, estimated_input_tokens=estimate)
    write_json(invocation, receipt)
    output = directory / ("semantic_bindings.json" if kind == "identity" else "semantic_result.json")
    return (json.loads(output.read_text()) if output.exists() else None), receipt


def initialize_schema(store, arrival, parsed, config, model_config):
    """Import a public environment schema; source extraction never waits on one."""
    from .semantic_schema import make_schema
    rows = _rows(store, "task_schema")
    if rows:
        return rows[-1]["schema"], []
    formal = config.get("task_schema")
    request_id = "schema_" + arrival["arrival_id"]
    if formal is not None:
        schema = make_schema(formal)
        append_once(store, "task_schema", request_id, {"arrival_id": arrival["arrival_id"], "schema": schema,
                    "mode": "formal_import", "selected_evidence_ref_ids": []})
        return schema, []
    return make_schema({"concepts": []}), []


def resolve_arrival(store, arrival, config, model_config):
    from .semantic_schema import (prepare_resolution, validate_resolution, identity_plan,
                                  register_source_concepts, IDENTITY_SYSTEM)
    exported = store.export()
    merged = {r["extraction_run_id"] for r in _rows(store, "extraction_merged")}
    groups = sorted([g for g in exported["extractions"] if g["arrival_id"] == arrival["arrival_id"]
                     and g["record_id"] not in merged], key=lambda g: (g["group_order"], g["seq"]))
    if not groups:
        return []
    signature = hashlib.sha256(json.dumps([g["record_id"] for g in groups]).encode()).hexdigest()[:12]
    request_id = "alignment_" + arrival["arrival_id"] + "_" + signature
    if any(r["request_id"] == request_id for r in _rows(store, "semantic_resolution")):
        return []
    schema = current_schema(store)
    policy = config.get("semantic", {})
    material = prepare_resolution(groups, exported["entities"], schema,
        candidate_limit=policy.get("candidate_limit", 12))
    exact_schema = register_source_concepts(material, schema)
    result = validate_resolution({}, material, exact_schema)
    settings = {**config["ingestion"],
                "semantic_max_requests_per_batch": policy.get("max_requests_per_batch", 2),
                "semantic_max_transport_attempts_per_arrival": policy.get("max_transport_attempts_per_arrival", 4)}
    max_batches = policy.get("max_batches_per_arrival", 4)
    jobs, pending = identity_plan(material, exact_schema, settings["batch_input_budget_tokens"], max_batches=max_batches)
    receipts, submitted = [], []
    for index, job in enumerate(jobs):
        payload, receipt = _request(store, arrival, "identity", job["model_input"], model_config,
                                    settings, IDENTITY_SYSTEM, request_id + f"_B{index:03d}",
                                    validation={"job": job, "material": material, "schema": result["schema"]})
        receipts.append(receipt)
        submitted.append({"batch_index": index, "handles": job["handles"], "receipt": receipt})
        if payload is None:
            pending.append({"kind": "batch", "index": index, "reason": receipt.get("error", "organization unavailable")})
            continue
        applied = payload
        result["schema"] = applied["schema"]
        for mid in job["handles"]["focus_mentions"]:
            result["entity_bindings"][mid] = applied["entity_bindings"][mid]
        # Newly defined exact keys can resolve remaining fields in this batch;
        # a later batch must not overwrite an earlier explicit field decision.
        for oid, binding in applied["concept_bindings"].items():
            result["concept_bindings"].setdefault(oid, binding)
        for entries in job["handles"]["fields"].values():
            for occurrence in entries:
                oid = occurrence["occurrence_id"]
                if oid in applied["concept_bindings"]:
                    result["concept_bindings"][oid] = applied["concept_bindings"][oid]
        result["alias_mentions"].extend(applied["alias_mentions"])
        result["warnings"].extend(applied["warnings"])
    result["unresolved_concepts"] = [o for o in material["concept_occurrences"]
                                     if not result["concept_bindings"].get(o["occurrence_id"])]
    result["pending_batches"] = pending
    remaining = sum(b["decision"] == "unresolved" for b in result["entity_bindings"].values())
    remaining += len(result["unresolved_concepts"])
    record = append_once(store, "semantic_resolution", request_id, {"arrival_id": arrival["arrival_id"],
        "input_extraction_ids": [g["record_id"] for g in groups], "material": material,
        "result": result, "receipt": receipts[0] if receipts else None, "organization_batches": submitted,
        "status": "partial" if remaining or pending else "bindings_applied", "unresolved_count": remaining})
    if result["schema"]["version_id"] != schema["version_id"]:
        append_once(store, "task_schema", request_id, {"arrival_id": arrival["arrival_id"],
                    "schema": result["schema"], "mode": "batch_extension", "resolution_record_id": record["record_id"]})
    return receipts

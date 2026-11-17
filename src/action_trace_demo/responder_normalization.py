"""Normalize one observed redundant JSON suffix without another model call.

Only a complete, schema-valid first object followed by exactly `]}` is eligible.
This writes a provenance-bound structured cache, never a response receipt/public
file, and does not advance the Candidate. Original audit bytes remain untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re

import jsonschema

from .benchmark import _inside
from .responder import ResponderBridge, _digest


SYSTEM_SUFFIX = "\nReturn only one valid JSON object matching the supplied JSON schema."


class NormalizationError(ValueError):
    pass


def _read(path: Path) -> tuple[dict, bytes]:
    if path.is_symlink() or not path.is_file(): raise NormalizationError("Missing or symlink audit input")
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict): raise NormalizationError("Expected audit object")
    return value, raw


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _model_file(model: Path, reference: str) -> Path:
    if not isinstance(reference, str) or not re.fullmatch(r"model_calls/call_[0-9]+\.(?:request|response)\.json", reference):
        raise NormalizationError("Unsafe model audit reference")
    return _inside(model, reference)


def _new(path: Path, raw: bytes) -> None:
    """Idempotent exact-byte creation; never replace existing evidence."""
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.read_bytes() != raw: raise NormalizationError("Existing recovery evidence differs")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())


def _encoded(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def normalize_responder_suffix(run_dir: Path, config: dict) -> dict:
    run_dir = Path(run_dir).resolve()
    status_path = _inside(run_dir, "run_status.json")
    status, status_raw = _read(status_path)
    if (status.get("state") != "failed" or status.get("error_type") != "DynamicResponseRequired"
            or status.get("current_stage") != 10 or status.get("completed_stages") != list(range(1, 11))):
        raise NormalizationError("Only the observed T11 release failure after completed T01–T10 is eligible")
    manifest, _ = _read(_inside(run_dir, "run_manifest.json"))
    private = _inside(run_dir, "private_responder")
    binding, _ = _read(_inside(private, "P02_LENDER.binding.json"))
    fingerprint = binding.get("fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch("[0-9a-f]{64}", fingerprint):
        raise NormalizationError("Invalid private session fingerprint")
    session = _inside(private, "sessions/" + fingerprint)
    pending = _inside(session, "trial/responses/P02_LENDER.pending")
    if not pending.is_dir() or (pending.parent / "P02_LENDER/receipt.json").exists():
        raise NormalizationError("Expected an uncommitted failed pending event")
    model = _inside(private, "model")
    log_path = _inside(model, "model_calls.jsonl")
    if log_path.is_symlink(): raise NormalizationError("Symlink model log")
    log_raw = log_path.read_bytes()
    records = [json.loads(line) for line in log_raw.splitlines() if line.strip()]
    if not records or any(not isinstance(row, dict) for row in records): raise NormalizationError("Invalid model call audit")
    call = records[-1]
    if call.get("status") != "succeeded" or not isinstance(call.get("task"), str) or not call["task"].startswith("responder:"):
        raise NormalizationError("Last real model call is not the failed structured Responder output")
    call_id = call.get("call_id")
    if not isinstance(call_id, str) or sum(r.get("call_id") == call_id for r in records) != 1:
        raise NormalizationError("Ambiguous model call identity")
    name = call["task"][len("responder:"):]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name): raise NormalizationError("Invalid structured call name")
    request_path = _model_file(model, call.get("request_ref"))
    response_path = _model_file(model, call.get("response_ref"))
    if request_path.name != call_id + ".request.json" or response_path.name != call_id + ".response.json":
        raise NormalizationError("Model references do not match the call identity")
    request, request_raw = _read(request_path)
    response, response_raw = _read(response_path)
    failure_path = _inside(pending, "audit/" + name + "-failure.json")
    failure, failure_raw = _read(failure_path)
    if failure.get("category") != "invalid_structured_response": raise NormalizationError("Failure was not the structured-output parse failure")
    messages = request.get("messages")
    if not isinstance(messages, list) or len(messages) != 2 or messages[0].get("role") != "system" or messages[1].get("role") != "user":
        raise NormalizationError("Unexpected structured request envelope")
    system = messages[0].get("content")
    if not isinstance(system, str) or not system.endswith(SYSTEM_SUFFIX): raise NormalizationError("Exact SDK system suffix not found")
    envelope = json.loads(messages[1]["content"])
    if not isinstance(envelope, dict) or set(envelope) != {"task", "json_schema"}: raise NormalizationError("Unexpected schema request envelope")
    schema = envelope["json_schema"]
    request_hash = _digest({"system": system[:-len(SYSTEM_SUFFIX)], "payload": envelope["task"], "schema": schema})
    if failure.get("request_sha256") != request_hash: raise NormalizationError("Canonical request hash mismatch")
    # The original, unchanged Bridge computes the same configuration identity;
    # no model is instantiated and no API is called by its constructor.
    bridge = ResponderBridge(Path(manifest["case_root"]), run_dir, config)
    config_hash = bridge.configuration_sha256
    if failure.get("configuration_sha256") != config_hash or binding.get("configuration_sha256") != config_hash:
        raise NormalizationError("Original Responder code/configuration hash mismatch")
    if request.get("model") != bridge.configuration["model"] or call.get("model") != bridge.configuration["model"]:
        raise NormalizationError("Model identity mismatch")
    choice = response.get("choices", [{}])[0]
    message = choice.get("message", {})
    if choice.get("finish_reason") != "stop" or message.get("refusal") or message.get("tool_calls"):
        raise NormalizationError("Incomplete, refused or tool-bearing response is ineligible")
    content = message.get("content")
    if not isinstance(content, str): raise NormalizationError("Response has no text")
    stripped = content.strip()
    result, end = json.JSONDecoder().raw_decode(stripped)
    if not isinstance(result, dict) or stripped[end:] != "]}":
        raise NormalizationError("Only the exact redundant closing suffix ]} is eligible")
    try:
        jsonschema.validators.validator_for(schema).check_schema(schema)
        jsonschema.validate(result, schema)
    except (jsonschema.ValidationError, jsonschema.SchemaError):
        # Validation tracebacks can contain private business contents.
        raise NormalizationError("Original schema validation failed; no business fields may be repaired") from None
    audit_relative = "recoveries/responder_syntax_" + request_hash[:16] + "_" + _sha(response_raw)[:16]
    audit = _inside(run_dir, audit_relative)
    provenance = {"kind": "deterministic_redundant_json_closers_normalization", "schema_version": 1,
        "call_id": call_id, "call_name": name, "session_fingerprint": fingerprint,
        "request_sha256": request_hash, "configuration_sha256": config_hash,
        "source_request_file_sha256": _sha(request_raw), "source_response_file_sha256": _sha(response_raw),
        "source_failure_file_sha256": _sha(failure_raw), "source_status_file_sha256": _sha(status_raw),
        "source_model_log_sha256": _sha(log_raw), "original_content_sha256": _sha(content.encode()),
        "retained_json_prefix_sha256": _sha(stripped[:end].encode()), "discarded_suffix": "]}", "discarded_characters": 2,
        "business_fields_modified": False, "new_model_calls": 0, "writes_official_receipt": False,
        "normalizer_code_sha256": _sha(Path(__file__).read_bytes()), "recovery_audit": audit_relative,
        "result_canonical_sha256": _digest(result)}
    cached = {"request_sha256": request_hash, "configuration_sha256": config_hash,
        "configuration": bridge.configuration, "usage": response.get("usage"), "result": result,
        "normalization_provenance": provenance}
    cache_path = _inside(pending, "audit/" + name + ".json")
    # Validate all prospective existing files before creating anything new.
    copies = {"original_request.json": request_raw, "original_response.json": response_raw,
              "original_failure.json": failure_raw, "original_run_status.json": status_raw,
              "normalization_manifest.json": _encoded(provenance)}
    if cache_path.exists() and (cache_path.is_symlink() or cache_path.read_bytes() != _encoded(cached)):
        raise NormalizationError("Existing structured cache differs; it must not be overwritten")
    if audit.exists():
        if audit.is_symlink() or not audit.is_dir(): raise NormalizationError("Unsafe recovery directory")
        for filename, raw in copies.items():
            target = _inside(audit, filename)
            if target.exists() and target.read_bytes() != raw: raise NormalizationError("Existing recovery snapshot differs")
    # Ensure no source input changed between validation and cache creation.
    for path, raw in ((request_path, request_raw), (response_path, response_raw), (failure_path, failure_raw), (status_path, status_raw), (log_path, log_raw)):
        if path.read_bytes() != raw: raise NormalizationError("Audit input changed during normalization")
    audit.mkdir(parents=True, exist_ok=True, mode=0o700); audit.chmod(0o700)
    for filename, raw in copies.items(): _new(_inside(audit, filename), raw)
    already = cache_path.exists()
    _new(cache_path, _encoded(cached))
    return {"status": "already_normalized" if already else "normalized_cache_created", "call_id": call_id,
            "call_name": name, "recovery_audit": audit_relative, "cache_sha256": _sha(cache_path.read_bytes()),
            "request_sha256": request_hash, "configuration_sha256": config_hash,
            "discarded_characters": 2, "business_fields_modified": False, "new_model_calls": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    print(json.dumps(normalize_responder_suffix(args.run_dir, config), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

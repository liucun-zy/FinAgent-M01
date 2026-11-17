"""One typed contract for M03 schemas, decoding, examples and execution inputs."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
from functools import lru_cache
import json
import math
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from jsonschema import Draft202012Validator, ValidationError


@dataclass
class LedgerRepresentation:
    entity_id: str
    predicate: str
    display_statement: str
    display_value: dict | None
    qualifiers: dict
    effective_time: str | dict | None


@dataclass
class TargetState:
    target_item_id: str | None
    after: LedgerRepresentation | None
    adopted_claim_ids: list[str]
    compared_claim_ids: list[str] = field(default_factory=list)
    evidence_ref_ids: list[str] = field(default_factory=list)
    context_ref_ids: list[str] = field(default_factory=list)
    field_sources: dict[str, list[str]] = field(default_factory=dict)
    reason: str = ""
    compilation: dict = field(default_factory=dict)


@dataclass
class ChangeSet:
    change_set_id: str
    input_claim_ids: list[str]
    states: list[TargetState]
    reason: str
    compilation_errors: list[dict] = field(default_factory=list)


@dataclass
class InputDisposition:
    input_claim_id: str
    handled: bool
    change_set_ids: list[str]
    pending_questions: list[str]
    reason: str


@dataclass
class MaintenanceProposal:
    change_sets: list[ChangeSet]
    dispositions: list[InputDisposition]
    previous_receipt_id: str | None = None


@dataclass
class TargetProposal:
    target_item_id: str | None
    after: dict | None = field(metadata={"direct_representation": True})
    adopted_claim_ids: list[str]
    compared_claim_ids: list[str] = field(default_factory=list)
    evidence_ref_ids: list[str] = field(default_factory=list)
    context_ref_ids: list[str] = field(default_factory=list)
    field_sources: dict[str, list[str]] = field(default_factory=dict)
    reason: str = ""


@dataclass
class ProposedChangeSet:
    change_set_id: str
    input_claim_ids: list[str]
    states: list[TargetProposal]
    reason: str


@dataclass
class ModelMaintenanceProposal:
    change_sets: list[ProposedChangeSet]
    dispositions: list[InputDisposition]
    previous_receipt_id: str | None = None


def representation_fields():
    """Business assignments reuse the internal representation's actual types."""
    return {name: kind for name, kind in get_type_hints(LedgerRepresentation).items()
            if name != "display_statement"}


@dataclass
class WindowContextRequest:
    window_id: str
    relation: Literal["neighbors", "section", "table_context", "referenced"]
    target_ref_or_query: str = ""


@dataclass
class EvidenceContextRequest:
    evidence_ref_id: str


@dataclass
class ContextRequest:
    claim_id: str
    requests: list[WindowContextRequest | EvidenceContextRequest] | None = None


@dataclass
class ReadMaintenanceContext:
    requests: list[ContextRequest]


@dataclass
class ExpansionRequest:
    input_claim_ids: list[str]
    limit: int = field(metadata={"minimum": 1})


@dataclass
class ExpandMaintenanceCandidates:
    requests: list[ExpansionRequest]


TOOL_TYPES = {
    "read_maintenance_context": ReadMaintenanceContext,
    "expand_maintenance_candidates": ExpandMaintenanceCandidates,
    "commit_maintenance": ModelMaintenanceProposal,
}
DESCRIPTIONS = {
    "read_maintenance_context": "Batch read missing source context at the pinned visible time. A claim_id alone uses its saved context; an exact evidence_ref_id resolves from that source index, or choose a provided window_id and relation. Context is not adopted evidence automatically.",
    "expand_maintenance_candidates": "Expand target/context candidates for the same inputs and fixed ledger version.",
    "commit_maintenance": "Propose business after-state fields directly, grouped by logical dependency, and separate input dispositions. Omitted fields on an existing target retain its fixed base state; explicit null clears nullable fields and qualifiers={} clears qualifiers. New items require entity_id, predicate and display_value; after=null withdraws an existing item. The program renders display text, computes deltas and saves a receipt. Independent valid groups can execute. Repair only unfinished scope using the previous receipt.",
}


@lru_cache(maxsize=None)
def schema_for(kind):
    """Derive the small JSON type vocabulary used by these dataclasses."""
    from dataclasses import MISSING
    origin, args = get_origin(kind), get_args(kind)
    if kind is Any:
        return {}
    if origin in (Union, UnionType):
        return {"anyOf": [schema_for(arg) for arg in args]}
    if origin is Literal:
        return {"type": "string", "enum": list(args)}
    if kind is type(None):
        return {"type": "null"}
    if origin is list:
        return {"type": "array", "items": schema_for(args[0])}
    if kind is dict or origin is dict:
        return {"type": "object", "additionalProperties": schema_for(args[1]) if args else True}
    if is_dataclass(kind):
        hints = get_type_hints(kind)
        return {"type": "object", "properties": {
            f.name: _field_schema(f, hints[f.name]) for f in fields(kind)},
            "required": [f.name for f in fields(kind)
                         if f.default is MISSING and f.default_factory is MISSING],
            "additionalProperties": False}
    return {"type": {str: "string", int: "integer", bool: "boolean", float: "number"}[kind]}


def _field_schema(f, kind):
    if f.metadata.get("direct_representation"):
        direct = {"type": "object", "properties": {
            name: schema_for(value_type) for name, value_type in representation_fields().items()},
            "additionalProperties": False}
        return {"anyOf": [direct, {"type": "null"}],
                "description": "Direct business fields: existing target omissions retain the fixed base fields; explicit null clears nullable fields; qualifiers={} clears and nonempty qualifiers replaces the entire collection. A new target requires entity_id, predicate, display_value, with qualifiers={} and effective_time=null if omitted. display_value is the structured value object, including unit; display text is generated."}
    return {**schema_for(kind), **dict(f.metadata)}


NORMALIZATION_POLICY = "declared-json-containers-v1"


def _strict_json(text):
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError("Duplicate JSON object key")
            result[key] = value
        return result

    def number(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("Non-finite JSON number")
        return value

    def constant(text):
        raise ValueError("Non-standard JSON number")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)


def normalize_containers(value, schema):
    """Decode each explicitly container-typed string once; never infer business meaning.

    This also accepts a saved historical schema for read-only protocol replay.
    A node allowing strings or Any remains unchanged, including JSON-looking text.
    """
    records = []

    def visit(node, declared, path):
        alternatives = declared.get("anyOf", [declared])
        types = set()
        unconstrained = False
        for option in alternatives:
            typ = option.get("type")
            if typ is None:
                unconstrained = True
            else:
                types.update(typ if isinstance(typ, list) else [typ])
        if isinstance(node, str) and not unconstrained and "string" not in types:
            containers = types & {"object", "array"}
            if containers:
                try:
                    decoded = _strict_json(node)
                except (ValueError, TypeError):
                    decoded = node
                actual = "object" if isinstance(decoded, dict) else "array" if isinstance(decoded, list) else None
                if actual in containers:
                    node = decoded
                    records.append({"path": path, "before_type": "string", "after_type": actual,
                                    "policy_version": NORMALIZATION_POLICY})
        actual = "object" if isinstance(node, dict) else "array" if isinstance(node, list) else None
        matching = next((option for option in alternatives if option.get("type") == actual), declared)
        if isinstance(node, dict):
            properties = matching.get("properties", {})
            extra = matching.get("additionalProperties", True)
            return {key: visit(item, properties.get(key, extra if isinstance(extra, dict) else {}),
                               path + "." + key) for key, item in node.items()}
        if isinstance(node, list):
            item_schema = matching.get("items", {})
            return [visit(item, item_schema, path + f"[{index}]") for index, item in enumerate(node)]
        return node

    return visit(value, schema, "$"), records



def protocol_issue(exc: ValidationError):
    path = "$" + "".join(f"[{p}]" if isinstance(p, int) else "." + p for p in exc.absolute_path)
    # Report the path and expected type before any source payload; never echo an
    # enormous invalid value and truncate the actionable part of the message.
    if exc.validator == "type":
        message = f"Expected {exc.validator_value}; received {type(exc.instance).__name__}"
    elif exc.validator == "anyOf":
        message = "Value does not match the declared alternatives"
    else:
        message = exc.message
    return {"code": "protocol_error", "path": path, "message": message[:500]}


class ProtocolError(ValueError):
    def __init__(self, issue, protocol_record=None):
        self.issue = issue
        self.protocol_record = protocol_record
        super().__init__(f"{issue['path']}: {issue['message']}")


def _decode(kind, value):
    if value is None:
        return None
    origin, args = get_origin(kind), get_args(kind)
    if origin in (Union, UnionType):
        match = next(arg for arg in args if Draft202012Validator(schema_for(arg)).is_valid(value))
        return _decode(match, value)
    if is_dataclass(kind):
        hints = get_type_hints(kind)
        return kind(**{key: _decode(hints[key], item) for key, item in value.items()})
    if origin is list:
        return [_decode(args[0], item) for item in value]
    return value


def decode_tool(name, arguments, *, protocol_log=None):
    kind = TOOL_TYPES[name]
    schema = schema_for(kind)
    validator = Draft202012Validator(schema)
    raw_valid = validator.is_valid(arguments)
    normalized, paths = normalize_containers(arguments, schema)
    record = {"tool": name, "policy_version": NORMALIZATION_POLICY,
              "raw_valid": raw_valid, "normalizations": paths}
    try:
        validator.validate(normalized)
    except ValidationError as exc:
        record.update(status="invalid", valid_after_normalization=False)
        if protocol_log is not None:
            protocol_log.append(record)
        raise ProtocolError(protocol_issue(exc), record) from None
    record.update(status="raw_valid" if raw_valid else "normalized_valid", valid_after_normalization=True)
    if protocol_log is not None:
        protocol_log.append(record)
    return _decode(kind, normalized)


def tool_specs():
    return [{"name": name, "description": DESCRIPTIONS[name], "parameters": schema_for(kind)}
            for name, kind in TOOL_TYPES.items()]


def minimal_example():
    # A handled input can retain an unresolved question without a second write.
    return asdict(ModelMaintenanceProposal(change_sets=[ProposedChangeSet(
        change_set_id="change-1", input_claim_ids=["<new-claim-id>"], states=[TargetProposal(
            target_item_id="<existing-item-id>", after=None, adopted_claim_ids=[],
            compared_claim_ids=["<new-claim-id>"], reason="来源明确撤销原安排")],
        reason="退出原安排")], dispositions=[InputDisposition(
            input_claim_id="<new-claim-id>", handled=True, change_set_ids=["change-1"],
            pending_questions=["替代安排是什么？"], reason="撤销已处理，替代安排仍未知")]))

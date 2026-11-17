"""Rebuild the last *observed* Candidate exchange without running any tool.

This is technical recovery, not a replay or a new Candidate attempt. All inputs
are immutable audit files. A missing, ambiguous, or mismatched result is fatal:
the caller must not guess, rerun a tool, or rewind an already frozen stage.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any


class RecoveryError(ValueError):
    """The audit trail does not establish one exact recoverable exchange."""


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _jsonl(path: Path) -> list[tuple[dict[str, Any], bytes]]:
    try:
        lines = path.read_bytes().splitlines()
        rows = [(json.loads(line), line) for line in lines if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"Cannot read complete audit log: {path.name}") from exc
    if any(not isinstance(row, dict) for row, _ in rows):
        raise RecoveryError(f"Audit rows must be objects: {path.name}")
    return rows


def _model_file(run_dir: Path, reference: Any) -> tuple[dict[str, Any], bytes]:
    if not isinstance(reference, str) or not reference:
        raise RecoveryError("Missing model audit reference")
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "model_calls":
        raise RecoveryError("Model audit reference must be inside model_calls")
    path = run_dir / relative
    cursor = run_dir
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise RecoveryError("Symlink model audit references are forbidden")
    try:
        path.resolve().relative_to((run_dir / "model_calls").resolve())
        if not path.is_file():
            raise RecoveryError("Model audit reference is not a file")
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RecoveryError("Invalid model audit reference") from exc
    if not isinstance(value, dict):
        raise RecoveryError("Model audit payload must be an object")
    return value, raw


def _assistant(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        raise RecoveryError("Missing assistant response")
    result = {"role": "assistant", "content": message.get("content")}
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list):
        raise RecoveryError("tool_calls must be a list")
    if calls:
        result["tool_calls"] = deepcopy(calls)
    return result


def _calls(message: dict[str, Any]) -> list[tuple[str, str, Any]]:
    result = []
    ids = set()
    for call in message.get("tool_calls", []):
        if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"]:
            raise RecoveryError("Tool call is missing a stable ID")
        if call["id"] in ids:
            raise RecoveryError("Duplicate tool call ID")
        ids.add(call["id"])
        function = call.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise RecoveryError("Invalid tool function")
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise RecoveryError("Cannot exactly match unparsable tool arguments") from exc
        result.append((call["id"], function["name"], arguments))
    return result


def restore_last_exchange(run_dir: Path) -> dict[str, Any]:
    """Return an exact request + response + observed tool-result checkpoint.

    Only successful Candidate tasks named TNN are eligible. The last assistant
    event for that task must match the model response and its tool calls, and
    all subsequent tool records must match in the runner's execution order.
    This function performs no writes, source reads, model calls, or tool calls.
    """
    run_dir = Path(run_dir).resolve()
    audit = _jsonl(run_dir / "model_calls.jsonl")
    eligible = [row for row, _ in audit if row.get("status") == "succeeded"
                and re.fullmatch(r"T\d{2}", str(row.get("task", "")))
                and row.get("response_ref")]
    if not eligible:
        raise RecoveryError("No successful Candidate exchange is recorded")
    selected = eligible[-1]
    call_id = selected.get("call_id")
    if not isinstance(call_id, str) or sum(row.get("call_id") == call_id for row, _ in audit) != 1:
        raise RecoveryError("Model call identity is ambiguous")
    task = selected["task"]
    stage = int(task[1:])
    request, raw_request = _model_file(run_dir, selected.get("request_ref"))
    response, raw_response = _model_file(run_dir, selected.get("response_ref"))
    messages = request.get("messages")
    if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
        raise RecoveryError("Model request does not contain a valid message list")
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RecoveryError("Model response has no completion choice")
    assistant = _assistant(choices[0].get("message"))
    calls = _calls(assistant)

    trajectory_path = run_dir / "agent_trajectory.jsonl"
    trajectory = _jsonl(trajectory_path)
    indices = [i for i, (row, _) in enumerate(trajectory)
               if row.get("task") == task and row.get("role") == "assistant"]
    if not indices:
        raise RecoveryError("Candidate assistant event is missing")
    index = indices[-1]
    event = trajectory[index][0]
    logged = _assistant(event.get("message"))
    if _calls(logged) != calls or logged.get("content") != assistant.get("content"):
        raise RecoveryError("Last assistant event does not match the last successful model response")
    if event.get("stage") != stage:
        raise RecoveryError("Candidate task and event stage disagree")
    turn = event.get("turn")
    if not isinstance(turn, int) or isinstance(turn, bool) or turn < 0:
        raise RecoveryError("Assistant turn is missing or invalid")
    tools = [(row, raw) for row, raw in trajectory[index + 1:]
             if row.get("task") == task and "tool" in row]
    if len(tools) != len(calls):
        raise RecoveryError("Missing or extra tool results; recovery must not execute tools")

    reconstructed = deepcopy(messages)
    reconstructed.append(assistant)
    result_hashes = {}
    record_hashes = {}
    for (tool_id, name, arguments), (row, raw) in zip(calls, tools):
        if row.get("stage") != stage or row.get("turn") != turn:
            raise RecoveryError("Tool result belongs to a different stage or turn")
        if row.get("tool") != name or row.get("arguments") != arguments or "result" not in row:
            raise RecoveryError("Tool result name/arguments do not match execution order")
        content = json.dumps(row["result"], ensure_ascii=False)
        reconstructed.append({"role": "tool", "tool_call_id": tool_id, "content": content})
        result_hashes[tool_id] = _hash(content.encode("utf-8"))
        record_hashes[tool_id] = _hash(raw)
    return {
        "messages": reconstructed, "task": task, "call_id": call_id,
        "current_stage": stage, "completed_turns": turn + 1,
        "raw_result_hashes": {
            "request_file_sha256": _hash(raw_request), "response_file_sha256": _hash(raw_response),
            "trajectory_file_sha256": _hash(trajectory_path.read_bytes()),
            "tool_result_sha256": result_hashes, "tool_record_line_sha256": record_hashes,
        },
    }

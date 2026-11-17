"""One-window financial ledger decision exposed as a terminal DSH tool.

The experiment prepares the same action contracts for both arms. The candidate
has neither a gold-state reader nor an unmetered tool for obtaining more text.
The schema/quote guard certifies execution and provenance. Independent scoring
judges whether the selected, executable contract was financially appropriate.
"""
from __future__ import annotations

import hashlib
import json

from .dsh_bridge import write_json
from .temporal_dag import TemporalDAG


WINDOW_LIMIT = 960


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def fingerprint(value):
    return hashlib.sha256(encode(value)).hexdigest()


def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def initial_context(episode):
    """Only old/new propositions, ledger and possible transactions are public."""
    return {key: episode[key] for key in ("query", "old", "new", "initial_state", "actions")}


def make_workspace(root, episode, window, text):
    if not text.strip() or len(text) > WINDOW_LIMIT:
        raise ValueError("Window outside the fixed character allowance")
    if window["end"] - window["start"] != len(text):
        raise ValueError("Released window length differs from the source offsets")
    dag = TemporalDAG(root)
    if dag.nodes():
        raise FileExistsError("A decision must start from an unused DAG")
    public = initial_context(episode)
    actions = public["actions"]
    options = {x["id"]: x["patch"] for x in actions}
    if (len(options) != len(actions) or options.get("defer") != {}
            or any(not isinstance(p, dict) or set(p) - public["initial_state"].keys()
                   for p in options.values())):
        raise ValueError("Invalid candidate ledger transaction contract")
    prefix = dag.append("ledger_conflict", public)
    received = dag.release_source(1, window["locator"], text, source_key=window["source_id"])
    observation = {k: window[k] for k in ("id", "locator", "source_id", "start", "end")}
    read = dag.append("read_window", {**observation, "chars": len(text),
                      "text_sha256": hashlib.sha256(text.encode()).hexdigest()},
                      [prefix["id"], received["id"]])
    ctx = {"mode": "paired_repair", "request_id": prefix["id"], "source_id": received["id"],
           "read_id": read["id"], "actions": list(options)}
    write_json(root / "task_context.json", ctx)
    # Nothing identifying the policy, rank, trial arm, gold or execution order
    # enters the model request. Subjects get identical bytes for a swapped read.
    instructions = (
        "Decide this financial ledger conflict using the stated purpose, opening ledger and "
        "the single released source excerpt. Interpret statements at their business date and scope. "
        "The two claims are hypotheses and each action is a possible transaction. Select the "
        "narrowest justified action. Confirm an unchanged state if that action is explicitly "
        "supported; defer if the excerpt and opening facts do not establish a decision. "
        "Use commit_repair. Give a short reason and copy a short exact continuous quote from "
        "the excerpt (empty quote is permitted for defer). A selected patch writes the ledger."
    )
    pre = instructions + "\n" + encode(public).decode() + "\nEvidence window\n"
    prompt = pre + encode({"locator": window["locator"], "description": window["description"],
                           "start": window["start"], "end": window["end"], "excerpt": text}).decode()
    return prompt, hashlib.sha256(pre.encode()).hexdigest()


def tool_spec(ctx):
    return {"name": "commit_repair", "description": "Commit one of this ledger's released transaction options.",
            "parameters": {"type": "object", "additionalProperties": False, "required": ["action_id", "quote", "reason"],
                           "properties": {"action_id": {"type": "string", "enum": ctx["actions"]},
                                          "quote": {"type": "string"}, "reason": {"type": "string"}}}}


def commit(dag, ctx, action_id, quote, reason):
    prefix = dag.get(ctx["request_id"])
    already = dag.nodes("ledger_repair")
    if already:
        raise ValueError("A ledger decision has already committed")
    source = dag.get(ctx["source_id"])["payload"]["text"]
    if not reason.strip() or (action_id != "defer" and not quote.strip()) or quote not in source:
        raise ValueError("The citation must occur verbatim in the released excerpt")
    action = next(a for a in prefix["payload"]["actions"] if a["id"] == action_id)
    before = prefix["payload"]["initial_state"]
    final = {**before, **action["patch"]}
    payload = {"action": action_id, "quote": quote, "reason": reason, "initial_state": before,
               "final_state": final, "changed_slots": sorted(k for k in final if final[k] != before[k]),
               "opening_hash": fingerprint(before), "final_hash": fingerprint(final)}
    result = dag.append("ledger_repair", payload, [ctx["request_id"], ctx["read_id"]])
    return {"committed": True, "event": result["id"], **payload}

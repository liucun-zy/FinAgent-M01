"""Audit new checkpoint maintenance actions using the recorded graph and physical native files."""
from __future__ import annotations

import argparse
from collections import Counter
from functools import lru_cache
import json
from pathlib import Path
import sqlite3

from .guarded_artifacts import _slots
from .live_review import source_role
from .tools import digest


def _nodes(root):
    database = (root / "temporal_dag.sqlite3").resolve()
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        db.execute("BEGIN")
        nodes = {f"D{i:09d}": {"seq": i, "kind": kind, "p": json.loads(body), "parents": []}
                 for i, kind, body in db.execute("SELECT seq,kind,payload FROM event ORDER BY seq")}
        for parent, child in db.execute("SELECT parent,child FROM dependency"):
            nodes[f"D{child:09d}"]["parents"].append(f"D{parent:09d}")
    return nodes


def _route_witness(row, route, nodes):
    edges = route["edges"]
    if not edges or edges[0]["from"] != row["source_id"] or edges[0]["to"] != route["claim_id"]:
        return False
    chain = row["source_id"]
    for edge in edges:
        target = nodes.get(edge["to"])
        if (chain != edge["from"] or target is None or chain not in target["parents"]
                or target["seq"] > row["cutoff"]):
            return False
        chain = edge["to"]
    claim = nodes[route["claim_id"]]["p"]
    original = nodes[row["source_id"]]["p"]
    return (chain == route["anchor"] and chain in row["anchor_ids"]
            and claim["source_id"] == row["source_id"] and row["start"] <= claim["start"] < claim["end"] <= row["end"]
            and claim["quote"] == original["text"][claim["start"]:claim["end"]])


def _session_data(root):
    calls, errors, reads, requests = Counter(), Counter(), [], []
    for session in sorted((root / ".dsh").glob("*/session.jsonl")):
        names = {}
        with session.open() as stream:
            for line in stream:
                event = json.loads(line)
                data = event["data"]
                if event["type"] == "tool/call":
                    names[data["callId"]] = data["name"]
                    calls[data["name"]] += 1
                if event["type"] != "tool/result":
                    continue
                message = data["message"]
                name = names[message["toolCallId"]]
                raw = "".join(b.get("text", "") for b in message["content"])
                if message.get("isError") or raw.startswith("Error:"):
                    errors[name] += 1
                elif name == "read_source":
                    item = json.loads(raw)
                    reads.append({"id": item["id"], "locator": item["locator"], "start": item["start"],
                                  "chars": len(item["text"]), "utf8_bytes": len(item["text"].encode())})
        usage = session.with_name("model_calls.jsonl")
        if usage.is_file():
            requests.extend(json.loads(line) for line in usage.read_text().splitlines())
    return {"tool_calls": dict(calls), "tool_errors": dict(errors), "explicit_source_reads": reads,
            "api_attempts_logged": len(requests), "usage_missing": sum(not r.get("usage_complete") for r in requests),
            "estimated_rmb_logged": round(sum(r.get("estimated_cost_rmb") or 0 for r in requests), 8),
            "tokens": {k: sum(r.get(k) or 0 for r in requests)
                       for k in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens")}}


def audit(root, after_seq):
    root = Path(root)
    nodes = _nodes(root)
    if after_seq < 0 or after_seq > max((n["seq"] for n in nodes.values()), default=0):
        raise ValueError("Invalid checkpoint cutoff")
    fresh = {i: n for i, n in nodes.items() if n["seq"] > after_seq}

    @lru_cache(maxsize=24)
    def slots(suffix, sha):
        path = root / ".artifact_versions" / (sha + suffix)
        if not sha or not path.is_file() or digest(path) != sha:
            raise ValueError("Native bytes do not match the registered hash")
        return _slots(path)

    reviews = {}
    for i, n in fresh.items():
        if n["kind"] != "ledger_self_review":
            continue
        p = n["p"]
        src = nodes[p["source_id"]]["p"]
        if src["text"][p["start"]:p["end"]] != p["text"]:
            raise ValueError("Recorded source window differs from its released original")
        if any(not _route_witness(p, r, nodes) for r in p["paths"]):
            raise ValueError("An alleged review route lacks its original witness or dependency")
        reviews[i] = {k: p[k] for k in ("target_id", "source_id", "locator", "start", "end", "window_id", "bm25_first")}
        reviews[i].update(role=source_role(src), graph_routed=bool(p["paths"]),
                          changed_first_window=p["bm25_first"] != p["window_id"], paths=p["paths"],
                          chars=len(p["text"]), utf8_bytes=len(p["text"].encode()))

    published = {}
    for i, n in fresh.items():
        if n["kind"] != "stage_submission":
            continue
        for path, binding in n["p"]["versions"].items():
            v = nodes[binding["id"]]
            sha = binding["sha256"]
            suffix = Path(path).suffix.lower()
            if v["kind"] != "artifact_version" or v["p"]["path"] != path or v["p"]["sha256"] != sha:
                raise ValueError("Submission receipt disagrees with its artifact version")
            slots(suffix, sha)
            published.setdefault(path, []).append({"submission": i, **binding})
    repairs = []
    for i, n in fresh.items():
        p = n["p"]
        if n["kind"] != "artifact_check" or not p.get("repair_outcome"):
            continue
        outcome = p["repair_outcome"]
        if not p["sha256"] or not outcome["before_sha256"]:
            continue
        suffix = Path(p["path"]).suffix.lower()
        before, after = slots(suffix, outcome["before_sha256"]), slots(suffix, p["sha256"])
        changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
        if changed != sorted(outcome["changed_slots"]):
            raise ValueError("Physical native changes disagree with the repair receipt")
        # Walk only the actual authorization edges for this edit. An older
        # review mentioned somewhere in the instrument's history is insufficient.
        attempts = [nodes[j] for j in n["parents"] if nodes[j]["kind"] == "artifact_repair_proposal"]
        diagnoses = [j for a in attempts for j in a["parents"] if nodes[j]["kind"] == "diagnosis_step"]
        consumed = {nodes[k]["p"].get("review_id") for j in diagnoses for k in nodes[j]["parents"]
                    if nodes[k]["kind"] == "probe" and nodes[k]["p"].get("review_id")}
        retained = []
        for submit in published.get(p["path"], []):
            v = nodes[submit["id"]]["p"].get("proposal_id")
            while v and v != i:
                v = nodes[v]["p"].get("repair_of")
            if v:
                final = slots(suffix, submit["sha256"])
                retained.append({"submission": submit["submission"], "version": submit["id"],
                                 "retained_slots": [k for k in changed if final.get(k) == after.get(k)]})
        repairs.append({"id": i, "before_id": outcome["before_id"], "path": p["path"], "status": p["status"],
                        "kind": outcome["kind"], "before_sha256": outcome["before_sha256"], "after_sha256": p["sha256"],
                        "physical_slot_count": len(changed), "changed_slots": changed, "diagnoses": diagnoses,
                        "graph_reviews_used_in_authorization": sorted(j for j in consumed if reviews.get(j, {}).get("graph_routed")),
                        "cleared_conflict_ids": outcome["cleared_conflict_ids"], "cleared_obligation_ids": outcome["cleared_obligation_ids"],
                        "signed_lineage": retained})
    signatures = [{"proof_id": i, "proposal": n["p"]["proposal_id"], "review_id": n["p"]["review_id"],
                   "sha256": n["p"]["sha256"], "witness_claim": n["p"]["witness_claim_id"]}
                  for i, n in fresh.items() if n["kind"] == "artifact_review_decision"]
    usage = _session_data(root)
    return {"checkpoint_last_seq": after_seq, "observed_last_seq": max((n["seq"] for n in nodes.values()), default=0),
            "new_event_counts": dict(Counter(n["kind"] for n in fresh.values())),
            "counts": {"self_review_windows": len(reviews), "business_routed_windows": sum(
                r["role"] == "business_evidence" and r["graph_routed"] for r in reviews.values()),
                "windows_with_consumer_to_original_route": sum(any(len(route["edges"]) > 1 and nodes[route["anchor"]]["kind"] in {
                    "artifact_check", "artifact_proposal", "artifact_version", "native_finance_check"}
                    for route in r["paths"]) for r in reviews.values()),
                "graph_changed_first_window": sum(r["graph_routed"] and r["changed_first_window"] for r in reviews.values()),
                "physically_changed_repair_proposals": sum(bool(r["changed_slots"]) for r in repairs),
                "graph_read_in_changed_repair_authorization": sum(bool(r["changed_slots"] and r[
                    "graph_reviews_used_in_authorization"]) for r in repairs),
                "published_paths": len(published), "signed_stages": sum(n["kind"] == "stage_submission" for n in fresh.values())},
            "reviews": reviews, "repairs": repairs, "signatures": signatures, "stage_submission_files": published,
            "sessions": usage, "interpretation": "A recorded authorization dependency is observable. A causal improvement requires a matched comparison."}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--operator", type=Path, required=True)
    p.add_argument("--after-seq", type=int, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    result = audit(args.operator, args.after_seq)
    with args.out.open("x") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
    print(json.dumps(result["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()

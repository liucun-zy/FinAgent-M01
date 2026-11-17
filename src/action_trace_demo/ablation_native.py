"""Replay the real single-slot T13 memo proposal through the native edit guard.

No model, judge or simulated file writes. The two independent copies of the
same historical prefix differ only in the policy ordering their one allowed
source window. The common edit proposal is the actual action from the prefix.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from .dsh_bridge import write_json
from .dsh_replay import _native_view, prepare
from .guarded_artifacts import ArtifactOperator
from .temporal_dag import TemporalDAG


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _run_arm(root, strategy, proposal):
    dag = TemporalDAG(root)
    operator = ArtifactOperator(root, dag, native_read_strategy=strategy)
    old_file = operator.materialized(proposal["source"])
    dest = operator.materialized(proposal["path"])
    old_hash, dest_hash = _digest(old_file), _digest(dest)
    verdict = operator.call("artifact_propose", proposal)
    if verdict["status"] != "requires_repair" or len(verdict["conflicts"]) != 1:
        raise ValueError("Expected the real memo's one conflicting native slot")
    pair = dag.get(verdict["conflicts"][0]["conflict_id"])["payload"]
    repair = operator.call("artifact_repair", {"proposal_id":verdict["proposal_id"],
        "changes":verdict["suggested_changes"], "description":"Apply the verified current-purpose local change.",
        "probe_budget":1})
    # Always attempt the very same normal commit. A held proposal must not materialize.
    if repair["status"] == "ready":
        commit = operator.call("artifact_commit", {"proposal_id":repair["proposal_id"]})
        if commit["status"] != "committed" or _digest(dest) != commit["sha256"]:
            raise ValueError("Unverified destination bytes")
        old, new = _native_view(old_file), _native_view(dest)
        changed = sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))
        if changed != ["p:1"] or "Case 1" not in new["p:1"] or old["p:1"] not in new["p:1"]:
            raise ValueError("Native memo failed independent selected-basis and local-change checks")
        protected = len((old.keys() | new.keys()) - set(changed))
    elif repair["status"] == "unresolved":
        try:
            operator.call("artifact_commit", {"proposal_id":repair["proposal_id"]})
        except ValueError as exc:
            if "Commit requires a ready" not in str(exc):
                raise
            commit = {"status":"blocked", "reason":str(exc)}
        else:
            raise ValueError("Normal commit accepted an unresolved proposal")
        changed, protected = [], len(_native_view(old_file))
        if _digest(dest) != dest_hash:
            raise ValueError("Held proposal modified current destination")
    else:
        raise ValueError(f"Unexpected native repair result {repair['status']}")
    steps = dag.nodes("diagnosis_step")
    if len(steps) != 1 or len(steps[0]["payload"]["evidence_ids"]) != 1:
        raise ValueError("Each arm must actually read one original window")
    step = steps[0]["payload"]
    probe = dag.get(step["evidence_ids"][0])["payload"]
    if _digest(old_file) != old_hash or step["remaining"] != 0:
        raise ValueError("Invalid history protection or window charge")
    return {"strategy":strategy, "proposal":proposal, "comparison":pair,
            "ranking_query":" ".join([proposal["description"],pair["purpose"],step["locator"]]),
            "ranking":step["probe_ranking"], "selected":step["chosen"],
            "source":{k:probe[k] for k in ("source_id","locator","start","end","source_sha256","quote")},
            "window_reads":1, "chars_read":len(probe["quote"]), "budget":1,
            "repair_status":repair["status"], "commit":commit, "changed_native_slots":changed,
            "protected_positions_unchanged":protected, "historical_sha256_before_after":old_hash,
            "destination_before_sha256":dest_hash, "destination_after_sha256":_digest(dest)}


def replay(base, case, output):
    output.mkdir(parents=True, exist_ok=False)
    root = prepare(base.resolve(), case.resolve(), output/"prefix")
    info = json.loads((output/"prefix/prefix.json").read_text())
    proposal = next(p for p in info["proposals"]
                    if p["path"].endswith(".docx") and p["path"].startswith("working/checkpoints/T13/"))
    result = {"protocol":"native_natural_T13_single_memo_slot", "model_calls":0,
              "boundary":"before original T13 copy", "source_run":str(base.resolve()), "arms":{}}
    for strategy in ("relevance","decision_value"):
        workspace = output/strategy/"operator"
        shutil.copytree(root, workspace)
        result["arms"][strategy] = _run_arm(workspace, strategy, proposal)
    result["window_changed"] = len({arm["selected"] for arm in result["arms"].values()}) > 1
    result["commit_action_changed"] = len({arm["commit"]["status"] for arm in result["arms"].values()}) > 1
    write_json(output/"native_result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = replay(args.base, args.case, args.output)
    for arm, data in report["arms"].items():
        print(arm, data["selected"], data["window_reads"], data["commit"]["status"])

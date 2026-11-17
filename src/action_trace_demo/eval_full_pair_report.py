"""Read-only audit of partially or fully completed matched native-artifact runs."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from hashlib import sha256
import json
from pathlib import Path

from . import evaluation as ev
from .codex_judge import PROTOCOL
from .eval_review_impact import _nodes, audit

POLICIES = ("ledger_only", "ledger_graph")


def read(path):
    return json.loads(path.read_text())


def require(ok, message):
    if not ok:
        raise ValueError(message)


def rows(path):
    if path.exists():
        with path.open("rb") as stream:
            for line in stream:
                # A concurrent append is not a completed audit event.
                if line.endswith(b"\n"):
                    yield json.loads(line)


def _candidate_sessions(root):
    by_id, by_hash = {}, defaultdict(set)
    totals = defaultdict(Counter)
    for path in sorted((root / "sessions").glob("*/invocation.json")):
        item = read(path)
        unit = item["label"]
        if unit.startswith("T") and unit[1:].isdigit():
            by_hash[item["prompt_sha256"]].add(unit)
            totals[unit]["sessions_started"] += 1
            receipt = item.get("receipt", {})
            if receipt.get("session_id"):
                by_id[receipt["session_id"]] = unit
            if item.get("steps") is not None:
                totals[unit]["completed_invocation_turns"] += item["steps"]
    stops=defaultdict(list)
    for folder in sorted((root / ".dsh").glob("*/")):
        log=folder/"model_calls.jsonl"
        session=folder/"session.jsonl"
        unit = by_id.get(folder.name)
        if not unit:
            event = next(rows(session), {})
            messages = event.get("data", {}).get("inserted", [])
            for message in messages:
                if message.get("source", {}).get("kind") != "user":
                    continue
                text = "".join(block.get("text", "") for block in message["content"])
                matches = by_hash[sha256(text.encode()).hexdigest()]
                if len(matches) == 1:
                    unit = next(iter(matches))
                    break
        unit = unit or "unassigned"
        current = totals[unit]
        lifecycle={}
        for event in rows(session):
            data=event.get("data",{})
            if event.get("type")=="tool/call":
                name=data.get("name")
                if name in ("artifact_commit","finish_stage"):
                    args=data.get("arguments",{})
                    lifecycle[data["callId"]]=(name,args.get("proposal_id") if isinstance(args,dict) else None)
            elif event.get("type")=="tool/result":
                message=data.get("message",{})
                cid=message.get("toolCallId")
                if cid not in lifecycle:
                    continue
                name,proposal=lifecycle[cid]
                raw="".join(x.get("text","") for x in message.get("content",[]))
                error=message.get("isError") or raw.startswith("Error:")
                if error or (name=="finish_stage" and '"committed": false' in raw.lower()):
                    stops[unit].append({"session":folder.name,"call_id":cid,"tool":name,
                                        "proposal_id":proposal,"observation":raw[:1000]})
        for call in rows(log):
            current["api_attempts"] += 1
            current["successful_api_attempts"] += call.get("status") == "succeeded"
            current["usage_missing"] += not call.get("usage_complete")
            for key in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens",
                        "estimated_cost_rmb", "budget_accounted_cost_rmb"):
                current[key] += call.get(key) or 0
    return {u:dict(v) for u,v in totals.items()},dict(stops)


def _stage_graph(root):
    nodes = _nodes(root)
    # Audit every route/repair once, then assign it to the request interval
    # containing the event. Invoking a cutoff-only audit per stage double-counts
    # every later review and repair.
    impact = audit(root, 0)
    cutoff = max((n["seq"] for n in nodes.values()), default=0)
    stage_for, stages = {}, {}
    active = None
    for node_id, node in nodes.items():
        p = node["p"]
        if node["kind"] == "stage":
            active = f"T{p['stage']:02d}"
            require(active not in stages, f"Duplicate request for {active}")
            stages[active] = {"request":node_id, "expected":p["expected"], "last_checks":{}, "commit_ids":{},
                              "stage_submission":None, "routes":[], "repairs":[], "review_windows":0,
                              "routed_windows":0, "changed_first_window":0, "last_package_check":None, "signatures":[]}
        if node["kind"] == "question":
            active = None
        if not active:
            continue
        stage_for[node_id] = active
        s = stages[active]
        if node["kind"] == "stage_submission":
            require(s["request"] in node["parents"] and set(s["expected"])==set(p["versions"])
                    and p["stage"]==int(active[1:]), "Stage signature disagrees with its request")
            for path,binding in p["versions"].items():
                version = nodes[binding["id"]]
                require(binding["id"] in node["parents"] and version["kind"]=="artifact_version"
                        and version["p"]["path"]==path and version["p"]["sha256"]==binding["sha256"],
                        "Stage signature does not cite the exact native version")
            s["stage_submission"] = {"id":node_id,"versions":p["versions"]}
        elif node["kind"] == "package_check" and s["request"] in node["parents"]:
            s["last_package_check"] = {"id":node_id,"blocking":p.get("blocking",[])}
        elif node["kind"]=="artifact_check" and p["path"] in s["expected"]:
            s["last_checks"][p["path"]] = {"id":node_id, "sha256":p.get("sha256"), "status":p["status"],
                                          "reasons":p.get("reasons",[]), "conflicts":p.get("conflicts",[])}
        elif node["kind"]=="artifact_version" and p["path"] in s["expected"] and p.get("proof_id"):
            s["commit_ids"][p["path"]] = {"id":node_id,"proof_id":p["proof_id"],"sha256":p["sha256"]}
    for rid,review in impact["reviews"].items():
        if rid not in stage_for:
            continue  # The live audit may already have seen later events.
        s = stages[stage_for[rid]]
        s["review_windows"] += 1
        if review["role"]=="business_evidence" and review["graph_routed"]:
            s["routed_windows"] += 1
            s["changed_first_window"] += review["changed_first_window"]
            for path in review["paths"]:
                s["routes"].append({"review_id":rid,"target":review["target_id"],"source":review["source_id"],
                                    "locator":review["locator"],"offsets":[review["start"],review["end"]],
                                    "claim":path["claim_id"],"anchor":path["anchor"],"edges":path["edges"]})
    for repair in impact["repairs"]:
        if repair["id"] in stage_for:
            stages[stage_for[repair["id"]]]["repairs"].append({**repair,"signed_lineage":[
                row for row in repair["signed_lineage"] if int(row["submission"][1:])<=cutoff]})
    for signature in impact["signatures"]:
        if signature["proof_id"] in stage_for:
            stages[stage_for[signature["proof_id"]]]["signatures"].append(signature)
    for s in stages.values():
        s["business_path_observations"] = len(s["routes"])
        s["distinct_business_paths"] = len({tuple((e["from"],e["to"]) for e in p["edges"]) for p in s["routes"]})
        s["changed_repairs"] = sum(bool(r["changed_slots"]) for r in s["repairs"])
        s["graph_authorized_changed_repairs"] = sum(bool(r["changed_slots"] and r["graph_reviews_used_in_authorization"])
                                                   for r in s["repairs"])
        s["signed_graph_authorized_repairs"] = sum(bool(r["graph_reviews_used_in_authorization"] and
            any(line["retained_slots"] for line in r["signed_lineage"])) for r in s["repairs"])
    return stages, cutoff


def _freeze(run, unit, graph):
    signed = graph.get("stage_submission")
    pin = run/"freezes/workspace"/unit/"freeze.json"
    stored = run/"stage_results"/(unit+".json")
    if not pin.is_file() or not stored.is_file():
        return None
    require(signed, "Persisted freeze without a stage signature")
    freeze, _ = ev._freeze(run,unit)  # checks state digest and hashes every frozen native file
    receipt = read(stored)
    require(receipt["response"]["id"]==signed["id"] and receipt["response"]["committed"] is True and
            receipt["freeze"]==freeze and freeze["evidence_files"]=={p:v["sha256"] for p,v in signed["versions"].items()},
            "Freeze is not the actual signed submission")
    return {"record_sha256":ev._hash(pin),"stage_result_sha256":ev._hash(stored),"files":freeze["evidence_files"]}


def _judge_usage(folder, protocol, rates):
    usage = Counter()
    for request in folder.glob("batches/*/attempt-*/request.json"):
        cfg = read(request)
        require((cfg["model"],cfg["model_reasoning_effort"],cfg["timeout_seconds"]) ==
                (protocol["model"],protocol["reasoning_effort"],protocol["timeout_seconds"]), "Judge request settings differ")
        usage["requests"] += 1
        receipt = request.with_name("session.json")
        if not receipt.is_file():
            usage["attempts_without_receipt"] += 1
            continue
        session=read(receipt)
        usage["completed_sessions"] += 1
        usage["timeouts"] += bool(session.get("timeout"))
        usage["sessions_without_usage"] += not bool(session.get("usage"))
        for k in ("input_tokens","cached_input_tokens","output_tokens","reasoning_output_tokens"):
            usage[k] += (session.get("usage") or {}).get(k,0)
    result=dict(usage)
    result["estimated_rmb_from_observed_usage"] = ((usage["input_tokens"]*rates[0]+usage["output_tokens"]*rates[1])/1e6
                                                  if rates else None)
    return result


def _judge_unit(folder,unit,freeze,protocol,rates):
    usage = _judge_usage(folder,protocol,rates)
    result = {"status":"awaiting_judge", "finalized_valid_rules":0, "budget":usage}
    binding_file=folder/"unit_binding.json"
    if not binding_file.exists():
        return result
    binding=read(binding_file)
    evidence=binding["evidence"]
    require(binding["unit"]==unit and evidence==read(folder/"evidence/evaluation_manifest.json")
            and evidence["evaluation_unit"]==unit,"Judge inputs are bound to a different unit")
    stage="T13" if unit=="FINAL" else unit
    prefix="deliverables/final/" if unit=="FINAL" else f"working/checkpoints/{stage}/"
    expected={"submission/"+k[len(prefix):]:v for k,v in freeze["files"].items() if k.startswith(prefix)}
    submitted={x["path"]:x["sha256"] for x in evidence["files"] if x["role"]=="submission"}
    require(submitted==expected,"Judge files do not match this arm's signed freeze")
    for row in evidence["files"]:
        require(ev._hash(ev._safe(folder/"evidence",row["path"]))==row["sha256"],"Changed Judge evidence file")
    if not (folder/"rubric.json").exists():
        return result
    require(ev._hash(folder/"rubric.json")==binding["rubric_sha256"] and
            ev._hash(folder/"task_prompt.md")==binding["prompt_sha256"],"Changed stage rubric or task prompt")
    rubric=read(folder/"rubric.json")["rubrics"]
    ids={r["id"] for r in rubric}
    require(len(ids)==len(rubric),"Duplicate Judge criterion")
    result.update(status="grading", rubric_count=len(ids),unit_binding_sha256=ev._hash(binding_file))
    grade_file=folder/"evaluation_result.json"
    if not grade_file.exists():
        resolved=set()
        for file in folder.glob("batches/*/accepted.json"):
            for rid,v in read(file)["accepted"].items():
                if rid in ids and v.get("status")=="graded" and type(v.get("hit")) is bool:
                    resolved.add(rid)
        result["provisional_valid_rules"]=len(resolved)
        return result
    grade=read(grade_file)
    for key,file in (("unit_binding_sha256","unit_binding.json"),("verdicts_sha256","verdicts.json"),
                     ("score_details_sha256","score_details.json")):
        require(grade[key]==ev._hash(folder/file),"Changed Judge result receipt")
    require(grade["evaluation_unit"]==unit and grade["rubric_count"]==len(ids),"Wrong rubric cohort")
    verdicts=read(folder/"verdicts.json")
    observed={v["rubric_id"] for v in verdicts}
    valid=sum(v["status"]=="graded" and type(v.get("hit")) is bool for v in verdicts)
    score=read(folder/"score_details.json")
    require(len(observed)==len(verdicts)==grade["verdict_count"] and observed<=ids
            and score["status"]==grade["status"] and grade["local_score"]==score.get("score")
            and (grade["status"]!="graded" or valid==len(ids)),"Wrong or incomplete Judge verdict cohort")
    require(usage.get("completed_sessions",0)==grade["cli_sessions"] and all(
        usage.get(k,0)==v for k,v in grade["usage"].items()),"Judge resource receipts differ from result")
    result.update(status=grade["status"], finalized_valid_rules=valid, score=grade["local_score"],
                  score_raw=grade["score_raw"], receipt_sha256=ev._hash(grade_file))
    return result


def summarize(pair, evaluations=None, judge_config=None, candidate_config=None):
    pair=Path(pair).resolve()
    binding=read(pair/"pair_protocol.json")
    require(set(binding["policies"])==set(POLICIES),"Expected both review policies")
    for filename,h in binding["candidate_code"].items():
        require(ev._hash(ev._safe(pair/"candidate_code",filename))==h,"Changed frozen candidate code")
    native_budget=None
    if candidate_config:
        path=Path(candidate_config)
        require(ev._hash(path)==binding["candidate_config_sha256"],"Incorrect candidate budget configuration")
        conf=read(path)
        native_budget=conf.get("max_cost_rmb", conf.get("budget",{}).get("max_cost_rmb"))
    rates=None
    if judge_config:
        config_path=Path(judge_config)
        require(ev._hash(config_path)==binding["judge_config_sha256"],"Incorrect Judge pricing configuration")
        config=read(config_path)
        rates=(config["input_rmb_per_million_tokens"],config["output_rmb_per_million_tokens"])
    arms, metadata, judges = {}, {}, {}
    for policy in POLICIES:
        run=pair/policy
        manifest=run/"run_manifest.json"
        m=read(manifest)
        require(m["review_policy"]==policy and m["candidate_code_sha256"]==binding["candidate_code"]
                and m["config_sha256"]==binding["candidate_config_sha256"] and m["model"]==binding["model"]
                and m["requested_through_stage"]==binding["through"] and m["candidate_total_stage_turn_limit"]==binding["calls_per_stage"]
                and m["candidate_session_count_limit"]==binding["max_sessions_per_stage"],"Candidate differs from paired protocol")
        metadata[policy]={k:v for k,v in m.items() if k!="review_policy"}
        status=read(run/"run_status.json")
        root=run/"operator"
        stages,cutoff=_stage_graph(root)
        current=f"T{status['current_stage']:02d}"
        costs, terminal_attempts=_candidate_sessions(root)
        for unit,s in stages.items():
            freeze=_freeze(run,unit,s)
            s["freeze"]=freeze
            s["state"]=("frozen" if freeze else "signed_pending_freeze" if s["stage_submission"] else
                        status["state"] if unit==current else "unsubmitted")
            s["usage"]=costs.get(unit,{})
            s["blocked_terminal_attempts"]=terminal_attempts.get(unit,[])
            s["awaiting_file_commit"]=[path for path in s["expected"] if path not in s["commit_ids"]]
        judging={}
        judge_dir=Path(evaluations)/policy if evaluations else None
        judged_protocol=None
        if judge_dir and (judge_dir/"protocol.json").is_file():
            judged_protocol=read(judge_dir/"protocol.json")
            require(judged_protocol["candidate_manifest_sha256"]==ev._hash(manifest) and
                    Path(judged_protocol["candidate"]).resolve()==run and
                    judged_protocol["config_sha256"]==binding["judge_config_sha256"]
                    and judged_protocol["case"]==m["case_root"]
                    and all(judged_protocol.get(k)==v for k,v in PROTOCOL.items()),"Judge bound to another run/protocol")
            judges[policy]={k:v for k,v in judged_protocol.items() if k not in ("candidate_manifest_sha256","candidate")}
        for unit in [*(f"T{i:02d}" for i in range(1,binding["through"]+1)),"FINAL"]:
            source="T13" if unit=="FINAL" else unit
            freeze=stages.get(source,{}).get("freeze")
            if not freeze or judged_protocol is None:
                judging[unit]={"status":"not_frozen" if not freeze else "awaiting_judge","finalized_valid_rules":0}
                if judge_dir:
                    require(not (judge_dir/unit/"evaluation_result.json").exists(),"Grade without matching protocol or freeze")
                continue
            judging[unit]=_judge_unit(judge_dir/unit,unit,freeze,judged_protocol,rates)
        process_units=[f"T{i:02d}" for i in range(1,14)]
        scores=[judging[u]["score"] for u in process_units if judging.get(u,{}).get("status")=="graded"]
        aggregate_candidate=Counter()
        for bucket in costs.values():
            aggregate_candidate.update(bucket)
        aggregate_judge=Counter()
        for unit in judging.values():
            aggregate_judge.update({k:v for k,v in unit.get("budget",{}).items() if v is not None})
        arms[policy]={"run_status":status,"candidate_usage":dict(aggregate_candidate),
                      "judge_usage":dict(aggregate_judge),"candidate_budget_rmb":native_budget,"candidate_manifest_sha256":ev._hash(manifest),
                      "observed_last_seq":cutoff,"stages":stages,"candidate_usage_unassigned":costs.get("unassigned",{}),
                      "judge_protocol_sha256":ev._hash(judge_dir/"protocol.json") if judged_protocol else None,
                      "judge_units":judging,"graded_process_rules":sum(judging.get(u,{}).get("finalized_valid_rules",0) for u in process_units),
                      "process_mean":sum(scores)/13 if len(scores)==13 else None,
                      "final_score":judging["FINAL"].get("score") if judging["FINAL"]["status"]=="graded" else None}
    require(metadata[POLICIES[0]]==metadata[POLICIES[1]],"Candidate settings differ between arms")
    if len(judges)==2:
        require(judges[POLICIES[0]]==judges[POLICIES[1]],"Judge settings differ between arms")
    return {"pair_protocol_sha256":ev._hash(pair/"pair_protocol.json"),"candidate_config_sha256":binding["candidate_config_sha256"],
            "judge_config_sha256":binding["judge_config_sha256"],"stage_turn_budget":binding["calls_per_stage"],
            "stage_session_budget":binding["max_sessions_per_stage"],"judge_budget":PROTOCOL,
            "judge_prices_from_pinned_config":({"input_rmb_per_million_tokens":rates[0],
                                                 "output_rmb_per_million_tokens":rates[1]} if rates else None),
            "arms":arms,"jointly_frozen":[u for u in arms["ledger_only"]["stages"] if all(
                arms[a]["stages"].get(u,{}).get("freeze") for a in POLICIES)],
            "interpretation":"Partial stages, provisional verdicts and final grading remain separate; each full run has its own evolving history."}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pair",type=Path,required=True)
    parser.add_argument("--evaluations",type=Path,help="Directory containing ledger_only and ledger_graph Judge roots")
    parser.add_argument("--judge-config",type=Path,help="Optional SHA-bound source of public pricing rates")
    parser.add_argument("--candidate-config",type=Path,help="Optional SHA-bound source of the experiment spend cap")
    parser.add_argument("--out",type=Path)
    args=parser.parse_args()
    result=summarize(args.pair,args.evaluations,args.judge_config,args.candidate_config)
    if args.out:
        for root in (args.pair,args.evaluations):
            if root:
                require(not args.out.resolve().is_relative_to(root.resolve()),"Write only to a separate evaluator report directory")
        ev._dump(args.out,result)
    else:
        print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()

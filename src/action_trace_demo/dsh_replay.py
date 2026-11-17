"""Paired historical intervention for the first T13 BaseSpread copy proposal.

The maintenance operator consumes only the observed trace prefix and released
T13 instruction. Evaluation assembles its own independent inherited submission
*after* a repaired artifact exists. These are intentionally separate directories.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
import re
import shutil
from pathlib import Path

from .dsh_bridge import open_question, open_stage, write_json
from .dsh_project import export_graph, invoke
from .financial_projection import _register, bind_current_model
from .guarded_artifacts import ArtifactOperator
from .temporal_dag import TemporalDAG
from .tools import digest

DEMO = Path(__file__).resolve().parents[2]
DEFAULT_BASE = DEMO / "runs/mars_eval_20261113_02"
DEFAULT_CASE = DEMO.parent / "deliverbench/data/case-01_mars_refinancing"


def records(path):
    with path.open() as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def source_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def before_copies(base):
    before = []
    for line, entry in enumerate(records(base / "agent_trajectory.jsonl"), 1):
        if entry.get("task") == "T13" and entry.get("role") == "assistant":
            calls = [json.loads(c["function"]["arguments"]) for c in entry["message"].get("tool_calls", [])
                     if c["function"]["name"] == "copy_file"]
            copies = [c for c in calls if c["source"].startswith("/workspace/working/checkpoints/T09/")
                      and "BaseSpread" in (c["source"] + c["description"])]
            if any(c["source"].endswith(".xlsx") for c in copies):
                # The sibling memo proposal is already visible in the same assistant message.
                copies = [c for c in calls if c["source"].startswith("/workspace/working/checkpoints/T09/")
                          and Path(c["source"]).suffix in {".xlsx", ".docx"}]
                return before, copies, {"assistant_proposal_line":line,"turn":entry["turn"],"stage":13}
        before.append(entry)
    raise ValueError("Historical trajectory contains no T13 BaseSpread copy proposal")


def observed_register(dag, source):
    pair = _register(source)
    if not pair:
        return
    order, fields = pair
    order_claim = dag.record_claim(source["id"],"Refinancing scenarios","Scenario order in revised register",
                    "The register supplies the order in which the separate parameter rows are reported.",order,
                    field="case_order",value=list(fields["spread_bps"][1]),scope={"basis":"historical register"})
    for key,(row,values) in fields.items():
        for name,value in values.items():
            entity = "Case "+name
            dag.record_claim(source["id"],entity,f"{entity} {key} = {value}",
                "Value in the approved working register under the scenario's column position.",row,
                field=key,value=value,scope={"scenario":entity},supports=[[order_claim["id"],source["id"]]])


def import_prefix(base, root, before):
    dag = TemporalDAG(root)
    op = ArtifactOperator(root,dag)
    shots = {r["id"]: r for r in records(base/"artifact_events.jsonl")}
    seen_artifacts = [shots[row["result"]["action_id"]] for row in before if
                      isinstance(row.get("result"),dict) and row["result"].get("action_id") in shots]
    last_file = {a["path"]:a["id"] for a in seen_artifacts}
    actions, prior, sources, claims, raw_text, raw_head = {}, {}, {}, {}, {}, {}
    source_seq, end_seq = {}, {}
    # Read the baseline log up to the exact prefix's final recorded proposal.
    proposal_ids = {row["result"]["proposal_id"] for row in before if row.get("tool")=="record_claims"
                    and isinstance(row.get("result"),dict) and row["result"].get("proposal_id")}
    trace = []
    active = None
    for e in records(base/"trace_events.jsonl"):
        p=e["payload"]
        if e["kind"]=="proposal_submitted":
            active=p["proposal_id"]
            if active not in proposal_ids:
                break
        if e["kind"]=="source_registered":
            source_seq[p["source_id"]]=e["sequence"]
        if e["kind"]=="action_decided":
            end_seq[p["proposal_id"]]=e["sequence"]
        trace.append(e)
    cutoff=max(end_seq.values())
    trace = [e for e in trace if e["sequence"]<=cutoff]
    cursor=0
    def advance(end):
        nonlocal cursor
        while cursor<len(trace) and trace[cursor]["sequence"]<=end:
            event=trace[cursor]; cursor+=1
            p=event["payload"]; kind=event["kind"]; stage=event["stage"]
            if kind=="source_registered":
                text=p["text"]
                if source_hash(text)!=p["sha256_text_utf8"]:
                    raise ValueError("Recorded source hash mismatch")
                s=dag.release_source(stage,p["path"],text,source_key="legacy:"+p["source_id"])
                sources[p["source_id"]]=s["id"];raw_text[p["source_id"]]=text
                if p["path"].startswith("source/") and ".xlsx" in p["path"].lower():
                    observed_register(dag,s)
            elif kind=="action_proposed":
                actions[p["action_id"]]=p
            elif kind=="claim_created":
                accepted=None
                for match in p.get("quote_matches",[]):
                    sid=match["source_id"]; start=match["start_char"];end_quote=match["end_char_exclusive"]
                    if sid not in sources:continue
                    quote=raw_text[sid][start:end_quote]
                    if not quote.strip() or quote!=match["quote"]:continue
                    accepted=(sources[sid],quote,start);break
                old=actions.get(p["action_id"],{}).get("old_claim_id")
                if not accepted:
                    dag.append("historical_unverified_claim",{"legacy_id":p["claim_id"],"stage":stage,
                               "reason":"No byte-validated original quotation in visible history"})
                    continue
                sid,quote,start=accepted
                # Preserve the source-backed historical model assertion as such.
                # A distinct legacy scope prevents replay from promoting a receipt into an adjudicated business correction.
                previous=claims.get(old)
                claim=dag.record_claim(sid,p["entity"],f"{p['field']}: {json.dumps(p['value'],ensure_ascii=False)}",
                       p["description"] or "Historical model assertion",quote,field=p["field"],value=p["value"],
                       scope={"legacy_id":p["claim_id"],"described_scope":p.get("scope",""),"assertion":"historical model assessment"},
                       valid_from=p.get("valid_from"),valid_to=p.get("valid_to"),start=start,
                       parents=[previous] if previous else [])
                claims[p["claim_id"]]=claim["id"]
            elif kind=="action_decided":
                parents=[claims[x] for x in (p.get("old_claim_id"),p.get("claim_id")) if x in claims]
                parents.extend(sources[x] for x in p.get("source_ids",[]) if x in sources)
                dag.append("historical_decision",{k:p.get(k) for k in
                    ("action_id","stage","op","entity","field","value","status","description","quote","old_claim_id","claim_id")},
                    list(dict.fromkeys(parents)))
    historic_cutoff=0
    for row in before:
        stage=row.get("stage",0) or 0
        if stage==13 and not historic_cutoff:
            historic_cutoff=dag.nodes()[-1]["seq"]
        out=row.get("result")
        if not isinstance(out,dict):continue
        seq=source_seq.get(out.get("source_id"))
        if row.get("tool")=="record_claims":seq=end_seq.get(out.get("proposal_id"))
        if seq and seq<=cutoff:advance(seq)
        aid=out.get("action_id")
        if aid not in shots:continue
        a=shots[aid]; path=a["path"]
        parents=[claims[x] for x in a.get("claim_ids",[]) if x in claims]
        parents.extend(sources[x] for x in a.get("source_ids",[]) if x in sources)
        parents.extend(prior[x] for x in dict.fromkeys([path,a.get("copy_source_path")]) if x in prior)
        if raw_head.get(path)!=a.get("before_sha256"):
            raise ValueError("Prefix artifact version chain mismatch: "+path)
        raw_head[path]=a.get("after_sha256")
        obs=dag.append("historical_artifact_action",a,list(dict.fromkeys(parents)));prior[path]=obs["id"]
        blob=base/"artifact_versions"/aid/("after"+Path(path).suffix)
        if raw_head[path] and (not blob.is_file() or digest(blob)!=raw_head[path]):
            raise ValueError("Archived file hash mismatch: "+aid)
        if last_file[path]==aid and raw_head[path]:
            op.seed(path,blob,purpose="historical checkpoint",description=a.get("description") or path,
                    parents=[obs["id"]],stage=a["stage"])
    advance(cutoff)
    return claims, {"original_trace_sequence":cutoff,"historic_cutoff":historic_cutoff,"original_sources":len(sources),
                    "anchored_claims":len(claims),"artifact_actions":len(seen_artifacts)}


def prepare(base, case_root, run):
    root=run/"operator"
    if root.exists() or (run/"prefix.json").exists():
        raise ValueError("Use a fresh intervention directory")
    root.mkdir(parents=True)
    before,copies,boundary=before_copies(base)
    claims,summary=import_prefix(base,root,before)
    instruction=(case_root/"task/steps/T13/instruction.md").read_text()
    proposals=[]
    for dest_dir in ("working/checkpoints/T13","deliverables/final"):
        for c in copies:
            proposals.append({"action":"copy","source":c["source"].removeprefix("/workspace/"),
                              "path":dest_dir+"/"+Path(c["path"]).name,"description":c["description"],
                              "claim_ids":[claims[x] for x in c["claim_ids"] if x in claims]})
    expected=[p["path"] for p in proposals]
    ctx=open_stage(root,13,instruction,expected,[("instructions/T13",instruction)])
    bound=bind_current_model(root,ctx)
    if set(bound)!=set(expected):
        raise ValueError("Released selection and source-native slot mapping did not bind every requested destination")
    write_json(run/"prefix.json",{**boundary,**summary,"scope":"first historical copy request and same-purpose final use",
              "baseline":str(base),"case":str(case_root),"proposals":proposals,"contracts":bound})
    return root


def repair(run):
    root=run/"operator"
    info=json.loads((run/"prefix.json").read_text())
    script=[]
    for proposal in info["proposals"]:
        i=len(script)
        script.extend([
          {"name":"artifact_propose","arguments":proposal},
          {"name":"artifact_repair","arguments":{"proposal_id":{"$ref":f"{i}.proposal_id"},
               "changes":{"$ref":f"{i}.suggested_changes"},
               "description":"Apply only the grounded local edits suggested by the current-purpose check."}},
          {"name":"artifact_commit","arguments":{"proposal_id":{"$ref":f"{i+1}.proposal_id"}}}])
    script.append({"name":"finish_stage","arguments":{"description":"Submit verified current workbook and memo versions for the paired historical intervention."}})
    result=invoke(root,"Replay the original copy proposal; resolve the current-purpose check using the returned native-slot edits.",
                  None,script=script,max_calls=len(script),label="T13-prefix")
    write_json(run/"repair_receipt.json",result)
    return result


def repair_live(run, config):
    """Run a fresh candidate from the same chronological prefix with no supplied patch/correct values."""
    root=run/"operator"
    prefix=json.loads((run/"prefix.json").read_text())
    dag=TemporalDAG(root)
    if dag.nodes("artifact_check") or (run/"repair_receipt.json").exists():
        raise ValueError("Live and scripted repairs must start from separate fresh prefixes")
    stage=next(n for n in reversed(dag.nodes("stage")) if n["payload"]["stage"]==13)
    prompt=(stage["payload"]["instruction"]+
            "\nThis is an interrupted handover. Resume the pending copy proposals below under their actual destination purposes. "
            "Examine the released ledger, the historical files, and their action paths as needed. "
            "Use the registered tools to resolve any conflicts, verify each output, and submit via finish_stage. "
            "This intervention is scoped to the four listed workbook/memo destinations.\n"
            +json.dumps(prefix["proposals"],ensure_ascii=False))
    result=invoke(root,prompt,config,max_calls=40,label="T13-live-copy")
    write_json(run/"repair_receipt.json",result)
    return result


def _native_view(path):
    if path.suffix==".xlsx":
        from openpyxl import load_workbook
        wb=load_workbook(path,read_only=True,data_only=False)
        try:return {sh.title+"!"+c.coordinate:c.value for sh in wb for row in sh for c in row if c.value is not None}
        finally:wb.close()
    from docx import Document
    doc=Document(path)
    cells={f"t:{ti}:{ri}:{ci}":cell.text for ti,table in enumerate(doc.tables,1)
           for ri,row in enumerate(table.rows,1) for ci,cell in enumerate(row.cells,1) if cell.text}
    cells.update({f"p:{i}":p.text for i,p in enumerate(doc.paragraphs,1) if p.text})
    return cells


def verify(run):
    # Evaluation only. Expected native values are independently checked here,
    # after the host has committed its decisions.
    info=json.loads((run/"prefix.json").read_text())
    base=Path(info["baseline"]);root=run/"operator"
    dag=TemporalDAG(root);op=ArtifactOperator(root,dag)
    report={}
    for proposal in info["proposals"]:
        path=proposal["path"]; src=proposal["source"]
        old=op.materialized(src); new=op.materialized(path)
        sid=Path(src).parts[2]
        frozen=base/"freezes/workspace"/sid/"evidence"/src
        if digest(old)!=digest(frozen):raise ValueError("Historical checkpoint changed: "+src)
        a,b=_native_view(old),_native_view(new)
        changed=sorted(k for k in a.keys()|b.keys() if a.get(k)!=b.get(k))
        checks=[n for n in dag.nodes("artifact_check") if n["payload"]["path"]==path]
        detection=next(n for n in checks if n["payload"]["status"]=="requires_repair")
        ready=next(n for n in checks if n["payload"]["status"]=="ready")
        permitted={c["locator"] for c in detection["payload"]["conflicts"]}
        protected=set(a.keys()|b.keys())-permitted
        if not changed or not set(changed)<=permitted or any(a.get(k)!=b.get(k) for k in protected):
            raise ValueError("Repair modified an unapproved location")
        if new.suffix==".xlsx":
            if (a.get("Inputs!B17"),a.get("Inputs!B18"))!=(350,2950000) or (b.get("Inputs!B17"),b.get("Inputs!B18"))!=(325,2650000):
                raise ValueError("Independent workbook principal/spread check failed")
            if ready["payload"]["recalculation"]["status"]!="complete":raise ValueError("Missing successful workbook recalculation")
        else:
            if not re.search(r"Case\s+1", b["p:1"]) or "Case 2" not in " ".join(str(v) for v in b.values()):
                raise ValueError("Memo current scope or historical analysis missing")
        if digest(new)!=ready["payload"]["sha256"]:raise ValueError("Committed and verified bytes differ")
        report[path]={"detected":detection["id"],"verified":ready["id"],"historical_sha256":digest(old),
                      "current_sha256":digest(new),"changed_slots":changed,"protected_equal_slots":len(protected),
                      "recalculation":ready["payload"]["recalculation"]}
    write_json(run/"independent_repair_checks.json",report)
    return report


def check_answer(label, result, route, probes):
    text = result.get("answer","").lower().replace(",","").replace("*","")
    amounts = set()
    units = {"billion":10**9,"bn":10**9,"million":10**6,"m":10**6,
             "thousand":1000,"k":1000,"$000s":1000}
    for value,unit in re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)\s*\(?\s*(billion|bn|million|thousand|\$000s|m|k)\b", text):
        amounts.add(Decimal(value)*units[unit])
    for value in re.findall(r"(?:usd|us\$|\$)\s*(\d{7,})\b",text):
        amounts.add(Decimal(value))
    selected = label=="current"
    return {"supported":result.get("status")=="supported","graph_routed":bool(route),"original_inspected":bool(probes),
            "scenario":bool(re.search(r"case\s*"+("1" if selected else "2"),text)),
            "facility":Decimal(2650000000 if selected else 2950000000) in amounts,
            "spread":bool(re.search(r"\b"+("325" if selected else "350")+r"\s*(?:bp|basis)",text))}


def run_qa(run,config):
    root=run/"operator"; prefix=json.loads((run/"prefix.json").read_text())
    maintenance=json.loads((root/"task_context.json").read_text())
    cutoff=TemporalDAG(root).nodes()[-1]["seq"]
    questions={
       "current":("For the 21 August signing-paper preparation, which scenario, total facility and credit spread apply? "
                  "Check the selected scenario against the historical workbook and original instruction or register. Does this establish funding?",
                  cutoff, "2026-08-21"),
       "historical":("As of the historical working register and T09 checkpoint, what were Case 2 financing's total facility and credit spread? "
                     "Refer to the original register and state the units. Use the then-visible evidence.",prefix["historic_cutoff"],"2026-07-17")}
    outcome={}
    try:
        for label,(question,seq,valid_at) in questions.items():
            ctx=open_question(root,question,seq,valid_at)
            receipt=invoke(root,question+" Retrieve the ledger, route through the DAG and inspect the supporting original before answering.",
                           config,max_calls=24,label="qa-"+label)
            dag=TemporalDAG(root)
            probes=[n["id"] for n in dag.nodes("qa_probe") if n["payload"]["question_id"]==ctx["request_id"]]
            result=receipt["result"]
            route=[n["id"] for n in dag.nodes("qa_route") if n["payload"]["question_id"]==ctx["request_id"]]
            checks=check_answer(label,result,route,probes)
            outcome[label]={"question":question,"as_of":seq,"valid_at":valid_at,"route_ids":route,"probe_ids":probes,
                            "answer":result,"mechanical_checks":checks,"session":receipt["session_id"]}
    finally:
        write_json(root/"task_context.json",maintenance)
        write_json(run/"qa_checks.json",outcome)
        export_graph(root)
    return outcome


def freeze_intervention(run):
    """Create verifier inputs in a sibling directory outside the operator boundary."""
    info=json.loads((run/"prefix.json").read_text())
    base=Path(info["baseline"]); destination=run/"intervention_freeze"; root=run/"operator"
    if not (run/"independent_repair_checks.json").exists():raise ValueError("Verify repaired files before constructing the paired freeze")
    if destination.exists():raise ValueError("Use a fresh intervention freeze")
    destination.mkdir()
    for rel in ("freezes","workspace/source","private_responder/sessions"):
        shutil.copytree(base/rel,destination/rel)
    for rel in ("metrics.json","run_manifest.json","run_status.json"):
        shutil.copyfile(base/rel,destination/rel)
    state=json.loads((base/".workspace.case_state.json").read_text())
    data_root=destination/"freezes/workspace/T13"
    record=json.loads((data_root/"freeze.json").read_text())
    if digest(base/"freezes/workspace/T13/freeze.json")!=state["freeze_record_hashes"]["T13"]:
        raise ValueError("Untrusted baseline freeze receipt")
    op=ArtifactOperator(root,TemporalDAG(root))
    changes={}
    for proposal in info["proposals"]:
        path=proposal["path"]; target=data_root/"evidence"/path
        if digest(target)!=record["evidence_files"][path]:raise ValueError("Untrusted baseline artifact")
        old=digest(target);shutil.copyfile(op.materialized(path),target)
        new=digest(target);record["evidence_files"][path]=new
        if path in record["checkpoint_files"]:record["checkpoint_files"][path]=new
        changes[path]={"original":old,"replacement":new}
    record["mode"]="local_research_paired_intervention"
    write_json(data_root/"freeze.json",record);state["freeze_record_hashes"]["T13"]=digest(data_root/"freeze.json")
    write_json(destination/".workspace.case_state.json",state)
    write_json(destination/"intervention.json",{"base":str(base),"changed":changes,"units":["T13","FINAL"],
               "inherited":"All other historical outputs; paired local re-score, not a new end-to-end candidate run."})
    return destination


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=["prepare","repair","repair-live","verify","qa","freeze"])
    parser.add_argument("--run",required=True,type=Path)
    parser.add_argument("--base",default=DEFAULT_BASE,type=Path)
    parser.add_argument("--case",default=DEFAULT_CASE,type=Path)
    parser.add_argument("--config",type=Path)
    args=parser.parse_args();run=args.run.resolve()
    if args.action=="prepare":result=str(prepare(args.base.resolve(),args.case.resolve(),run))
    elif args.action=="repair":result=repair(run)
    elif args.action=="repair-live":
        if not args.config:parser.error("repair-live requires --config")
        result=repair_live(run,args.config.resolve())
    elif args.action=="verify":result=verify(run)
    elif args.action=="qa":
        if not args.config:parser.error("qa requires --config")
        result=run_qa(run,args.config.resolve())
    else:result=str(freeze_intervention(run))
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()

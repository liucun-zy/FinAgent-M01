"""Run a temporal graph project with the actual DSH agent and candidate-visible evidence."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import shutil
from tempfile import NamedTemporaryFile
import subprocess
from uuid import uuid4

from .benchmark import DeliverBenchCase
from .dsh_bridge import current, handover_mirrors, open_question, open_stage, write_json
from .guarded_artifacts import ArtifactOperator
from .responder import ResponderBridge
from .temporal_dag import TemporalDAG
from .tools import read_document, digest

DEMO = Path(__file__).resolve().parents[2]
HOST = DEMO / "dsh_host/run"


class DSHSessionError(RuntimeError):
    def __init__(self, message, receipt):
        super().__init__(message)
        self.receipt = receipt


def invoke(root, prompt, config, *, max_calls=45, label="turn", script=None):
    run = root / "sessions" / (label + "-" + uuid4().hex[:10])
    run.mkdir(parents=True)
    prompt_file = run / "prompt.txt"
    prompt_file.write_text(prompt)
    invocation = {"label":label, "max_calls":max_calls,
                  "started_at":datetime.now(timezone.utc).isoformat(),
                  "prompt_sha256":digest(prompt_file), "status":"running"}
    write_json(run/"invocation.json", invocation)
    cmd = [str(HOST),"--workspace",str(root),"--prompt-file",str(prompt_file),
           "--max-calls",str(max_calls)]
    if script is not None:
        write_json(run/"script.json",script)
        cmd.extend(["--script",str(run/"script.json")])
    else:
        cmd.extend(["--config",str(Path(config).resolve())])
    with (run/"stdout.json").open("w") as out, (run/"stderr.log").open("w") as err:
        process = subprocess.run(cmd, stdout=out, stderr=err)
    try:
        result = json.loads((run/"stdout.json").read_text())
    except (ValueError,OSError):
        result = {"committed":False,"steps":None,"error":f"DSH exited {process.returncode} without a receipt"}
    write_json(run/"invocation.json", {**invocation,"status":"submitted" if result.get("committed") else "unsubmitted",
               "ended_at":datetime.now(timezone.utc).isoformat(), "exit_code":process.returncode,
               "steps":result.get("steps"),"receipt":result})
    if process.returncode or not result.get("committed"):
        raise DSHSessionError(f"DSH did not reach an evidence-checked submission. See {run}",result)
    return result


def graph_edge(parent, child):
    from .dependency_queries import semantic_edges
    return semantic_edges(parent, child)[0]


def export_graph(root):
    dag=TemporalDAG(root)
    nodes=dag.nodes()
    index={n["id"]:n for n in nodes}
    from .dependency_queries import semantic_edges
    edges=[edge for n in nodes for p in n["parents"] for edge in semantic_edges(index[p],n)]
    event_edges=[{"from":p,"to":n["id"],"kind":"event_reference"} for n in nodes for p in n["parents"]]
    if any(index[e["from"]]["seq"]>=index[e["to"]]["seq"] for e in edges):
        raise ValueError("Invalid chronological dependency")
    artifact={"nodes":nodes,"edges":edges,"event_edges":event_edges,"semantic_schema":2,
              "order":"knowledge sequence; business-effective dates are claim metadata"}
    write_json(root/"action_graph.json",artifact)
    write_json(root/"ledger.json",{"as_of":nodes[-1]["seq"] if nodes else 0,"claims":dag.claim_at()})
    from .report import render_report
    render_report(root)
    return {"nodes":len(nodes),"edges":len(edges),"report":str(root/"report.html")}


def _read_released(case, info):
    for path in info["released_files"]:
        yield path, read_document(case.workspace / path)
    yield f'instructions/{info["stage_id"]}', info["instruction"]


def seed_released_snapshots(case,root,info):
    """Make public native source versions available to copy without rewriting formulas.

    These paths remain outside every stage's write contract. A candidate can
    inspect or copy their exact bytes; only a reviewed stage destination can
    be materialized as a new business artifact.
    """
    dag=TemporalDAG(root)
    operator=ArtifactOperator(root,dag)
    sources={n["payload"]["locator"]:n for n in dag.nodes("source")}
    unavailable={n["payload"]["path"]:n for n in dag.nodes("native_source_snapshot_unavailable")}
    for path in info["released_files"]:
        original=case.workspace/path
        if original.suffix.lower() not in {".xlsx",".docx",".pptx",".pdf",".md",".txt"}:
            continue
        source=sources.get(path)
        if source is None:
            raise ValueError("A native source snapshot needs its released text provenance")
        original_hash=digest(original)
        prior=unavailable.get(path)
        if prior and prior["payload"]["sha256"]==original_hash:
            continue
        try:
            operator.seed(path,original,purpose="released source snapshot",
                          description="Exact bytes of the candidate-visible released original.",
                          stage=info["stage"],parents=[source["id"]])
        except ValueError as exc:
            if str(exc)!="Native annotation inspection incomplete":
                raise
            # Native editing is optional for a released source. Keep the
            # original and all of its existing textual evidence available;
            # incomplete annotation inspection prevents registering an editable
            # native version, not advancing the source-release transaction.
            from .native_annotations import extract_native_annotations
            annotations=extract_native_annotations(original)
            dag.append("native_source_snapshot_unavailable",{
                "path":path,"sha256":original_hash,"stage":info["stage"],
                "source_id":source["id"],"body_readable":True,"native_copy_available":False,
                "reason":"Original annotation representation requires review before native copying",
                "annotation_inspection":{k:annotations.get(k) for k in
                    ("status","complete","unsupported","truncated","reason","selected_parts")}},[source["id"]])


def normalize_paths(info):
    stage=info["stage"]
    prefix=f"working/checkpoints/T{stage:02d}/"
    expected = [p.replace("working/current/",prefix,1) for p in info["expected_outputs"]]
    # The handover instructions require identical submitted and checkpoint files.
    out=list(expected)
    if "working/current/" in info["instruction"] and "deliverables/final/" in info["instruction"]:
        out.extend(prefix+Path(p).name for p in expected if p.startswith("deliverables/final/"))
    return list(dict.fromkeys(out))


def stage_prompt(ctx, info, dag=None):
    prompt = (info["instruction"]+"\nCurrent stage write paths: "+json.dumps(ctx["expected"],ensure_ascii=False)+
        "\nNewly released source IDs: "+", ".join(ctx["released"])+
        "\nRead the current source materials and record atomic claims with exact original quotes, entities and scopes. "
        "Use find_claims(detail='compact',limit=5) to locate evidence. Pass a single claim_id when you need the exact statement, original quote and structured value. Use artifact_list/read for prior deliverables. "
        "Released native source snapshots are also listed there; copy them into the current destination "
        "when maintaining a supplied workbook or deck so that its original formulas and layout survive. "
        "Prepare the complete requested professional artifact, covering the business question, calculations, "
        "supporting evidence and material open conditions. Read artifact_format before drafting an unfamiliar format. "
        "Use artifact_read(path,locator='*',limit=25) to find native locations. To read a staged file pass "
        "path and proposal_id only. To create a copy at a new destination omit base_sha256. "
        "Copy a full source-backed excerpt for record_claim; include table header/row evidence and native units. "
        "For each artifact_propose pass claim_ids for the ledger inputs actually used, including the current "
        "purpose when carrying forward an old file. Inspect conflicts and the returned self_review. "
        "The review includes one traced original, the ledger context and any coverage gaps. Consider whether "
        "the source supports this exact entity, business period, scope and destination use. "
        "Before commit, cite the actual self_review window and explain the use and remaining limitations with "
        "artifact_commit(proposal_id,review_id=self_review.id,review_quote=<verbatim excerpt>,review_note=<your assessment>). "
        "Repair conflicts or regenerate the draft when the review exposes an unsupported use. "
        "Use list_work_items and map_work_item to preserve every requirement with explicit acceptance locations. "
        "After generating the output use update_work_item with its check ID. Verify each criterion with a fresh "
        "business review, review ID, and an explicit assessment before finish_stage. "
        "Preserve valid historical conclusions and explicit unresolved issues. Publish all requested files, "
        "then use finish_stage. Inspect the task obligations and their cited source passages; put each required "
        "analysis in the actual file. Use derive_amount for computed amounts/ratios in prose or slides. "
        "For financial content bind a business claim to the exact native slot "
        "and distinguish observed values from derived cash flows and recommendations. Use before:null changes "
        "for missing Excel cells. On a workbook repair you can pass import_sheets:{source_version_id,sheets:[sheetName]} "
        "to add missing operating schedules and their dependencies from a released workbook. Verify that formula "
        "consumers, periods and assumptions match the current use. Compare all structures without overwriting comparison inputs. For handover "
        "mirrors copy the just-committed primary of the same purpose. If its self_review status is proof_reused, "
        "commit with the proposal_id. Address any package-level findings and call finish_stage again.")
    if dag is not None:
        contracts = {n["payload"]["path"]:n["payload"] for n in dag.nodes("artifact_contract")}
        mirrors = handover_mirrors(ctx["expected"], ctx["stage"], contracts) if ctx.get("self_review") else {}
        if mirrors:
            prompt += ("\nFormal primary → required checkpoint mirror pairs: "
                       + json.dumps({primary:mirror for mirror,primary in mirrors.items()}, ensure_ascii=False)
                       + "\nWork on the formal primary path. After its artifact_commit returns sha256, send "
                         "artifact_propose(action='copy',source=<primary>,source_sha256=<committed sha256>,"
                         "path=<mirror>,description='Copy signed primary'). Commit that mirror proposal using its "
                         "proof_reused receipt or the review it requests. "
                         "Read prior checkpoint versions as inputs; recheck their content at the formal path.")
        obligations = [{"id":n["id"], "kind":n["payload"]["kind"], "paths":n["payload"]["paths"],
                        "evidence": [{"id":e["source_id"],"start":e["start"],"end":e["end"]}
                                     for e in n["payload"]["evidence"][:4]]}
                       for n in dag.nodes("task_obligation") if n["payload"].get("stage") == ctx["stage"]]
        if obligations:
            prompt += "\nObligations from your released assignment: " + json.dumps(obligations, ensure_ascii=False)
        from .work_items import WorkItems
        prompt += '\nPersistent requested work: ' + json.dumps(WorkItems(dag).list(ctx['request_id']), ensure_ascii=False)
        rows = [{"id":n["id"],"stage":n["payload"]["stage"],"locator":n["payload"]["locator"],
                 "chars":len(n["payload"]["text"])} for n in dag.nodes("source")
                if not n["payload"].get("source_key", "").startswith(("artifact-observation:","native:"))]
        prompt += "\nReleased source directory (bodies require a source read): "+json.dumps(rows,ensure_ascii=False)
        unavailable=[n["payload"] for n in dag.nodes("native_source_snapshot_unavailable")]
        if unavailable:
            prompt += "\nOriginals available for source reading, with native copying unavailable: "+json.dumps(unavailable,ensure_ascii=False)
    return prompt


def stage_sessions(root, stage_id):
    rows = []
    for file in sorted((root/"sessions").glob(stage_id+"-*/invocation.json")):
        item=json.loads(file.read_text())
        if item["label"]==stage_id:
            rows.append(item)
    if any(type(r.get("steps")) is not int for r in rows):
        raise RuntimeError("A previous session lacks a completed usage receipt; inspect it before resuming")
    return sorted(rows,key=lambda r:r["started_at"])


def resume_prompt(dag,stage,request_id):
    cutoff = dag.get(request_id)["seq"]
    nodes = [n for n in dag.nodes() if n["seq"] > cutoff]
    checks={n["payload"]["path"]:{k:n["payload"].get(k) for k in
                ("status","path","reasons","conflicts","suggested_changes")}|{"proposal_id":n["id"]}
            for n in nodes if n["kind"]=="artifact_check" and n["payload"].get("stage")==stage}
    versions=[{"path":n["payload"]["path"],"id":n["id"],"sha256":n["payload"]["sha256"]}
              for n in nodes if n["kind"]=="artifact_version" and n["payload"].get("stage")==stage]
    active=dag.claim_at()
    summaries=[]
    for n in active[-80:]:
        p=n["payload"]
        value=json.dumps(p.get("value"),ensure_ascii=False)
        summaries.append({"id":n["id"],"entity":p["entity"],"field":p.get("field"),
                          "value_preview":value[:1000],"pending":p.get("pending"),"source_id":p["source_id"]})
    recent=[{"id":n["id"],"kind":n["kind"],
             **{k:n["payload"][k] for k in ("path","status","sha256","proposal_id","source_id","review_id")
                if k in n["payload"]}} for n in nodes[-8:] if n["kind"] not in {"source","claim"}]
    for line in checks.values():
        for key in ("reasons","conflicts","suggested_changes"):
            entries = line.get(key) or []
            line[key] = entries[:6]
            line[key+"_count"] = len(entries)
    progress={"request_id":request_id,"published_this_stage":versions,"staged_proposals":list(checks.values()),
              "current_ledger":summaries,"active_ledger_count":len(active),
              "ledger_note":"Only the latest eighty rows are previewed; find_claims accesses all active history.",
              "recent_actions":recent}
    return "\nPersisted progress from earlier sessions of this same stage: "+json.dumps(progress,ensure_ascii=False)


def run_stage(root, ctx, info, config, max_calls):
    settings=json.loads(config.read_text())
    per_session=int(settings.get("candidate_max_turns_per_session",max_calls))
    maximum_sessions=int(settings.get("candidate_max_sessions_per_stage",1))
    if min(per_session,maximum_sessions,max_calls)<1:
        raise ValueError("Candidate stage and session limits must be positive")
    stage_id=info["stage_id"]
    while True:
        previous=stage_sessions(root,stage_id)
        spent=sum(r["steps"] for r in previous)
        if len(previous)>=maximum_sessions or spent>=max_calls:
            last=previous[-1].get("receipt",{}) if previous else {}
            raise DSHSessionError("Cumulative candidate stage budget exhausted before submission",last)
        dag=TemporalDAG(root)
        prompt=stage_prompt(ctx,info,dag)
        if previous:
            prompt+=resume_prompt(dag,info["stage"],ctx["request_id"])
            prompt+="\nPrevious session stopped: "+str(previous[-1].get("receipt",{}).get("error","unknown"))
        prompt+=f"\nRemaining model requests for this stage: {max_calls-spent}; this session permits {min(per_session,max_calls-spent)}."
        try:
            return invoke(root,prompt,config,max_calls=min(per_session,max_calls-spent),label=stage_id)
        except DSHSessionError as exc:
            if type(exc.receipt.get("steps")) is not int:
                raise
            error=str(exc.receipt.get("error", ""))
            if not any(term in error for term in ("budget exhausted", "Session ended", "Model request failed", "timeout")):
                raise


@contextmanager
def task_lease(root):
    # Serialise task-context switches, but allow the subprocess bridge to take
    # its much shorter operator lock inside an active DSH session.
    with (root / ".task-owner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def project_case(case_root, run, config, through, max_calls, *, review_policy="ledger_graph"):
    run.mkdir(parents=True,exist_ok=True)
    if any(run.iterdir()) and not (run/"operator").is_dir():
        raise ValueError("Existing run is not a DSH workspace; select a new output directory")
    root=run/"operator"
    root.mkdir(exist_ok=True)
    with task_lease(root):
        return _run_stages(case_root,run,config,through,max_calls,root,review_policy)


def candidate_fingerprints():
    files = [*(DEMO/"src/action_trace_demo").glob("*.py"), *(DEMO/"dsh_host").glob("*.mjs"),
             DEMO/"dsh_host/model_call.py", DEMO/"dsh_host/run", DEMO/"dsh_host/pnpm-lock.yaml"]
    return {str(p.relative_to(DEMO)):digest(p) for p in sorted(files)
            if not p.name.startswith(("eval_", "codex_judge", "targeted_review", "prefix_workspace"))
            and p.name not in {"evaluation.py", "dsh_intervention_eval.py"}}


def _run_stages(case_root,run,config,through,max_calls,root,review_policy):
    conf=json.loads(config.read_text())
    from .live_review import REVIEW_POLICIES
    if review_policy not in REVIEW_POLICIES:
        raise ValueError("Unknown self-review policy")
    case=DeliverBenchCase(case_root,run/"workspace",responder=ResponderBridge(case_root,run,conf))
    fresh=not case._state["initialized"]
    opening=case.initialize()
    if fresh:
        dag=TemporalDAG(root)
        for path, body in _read_released(case,opening):
            dag.release_source(0,path,body,source_key=f"0/{path}")
        seed_released_snapshots(case,root,opening)
    manifest={"case_root":str(case_root.resolve()),"host":"DeepSeek Harness",
              "model":conf["model"],"requested_through_stage":through,"official_deliverbench_score":False,
              "require_self_review":True, "review_policy":review_policy, "candidate_total_stage_turn_limit":max_calls,
              "candidate_session_turn_limit":conf.get("candidate_max_turns_per_session",max_calls),
              "candidate_session_count_limit":conf.get("candidate_max_sessions_per_stage",1),
              "config_sha256":digest(config),
              "candidate_code_sha256":candidate_fingerprints()}
    manifest_path=run/"run_manifest.json"
    if manifest_path.is_file() and json.loads(manifest_path.read_text())!=manifest:
        raise ValueError("The candidate's frozen model, budget or requested stages changed")
    if not manifest_path.is_file():
        write_json(manifest_path,manifest)
    for stage in range(1,through+1):
        if f"T{stage:02d}" in case._state["frozen_stages"]:
            continue
        stage_id=f"T{stage:02d}"
        if case._state["current_stage"] == stage:
            info={"stage":stage,"stage_id":stage_id,"instruction":(case.task_root/"steps"/stage_id/"instruction.md").read_text(),
                  "expected_outputs":case._expected_outputs(stage_id),"released_files":list(case._state["published_sources"])}
        else:
            info=case.release(stage)
        try:
            ctx=current(root)
        except FileNotFoundError:
            ctx={}
        write_json(run/"run_status.json",{"state":"initializing_stage","current_stage":stage,
                   "completed_stages":list(range(1,stage)),"official_score":None})
        try:
            if ctx.get("mode")!="maintenance" or ctx.get("stage")!=stage:
                ctx=open_stage(root,stage,info["instruction"],normalize_paths(info),_read_released(case,info),self_review=True,
                               review_policy=review_policy)
            seed_released_snapshots(case,root,info)
            dag=TemporalDAG(root)
            if not any(ctx["request_id"] in n["parents"] for n in dag.nodes("stage_setup_ready")):
                from .financial_projection import bind_current_model
                bind_current_model(root,ctx)
                dag.append("stage_setup_ready",{"stage":stage,"request_id":ctx["request_id"]},[ctx["request_id"]])
        except Exception as exc:
            write_json(run/"run_status.json",{"state":"stage_initialization_failed","current_stage":stage,
                       "completed_stages":list(range(1,stage)),"error":str(exc),"official_score":None})
            raise
        write_json(run/"run_status.json",{"state":"running","current_stage":stage,
                   "completed_stages":list(range(1,stage)),"official_score":None})
        dag=TemporalDAG(root)
        submitted=next((n for n in reversed(dag.nodes("stage_submission"))
                        if ctx["request_id"] in n["parents"]),None)
        if submitted:
            result={"result":{"id":submitted["id"], "committed":True, **submitted["payload"]}}
        else:
            try:
                result=run_stage(root,ctx,info,config,max_calls)
            except Exception as exc:
                write_json(run/"run_status.json", {"state":"stage_unsubmitted","current_stage":stage,
                           "completed_stages":list(range(1,stage)), "error":str(exc),"official_score":None})
                export_graph(root)
                raise
        op=ArtifactOperator(root,dag,self_review=True)
        op.review_policy = ctx["review_policy"]
        signed = dag.get(result["result"]["id"])["payload"]["versions"]
        for path in ctx["expected"]:
            current_head = op.validate_head(path)
            if signed[path] != {"id": current_head["id"], "sha256": current_head["payload"]["sha256"]}:
                raise ValueError("Signing and export refer to different native versions")
            dest=case.workspace/path
            dest.parent.mkdir(parents=True,exist_ok=True)
            with NamedTemporaryFile(dir=dest.parent,delete=False) as out:
                tmp = Path(out.name)
            try:
                shutil.copyfile(op.materialized(path), tmp)
                if digest(tmp) != signed[path]["sha256"]:
                    raise ValueError("Copied bytes do not match the stage signature")
                tmp.replace(dest)
            finally:
                tmp.unlink(missing_ok=True)
        freeze=case.freeze(stage)
        if freeze["evidence_files"] != {p: row["sha256"] for p,row in signed.items()}:
            raise ValueError("Frozen inventory or bytes differ from the signed submission")
        if not freeze["submission_complete"]:
            raise RuntimeError(f"Unfulfilled stage contract: {stage_id}")
        write_json(run/"stage_results"/(stage_id+".json"),{"response":result["result"],"freeze":freeze})
    write_json(run/"run_status.json",{"state":"requested_stages_completed","current_stage":through,
               "completed_stages":list(range(1,through+1)),"official_score":None})
    return export_graph(root)


def ask(root, question, config, cutoff, valid_at, max_calls):
    with task_lease(root):
        return _ask_locked(root,question,config,cutoff,valid_at,max_calls)


def _ask_locked(root,question,config,cutoff,valid_at,max_calls):
    prior=current(root) if (root/"task_context.json").exists() else None
    # The host executes a single task at a time; restore the maintenance context on return.
    ctx=open_question(root,question,cutoff,valid_at)
    try:
        result=invoke(root,question,config,max_calls=max_calls,label="qa")
        export_graph(root)
        return result
    finally:
        if prior is not None:
            write_json(root/"task_context.json",prior)


def main():
    p=argparse.ArgumentParser()
    sub=p.add_subparsers(required=True,dest="op")
    case=sub.add_parser("case")
    case.add_argument("--case",type=Path,required=True)
    case.add_argument("--run",type=Path,required=True)
    case.add_argument("--review-policy",choices=("ledger_graph","ledger_only"), default="ledger_graph")
    case.add_argument("--through",type=int,default=13)
    qa=sub.add_parser("ask")
    qa.add_argument("--workspace",type=Path,required=True)
    qa.add_argument("--question",required=True)
    qa.add_argument("--as-of",type=int)
    qa.add_argument("--valid-at")
    for parser in (case,qa):
        parser.add_argument("--config",type=Path,required=True)
        parser.add_argument("--max-calls",type=int)
    ex=sub.add_parser("export")
    ex.add_argument("--workspace",type=Path,required=True)
    args=p.parse_args()
    if args.op=="case":
        count=args.max_calls or json.loads(args.config.read_text()).get("candidate_max_turns_per_stage",35)
        result=project_case(args.case.resolve(),args.run.resolve(),args.config.resolve(),args.through,count,
                            review_policy=args.review_policy)
    elif args.op=="ask":
        count=args.max_calls or json.loads(args.config.read_text()).get("query_max_turns",20)
        result=ask(args.workspace.resolve(),args.question,args.config.resolve(),args.as_of,args.valid_at,count)
    else:
        result=export_graph(args.workspace)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=="__main__":
    main()

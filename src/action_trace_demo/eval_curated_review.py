"""Run one independent self-review on an immutable, candidate-visible POC checkpoint.

The original full DSH graph and ledger are restored exactly through the
historical submission. New work uses a distinct request and reviewed write
contracts; a historical signature can never complete the new request.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import time

from .dsh_bridge import Bridge, open_stage, write_json
from .dsh_project import DSHSessionError, candidate_fingerprints, export_graph, invoke, run_stage, stage_prompt
from .financial_projection import bind_current_model
from .guarded_artifacts import ArtifactOperator
from .live_review import REVIEW_POLICIES
from .task_obligations import register_obligations
from .temporal_dag import TemporalDAG
from .tools import read_document


def _sha(file):
    return hashlib.sha256(file.read_bytes()).hexdigest()


def _load(file):
    return json.loads(file.read_text())


def _files(folder):
    result = {}
    for f in sorted(folder.rglob("*")):
        if f.is_symlink():
            raise ValueError("A checkpoint cannot reference external files via symlink")
        if f.is_file():
            result[f.relative_to(folder).as_posix()] = f
    return result


def _restore(root, frozen, cutoff_stage):
    graph = _load(frozen/"memory/action_graph.json")
    ledger = _load(frozen/"memory/ledger.json")
    nodes = graph["nodes"]
    if not nodes or nodes[-1]["kind"] != "stage_submission" or nodes[-1]["payload"]["stage"] != cutoff_stage:
        raise ValueError("Graph must end at this checkpoint's historical submission")
    dag = TemporalDAG(root)
    if dag.nodes():
        raise ValueError("Restoration requires a new operator")
    edges = set()
    with sqlite3.connect(dag.path) as con:
        for seq,n in enumerate(nodes,1):
            if n["seq"]!=seq or n["id"]!=f"D{seq:09d}" or n["payload"].get("stage",0)>cutoff_stage:
                raise ValueError("Invalid or future checkpoint event")
            con.execute("INSERT INTO event(seq,kind,payload) VALUES(?,?,?)",
                        (seq,n["kind"],json.dumps(n["payload"],ensure_ascii=False,allow_nan=False)))
            for parent in n["parents"]:
                if not re.fullmatch(r"D\d{9}",parent) or not 1<=int(parent[1:])<seq:
                    raise ValueError("Invalid or future graph parent")
                con.execute("INSERT INTO dependency(parent,child) VALUES(?,?)",(int(parent[1:]),seq))
                edges.add((parent,n["id"]))
            if n["kind"]=="source":
                p=n["payload"]
                if hashlib.sha256(p["text"].encode()).hexdigest()!=p["sha256"]:
                    raise ValueError("Released graph source digest mismatch")
                con.execute("INSERT INTO released VALUES(?,?,?,?,?)",(p["source_key"],seq,p["locator"],p["stage"],len(p["text"])))
        recorded = [(e["from"],e["to"]) for e in graph["edges"]]
        if len(recorded)!=len(edges) or set(recorded)!=edges:
            raise ValueError("Recorded edges and checkpoint parent lists disagree")
    if ledger["as_of"]!=len(nodes) or dag.claim_at(as_of=len(nodes))!=ledger["claims"]:
        raise ValueError("Restored historical ledger differs from supplied checkpoint")
    return dag, nodes


def _mount(root, frozen, nodes):
    art=ArtifactOperator(root,TemporalDAG(root))
    versions=frozen/"memory/artifact_versions"
    blobs = {}
    heads = {}
    for n in nodes:
        p=n["payload"]
        if n["kind"] not in {"artifact_check","artifact_commit_intent","artifact_version"}:
            continue
        if n["kind"]=="artifact_version":
            heads[p["path"]]=n
        if p.get("sha256"):
            h=p["sha256"]
            key=h+Path(p["path"]).suffix.lower()
            if not re.fullmatch(r"[0-9a-f]{64}",h):
                raise ValueError("Invalid blob digest")
            blobs[key]=h
    for name,h in blobs.items():
        f=versions/name
        if not f.is_file() or f.is_symlink() or _sha(f)!=h:
            raise ValueError("Missing or altered archived artifact version: "+name)
        shutil.copyfile(f, art.store/name)
    for path,n in heads.items():
        h=n["payload"]["sha256"]
        if h:
            dest=art.materialized(path)
            dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(art._blob(path,h),dest)
    return art,heads


def _verify_inputs(folder, nodes, heads, raw_paths, stage):
    inventory={}
    for role in ("sources","instructions","history","submission"):
        files=_files(folder/role)
        if role!="history" and not files:
            raise ValueError("Empty curated input area: "+role)
        inventory[role]={name:_sha(f) for name,f in files.items()}
    sources={n["payload"]["locator"]:n for n in nodes if n["kind"]=="source"
             and not n["payload"].get("source_key","").startswith(("native:","artifact-observation:"))}
    logical={}
    for path in raw_paths:
        name=Path(path).name
        expected=inventory["submission"].get(name)
        if not expected or path not in heads or expected!=heads[path]["payload"]["sha256"]:
            raise ValueError("Historical native submission differs from sealed input: "+path)
        if name not in logical or path.startswith("deliverables/final/"):
            logical[name]=path
    if logical.keys()!=inventory["submission"].keys():
        raise ValueError("Unexpected or missing native file in the curated submission")
    for name,h in inventory["history"].items():
        path="working/checkpoints/"+name
        if path not in heads or heads[path]["payload"]["sha256"]!=h:
            raise ValueError("Checkpoint history file differs from graph: "+name)
    public={loc.removeprefix("source/") for loc in sources if loc.startswith("source/")}
    if public!=inventory["sources"].keys():
        raise ValueError("Visible sources and restored graph disagree")
    for name,h in inventory["sources"].items():
        n=heads.get("source/"+name)
        if n and n["payload"]["sha256"]!=h:
            raise ValueError("Original native source differs from archived bytes: "+name)
        if not n and read_document(folder/"sources"/name)!=sources["source/"+name]["payload"]["text"]:
            raise ValueError("Original source differs from the released text: "+name)
    for name,h in inventory["instructions"].items():
        part="OPENING" if name=="project.md" else Path(name).stem
        loc="instructions/"+part
        if loc not in sources or sources[loc]["payload"]["sha256"]!=h:
            raise ValueError("Original instruction differs from archived graph: "+name)
    return inventory,logical,sources


def prepare(checkpoint, run, policy):
    checkpoint=Path(checkpoint).absolute()
    run=Path(run).resolve()
    if checkpoint!=checkpoint.resolve() or not checkpoint.is_dir():
        raise ValueError("Choose a checkpoint directory without symlinks")
    if checkpoint.parent.name=="demo":
        try:
            from .eval_curated_legacy import prepare as prepare_legacy
        except ModuleNotFoundError as exc:
            if exc.name != "action_trace_demo.eval_curated_legacy":
                raise
            raise ValueError("Legacy demo requires its separate legacy provenance adapter") from exc
        return prepare_legacy(checkpoint,run,policy)
    unit=checkpoint.name
    if checkpoint.parent.name!="full" or checkpoint.parent.parent.name!="candidate" or not re.fullmatch(r"T\d{2}|FINAL",unit):
        raise ValueError("Choose candidate/full/Txx or candidate/full/FINAL")
    if policy not in REVIEW_POLICIES:
        raise ValueError("Unknown review policy")
    if run.exists() or run.is_relative_to(checkpoint.parents[2]):
        raise ValueError("Choose a fresh run outside the frozen package")
    if any(p.is_symlink() for p in checkpoint.rglob("*")):
        raise ValueError("Checkpoint contains a symlink")
    run.mkdir(parents=True)
    root=run/"operator"
    stage=13 if unit=="FINAL" else int(unit[1:])
    dag,nodes=_restore(root,checkpoint,stage)
    art,heads=_mount(root,checkpoint,nodes)
    raw_paths=nodes[-1]["payload"]["paths"]
    inventory,logical,sources=_verify_inputs(checkpoint/"output",nodes,heads,raw_paths,stage)
    # FINAL has its own input snapshot. Its T13 history remains immutable;
    # only the final package is an authorized output in this execution.
    expected=list(logical.values()) if unit=="FINAL" else raw_paths
    instruction=sources[f"instructions/T{stage:02d}"]["payload"]["text"]
    # Retain unsupported native binaries, plain-text instructions and history
    # for evaluator freeze. The DSH catalog can access only released DAG
    # sources and protected artifact heads, never the enclosing package.
    evidence_dir=run/"before"
    shutil.copytree(checkpoint/"output",evidence_dir)
    released=[s for s in sources.values() if s["payload"]["stage"]==stage]
    for old in (n for n in nodes if n["kind"]=="stage" and n["payload"]["stage"]<stage):
        ids=[s["id"] for s in sources.values() if s["payload"]["stage"]==old["payload"]["stage"]]
        register_obligations(dag,old,ids,old["payload"]["expected"])
    ctx=open_stage(root,stage,instruction,expected,[(n["payload"]["locator"],n["payload"]["text"]) for n in released],
                   parents=[nodes[-1]["id"]],self_review=True,review_policy=policy)
    bind_current_model(root,ctx)
    record={"version":"full","unit":unit,"policy":policy,"operator_root":str(root),
            "checkpoint":str(checkpoint),"stage":stage,"request_id":ctx["request_id"],"expected":expected,
            "last_seq":nodes[-1]["seq"],"last_historical_id":nodes[-1]["id"],
            "graph_sha256":_sha(checkpoint/"memory/action_graph.json"),"ledger_sha256":_sha(checkpoint/"memory/ledger.json"),
            "original_paths":logical,"original_file_versions":{p:heads[p]["id"] for p in expected},
            "inputs":inventory,"baseline_stage_submission":nodes[-1]["id"]}
    write_json(run/"input_receipt.json",record)
    return record


def _usage(root):
    stats=Counter({k:0 for k in ("transport_attempts","input_tokens","cached_tokens","output_tokens","estimated_cost_rmb",
                                  "budget_accounted_cost_rmb","failed_attempts","tool_errors")})
    tools=Counter()
    for f in sorted((root/".dsh").glob("*/model_calls.jsonl")):
        for line in f.read_text().splitlines():
            event=json.loads(line)
            stats["transport_attempts"]+=1
            for k in ("input_tokens","cached_tokens","output_tokens","estimated_cost_rmb","budget_accounted_cost_rmb"):
                stats[k]+=event.get(k) or 0
            if event.get("status")!="succeeded":
                stats["failed_attempts"]+=1
    for f in sorted((root/".dsh").glob("*/session.jsonl")):
        for line in f.read_text().splitlines():
            event=json.loads(line)
            if event["type"]=="tool/call":
                tools[event["data"]["name"]]+=1
            elif event["type"]=="tool/result" and event["data"]["message"].get("isError"):
                stats["tool_errors"]+=1
    return {"counts":dict(tools),"total_tool_calls":sum(tools.values()),**stats}


def export(run):
    run=Path(run).resolve()
    record=_load(run/"input_receipt.json")
    root=run/"operator"
    bridge=Bridge(root)
    dag,art=bridge.dag,bridge.art
    if bridge.ctx["request_id"]!=record["request_id"]:
        raise ValueError("Operator is no longer bound to this review request")
    cutoff=record["last_seq"]
    submissions=[n for n in dag.nodes("stage_submission") if n["seq"]>cutoff and record["request_id"] in n["parents"]]
    signed=submissions[-1] if submissions else None
    files={}
    checks={}
    for n in dag.nodes("artifact_check"):
        if n["seq"]>cutoff and n["payload"]["path"] in record["expected"]:
            checks[n["payload"]["path"]]=n
    physical={}
    for path in record["expected"]:
        head=art.head(path)
        check=checks.get(path)
        verified=None
        if head and head["seq"]>cutoff and head["payload"].get("proof_id"):
            try:
                verified=art.validate_head(path)
            except ValueError:
                if signed:
                    raise
        physical[path]={"source_version":record["original_file_versions"][path],"head_id":head["id"] if head else None,
                        "valid_commit_id":verified["id"] if verified else None,
                        "last_proposal":check["id"] if check else None,
                        "last_proposal_status":check["payload"]["status"] if check else None}
        if signed:
            receipt=signed["payload"]["versions"].get(path)
            if not verified or receipt!={"id":verified["id"],"sha256":verified["payload"]["sha256"]}:
                raise ValueError("Stage signature does not match the current reviewed artifact")
    for name,path in record["original_paths"].items():
        before=record["inputs"]["submission"][name]
        if _sha(run/"before/submission"/name)!=before:
            raise ValueError("Baseline snapshot changed")
        check=checks.get(path)
        valid_id=physical[path]["valid_commit_id"]
        if signed or valid_id and (not check or dag.get(valid_id)["seq"]>check["seq"]):
            source=art.materialized(path)
            kind="stage_signed" if signed else "file_committed_stage_unsigned"
            h=dag.get(valid_id)["payload"]["sha256"]
        elif check and check["payload"].get("sha256"):
            h=check["payload"]["sha256"]
            source=art._blob(path,h)
            kind="staged_unsigned"
        else:
            h=before
            source=run/"before/submission"/name
            kind="baseline_unreviewed"
        dest=run/"after/submission"/name
        dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source,dest)
        if _sha(dest)!=h:
            raise ValueError("Exported file digest does not match selected version")
        files[name]={"path":str(dest),"sha256":h,"before_sha256":before,"changed":h!=before,"state":kind}
    for name,h in record["inputs"]["history"].items():
        dst=art.materialized("working/checkpoints/"+name)
        if not dst.is_file() or _sha(dst)!=h:
            raise ValueError("A protected historical checkpoint changed")
    native_source_heads={n["payload"]["path"] for n in dag.nodes("artifact_version") if n["payload"]["path"].startswith("source/")}
    for name,h in record["inputs"]["sources"].items():
        p="source/"+name
        if p in native_source_heads and _sha(art.materialized(p))!=h:
            raise ValueError("A protected source file changed")
    for role in ("sources","instructions","history"):
        retained=_files(run/"before"/role)
        if {name:_sha(f) for name,f in retained.items()}!=record["inputs"][role]:
            raise ValueError("Protected raw "+role+" snapshot changed")
    unit=record["unit"]
    snapshot={"version":record["version"],"origin":str(run),"units":{unit:{
        "status":"submitted" if signed else "staged_unsigned",
        "files":{name:{k:v[k] for k in ("path","sha256")} for name,v in files.items()}}}}
    if signed:
        snapshot["units"][unit]["receipt"]=signed["id"]
    write_json(run/"snapshot.json",snapshot)
    write_json(run/"review_result.json",{"unit":unit,"operator_root":str(root),"last_seq":cutoff,
               "policy":record["policy"],"request_id":record["request_id"],"receipt":signed,"files":files,
               "native_paths":physical,"status":snapshot["units"][unit]["status"],"usage":_usage(root)})
    export_graph(root)
    return snapshot


@contextmanager
def _lock(run):
    with (run/".curated-review.lock").open("a+") as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield


def execute(run,config=None,*,script=None,max_calls=None):
    run=Path(run).resolve()
    if bool(config)==bool(script):
        raise ValueError("Choose exactly one private model configuration or an offline action script")
    with _lock(run):
        record=_load(run/"input_receipt.json")
        base=run/"before/instructions"
        root=run/"operator"
        bridge=Bridge(root)
        if bridge.ctx["request_id"]!=record["request_id"]:
            raise ValueError("Request identity changed")
        if any(record["request_id"] in n["parents"] for n in bridge.dag.nodes("stage_submission")):
            return export(run)
        configuration=Path(config or script).resolve()
        budget=(int(_load(configuration)["candidate_max_turns_per_stage"]) if config else len(_load(configuration))+1)
        budget=budget if max_calls is None else max_calls
        if type(budget)!=int or budget<1:
            raise ValueError("A positive turn budget is required")
        settings={"engine":"live" if config else "script","configuration_sha256":_sha(configuration),"max_turns":budget,
                  "candidate_code_sha256":candidate_fingerprints()}
        mode=run/"execution_settings.json"
        if mode.exists() and _load(mode)!=settings:
            raise ValueError("Execution configuration differs from previous sessions of this arm")
        if not mode.exists():
            write_json(mode,settings)
        original=base/f"T{record['stage']:02d}.md"
        if _sha(original)!=record["inputs"]["instructions"][original.name]:
            raise ValueError("Frozen task text changed")
        instruction=(original.read_text()+"\nReview the existing native files at the requested paths for the above assignment. "
                     "Use artifact_propose(action='patch', path=...) with no changes to inspect their actual current bytes. "
                     "Then apply only evidenced edits using artifact_repair or propose a located patch. "
                     "When the existing bytes are correct, revalidate them unchanged. For mirrors copy from the revalidated primary. "
                     "Preserve historical inputs. Sign only supported outputs and submit this review using finish_stage.")
        info={"stage":record["stage"],"stage_id":f"review-{record['unit']}","instruction":instruction}
        start=time.monotonic()
        error=None
        try:
            if config:
                run_stage(root,bridge.ctx,info,configuration,budget)
            else:
                if (root/"sessions").exists() and any((root/"sessions").iterdir()):
                    raise ValueError("An offline script may be dispatched only once per fresh run")
                invoke(root,stage_prompt(bridge.ctx,info,bridge.dag),None,max_calls=budget,label=info["stage_id"],script=_load(configuration))
        except DSHSessionError as exc:
            error=str(exc)
        finally:
            snapshot=export(run)
            result=_load(run/"review_result.json")
            result.update(execution_seconds=time.monotonic()-start,session_error=error,
                          code_unchanged_during_execution=settings["candidate_code_sha256"]==candidate_fingerprints())
            write_json(run/"review_result.json",result)
        return snapshot


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest="command",required=True)
    p=commands.add_parser("prepare")
    p.add_argument("--checkpoint",type=Path,required=True)
    p.add_argument("--run",type=Path,required=True)
    p.add_argument("--policy",choices=REVIEW_POLICIES,required=True)
    x=commands.add_parser("execute")
    x.add_argument("--run",type=Path,required=True)
    mode=x.add_mutually_exclusive_group(required=True)
    mode.add_argument("--config",type=Path)
    mode.add_argument("--script",type=Path)
    x.add_argument("--max-turns",type=int)
    e=commands.add_parser("export")
    e.add_argument("--run",type=Path,required=True)
    a=parser.parse_args()
    if a.command=="prepare":
        r=prepare(a.checkpoint,a.run,a.policy)
        print(json.dumps({k:r[k] for k in ("unit","policy","request_id","last_seq")}))
    elif a.command=="execute":
        snapshot=execute(a.run,a.config,script=a.script,max_calls=a.max_turns)
        print(json.dumps({"unit_status":next(iter(snapshot["units"].values()))["status"],"snapshot":str(a.run/"snapshot.json")}))
    else:
        export(a.run)
        print(a.run/"snapshot.json")


if __name__=="__main__":
    main()

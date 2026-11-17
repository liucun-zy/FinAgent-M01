"""Read-only legacy checkpoint adapter for side-by-side curated POC trials.

Legacy records retain their original IDs, endpoint directions, evidence basis and
legacy knowledge sequence. Only exact, unambiguous quote matches from active
ledger rows are projected as DSH claims. A new snapshot read is distinguished
from an original reader action. No missing legacy edge or stage seal is inferred.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import shutil

from .dsh_bridge import open_stage, write_json
from .financial_projection import bind_current_model
from .guarded_artifacts import ArtifactOperator, SUPPORTED
from .live_review import REVIEW_POLICIES
from .task_obligations import register_obligations
from .temporal_dag import TemporalDAG
from .tools import digest, read_document


def _lines(p):
    with p.open() as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _load(p):
    return json.loads(p.read_text())


def _hashtext(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _identity(record):
    return record["event_id"] if "event_id" in record else record["id"]


def _inputs(checkpoint):
    """Inspect only candidate/; no path into grader, rubric, evaluation or reference."""
    return {role:{f.relative_to(checkpoint/"output"/role).as_posix():digest(f)
                  for f in sorted((checkpoint/"output"/role).rglob("*")) if f.is_file()}
            for role in ("sources","history","instructions","submission")}


def _stage_from_history(path):
    match=re.match(r"^T(\d\d)/",path)
    if not match:
        raise ValueError("Unexpected historical checkpoint path")
    return int(match[1])


def _legacy_source_key(payload, released):
    locator = payload["path"]
    if locator.startswith("source/"):
        path, match, fragment = locator[7:].partition("#")
        span = re.fullmatch(r"chars=(\d+):(\d+)", fragment)
        if not match or not span or path not in released:
            raise ValueError("Legacy business window lacks a released document and exact locator")
        start, end = map(int, span.groups())
        text = released[path]
        if not (0 <= start < end <= len(text)) or text[start:end] != payload["text"]:
            raise ValueError("Legacy business window disagrees with released original")
        return "legacy-window:" + payload["source_id"]
    # Intermediate outputs can legitimately differ from the later checkpoint
    # at the same path. Their recorded text remains a historical observation.
    if not locator.startswith(("working/", "deliverables/", "artifact_version/")):
        raise ValueError("Unexpected origin of a legacy source window")
    return "native:legacy-window:" + payload["source_id"]


def _restore_history(root, checkpoint, stage, released):
    memory=checkpoint/"memory"
    graph, ledger = _load(memory/"action_graph.json"), _load(memory/"ledger.json")
    events, artifacts = _lines(memory/"trace_events.jsonl"), _lines(memory/"artifact_events.jsonl")
    if ledger["stage"]!=stage or graph["stage"]!=stage or any(n["stage"]>stage for n in events+artifacts+graph["nodes"]):
        raise ValueError("Wrong or future legacy checkpoint")
    if [e["sequence"] for e in events]!=list(range(1,len(events)+1)) or len({_identity(e) for e in events})!=len(events):
        raise ValueError("Legacy event order or identity is invalid")
    nodes = {n["id"]:n for n in graph["nodes"]}
    if len(nodes)!=len(graph["nodes"]):
        raise ValueError("Duplicate legacy node")
    dag=TemporalDAG(root)
    active={row["claim_id"]:row for row in ledger["claims"]}
    if len(active)!=len(ledger["claims"]) or any(nid not in nodes or nodes[nid]["kind"]!="claim" for nid in active):
        raise ValueError("Invalid legacy ledger identity")
    node_for_event=defaultdict(list)
    for key,row in nodes.items():
        seq=row["created_event_sequence"]
        if type(seq) is not int or not 1<=seq<=len(events):
            raise ValueError("No creation event for legacy graph node")
        node_for_event[seq].append(row)
    translated,source_ids,projected,held={},{},{},{}
    for e in events:
        # Original event and graph rows keep the legacy sequence (knowledge time)
        # even if snapshot-only data later enriches the row's status projection.
        event=dag.append("legacy_event",{"legacy_id":e["event_id"],"sequence":e["sequence"],
                      "legacy_kind":e["kind"],"stage":e["stage"],"recorded_at":e["recorded_at_utc"]})
        for old in node_for_event[e["sequence"]]:
            n=dag.append("legacy_node",{"legacy_id":old["id"],"original":old,
                          "legacy_sequence":e["sequence"],"stage":old["stage"]},[event["id"]])
            translated[old["id"]]=n["id"]
        if e["kind"]=="source_registered":
            p=e["payload"]; sid=p["source_id"];text=p["text"]
            source_key=_legacy_source_key(p,released)
            if (sid not in nodes or nodes[sid]["kind"]!="source" or nodes[sid]["path"]!=p["path"]
                    or nodes[sid]["created_event_sequence"]!=e["sequence"] or nodes[sid]["stage"]!=e["stage"]
                    or nodes[sid]["sha256_text_utf8"]!=p["sha256_text_utf8"] or _hashtext(text)!=p["sha256_text_utf8"]):
                raise ValueError("Archived source window provenance mismatch")
            source_ids[sid]=dag.release_source(e["stage"],p["path"],text,source_key=source_key)["id"]
        if e["kind"]=="claim_created" and e["payload"]["claim_id"] in active:
            cid=e["payload"]["claim_id"];row=active[cid]
            if row.get("status")!="active" or not row.get("structural_pass"):
                held[cid]="Legacy ledger does not assert an active structurally checked row"
                continue
            matches=row.get("quote_matches",[])
            if len(matches)!=1 or matches[0].get("match_mode") not in ("exact","whitespace_normalized"):
                held[cid]="Legacy source attribution is ambiguous or non-exact; original row retained"
                continue
            m=matches[0]; sid=m["source_id"]; origin=dag.get(source_ids[sid]); quote=m["quote"]
            same_quote = (quote == row["quote"] if m["match_mode"] == "exact" else
                          " ".join(quote.split()) == " ".join(row["quote"].split()))
            if (not same_quote or m["end_char_exclusive"]!=m["start_char"]+len(quote)
                    or origin["payload"]["text"][m["start_char"]:m["end_char_exclusive"]]!=quote):
                raise ValueError("Original claim quotation and offset disagree with its legacy source window")
            new=dag.record_claim(origin["id"],row["entity"],row["description"],row["description"],quote,
                  field=row["field"],value=row["value"],start=m["start_char"],valid_from=row.get("valid_from"),
                  valid_to=row.get("valid_to"), scope={"legacy_scope":row["scope"]},
                  parents=[translated[cid]])
            projected[cid]=new["id"]
    if translated.keys()!=nodes.keys() or projected.keys()|held.keys()!=active.keys():
        raise ValueError("Legacy ledger or graph was not fully accounted for")
    edge_kinds=Counter()
    for edge in graph["edges"]:
        src,dst=edge["from"],edge["to"]
        if src not in translated or dst not in translated:
            raise ValueError("A legacy edge references an absent endpoint")
        dag.append("legacy_edge",{"original":edge,"from_projection":translated[src],
                   "to_projection":translated[dst],"stage":stage},[translated[src],translated[dst]])
        edge_kinds[(edge["kind"],edge["evidence_basis"])]+=1
    archive = root.parent/"before"/"legacy_memory"
    shutil.copytree(memory,archive)
    native_versions=0
    for row in artifacts:
        parents=[translated[i] for i in (row.get("source_ids",[])+row.get("claim_ids",[])) if i in translated]
        for moment in ("before","after"):
            h=row.get(moment+"_sha256")
            if h:
                path=archive/"artifact_versions"/row["id"]/(moment+Path(row["path"]).suffix.lower())
                if not path.is_file() or digest(path)!=h:
                    raise ValueError("Missing or modified legacy native event version: "+row["id"])
                native_versions+=1
        dag.append("legacy_artifact_action",{"original":row,"stage":row["stage"],
                   "association":"recorded input IDs; original legacy artifact action"},parents)
    receipt={"node_ids":translated,"active_claim_projection":projected,"active_unprojected":held,
             "legacy_source_projection":source_ids,
             "source_verification":{"business_windows_rechecked":sum(not dag.get(i)["payload"]["source_key"].startswith("native:") for i in source_ids.values()),
                    "native_windows":"Archived read window of an earlier version; not treated as a current business original."},
             "original_graph_edges":len(graph["edges"]),"native_version_files":native_versions,
             "archived_trace_events":len(events),"archived_artifact_events":len(artifacts),
             "edge_basis_counts":[{"kind":k,"evidence_basis":b,"count":v} for (k,b),v in sorted(edge_kinds.items())],
             "legacy_cutoff_sequence":events[-1]["sequence"],"true_legacy_stage_seal":False,
             "compatibility":"Legacy edge records retain direction and model-declared versus observed basis. DSH native ranking uses exact original-window projections; other legacy edge types remain historical relationship records.",
             "memory_sha256":{n:digest(archive/n) for n in ("action_graph.json","ledger.json","artifact_events.jsonl","trace_events.jsonl")}}
    return dag,receipt


def prepare(checkpoint,run,policy):
    checkpoint=Path(checkpoint).absolute();run=Path(run).resolve()
    if (checkpoint!=checkpoint.resolve() or not checkpoint.is_dir() or checkpoint.parent.name!="demo"
            or checkpoint.parent.parent.name!="candidate" or not re.fullmatch(r"T\d{2}|FINAL",checkpoint.name)
            or any(f.is_symlink() for f in checkpoint.rglob("*"))):
        raise ValueError("Choose a candidate/demo checkpoint without symbolic links")
    if run.exists() or run.is_relative_to(checkpoint.parents[2]) or policy not in REVIEW_POLICIES:
        raise ValueError("Use a fresh external run directory and valid review policy")
    unit=checkpoint.name;stage=13 if unit=="FINAL" else int(unit[1:])
    inventory=_inputs(checkpoint)
    for role in ("sources","instructions","submission"):
        if not inventory[role]:
            raise ValueError("Missing candidate input: "+role)
    if any(_stage_from_history(p)>stage for p in inventory["history"]):
        raise ValueError("Future stage in native history")
    run.mkdir(parents=True)
    released={name:read_document(checkpoint/"output/sources"/name) for name in inventory["sources"]}
    root=run/"operator";dag,legacy=_restore_history(root,checkpoint,stage,released)
    art=ArtifactOperator(root,dag); public=[]
    for role, binding in inventory.items():
        folder=checkpoint/"output"/role; out=run/"before"/role
        if folder.is_dir():
            shutil.copytree(folder,out)
        else:
            out.mkdir()
        if role in ("instructions","sources"):
            for name in binding:
                if role=="instructions":
                    tid="OPENING" if name=="project.md" else Path(name).stem
                    if tid!="OPENING" and (not re.fullmatch(r"T\d{2}",tid) or int(tid[1:])>stage):
                        raise ValueError("Future instruction")
                    locator="instructions/"+tid
                else:
                    locator="source/"+name
                public.append((locator,released[name] if role=="sources" else read_document(out/name)))
    for name in inventory["history"]:
        art.seed("working/checkpoints/"+name,run/"before/history"/name,purpose="immutable prior checkpoint",
                 description="Native snapshot supplied in the historical candidate context.",stage=_stage_from_history(name))
    logical={name: ("deliverables/final/" if unit=="FINAL" else f"working/checkpoints/T{stage:02d}/")+name
             for name in inventory["submission"]}
    # In the legacy T13 archive a final-folder letter may differ from the
    # graded T13 checkpoint. FINAL is an independent snapshot. Never attach
    # the checkpoint's bytes to the other path or label them a historical mirror.
    expected=list(logical.values())
    heads={}
    for path in expected:
        name=Path(path).name
        heads[path]=art.seed(path,run/"before/submission"/name,purpose="current file under review",
                    description="Frozen candidate input from the original checkpoint.",stage=stage)["id"]
    # This adapter knows which source originals are in the frozen snapshot. It
    # does not guess a release date for a file the old agent never read.
    unavailable=[]
    current_sources={}
    for locator,text in public:
        source=dag.release_source(stage,locator,text,source_key=f"{stage}/{locator}")
        current_sources[locator]=source
        if locator.startswith("source/"):
            path=run/"before/sources"/locator.removeprefix("source/")
            if path.suffix.lower() not in SUPPORTED:
                continue
            try:
                art.seed(locator,path,purpose="released source snapshot",description="Native file known by this checkpoint.",
                         stage=stage,parents=[source["id"]])
            except ValueError as exc:
                if str(exc)!="Native annotation inspection incomplete":
                    raise
                unavailable.append(locator)
    # Prior task text can still authorize preservation of original analysis.
    # Explicit stage/history file membership comes from the sealed candidate
    # snapshot. The release cutoff used here is a new projection, never an old seal.
    instructions=dict(public)
    history=defaultdict(list)
    for path in inventory["history"]:
        history[_stage_from_history(path)].append("working/checkpoints/"+path)
    for st,paths in sorted(history.items()):
        key=f"instructions/T{st:02d}"
        if st>=stage or key not in instructions:
            continue
        original=current_sources[key]
        projected=dag.append("legacy_requirement_context",{"stage":st,"instruction":instructions[key],
                             "expected":paths,"source_id":original["id"]},[original["id"]])
        register_obligations(dag,projected,[],paths)
    boundary=dag.append("legacy_checkpoint_import",{"stage":stage,"archived_graph":legacy["memory_sha256"]["action_graph.json"],
                        "legacy_cutoff_sequence":legacy["legacy_cutoff_sequence"],
                        "description":"Imported original candidate snapshot; a historical stage signature is unavailable."})
    ctx=open_stage(root,stage,instructions[f"instructions/T{stage:02d}"],expected,public,
                   parents=[boundary["id"]],self_review=True,review_policy=policy)
    bind_current_model(root,ctx)
    legacy["original_native_snapshot_unavailable"]=unavailable
    write_json(run/"legacy_import.json",legacy)
    record={"version":"demo","unit":unit,"policy":policy,"operator_root":str(root),"checkpoint":str(checkpoint),
            "stage":stage,"request_id":ctx["request_id"],"expected":expected,"last_seq":boundary["seq"],
            "last_historical_id":boundary["id"],"graph_sha256":legacy["memory_sha256"]["action_graph.json"],
            "ledger_sha256":legacy["memory_sha256"]["ledger.json"],"original_paths":logical,
            "original_file_versions":heads,"inputs":inventory,"baseline_stage_submission":None,
            "legacy_import":"legacy_import.json"}
    write_json(run/"input_receipt.json",record)
    return record

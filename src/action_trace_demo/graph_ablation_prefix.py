"""Pre-decision views of the persisted Mars action graph for the paired ablation.

The only hand registration is the original consumer/task for each previously
frozen question. There are no registered claims, source files or answer windows.
Original proposal receipts supply the boundary, declared input claim IDs and
their ordering. The completed proposal, imported domain projections, its
outputs and later graph events are outside the view.
"""
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re


# Real consumer of the disputed field. M09 is a retrospective question at
# M08's timestamp, with no observed write proposal of its own.
CONSUMERS = {
    "M01": "QoE_report_Alpha_FY2025_final.pdf",
    "M02": "insurance_program_summary.docx",
    "M03": "environmental_and_permitting_summary.pdf",
    "M04": "platform_refinancing_term_sheet_v5.docx",
    "M05": "platform_refinancing_term_sheet_v5.docx",
    "M06": "Alpha_Platform_Model_v4.xlsx",
    "M07": "lender_syndication_update.pdf",
    "M08": "Platform_Model_v4 BaseSpread.xlsx",
    "M09": None,
}

STOP = set(("a an at and by for from in of the to with on is into between "
            "as old new both each pending existing original already record current "
            "proposed confirm which resolve ledger update status fields field value").split())


def tokens(text):
    # Identifiers retain entity and business-field names. Never tokenize a
    # historical claim's asserted value, description, quote or source filename.
    words = re.findall(r"[a-z]+", text.casefold())
    return {w[:-1] if len(w) > 4 and w.endswith("s") else w
            for w in words if len(w) > 1 and w not in STOP}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _bounded(value, depth=0):
    if isinstance(value, str):
        return value[:100] + ("…" if len(value) > 100 else "")
    if isinstance(value, (list, dict)) and depth >= 3:
        return {"omitted_container": type(value).__name__, "items": len(value)}
    if isinstance(value, list):
        return [_bounded(x, depth+1) for x in value[:8]] + ([{"omitted":len(value)-8}] if len(value)>8 else [])
    if isinstance(value, dict):
        keys = sorted(value)
        return {**{k:_bounded(value[k],depth+1) for k in keys[:8]},
                **({"_omitted_keys":len(keys)-8} if len(keys)>8 else {})}
    return value


def _boundary(episode, transcript, graph):
    name = CONSUMERS[episode["id"]]
    chronology = {r["tool_call_id"]:r["result"].get("action_id") for r in transcript
                  if r.get("tool_call_id") and isinstance(r.get("result"),dict)}
    artefacts = {n["payload"]["id"]:n for n in graph["nodes"] if n["kind"]=="historical_artifact_action"}
    for line,r in enumerate(transcript,1):
        if r.get("role")!="assistant" or r.get("stage")!=episode["stage"]:
            continue
        for call in r["message"].get("tool_calls",[]):
            fn=call["function"]
            if fn["name"] not in {"write_artifact","copy_file"}:
                continue
            args = json.loads(fn["arguments"])
            consumer = Path(args.get("path","")).name
            if consumer != (name or CONSUMERS["M08"]):
                continue
            # The foreign receipt ID is used exclusively to establish the upper
            # bound before this write; never include the resulting event or bytes.
            limit = artefacts.get(chronology.get(call["id"]))
            if not limit:
                if episode["stage"]!=13 or line!=605:
                    raise ValueError("No registered before-write boundary")
                # The historical import stops before the observed T13 copy;
                # open_stage starts the replay and all following nodes are excluded.
                limit = next(n for n in graph["nodes"] if n["kind"]=="stage" and n["payload"]["stage"]==13)
            proposal = {"relation":"observed_consumer" if name else "snapshot_only_no_consumer",
                        "tool":fn["name"],"stage":r["stage"],"turn":r["turn"],"line":line,"call_id":call["id"],
                        "path":args["path"],"copy_source":args.get("source"),
                        "legacy_claim_ids":args.get("claim_ids",[]) if name else []}
            return limit["seq"]-1, proposal
    raise ValueError("Cannot locate the first original operation for the registered consumer")


def _source_range(node):
    p=node["payload"]
    m=re.fullmatch(r"(.+)#chars=(\d+):(\d+)", p["locator"])
    if not m:
        raise ValueError(f"Missing physical source coordinates: {node['id']}")
    lo,hi=map(int,m.groups()[1:])
    if hi-lo!=len(p["text"]) or _sha(p["text"].encode())!=p["sha256"]:
        raise ValueError("Historical source changed")
    return m[1],lo,hi


def _trace_slice(graph, cutoff):
    selected = {}
    active = {}
    for n in graph["nodes"]:
        if n["seq"]>cutoff:
            break
        p=n["payload"]
        kind=n["kind"]
        include=kind in {"source","historical_decision","historical_artifact_action"}
        if kind=="source":
            include=str(p.get("source_key","")).startswith("legacy:")
        if kind=="claim":
            # Only model-authored historical assertions materialized before the
            # decision. Imported finance adapter projections do not enter.
            include=bool(p.get("scope",{}).get("legacy_id"))
        if not include:
            continue
        selected[n["id"]]=n
        if kind=="historical_decision" and p.get("status")=="accepted":
            if p.get("op") in ("retract","update"):
                active.pop(p.get("old_claim_id"),None)
            if p.get("op") in ("create","update") and p.get("claim_id"):
                for pid in n["parents"]:
                    parent=selected.get(pid)
                    if parent and parent["kind"]=="claim" and parent["payload"]["scope"]["legacy_id"]==p["claim_id"]:
                        active[p["claim_id"]]=parent
    edges=[e for e in graph["edges"] if e["from"] in selected and e["to"] in selected]
    return selected,edges,active


def _claim_fields(node):
    p=node["payload"]
    keys=p["value"].keys() if isinstance(p.get("value"),dict) else ()
    return tokens(" ".join([p["entity"],p["field"],*keys]))


def _anchors(episode,active,proposal):
    fields={r["id"]:_claim_fields(r) for r in active.values()}
    frequency=Counter(t for doc in fields.values() for t in doc)
    side_matches={}
    choices={}
    for side in ("old","new"):
        side_text = episode[side]
        # Both hypotheses, neither action descriptions nor answers, are public
        # query inputs. Matches are field/entity candidates, never equality of
        # the old or disputed value.
        terms=tokens(" ".join([episode["query"], side_text["entity"], side_text["claim"]]))
        ranked=[]
        for legacy,n in active.items():
            shared=terms & fields[n["id"]]
            if len(shared)<2:
                continue
            score=sum(math.log1p(len(fields)/frequency[k]) for k in sorted(shared))
            ranked.append((score,n["id"],legacy,sorted(shared)))
        ranked.sort(key=lambda x:(-x[0],x[1]))
        side_matches[side]=[]
        for score,nid,legacy,terms_matched in ranked[:1]:
            side_matches[side].append({"id":nid,"field_match":score,"matched_tokens":terms_matched,
                                       "status":"field_candidate_not_value_verified"})
            choices.setdefault(nid,{"id":nid,"role":"ledger_field","origin":"visible_ledger_field_name_overlap",
                                    "field_match":score,"match_tokens":terms_matched,"sides":[]})["sides"].append(side)
    for original in proposal["legacy_claim_ids"]:
        n=active.get(original)
        if n:
            a=choices.setdefault(n["id"],{"id":n["id"],"sides":[],"field_match":0., "match_tokens":[]})
            a.update(role="proposal_claim",origin=f"historical proposal line {proposal['line']}: claim_ids included {original}")
    # The order of the recorded proposal is available to both arms and is not
    # interpreted as a vote by another ranker.
    anchors=sorted(choices.values(),key=lambda a:(a["role"]!="proposal_claim",-a["field_match"],a["id"]))
    by_id={n["id"]:n for n in active.values()}
    ledger=[]
    for a in anchors[:8]:
        n=by_id[a["id"]];p=n["payload"]
        ledger.append({"id":n["id"],"known_at":n["seq"],"stage":p["stage"],"entity":p["entity"],"field":p["field"],
                       "value_preview":_bounded(p["value"]),"legacy_id":p["scope"]["legacy_id"],
                       "evidence_status":"historical_model_assertion_requires_original"})
    return anchors,side_matches,ledger


def _view(graph,sha,path,record_hash,transcript,episode):
    cutoff,proposal=_boundary(episode,transcript,graph)
    selected,edges,active=_trace_slice(graph,cutoff)
    anchors,sides,ledger=_anchors(episode,active,proposal)
    nodes=[]; witnesses={}; source_meta={}
    for n in selected.values():
        p=n["payload"]
        info={k:n[k] for k in ("id","seq","kind")}
        info.update(stage=p.get("stage"),parents=[x for x in n["parents"] if x in selected])
        if n["kind"]=="source":
            loc,lo,hi=_source_range(n)
            source_meta[n["id"]]=(loc,lo,hi)
            info.update(path=loc,start=lo,end=hi)
        elif n["kind"]=="claim":
            loc,base,_=source_meta[p["source_id"]]
            claim_range=(base+p["start"],base+p["end"])
            chunk=selected[p["source_id"]]["payload"]["text"]
            if chunk[p["start"]:p["end"]]!=p["quote"]:
                raise ValueError("Unverified historical quote offsets")
            info.update(entity=p["entity"],field=p["field"],source_id=p["source_id"],
                        legacy_id=p["scope"]["legacy_id"],start=claim_range[0],end=claim_range[1],
                        active=p["scope"]["legacy_id"] in active)
            witnesses.setdefault(loc,[]).append(info)
        elif n["kind"]=="historical_artifact_action":
            info.update(path=p["path"],legacy_id=p["id"])
        nodes.append(info)
    endpoints={}; window_bindings={}
    for w in episode["windows"]:
        loc,lo,hi=source_meta[w["source_id"]]
        start,end=w["start"],w["end"]
        body=selected[w["source_id"]]["payload"]["text"][start-lo:end-lo]
        if loc!=w["locator"] or not lo<=start<end<=hi or _sha(body.encode())!=w["window_sha256"]:
            raise ValueError(f"Frozen physical window does not match the visible source: {episode['id']}/{w['id']}")
        window_bindings[w["id"]]={"source_id":w["source_id"],"locator":loc,"start":start,"end":end,
                                  "window_sha256":w["window_sha256"],"source_start":lo,"source_end":hi}
        endpoints[w["id"]]=[{"source_id":c["source_id"],"claim_id":c["id"],
                            "start":c["start"],"end":c["end"],"window_start":start,"window_end":end,
                            "source_start":source_meta[c["source_id"]][1],"source_end":source_meta[c["source_id"]][2],
                            "locator":loc,"window_sha256":w["window_sha256"]}
                            for c in witnesses.get(loc,[]) if start<=c["start"]<c["end"]<=end
                            and selected[c["id"]]["payload"]["quote"]==body[c["start"]-start:c["end"]-start]]
    proposal["anchor_claims"]=[a["id"] for a in anchors if a["role"]=="proposal_claim"]
    return {"id":episode["id"],"stage":episode["stage"],"cutoff":cutoff,"source_graph":str(path),
            "graph_sha256":sha,"transcript_sha256":record_hash,"nodes":nodes,"edges":edges,"anchors":anchors,
            "ledger_context":ledger,"side_matches":sides,"proposal":proposal,"windows":endpoints,
            "window_bindings":window_bindings,"provenance":"historical candidate model assertions and persisted dependencies strictly before the original operation; submitted output, injected domain contracts, adjudications and QA excluded"}


def build_views(graph_path, episodes, *, trajectory_path=None):
    path=Path(graph_path).resolve()
    raw=path.read_bytes()
    graph=json.loads(raw)
    if trajectory_path is None:
        prefix=json.loads((path.parent.parent/"prefix.json").read_text())
        trajectory_path=Path(prefix["baseline"])/"agent_trajectory.jsonl"
    raw_history=Path(trajectory_path).read_bytes()
    # The original stop in the imported graph was the first T13 workbook copy.
    # Outcome fields from later operations are unavailable to any policy.
    transcript=[json.loads(line) for line in raw_history.splitlines()[:605] if line.strip()]
    sha=_sha(raw);history_sha=_sha(raw_history)
    return {e["id"]:_view(graph,sha,path,history_sha,transcript,e) for e in episodes}

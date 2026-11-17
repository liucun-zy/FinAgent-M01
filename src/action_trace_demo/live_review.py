"""Pre-write review from the candidate's current ledger and actually recorded DAG.

First-stage retrieval searches released source blocks. Ledger claims contribute
real quotation offsets; they do not supply new edges. The same path selection
used in the paired ablation operates on a bounded, as-of view.
"""
from __future__ import annotations

from .ablation_selection import shared_bm25_index, rank_windows
from .graph_ablation_rank import rerank

BLOCK = 960


def source_role(payload):
    if payload.get("source_key", "").startswith(("native:", "artifact-observation:")):
        return "native_observation"
    locator = payload["locator"].split("#", 1)[0]
    if locator.startswith("instructions/"):
        return "task_instruction"
    # Project instructions can also be released as a standalone source file.
    # Preserve the locator for citation while excluding these process instructions
    # from business witnesses. A mandate, term sheet or model is not an instruction.
    from pathlib import PurePosixPath
    stem = PurePosixPath(locator).stem.casefold().replace("_", " ").replace("-", " ").strip()
    if stem in {"project working conventions", "working conventions", "项目工作约定"}:
        return "task_instruction"
    return "business_evidence"


REVIEW_POLICIES = ("ledger_only", "ledger_graph")


def _view(dag, query, anchor_ids, skip, *, purpose_role, policy):
    if policy not in REVIEW_POLICIES:
        raise ValueError("Unknown self-review policy")
    from .dsh_project import graph_edge
    from .dag_qa import ledger_search
    nodes = dag.nodes()
    cutoff = nodes[-1]["seq"]
    by_id = {n["id"]: n for n in nodes}
    if not anchor_ids:
        anchor_ids = [n["id"] for n in ledger_search(dag, query, cutoff, limit=3)]
    anchors = [dict(id=i) for i in dict.fromkeys(anchor_ids)
               if i in by_id and by_id[i]["kind"] in {"claim","artifact_proposal","comparison","artifact_version","native_finance_check","artifact_check"}]
    sources = {n["id"]:n for n in nodes if n["kind"] == "source" and
               source_role(n["payload"]) != "native_observation" and
               (purpose_role is None or source_role(n["payload"]) == purpose_role)}
    candidates = {}
    bodies = {}
    witnesses = {}

    def add(sid, lo, hi):
        source = sources[sid]["payload"]
        key = f"{sid}@{lo}:{hi}"
        if key not in candidates and key not in skip and source["text"][lo:hi].strip():
            candidates[key] = {"id":key, "source_id":sid,"start":lo,"end":hi,
                               "locator":source["locator"],"description":""}
            bodies[key] = source["text"][lo:hi]
            witnesses[key] = []
        return key

    for sid, s in sources.items():
        size = len(s["payload"]["text"])
        for i in range(0,size,BLOCK):
            add(sid, i, min(i+BLOCK,size))
    for n in nodes:
        if n["kind"] != "claim":
            continue
        p = n["payload"]
        sid = p["source_id"]
        if sid not in sources:
            continue
        size = len(sources[sid]["payload"]["text"])
        a,b = p["start"],p["end"]
        lo,hi = a//BLOCK*BLOCK, min((a//BLOCK+1)*BLOCK,size)
        if b > hi:
            if b-a > BLOCK:
                continue
            lo,hi = max(0,b-BLOCK),b
        key=add(sid,lo,hi)
        if key in witnesses:
            witnesses[key].append({"claim_id":n["id"],"source_id":sid,"start":a,"end":b,
                                   "window_start":lo,"window_end":hi,"source_start":0,"source_end":size})
    if not candidates:
        return None
    # Distinct released versions can share a locator and character range. Use the
    # immutable source version in the retrieval address and keep the original
    # locator for the review receipt.
    indexed = [{**w, "locator": f"{w['locator']} [{w['source_id']}]"} for w in candidates.values()]
    scores=shared_bm25_index(query,indexed,bodies)
    for key,row in candidates.items():
        row["bm25_score"]=scores[key]
    for w in indexed:
        w["bm25_score"] = scores[w["id"]]
    ordered = [r["id"] for r in rank_windows("relevance", query, indexed)]
    if policy == "ledger_only":
        shortlist = ordered[:12]
        # No traversal, path-guided shortlist expansion or graph metadata is
        # available to this policy. Quotes and the underlying ledger are shared.
        winner = shortlist[0]
        audit = {"cutoff": cutoff, "anchors": anchors, "selected": winner, "baseline": winner,
                 "bm25_order": shortlist, "paths": {winner: []}, "reason": "ledger_bm25",
                 "edge_count": 0, "edge_set_sha256": None,
                 "counterfactual": {"joint": None, "empty_graph_winner": winner}}
        return audit, candidates[winner], by_id
    # Include cited windows reachable from the proposal: sparse
    # lexical retrieval alone can omit the authoritative signing instruction.
    ancestry = {n["id"] for n in dag.ancestors([a["id"] for a in anchors],as_of=cutoff)} if anchors else set()
    cited={k for k,rows in witnesses.items() if any(r["claim_id"] in ancestry for r in rows)}
    retained=set(ordered[:12]) | cited
    shortlist=[k for k in ordered if k in retained]
    eligible_ids = ancestry | {c for k in shortlist for w in witnesses[k] for c in (w["claim_id"],w["source_id"])}
    selected = [n for n in nodes if n["id"] in eligible_ids]
    vnodes=[{"id":n["id"],"seq":n["seq"],"kind":n["kind"],"parents":n["parents"],
             "source_id":n["payload"].get("source_id"),"supports":n["payload"].get("supports"),
             "candidate_ids":n["payload"].get("candidate_ids",[])} for n in selected]
    from .dependency_queries import semantic_edges
    edges = [edge for n in selected for p in n["parents"] if p in eligible_ids
             for edge in semantic_edges(by_id[p],n)]
    view = {"cutoff":cutoff,"nodes":vnodes,"edges":edges,"anchors":anchors,
            "windows":{k:witnesses[k] for k in shortlist}}
    audit=rerank(shortlist,view)
    return audit,candidates[audit["selected"]],by_id


def review(dag, target_id, query, anchors, *, previous=(), stage=None, purpose_role=None, policy="ledger_graph"):
    skip={n["payload"].get("window_id") for n in previous}
    ranked=_view(dag,query,anchors,skip,purpose_role=purpose_role,policy=policy)
    if ranked is None:
        return None
    audit,window,nodes=ranked
    selected=audit["selected"]
    paths=audit["paths"][selected]
    source_id=window["source_id"]
    text=dag.read_excerpt(source_id,window["start"],window["end"]-window["start"])
    ledger=[]
    for i in dict.fromkeys([a["id"] for a in audit["anchors"]]+[p["claim_id"] for p in paths]):
        n=nodes[i]
        if n["kind"]=="claim":
            ledger.append({"id":i,"entity":n["payload"]["entity"],"field":n["payload"]["field"],
                           "claim":n["payload"]["claim"][:300],"valid_from":n["payload"]["valid_from"],
                           "valid_to":n["payload"]["valid_to"]})
    payload={"target_id":target_id,"stage":stage,"query":query,"cutoff":audit["cutoff"],
             "anchor_ids":[a["id"] for a in audit["anchors"]],"ledger":ledger,
             "policy":policy,"source_role":source_role(nodes[source_id]["payload"]),"purpose_role":purpose_role,
             "bm25_first":audit["baseline"],"bm25_order":audit["bm25_order"],"window_id":selected,
             "source_id":source_id,"locator":window["locator"],"start":window["start"],"end":window["end"],
             "text":text,"chars_read":len(text),"paths":paths,"selection_reason":audit["reason"],
             "edge_deletion":audit["counterfactual"]["joint"],
             "empty_graph_first":audit["counterfactual"]["empty_graph_winner"],
             "edge_count":audit["edge_count"],"edge_set_sha256":audit["edge_set_sha256"]}
    parents=list(dict.fromkeys([target_id,source_id,*payload["anchor_ids"],*(p["claim_id"] for p in paths)]))
    return dag.append("ledger_self_review",payload,parents)


def present(node):
    if not node:
        return {"status":"no_more_visible_windows"}
    p=node["payload"]
    return {"id":node["id"], "ledger":p["ledger"][:6],"locator":p["locator"],"source_id":p["source_id"],
            "original":p["text"],"source_role":p.get("source_role"),"start":p["start"],"end":p["end"],"selection_reason":p["selection_reason"],
            "paths":[path["edges"] for path in p["paths"][:3]],
            "instruction":"Check this original against the proposal's date, entity and use. For a ready "
                          "artifact_commit send review_id, review_quote copied verbatim from original, "
                          "and review_note stating your decision and limitations. For another aspect use "
                          "artifact_review with a focused query. If a claim conflicts, repair before committing."}

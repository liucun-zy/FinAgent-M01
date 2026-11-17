"""Ledger-guided pre-commit self-review on a frozen BM25 shortlist and temporal DAG."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import random
import shutil

from .ablation_run import DEMO, code_bindings, file_digest, fingerprint, live_config, load, one_trial
from .dsh_bridge import write_json

ARMS = ("relevance", "graph")
BASELINE = DEMO / "evaluations/evidence_ablation_20261113_01"
RUNS = DEMO / "runs"
GRAPH = RUNS / "dsh_mars_replay_20261113_01/operator/action_graph.json"
TRAJECTORY = RUNS / "mars_eval_20261113_02/agent_trajectory.jsonl"
FILES = ("fixtures_episodes.json", "fixtures_texts.json", "fixtures_gold.json")
MODULES = ("graph_ablation_prefix", "graph_ablation_rank", "graph_ablation_run")


def implementation():
    hashes = code_bindings()
    for name in MODULES:
        path = Path(__file__).with_name(name + ".py")
        hashes[str(path.relative_to(DEMO))] = file_digest(path)
    return hashes


def reviewed_episode(episode, ledger):
    # Keep the opening claims, proposed operations and their patches unchanged.
    # Both arms get precisely the same additional pre-decision ledger entries.
    review = {"notice":"These entries are recorded historical model assertions. Check their "
              "entity, business scope and use against the purchased original before confirming "
              "or changing the current ledger. Historical entries can be wrong.",
              "entries":ledger}
    return {**episode, "old":{**episode["old"], "recorded_ledger_for_review":review}}


def verify_edge_intervention(view, ranking, raw_edges):
    from .graph_ablation_rank import rerank
    def identity(edge):
        return f"{edge['from']}/{edge['kind']}/{edge['to']}"
    if any(identity(edge) not in raw_edges for edge in view["edges"]):
        raise ValueError("Review prefix contains an edge absent from the frozen trajectory")
    baseline = ranking["bm25_order"]
    empty = rerank(baseline,{**view,"edges":[]})
    if empty["order"] != baseline:
        raise ValueError("An edge-free review changed the shortlist")
    tests = []
    if ranking["changed"]:
        for deletion in ranking["counterfactual"]["individual"]:
            dropped = {deletion["removed_edge"]}
            modified = {**view, "edges":[e for e in view["edges"] if identity(e) not in dropped]}
            result = rerank(baseline,modified)
            if result["selected"] != deletion["winner"]:
                raise ValueError("Path-deletion and actual edge-deletion disagree")
            tests.append({"removed":sorted(dropped),"selected":result["selected"]})
        joint = set(ranking["counterfactual"]["joint"]["removed_edges"])
        modified = {**view,"edges":[e for e in view["edges"] if identity(e) not in joint]}
        if not joint or rerank(baseline,modified)["order"] != baseline:
            raise ValueError("Deleting the recorded witness edges did not restore BM25")
    return {"all_edges_removed":empty["order"],"individual_retraversals":tests,"joint_restoration":True}


def prepare(out, baseline, graph_path, trajectory_path, config):
    from .graph_ablation_prefix import build_views
    from .graph_ablation_rank import rerank
    if out.exists():
        raise FileExistsError("Use a fresh experiment directory")
    old = load(baseline/"manifest.json")
    if old["implementation"] != code_bindings() or old["config_sha256"] != fingerprint(live_config(config)):
        raise ValueError("The model, decision adapter or source-index implementation changed since the paired run")
    for name in (*FILES,"rankings.json"):
        if file_digest(baseline/name) != old["inputs"][name]:
            raise ValueError(f"Frozen baseline input changed: {name}")
    episodes = load(baseline/FILES[0])
    bm25 = {key: value["relevance"] for key, value in load(baseline/"rankings.json").items()}
    original_edges = {f"{e['from']}/{e['kind']}/{e['to']}" for e in load(graph_path)["edges"]}
    views = build_views(graph_path,episodes,trajectory_path=trajectory_path)
    if set(views) != set(bm25):
        raise ValueError("Missing chronological graph views")
    out.mkdir(parents=True)
    for name in FILES:
        shutil.copyfile(baseline/name,out/name)
    write_json(out/"bm25.json",bm25)
    review_inputs, audits = {}, {}
    for episode in episodes:
        key = episode["id"]
        view = views[key]
        order = [r["id"] for r in bm25[key]]
        if set(order) != {w["id"] for w in episode["windows"]} or len(set(order)) != len(order):
            raise ValueError("The comparison must preserve the full original shortlist")
        if key not in views or "ledger_context" not in view:
            raise ValueError("Missing pre-action ledger snapshot")
        ranking = rerank(order,view)
        if set(ranking["order"]) != set(order) or len(ranking["order"]) != len(order):
            raise ValueError("Graph reranking changed the candidate set")
        review_inputs[key] = reviewed_episode(episode,view["ledger_context"])
        audits[key] = {**ranking, "cutoff":view["cutoff"], "anchors":view["anchors"],
                       "edge_retraversal":verify_edge_intervention(view, ranking, original_edges),
                       "ledger_review":view["ledger_context"], "prefix_meta":view.get("provenance",{}),
                       "side_matches":view.get("side_matches"), "proposal":view.get("proposal"),
                       "ledger_sha256":fingerprint(review_inputs[key]["old"]["recorded_ledger_for_review"])}
    write_json(out/"graph_views.json",views)
    write_json(out/"graph_audits.json",audits)
    write_json(out/"ledger_inputs.json",review_inputs)
    bound = (*FILES,"bm25.json","graph_views.json","graph_audits.json","ledger_inputs.json")
    write_json(out/"manifest.json",{
        "protocol":"same-ledger one-window self-review; frozen BM25 versus edge-only reranking",
        "model":old["model"], "temperature":old["temperature"], "max_output_tokens":old["max_output_tokens"],
        "read_budget":old["read_budget"], "config_sha256":old["config_sha256"],
        "graph_path":str(graph_path), "graph_sha256":file_digest(graph_path),
        "trajectory_path":str(trajectory_path), "trajectory_sha256":file_digest(trajectory_path),
        "baseline_dir":str(baseline), "prior_bm25_file_sha256":old["inputs"]["rankings.json"],
        "prior_manifest_sha256":file_digest(baseline/"manifest.json"),
        "decision":"Original DSH commit_repair; fresh independent completions, identical ledger, "
                   "proposal, action set and prompt within each pair; one source excerpt.",
        "intervention":"Bidirectional exchange for every changed first window. For identical selections, "
                       "the two fresh arms are same-input replications.",
        "implementation":implementation(), "inputs":{name:file_digest(out/name) for name in bound}})


def run(out,config,workers):
    manifest = load(out/"manifest.json")
    if (implementation()!=manifest["implementation"] or fingerprint(live_config(config))!=manifest["config_sha256"]
            or any(file_digest(out/k)!=v for k,v in manifest["inputs"].items())
            or any(file_digest(Path(manifest[k+"_path"]))!=manifest[k+"_sha256"] for k in ("trajectory","graph"))):
        raise ValueError("A frozen input, chronological graph, model or program changed")
    episodes = load(out/"ledger_inputs.json")
    ranks = load(out/"graph_audits.json")
    baseline = load(out/"bm25.json")
    texts = load(out/"fixtures_texts.json")
    settings = out/".model.local.json"
    write_json(settings,live_config(config))
    os.chmod(settings,0o600)
    picks = {k:{"relevance":baseline[k][0]["id"], "graph":r["order"][0]} for k,r in ranks.items()}
    rng = random.Random(20261113)

    def execute(item):
        key,side,donor,control=item
        episode=episodes[key]
        wid=picks[key][donor]
        w=next(window for window in episode["windows"] if window["id"]==wid)
        name=("swap-" if control else "arm-")+side
        trace=ranks[key]
        # Persist the selection before dispatch. The candidate sees the selected
        # original, not the ranker's witness path or a counterfactual's donor.
        dispatch_path=out/"dispatch"/key/(name+".json")
        decision={"episode_id":key,"arm":side,"selector":donor,"window_id":wid,
                  "ledger_sha256":trace["ledger_sha256"], "cutoff":trace["cutoff"],
                  "source_graph_sha256":manifest["graph_sha256"], "audit_sha256":fingerprint(trace),
                  "paths_used":trace["paths"][wid] if donor=="graph" else []}
        if dispatch_path.exists() and load(dispatch_path)!=decision:
            raise ValueError("An existing dispatch changed before a retried decision")
        if not dispatch_path.exists():
            write_json(dispatch_path,decision)
        row=one_trial(out,episode,w,texts[key+"/"+wid],name,settings)
        row={**row, "dispatch":str(dispatch_path.relative_to(out)), "ledger_sha256":trace["ledger_sha256"]}
        if control:
            return {**row, "side":side,"replace_index":0,"replicate":1,
                    "baseline_reads":[picks[key][side]],"swapped_reads":[wid]}
        return {**row, "policy":side}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for is_control, name in ((False,"arms.json"),(True,"controls.json")):
            tasks = []
            for key in episodes:
                for i,side in enumerate(ARMS):
                    other=ARMS[1-i]
                    if not is_control or picks[key][side]!=picks[key][other]:
                        tasks.append((key,side,other if is_control else side,is_control))
            rng.shuffle(tasks)
            write_json(out/name,list(pool.map(execute,tasks)))
    arms=load(out/"arms.json")
    lookup={(x["id"],x["policy"]):x for x in arms}
    for key,choices in picks.items():
        left,right=[lookup[key,a] for a in ARMS]
        if left["prefix_sha256"]!=right["prefix_sha256"]:
            raise ValueError("Factual arms did not receive an identical ledger")
        if choices[ARMS[0]]==choices[ARMS[1]] and left["input_sha256"]!=right["input_sha256"]:
            raise ValueError("Equal source selections must yield equal full model inputs")
    for x in load(out/"controls.json"):
        donor=lookup[x["id"],next(a for a in ARMS if a!=x["side"])]
        if x["input_sha256"]!=donor["input_sha256"]:
            raise ValueError("Crossover input changed beyond the source window")


def report(out):
    from .graph_ablation_report import write_report
    write_report(out,load(out/FILES[0]),load(out/FILES[2]),load(out/"arms.json"),load(out/"controls.json"),
                 load(out/"manifest.json"),load(out/"graph_audits.json"))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("prepare","run","report"))
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--config",type=Path,default=DEMO/"configs/efunds.eval_bounded_final.local.json")
    parser.add_argument("--baseline",type=Path,default=BASELINE)
    parser.add_argument("--graph",type=Path,default=GRAPH)
    parser.add_argument("--trajectory",type=Path,default=TRAJECTORY)
    parser.add_argument("--workers",type=int,choices=range(1,5),default=3)
    args=parser.parse_args()
    out=args.out.resolve()
    if args.command=="prepare":
        prepare(out,args.baseline.resolve(),args.graph.resolve(),args.trajectory.resolve(),args.config)
    elif args.command=="run":
        run(out,args.config,args.workers)
        report(out)
    else:
        report(out)


if __name__=="__main__":
    main()

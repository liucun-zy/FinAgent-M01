"""Freeze and run the blind paired evidence-selection ablation on native DSH."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import random
import shutil
import subprocess

from .ablation_task import WINDOW_LIMIT, fingerprint, file_digest, make_workspace
from .dsh_bridge import write_json
from .dsh_project import HOST, DEMO
from .temporal_dag import TemporalDAG

STRATEGIES = ("relevance", "decision_value")
DATA = DEMO / "experiments/evidence_policy"
PACKAGE = Path(__file__).parent


def load(path):
    return json.loads(path.read_text())


def code_bindings():
    paths = [PACKAGE / (name + ".py") for name in ("ablation_task", "ablation_run", "ablation_selection",
             "active_diagnosis", "temporal_dag", "model", "financial_projection", "native_grounding",
             "guarded_artifacts", "dsh_bridge", "ablation_report")]
    paths += [DEMO / "dsh_host" / f for f in ("plugin.mjs", "adapters.mjs", "runner.mjs", "model_call.py")]
    return {str(p.relative_to(DEMO)): file_digest(p) for p in paths}


def live_config(config):
    c = load(config)
    settings = c["model"] if isinstance(c.get("model"), dict) else c
    if settings.get("model") != "gpt-6-luna":
        raise ValueError("The paired protocol fixes the candidate model")
    if settings.get("api_key"):
        raise ValueError("Use the environment or a private external key file for this experiment")
    settings.update(temperature=0, max_output_tokens=1600, max_transport_retries=1, max_calls=2)
    return c


def prepare(folder, config, data):
    from .ablation_selection import rank_windows, shared_bm25_index
    folder.mkdir(parents=True, exist_ok=False)
    for name in ("fixtures_episodes.json", "fixtures_texts.json", "fixtures_gold.json"):
        shutil.copyfile(data / name, folder / name)
    episodes = load(folder / "fixtures_episodes.json")
    windows = load(folder / "fixtures_texts.json")
    if len(set(e["id"] for e in episodes)) != len(episodes):
        raise ValueError("Each conflict needs an independent ID")
    rankings = {}
    for e in episodes:
        # No private role tags, adjudication, answers or evidence bodies in either ranker.
        public = [{k: w[k] for k in ("id", "locator", "source_id", "description", "start", "end")}
                  for w in e["windows"]]
        for w in public:
            text = windows[e["id"] + "/" + w["id"]]
            if not text.strip() or not 0 < len(text) == w["end"] - w["start"] <= WINDOW_LIMIT:
                raise ValueError(f"Invalid source window {e['id']} / {w['id']}")
        query = e["query"]
        public_scores = shared_bm25_index(query, public, {
            w["id"]:windows[e["id"] + "/" + w["id"]] for w in public})
        public = [{**w, "bm25_score": public_scores[w["id"]]} for w in public]
        rankings[e["id"]] = {s: rank_windows(s, query, public) for s in STRATEGIES}
        for v in rankings[e["id"]].values():
            if len(v) != len(public) or {x["id"] for x in v} != {x["id"] for x in public}:
                raise ValueError("Selectors must rank exactly the same window IDs")
    write_json(folder / "rankings.json", rankings)
    write_json(folder / "manifest.json", {
        "model": "gpt-6-luna", "temperature": 0, "max_output_tokens": 1600,
        "read_budget": {"windows": 1, "max_chars": WINDOW_LIMIT}, "one_decision_call": True,
        "tie_and_cost_policy": "One real window; report actual delivered Unicode codepoints separately.",
        "intervention": "For every divergent first window run both exchanged inputs with fresh decoding.",
        "ranker_query": "Fixed conflict query. A shared offline index of eligible window bodies and headings supplies lexical scores to both policies; the decision model sees one purchased excerpt.",
        "offline_index": {"candidate_window_entries":len(windows),
                          "original_chars_across_entries":sum(len(text) for text in windows.values())},
        "units": [e["id"] for e in episodes], "config_sha256": fingerprint(live_config(config)),
        "implementation": code_bindings(),
        "inputs": {name: file_digest(folder/name) for name in
                   ("fixtures_episodes.json", "fixtures_texts.json", "fixtures_gold.json", "rankings.json")}})


def one_trial(folder, e, window, text, audit_name, config_path):
    path = folder / "trials" / e["id"] / audit_name
    row_path = path / "row.json"
    if row_path.exists():
        return load(row_path)
    if path.exists():
        # A previous interrupted invocation may already have an immutable decision; recover
        # its audited result without exposing another input or rerolling that model response.
        if not (path / "session/receipt.json").is_file():
            raise RuntimeError(f"Interrupted session without receipt; inspect before resuming: {path}")
        pre_hash = load(path / "prefix.json")["sha256"]
    else:
        path.mkdir(parents=True)
        prompt, pre_hash = make_workspace(path / "dag", e, window, text)
        write_json(path / "prefix.json", {"sha256": pre_hash})
        (path / "prompt.txt").write_text(prompt)
        with (path/"stdout.json").open("w") as out, (path/"stderr.log").open("w") as err:
            subprocess.run([str(HOST), "--workspace", str(path/"dag"), "--prompt-file", str(path/"prompt.txt"),
                            "--config", str(config_path), "--max-calls", "1", "--audit-dir", str(path/"session")],
                           stdout=out, stderr=err)
    receipt = load(path / "session/receipt.json")
    dag = TemporalDAG(path / "dag")
    records = dag.nodes("ledger_repair")
    if bool(records) != receipt["committed"] or len(records) > 1:
        raise ValueError("Receipt differs from physical ledger commit")
    proposed = None
    calls_dir = path / "session/model_calls"
    requests = sorted(calls_dir.glob("*.request.json"))
    if not requests:
        raise RuntimeError(f"No audited model request in {path}")
    inputs = {fingerprint(load(p)) for p in requests}
    if len(inputs) != 1:
        raise ValueError("A transport retry changed the model decision input")
    responses = sorted(calls_dir.glob("*.response.json"))
    if responses:
        calls = load(responses[-1])["choices"][0]["message"].get("tool_calls") or []
        if calls:
            try:
                proposed = json.loads(calls[0]["function"]["arguments"]).get("action_id")
            except (ValueError, TypeError):
                pass
    result = records[0]["payload"] if records else {}
    row = {"id": e["id"], "reads": [{"window_id":window["id"], "source_id":window["source_id"],
           "locator":window["locator"], "start":window["start"], "end":window["end"],
           "chars":len(text), "cost":1, "description":window["description"], "text":text}],
           "committed":receipt["committed"], "proposed_action":proposed, "action":result.get("action","defer"),
           "final_state":result.get("final_state",e["initial_state"]), "quote":result.get("quote"),
           "reason":result.get("reason",receipt.get("error")), "changed_slots":result.get("changed_slots",[]),
           "receipt":str((path/"session/receipt.json").relative_to(folder)), "decision_tokens":receipt.get("usage"),
           "input_sha256":next(iter(inputs)), "prefix_sha256":pre_hash}
    write_json(row_path, row)
    return row


def run(folder, config, workers):
    manifest = load(folder / "manifest.json")
    if (manifest["implementation"] != code_bindings() or manifest["config_sha256"] != fingerprint(live_config(config))
            or any(file_digest(folder/f) != h for f,h in manifest["inputs"].items())):
        raise ValueError("Frozen implementation or inputs changed; start an independently labelled run")
    episodes = load(folder / "fixtures_episodes.json")
    ranks = load(folder / "rankings.json")
    texts = load(folder / "fixtures_texts.json")
    settings = folder / ".model.local.json"
    write_json(settings, live_config(config))
    settings.chmod(0o600)
    arms, swaps = [], []
    def execute(item):
        e, side, donor, control = item
        w_id = ranks[e["id"]][donor][0]["id"]
        win = next(w for w in e["windows"] if w["id"] == w_id)
        row = one_trial(folder,e,win,texts[e["id"]+"/"+w_id],("swap-" if control else "arm-")+side,settings)
        if control:
            return {**row, "side":side, "replace_index":0, "replicate":1,
                    "baseline_reads":[ranks[e["id"]][side][0]["id"]], "swapped_reads":[w_id]}
        return {**row,"policy":side}
    rng = random.Random(20261113)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for control, dest in ((False, arms),(True, swaps)):
            todo = []
            for e in episodes:
                for i, side in enumerate(STRATEGIES):
                    other = STRATEGIES[1-i]
                    if not control or ranks[e["id"]][side][0]["id"] != ranks[e["id"]][other][0]["id"]:
                        todo.append((e,side,other if control else side,control))
            rng.shuffle(todo)
            for row in pool.map(execute,todo):
                dest.append(row)
            write_json(folder/("controls.json" if control else "arms.json"), dest)
    for ctrl in swaps:
        donor = next(a for a in arms if a["id"] == ctrl["id"] and a["policy"] != ctrl["side"])
        if ctrl["input_sha256"] != donor["input_sha256"]:
            raise ValueError("A crossover changed more than the purchased evidence")
    report(folder)


def report(folder):
    from .ablation_report import write_report
    write_report(folder, load(folder/"fixtures_episodes.json"), load(folder/"fixtures_gold.json"),
                 load(folder/"arms.json"), load(folder/"controls.json"), load(folder/"manifest.json"))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("prepare","run","report"))
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--config", type=Path, default=DEMO/"configs/efunds.eval_bounded_final.local.json")
    p.add_argument("--data", type=Path, default=DATA)
    p.add_argument("--workers", type=int, choices=range(1,5), default=3)
    args=p.parse_args()
    folder=args.out.resolve()
    if args.command == "prepare":
        prepare(folder,args.config,args.data)
    elif args.command == "run":
        run(folder,args.config,args.workers)
    else:
        report(folder)


if __name__ == "__main__":
    main()

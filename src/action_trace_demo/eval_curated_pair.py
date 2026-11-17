"""Run a matched POC from identical frozen native inputs, retaining unsigned outcomes."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path

from .dsh_bridge import write_json
from .dsh_project import candidate_fingerprints
from .eval_curated_grading import PACKAGE, freeze
from .eval_curated_review import execute, prepare
from .eval_review_impact import audit
from .tools import digest


def run_pair(package, out, profile, version, config, turns, jobs):
    package, out, config = map(lambda p: Path(p).resolve(), (package, out, config))
    if out.is_relative_to(package) or out.exists():
        raise ValueError("Choose a fresh, independent experiment folder outside the package")
    plan = json.loads((package / "grader/jobs" / profile / version / "plan.json").read_text())
    units = list(dict.fromkeys(j["unit"] for j in plan["jobs"]))
    if turns is None:
        turns = int(json.loads(config.read_text())["candidate_max_turns_per_stage"])
    if not 1 <= jobs <= 4 or not 1 <= turns <= int(json.loads(config.read_text())["candidate_max_turns_per_stage"]):
        raise ValueError("Use at most four workers and the configured stage maximum")
    out.mkdir(parents=True)
    arms = ("ledger_only","ledger_graph")
    pin = candidate_fingerprints()
    binding = {"profile":profile, "version":version, "units":units, "policies":list(arms),
               "per_unit_turns":turns,"model_config_sha256":digest(config),"candidate_code":pin,
               "model":json.loads(config.read_text())["model"],"grader_visible_to_candidate":False}
    write_json(out/"pair_protocol.json",binding)
    work = []
    for unit in units:
        original = package / "candidate" / version / unit
        ledgers = []
        for policy in arms:
            folder = out / policy / unit
            record = prepare(original,folder,policy)
            ledgers.append({k:record[k] for k in ("graph_sha256","ledger_sha256","inputs","original_paths","expected")})
            work.append((policy,unit,folder,record["last_seq"]))
        if ledgers[0] != ledgers[1]:
            raise ValueError("The paired candidates did not receive identical initial inputs")
    def trial(job):
        policy, unit, folder, cutoff = job
        if candidate_fingerprints()!=pin:
            raise ValueError("Candidate code drift before execution")
        snapshot = execute(folder,config,max_calls=turns)
        result = json.loads((folder/"review_result.json").read_text())
        if not result.get("code_unchanged_during_execution") or candidate_fingerprints()!=pin:
            raise ValueError("Candidate code drift in matched experiment")
        impact = audit(folder/"operator",cutoff)
        write_json(folder/"impact.json",impact)
        return policy, unit, snapshot["units"][unit], {
            "stage_signed":bool(result["receipt"]), "states":{k:v["state"] for k,v in result["files"].items()},
            "files_physically_changed":[k for k,v in result["files"].items() if v["changed"]],
            "cost_rmb":impact["sessions"]["estimated_rmb_logged"], "calls":impact["sessions"]["api_attempts_logged"],
            "graph":impact["counts"]}
    reports = {p:{} for p in arms}
    snapshots = {p:{"version":version,"origin":str(out/p),"units":{}} for p in arms}
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        tasks = [pool.submit(trial, job) for job in work]
        for f in as_completed(tasks):
            policy,unit,snapshot,stats = f.result()
            snapshots[policy]["units"][unit] = snapshot
            reports[policy][unit] = stats
            write_json(out/"pair_progress.json",reports)
    for policy in arms:
        manifest = out/policy/"snapshot.json"
        write_json(manifest,snapshots[policy])
        freeze(package,out/policy/"judge_freeze",profile,version,units,manifest)
    write_json(out/"pair_result.json",reports)
    return reports


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--package",type=Path,default=PACKAGE)
    p.add_argument("--profile",choices=("smoke","core"),default="smoke")
    p.add_argument("--version",choices=("demo","full"),default="full")
    p.add_argument("--config",required=True,type=Path)
    p.add_argument("--out",required=True,type=Path)
    p.add_argument("--turns",type=int)
    p.add_argument("--workers",type=int,default=4)
    a=p.parse_args()
    report=run_pair(a.package,a.out,a.profile,a.version,a.config,a.turns,a.workers)
    print(json.dumps({p:[u for u,r in units.items() if r["stage_signed"]] for p,units in report.items()}))


if __name__=="__main__":
    main()

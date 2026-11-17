"""Schedule independent frozen evaluation units without changing Judge protocol."""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as wait_futures
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
from pathlib import Path
import time

from .evaluation import _freeze, score_stage
from .runner import save


UNITS = [f"T{stage:02d}" for stage in range(1, 14)] + ["FINAL"]
ACTIVE_CANDIDATE_STATES = {"initializing", "running"}


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected metadata object: {path.name}")
    return value


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def _exclusive(eval_root: Path):
    # The serial scheduler predates this lock and must be stopped before a
    # handoff. All parallel scheduler invocations cooperate on this lock.
    with (eval_root / ".parallel_scheduler.lock").open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another parallel scheduler owns this evaluation directory") from None
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _bind_inputs(case_root: Path, run_dir: Path, config: dict, eval_root: Path,
                 workers: int, wait: bool) -> dict:
    # Deliberately identical to eval_sequence's scoring identity. Scheduling is
    # recorded separately and never changes adapter/configuration provenance.
    manifest = {
        "case_root": str(case_root), "run_dir": str(run_dir),
        "run_manifest_sha256": _hash(run_dir / "run_manifest.json"),
        "judge_configuration_sha256": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
        "adapter_sha256": _hash(Path(__file__).with_name("evaluation.py")),
    }
    if str(config.get("local_evaluation_protocol", "")).startswith("v4_"):
        manifest["adapter_dependency_sha256"] = {
            name: _hash(Path(__file__).with_name(name))
            for name in ("evidence_stats.py", "workbook_evidence.py")
        }
    path = eval_root / "sequence_manifest.json"
    if path.exists() and _json(path) != manifest:
        raise ValueError("Evaluation inputs/configuration changed; preserve results and use a new directory")
    if not path.exists():
        save(path, manifest)
    scheduling = {
        "schema_version": "1", "scheduler": "independent_frozen_units_thread_pool",
        "workers": workers, "wait_for_candidate": wait,
        "scheduler_sha256": _hash(Path(__file__)), "scoring_protocol_unchanged": True,
        "sequence_manifest_sha256": _hash(path), "unit_order": UNITS,
        "reuse_policy": "completed_results_only_never_rejudge; reject_nonempty_unfinished_unit_directories",
        "readiness_policy": "hash_bound_complete_freeze; FINAL_requires_T13_freeze",
        "shared_output_writer": "scheduler_main_thread_only",
        "handoff_requirement": "stop_serial_scheduler_before_starting_parallel_scheduler",
    }
    scheduling_path = eval_root / "scheduling_manifest.json"
    if scheduling_path.exists() and _json(scheduling_path) != scheduling:
        raise ValueError("Scheduling manifest changed; do not overwrite the existing scheduling audit")
    if not scheduling_path.exists():
        save(scheduling_path, scheduling)
    return manifest


def _existing_results(eval_root: Path, adapter_hash: str) -> dict[str, dict]:
    results = {}
    for unit in UNITS:
        output = eval_root / unit
        if output.is_symlink():
            raise ValueError(f"Symlink evaluation unit directory: {unit}")
        if output.exists() and not output.is_dir():
            raise ValueError(f"Evaluation unit path is not a directory: {unit}")
        result_path = output / "evaluation_result.json"
        if result_path.exists():
            result = _json(result_path)
            if result.get("evaluation_unit") != unit or result.get("mode") != "local_rubric_scoring":
                raise ValueError(f"Existing evaluation identity mismatch: {unit}")
            if result.get("official_harbor_run") is not False or result.get("protocol_version") not in {"v3", "v4"}:
                raise ValueError(f"Existing evaluation protocol mismatch: {unit}")
            protocol_path = output / "adapter_protocol.json"
            if not protocol_path.is_file() or _json(protocol_path).get("adapter_sha256") != adapter_hash:
                raise ValueError(f"Existing evaluation adapter mismatch: {unit}")
            results[unit] = result
        elif output.exists() and any(output.iterdir()):
            raise ValueError(f"Unfinished nonempty evaluation directory must be preserved, not overwritten: {unit}")
    return results


def _ready_units(run_dir: Path) -> set[str]:
    state = _json(run_dir / ".workspace.case_state.json") if (run_dir / ".workspace.case_state.json").exists() else {}
    ready = set()
    for unit in UNITS[:-1]:
        # The publisher commits its hash-bound freeze before run_status is
        # updated. A terminal Candidate may still have a published last freeze.
        if unit not in state.get("freeze_record_hashes", {}):
            continue
        _freeze(run_dir, unit)
        ready.add(unit)
    if "T13" in ready:
        ready.add("FINAL")
    return ready


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _summary(results: dict[str, dict], candidate: dict, *, finished: bool, waiting: bool) -> dict:
    def row(unit):
        return results.get(unit, {"evaluation_unit": unit, "status": "not_run", "local_score": None,
                                  "reason": "waiting_for_frozen_stage" if waiting else "candidate_stage_not_frozen"})
    rows = [row(unit) for unit in UNITS[:-1]]
    numeric = len(rows) == 13 and all(r.get("status") == "graded" and _number(r.get("local_score")) for r in rows)
    state = "in_progress"
    if finished:
        state = "finished" if len(results) == 14 else "candidate_incomplete"
    return {"mode": "local_rubric_scoring", "official_harbor_run": False, "state": state,
            "results": rows, "FINAL": row("FINAL"), "candidate_status": candidate,
            "process_mean": sum(r["local_score"] for r in rows) / 13 if numeric else None,
            "complete_numeric_process_scores": numeric}


def evaluate_parallel(case_root: Path, run_dir: Path, config: dict, eval_root: Path, *,
                      wait: bool = False, workers: int = 3, poll_seconds: float = 10) -> dict:
    if isinstance(workers, bool) or workers < 1 or poll_seconds <= 0:
        raise ValueError("workers and poll_seconds must be positive")
    case_root, run_dir, eval_root = (Path(p).resolve() for p in (case_root, run_dir, eval_root))
    if eval_root.is_relative_to(run_dir / "workspace"):
        raise ValueError("Evaluation scheduler cannot write inside the Candidate workspace")
    eval_root.mkdir(parents=True, exist_ok=True)
    with _exclusive(eval_root):
        manifest = _bind_inputs(case_root, run_dir, config, eval_root, workers, wait)
        results = _existing_results(eval_root, manifest["adapter_sha256"])
        active = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="deliverbench-judge") as executor:
            while True:
                # Reject any accidental adapter mutation during a long run.
                if _hash(Path(__file__).with_name("evaluation.py")) != manifest["adapter_sha256"]:
                    raise ValueError("Scoring adapter changed while scheduler was running")
                for name, digest in manifest.get("adapter_dependency_sha256", {}).items():
                    if _hash(Path(__file__).with_name(name)) != digest:
                        raise ValueError("Scoring evidence helper changed while scheduler was running")
                candidate = _json(run_dir / "run_status.json")
                ready = _ready_units(run_dir)
                active_units = set(active.values())
                pending = [u for u in UNITS if u in ready and u not in results and u not in active_units]
                for unit in pending[:max(0, workers - len(active))]:
                    output = eval_root / unit
                    if output.exists() and any(output.iterdir()):
                        raise ValueError(f"Unit directory became nonempty before scheduling: {unit}")
                    active[executor.submit(score_stage, case_root, run_dir, unit, config, output)] = unit
                waiting = wait and candidate.get("state") in ACTIVE_CANDIDATE_STATES and len(results) < 14
                finished = not active and not waiting
                summary = _summary(results, candidate, finished=finished, waiting=waiting)
                save(eval_root / "summary.json", summary)
                save(eval_root / "progress.json", {
                    "state": summary["state"] if finished else "judging" if active else "waiting_for_frozen_stage",
                    "active": [u for u in UNITS if u in active.values()],
                    "completed": [u for u in UNITS if u in results],
                    "completed_evaluations": len(results), "workers": workers,
                    "available_frozen_units": [u for u in UNITS if u in ready],
                    "unavailable_units": [u for u in UNITS if u not in ready],
                })
                if finished:
                    return summary
                if not active:
                    time.sleep(poll_seconds)
                    continue
                done, _ = wait_futures(active, timeout=poll_seconds, return_when=FIRST_COMPLETED)
                for future in done:
                    unit = active.pop(future)
                    result = future.result()
                    if result.get("evaluation_unit") != unit:
                        raise ValueError(f"Scorer returned mismatched evaluation unit: {unit}")
                    results[unit] = result
                    print(json.dumps({"unit": unit, "status": result.get("status"),
                                      "local_score": result.get("local_score")}, ensure_ascii=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-root", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--eval-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()
    result = evaluate_parallel(args.case_root, args.run_dir, _json(args.config), args.eval_dir,
                               wait=args.wait, workers=args.workers)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

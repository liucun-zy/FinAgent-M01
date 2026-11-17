"""M01: ordered arrivals, cached parsing and short extraction sessions on DSH."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from .dsh_bridge import write_json

DEMO = Path(__file__).resolve().parents[2]
DEFAULTS = {
    "parse_concurrency": 2, "extraction_concurrency": 20,
    "window_target_tokens": 1800, "window_max_tokens": 3000,
    "default_context_max_tokens": 800, "batch_input_budget_tokens": 8000,
    "max_windows_per_request": 4, "extraction_output_max_tokens": 4096,
    "max_context_rounds_per_group": 1, "context_extra_max_tokens": 2000,
    "max_model_requests_per_group": 2, "max_transport_attempts_per_group": 3,
    "max_model_requests_total": 8,
    "source_unit_max_chars": 1800, "source_context_max_chars": 1200, "source_batch_max_units": 8,
}


def load_config(path, workspace):
    path, workspace = Path(path).resolve(), Path(workspace).resolve()
    config = json.loads(path.read_text())
    config["ingestion"] = {**DEFAULTS, **config.get("ingestion", {})}
    if any(type(value) is not int or value < 1 for value in config["ingestion"].values()):
        raise ValueError("Ingestion budgets and concurrency must be positive integers")
    model_path = Path(config["model_config_file"])
    config["model_config_file"] = str((path.parent / model_path).resolve())
    mineru = config.setdefault("mineru", {})
    for key in ("api_keys_file", "cache_dir", "task_budget_file"):
        if mineru.get(key):
            mineru[key] = str((path.parent / mineru[key]).resolve())
    mineru.setdefault("cache_dir", str(workspace / "parse_cache"))
    mineru.setdefault("task_budget_file", str(workspace / "mineru_budget.json"))
    mineru.setdefault("max_new_tasks", 3)
    return config


def _originals(batch, manifest_dir, workspace):
    """Snapshot bytes before parsing; names are display metadata, never identity."""
    import httpx
    sources = []
    for item in batch["sources"]:
        entry = dict(item)
        if "path" in entry:
            original = (manifest_dir / entry.pop("path")).resolve()
            content = original.read_bytes()
            name = entry.get("display_name", original.name)
            location = str(original)
        else:
            from urllib.parse import urlparse
            location = entry.pop("url")
            response = httpx.get(location, follow_redirects=True, timeout=60)
            response.raise_for_status()
            content = response.content
            name = entry.get("display_name") or Path(urlparse(location).path).name
            if not name:
                raise ValueError("A URL without a filename requires display_name with a file extension")
        sha = hashlib.sha256(content).hexdigest()
        snapshot = workspace / "originals" / sha / Path(name).name
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        if not snapshot.exists():
            snapshot.write_bytes(content)
        sources.append({**entry, "display_name": name, "content_hash": sha,
                        "media_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
                        "original_location": str(snapshot), "received_location": location})
    return sources


def register_arrival(batch, workspace, *, manifest_dir=None):
    from .ingestion_store import IngestionStore
    root = Path(workspace).resolve()
    return IngestionStore(root).register_arrival(
        _originals(batch, Path(manifest_dir or ".").resolve(), root),
        task_context=batch.get("task_context"), received_at=batch.get("received_at"),
    )


def _model_config(config, root):
    """Reuse the private provider configuration; only this run's budgets change."""
    model = json.loads(Path(config["model_config_file"]).read_text())
    settings = model["model"] if isinstance(model.get("model"), dict) else model
    if config.get("extraction_model"):
        settings["model"] = config["extraction_model"]
    model["ingest_parallel_budget"] = True
    model["transport_stage_limits"] = config.get("transport_stage_limits", {})
    model["ingestion_pricing_verified"] = bool(config.get("model_pricing_verified", False))
    settings["max_calls"] = config["ingestion"]["max_model_requests_total"]
    settings["max_output_tokens"] = config["ingestion"]["extraction_output_max_tokens"]
    directory = root / ".private"
    directory.mkdir(exist_ok=True, mode=0o700)
    path = directory / "model.local.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(model, stream)
    os.chmod(path, 0o600)
    return path


def extract_group_with_dsh(group, workspace, model_config, *, prompt, script=None):
    root = Path(workspace).resolve()
    group_dir = root / "groups" / group["window_group_id"]
    group_dir.mkdir(parents=True, exist_ok=True)
    previous = sorted(group_dir.glob("**/invocation.json"), key=lambda p: p.stat().st_mtime_ns)
    if previous:
        prior_dir = previous[-1].parent
        group_dir = group_dir / f"resume_{len(previous):03d}"
        group_dir.mkdir()
        state = prior_dir / "ingest_tool_state.json"
        if state.exists():
            write_json(group_dir / state.name, json.loads(state.read_text()))
    write_json(group_dir / "task_context.json", {
        "mode": "ingest", "request_id": group["window_group_id"],
        "ingestion_root": str(root), "group_file": "ingest_group.json",
    })
    write_json(group_dir / "ingest_group.json", group)
    (group_dir / "prompt.txt").write_text(prompt)
    audit = root / ".dsh" / (group["window_group_id"] + "-" + uuid4().hex[:8])
    command = [str(DEMO / "dsh_host/run"), "--workspace", str(group_dir),
               "--prompt-file", str(group_dir / "prompt.txt"),
               "--max-calls", str(group["config"]["max_model_requests_per_group"]),
               "--audit-dir", str(audit)]
    if script is None:
        private = json.loads(Path(model_config).read_text())
        private["input_budget_tokens"] = group["config"]["batch_input_budget_tokens"]
        private["attempt_tags"] = {"original_group": group.get("original_group_id", group["window_group_id"]),
                                   "range": group["window_group_id"]}
        private["attempt_limits"] = {"original_group": group["config"]["max_transport_attempts_per_group"]}
        if group.get("recovery_parent_id"):
            private["attempt_tags"]["recovery_slot"] = group["window_group_id"]
            private["attempt_limits"]["range"] = 1
        private_path = root / ".private" / (group["window_group_id"] + ".json")
        fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(private, stream)
        command += ["--config", str(private_path)]
    else:
        write_json(group_dir / "script.json", script)
        command += ["--script", str(group_dir / "script.json")]
    start = time.perf_counter()
    with (group_dir / "stdout.json").open("w") as out, (group_dir / "stderr.log").open("w") as err:
        process = subprocess.run(command, stdout=out, stderr=err)
    try:
        receipt = json.loads((group_dir / "stdout.json").read_text())
    except ValueError:
        receipt = {"committed": False, "error": "DSH exited without a JSON receipt; see group stderr.log"}
    receipt.update(exit_code=process.returncode, elapsed_seconds=round(time.perf_counter() - start, 3),
                   group_directory=str(group_dir), audit_dir=str(audit))
    responses = sorted((audit / "model_calls").glob("*.response.json"))
    receipt["truncated"] = bool(script and any(a.get("response", {}).get("choices", [{}])[0].get("finish_reason") == "length" for a in script)) or any(json.loads(path.read_text()).get("choices", [{}])[0].get("finish_reason") == "length"
                               for path in responses)
    write_json(group_dir / "invocation.json", receipt)
    return receipt


def model_usage(workspace):
    records = []
    for path in sorted((Path(workspace) / ".dsh").glob("*/model_calls.jsonl")):
        records.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
    complete = all(r["usage_complete"] for r in records)
    model_file = Path(workspace) / ".private" / "model.local.json"
    verified_price = json.loads(model_file.read_text()).get("ingestion_pricing_verified", False) if model_file.exists() else False
    return {
        "transport_attempts": len(records),
        "successful_transport_attempts": sum(r["status"] == "succeeded" for r in records),
        "input_tokens": sum(r.get("input_tokens") or 0 for r in records) if complete else None,
        "output_tokens": sum(r.get("output_tokens") or 0 for r in records) if complete else None,
        "estimated_cost_rmb": sum(r.get("estimated_cost_rmb") or 0 for r in records) if complete and verified_price else None,
        "cost_basis": "configured_verified_rates" if verified_price else "unverified; report tokens only",
        "usage_complete": complete,
        "unpriced_attempts": sum(not r["usage_complete"] for r in records),
    }


def _parse_one(source, config):
    from .mineru_adapter import parse_source
    from .evidence_windows import normalize_parse_result, build_windows, evidence_refs
    artifact = parse_source(source, config["mineru"])
    blocks = normalize_parse_result(artifact)
    windows = build_windows(blocks, config["ingestion"])
    return {"artifact": artifact, "blocks": blocks, "windows": windows,
            "evidence_refs": evidence_refs(blocks)}


def _groups(arrival, parsed, store, settings, *, extraction_profile=None, processed_units=(), order_offset=0):
    from .source_pipeline import plan_groups
    return plan_groups(arrival, parsed, store, settings, extraction_profile=extraction_profile,
                       processed_units=processed_units, order_offset=order_offset)


def _record_group_plan(store, root, group, prompt):
    directory = root / "groups" / group["window_group_id"]
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "ingest_group.json", group)
    (directory / "prompt.txt").write_text(prompt)
    store.record_group_plan(group, directory / "ingest_group.json")


def _execute_group(group, prompt, root, model_config, store):
    from .source_pipeline import execute_group
    return execute_group(group, prompt, root, model_config, store,
                         extract_group_with_dsh, _record_group_plan)


def process_manifests(workspace, manifests, config_path, *, arrival_ids=None):
    """Register before parsing, parse concurrently, then merge in arrival order."""
    from .ingestion_store import IngestionStore
    root = Path(workspace).resolve()
    root.mkdir(parents=True, exist_ok=True)
    config = load_config(config_path, root)
    settings = config["ingestion"]
    store = IngestionStore(root)
    arrivals = [store.get_arrival(aid) for aid in (arrival_ids or [])]
    for path in manifests:
        path = Path(path).resolve()
        manifest = json.loads(path.read_text())
        for batch in manifest.get("arrivals", [manifest]):
            arrivals.append(register_arrival(batch, root, manifest_dir=path.parent))
    model_config = _model_config(config, root)
    from .source_pipeline import extraction_profile
    private_settings = json.loads(model_config.read_text())
    model_settings = private_settings["model"] if isinstance(private_settings.get("model"), dict) else private_settings
    task_ledger = Path(config["mineru"]["task_budget_file"])
    tasks_before = json.loads(task_ledger.read_text()).get("new_tasks", 0) if task_ledger.exists() else 0
    # Identical source versions share one future, irrespective of arrival names.
    source_ids = list(dict.fromkeys(s for a in arrivals for s in a["source_version_ids"]))
    parsed_by_source, parse_errors = {}, {}
    with ThreadPoolExecutor(max_workers=settings["parse_concurrency"]) as pool:
        futures = {sid: pool.submit(_parse_one, store.get_source(sid), config) for sid in source_ids}
        for sid, future in futures.items():
            try:
                item = future.result()
                artifact = item["artifact"]
                store.save_parse(sid, item["blocks"], item["windows"],
                                 evidence_refs=item["evidence_refs"],
                                 parse_result_location=artifact["parse_result_location"],
                                 parse_profile=artifact["profile"])
                parsed_by_source[sid] = item
            except (ValueError, RuntimeError, OSError, KeyError, json.JSONDecodeError) as exc:
                # A source is explicitly failed; its siblings remain processable.
                parse_errors[sid] = f"{type(exc).__name__}: {exc}"
    task_state = json.loads(task_ledger.read_text()) if task_ledger.exists() else {"new_tasks": 0, "tasks": []}
    parser_usage = {"new_tasks": task_state["new_tasks"] - tasks_before,
                    "recorded_tasks_total": task_state["new_tasks"],
                    "task_states": {state: sum(t["state"] == state for t in task_state["tasks"])
                                    for state in sorted({t["state"] for t in task_state["tasks"]})},
                    "task_ledger": str(task_ledger)}
    outputs, parse_usage_claimed = [], set()
    for arrival in arrivals:
        started = time.perf_counter()
        usage_before = model_usage(root)
        parsed, unresolved = [], []
        from .semantic_ingestion import initialize_schema, resolve_arrival
        # Source extraction never depends on a generated catalog or historical
        # entity summaries. Formal public schema may seed the post-save registry.
        semantic_invocations = []
        if config.get("task_schema") is not None:
            initialize_schema(store, arrival, [], config, model_config)
        arrival = {**arrival, "task_context": arrival.get("task_context") or
                   (store.task_card() or {}).get("instruction_text")}
        profile = extraction_profile(model_settings, settings, arrival.get("task_context"))
        already_processed = {wid for record in store.export()["extractions"]
                             if record["arrival_id"] == arrival["arrival_id"]
                             and (record.get("extraction_profile") or {}).get("profile_id") == profile["profile_id"]
                             for wid in record["result"]["processed_window_ids"]}
        for sid in dict.fromkeys(arrival["source_version_ids"]):
            if sid in parse_errors:
                store.mark_source_failed(arrival["arrival_id"], sid, parse_errors[sid])
                unresolved.append({"source_version_id": sid, "error": parse_errors[sid]})
                continue
            item = parsed_by_source[sid]
            reusable = store.reusable_source_result(sid, profile["profile_id"], exclude_arrival_id=arrival["arrival_id"])
            if reusable is not None:
                store.mark_reused(arrival["arrival_id"], sid, reusable["assertion_ids"], profile_id=profile["profile_id"])
            elif not item["windows"]:
                unresolved.append({"source_version_id": sid, "reason": "No readable evidence windows"})
            else:
                parsed.append(item)
        plans = [r for r in store.export()["group_plans"]
                 if r["arrival_id"] == arrival["arrival_id"] and r["profile_id"] == profile["profile_id"]]
        split_parents = {r["parent_group_id"] for r in store.export()["group_splits"]}
        groups = []
        scheduled = set()
        for plan in plans:
            scheduled.update(w["window_id"] for w in plan["windows"])
            if plan["window_group_id"] in split_parents:
                continue
            group = json.loads(Path(plan["group_file"]).read_text())
            if all(w["window_id"] in already_processed for w in group["windows"]):
                continue
            groups.append((group, (Path(plan["group_file"]).parent / "prompt.txt").read_text()))
        fresh, budget_unresolved = _groups(arrival, parsed, store, settings, extraction_profile=profile,
            processed_units=already_processed | scheduled, order_offset=len(plans))
        unresolved.extend(budget_unresolved)
        for group, prompt in fresh:
            _record_group_plan(store, root, group, prompt)
        groups.extend(fresh)
        invocations = []
        with ThreadPoolExecutor(max_workers=settings["extraction_concurrency"]) as pool:
            sessions = [pool.submit(_execute_group, group, prompt, root, model_config, store)
                        for group, prompt in groups]
            for future in sessions:
                attempts, pending = future.result()
                invocations.extend(attempts)
                unresolved.extend(pending)
        semantic_invocations.extend(resolve_arrival(store, arrival, config, model_config))
        store.merge_arrival(arrival["arrival_id"])
        usage_after = model_usage(root)
        usage = {"model": {k: usage_after[k] - usage_before[k] if isinstance(usage_after[k], (int, float))
                            and not isinstance(usage_after[k], bool) and usage_before[k] is not None
                            else usage_after[k] for k in usage_after},
                 "model_requests": sum(r.get("steps", 0) or 0 for r in [*invocations, *semantic_invocations]),
                 "semantic_invocations": semantic_invocations,
                 "group_invocations": invocations,
                 "parser": {"task_ledger": str(task_ledger),
                            "cache_hits": sum(1 if s in parse_usage_claimed else
                            parsed_by_source[s]["artifact"]["usage"].get("cache_hits", 0)
                           for s in set(arrival["source_version_ids"]) if s in parsed_by_source)},
                 "extraction_elapsed_seconds": round(time.perf_counter() - started, 3)}
        parse_usage_claimed.update(arrival["source_version_ids"])
        store.finish_arrival(arrival["arrival_id"], usage=usage, unresolved_items=unresolved, extraction_profile=profile)
        outputs.append(get_ingestion_result(root, arrival["arrival_id"]))
    return {"workspace": str(root), "arrivals": outputs, "model_usage_total": model_usage(root),
            "parser_usage": parser_usage}


def get_ingestion_result(workspace, arrival_id=None):
    from .ingestion_store import IngestionStore
    root = Path(workspace).resolve()
    store = IngestionStore(root)
    exported = store.export(arrival_id)
    with store.dag._db() as db:
        cutoff = db.execute("SELECT COALESCE(MAX(seq),0) FROM event").fetchone()[0]
    from .semantic_ingestion import PROJECTION_VERSION
    target = root / "exports" / (arrival_id or "current") / f"at_{cutoff:09d}_{PROJECTION_VERSION}"
    target.mkdir(parents=True, exist_ok=True)
    locations = {}
    for name in ("entities", "source_claims", "initial_ledger", "evidence_refs", "m02_delta", "task_schema", "semantic_mappings"):
        path = target / (name + ".json")
        write_json(path, exported[name])
        locations[name] = str(path)
    write_json(target / "evidence_store.json", exported)
    locations["evidence_store"] = str(target / "evidence_store.json")
    result = store.get_ingestion_result(arrival_id) if arrival_id else {"results": exported["results"]}
    result = {**result, "as_of_record_seq": cutoff, "initial_ledger_item_count": len(exported["initial_ledger"]),
              "output_locations": locations}
    write_json(target / "result.json", result)
    return result


def run_command(args):
    if args.command == "ingest":
        return process_manifests(args.workspace, args.manifest, args.config)
    if args.command == "resume-ingestion":
        return process_manifests(args.workspace, [], args.config, arrival_ids=[args.arrival])
    if args.command == "build-ledger":
        from .initial_context import (
            build_initial_context, read_initial_item_evidence, render_initial_context,
        )
        task_input = json.loads(args.task_file.read_text()) if args.task_file else None
        bundle = build_initial_context(args.workspace, task_input=task_input, arrival_id=args.arrival)
        snapshot = bundle["snapshot"]
        result = get_ingestion_result(args.workspace, snapshot["initial_arrival_id"])
        target = Path(result["output_locations"]["initial_ledger"]).parent
        evidence_views = {
            item["ledger_item_id"]: read_initial_item_evidence(
                args.workspace, item["ledger_item_id"], snapshot["ledger_snapshot_id"])
            for item in bundle["items"]
        }
        write_json(target / "initial_ledger_bundle.json", bundle)
        (target / "initial_context.md").write_text(
            render_initial_context(bundle, evidence_views=evidence_views), encoding="utf-8")
        result["output_locations"].update({
            "initial_ledger_bundle": str(target / "initial_ledger_bundle.json"),
            "initial_context": str(target / "initial_context.md"),
        })
        result["snapshot"] = snapshot
        write_json(target / "result.json", result)
        return result
    if args.command == "ledger-evidence":
        from .initial_context import read_initial_item_evidence
        return read_initial_item_evidence(args.workspace, args.item, args.snapshot)
    if args.command == "generate-candidates":
        from .candidate_pairing import generate_candidates
        from .candidate_materials import render_candidates
        snapshot = json.loads(args.ledger_file.read_text()) if args.ledger_file else None
        bundle = generate_candidates(args.workspace, ledger_snapshot=snapshot, arrival_id=args.arrival,
                                     limit=args.limit, batch_id=args.batch,
                                     claim_ids=args.claim, visible_as_of=args.as_of,
                                     context_budget=args.context_budget)
        version = int(bundle["batch_id"][1:]) if bundle["batch_id"] else bundle["input_scope"]["visible_as_of"]
        target = Path(args.workspace).resolve() / "exports" / (bundle["input_scope"]["arrival_id"] or "current") / f"at_{version:09d}"
        target.mkdir(parents=True, exist_ok=True)
        locations = {"candidate_pairs": str(target / "candidate_pairs.json"),
                     "comparison_materials": str(target / "comparison_materials.json"),
                     "candidate_context": str(target / "candidate_context.md")}
        write_json(Path(locations["candidate_pairs"]), {k: v for k, v in bundle.items() if k not in {"materials", "reused"}})
        write_json(Path(locations["comparison_materials"]), bundle["materials"])
        Path(locations["candidate_context"]).write_text(render_candidates(bundle), encoding="utf-8")
        return {"batch_id": bundle["batch_id"], "reused": bundle["reused"],
                "metrics": bundle["metrics"], "output_locations": locations}
    if args.command == "candidate-context":
        from .candidate_materials import read_candidate_context
        return read_candidate_context(args.workspace, args.batch, args.claim, extra_token_budget=args.budget)
    if args.command == "maintain-ledger":
        from .maintenance import maintain_ledger
        return maintain_ledger(args.workspace, args.batch, config=args.config, script=args.script,
                               previous_receipt_id=args.retry_receipt)
    if args.command == "maintenance-result":
        from .maintenance import maintenance_result
        return maintenance_result(args.workspace, args.request)
    if args.command == "current-ledger":
        from .maintenance_store import current_ledger_state
        return current_ledger_state(args.workspace)
    return get_ingestion_result(args.workspace, args.arrival)


def add_commands(sub):
    ingest = sub.add_parser("ingest", help="M01: parse arrivals and save source claims through DSH")
    ingest.add_argument("--workspace", type=Path, required=True)
    ingest.add_argument("--manifest", type=Path, action="append", required=True)
    ingest.add_argument("--config", type=Path, required=True)
    resume = sub.add_parser("resume-ingestion", help="M01: explicitly process remaining windows; append new receipts")
    resume.add_argument("--workspace", type=Path, required=True)
    resume.add_argument("--arrival", required=True)
    resume.add_argument("--config", type=Path, required=True)
    ledger = sub.add_parser("build-ledger", help="M02 initial: build or read the initial ledger with task context")
    ledger.add_argument("--workspace", type=Path, required=True)
    ledger.add_argument("--arrival")
    ledger.add_argument("--task-file", type=Path, help="JSON containing the explicit task instruction and optional fields")
    evidence = sub.add_parser("ledger-evidence", help="M02 initial: read an item's source claims and evidence at its snapshot")
    evidence.add_argument("--workspace", type=Path, required=True)
    evidence.add_argument("--item", required=True, help="Existing ledger_item_id")
    evidence.add_argument("--snapshot", required=True, help="Existing ledger_snapshot_id")
    candidates = sub.add_parser("generate-candidates", help="M02: retrieve old items for unprocessed source claims")
    candidates.add_argument("--workspace", type=Path, required=True)
    candidates.add_argument("--arrival", help="Optionally restrict pending claims to one arrival")
    candidates.add_argument("--ledger-file", type=Path, help="Explicit ledger snapshot JSON; defaults to the actual current ledger")
    candidates.add_argument("--limit", type=int, default=3, help="Maximum candidates per new claim")
    candidates.add_argument("--batch", help="Read or expand an existing batch at its fixed visibility and ledger snapshot")
    candidates.add_argument("--claim", action="append", help="Saved claim ID to pair again against the current ledger")
    candidates.add_argument("--as-of", type=int, help="Evidence cutoff for explicit re-pairing")
    candidates.add_argument("--context-budget", type=int, default=8000,
                            help="Estimated prompt context budget; omitted evidence remains readable by ID")
    context = sub.add_parser("candidate-context", help="M02: expand a batch claim's context at its original visibility cutoff")
    context.add_argument("--workspace", type=Path, required=True)
    context.add_argument("--batch", required=True)
    context.add_argument("--claim", required=True)
    context.add_argument("--budget", type=int, default=2000)
    maintain = sub.add_parser("maintain-ledger", help="M03: run the Agent and execute ledger maintenance")
    maintain.add_argument("--workspace", type=Path, required=True)
    scope = maintain.add_mutually_exclusive_group(required=True)
    scope.add_argument("--batch")
    scope.add_argument("--retry-receipt", help="New request for only the unfinished scope of a saved receipt")
    execution = maintain.add_mutually_exclusive_group(required=True)
    execution.add_argument("--config", type=Path)
    execution.add_argument("--script", type=Path, help="Explicit offline DSH action replay; no model calls")
    result = sub.add_parser("maintenance-result", help="Read actual receipts or pending inputs")
    result.add_argument("--workspace", type=Path, required=True)
    result.add_argument("--request")
    current = sub.add_parser("current-ledger", help="Read the latest actual ledger version")
    current.add_argument("--workspace", type=Path, required=True)
    show = sub.add_parser("ingestion-result", help="M01: export evidence, claims and the M02 increment")
    show.add_argument("--workspace", type=Path, required=True)
    show.add_argument("--arrival")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_commands(parser.add_subparsers(dest="command", required=True))
    result = run_command(parser.parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

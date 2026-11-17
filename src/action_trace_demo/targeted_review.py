"""Targeted historical-draft validation. Reports are outside every candidate root.

Offline exercises the production bridge without model calls. Live starts each
independent native DSH session on a fresh chronological prefix. Completed historical stages supply the shared prefix. The candidate cannot read
historical reviews from its target stage, rubrics, labels or grader outputs.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .dsh_bridge import Bridge, handover_mirrors, write_json
from .dsh_project import DSHSessionError, run_stage, candidate_fingerprints
from .guarded_artifacts import _slots
from .native_finance import inspect_workbook
from .tools import digest
from .workbook_evidence import recalculate_workbook


def _frozen(base, stage, path):
    prefix = base / "freezes" / "workspace" / f"T{stage:02d}"
    manifest = json.loads((prefix / "freeze.json").read_text())
    file = prefix / "evidence" / path
    sha = manifest["evidence_files"][path]
    if digest(file) != sha:
        raise ValueError("Baseline freeze and physical evidence differ: " + path)
    return file, sha


def _diff(before, after):
    return {k: {"before": before.get(k), "after": after.get(k)} for k in sorted(before.keys() | after.keys())
            if before.get(k) != after.get(k)}


def _independent_workbook(before_file, after_file, cache):
    """Physically recalculate in the evaluator's own directory even if other
    obligations leave the stage blocked or cached cells were not published."""
    old_slots = _slots(before_file)
    new_slots = _slots(after_file)
    old_map = inspect_workbook(old_slots)
    new_map = inspect_workbook(new_slots)
    comparison_cells = {field_loc for fields in old_map["scenario_slots"].values() for field_loc in fields.values()}
    protected = sorted(loc for loc in comparison_cells if old_slots[loc] != new_slots.get(loc))
    changed = _diff(old_slots, new_slots)
    rows = {}
    for label, file in (("before", before_file), ("after", after_file)):
        receipt = recalculate_workbook(file, cache / label)
        values = {}
        if receipt["status"] == "complete" and receipt.get("no_errors_verified"):
            values = _slots(Path(receipt["derived_path"]), cached=True)
        rows[label] = {"status": receipt["status"], "reason": receipt.get("reason"),
                       "no_errors_verified": receipt.get("no_errors_verified"), "cache_hash": receipt.get("derived_sha256"),
                       "values": values}
    downstream = set(changed)
    for _ in range(len(new_map["dependencies"])):
        next_set = downstream | {loc for loc, inputs in new_map["dependencies"].items() if downstream.intersection(inputs)}
        if next_set == downstream:
            break
        downstream = next_set
    old_values = rows["before"].pop("values")
    new_values = rows["after"].pop("values")
    observed = {k: {"before": old_values.get(k), "after": new_values.get(k)}
                for k in sorted(downstream) if old_values.get(k) != new_values.get(k)}
    return {"native_changes": changed, "comparison_fields": old_map["scenario_slots"],
            "comparison_cells_changed": protected, "funding_before": old_map["schedules"], "funding_after": new_map["schedules"],
            "findings_before": old_map["findings"], "findings_after": new_map["findings"],
            "checks_after": new_map["checks"], "cache_verification": rows, "changed_derived_values": observed}


def _capture(bridge, reply):
    p = bridge.dag.get(reply["proposal_id"])["payload"]
    review = reply.get("self_review") or {}
    return {"proposal_id": reply["proposal_id"], "sha256": p["sha256"], "status": p["status"],
            "issues": p["reasons"], "conflicts": p["conflicts"], "suggested_changes": p["suggested_changes"],
            "obligations": p.get("obligation_checks", []), "finance_receipt_id": p.get("finance_receipt_id"),
            "coverage_gaps": p.get("coverage_gaps", []), "evidence_review_id": review.get("id"),
            "source_role": review.get("source_role"), "source": review.get("locator"), "evidence_paths": review.get("paths", []),
            "proof_reused_from": p.get("proof_reused_from")}


def _offline_file(base, stage, path, draft, bridge, directory, audited_workbooks):
    frozen, sha = _frozen(base, stage, path)
    original_draft = bridge.art.head(draft)
    if (not original_draft or original_draft["payload"]["sha256"] != sha
            or digest(bridge.art.materialized(draft)) != sha or digest(bridge.art._blob(draft, sha)) != sha):
        raise ValueError("Read-only historical draft bytes differ from freeze")
    result = {"path": path, "input_sha256": sha, "input_draft": draft}
    reply = bridge.call("artifact_propose", {"action": "copy", "source": draft, "source_sha256": sha, "path": path,
                        "description": "Assess this existing work for the requested destination and task."})
    result["before"] = _capture(bridge, reply)
    # No new analysis or guessed values: submit only formula edits actually
    # proposed by the normal guard after reading the version's own dependencies.
    native_edits = [edit for edit in reply.get("suggested_changes", [])
                    if isinstance(edit.get("before"), str) and edit["before"].startswith("=")
                    and isinstance(edit.get("after"), str) and edit["after"].startswith("=")]
    if native_edits:
        result["authorized_edits_requested"] = native_edits
        repaired = bridge.call("artifact_repair", {"proposal_id": reply["proposal_id"], "changes": native_edits,
                               "description": "Apply the local formula corrections justified by the inspected precedents."})
        result["repair_reply_status"] = repaired["status"]
        result["after"] = _capture(bridge, repaired)
        candidate = bridge.art._blob(path, result["after"]["sha256"])
        if path.lower().endswith(".xlsx"):
            key = (sha, digest(candidate))
            if key not in audited_workbooks:
                audited_workbooks[key] = _independent_workbook(frozen, candidate, directory / "verified_caches" / key[1])
            result["independent"] = audited_workbooks[key]
            requested = {e["locator"] for e in native_edits}
            changed = set(result["independent"]["native_changes"])
            result["protected_native_slots_preserved"] = not changed - requested
            result["all_requested_edits_applied"] = changed == requested and all(
                result["independent"]["native_changes"][e["locator"]]["after"] == e["after"] for e in native_edits)
    result["baseline_preserved"] = sha == digest(frozen) == digest(bridge.art.materialized(draft))
    # This driver never attests business conclusions or fabricates model review
    # notes. A corrected staged file is recorded separately from a published one.
    result["published"] = False
    return result


def _live_files(base, stage, drafts, bridge, directory, receipt_id):
    result = []
    cache = {}
    signed = bridge.dag.get(receipt_id) if receipt_id else None
    if signed and (signed["kind"] != "stage_submission" or signed["payload"]["stage"] != stage
                   or set(signed["payload"]["versions"]) != set(drafts)):
        raise ValueError("Submission does not cover the requested native stage")
    for path, draft in drafts.items():
        original, sha = _frozen(base, stage, path)
        row = {"path": path, "input_sha256": sha,
               "baseline_preserved": sha == digest(bridge.art.materialized(draft)) == digest(bridge.art._blob(draft, sha))}
        version = bridge.art.head(path)
        if not version or version["payload"]["stage"] != stage:
            row["published"] = False
        else:
            bridge.art.validate_head(path)
            real = bridge.art.materialized(path)
            row.update(published=True, version_id=version["id"], submitted_sha256=digest(real),
                       native_changes=_diff(_slots(original), _slots(real)), proof_id=version["payload"].get("proof_id"))
            if signed:
                row["receipt_matches_bytes"] = signed["payload"]["versions"][path] == {"id": version["id"], "sha256": digest(real)}
            if real.suffix.lower() == ".xlsx":
                key = sha, row["submitted_sha256"]
                if key not in cache:
                    cache[key] = _independent_workbook(original, real, directory / "verified_caches" / key[1])
                row["independent"] = cache[key]
                after_cache = row["independent"]["cache_verification"]["after"]
                recalculated = directory / "verified_caches" / key[1] / "after"
                info = recalculate_workbook(real, recalculated)
                if info.get("no_errors_verified") and info.get("derived_path"):
                    stored = _slots(real, cached=True)
                    actual = _slots(Path(info["derived_path"]), cached=True)
                    formulas = inspect_workbook(_slots(real))["dependencies"]
                    row["submitted_cache_mismatches"] = {loc: {"stored": stored.get(loc), "recalculated": actual.get(loc)}
                                                         for loc in formulas if stored.get(loc) != actual.get(loc)}
                    row["submitted_cache_verified"] = after_cache.get("no_errors_verified") and not row["submitted_cache_mismatches"]
                else:
                    row["submitted_cache_verified"] = False
        result.append(row)
    return result


def _history(bridge, stage):
    heads = {}
    for n in bridge.dag.nodes("artifact_version"):
        if n["payload"]["stage"] < stage:
            heads[n["payload"]["path"]] = n["payload"]["sha256"]
    return {p: digest(bridge.art.materialized(p)) if bridge.art.materialized(p).is_file() else None for p in heads}


def _check_held_mirror(base, stage, path, draft, primary, bridge):
    """A real primary that remains unsigned cannot authorize a checkout mirror."""
    frozen, sha = _frozen(base, stage, path)
    current = bridge.art.head(draft)
    if not current or current["payload"]["sha256"] != sha or digest(bridge.art.materialized(draft)) != sha:
        raise ValueError("Mirror baseline bytes changed")
    count = len(bridge.dag.nodes("artifact_check"))
    try:
        bridge.call("artifact_propose", {"action":"copy","source":draft,"source_sha256":sha,
                    "path":path,"description":"Check whether this independent historical mirror can be carried forward."})
    except ValueError as exc:
        if primary not in str(exc) or "formal primary" not in str(exc):
            raise
        redirect = str(exc)
    else:
        raise ValueError("Independent mirror write escaped the formal primary check")
    return {"path":path,"input_sha256":sha,"primary":primary,"mirror_waiting_for_primary":True,
            "redirect":redirect,"no_new_mirror_proposal":count==len(bridge.dag.nodes("artifact_check")),
            "baseline_preserved":digest(frozen)==digest(bridge.art.materialized(draft))==sha,"published":False}


def _target_result(stage, files):
    """Evaluator-side regression targets; kept outside the candidate's prompt."""
    detected = {c["kind"] for f in files for c in f.get("before", {}).get("obligations", [])
                if c["status"] in {"missing", "unmapped"}}
    checks = {"historical_inputs_preserved": bool(files) and all(f.get("baseline_preserved") for f in files)}
    if stage == 6:
        checks["missing_comparison_detected"] = "quantified_alternatives" in detected
    if stage in (8, 13):
        workbooks = [f for f in files if f.get("path", "").lower().endswith(".xlsx") and not f.get("mirror_waiting_for_primary")]
        checks["scheduled_payment_preserved"] = bool(workbooks) and all(any(
            c["kind"] == "scheduled_amortization" and c["status"] == "satisfied" and c["locators"]
            for c in f.get("before", {}).get("obligations", [])) for f in workbooks)
    if stage == 8:
        checks["missing_cash_flow_detected"] = "cash_available_for_debt_service" in detected
        checks["missing_sweep_detected"] = "cash_sweep" in detected
    if stage == 13:
        mirrors = [f for f in files if f.get("mirror_waiting_for_primary")]
        if mirrors:
            checks["unauthorized_mirror_writes_blocked"] = all(
                f["no_new_mirror_proposal"] and any(g.get("path")==f["primary"] and "before" in g for g in files)
                for f in mirrors)
        checks["missing_lender_template_field_detected"] = "fillable_commitment" in detected
        checks["missing_order_annex_detected"] = "order_annex" in detected
        books = [f for f in files if f.get("independent")]
        checks["formula_locally_repaired"] = bool(books) and all(
            bool(f["all_requested_edits_applied"]) and f["protected_native_slots_preserved"]
            and any(r["rule"] == "opening_equals_prior_closing" and r["locator"] == "Debt Service Proxy!C3" for r in f["independent"]["findings_before"])
            and not any(r["rule"] == "opening_equals_prior_closing" for r in f["independent"]["findings_after"])
            and f["independent"]["changed_derived_values"].get("Debt Service Proxy!C3") == {"before": 26500, "after": 2623500}
            for f in books)
        checks["comparison_and_funding_protected"] = bool(books) and all(
            bool(f["independent"]["comparison_fields"]) and not f["independent"]["comparison_cells_changed"]
            and f["independent"]["funding_before"] == f["independent"]["funding_after"]
            and any("Case 1" in schedule["funding"] for schedule in f["independent"]["funding_after"])
            for f in books)
    return {"checks": checks, "passed": all(checks.values())}


def run(base, out, stages, live_config=None, *, review_policy="ledger_graph"):
    from .prefix_workspace import prepare_checkpoint
    if out == base or out.is_relative_to(base):
        raise ValueError("The experiment directory must be outside the baseline")
    out.mkdir(parents=True, exist_ok=False)
    mode = "live" if live_config else "offline"
    report = {"mode": mode, "baseline": str(base), "stage_results": {}, "full_project_run": False, "review_policy": review_policy}
    report["candidate_code_sha256"] = candidate_fingerprints()
    write_json(out/"target_manifest.json", report)
    if live_config:
        settings = json.loads(live_config.read_text())
        report.update(model=settings["model"], config_sha256=digest(live_config))
        write_json(out/"target_manifest.json",report)
    for stage in stages:
        sid = f"T{stage:02d}"
        directory = out / sid
        ctx, info, drafts = prepare_checkpoint(base, directory, stage, review_policy=review_policy)
        bridge = Bridge(directory / "operator")
        if not ctx.get("self_review") or bridge.ctx["stage"] != stage:
            raise ValueError("Targeted validation requires the actual stage and enabled self-review")
        result = {"files": []}
        if report["candidate_code_sha256"] != candidate_fingerprints():
            raise ValueError("Candidate code changed during experiment; use an independent directory")
        historical_heads = _history(bridge, stage)
        if live_config:
            info = dict(info)
            info["instruction"] += ("\nExisting candidate drafts for this maintenance task (destination to draft path): " +
                                    json.dumps(drafts, ensure_ascii=False) +
                                    "\nCheck and maintain the actual drafts through the native artifact operators and submit the requested files.")
            try:
                receipt = run_stage(bridge.root, ctx, info, live_config, settings["candidate_max_turns_per_stage"])
                result["submission_receipt"] = receipt["result"]["id"]
            except DSHSessionError as exc:
                result["session_failure"] = exc.receipt
            result["files"] = _live_files(base, stage, drafts, bridge, directory, result.get("submission_receipt"))
            result["stage_completed"] = bool(result.get("submission_receipt")) and all(
                row.get("receipt_matches_bytes") and row.get("baseline_preserved")
                and row.get("submitted_cache_verified", True) for row in result["files"])
        else:
            audited = {}
            contracts = {path:bridge.art._contract(path)["payload"] for path in drafts}
            mirrors = handover_mirrors(drafts,stage,contracts)
            for path, draft in drafts.items():
                try:
                    row = (_check_held_mirror(base,stage,path,draft,mirrors[path],bridge) if path in mirrors
                           else _offline_file(base,stage,path,draft,bridge,directory,audited))
                    result["files"].append(row)
                except (ValueError, OSError, RuntimeError) as exc:
                    result["files"].append({"path": path, "error": str(exc)})
                write_json(directory / "target_result.json", result)
            result.update(_target_result(stage, result["files"]))
        result["historical_head_changes"] = _diff(historical_heads, _history(bridge, stage))
        result["historical_native_files_checked"] = len(historical_heads)
        if result["historical_head_changes"]:
            result["stage_completed" if live_config else "passed"] = False
        if report["candidate_code_sha256"] != candidate_fingerprints():
            raise ValueError("Candidate code changed within the stage; results are not a fixed-version trial")
        write_json(directory / "target_result.json", result)
        report["stage_results"][sid] = {k: v for k, v in result.items() if k != "files"}
        write_json(out / "target_summary.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--stages", nargs="+", type=int, choices=(6, 8, 13), default=[6, 8, 13])
    parser.add_argument("--live-config", type=Path)
    parser.add_argument("--review-policy", choices=("ledger_only", "ledger_graph"), default="ledger_graph")
    args = parser.parse_args()
    report = run(args.base.resolve(), args.out.resolve(), list(dict.fromkeys(args.stages)),
                 args.live_config.resolve() if args.live_config else None, review_policy=args.review_policy)
    print(json.dumps(report["stage_results"], ensure_ascii=False))
    if not all(x.get("passed", x.get("stage_completed", False)) for x in report["stage_results"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

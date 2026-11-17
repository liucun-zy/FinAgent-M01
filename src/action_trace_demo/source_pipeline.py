"""Source-scoped M01 jobs: plan, execute, resume and recover bounded work.

The extractor sees document elements, not the storage catalog. Stable units are
the work/cache boundary; a successful sibling is never sent again for repair.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .evidence_windows import request_input_estimate, token_estimate

PIPELINE_VERSION = "source-elements-v1"


def extraction_profile(model, settings, task_context):
    from .source_extraction import SYSTEM_PROMPT, tool_specs
    from .source_units import SOURCE_UNIT_VERSION
    from .evidence_windows import NORMALIZATION_VERSION
    def digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    profile = {"source_pipeline_version": PIPELINE_VERSION, "source_unit_version": SOURCE_UNIT_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "prompt_sha256": digest(SYSTEM_PROMPT), "schema_sha256": digest(tool_specs()),
        "task_sha256": digest(task_context),
        "model_settings_sha256": digest({k: model[k] for k in
            ("model", "base_url", "temperature", "top_p", "seed", "max_output_tokens") if k in model}),
        "source_unit_max_chars": settings.get("source_unit_max_chars", 1800),
        "source_context_max_chars": settings.get("source_context_max_chars", 1200)}
    return {**profile, "profile_id": digest(profile)}


def make_group(arrival, units, settings, profile, group_id, order, sources):
    from .source_units import render_packet
    from .source_extraction import SYSTEM_PROMPT, tool_specs, build_prompt
    packet = render_packet(units)
    group = {"arrival_id": arrival["arrival_id"], "window_group_id": group_id,
        "group_order": order, "source_packet": packet, "source_pipeline_version": PIPELINE_VERSION,
        "windows": packet["windows"], "evidence_refs": packet["evidence_refs"],
        "blocks": [], "entity_candidates": [], "sources": sources,
        "task_context": arrival.get("task_context"), "config": settings, "extraction_profile": profile}
    prompt = build_prompt(group)
    group["estimated_input_tokens"] = request_input_estimate(SYSTEM_PROMPT, prompt, tool_specs())
    # Source facts repeat only a small literal and their semantic attributes.
    # Keep rows/paragraphs small rather than predicting a gold number of facts.
    group["output_token_estimate"] = 128 + 5 * token_estimate(packet["model_input"])
    group["capacity_policy"] = {"strategy": PIPELINE_VERSION,
        "input_limit": settings["batch_input_budget_tokens"],
        "output_limit": settings["extraction_output_max_tokens"],
        "max_units": settings.get("source_batch_max_units", 8)}
    return group, prompt


def plan_groups(arrival, parsed, store, settings, *, extraction_profile, processed_units=(), order_offset=0):
    from .source_units import build_source_units
    completed = set(processed_units)
    groups, unresolved = [], []
    all_blocks = [b for item in parsed for b in item["blocks"]]
    units = build_source_units(all_blocks, max_unit_chars=settings.get("source_unit_max_chars", 1800),
                               context_chars=settings.get("source_context_max_chars", 1200))
    by_source = {}
    for unit in units:
        if unit["unit_id"] not in completed:
            by_source.setdefault(unit["source_version_id"], []).append(unit)
    for source_id, entries in by_source.items():
        source = store.get_source(source_id)
        sources = [{k: source.get(k) for k in ("source_version_id", "display_name", "document_date")}]
        pending = []
        def candidate(items):
            gid = f'{arrival["arrival_id"]}_{extraction_profile["profile_id"][:10]}_G{order_offset + len(groups):04d}'
            return make_group(arrival, items, settings, extraction_profile, gid, order_offset + len(groups), sources)
        for unit in entries:
            proposed, prompt = candidate([*pending, unit])
            too_large = (proposed["estimated_input_tokens"] > settings["batch_input_budget_tokens"] or
                         proposed["output_token_estimate"] > settings["extraction_output_max_tokens"] - 512 or
                         len(pending) >= settings.get("source_batch_max_units", 8))
            if pending and too_large:
                groups.append(candidate(pending))
                pending = []
                proposed, prompt = candidate([unit])
            if proposed["estimated_input_tokens"] > settings["batch_input_budget_tokens"]:
                unresolved.append({"unit_id": unit["unit_id"], "source_version_id": source_id,
                    "reason": "Source unit plus required context exceeds hard request capacity",
                    "estimated_input_tokens": proposed["estimated_input_tokens"]})
            else:
                pending.append(unit)
        if pending:
            groups.append(candidate(pending))
    return groups, unresolved


def execute_group(group, prompt, root, model_config, store, executor, record_plan):
    from .source_extraction import SYSTEM_PROMPT, tool_specs
    from .model import SharedAttemptBudget, BudgetExceeded
    gid = group["window_group_id"]
    prior = sorted((root / "groups" / gid).glob("**/invocation.json"))
    used = sum(json.loads(p.read_text()).get("steps", 0) or 0 for p in prior)
    remaining = group["config"]["max_model_requests_per_group"] - used
    pending = {"window_group_id": gid, "unit_ids": [w["window_id"] for w in group["windows"]]}
    if remaining < 1:
        return [], [{**pending, "reason": "Source job request allowance exhausted; saved units retained"}]
    current = {**group, "config": {**group["config"], "max_model_requests_per_group": remaining}}
    if prior:
        state_files = sorted((root / "groups" / gid).glob("**/ingest_tool_state.json"), key=lambda p: p.stat().st_mtime_ns)
        if state_files:
            state = json.loads(state_files[-1].read_text())
            prompt += "\nResume only the pending units in this saved receipt: " + json.dumps(state.get("receipt", {}), ensure_ascii=False)
    estimate = request_input_estimate(SYSTEM_PROMPT, prompt, tool_specs())
    if estimate > group["config"]["batch_input_budget_tokens"]:
        return [], [{**pending, "reason": "Source repair context exceeds hard request capacity",
                     "estimated_input_tokens": estimate}]
    receipt = executor(current, root, model_config, prompt=prompt)
    attempts = [receipt]
    if receipt.get("committed") and receipt.get("session_complete") is not False:
        return attempts, []
    pending["reason"] = receipt.get("error", "Unfinished source units; see independent save receipts")
    progress = store.group_extraction_progress(group["arrival_id"], gid, group["extraction_profile"]["profile_id"])
    done = set(progress["processed_unit_ids"])
    pending["unit_ids"] = [wid for wid in pending["unit_ids"] if wid not in done]
    # A length recovery subdivides the unit list once. A partially saved unit
    # remains on its explicit claim-repair path instead of inventing new facts.
    saved = progress["has_saved_units"]
    units = list(group["source_packet"]["units"].values())
    if not receipt.get("truncated") or saved or group.get("recovery_parent_id") or len(units) < 2:
        return attempts, [pending]
    halves = [units[:len(units)//2], units[len(units)//2:]]
    children = []
    for index, half in enumerate(halves):
        child_id = f"{gid}_R{index}"
        child, child_prompt = make_group({"arrival_id": group["arrival_id"], "task_context": group.get("task_context")},
            half, {**group["config"], "max_model_requests_per_group": 1}, group["extraction_profile"],
            child_id, group["group_order"] + (index + 1) / 10, group["sources"])
        child.update(original_group_id=group.get("original_group_id", gid), recovery_parent_id=gid)
        children.append((child, child_prompt))
    private = json.loads(Path(model_config).read_text())
    provider = private["model"] if isinstance(private.get("model"), dict) else private
    limits = {"original_group": group["config"]["max_transport_attempts_per_group"]}
    if "m01" in private.get("transport_stage_limits", {}):
        limits["stage"] = private["transport_stage_limits"]["m01"]
    try:
        SharedAttemptBudget(root / ".dsh").update(max_calls=provider["max_calls"],
            tags={"stage": "m01", "original_group": gid}, limits=limits,
            recovery_slots=[child["window_group_id"] for child, _ in children])
    except BudgetExceeded as error:
        return attempts, [{**pending, "recovery_not_started": str(error)}]
    for child, child_prompt in children:
        record_plan(store, root, child, child_prompt)
    store.record_group_split(group, [child for child, _ in children], receipt)
    unfinished = []
    for child, child_prompt in children:
        child_attempts, child_pending = execute_group(child, child_prompt, root, model_config, store, executor, record_plan)
        attempts.extend(child_attempts)
        unfinished.extend(child_pending)
    return attempts, unfinished

"""Deduplicated, as-of comparison material for M02 candidate retrieval."""
from __future__ import annotations

from html import escape
import json
from pathlib import Path
import sqlite3

from .evidence_windows import read_context, token_estimate
from .ingestion_store import IngestionStore
from .initial_context import _cell, _value
from .claim_view import semantic_view, observation_view


def _project_fields(record, fields):
    return {field: record[field] for field in fields if field in record}


def _maintenance_evidence(ref):
    """A verbatim quotation; normalized values remain in the claim objects."""
    return _project_fields(ref, ("source_version_id", "raw_content"))


def build_maintenance_context(workspace, batch, task_card=None, *, prefetch_token_budget=2000):
    """Project one frozen candidate batch for the Agent without changing its audit.

    Only structurally linked table context is prefetched. The database is opened
    read-only, so this projection is also safe on an existing frozen workspace.
    """
    materials = batch["materials"]
    item_fields = ("entity_id", "predicate", "display_statement", "display_value", "qualifiers",
                   "canonical_qualifiers", "effective_time", "assertion_ids", "field_sources", "semantic_contract_version")
    claim_fields = ("entity_id", "predicate", "statement", "raw_value", "extracted_value", "value", "qualifiers",
                    "canonical_qualifiers", "effective_time", "source_version_id", "arrival_id",
                    "evidence_ref_ids", "temporal", "field_evidence", "semantic_contract_version")
    task_fields = ("task_id", "created_record_id", "instruction_text", "instruction_ref", "goal_text",
                   "scope_text", "output_requirements")
    items = {iid: _project_fields(item, item_fields) for iid, item in materials["items"].items()}
    claims = {cid: _project_fields(claim, claim_fields) for cid, claim in materials["claims"].items()}
    for records, originals in ((items, materials["items"]), (claims, materials["claims"])):
        for identity, record in records.items():
            if record.get("canonical_qualifiers") == record.get("qualifiers"):
                record.pop("canonical_qualifiers", None)
            links = originals[identity].get("qualifier_entity_links", [])
            if links:
                record["qualifier_entity_links"] = [
                    _project_fields(link, ("path", "entity_id", "surface"))
                    for link in links]
    requests = {cid: [_project_fields(r, ("window_id", "relation")) for r in entries]
                for cid, entries in materials["context_requests"].items()}
    prefetch = list({(r["window_id"], r["relation"]): r
                     for entries in requests.values() for r in entries
                     if r["relation"] == "table_context"}.values())
    context = {"evidence_refs": {}, "requests": prefetch, "unresolved": [], "token_estimate": 0}
    compact = {
        "task_card": _project_fields(task_card, task_fields) if task_card else None,
        "batch_id": batch["batch_id"],
        "base_ledger_snapshot_id": batch["ledger_snapshot"]["ledger_snapshot_id"],
        "visible_as_of": batch["input_scope"]["visible_as_of"],
        "input_claim_ids": batch["input_scope"]["input_claim_ids"],
        "retrieval_coverage": {
            "base_active_item_count": len(batch["ledger_snapshot"]["items"]),
            "inputs": {row["new_claim_id"]: {
                "eligible_target_count": row["eligible_candidate_count"],
                "has_more_targets": row["has_more"],
                "eligible_context_count": row.get("eligible_context_count", 0),
                "has_more_context": row.get("has_more_context", False)} for row in batch["results"]},
            "note": "Expansion only enlarges this retrieval's finite list; it cannot create absent ledger items or a new semantic search channel."},
        "candidates": {row["new_claim_id"]: [{"ledger_item_id": item["ledger_item_id"],
                        "rank": item["rank"],
                        "reasons": list(dict.fromkeys(reason["kind"] for reason in item["recall_basis"]))}
                        for item in row["candidates"]] for row in batch["results"]},
        "context_items": {row["new_claim_id"]: [{"ledger_item_id": item["ledger_item_id"],
                          "reasons": list(dict.fromkeys(reason["kind"] for reason in item["recall_basis"]))}
                          for item in row.get("context_items", [])] for row in batch["results"]},
        "items": items, "claims": claims,
        "representation_layers": {
            "source_observation": "direct_evidence and separately read original context; verbatim only",
            "raw_value": "saved model interpretation of the original value; not source bytes",
            "extracted_value": "model value before mechanical normalization; null if unavailable in historical extraction",
            "value": "stored normalized interpretation after the existing mechanical conversion",
            "role_fields": {"subject": "entity_id", "relation": "predicate", "object": "value/display_value",
                            "qualifiers": "qualifiers", "valid_time": "effective_time",
                            "event_time": "temporal.events; not applicable time",
                            "observed_at": "arrival_id -> arrivals.received_at"}},
        "entities": {eid: _project_fields(entity, ("canonical_name", "description", "aliases",
                                                    "identity_clues", "entity_type"))
                     for eid, entity in materials["entities"].items()},
        "sources": {sid: _project_fields(source, ("display_name", "document_date"))
                    for sid, source in materials["sources"].items()},
        "arrivals": {aid: _project_fields(arrival, ("received_at", "source_version_ids"))
                     for aid, arrival in materials["arrivals"].items()},
        "direct_evidence": {}, "context_requests": requests, "context": context,
    }
    budget = batch.get("parameters", {}).get("context_budget")
    required_tokens = token_estimate(compact)
    remaining = max(0, budget - required_tokens) if budget is not None else None
    deferred = []
    for rid, ref in materials["evidence_refs"].items():
        projected = _maintenance_evidence(ref)
        cost = token_estimate({rid: projected})
        if remaining is not None and cost > remaining:
            deferred.append(rid)
        else:
            compact["direct_evidence"][rid] = projected
            if remaining is not None:
                remaining -= cost
    if remaining is not None:
        prefetch_token_budget = min(prefetch_token_budget, remaining)
    if prefetch and prefetch_token_budget > 0:
        source_ids = list(materials["sources"])
        database = Path(workspace) / "temporal_dag.sqlite3"
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT payload FROM event WHERE kind='m01_parse' AND seq<=? "
                "AND json_extract(payload,'$.source_version_id') IN ("
                + ",".join("?" for _ in source_ids) + ") ORDER BY seq",
                [batch["input_scope"]["visible_as_of"], *source_ids])
            parses = {}
            for (payload,) in rows:
                parse = json.loads(payload)
                parses[parse["source_version_id"]] = parse
        blocks = [block for parse in parses.values() for block in parse["blocks"]]
        windows = [window for parse in parses.values() for window in parse["windows"]]
        direct_blocks = list({ref["block_id"] for ref in materials["evidence_refs"].values()})
        loaded = read_context(blocks, windows, prefetch, direct_blocks, prefetch_token_budget)
        context.update(evidence_refs={ref["evidence_ref_id"]: _maintenance_evidence(ref)
                                      for ref in loaded["evidence_refs"]
                                      if ref["evidence_ref_id"] not in materials["evidence_refs"]},
                       unresolved=loaded["unresolved"], token_estimate=loaded["token_estimate"])
    compact["context_delivery"] = {
        "budget_tokens": budget, "required_metadata_tokens": required_tokens,
        "deferred_evidence_ref_ids": deferred,
        "read_claim_ids": [cid for cid, c in claims.items() if set(c["evidence_ref_ids"]) & set(deferred)],
        "required_metadata_exceeds_budget": budget is not None and required_tokens > budget,
        "note": "Missing text is delayed, not missing evidence or a no-candidate decision; read by claim ID."}
    compact["context_delivery"]["estimated_tokens"] = token_estimate(compact)
    return compact


def build_comparison_materials(exported, new_claim_ids, candidate_item_map):
    """Organize existing source records once; never inherit item conditions into claims."""
    claim_ids = dict.fromkeys([*new_claim_ids, *(
        cid for item in candidate_item_map.values() for cid in item["assertion_ids"]), *(
        cid for item in candidate_item_map.values() for ids in item.get("field_sources", {}).values()
        for cid in ids)])
    by_claim = {c["assertion_id"]: c for c in exported["source_claims"]}
    selected = [by_claim[cid] for cid in claim_ids]
    source_ids = {c["source_version_id"] for c in selected}
    parses = {p["source_version_id"]: p for p in exported["parses"]
              if p["source_version_id"] in source_ids}
    evidence = {ref["evidence_ref_id"]: ref for claim in selected for ref in claim["evidence_refs"]}
    claims = {}
    context_requests = {}
    extracted_values = {
        (extraction["record_id"], c.get("source_version_id"), c["subject_local_id"], c["predicate"], c["statement"]): c.get("value")
        for extraction in exported.get("extractions", []) for c in extraction["result"]["claims"]}
    entity_ids = set()
    for obj in [*selected, *candidate_item_map.values()]:
        entity_ids.add(obj.get("subject_entity_id", obj.get("entity_id")))
        value = obj.get("value", obj.get("display_value")) or {}
        if value.get("kind") == "entity" and value.get("entity_id"):
            entity_ids.add(value["entity_id"])
        entity_ids.update(link["entity_id"] for link in obj.get("qualifier_entity_links", []))
    for claim in selected:
        cid = claim["assertion_id"]
        claims[cid] = {key: value for key, value in claim.items() if key != "evidence_refs"}
        claims[cid]["evidence_ref_ids"] = [r["evidence_ref_id"] for r in claim["evidence_refs"]]
        claims[cid]["extracted_value"] = extracted_values.get((claim.get("extraction_run_id"),
            claim["source_version_id"], claim.get("subject_local_id"),
            claim.get("source_representation", {}).get("predicate", claim["predicate"]), claim["statement"]))
        direct_blocks = {r["block_id"] for r in claim["evidence_refs"]}
        relation = "table_context" if any(r.get("locator", {}).get("table_id")
                                           for r in claim["evidence_refs"]) else "neighbors"
        parse = parses.get(claim["source_version_id"])
        context_requests[cid] = [
            {"window_id": w["window_id"], "relation": relation,
             "source_version_id": claim["source_version_id"], "parse_record_id": parse["record_id"]}
            for w in parse["windows"]
            if direct_blocks.intersection([*w["main_block_ids"], *w.get("context_block_ids", [])])
        ] if parse else []
    arrival_ids = {c["arrival_id"] for c in selected}
    sources = {}
    for source in exported["sources"]:
        sid = source["source_version_id"]
        if sid in source_ids:
            parse = parses.get(sid)
            sources[sid] = {**source,
                "parse_record_id": parse["record_id"] if parse else None,
                "parse_result_location": parse["parse_result_location"] if parse else None}
    entity_map = {e["entity_id"]: e for e in exported["entities"] if e["entity_id"] in entity_ids}
    arrivals = {a["arrival_id"]: a for a in exported["arrivals"] if a["arrival_id"] in arrival_ids}
    return {"items": dict(candidate_item_map), "claims": claims,
            "entities": entity_map,
            "sources": sources,
            "arrivals": arrivals,
            "evidence_refs": evidence, "context_requests": context_requests,
            "semantic_views": {cid: semantic_view(claim, entity_map,
                               arrival=arrivals[claim["arrival_id"]], source=sources[claim["source_version_id"]])
                               for cid, claim in claims.items()},
            "source_observations": {cid: observation_view(claim, evidence) for cid, claim in claims.items()}}


def read_candidate_context(workspace, batch_id, claim_id, requests=None, extra_token_budget=2000,
                           provided_direct_ref_ids=None, provided_context_ref_ids=None):
    """Expand saved evidence at the batch visibility cutoff, separately from direct quotes."""
    store = IngestionStore(workspace)
    batch = next((n["payload"] for n in store.dag.nodes("m02_candidate_batch") if n["id"] == batch_id), None)
    if batch is None:
        raise ValueError("Candidate batch does not exist")
    if claim_id not in batch["materials"]["claims"]:
        raise ValueError("Claim is not part of this candidate batch")
    cutoff = batch["input_scope"]["visible_as_of"]
    exported = store.export(as_of=cutoff)
    claim = batch["materials"]["claims"][claim_id]
    parse = next((p for p in reversed(exported["parses"])
                  if p["source_version_id"] == claim["source_version_id"]), None)
    selected_requests = batch["materials"]["context_requests"][claim_id] if requests is None else requests
    direct_blocks = [batch["materials"]["evidence_refs"][rid]["block_id"]
                     for rid in claim["evidence_ref_ids"]]
    known_context = set(provided_context_ref_ids or [])
    direct_blocks.extend(ref["block_id"] for ref in (parse or {}).get("evidence_refs", [])
                         if ref["evidence_ref_id"] in known_context)
    remaining, direct, deferred = extra_token_budget, {}, []
    provided_direct = set(provided_direct_ref_ids or [])
    for rid in claim["evidence_ref_ids"]:
        if rid in provided_direct:
            continue
        ref = batch["materials"]["evidence_refs"][rid]
        projected = _maintenance_evidence(ref)
        cost = token_estimate({rid: projected})
        if cost <= remaining:
            direct[rid] = projected
            remaining -= cost
        else:
            deferred.append(rid)
    direct_cost = extra_token_budget - remaining
    # Exact evidence reads need no model-supplied window. Resolve only within
    # this claim's source parse at the batch's fixed visibility cutoff.
    indexed = {r["evidence_ref_id"]: r for r in (parse or {}).get("evidence_refs", [])}
    windows, exact, unresolved, already_context = [], {}, [], []
    for request in selected_requests:
        if "evidence_ref_id" not in request:
            windows.append(request)
            continue
        rid = request["evidence_ref_id"]
        if rid not in indexed:
            raise ValueError("Evidence ID is outside the claim's source at the pinned visible time")
        if rid in claim["evidence_ref_ids"]:
            continue  # Delivered, already provided, or explicitly deferred above.
        if rid in known_context:
            already_context.append(rid)
            continue
        if rid in exact:
            continue
        ref = indexed[rid]
        cost = token_estimate({rid: _maintenance_evidence(ref)})
        if cost > remaining:
            unresolved.append({"evidence_ref_id": rid, "reason": "context_budget"})
            continue
        exact[rid] = ref
        direct_blocks.append(ref["block_id"])
        remaining -= cost
    exact_cost = extra_token_budget - direct_cost - remaining
    context = read_context(parse["blocks"] if parse else [], parse["windows"] if parse else [],
                           windows, direct_blocks, remaining)
    context["evidence_refs"] = [*exact.values(), *context["evidence_refs"]]
    context["unresolved"] = [*unresolved, *context["unresolved"]]
    context["token_estimate"] += exact_cost
    return {"batch_id": batch_id, "claim_id": claim_id, "visible_as_of": cutoff,
            "direct_evidence_ref_ids": claim["evidence_ref_ids"], "direct_evidence": direct,
            "already_provided_direct_ref_ids": sorted(set(claim["evidence_ref_ids"]) & provided_direct),
            "already_provided_context_ref_ids": sorted(set(already_context)),
            "deferred_direct_evidence_ref_ids": deferred,
            "direct_token_estimate": direct_cost, "context": context}


def _links(ids):
    return "、".join("[" + _cell(identity) + "](#" + escape(identity, quote=True) + ")" for identity in ids)


def render_candidates(bundle):
    """Render references to shared items, claims and evidence rather than copying each pair."""
    materials = bundle["materials"]
    names = {eid: entity["canonical_name"] for eid, entity in materials["entities"].items()}
    lines = ["# 新旧事项候选配对", "",
             "候选仅表示值得比较，不表示冲突；没有候选也不代表已经确定新增。", "",
             "批次：" + _cell(bundle["batch_id"]), "",
             "台账版本：" + _cell(bundle["ledger_snapshot"]["ledger_snapshot_id"]), "",
             "本轮证据可见截止点：" + _cell(bundle["input_scope"]["visible_as_of"]), "",
             "## 候选列表", "",
             "| 新 claim | 新陈述 | 维护目标候选 | 辅助上下文事项 | 本次召回 |", "|---|---|---|---|---|"]
    for row in bundle["results"]:
        cid = row["new_claim_id"]
        lines.append("| " + _links([cid]) + " | " + _cell(materials["claims"][cid]["statement"])
                     + " | " + (_links([c["ledger_item_id"] for c in row["candidates"]]) or "—")
                     + " | " + (_links([c["ledger_item_id"] for c in row.get("context_items", [])]) or "—")
                     + " | " + _cell(row["retrieval_status"]) + " |")
    for row in bundle["results"]:
        lines += ["", "**" + _cell(row["new_claim_id"]) + " 的召回依据**", ""]
        for candidate in row["candidates"]:
            lines.append("- " + _links([candidate["ledger_item_id"]]) + "：" + _cell(candidate["recall_basis"]))
        for candidate in row.get("context_items", []):
            lines.append("- 辅助上下文（不表示维护目标） " + _links([candidate["ledger_item_id"]])
                         + "：" + _cell(candidate["recall_basis"]))
        if not row["candidates"]:
            lines.append("本次未召回相关旧事项，留待后续判断或扩大检索。")
        lines += ["", "本次满足召回条件的事项数：" + str(row["eligible_candidate_count"])
                  + "；列表外仍有候选：" + ("是" if row["has_more"] else "否") + "。"]
    lines += ["", "可用同一批次 ID 和更大的 `--limit` 扩大候选；仍使用该批次的台账与证据时点。", "",
              "## 共享旧事项", "",
              "每个旧事项只列一次；以下汇总条件属于事项，不自动归属于每条来源 claim。", ""]
    for iid, item in materials["items"].items():
        entity_id = item.get("subject_entity_id", item.get("entity_id"))
        lines += ['<a id="' + escape(iid, quote=True) + '"></a>', "<details>",
                  "<summary>" + _cell(iid + " · " + item["predicate"]) + "</summary>", "",
                  "主体：" + _links([entity_id]) + "（" + _cell(names.get(entity_id, entity_id)) + "）", "",
                  "规范陈述：" + _cell(item["display_statement"]), "",
                  "规范值：" + _cell(_value(item.get("display_value"), names)), "",
                  "事项限定：" + _cell(item.get("qualifiers", {})), "",
                  "事项规范限定：" + _cell(item.get("canonical_qualifiers", {})), "",
                  "业务时间：" + _cell(item.get("effective_time")), "",
                  "相关实体关联：" + _cell(item.get("qualifier_entity_links", [])), "",
                  "独立来源 claim：" + _links(item["assertion_ids"]), "", "</details>", ""]
    lines += ["## 独立来源 claim", "",
              "原值为保存的提取字段，规范值为提取后的表示；逐字原文在下方证据表中独立保存。", ""]
    for cid, claim in materials["claims"].items():
        sid, aid = claim["source_version_id"], claim["arrival_id"]
        entity_id = claim["entity_id"]
        lines += ['<a id="' + escape(cid, quote=True) + '"></a>', "<details>",
                  "<summary>" + _cell(cid + " · " + claim["predicate"]) + "</summary>", "",
                  "主体：" + _links([entity_id]) + "（" + _cell(names.get(entity_id, entity_id)) + "）", "",
                  "来源陈述：" + _cell(claim["statement"]), "",
                  "提取原值字段（非逐字引语）：" + _cell(claim.get("raw_value")), "",
                  "机械规范前的模型值：" + _cell(claim.get("extracted_value")), "",
                  "规范值：" + _cell(_value(claim.get("value"), names)), "",
                  "本来源限定：" + _cell(claim.get("qualifiers", {})), "",
                  "本来源规范限定：" + _cell(claim.get("canonical_qualifiers", {})), "",
                  "业务时间：" + _cell(claim.get("effective_time")), "",
                  "来源：" + _links([sid]) + "；抵达：" + _links([aid]), "",
                  "相关实体关联：" + _cell(claim.get("qualifier_entity_links", [])), "",
                  "直接原文：" + _links(claim["evidence_ref_ids"]), "",
                  "可展开上下文请求：" + _cell(materials["context_requests"][cid]), "", "</details>", ""]
    lines += ["## 实体资料", ""]
    for eid, entity in materials["entities"].items():
        lines += ['<a id="' + escape(eid, quote=True) + '"></a>', "<details>",
                  "<summary>" + _cell(eid + " · " + entity["canonical_name"]) + "</summary>", "",
                  "描述：" + _cell(entity.get("description")), "",
                  "已确认别名：" + _cell(entity.get("aliases", [])), "",
                  "业务标识：" + _cell(entity.get("identity_clues", {})), "", "</details>", ""]
    lines += ["## 直接证据（按 ID 去重）", ""]
    for rid, ref in materials["evidence_refs"].items():
        lines += ['<a id="' + escape(rid, quote=True) + '"></a>', "<details>",
                  "<summary>" + _cell(rid) + "</summary>", "",
                  "来源：" + _links([ref["source_version_id"]]), "",
                  "定位：" + _cell(ref["locator"]), "",
                  "<pre>" + escape(ref["raw_content"]) + "</pre>", "", "</details>", ""]
    lines += ["## 来源与抵达", ""]
    for sid, source in materials["sources"].items():
        lines += ['<a id="' + escape(sid, quote=True) + '"></a>',
                  "**" + _cell(sid + " · " + source["display_name"]) + "**", "",
                  "材料日期：" + _cell(source.get("document_date")), "",
                  "原件位置：" + _cell(source["original_location"]), "",
                  "解析记录：" + _cell(source["parse_record_id"]), ""]
    for aid, arrival in materials["arrivals"].items():
        source_labels = "、".join(_links([sid]) if sid in materials["sources"] else _cell(sid)
                                  for sid in arrival["source_version_ids"])
        lines += ['<a id="' + escape(aid, quote=True) + '"></a>',
                  "**" + _cell(aid) + "** · 抵达时间：" + _cell(arrival["received_at"]), "",
                  "来源：" + source_labels, ""]
    metadata = {key: value for key, value in bundle["ledger_snapshot"].items() if key != "items"}
    lines += ["## 范围与用量", "", "<pre>" + escape(json.dumps({
        "ledger_snapshot": metadata, "input_scope": bundle["input_scope"], "metrics": bundle["metrics"]},
        ensure_ascii=False, indent=2)) + "</pre>", ""]
    return "\n".join(lines)

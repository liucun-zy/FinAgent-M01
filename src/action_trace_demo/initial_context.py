"""M02 initialization: views over the existing M01 ledger and evidence."""
from __future__ import annotations

from html import escape
import json

from .evidence_windows import read_context
from .ingestion_store import IngestionStore


def _snapshot_export(store, state):
    return store.export(state["initial_arrival_id"], as_of=state["source_cutoff_event_seq"])


def build_initial_context(workspace, task_input=None, arrival_id=None):
    """Save explicit task input and organize one existing or first M01 ledger."""
    store = IngestionStore(workspace)
    state = store.initial_ledger_state()
    if state and arrival_id and arrival_id != state["initial_arrival_id"]:
        raise ValueError("Initial ledger already exists for arrival " + state["initial_arrival_id"]
                         + "; other arrivals remain available through the existing increment export")
    card = store.task_card(task_input)
    if state is None:
        arrivals = store.export()["arrivals"]
        if arrival_id is None and arrivals:
            arrival_id = arrivals[0]["arrival_id"]
        if arrival_id:
            store.build_initial_ledger(arrival_id)
            state = store.initial_ledger_state()
    if state is None:
        return {"task_card": card, "entities": [], "items": [], "source_catalog": [],
                "snapshot": {"task_id": card["task_id"] if card else None,
                             "initial_arrival_id": None, "ledger_snapshot_id": None,
                             "included_claim_ids": [], "source_cutoff_event_seq": 0,
                             "normalization_as_of": None}, "notes": []}

    exported = _snapshot_export(store, state)
    arrival = next(a for a in exported["arrivals"] if a["arrival_id"] == state["initial_arrival_id"])
    claims = {c["assertion_id"]: c for c in exported["source_claims"]}
    items = [{**item, "subject_entity_id": item["entity_id"],
              "source_version_ids": list(dict.fromkeys(
                  claims[cid]["source_version_id"] for cid in item["assertion_ids"]))}
             for item in state["items"]]
    entity_ids = {m["entity_id"] for m in exported["entity_mentions"]
                  if m["arrival_id"] == state["initial_arrival_id"]}
    for item in items:
        entity_ids.add(item["subject_entity_id"])
        value = item.get("display_value") or {}
        if value.get("kind") == "entity" and value.get("entity_id"):
            entity_ids.add(value["entity_id"])
        entity_ids.update(link["entity_id"] for link in item.get("qualifier_entity_links", []))
    entities = [e for e in exported["entities"] if e["entity_id"] in entity_ids]
    results = [r for r in exported["results"] if r["arrival_id"] == state["initial_arrival_id"]]
    result = results[-1] if results else {}
    source_catalog = []
    for source in exported["sources"]:
        sid = source["source_version_id"]
        if sid not in arrival["source_version_ids"]:
            continue
        parses = [p for p in exported["parses"] if p["source_version_id"] == sid]
        failures = [f for f in exported["source_failures"]
                    if f["arrival_id"] == state["initial_arrival_id"] and f["source_version_id"] == sid]
        source_catalog.append({**source,
            "parse_result_location": parses[-1]["parse_result_location"] if parses else None,
            "parse_record_ids": [p["record_id"] for p in parses],
            "arrival_status": result.get("status"), "source_failures": failures})
    notes = ([{"ingestion_unresolved_items": result["unresolved_items"]}]
             if result.get("unresolved_items") else [])
    snapshot = {k: v for k, v in state.items() if k != "items"}
    snapshot["task_id"] = card["task_id"] if card else None
    return {"task_card": card, "entities": entities, "items": items,
            "source_catalog": source_catalog, "snapshot": snapshot, "notes": notes}


def read_initial_item_evidence(workspace, ledger_item_id, ledger_snapshot_id, *,
                               context_requests=None, extra_token_budget=2000):
    """Read pinned direct evidence; optional context remains separately labelled."""
    store = IngestionStore(workspace)
    state = store.initial_ledger_state()
    if state is None or state["ledger_snapshot_id"] != ledger_snapshot_id:
        raise ValueError("The requested initial ledger snapshot does not exist in this workspace")
    item = next((i for i in state["items"] if i["ledger_item_id"] == ledger_item_id), None)
    if item is None:
        raise ValueError("The item does not belong to the requested initial ledger snapshot")
    exported = _snapshot_export(store, state)
    by_id = {c["assertion_id"]: c for c in exported["source_claims"]}
    claims = [by_id[cid] for cid in item["assertion_ids"]]
    source_ids = {c["source_version_id"] for c in claims}
    parses = {p["source_version_id"]: p for p in exported["parses"]
              if p["source_version_id"] in source_ids}
    blocks = [b for p in parses.values() for b in p["blocks"]]
    windows = [w for p in parses.values() for w in p["windows"]]
    views = []
    for claim in claims:
        direct = claim["evidence_refs"]
        block_ids = {r["block_id"] for r in direct}
        relation = "table_context" if any(r.get("locator", {}).get("table_id") for r in direct) else "neighbors"
        entries = [{"window_id": w["window_id"], "relation": relation}
                   for w in windows if w["source_version_id"] == claim["source_version_id"]
                   and block_ids.intersection([*w.get("main_block_ids", []), *w.get("context_block_ids", [])])]
        views.append({**claim, "direct_evidence": direct, "context_requests": entries})
    context = {"blocks": [], "evidence_refs": [], "unresolved": [], "token_estimate": 0}
    if context_requests:
        direct_blocks = list(dict.fromkeys(r["block_id"] for c in claims for r in c["evidence_refs"]))
        context = read_context(blocks, windows, context_requests, direct_blocks, extra_token_budget)
    return {"ledger_item_id": ledger_item_id, "ledger_snapshot_id": ledger_snapshot_id,
            "source_cutoff_event_seq": state["source_cutoff_event_seq"],
            "claims": views, "context": context}


def _text(value):
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _cell(value):
    return escape(_text(value)).replace("|", "&#124;").replace("\n", "<br>")


def _value(value, entities):
    if not isinstance(value, dict):
        return _text(value)
    if value.get("kind") == "entity":
        return entities.get(value.get("entity_id"), value.get("normalized", value.get("entity_id", "—")))
    if "normalized" in value:
        return str(value["normalized"]) + (" " + value["unit"] if value.get("unit") else "")
    return _text(value)


def render_initial_context(bundle, evidence_views=None):
    """A compact readable table, with original evidence behind details controls."""
    card, snapshot = bundle["task_card"], bundle["snapshot"]
    names = {e["entity_id"]: e["canonical_name"] for e in bundle["entities"]}
    source_names = {s["source_version_id"]: s["display_name"] for s in bundle["source_catalog"]}
    lines = ["# 初始台账", "", "## 任务卡", ""]
    if card:
        lines += ["<pre>" + escape(card["instruction_text"]) + "</pre>", "",
                  "任务 ID：" + _cell(card["task_id"]), ""]
        for key, label in (("instruction_ref", "输入位置"), ("goal_text", "目标"),
                           ("scope_text", "范围"), ("output_requirements", "输出要求")):
            if card.get(key) is not None:
                lines.append("- " + label + "：" + _cell(card[key]))
        lines.append("")
    else:
        lines += ["尚未提供任务原文。", ""]
    lines += ["## 实体目录", "", "| 实体 | 名称 | 已确认别名 | 描述 |",
              "|---|---|---|---|"]
    for entity in bundle["entities"]:
        lines.append("| " + " | ".join(_cell(x) for x in (entity["entity_id"], entity["canonical_name"],
            "；".join(entity.get("aliases", [])), entity.get("description"))) + " |")
    lines += ["", "## 事项表", "", "| 事项 ID | 主体 | 事项 | 当前记录 | 条件／性质 | 来源 |",
              "|---|---|---|---|---|---|"]
    for item in bundle["items"]:
        label = "、".join(source_names[sid] for sid in item["source_version_ids"])
        lines.append("| " + " | ".join(_cell(x) for x in (item["ledger_item_id"],
            names.get(item["subject_entity_id"], item["subject_entity_id"]), item["predicate"],
            _value(item["display_value"], names) if item.get("display_value") is not None
            else item["display_statement"], item.get("qualifiers", {})))
            + " | [" + _cell(label) + "](#" + item["ledger_item_id"] + ") |")
    if not bundle["items"]:
        lines += ["", "尚未建立初始事项；任务卡可独立保留，之后仍可首次建账。"]
    lines += ["", "## 事项来源展开", "",
              "提取字段包含模型的解释与规范化；下列“直接原文”来自已保存的 evidence。上下文入口不代表单条引用已经充分支持全部字段。", ""]
    for item in bundle["items"]:
        iid = item["ledger_item_id"]
        lines += ['<a id="' + iid + '"></a>', "<details>",
                  "<summary>" + _cell(iid + " · " + item["predicate"]) + "</summary>", "",
                  "规范陈述：" + _cell(item["display_statement"]), ""]
        view = (evidence_views or {}).get(iid)
        for claim in view["claims"] if view else []:
            lines += ["**" + _cell(claim["assertion_id"] + " · " + source_names[claim["source_version_id"]]) + "**", "",
                      "来源陈述：" + _cell(claim["statement"]), "",
                      "提取原值字段（非逐字引语）：" + _cell(claim.get("raw_value")), "",
                      "规范值：" + _cell(_value(claim.get("value"), names)), "",
                      "限定条件：" + _cell(claim.get("qualifiers", {})), "",
                      "规范限定：" + _cell(claim.get("canonical_qualifiers", {})), ""]
            for ref in claim["direct_evidence"]:
                lines += ["直接原文 · " + _cell(ref["evidence_ref_id"]) + " · " + _cell(ref["locator"]), "",
                          "<pre>" + escape(ref["raw_content"]) + "</pre>", ""]
            lines += ["可展开上下文请求：" + _cell(claim["context_requests"]), ""]
        lines += ["</details>", ""]
    lines += ["## 来源信息", "", "| 来源 ID | 文件 | 格式 | 当前快照的处理信息 |",
              "|---|---|---|---|"]
    for source in bundle["source_catalog"]:
        lines.append("| " + " | ".join(_cell(x) for x in (source["source_version_id"],
            source["display_name"], source["media_type"], {"arrival_status": source["arrival_status"],
            "parse_record_ids": source["parse_record_ids"], "source_failures": source["source_failures"]})) + " |")
    lines.append("")
    for source in bundle["source_catalog"]:
        lines += ["<details>", "<summary>" + _cell(source["display_name"]) + " · 文件位置</summary>", "",
                  "原件：" + _cell(source["original_location"]), "",
                  "解析结果：" + _cell(source["parse_result_location"]), "", "</details>", ""]
    lines += ["## 快照", "", "<pre>" + escape(json.dumps(snapshot, ensure_ascii=False, indent=2)) + "</pre>", ""]
    if bundle["notes"]:
        lines += ["## 处理说明", "", "<pre>" + escape(json.dumps(bundle["notes"], ensure_ascii=False, indent=2)) + "</pre>", ""]
    return "\n".join(lines)

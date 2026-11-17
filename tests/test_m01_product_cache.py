"""Per-unit completion and source cache boundaries; prescribed facts, no model calls."""
import json
from pathlib import Path

from action_trace_demo import ingestion
from action_trace_demo.ingestion_store import IngestionStore
from action_trace_demo.ingestion_tools import IngestionTools


def test_empty_and_valid_source_reuse_survive_failed_sibling(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("站点 A 配额为 2 台。")
    (tmp_path / "empty.txt").write_text("运行记录标题。")
    (tmp_path / "bad.bin").write_bytes(b"unsupported")
    (tmp_path / "model.json").write_text(json.dumps({"model": "EFundGPT-air"}))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"model_config_file": "model.json"}))
    first = tmp_path / "first.json"
    first.write_text(json.dumps({"task_context": "记录站点配额", "sources": [
        {"path": "a.txt"}, {"path": "empty.txt"}, {"path": "bad.bin"}]}))
    again = tmp_path / "again.json"
    again.write_text(json.dumps({"task_context": "记录站点配额", "sources": [
        {"path": "a.txt"}, {"path": "empty.txt"}]}))
    calls = []
    def prescribed(group, workspace, model_config, *, prompt):
        calls.append(group["window_group_id"])
        folder = Path(workspace) / "groups" / group["window_group_id"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "ingest_group.json").write_text(json.dumps(group))
        adapter = IngestionTools(folder, {"request_id": group["window_group_id"], "ingestion_root": str(workspace)})
        if group["sources"][0]["display_name"] == "empty.txt":
            result = {"unit_id": "U1", "facts": [], "empty_reason": "只有标题"}
        else:
            result = {"unit_id": "U1", "entities": [{"name": "站点 A", "mention_text": "站点 A"}],
                      "facts": [{"subject": "站点 A", "statement": "站点 A 配额为2台", "qualifiers": {}, "predicate": "配额", "value_text": "2 台",
                      "value_kind": "number", "unit": "台", "value_source": "U1"}]}
        receipt = adapter.call("save_extraction", {"results": [result]})
        (folder / "invocation.json").write_text(json.dumps({"steps": 1, **receipt}))
        return receipt
    monkeypatch.setattr(ingestion, "extract_group_with_dsh", prescribed)
    monkeypatch.setattr("action_trace_demo.semantic_ingestion.resolve_arrival", lambda *args: [])
    work = tmp_path / "work"
    first_result = ingestion.process_manifests(work, [first], config)["arrivals"][0]
    assert first_result["status"] == "partial" and len(calls) == 2
    store = IngestionStore(work)
    completed = [s for s in first_result["source_statuses"].values() if s["status"] == "completed"]
    assert len(completed) == 2 and sorted(len(s["assertion_ids"]) for s in completed) == [0, 1]
    second = ingestion.process_manifests(work, [again], config)["arrivals"][0]
    assert second["status"] == "completed" and len(calls) == 2
    assert all(s["reused"] for s in second["source_statuses"].values())
    assert len(store.export()["source_claims"]) == 1
    ingestion.process_manifests(work, [], config, arrival_ids=[second["arrival_id"]])
    assert len(calls) == 2 and len(store.export()["source_claims"]) == 1


def test_one_sentence_does_not_complete_another_in_same_block(tmp_path):
    from action_trace_demo.source_pipeline import make_group
    from action_trace_demo.source_units import build_source_units
    from action_trace_demo.source_extraction import compile_unit
    store = IngestionStore(tmp_path / "work")
    arrival = store.register_arrival([{"content_hash": "fixture", "display_name": "x.txt",
        "media_type": "text/plain", "original_location": "fixture"}])
    sid = arrival["source_version_ids"][0]
    block = {"block_id": "B", "source_version_id": sid, "kind": "text", "order_index": 0,
        "content": "站点 A 有2台设备。站点 B 有3台设备。", "context_refs": [], "locator": {}}
    units = build_source_units([block])
    profile = {"profile_id": "fixture", "source_pipeline_version": "source-elements-v1"}
    group, _ = make_group(arrival, units, ingestion.DEFAULTS, profile, "G", 0, [])
    store.save_parse(sid, [block], group["windows"], parse_result_location="fixture", parse_profile={})
    store.record_group_plan(group, "fixture")
    compiled = compile_unit({"unit_id": "U1", "entities": [{"name": "站点 A", "mention_text": "站点 A"}],
        "facts": [{"subject": "站点 A", "statement": "站点 A 有2台设备", "qualifiers": {}, "predicate": "设备数量",
        "value_text": "2台", "value_kind": "number", "unit": "台", "value_source": "U1"}]}, group)
    store.stage_extraction(arrival["arrival_id"], "G_U1", compiled["result"], compiled["provided_evidence_refs"],
        0, extraction_profile=profile)
    store.merge_arrival(arrival["arrival_id"])
    result = store.finish_arrival(arrival["arrival_id"], extraction_profile=profile)
    assert result["status"] == "partial"
    assert result["source_statuses"][sid]["processed_unit_ids"] == [units[0]["unit_id"]]
    assert store.reusable_source_result(sid, "fixture") is None

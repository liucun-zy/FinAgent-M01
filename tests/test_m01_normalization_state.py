"""Local normalization versions and unresolved elements survive the storage seam."""
from action_trace_demo.ingestion import DEFAULTS
from action_trace_demo.ingestion_store import IngestionStore
from action_trace_demo.source_pipeline import make_group
from action_trace_demo.source_units import build_source_units


def test_parser_element_warning_prevents_false_source_completion(tmp_path):
    store = IngestionStore(tmp_path)
    arrival = store.register_arrival([{"content_hash": "native", "display_name": "native.pdf",
        "media_type": "application/pdf", "original_location": "fixture.pdf"}])
    sid = arrival["source_version_ids"][0]
    blocks = [{"block_id": "B1", "source_version_id": sid, "kind": "text",
        "order_index": 0, "content": "Source A states a condition.", "context_refs": [], "locator": {}},
        {"block_id": "B2", "source_version_id": sid, "kind": "metadata", "order_index": 1,
         "content": "", "context_refs": [], "locator": {"page_index": 1}, "extraction_eligible": False,
         "raw_entry_ref": {"parse_file": "content_list.json", "json_pointer": "/1"},
         "normalization": {"diagnostics": [
             {"code": "unknown_element", "severity": "warning", "requires_review": True,
              "message": "Unrecognized structured payload retained for review"}]}}]
    profile = {"profile_id": "fixture", "source_pipeline_version": "source-elements-v1"}
    group, _ = make_group(arrival, build_source_units(blocks), DEFAULTS, profile, "G", 0, [])
    store.save_parse(sid, blocks, group["windows"], parse_result_location="fixture", parse_profile={})
    store.record_group_plan(group, "fixture")
    store.stage_extraction(arrival["arrival_id"], "G", {"entities": [], "claims": [],
        "processed_window_ids": [w["window_id"] for w in group["windows"]], "unresolved": []},
        group["evidence_refs"], 0, extraction_profile=profile)
    result = store.finish_arrival(arrival["arrival_id"], extraction_profile=profile)
    assert result["status"] == "partial"
    assert result["source_statuses"][sid]["status"] == "partial"
    issue = next(x for x in result["unresolved_items"] if x.get("stage") == "normalization")
    assert issue["source_version_id"] == sid and issue["diagnostic"]["code"] == "unknown_element"
    assert store.reusable_source_result(sid, "fixture") is None


def test_local_normalization_versions_append_without_changing_remote_profile(tmp_path):
    store = IngestionStore(tmp_path)
    arrival = store.register_arrival([{"content_hash": "native", "display_name": "native.pdf",
        "media_type": "application/pdf", "original_location": "fixture.pdf"}])
    sid = arrival["source_version_ids"][0]
    remote = {"route": "mineru", "model_version": "vlm"}
    old = store.save_parse(sid, [], [], parse_result_location="same-cache", parse_profile=remote,
                           normalization_version="old-local")
    new = store.save_parse(sid, [], [], parse_result_location="same-cache", parse_profile=remote,
                           normalization_version="new-local")
    reused = store.save_parse(sid, [], [], parse_result_location="same-cache", parse_profile=remote,
                              normalization_version="new-local")
    assert old["record_id"] != new["record_id"] == reused["record_id"]
    assert old["parse_profile"] == new["parse_profile"] == remote
    assert store.get_parse(sid)["normalization_version"] == "new-local"

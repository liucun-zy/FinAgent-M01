"""Product source contracts through grounded compilation and the official DSH save path."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from action_trace_demo.dsh_bridge import Bridge, write_json
from action_trace_demo.ingestion_store import IngestionStore
from action_trace_demo.source_extraction import build_prompt, compile_unit, tool_specs
from action_trace_demo.source_units import build_source_units, render_packet


def v2_fixture(raw):
    """Explicit structural migration of prescribed v1 fixtures; not model output."""
    raw = deepcopy(raw)
    facts = raw.get("facts", [])
    if not isinstance(facts, list):
        return raw
    entities = raw.setdefault("entities", [])
    for entity in entities:
        entity.setdefault("mention_text", entity["name"])
    declared = {e["name"] for e in entities}
    for fact in facts:
        if fact["subject"] not in declared:
            entities.append({"name": fact["subject"], "mention_text": fact["subject"]})
            declared.add(fact["subject"])
        fact.setdefault("qualifiers", {})
        fact.setdefault("statement", f'{fact["subject"]}的{fact["predicate"]}为{fact["value_text"]}。')
    return raw


def compile_fixture(raw, group):
    return compile_unit(v2_fixture(raw), group)


def source_group(text, sid="source", *, arrival="arrival"):
    blocks = [{"block_id": f"B{i}", "source_version_id": sid, "kind": "text", "order_index": i,
               "content": line, "locator": {"space": "text", "line_start": i + 1},
               "context_refs": [], "parent_block_id": None}
              for i, line in enumerate(text.splitlines())]
    packet = render_packet(build_source_units(blocks))
    return {"window_group_id": "G", "arrival_id": arrival, "group_order": 0,
            "source_packet": packet, "windows": packet["windows"], "evidence_refs": packet["evidence_refs"],
            "blocks": blocks, "entity_candidates": [], "task_context": "记录站点运行情况",
            "config": {"max_model_requests_per_group": 2}}


def test_native_contract_compiles_independent_facts_and_preserves_time_roles():
    group = source_group("松林服务点有三台扫描仪，仅供夜班使用。\n站点 Q 预计于2027年4月6日结束试运行。")
    first = {"unit_id": "U1", "entities": [{"name": "松林服务点", "type": "facility"}], "facts": [
        {"subject": "松林服务点", "predicate": "扫描仪数量", "value_text": "三台", "value_kind": "number",
         "normalized_value": "3", "unit": "台", "value_source": "U1", "qualifiers": {"用途": "仅供夜班使用"}},
        {"subject": "松林服务点", "predicate": "区域", "value_text": "远郊", "value_kind": "text"}]}
    compiled = compile_fixture(first, group)
    assert compiled["result"]["claims"][0]["value"] == {"kind": "number", "normalized": "3", "unit": "台"}
    assert compiled["result"]["claims"][0]["raw_value"] == {"text": "三台"}
    assert compiled["result"]["claims"][0]["qualifiers"] == {"用途": "仅供夜班使用"}
    assert compiled["result"]["claims"][1] == {}
    assert list(compiled["prevalidation_errors"]) == [1]
    second = compile_fixture({"unit_id": "U2", "facts": [{"subject": "站点 Q", "predicate": "试运行结束日期",
        "value_text": "2027年4月6日", "value_kind": "date", "qualifiers": {"性质": "预计"}}]}, group)
    claim = second["result"]["claims"][0]
    assert claim["value"]["normalized"] == "2027-04-06"
    assert claim["effective_time"] is None
    assert claim["qualifiers"] == {"性质": "预计"}
    prompt = build_prompt(group)
    schema = json.dumps(tool_specs())
    assert [x["name"] for x in tool_specs()] == ["save_extraction"]
    assert all(key not in prompt + schema for key in ("existing_entity_id", "evidence_ref_id", "candidate_entity_ids"))


def test_same_name_entities_use_their_own_identifiers_and_explicit_applicability():
    group = source_group("同名站点（编号 X1）配额为3台；同名站点（编号 X2）配额为5台，自2027年5月1日起适用。")
    payload = {"unit_id": "U1", "entities": [
        {"name": "同名站点", "identifiers": {"编号": "X1"}},
        {"name": "同名站点", "identifiers": {"编号": "X2"}}], "facts": [
        {"subject": "同名站点", "subject_identifiers": {"编号": "X1"}, "predicate": "配额", "value_text": "3台",
         "value_kind": "number", "unit": "台", "value_source": "U1"},
        {"subject": "同名站点", "subject_identifiers": {"编号": "X2"}, "predicate": "配额", "value_text": "5台",
         "value_kind": "number", "unit": "台", "value_source": "U1",
         "applicability": {"text": "自2027年5月1日起适用", "normalized": "2027-05-01", "source": "U1"}}]}
    compiled = compile_fixture(payload, group)
    assert compiled["prevalidation_errors"] == {}
    claims = compiled["result"]["claims"]
    assert len({c["subject_local_id"] for c in claims}) == 2
    assert claims[1]["effective_time"] == "2027-05-01"
    del payload["facts"][0]["subject_identifiers"]
    assert "multiple objects" in compile_fixture(payload, group)["prevalidation_errors"][0]


def test_explicit_empty_is_completed_source_coverage_not_an_unresolved_fact():
    group = source_group("运行记录说明。")
    with pytest.raises(ValueError, match="empty_reason"):
        compile_fixture({"unit_id": "U1", "facts": []}, group)
    compiled = compile_fixture({"unit_id": "U1", "facts": [], "empty_reason": "只有标题，无独立运行事实"}, group)
    assert compiled["result"]["unresolved"] == []
    assert compiled["result"]["processed_window_ids"] == [group["windows"][0]["window_id"]]


def test_value_belongs_to_primary_unit_while_subject_can_use_context():
    group = source_group("北侧仓库。\n容量为3台。")
    fact = {"subject": "北侧仓库", "predicate": "容量", "value_text": "3台", "value_kind": "number",
            "unit": "台", "value_source": "U2"}
    good = compile_fixture({"unit_id": "U2", "facts": [fact]}, group)
    assert good["prevalidation_errors"] == {}
    assert good["result"]["entities"][0]["surface_name"] == "北侧仓库"
    wrong_unit = compile_fixture({"unit_id": "U1", "facts": [fact]}, group)
    assert list(wrong_unit["prevalidation_errors"]) == [0]
    assert "Unknown source address" in wrong_unit["prevalidation_errors"][0]
    del fact["value_source"]
    assert list(compile_fixture({"unit_id": "U1", "facts": [fact]}, group)["prevalidation_errors"]) == [0]


def test_source_packets_save_valid_siblings_and_repair_through_official_dsh(tmp_path):
    project = tmp_path / "project"
    source = tmp_path / "stations.txt"
    source.write_text("松林服务点有三台扫描仪，仅供夜班使用。\n站点 Q 预计于2027年4月6日结束试运行。")
    store = IngestionStore(project)
    arrival = store.register_arrival([{"content_hash": hashlib.sha256(source.read_bytes()).hexdigest(),
        "original_location": str(source), "display_name": source.name, "media_type": "text/plain"}])
    sid = arrival["source_version_ids"][0]
    group = source_group(source.read_text(), sid, arrival=arrival["arrival_id"])
    store.save_parse(sid, group["blocks"], group["windows"], parse_result_location=source,
                     parse_profile={"reader": "test-text"}, evidence_refs=group["evidence_refs"])
    groupdir = project / "groups" / "G"
    groupdir.mkdir(parents=True)
    write_json(groupdir / "task_context.json", {"mode": "ingest", "request_id": "G",
        "ingestion_root": str(project), "group_file": "ingest_group.json"})
    write_json(groupdir / "ingest_group.json", group)
    (groupdir / "prompt.txt").write_text(build_prompt(group))
    first = {"results": [{"unit_id": "U1", "facts": [
        {"subject": "松林服务点", "predicate": "扫描仪数量", "value_text": "三台", "value_kind": "number",
         "normalized_value": "3", "unit": "台", "value_source": "U1", "qualifiers": {"用途": "仅供夜班使用"}},
        {"subject": "松林服务点", "predicate": "使用时段", "value_text": "白天", "value_kind": "text"}]},
        {"unit_id": "U2", "facts": "malformed independent unit"}]}
    repair = {"results": [{"unit_id": "U1", "retry_indices": [1], "facts": [
        {"subject": "松林服务点", "predicate": "使用时段", "value_text": "夜班", "value_kind": "text", "value_source": "U1"}]},
        {"unit_id": "U2", "facts": [{"subject": "站点 Q", "predicate": "试运行结束日期", "value_text": "2027年4月6日",
             "value_kind": "date", "value_source": "U2", "qualifiers": {"性质": "预计"}}]}]}
    first["results"] = [v2_fixture(raw) for raw in first["results"]]
    repair["results"] = [v2_fixture(raw) for raw in repair["results"]]
    repair["results"][0]["entities"] = []  # The earlier declaration survives the failed fact repair.
    write_json(groupdir / "actions.json", [{"name": "save_extraction", "arguments": first},
        {"name": "save_extraction", "arguments": repair}, {"name": "finish_stage", "arguments": {}}])
    assert [tool["name"] for tool in Bridge(groupdir).catalog()] == ["save_extraction"]
    repo = Path(__file__).resolve().parents[1]
    process = subprocess.run([str(repo / "dsh_host/run"), "--workspace", str(groupdir),
        "--prompt-file", str(groupdir / "prompt.txt"), "--script", str(groupdir / "actions.json"),
        "--max-calls", "2", "--audit-dir", str(project / ".dsh" / "replay")], cwd=repo,
        text=True, capture_output=True, timeout=60)
    assert process.returncode == 0, process.stderr + process.stdout
    result = json.loads(process.stdout)
    assert result["committed"] and result["steps"] == 2 and result["usage"] is None
    state = json.loads((groupdir / "ingest_tool_state.json").read_text())
    assert state["receipt"]["session_complete"] and state["receipt"]["pending_unit_ids"] == []
    store.merge_arrival(arrival["arrival_id"])
    extracted = store.export()
    assert len(extracted["source_claims"]) == 3
    attempts = extracted["extractions"]
    assert len(attempts) == 3
    assert attempts[0]["proposal_metadata"]["proposal"] == first["results"][0]
    assert attempts[0]["save_receipt"]["accepted_claim_indices"] == [0]
    assert attempts[0]["result"]["processed_window_ids"] == []
    assert attempts[1]["save_receipt"]["newly_accepted_claim_indices"] == [1]
    assert attempts[1]["save_receipt"]["accepted_claim_indices"] == [0, 1]
    assert attempts[1]["proposal_metadata"]["proposal"]["entities"] == []
    assert attempts[1]["proposal_metadata"]["effective_entity_declarations"][0]["mention_text"] == "松林服务点"
    assert all(claim["effective_time"] is None for claim in extracted["source_claims"])
    assert not {"stage_submission", "artifact_check", "work_item"} & {n["kind"] for n in store.dag.nodes()}


def test_v2_separates_business_labels_from_grounded_mentions_and_requires_declared_subject():
    group = source_group("松林服务点有三台扫描仪，仅供夜班使用。")
    raw = {"unit_id": "U1", "entities": [{"name": "松林夜班设备安排", "mention_text": "松林服务点", "source": "U1"}],
        "facts": [{"subject": "松林夜班设备安排", "predicate": "扫描仪数量", "value_text": "三台",
            "value_kind": "number", "normalized_value": "3", "unit": "台", "value_source": "U1",
            "statement": "松林服务点有三台仅供夜班使用的扫描仪。", "qualifiers": {"设备": "扫描仪", "用途": "仅供夜班使用"}}]}
    compiled = compile_unit(raw, group)
    assert compiled["prevalidation_errors"] == {}
    assert compiled["result"]["entities"][0]["surface_name"] == "松林服务点"
    assert compiled["result"]["entities"][0]["description"] == "松林夜班设备安排"
    assert compiled["result"]["claims"][0]["statement"] == raw["facts"][0]["statement"]
    assert compiled["result"]["claims"][0]["qualifiers"] == raw["facts"][0]["qualifiers"]
    raw["facts"].append({**raw["facts"][0], "subject": "松林服务点扫描仪数量"})
    failed = compile_unit(raw, group)
    assert list(failed["prevalidation_errors"]) == [1]
    assert "available labels: ['松林夜班设备安排']" in failed["prevalidation_errors"][1]
    raw["entities"][0]["mention_text"] = "松林服务点夜班设备安排"
    ungrounded = compile_unit(raw, group)
    assert "not a literal" in ungrounded["warnings"][0]["error"]
    assert ungrounded["result"]["entities"] == []


def test_v2_requires_individual_statement_and_qualifier_fields():
    group = source_group("站点 A 有2台设备。")
    raw = {"unit_id": "U1", "entities": [{"name": "站点 A", "mention_text": "站点 A"}],
        "facts": [{"subject": "站点 A", "predicate": "数量", "value_text": "2台", "value_kind": "number",
            "unit": "台", "qualifiers": {}},
            {"subject": "站点 A", "predicate": "数量", "value_text": "2台", "value_kind": "number",
             "unit": "台", "statement": "站点 A 有2台设备。"}]}
    rejected = compile_unit(raw, group)["prevalidation_errors"]
    assert "statement" in rejected[0] and "qualifiers" in rejected[1]


def test_v2_record_applicability_uses_field_literal_instead_of_key_date_concatenation(tmp_path):
    from action_trace_demo.evidence_windows import normalize_parse_result
    path = tmp_path / "record.json"
    path.write_text(json.dumps([{"站点": "东门站点", "配额": "3台", "适用起始日": "2027-05-01"}], ensure_ascii=False))
    blocks = normalize_parse_result({"source_version_id": "S", "original_location": str(path),
                                    "profile": {"route": "direct", "extension": ".json"}})
    packet = render_packet(build_source_units(blocks))
    group = {"source_packet": packet}
    anchors = {json.loads(ref["raw_content"]): short for short, ref in packet["anchors"].items() if ref.get("role") == "field"}
    raw = {"unit_id": "U1", "entities": [{"name": "东门站点配额", "mention_text": "东门站点"}], "facts": [{
        "subject": "东门站点配额", "predicate": "配额", "value_text": "3台", "value_kind": "number", "unit": "台",
        "value_source": anchors["3台"], "statement": "东门站点自2027年5月1日起配额为3台。", "qualifiers": {},
        "applicability": {"text": "适用起始日2027-05-01", "normalized": "2027-05-01", "source": anchors["2027-05-01"]}}]}
    assert list(compile_unit(raw, group)["prevalidation_errors"]) == [0]
    raw["facts"][0]["applicability"]["text"] = "2027-05-01"
    compiled = compile_unit(raw, group)
    assert compiled["prevalidation_errors"] == {}
    assert compiled["result"]["claims"][0]["effective_time"] == "2027-05-01"

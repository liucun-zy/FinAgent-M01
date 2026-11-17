"""Source element packets preserve content and grounding without prompt metadata."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from action_trace_demo.evidence_windows import _Blocks, evidence_refs, normalize_parse_result
from action_trace_demo.source_alignment import align_phrase, build_source_observations
from action_trace_demo.source_units import build_source_units, render_packet, split_unit
from action_trace_demo.unit_consistency import validate_numeric_unit


SAMPLES = Path(__file__).parents[1] / "examples/m01/parse_samples"


def sample(extension):
    directory = SAMPLES / extension
    meta = json.loads((directory / "sample.json").read_text())
    return normalize_parse_result({"source_version_id": "SAMPLE_" + extension,
                                   "result_dir": str(directory), "profile": meta["profile"]})


def prose(text):
    blocks = _Blocks("S")
    blocks.add("text", text, {"space": "text", "paragraph": 0})
    return blocks.items


def test_real_html_packet_has_one_cell_representation_and_all_business_content():
    blocks = sample("html")
    before = deepcopy(blocks)
    units = build_source_units(blocks)
    packet = render_packet(units)
    encoded = json.dumps(packet["model_input"], ensure_ascii=False)
    assert blocks == before
    assert all(marker not in encoded for marker in ("<tr", "locator", "evidence_ref_id", "source_version_id"))
    table = next(u for u in packet["model_input"]["units"] if u["kind"] == "table_row")
    assert [c["text"] for c in table["cells"]] == ["PO-017", "A型", "120", "0.8", "96"]
    assert "2026 年 12 月 15 日" in encoded
    assert len(encoded) < len(json.dumps(evidence_refs(blocks), ensure_ascii=False)) / 2
    for short, unit in packet["units"].items():
        assert unit["window"]["window_id"] == unit["unit_id"]
        assert set(unit["evidence_ref_ids"]) == {r["evidence_ref_id"] for r in unit["evidence_refs"]}
        assert all(a in packet["anchors"] for a in unit["primary_anchor_ids"] + unit["context_anchor_ids"])
        assert packet["anchors"][short]["source_version_id"] == "SAMPLE_html"


def test_repeated_lab_counts_are_unique_in_their_own_exact_source_spans():
    text = "东楼实验室的离心机为两台，限科研使用。西楼实验室的恒温箱为两台，限教学使用。"
    blocks = prose(text)
    units = build_source_units(blocks)
    assert len(units) == 2
    assert align_phrase("两台", evidence_refs(blocks)[0])["status"] == "ambiguous"
    packet = render_packet(units)
    for short, unit in packet["units"].items():
        ref = packet["anchors"][short]
        observation = align_phrase("两台", ref)
        assert observation["status"] == "located"
        loc = ref["locator"]
        assert text[loc["block_char_start"]:loc["block_char_end"]] == ref["raw_content"]
        assert ref["parent_evidence_ref_id"] == "EV_" + blocks[0]["block_id"]
        assert unit["window"]["main_block_ids"] == [blocks[0]["block_id"]]
    assert "西楼" not in packet["anchors"]["U1"]["raw_content"]
    assert "东楼" not in packet["anchors"]["U2"]["raw_content"]
    assert [u["unit_id"] for u in units] == [u["unit_id"] for u in build_source_units(blocks)]


def test_real_xlsx_retains_price_header_tax_note_and_true_column_association():
    packet = render_packet(build_source_units(sample("xlsx")))
    row = next(u for u in packet["model_input"]["units"] if u["kind"] == "table_row" and
               any(c["text"] == "0.8" for c in u.get("cells", [])))
    cell = next(c for c in row["cells"] if c["text"] == "0.8")
    assert [packet["anchors"][a]["raw_content"] for a in cell["headers"]] == ["单价（万元／台）"]
    assert any("不含税" in packet["anchors"][a]["raw_content"] for a in row["context"])
    assert packet["anchors"][cell["id"]]["locator"]["column_index"] == 3


def test_merged_headers_follow_actual_column_spans_without_guessing_first_row():
    blocks = _Blocks("S")
    blocks.table("<table><tr><th>项目</th><th colspan='2'>金额（万元）</th></tr>"
                 "<tr><td>甲</td><td>3</td><td>4</td></tr></table>", {"space": "html"})
    packet = render_packet(build_source_units(blocks.items))
    row = packet["model_input"]["units"][0]
    assert len(row["cells"][1]["headers"]) == len(row["cells"][2]["headers"]) == 1
    for cell in row["cells"][1:]:
        ref = packet["anchors"][cell["id"]]
        claim = {"value": {"kind": "number", "normalized": str(int(cell["text"]) * 10000), "unit": "CNY"},
                 "raw_value": {"text": cell["text"]}}
        result = validate_numeric_unit(claim, [ref], packet["evidence_refs"])
        assert result["input_basis"] == "source"
        assert any(r["role"] == "scale" for r in result["used_evidence_refs"])
    untyped = _Blocks("T")
    untyped.table("<table><tr><td>名称</td><td>金额</td></tr><tr><td>甲</td><td>3</td></tr></table>", {})
    rows = render_packet(build_source_units(untyped.items))["model_input"]["units"]
    assert len(rows) == 2 and not any(c["headers"] for r in rows for c in r["cells"])


def test_json_record_keeps_all_fields_and_original_paths_even_when_large(tmp_path):
    path = tmp_path / "records.json"
    path.write_text(json.dumps([{"订单": "甲", "规格": {"数量": 3}, "备注": "说明" * 40}], ensure_ascii=False))
    blocks = normalize_parse_result({"source_version_id": "JSON", "original_location": str(path),
                                    "profile": {"route": "direct", "extension": ".json"}})
    units = build_source_units(blocks, max_unit_chars=15)
    packet = render_packet(units)
    assert len(units) == 1 and split_unit(units[0]) == []
    fields = packet["model_input"]["units"][0]["fields"]
    assert {f["path"] for f in fields} == {"/0/订单", "/0/规格/数量", "/0/备注"}
    assert next(f["text"] for f in fields if f["path"].endswith("数量")) == "3"
    assert all(f["text"] == packet["anchors"][f["id"]]["raw_content"] for f in fields)


def test_long_prose_has_bounded_primary_spans_and_lossless_recovery_splits():
    text = "甲乙丙丁戊己庚辛壬癸" * 37
    units = build_source_units(prose(text), max_unit_chars=61, context_chars=0)
    primary = [next(r for r in u["evidence_refs"] if r["evidence_ref_id"] == u["primary_ref_ids"][0]) for u in units]
    assert all(len(r["raw_content"]) <= 61 for r in primary)
    assert "".join(r["raw_content"] for r in primary) == text
    children = split_unit(units[0])
    assert len(children) == 2
    assert "".join(next(r["raw_content"] for r in c["evidence_refs"] if r["evidence_ref_id"] == c["primary_ref_ids"][0])
                   for c in children) == primary[0]["raw_content"]
    assert children[0]["primary_spans"][0]["end"] == children[1]["primary_spans"][0]["start"]
    assert all(c["parent_unit_id"] == units[0]["unit_id"] for c in children)
    other = deepcopy(units[0]); other["source_version_id"] = "OTHER"
    with pytest.raises(ValueError, match="mix source"):
        render_packet([units[0], other])


def test_adjacent_records_are_not_duplicated_as_unscoped_context(tmp_path):
    path = tmp_path / "records.json"
    path.write_text(json.dumps([{"站点": "东", "值": 3}, {"站点": "西", "值": 4}], ensure_ascii=False))
    blocks = normalize_parse_result({"source_version_id": "JSON", "original_location": str(path),
                                    "profile": {"route": "direct", "extension": ".json"}})
    packet = render_packet(build_source_units(blocks))
    assert len(packet["model_input"]["units"]) == 2
    assert packet["model_input"]["context"] == []
    assert all(not u["context_ref_ids"] for u in packet["units"].values())


def test_text_value_phrase_is_located_instead_of_copying_its_entire_context():
    packet = render_packet(build_source_units(prose("甲设备仅限科研使用，禁止转售。")))
    ref = packet["anchors"]["U1"]
    claim = {"value": {"kind": "text", "normalized": "科研"}, "raw_value": {"text": "仅限科研使用"},
             "field_evidence": {"value": [ref["evidence_ref_id"]]}}
    result = build_source_observations(claim, packet["evidence_refs"])
    assert len(result) == 1 and result[0]["text"] == "仅限科研使用"
    assert result[0]["start"] == ref["raw_content"].index("仅限科研使用")


def test_generic_price_denominator_requires_cited_same_source_count_basis():
    packet = render_packet(build_source_units(prose("采购数量120台。执行单价8000元。")))
    quantity, price = packet["anchors"]["U1"], packet["anchors"]["U2"]
    claim = {"predicate": "执行单价", "value": {"kind": "number", "normalized": "8000", "unit": "CNY/台"},
             "raw_value": {"text": "8000"}, "field_evidence": {"unit": [quantity["evidence_ref_id"]]}}
    result = validate_numeric_unit(claim, [price, quantity])
    assert result["normalization"][0]["contextual_denominator"]
    assert {"ref_id": quantity["evidence_ref_id"], "role": "denominator"} in result["used_evidence_refs"]
    with pytest.raises(ValueError, match="Unit consistency"):
        validate_numeric_unit(claim, [price], [price, quantity])
    unrelated = {**quantity, "source_version_id": "OTHER"}
    with pytest.raises(ValueError, match="Unit consistency"):
        validate_numeric_unit(claim, [price, unrelated])
    explicit = {**price, "raw_content": "执行单价8000元/人"}
    with pytest.raises(ValueError, match="Unit consistency"):
        validate_numeric_unit(claim, [explicit, quantity])

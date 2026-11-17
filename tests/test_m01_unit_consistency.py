"""Unit failures remain rejected while the append-only receipt preserves the proposal."""
from copy import deepcopy

import pytest

from action_trace_demo.ingestion_store import IngestionStore
from action_trace_demo.extraction import build_extraction_prompt


def fixture(tmp_path, *, predicate="unit_price", value=None, raw_value=None, text="单价0.8万元/台"):
    store = IngestionStore(tmp_path)
    arrival = store.register_arrival([{"display_name": "order.txt", "content_hash": "hash",
        "media_type": "text/plain", "original_location": "/fixture/order.txt"}])
    sid = arrival["source_version_ids"][0]
    ref = {"evidence_ref_id": "EV1", "source_version_id": sid, "block_id": "B1",
           "locator": {"space": "text", "line": 1}, "raw_content": text}
    claim = {"subject_local_id": "order", "predicate": predicate, "statement": text,
             "value": value or {"kind": "number", "normalized": "8000", "unit": "CNY/台"},
             "raw_value": raw_value, "evidence_ref_ids": ["EV1"]}
    result = {"entities": [{"local_entity_id": "order", "surface_name": "PO-017",
                           "evidence_ref_ids": ["EV1"]}],
              "claims": [claim], "processed_window_ids": [], "unresolved": []}
    return store, arrival, result, [ref]


def save(store, arrival, result, refs):
    return store.stage_extraction(arrival["arrival_id"], "G1", result, refs, 0)


def test_missing_denominator_rejected_before_persistence_then_corrected(tmp_path):
    store, arrival, result, refs = fixture(tmp_path,
        value={"kind": "number", "normalized": "8000", "unit": "CNY"},
        raw_value={"text": "0.8", "unit": "万元/台"})
    before = store.export()
    rejected = save(store, arrival, result, refs)
    assert not rejected["accepted_claim_indices"]
    assert "Unit consistency" in rejected["rejected_claims"][0]["error"]
    assert store.export()["source_claims"] == before["source_claims"]
    assert store.export()["extractions"][-1]["proposal"] == result
    corrected = deepcopy(result)
    corrected["claims"][0]["value"]["unit"] = "CNY/台"
    assert save(store, arrival, corrected, refs)["committed"]
    store.merge_arrival(arrival["arrival_id"])
    row = store.export()["source_claims"][0]
    assert row["value"] == {"kind": "number", "normalized": "8000", "unit": "CNY/台"}
    assert row["raw_value"] == {"text": "0.8", "unit": "万元/台"}


def test_cited_cell_requires_its_delivered_column_unit_without_cross_column_leak(tmp_path):
    store, arrival, result, refs = fixture(tmp_path, predicate="quantity",
        value={"kind": "number", "normalized": "120"}, raw_value={"text": "120"}, text="120")
    refs[0].update(role="cell", locator={"space": "parsed_table", "table_id": "T1",
                                         "row_index": 1, "column_index": 2})
    refs.append({**refs[0], "evidence_ref_id": "EV_HEADER", "block_id": "BH", "role": "header",
        "locator": {"space": "parsed_table", "table_id": "T1", "row_index": 0, "column_index": 2},
        "raw_content": "数量（台）"})
    refs.append({**refs[-1], "evidence_ref_id": "EV_PRICE_HEADER", "locator": {
        "space": "parsed_table", "table_id": "T1", "row_index": 0, "column_index": 3},
        "raw_content": "单价（万元／台）"})
    rejected = save(store, arrival, result, refs)
    assert not rejected["accepted_claim_indices"]
    assert "Unit consistency" in rejected["rejected_claims"][0]["error"]
    result["claims"][0]["value"]["unit"] = "item"
    assert save(store, arrival, result, refs)["committed"]


def test_amount_cannot_gain_a_denominator_while_currency_scale_changes(tmp_path):
    store, arrival, result, refs = fixture(tmp_path, predicate="amount",
        value={"kind": "number", "normalized": "960000", "unit": "CNY/台"},
        raw_value={"text": "96", "unit": "万元"}, text="金额96万元")
    rejected = save(store, arrival, result, refs)
    assert not rejected["accepted_claim_indices"]
    assert "Unit consistency" in rejected["rejected_claims"][0]["error"]
    result["claims"][0]["value"]["unit"] = "CNY"
    assert save(store, arrival, result, refs)["committed"]


def test_prose_numeric_anchor_does_not_mix_quantity_and_price(tmp_path):
    store, arrival, result, refs = fixture(tmp_path, predicate="quantity",
        value={"kind": "number", "normalized": "120"}, raw_value={"text": "120"},
        text="采购120台，单价8000元/台。")
    rejected = save(store, arrival, result, refs)
    assert not rejected["accepted_claim_indices"]
    assert "Unit consistency" in rejected["rejected_claims"][0]["error"]
    result["claims"][0]["value"]["unit"] = "item"
    assert save(store, arrival, result, refs)["committed"]


def test_single_raw_text_keeps_explicit_denominator_for_open_predicates(tmp_path):
    store, arrival, result, refs = fixture(tmp_path, predicate="purchase_price",
        value={"kind": "number", "normalized": "8000", "unit": "CNY"},
        raw_value={"text": "0.8万元/台"}, text="采购价格见记录")
    rejected = save(store, arrival, result, refs)
    assert not rejected["accepted_claim_indices"]
    assert "Unit consistency" in rejected["rejected_claims"][0]["error"]
    result["claims"][0]["value"]["unit"] = "CNY/台"
    assert save(store, arrival, result, refs)["committed"]


@pytest.mark.parametrize("predicate,raw_number,raw_unit,number,unit", [
    ("quantity", "1.2", "万台", "12000", "台"),
    ("shares", "1.2", "万股", "12000", "股"),
    ("population", "1.2", "万人", "12000", "人"),
    ("unit_price", "8000", "元每台", "8000", "CNY/台"),
])
def test_explicit_count_scales_and_per_spelling_are_equivalent(tmp_path, predicate, raw_number, raw_unit, number, unit):
    store, arrival, result, refs = fixture(tmp_path, predicate=predicate,
        value={"kind": "number", "normalized": number, "unit": unit},
        raw_value={"text": raw_number, "unit": raw_unit}, text="明确来源值")
    assert save(store, arrival, result, refs)["committed"]


def test_production_prompt_includes_representation_contract_once():
    prompt = build_extraction_prompt({"window_group_id": "G", "windows": [], "evidence_refs": []})
    assert prompt.count("数值量级与单位量纲是两件事") == 1
    assert "不为合并而省略限定" in prompt


def test_equal_numbers_in_different_columns_do_not_supply_each_others_units(tmp_path):
    store, arrival, result, refs = fixture(tmp_path, predicate="quantity",
        value={"kind": "number", "normalized": "120", "unit": "台"},
        raw_value={"text": "120", "unit": "台"}, text="120 | 120")
    refs[0]["locator"] = {"space": "parsed_table", "table_id": "T", "row_index": 1}
    for column, header in ((0, "数量（台）"), (1, "单价（元/台）")):
        refs.append({**refs[0], "evidence_ref_id": f"C{column}", "role": "cell", "raw_content": "120",
            "locator": {"space": "parsed_table", "table_id": "T", "row_index": 1, "column_index": column}})
        refs.append({**refs[-1], "evidence_ref_id": f"H{column}", "block_id": "BH", "role": "header",
            "raw_content": header, "locator": {"space": "parsed_table", "table_id": "T", "row_index": 0,
                "column_index": column}})
    assert save(store, arrival, result, refs)["committed"]


@pytest.mark.parametrize("predicate,number,unit,raw_number,raw_unit,text", [
    ("unit_price", "8000", "CNY/", "8000", "人民币元", "采购120台，单价8000元"),
    ("unit_price", "8000", "CNY/1", "8000", "人民币元", "采购120台，单价8000元"),
    ("amount", "0.8", "CNY", "0.8", "万元", "金额0.8万元"),
    ("unit_price", "8000", "USD/台", "0.8", "万元/台", "单价0.8万元/台"),
    ("unit_price", "800", "CNY/台", "0.8", "万元/台", "单价0.8万元/台"),
    ("unit_price", "8000", "CNY/人", "0.8", "万元/台", "单价0.8万元/台"),
    ("quantity", "120", "人", "120", "台", "采购120台"),
    ("quantity", "120", None, "120", "台", "采购120台"),
    ("amount", "100", "CNY", "100", "CNY", "来源金额100USD"),
])
def test_inconsistent_payloads_are_rejected_without_source_claim_mutation(tmp_path, predicate, number, unit, raw_number, raw_unit, text):
    value = {"kind": "number", "normalized": number}
    if unit is not None:
        value["unit"] = unit
    store, arrival, result, refs = fixture(tmp_path, predicate=predicate, value=value,
        raw_value={"text": raw_number, "unit": raw_unit}, text=text)
    before = store.export()
    rejected = save(store, arrival, result, refs)
    assert not rejected["accepted_claim_indices"]
    assert "Unit consistency" in rejected["rejected_claims"][0]["error"]
    assert store.export()["source_claims"] == before["source_claims"]
    assert store.export()["extractions"][-1]["proposal"] == result


@pytest.mark.parametrize("number,raw_value,text", [
    ("0.05", {"text": "5", "unit": "%"}, "比例5%"),
    ("0.5", {"text": "0.5"}, "比值为0.5"),
    ("3", None, "得分为3"),
])
def test_dimensionless_and_unknown_units_are_not_invented(tmp_path, number, raw_value, text):
    store, arrival, result, refs = fixture(tmp_path, predicate="ratio",
        value={"kind": "number", "normalized": number}, raw_value=raw_value, text=text)
    assert save(store, arrival, result, refs)["committed"]


def test_unspecified_source_denominator_is_not_invented_from_a_predicate(tmp_path):
    store, arrival, result, refs = fixture(tmp_path, predicate="unit_price",
        value={"kind": "number", "normalized": "8000", "unit": "CNY"},
        raw_value={"text": "8000", "unit": "人民币元"}, text="单价8000元")
    assert save(store, arrival, result, refs)["committed"]


def test_opaque_raw_numeral_does_not_borrow_equal_calendar_number_unit(tmp_path):
    store, arrival, result, refs = fixture(tmp_path, predicate="quantity",
        value={"kind": "number", "normalized": "2026", "unit": "台"},
        raw_value={"text": "两千零二十六台"}, text="2026年采购两千零二十六台设备")
    assert save(store, arrival, result, refs)["committed"]

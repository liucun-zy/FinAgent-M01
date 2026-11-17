"""The table contract survives isolated requests; no business-specific labels."""
from pathlib import Path
import json

from action_trace_demo.evidence_windows import _Blocks, normalize_parse_result
from action_trace_demo.source_units import build_source_units, render_packet


def isolated(html, match):
    out = _Blocks('T'); out.table(html, {'space':'html'})
    unit = next(u for u in build_source_units(out.items) if any(r['raw_content'] == match for r in u['fields']))
    return out.items, render_packet([unit])


def labels(packet, text, key='headers'):
    cell = next(c for u in packet['model_input']['units'] for c in u.get('cells',[]) if c['text'] == text)
    return [packet['anchors'][a]['raw_content'] for a in cell[key]]


def test_untyped_leading_row_is_visible_candidate_without_promoting_a_fact_to_header():
    blocks, p = isolated('<table><tr><td>字段</td><td>金额/年</td></tr><tr><td>甲</td><td>3</td></tr></table>', '3')
    assert labels(p,'3','header_candidates') == ['金额/年']
    assert labels(p,'3') == []
    assert blocks[0]['structural_role'] == 'table_row'
    assert len(build_source_units(blocks)) == 2
    assert any(c.get('role') == 'header_candidate' for c in p['model_input']['context'])


def test_thead_td_and_repeated_multilevel_headers_keep_column_hierarchy():
    _, p = isolated('<table><thead><tr><td rowspan="2">项目</td><td colspan="2">金额（元）</td></tr>'
        '<tr><td>预算</td><td>实际</td></tr></thead><tbody><tr><td>甲</td><td>3</td><td>4</td></tr></tbody></table>', '4')
    assert labels(p,'4') == ['金额（元）','实际']
    _, p = isolated('<table><tr><th>项目</th><th>本期</th></tr><tr><td>甲</td><td>3</td></tr>'
        '<tr><th>项目</th><th>上期</th></tr><tr><td>乙</td><td>4</td></tr></table>', '4')
    assert labels(p,'4') == ['上期']


def test_explicit_headers_and_row_scope_never_leak_to_another_row():
    _, p = isolated('<table><tr><th id="amount" scope="col">金额（元）</th></tr>'
        '<tr><th scope="row">甲</th><td headers="amount">3</td></tr>'
        '<tr><th scope="row">乙</th><td>4</td></tr></table>', '3')
    assert labels(p,'3') == ['金额（元）']
    _, p = isolated('<table><tr><th>项目</th><th>金额</th></tr><tr><th scope="row">甲</th><td>3</td></tr>'
        '<tr><th scope="row">乙</th><td>4</td></tr></table>', '4')
    assert labels(p,'4') == ['金额','乙']
    assert '甲' not in json.dumps(p['model_input'],ensure_ascii=False)
    _, p = isolated('<table><tr><th scope="row">甲</th><td>3</td></tr>'
        '<tr><th scope="row">乙</th><td>4</td></tr></table>', '4')
    assert '甲' not in json.dumps(p['model_input'],ensure_ascii=False)
    assert 'header_candidates' not in p['model_input']['units'][0]['cells'][0]


def test_spans_and_nested_tables_keep_the_outer_grid():
    blocks,p = isolated('<table><tr><th>名称</th><th colspan="2">数量</th></tr>'
        '<tr><td rowspan="2">甲</td><td>3</td><td><table><tr><td>备注</td></tr></table></td></tr>'
        '<tr><td>4</td><td>5</td></tr></table>', '4')
    assert len(blocks) == 3
    assert labels(p,'4') == ['数量']
    ref = next(r for r in p['anchors'].values() if r['raw_content']=='4')
    assert ref['locator']['column_index'] == 1


def test_markdown_tables_and_raw_html_use_the_same_table_contract(tmp_path):
    path=tmp_path/'tables.md'
    path.write_text('# 数据\n\n名称 | 年费率\n--- | ---:\nA\\|B | **0.5%**\n\n'
        '<table><thead><tr><td>项目</td><td>单位</td></tr></thead><tr><td>乙</td><td>台</td></tr></table>\n')
    blocks=normalize_parse_result({'source_version_id':'M','original_location':str(path),'profile':{'route':'direct','extension':'.md'}})
    p=render_packet(build_source_units(blocks))
    assert labels(p,'0.5%') == ['年费率']
    assert labels(p,'台') == ['单位']
    assert any(c['text']=='A|B' for u in p['model_input']['units'] for c in u.get('cells',[]))
    row=next(b for b in blocks if '0.5%' in b['content'])
    assert row['locator']['source_format']=='markdown'
    assert row['raw_content']=='A\\|B | **0.5%**'



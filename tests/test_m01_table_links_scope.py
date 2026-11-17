"""Cached failures exercise the normalizer -> isolated source-packet boundary."""
import json
from pathlib import Path

import pytest

from action_trace_demo.evidence_windows import _Blocks, normalize_parse_result
from action_trace_demo.source_units import build_source_units, render_packet
from action_trace_demo.table_context import organize_table_context












def table(out, html, page, index):
    out.table(html, {'space': 'page', 'page_index': page, 'content_index': index,
                     'bbox': [80, 80, 920, 900], 'bbox_space': 'normalized'})


def packet_for(blocks, text):
    unit = next(u for u in build_source_units(blocks) if any(r['raw_content'] == text for r in u['fields']))
    return render_packet([unit])


def test_links_keep_geometry_and_reject_body_or_changed_headers():
    for interrupt in ('none', 'heading', 'different_columns'):
        out = _Blocks('S')
        table(out, '<table><tr><th>Object</th><th>Rate</th></tr><tr><td>A</td><td>3</td></tr></table>', 0, 0)
        if interrupt == 'heading':
            out.add('heading', 'New section', {'page_index': 1}, level=1)
        html = ('<table><tr><th>Object</th><th>Population</th></tr><tr><td>B</td><td>4</td></tr></table>'
                if interrupt == 'different_columns' else '<table><tr><td>B</td><td>4</td></tr></table>')
        table(out, html, 1, 1)
        geometry = [(b['block_id'], b['locator']['bbox'][:], b['locator']['page_index'], b['raw_content'])
                    for b in out.items if b['kind'] == 'table']
        organize_table_context(out.items)
        assert geometry == [(b['block_id'], b['locator']['bbox'], b['locator']['page_index'], b['raw_content'])
                            for b in out.items if b['kind'] == 'table']
        last = out.items[-1]['table_context']
        assert bool(last['link_path']) == (interrupt == 'none')
        if interrupt == 'none':
            p = packet_for(out.items, '4')
            cell = p['model_input']['units'][0]['cells'][1]
            assert [p['anchors'][a]['raw_content'] for a in cell['headers']] == ['Rate']


def test_preamble_scope_stops_at_table_section_and_sheet_boundaries(tmp_path):
    entries = [
        {'type': 'text', 'text': 'Sheet A', 'text_level': 1, 'page_idx': 0},
        {'type': 'table', 'table_body': '<table><tr><td>Only the following table: provisional.</td></tr></table>', 'page_idx': 0},
        {'type': 'table', 'table_body': '<table><tr><th>Item</th><th>Value</th></tr><tr><td>A</td><td>1</td></tr></table>', 'page_idx': 0},
        {'type': 'table', 'table_body': '<table><tr><th>Item</th><th>Value</th></tr><tr><td>B</td><td>2</td></tr></table>', 'page_idx': 0},
        {'type': 'table', 'table_body': '<table><tr><td>All amounts in EUR millions except percentages.</td></tr></table>', 'page_idx': 0},
        {'type': 'table', 'table_body': '<table><tr><th>Item</th><th>Value</th></tr><tr><td>C</td><td>3</td></tr></table>', 'page_idx': 0},
        {'type': 'table', 'table_body': '<table><tr><td>All amounts in GBP thousands.</td></tr></table>', 'page_idx': 0},
        {'type': 'table', 'table_body': '<table><tr><th>Item</th><th>Value</th></tr><tr><td>D</td><td>4</td></tr></table>', 'page_idx': 0},
        {'type': 'text', 'text': 'New section', 'text_level': 1, 'page_idx': 0},
        {'type': 'table', 'table_body': '<table><tr><th>Item</th><th>Value</th></tr><tr><td>E</td><td>5</td></tr></table>', 'page_idx': 0},
        {'type': 'table', 'table_body': '<table><tr><th>Item</th><th>Value</th></tr><tr><td>F</td><td>6</td></tr></table>', 'page_idx': 1},
    ]
    result = tmp_path / 'result'; result.mkdir()
    (result / 'test_content_list.json').write_text(json.dumps(entries))
    blocks = normalize_parse_result({'source_version_id': 'S', 'result_dir': str(tmp_path),
                                    'profile': {'route': 'mineru', 'extension': '.xlsx', 'model_version': 'pipeline'}})
    visible = {n: json.dumps(packet_for(blocks, n)['model_input']) for n in '123456'}
    assert 'provisional' in visible['1'] and 'provisional' not in visible['2']
    assert 'EUR millions except percentages' in visible['3']
    assert 'GBP thousands' in visible['4'] and 'EUR millions' not in visible['4']
    assert 'GBP' not in visible['5'] and 'GBP' not in visible['6']


def test_internal_note_bands_do_not_broadcast_sparse_business_rows():
    out = _Blocks('S')
    table(out, '<table><tr><th>Item</th><th>Amount</th></tr><tr><td>A</td><td>1</td></tr>'
          '<tr><td>说明：仅限白班。</td><td></td></tr>'
          '<tr><th>Item</th><th>Amount</th></tr><tr><td>B</td><td>2</td></tr>'
          '<tr><td>Unrelated business row</td><td></td></tr></table>', 0, 0)
    organize_table_context(out.items)
    a = json.dumps(packet_for(out.items, '1')['model_input'], ensure_ascii=False)
    b = json.dumps(packet_for(out.items, '2')['model_input'], ensure_ascii=False)
    assert '仅限白班' in a and '仅限白班' not in b
    assert 'Unrelated business row' not in a




def test_explicit_appendix_reference_carries_only_its_governing_section():
    out = _Blocks('S')
    out.add('heading', 'Planned adjustment', {}, level=1)
    out.add('text', 'For the proposed changes see “Capacity variation schedule”.', {})
    out.add('text', 'If approved, these changes become effective upon notice.', {})
    out.add('heading', 'Other business', {}, level=1)
    out.add('text', 'Other plans are effective immediately.', {})
    out.add('heading', 'Appendix: “Capacity variation schedule”', {}, level=1)
    table(out, '<table><tr><th>Object</th><th>After</th></tr><tr><td>A</td><td>6</td></tr></table>', 5, 0)
    organize_table_context(out.items)
    visible = json.dumps(packet_for(out.items, '6')['model_input'])
    assert 'If approved' in visible and 'proposed changes' in visible
    assert 'Other plans' not in visible


@pytest.mark.parametrize('title_kind', ['text', 'caption', 'narrative_table'])
def test_adjacent_explicit_table_title_resolves_without_retyping_source_heading(title_kind):
    out = _Blocks('S')
    out.add('heading', 'Change proposal', {}, level=1)
    out.add('text', 'For the proposed changes see “Lease variation schedule”.', {})
    out.add('text', 'If approved, these changes become effective upon notice.', {})
    out.add('heading', 'Other business: “Unrelated operating schedule”', {}, level=1)
    out.add('text', 'Other plans are effective immediately.', {})
    if title_kind == 'narrative_table':
        table(out, '<table><tr><td>Appendix C: “Lease variation schedule”</td></tr></table>', 4, 0)
    else:
        out.add('text', 'Appendix C: “Lease variation schedule”', {'page_index': 4},
                role='table_caption' if title_kind == 'caption' else None)
    table(out, '<table><tr><th>Object</th><th>After</th></tr><tr><td>Vela</td><td>9</td></tr></table>', 4, 1)
    table(out, '<table><tr><td>Nova</td><td>10</td></tr></table>', 5, 2)
    before = [(b['block_id'], b['kind'], b['parent_block_id'], b['content'], b.get('raw_content'),
               {k: b['locator'].get(k) for k in ['space', 'page_index', 'bbox', 'table_id', 'row_index']})
              for b in out.items]
    organize_table_context(out.items)
    after = [(b['block_id'], b['kind'], b['parent_block_id'], b['content'], b.get('raw_content'),
              {k: b['locator'].get(k) for k in ['space', 'page_index', 'bbox', 'table_id', 'row_index']})
             for b in out.items]
    # Relation annotations must not rewrite the physical parser output.
    assert before == after
    for value in ['9', '10']:
        visible = json.dumps(packet_for(out.items, value)['model_input'])
        assert 'If approved' in visible and 'proposed changes' in visible
        assert 'Other plans' not in visible and 'Unrelated operating schedule' not in visible


def test_nonadjacent_title_and_inline_mention_do_not_authorize_attachment():
    for caption in ['Earlier prose mentions “Lease variation schedule” as an example.',
                    'Appendix D: “Staff allocation schedule”']:
        out = _Blocks('S')
        out.add('heading', 'Change proposal', {}, level=1)
        out.add('text', 'For the proposed changes see “Lease variation schedule”.', {})
        out.add('text', 'If approved, these changes become effective upon notice.', {})
        out.add('heading', 'Other business', {}, level=1)
        out.add('text', 'Appendix C: “Lease variation schedule”', {})
        out.add('text', caption, {})
        table(out, '<table><tr><th>Object</th><th>Value</th></tr><tr><td>Vela</td><td>11</td></tr></table>', 4, 0)
        organize_table_context(out.items)
        assert 'If approved' not in json.dumps(packet_for(out.items, '11')['model_input'])


def test_continuation_inherits_latest_header_band_without_combining_periods():
    out = _Blocks('S')
    table(out, '<table><tr><th>Item</th><th>Period A</th></tr><tr><td>X</td><td>1</td></tr>'
          '<tr><th>Item</th><th>Period B</th></tr><tr><td>Y</td><td>2</td></tr></table>', 0, 0)
    table(out, '<table><tr><td>Z</td><td>3</td></tr></table>', 1, 1)
    organize_table_context(out.items)
    p = packet_for(out.items, '3')
    cell = p['model_input']['units'][0]['cells'][1]
    assert [p['anchors'][a]['raw_content'] for a in cell['headers']] == ['Period B']

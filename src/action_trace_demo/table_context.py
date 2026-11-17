"""Logical table links and bounded annotation scopes over existing parser blocks.

Continuation tests follow PaddleX's layout/column checks. Like AWS TRP LINK,
this module never moves rows, joins boundary cells, or changes source geometry.
See docs/m01_table_context.md for pinned upstream references and differences.
"""
from __future__ import annotations

from collections import defaultdict
import re
import unicodedata


_MARGINAL = {'page_number', 'page_header', 'page_footer', 'header', 'footer',
             'document_navigation', 'merged_table_continuation', 'table_footnote'}
_CONTINUED = re.compile(r"^(?:续|續|续表|續表|continued|continue|cont['’]?d)[.：:、\s]*$", re.I)
_GLOBAL_NOTE = re.compile(r'\ball\s+(?:amounts|figures|values)\b|\bamounts\s+(?:in|are)\b|'
                          r'\bunless\s+(?:otherwise\s+)?(?:stated|noted)\b|'
                          r'所有(?:金额|数据|数值)|(?:本表|全表|全篇|本节|本章).*?(?:单位|金额)|金额单位', re.I)
_NOTE = re.compile(r'^(?:注(?:释)?|说明|单位|币种|口径|期间)\s*[:：]|'
                   r'^(?:notes?\s*[:：]|units?\s*[:：]|amounts\s+in\b|all\s+(?:amounts|figures|values)\b)', re.I)
_REFERENCE = re.compile(r'详见|参见|见附件|见附录|\bsee\b|\brefer(?:red)?\b', re.I)
_CONDITION = re.compile(r'需经|须经|如.*(?:通过|批准)|生效|条件|\bif\b|\bupon\b|'
                        r'\bsubject to\b|\bunless\b|\beffective\b|\bapproval\b', re.I)
_STANDALONE_TITLE = re.compile(
    r'^(?:(?:附件|附录|附表|表|appendix|annex|attachment|exhibit|schedule|table)\s*'
    r'[A-Za-z0-9一二三四五六七八九十百零〇.\-]*\s*[:：、.]?\s*)?'
    r'(?:《[^》]+》|“[^”]+”|"[^"]+")\s*$', re.I)
_APPENDIX_TITLE = re.compile(r'^(?:附件|附录)|^(?:appendix|annex|attachment|exhibit)\b', re.I)


def _standalone_title(block):
    """A title can be plain text or a one-cell row, without retagging it."""
    return block['kind'] in {'text', 'heading', 'table'} and bool(
        _STANDALONE_TITLE.fullmatch(block['content'].strip()))


def _title_blocks(table, blocks, by_block, captions):
    """Use an explicit caption or the nearest visible title, then parent heading.

    Never search past ordinary prose/another table for a convenient title. A
    parser's stale parent heading remains untouched in the physical record.
    """
    if captions:
        return captions
    for block in reversed(blocks[:table['rows'][0]['order_index']]):
        if _marginal(block):
            continue
        if _standalone_title(block):
            return [block]
        break
    parent = by_block.get(table['rows'][0].get('parent_block_id'))
    return [parent] if parent else []


def _text(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text)).casefold()


def _marginal(block):
    return block.get('structural_role') in _MARGINAL or not block.get('extraction_eligible', True)


def _refs(block):
    return block.get('evidence_refs', [])


def _tables(blocks):
    groups = {}
    for block in blocks:
        tid = block['locator'].get('table_id')
        if block['kind'] == 'table' and tid:
            groups.setdefault(tid, []).append(block)
    tables = []
    for tid, rows in groups.items():
        loc = rows[0]['locator']
        cells = {r['evidence_ref_id']: r for b in rows for r in _refs(b)}
        ncols = max((c['column_index'] + c['colspan']
                     for b in rows for c in b['locator']['columns']), default=0)
        header_rows = [i for i, b in enumerate(rows) if b.get('structural_role') == 'table_header']
        header_band = []
        if header_rows:
            end = header_rows[-1]
            start = end
            while start > 0 and rows[start - 1].get('structural_role') == 'table_header':
                start -= 1
            header_band = rows[start:end + 1]
        headers = [r for b in header_band for r in _refs(b)]
        candidates = [r for r in cells.values() if r['locator'].get('header_candidate')]
        pages = loc.get('page_indices', [loc.get('page_index')])
        regions = {loc.get('page_index'): loc.get('bbox')}
        regions.update({s['page_index']: s['bbox'] for s in loc.get('merged_segments', [])})
        # A one-column narrative preamble is not a data-table continuation.
        narrative = bool(cells) and all(
            sum(bool(r['raw_content'].strip()) for r in _refs(b)) <= 1 for b in rows
        ) and not any(re.fullmatch(r'[\d.,%+\-\s]+', r['raw_content'])
                      for r in cells.values() if r['raw_content'].strip())
        tables.append({'id': tid, 'rows': rows, 'loc': loc, 'ncols': ncols,
                       'headers': headers, 'candidates': candidates, 'regions': regions,
                       'first_page': min(pages) if None not in pages else None,
                       'last_page': max(pages) if None not in pages else None,
                       'narrative': narrative, 'root': tid, 'path': [], 'status': 'standalone'})
    return tables


def _geometry(previous, current):
    a = previous['regions'].get(previous['last_page'])
    b = current['regions'].get(current['first_page'])
    if not a or not b or previous['loc'].get('bbox_space') != current['loc'].get('bbox_space'):
        return False
    aw, bw = a[2] - a[0], b[2] - b[0]
    # Width and both horizontal edges matter; equal-width side-by-side tables
    # must not link. The 10% width tolerance follows PaddleX's implementation.
    return (min(aw, bw) > 0 and abs(aw - bw) / min(aw, bw) < .1
            and max(abs(a[0] - b[0]), abs(a[2] - b[2])) / min(aw, bw) < .05)


def _boundary_signature(table):
    return [_text(r['raw_content']) for r in table['headers'] or table['candidates']]


def _overlap(left, right):
    """A real repeated boundary phrase, not a numeric or short generic label."""
    a, b = _text(left), _text(right)
    return any(a[-n:] == b[:n] for n in range(min(len(a), len(b), 120), 11, -1))


def _passage_bridge(previous, current, between):
    """Keep degraded text-only continuation pages as candidates, never facts.

    Require a single intervening page, aligned multi-column prose, no outside
    content, and a repeated phrase at a boundary. This extends the upstream
    adjacent-table rules without treating arbitrary page gaps as continuity.
    """
    body = [b for b in between if not _marginal(b)]
    if not body or any(b['kind'] not in {'text', 'heading', 'list'} for b in body):
        return False
    if {b['locator'].get('page_index') for b in body} != {previous['last_page'] + 1}:
        return False
    boxes = [b['locator'].get('bbox') for b in body]
    region = current['regions'][current['first_page']]
    if not region or not all(boxes):
        return False
    width = region[2] - region[0]
    if any(b[0] < region[0] - .03 * width or b[2] > region[2] + .03 * width for b in boxes):
        return False
    # Infer prose lanes from horizontal overlap. Titles inside a lane remain
    # source text; a heading spanning the lanes is a section break.
    lanes = []
    for box in sorted(boxes):
        lane = next((l for l in lanes if min(l[1], box[2]) - max(l[0], box[0]) >
                     .5 * min(l[1] - l[0], box[2] - box[0])), None)
        if lane is None:
            lanes.append([box[0], box[2], 1])
        else:
            lane[0], lane[1], lane[2] = min(lane[0], box[0]), max(lane[1], box[2]), lane[2] + 1
    if len(lanes) < 2 or any(l[2] < 2 for l in lanes):
        return False
    if any(a[1] > b[0] for a, b in zip(lanes, lanes[1:])):
        return False
    following = [r['raw_content'] for r in _refs(current['rows'][0])]
    prior = previous['rows'][-1]['content']
    bridge_text = _text(''.join(b['content'] for b in body))
    return (any(_overlap(prior, b['content']) for b in body) or
            any(len(_text(text)) >= 24 and _text(text)[:24] in bridge_text for text in following))


def _link_tables(blocks, tables):
    by_id = {t['id']: t for t in tables}
    by_block = {b['block_id']: b for b in blocks}
    for previous, current in zip(tables, tables[1:]):
        if previous['narrative'] or current['narrative'] or previous['last_page'] is None or current['first_page'] is None:
            continue
        gap = current['first_page'] - previous['last_page']
        if gap not in (1, 2) or not _geometry(previous, current):
            continue
        between = blocks[previous['rows'][-1]['order_index'] + 1:current['rows'][0]['order_index']]
        bridge = gap == 2 and _passage_bridge(previous, current, between)
        if (gap == 1 and any(not _marginal(b) for b in between)) or (gap == 2 and not bridge):
            continue
        root = by_id[previous['root']]
        same_columns = current['ncols'] == root['ncols']
        continued = any(_CONTINUED.fullmatch(r['raw_content'].strip()) for r in current['candidates'])
        # Missing-column fragments can be linked as uncertain context. Their
        # cells never receive a guessed positional mapping onto root columns.
        if not same_columns and not continued:
            continue
        root_signature, current_signature = _boundary_signature(root), _boundary_signature(current)
        if current['headers'] and root_signature != current_signature:
            continue
        current['root'] = root['id']
        current['status'] = 'candidate' if bridge or not same_columns or previous['status'] == 'candidate' else 'rule_linked'
        current['path'] = [*previous['path'], {'previous_table_id': previous['id'],
            'next_table_id': current['id'], 'basis': ['aligned_horizontal_bounds',
                'matching_root_column_grid' if same_columns else 'continuation_marker_unresolved_grid',
                'layout_and_boundary_text_bridge' if bridge else 'no_intervening_body'],
            'status': current['status'], 'column_mapping': 'aligned' if same_columns else 'unresolved',
            'bridge_block_ids': [b['block_id'] for b in between if not _marginal(b)] if bridge else []}]
        previous['next_table_id'] = current['id']
    for table in tables:
        root = by_id[table['root']]
        info = {'logical_table_id': root['id'], 'fragment_table_id': table['id'],
                'status': table['status'], 'link_path': table['path'],
                'column_mapping': 'aligned' if table['ncols'] == root['ncols'] else 'unresolved'}
        if table['path']:
            info['previous_table_id'] = table['path'][-1]['previous_table_id']
        if table.get('next_table_id'):
            info['next_table_id'] = table['next_table_id']
        for block in table['rows']:
            block['table_context'] = info
            block['locator']['logical_table_id'] = root['id']
            for ref in _refs(block):
                ref['locator']['logical_table_id'] = root['id']
        if table is root or info['column_mapping'] == 'unresolved':
            continue
        inherited = root['headers'] or root['candidates']
        for block in table['rows']:
            related = []
            for ref in _refs(block):
                loc = ref['locator']
                matches = [h for h in inherited if
                    h['locator']['column_index'] < loc['column_index'] + loc['colspan'] and
                    loc['column_index'] < h['locator']['column_index'] + h['locator']['colspan']]
                # An untyped leading row or uncertain link stays a candidate.
                key = 'header_ref_ids' if root['headers'] and table['status'] == 'rule_linked' else 'candidate_header_ref_ids'
                loc[key] = [h['evidence_ref_id'] for h in matches]
                if key == 'header_ref_ids':
                    loc['candidate_header_ref_ids'] = []
                related.extend(h['block_id'] for h in matches)
            # Local first data rows remain extractable, but no longer masquerade
            # as an extra leading-header candidate after root headers arrived.
            local_candidates = {r['block_id'] for r in table['candidates']}
            root_heading = root['rows'][0].get('parent_block_id')
            block['context_refs'] = list(dict.fromkeys([
                *[bid for bid in block['context_refs'] if bid not in local_candidates and
                  by_block[bid]['kind'] != 'heading'], *related,
                *([root_heading] if root_heading else [])]))


def _associate_notes(blocks, tables):
    by_start = {t['rows'][0]['block_id']: t for t in tables}
    table_blocks = {b['block_id'] for t in tables for b in t['rows']}
    preamble, global_notes, last_table = [], [], None
    boundary = None

    def attach(table, notes, scope, basis):
        for row in table['rows']:
            row.setdefault('table_note_bindings', []).extend(
                {'block_id': note['block_id'], 'scope': scope, 'basis': basis,
                 'status': 'context_only', 'logical_table_id': table['root']}
                for note in notes if note['block_id'] != row['block_id'])
            row['context_refs'] = list(dict.fromkeys([*row['context_refs'],
                *[n['block_id'] for n in notes if n['block_id'] != row['block_id']]]))

    for block in blocks:
        loc = block['locator']
        # Office page indices represent sheets/slides in this rendering. Never
        # let workbook-level-looking text leak into another sheet by proximity.
        current_boundary = (loc.get('parse_file'), loc.get('page_index') if loc.get('space') == 'parsed_office' or
                            loc.get('mineru_type') and loc.get('bbox') is None else None)
        if boundary != current_boundary or block['kind'] == 'heading':
            preamble, global_notes, last_table = [], [], None
            boundary = current_boundary
        if block['block_id'] in by_start:
            table = by_start[block['block_id']]
            if table['path']:
                # Body prose in a degraded continuation page is not a new
                # preamble for the returning table fragment.
                preamble, last_table = [], table
                continue
            if table['narrative']:
                titles = [row for row in table['rows'] if _standalone_title(row)]
                preamble = titles if titles else [*preamble, *table['rows']]
                if any(_APPENDIX_TITLE.search(row['content']) for row in titles):
                    global_notes = []
                continue
            new_general = [n for n in preamble if _GLOBAL_NOTE.search(n['content'])]
            if new_general:
                global_notes = new_general
            attach(table, global_notes, 'section', 'explicit_general_note')
            attach(table, [n for n in preamble if n not in new_general], 'table', 'preceding_preamble')
            preamble, last_table = [], table
        elif block['block_id'] in table_blocks:
            continue
        elif block.get('structural_role') in {'table_footnote', 'table_caption'}:
            if block.get('structural_role') == 'table_footnote' and last_table:
                attach(last_table, [block], 'table', 'following_table_footnote')
            elif block.get('structural_role') == 'table_caption':
                if _standalone_title(block):
                    preamble, last_table = [block], None
                    if _APPENDIX_TITLE.search(block['content']):
                        global_notes = []
                else:
                    preamble.append(block)
        elif block['kind'] == 'text' and not _marginal(block):
            if _standalone_title(block):
                preamble, last_table = [block], None
                if _APPENDIX_TITLE.search(block['content']):
                    global_notes = []
            elif last_table and _NOTE.match(block['content']):
                attach(last_table, [block], 'table', 'following_note')
            else:
                preamble.append(block)
        elif block['kind'] not in {'heading', 'metadata'}:
            preamble = []
    # Explicit parser captions/notes already have an exact table owner. Keep
    # them visible as scoped annotations, rather than untyped nearby prose.
    annotations, by_block, sections = defaultdict(list), {}, defaultdict(list)
    for block in blocks:
        by_block[block['block_id']] = block
        sections[block.get('parent_block_id')].append(block)
        if block.get('structural_role') in {'table_caption', 'table_footnote'}:
            annotations[block['locator'].get('table_id')].append(block)
    for table in tables:
        owned = annotations[table['id']]
        attach(table, owned, 'table', 'parser_table_annotation')
        # Sparse business rows are not universal qualifiers. Recognized note
        # rows have a bounded row-group scope. A trailing note describes the
        # preceding band; an internal note starts the following band.
        rows = table['rows']
        notes = [i for i, row in enumerate(rows) if
                 sum(bool(r['raw_content'].strip()) for r in _refs(row)) == 1 and
                 _NOTE.match(row['content'].lstrip(' |'))]
        for i in notes:
            end = next((j for j in range(i + 1, len(rows)) if j in notes or
                        rows[j].get('structural_role') == 'table_header'), len(rows))
            targets = rows[i + 1:end]
            if not targets:
                start = max((j for j in range(i) if j in notes or
                             rows[j].get('structural_role') == 'table_header'), default=-1) + 1
                targets = rows[start:i]
            attach({'rows': targets, 'root': table['root']}, [rows[i]], 'row_group', 'in_table_note_band')
        if table['root'] != table['id']:
            continue
        # Explicit references can give an appendix its governing conditions.
        # Match the actual quoted/caption title; do not retrieve generic nearby
        # proposals or infer that every comparison table is still proposed.
        title_blocks = _title_blocks(table, blocks, by_block,
                                     [n for n in owned if n.get('structural_role') == 'table_caption'])
        table['title_blocks'] = title_blocks
        titles = [title for block in title_blocks
                  for title in re.findall(r'《([^》]+)》|[“"]([^”"]+)[”"]', block['content'])]
        title_texts = [_text(a or b) for a, b in titles if len(_text(a or b)) >= 8]
        parent_id = table['rows'][0].get('parent_block_id')
        if title_blocks and all(b['block_id'] != parent_id for b in title_blocks):
            attach(table, title_blocks, 'table', 'explicit_table_title')
        for block in blocks:
            if (block['kind'] != 'text' or block['order_index'] >= table['rows'][0]['order_index'] or
                    not _REFERENCE.search(block['content']) or
                    not any(title in _text(block['content']) for title in title_texts)):
                continue
            attach(table, [block], 'referenced_section', 'explicit_title_reference')
            parent = block.get('parent_block_id')
            clauses = [b for b in sections[parent] if parent and b['kind'] == 'text' and
                       b['block_id'] != block['block_id'] and _CONDITION.search(b['content'])]
            attach(table, clauses, 'referenced_section', 'referencing_section_condition')
    # A continued fragment inherits only the root table's annotated context.
    # It does not inherit every intervening section title or body paragraph.
    by_id = {t['id']: t for t in tables}
    for table in tables:
        root = by_id[table['root']]
        title_blocks = root.get('title_blocks', [])
        parent_id = root['rows'][0].get('parent_block_id')
        # A nearer explicit title defines the logical table context, while the
        # original parent/kind/geometry remain available in parser evidence.
        if title_blocks and all(b['block_id'] != parent_id for b in title_blocks):
            for row in table['rows']:
                row['context_refs'] = [bid for bid in row['context_refs'] if bid != parent_id]
        if table['root'] != table['id']:
            notes = [n for n in by_id[table['root']]['rows'][0].get('table_note_bindings', [])
                     if n['scope'] != 'row_group']
            for row in table['rows']:
                row.setdefault('table_note_bindings', []).extend({**n, 'basis': 'linked_table_' + n['basis'],
                    'link_status': table['status']} for n in notes)
                row['context_refs'] = list(dict.fromkeys([*row['context_refs'], *[n['block_id'] for n in notes]]))
        for row in table['rows']:
            row['table_note_bindings'] = list({(n['block_id'], n['scope']): n
                for n in row.get('table_note_bindings', [])}.values())


def organize_table_context(blocks):
    """Annotate each source independently; physical evidence is left intact."""
    sources = defaultdict(list)
    for block in blocks:
        sources[block['source_version_id']].append(block)
    for source in sources.values():
        tables = _tables(source)
        _link_tables(source, tables)
        _associate_notes(source, tables)
    return blocks

"""HTML/GFM tables with explicit cell-to-header relationships.

Untagged leading cells are candidates, never silently promoted to headers.
HTML recovery and Markdown tokenization belong to lxml and markdown-it-py.
"""
from __future__ import annotations

from lxml import html as html_parser, etree
from markdown_it import MarkdownIt


def markdown_table_spans(text):
    """Yield source line ranges, including raw HTML tables embedded in Markdown."""
    parser = MarkdownIt('commonmark', {'html': True}).enable('table')
    tokens = parser.parse(text)
    lines = text.splitlines(keepends=True)
    for i, token in enumerate(tokens):
        if token.type == 'table_open':
            depth, end = 1, i + 1
            while depth:
                depth += (tokens[end].type == 'table_open') - (tokens[end].type == 'table_close')
                end += 1
            a, z = token.map
            yield a, z, ''.join(lines[a:z]), parser.renderer.render(tokens[i:end], parser.options, {})
        elif token.type == 'html_block' and '<table' in token.content.lower():
            a, z = token.map
            yield a, z, ''.join(lines[a:z]), token.content


def parse_table(source):
    """Return outer rows/cells and exact associations; no business vocabulary."""
    markdown = not source.lstrip().startswith('<')
    raw_lines = None
    if markdown:
        spans = list(markdown_table_spans(source))
        if len(spans) != 1:
            raise ValueError('Expected one HTML or GFM table')
        _, _, raw, source_html = spans[0]
        raw_lines = raw.splitlines()
    else:
        source_html = source
    root = html_parser.fragment_fromstring(source_html, create_parent='div')
    tables = root.xpath('.//table[not(ancestor::table)]')
    if not tables:
        root = html_parser.fragment_fromstring('<table>'+source_html+'</table>', create_parent='div')
        tables = root.xpath('.//table')
    if len(tables) != 1:
        raise ValueError('Expected one outer table')
    table = tables[0]
    row_nodes = [r for r in table.iter('tr') if next(r.iterancestors('table')) is table]
    if not row_nodes:
        raise ValueError('Table contains no rows')
    groups = []
    for r in row_nodes:
        groups.append(next((a for a in r.iterancestors() if a.tag in ('thead','tbody','tfoot')), table))
    group_ends = {g:i+1 for i,g in enumerate(groups)}
    rows, occupied, all_cells = [], set(), []
    for ri, node in enumerate(row_nodes):
        cells, ci = [], 0
        group_end = group_ends[groups[ri]]
        for element in node:
            if element.tag not in ('th','td'):
                continue
            while (ri,ci) in occupied: ci += 1
            rs = int(element.get('rowspan','1')); cs = int(element.get('colspan','1'))
            rs = group_end-ri if rs == 0 else rs
            if rs < 1 or cs < 1:
                raise ValueError('Invalid cell span')
            header = element.tag == 'th' or groups[ri].tag == 'thead' or element.get('role') in ('rowheader','columnheader')
            cell = {'key':f'{ri}:{ci}', 'row_index':ri,'column_index':ci,'rowspan':rs,'colspan':cs,
                'text':''.join(element.itertext()).strip(), 'header':header,
                'header_basis':'thead' if groups[ri].tag=='thead' else 'th' if element.tag=='th' else element.get('role'),
                'scope':element.get('scope',''), 'html_id':element.get('id'),
                'explicit_headers':element.get('headers').split() if element.get('headers') is not None else None,
                'group':groups[ri], 'header_keys':[], 'candidate_header_keys':[]}
            cells.append(cell); all_cells.append(cell)
            occupied.update((r,c) for r in range(ri,ri+rs) for c in range(ci,ci+cs))
            ci += cs
        raw = etree.tostring(node,encoding='unicode',method='html',with_tail=False)
        if raw_lines is not None:
            line_index = ri if ri == 0 else ri+1  # GFM delimiter is not a data row.
            raw = raw_lines[line_index] if line_index < len(raw_lines) else ''
        rows.append({'cells':cells,'raw':raw,'section':groups[ri].tag})
    by_key = {c['key']:c for c in all_cells}
    id_map = {c['html_id']:c for c in all_cells if c['html_id'] and c['header']}
    # A band can have several levels. A later all-header band supersedes it.
    band = -1
    prior_header_row = False
    for ri,row in enumerate(rows):
        column_row = bool(row['cells']) and all(c['header'] and c['scope'] not in ('row','rowgroup') for c in row['cells'])
        if column_row and not prior_header_row: band += 1
        for c in row['cells']:
            scope = c['scope']
            c['axis'] = ('row' if scope in ('row','rowgroup') or c['header_basis']=='rowheader' else
                'column' if scope in ('col','colgroup') or c['header_basis'] in ('thead','columnheader') or column_row else 'row')
            c['band'] = band
        prior_header_row = column_row
    column_headers = [c for c in all_cells if c['header'] and c['axis']=='column']
    row_headers, group_headers = {}, {}
    for h in all_cells:
        if not h['header'] or h['axis']!='row':
            continue
        if h['scope']=='rowgroup':
            group_headers.setdefault(h['group'],[]).append(h)
        else:
            for ri in range(h['row_index'],h['row_index']+h['rowspan']):
                row_headers.setdefault(ri,[]).append(h)
    leading = rows[0]['cells'] if not any(c['header'] for c in all_cells) else []
    for row in rows:
        for c in row['cells']:
            if c['explicit_headers'] is not None:
                headers = [id_map[k] for k in c['explicit_headers'] if k in id_map]
            else:
                prior = [h for h in column_headers if h['row_index'] < c['row_index'] and
                    h['column_index'] < c['column_index']+c['colspan'] and c['column_index'] < h['column_index']+h['colspan']]
                last_band = max((h['band'] for h in prior), default=-1)
                headers = [h for h in prior if h['band']==last_band]
                headers += [h for h in row_headers.get(c['row_index'],[])+group_headers.get(c['group'],[])
                    if h['column_index'] < c['column_index']]
            c['header_keys'] = list(dict.fromkeys(h['key'] for h in headers if h is not c))
            c['candidate_header_keys'] = [h['key'] for h in leading if h['row_index'] < c['row_index'] and
                h['column_index'] < c['column_index']+c['colspan'] and c['column_index'] < h['column_index']+h['colspan']]
            c['candidate'] = c in leading
    for c in all_cells:
        # Preserve only serializable source facts; group identity is internal.
        del c['group']
    return {'rows':rows, 'cells':by_key, 'source_format':'markdown' if markdown else 'html',
        'caption':[''.join(n.itertext()).strip() for n in table if n.tag=='caption']}

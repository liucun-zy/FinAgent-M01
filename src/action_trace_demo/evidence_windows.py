"""Stable source blocks, observed structural context, and bounded reading views."""
from __future__ import annotations

from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
import csv
import io
import json
import math
import re

from .mineru_content import NORMALIZATION_VERSION, normalization_diagnostics


def token_estimate(value: Any) -> int:
    """Lightweight packing heuristic: non-ASCII characters + ASCII/4.

    This is not a tokenizer or the provider's usage. The existing model client
    independently retains its conservative byte-based monetary reservation.
    """
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    non_ascii = sum(ord(c) > 127 for c in text)
    return non_ascii + math.ceil((len(text) - non_ascii) / 4)


def request_input_estimate(system, prompt, tools, reserve_tokens=256):
    """Estimate the actual nested wire shape, including escaped JSON in text.

    Counting the prompt and tools separately loses quote/backslash overhead.
    The small reserve covers the DSH system-section and last-request text.
    """
    return token_estimate({"messages": [{"role": "system", "content": system},
                                        {"role": "user", "content": prompt}],
                           "tools": [{"type": "function", "function": tool} for tool in tools]}) + reserve_tokens


def _id(prefix: str, *parts: Any) -> str:
    return prefix + sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


class _Node:
    def __init__(self, tag="root", attrs=None, path="", parent=None):
        self.tag, self.attrs, self.path, self.parent = tag, dict(attrs or []), path, parent
        self.children: list[_Node | str] = []

    def text(self):
        return "".join(c.text() if isinstance(c, _Node) else c for c in self.children).strip()

    def descendants(self, tags):
        for child in self.children:
            if isinstance(child, _Node):
                if child.tag in tags:
                    yield child
                yield from child.descendants(tags)


class _HTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        index = sum(isinstance(c, _Node) and c.tag == tag for c in self.current.children)
        node = _Node(tag, attrs, self.current.path + f"/{tag}[{index}]", self.current)
        self.current.children.append(node)
        if tag not in {"br", "img", "meta", "link", "hr", "input", "source", "wbr"}:
            self.current = node

    def handle_endtag(self, tag):
        cursor = self.current
        while cursor.parent:
            if cursor.tag == tag:
                self.current = cursor.parent
                break
            cursor = cursor.parent

    def handle_data(self, data):
        self.current.children.append(data)


def _text_parts(text: str, limit=1200):
    """Sentence-first exact spans; a single overlong sentence gets exact subspans."""
    start, current = 0, ""
    for match in re.finditer(r".*?(?:[。！？]|[.!?](?:\s+|$)|\n|$)", text, re.S):
        part = match.group()
        if not part:
            continue
        if current and token_estimate(current + part) > limit:
            yield start, start + len(current), current
            start += len(current)
            current = ""
        while token_estimate(part) > limit:
            cut, used = 0, 0
            for char in part:
                if used + token_estimate(char) > limit:
                    break
                used += token_estimate(char)
                cut += 1
            yield start, start + cut, part[:cut]
            start += cut
            part = part[cut:]
        current += part
    if current:
        yield start, start + len(current), current


class _Blocks:
    def __init__(self, source_id):
        self.source_id = source_id
        self.items = []
        self.headings: list[tuple[int, str]] = []

    def add(self, kind, content, locator, *, context=(), parent=None, level=None, role=None):
        bid = _id("B_", self.source_id, len(self.items), locator)
        context = list(dict.fromkeys([b for _, b in self.headings] + list(context)))
        block = {"block_id": bid, "source_version_id": self.source_id, "kind": kind,
                 "order_index": len(self.items), "content": content, "locator": locator,
                 "context_refs": context, "parent_block_id": parent or (self.headings[-1][1] if self.headings else None)}
        if role:
            block["structural_role"] = role
        if level is not None:
            block["heading_level"] = level
        self.items.append(block)
        if kind == "heading":
            while self.headings and self.headings[-1][0] >= (level or 1):
                self.headings.pop()
            self.headings.append((level or 1, bid))
        return block

    def text(self, kind, text, locator, **kwargs):
        out = []
        for start, end, part in _text_parts(text):
            if part.strip():
                span = dict(locator)
                span["parsed_char_start"], span["parsed_char_end"] = start, end
                out.append(self.add(kind, part, span, **kwargs))
        return out

    def table(self, html, locator, captions=(), footnotes=()):
        from .table_structure import parse_table
        parsed = parse_table(html)
        table_id = _id("T_", self.source_id, locator)
        context = [self.add("text", text, {**locator, "table_id": table_id, "caption_index": i}, role="table_caption")["block_id"]
                   for i, text in enumerate([*captions, *parsed['caption']]) if text]
        row_blocks, cell_refs, pending = [], {}, []
        for ri, row in enumerate(parsed['rows']):
            cells = row['cells']
            header = bool(cells) and all(c['header'] and c['axis']=='column' for c in cells)
            loc = {**locator, "space":"parsed_table", "source_format":parsed['source_format'], "table_id":table_id,
                "row_index":ri, "columns":[{k:c[k] for k in ('column_index','rowspan','colspan','header')} for c in cells]}
            content = " | ".join(c['text'] for c in cells)
            chunks = list(_text_parts(content)) or [(0,0,"")]
            for start,end,part in chunks:
                block = self.add("table",part,{**loc,"parsed_char_start":start,"parsed_char_end":end}, context=context,
                    role="table_header" if header else "table_row")
                if len(chunks)==1: block['raw_content']=row['raw']
                refs,offset=[],0
                for cell in cells:
                    cstart,cend=offset,offset+len(cell['text'])
                    a,z=max(start,cstart),min(end,cend)
                    if a<z:
                        ref={"evidence_ref_id":_id("EV_",block['block_id'],cell['column_index'],a,z),
                            "source_version_id":self.source_id,"block_id":block['block_id'],
                            "locator":{**{k:v for k,v in loc.items() if k!='columns'},
                                "column_index":cell['column_index'],"rowspan":cell['rowspan'],"colspan":cell['colspan'],
                                "cell_char_start":a-cstart,"cell_char_end":z-cstart,
                                "header_candidate":cell['candidate'],"header_basis":cell['header_basis']},
                            "raw_content":content[a:z],"role":"header" if cell['header'] else "cell"}
                        refs.append(ref);cell_refs.setdefault(cell['key'],[]).append(ref)
                        pending.append((ref,cell))
                    offset=cend+3
                block['evidence_refs']=refs;row_blocks.append(block)
        for ref,cell in pending:
            ref['locator']['header_ref_ids']=[r['evidence_ref_id'] for k in cell['header_keys'] for r in cell_refs.get(k,[])]
            ref['locator']['candidate_header_ref_ids']=[r['evidence_ref_id'] for k in cell['candidate_header_keys'] for r in cell_refs.get(k,[])]
        context += [self.add("text",text,{**locator,"table_id":table_id,"footnote_index":i},role="table_footnote")["block_id"]
                    for i,text in enumerate(footnotes) if text]
        by_ref={r['evidence_ref_id']:r for refs in cell_refs.values() for r in refs}
        for block in row_blocks:
            related=[by_ref[rid]['block_id'] for r in block['evidence_refs']
                for rid in r['locator']['header_ref_ids']+r['locator']['candidate_header_ref_ids']]
            block['context_refs']=list(dict.fromkeys(block['context_refs']+context+[bid for bid in related if bid!=block['block_id']]))



def _direct(artifact, out):
    path = Path(artifact["original_location"])
    text = path.read_text(encoding="utf-8-sig")
    ext = artifact["profile"]["extension"]
    if ext in {".csv", ".tsv"}:
        import html
        rows = list(csv.reader(io.StringIO(text), delimiter="\t" if ext == ".tsv" else ","))
        body = "<table>" + "".join("<tr>" + "".join(f"<{('th' if i == 0 else 'td')}>{html.escape(cell)}</{('th' if i == 0 else 'td')}>" for cell in row) + "</tr>" for i, row in enumerate(rows)) + "</table>"
        if rows:
            out.table(body, {"space": "delimited_record", "record_index_base": 0, "delimiter": "tab" if ext == ".tsv" else "comma"})
        return
    if ext == ".json":
        data = json.loads(text)
        records = list(enumerate(data)) if isinstance(data, list) else [(None, data)]
        for index, value in records:
            pointer = "" if index is None else f"/{index}"
            block = out.add("record", json.dumps(value, ensure_ascii=False), {"space": "json", "json_pointer": pointer})
            refs = []
            def leaves(item, path):
                if isinstance(item, dict):
                    for key, val in item.items():
                        yield from leaves(val, path + "/" + str(key).replace("~", "~0").replace("/", "~1"))
                elif isinstance(item, list):
                    for i, val in enumerate(item):
                        yield from leaves(val, path + f"/{i}")
                else:
                    yield path, item
            for pointer, value in leaves(value, pointer):
                refs.append({"evidence_ref_id": _id("EV_", block["block_id"], pointer), "source_version_id": out.source_id,
                             "block_id": block["block_id"], "locator": {"space": "json", "json_pointer": pointer},
                             "raw_content": json.dumps(value, ensure_ascii=False), "role": "field"})
            block["evidence_refs"] = refs
        return
    def prose(chunk, base):
        for paragraph in re.finditer(r"[^\n].*?(?=\n\s*\n|\Z)", chunk, re.S):
            raw = paragraph.group()
            for match in re.finditer(r"(?m)^#{1,6}[^\n]*(?:\n|$)|(?:[^\n]+(?:\n(?!#)[^\n]+)*)", raw):
                part=match.group();start=base+paragraph.start()+match.start()
                heading=re.match(r"^(#{1,6})\s+",part) if ext=='.md' else None
                loc={"space":"text","char_start":start,"char_end":start+len(part),
                     "line_start":text.count('\n',0,start)+1,"line_end":text.count('\n',0,start+len(part))+1}
                out.text('heading' if heading else 'text',part,loc,level=len(heading.group(1)) if heading else None)
    if ext=='.md':
        from .table_structure import markdown_table_spans
        lines=text.splitlines(keepends=True); offsets=[0]
        for line in lines: offsets.append(offsets[-1]+len(line))
        previous=0
        for a,z,raw,_ in markdown_table_spans(text):
            start,end=offsets[a],offsets[z]
            prose(text[previous:start],previous)
            out.table(raw,{"space":"markdown","char_start":start,"char_end":end,"line_start":a+1,"line_end":z})
            previous=end
        prose(text[previous:],previous)
    else:
        prose(text,0)


def normalize_parse_result(artifact: dict) -> list[dict]:
    from .table_context import organize_table_context
    def finish(blocks):
        for block in blocks:
            block['normalization_version'] = NORMALIZATION_VERSION
        return organize_table_context(blocks)
    out = _Blocks(artifact["source_version_id"])
    if artifact["profile"]["route"] == "direct":
        _direct(artifact, out)
        return finish(out.items)
    directory = Path(artifact["result_dir"]) / "result"
    if artifact["profile"]["model_version"] == "MinerU-HTML":
        paths = list(directory.rglob("main.html"))
        if len(paths) != 1:
            raise ValueError("Unsupported MinerU-HTML result: expected one main.html")
        parser = _HTML(); parser.feed(paths[0].read_text(encoding="utf-8"))
        text_tags = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "table", "li", "blockquote", "pre", "dt", "dd", "address", "figcaption"}
        for node in parser.root.descendants(text_tags | {"img", "body", "main", "article", "section", "div", "span"}):
            ancestor, nested = node.parent, False
            while ancestor:
                if ancestor.tag in text_tags | {"script", "style", "head", "noscript"}:
                    nested = True
                    break
                ancestor = ancestor.parent
            if nested:
                continue
            loc = {"space": "parsed_html", "element_path": node.path, "parse_file": paths[0].name}
            if node.tag == "table":
                def serialize(n):
                    from html import escape
                    if isinstance(n, str):
                        return escape(n)
                    attrs = "".join(f' {k}="{escape(v or "")}"' for k, v in n.attrs.items())
                    return f"<{n.tag}{attrs}>" + "".join(serialize(c) for c in n.children) + f"</{n.tag}>"
                html = serialize(node)
                before, headings = len(out.items), list(out.headings)
                try:
                    out.table(html, loc)
                except (ValueError, TypeError) as error:
                    del out.items[before:]; out.headings = headings
                    block = out.add("text", node.text(), loc, role="unstructured_table_text")
                    block["raw_content"] = html
                    block["normalization"] = {"diagnostics": [{"code": "malformed_table", "severity": "warning",
                        "requires_review": True, "message": f"HTML table structure unreadable: {type(error).__name__}"}]}
                    if not block["content"].strip():
                        block["extraction_eligible"] = False
            elif node.tag == "img":
                block = out.add("image", "", loc, role="requires_visual_read")
                block.update(asset_path=node.attrs.get("src", ""), extraction_eligible=False,
                    normalization={"diagnostics": [{"code": "visual_content_not_read", "severity": "warning",
                        "requires_review": True, "message": "HTML image retained for visual reading"}]})
            elif node.tag in text_tags:
                out.text("heading" if re.fullmatch(r"h[1-6]", node.tag) else "list" if node.tag == "li" else "code" if node.tag == "pre" else "text", node.text(), loc,
                         level=int(node.tag[1]) if re.fullmatch(r"h[1-6]", node.tag) else None)
            else:
                # Plain container text is real HTML content too; only direct
                # text children are emitted, so semantic descendants are not duplicated.
                for i, child in enumerate(node.children):
                    if isinstance(child, str) and child.strip():
                        out.text("text", child, {**loc, "text_child_index": i})
        for block in out.items:
            block["normalization_version"] = NORMALIZATION_VERSION
            block["raw_entry_ref"] = {"parse_file": paths[0].name, "element_path": block["locator"]["element_path"]}
        return finish(out.items)
    from .mineru_content import normalize_content
    return finish(normalize_content(directory, artifact, out))


def evidence_refs(blocks: list[dict]) -> list[dict]:
    refs = []
    for block in blocks:
        refs.append({"evidence_ref_id": "EV_" + block["block_id"], "source_version_id": block["source_version_id"],
                     "block_id": block["block_id"], "locator": block["locator"], "raw_content": block.get("raw_content", block["content"])})
        refs.extend(block.get("evidence_refs", []))
    return refs


CAPACITY_STRATEGY_VERSION = "structure-input-output-wire-v2"
CAPACITY_DEFAULTS = {
    "output_fixed_tokens": 256,
    "output_text_multiplier": 16,
    "output_record_tokens": 192,
    "output_reserve_tokens": 512,
}


def capacity_config(config=None):
    """The small frozen packing policy, independent of task and source names."""
    config = config or {}
    keys = {"window_target_tokens": 1800, "window_max_tokens": 3000,
            "default_context_max_tokens": 800, "batch_input_budget_tokens": 8000,
            "max_windows_per_request": 4, "extraction_output_max_tokens": 4096,
            **CAPACITY_DEFAULTS}
    return {key: config.get(key, value) for key, value in keys.items()}


def estimate_output_tokens(windows, blocks, config=None):
    """Capacity estimate only; never a fact count or a semantic coverage check."""
    config = capacity_config(config)
    main_ids = {bid for window in windows for bid in window["main_block_ids"]}
    main = [block for block in blocks if block["block_id"] in main_ids]
    return (config["output_fixed_tokens"]
            + config["output_text_multiplier"] * sum(token_estimate(b["content"]) for b in main)
            + config["output_record_tokens"] * sum(b["kind"] in {"table", "record"} for b in main))


def _window(main, blocks, config, inherited_context=()):
    by_id = {b["block_id"]: b for b in blocks}
    main_ids = [b["block_id"] for b in main]
    lo, hi = main[0]["order_index"], main[-1]["order_index"]
    source = main[0]["source_version_id"]
    # Observed titles, headers, notes and inherited context are required, not
    # silently removed to meet a packing estimate. The caller enforces input.
    required = list(dict.fromkeys([bid for b in main for bid in b["context_refs"]]
                                  + list(inherited_context)))
    contexts = [bid for bid in required if bid not in main_ids and bid in by_id
                and by_id[bid]["source_version_id"] == source]
    neighbors = [b["block_id"] for b in blocks if b["source_version_id"] == source
                 and b["order_index"] in {lo - 1, hi + 1}]
    spent = sum(token_estimate(by_id[bid]["content"]) for bid in contexts)
    main_cost = sum(token_estimate(b["content"]) for b in main)
    for bid in neighbors:
        if bid in main_ids or bid in contexts:
            continue
        cost = token_estimate(by_id[bid]["content"])
        if (spent + cost <= config["default_context_max_tokens"]
                and main_cost + spent + cost <= config["window_max_tokens"]):
            contexts.append(bid)
            spent += cost
    return {"window_id": _id("W_", main_ids), "source_version_id": source,
            "main_block_ids": main_ids, "context_block_ids": contexts,
            "available_context_refs": [bid for bid in neighbors if bid not in main_ids + contexts],
            "token_estimate": main_cost + spent}


def build_windows(blocks: list[dict], config: dict | None = None) -> list[dict]:
    config = capacity_config(config)
    output_limit = config["extraction_output_max_tokens"] - config["output_reserve_tokens"]
    windows, current = [], []
    def finish():
        if current:
            windows.append(_window(current, blocks, config))
            current.clear()
    for block in blocks:
        proposed = current + [block]
        output = estimate_output_tokens([{"main_block_ids": [b["block_id"] for b in proposed]}], proposed, config)
        if current and (block["source_version_id"] != current[0]["source_version_id"]
                        or block["kind"] == "heading"
                        or sum(token_estimate(b["content"]) for b in proposed) > config["window_target_tokens"]
                        or output > output_limit):
            finish()
        # An indivisible original block remains one window even when the
        # output estimate is high. Do not alter evidence spans to fit a quota.
        current.append(block)
    finish()
    return windows


def render_windows(windows: list[dict], blocks: list[dict]) -> dict:
    ids = set(b for w in windows for b in w["main_block_ids"] + w["context_block_ids"])
    selected = [b for b in blocks if b["block_id"] in ids]
    # Keep table cell locators in refs, avoiding two copies in the readable block.
    rendered = [{k: v for k, v in b.items() if k not in {"evidence_refs", "raw_content"}} for b in selected]
    payload = {"windows": windows, "blocks": rendered, "evidence_refs": evidence_refs(selected)}
    payload["token_estimate"] = token_estimate(payload)
    return payload


def split_window_group(windows, blocks, config=None):
    """Bisect once along existing blocks; return two window lists or [].

    The caller owns the retry allowance and saved-scope check. Original block
    identities, locators and source versions remain untouched.
    """
    config = capacity_config(config)
    by_id = {b["block_id"]: b for b in blocks}
    main_ids = list(dict.fromkeys(bid for w in windows for bid in w["main_block_ids"]))
    if len(main_ids) < 2:
        return []
    weights = [estimate_output_tokens([{"main_block_ids": [bid]}], [by_id[bid]], config)
               - config["output_fixed_tokens"] for bid in main_ids]
    middle = min(range(1, len(main_ids)), key=lambda i: abs(sum(weights[:i]) - sum(weights[i:])))
    children = []
    for half in (set(main_ids[:middle]), set(main_ids[middle:])):
        child = []
        for window in windows:
            main = [by_id[bid] for bid in window["main_block_ids"] if bid in half]
            if main:
                child.append(_window(main, blocks, config, window["context_block_ids"]))
        children.append(child)
    return children


def pack_window_groups(windows: list[dict], request_budget: dict | None = None, *, blocks: list[dict] | None = None,
                       overhead_tokens: int = 0) -> list[dict]:
    config = capacity_config(request_budget)
    budget = config["batch_input_budget_tokens"]
    count = config["max_windows_per_request"]
    output_budget = config["extraction_output_max_tokens"] - config["output_reserve_tokens"]
    groups, pending = [], []
    def size(items):
        if blocks is None:
            return overhead_tokens + sum(w["token_estimate"] for w in items)
        rendered = render_windows(items, blocks)
        return overhead_tokens + token_estimate({"windows": items, "evidence_refs": rendered["evidence_refs"]})
    def output_size(items):
        if blocks is None:
            return config["output_fixed_tokens"] + config["output_text_multiplier"] * sum(w["token_estimate"] for w in items)
        return estimate_output_tokens(items, blocks, config)
    def finish():
        ids = [w["window_id"] for w in pending]
        groups.append({"window_group_id": _id("WG_", ids), "window_ids": ids, "windows": list(pending),
                       "token_estimate": size(pending), "output_token_estimate": output_size(pending),
                       "output_capacity_tokens": output_budget, "capacity_strategy_version": CAPACITY_STRATEGY_VERSION,
                       "over_budget": size(pending) > budget or output_size(pending) > output_budget})
        pending.clear()
    def units(window):
        if blocks is not None and (size([window]) > budget or output_size([window]) > output_budget):
            children = split_window_group([window], blocks, config)
            if children:
                for child in children:
                    for part in child:
                        yield from units(part)
                return
        yield window
    for original in windows:
        for window in units(original):
            if pending and (len(pending) >= count or size(pending + [window]) > budget
                            or output_size(pending + [window]) > output_budget):
                finish()
            pending.append(window)
    if pending:
        finish()
    return groups


def read_context(blocks: list[dict], windows: list[dict], requests: list[dict], provided_block_ids: list[str],
                 extra_token_budget: int = 2000) -> dict:
    by_id = {b["block_id"]: b for b in blocks}
    by_window = {w["window_id"]: w for w in windows}
    seen, chosen, unresolved, used = set(provided_block_ids), [], [], 0
    for request in requests:
        window = by_window.get(request["window_id"])
        if not window:
            raise ValueError("read_context refers to an unknown window")
        main = [by_id[bid] for bid in window["main_block_ids"]]
        source = window["source_version_id"]
        same_source = [b for b in blocks if b["source_version_id"] == source]
        relation = request["relation"]
        target = request.get("target_ref_or_query", "")
        if relation == "neighbors":
            lo, hi = main[0]["order_index"], main[-1]["order_index"]
            candidates = sorted(same_source, key=lambda b: min(abs(b["order_index"]-lo), abs(b["order_index"]-hi)))
        elif relation == "section":
            parents = {b["parent_block_id"] for b in main}
            candidates = [b for b in same_source if b["parent_block_id"] in parents or b["block_id"] in parents]
        elif relation == "table_context":
            tables = {b["locator"].get("table_id") for b in main} - {None}
            candidates = [b for b in same_source if b["locator"].get("table_id") in tables or any(b["block_id"] in m["context_refs"] for m in main)]
        elif relation == "referenced":
            refs = {r["evidence_ref_id"]: r["block_id"] for r in evidence_refs(same_source)}
            bid = refs.get(target, target)
            candidates = [b for b in same_source if b["block_id"] == bid or (target and str(target).casefold() in b["content"].casefold())]
        else:
            raise ValueError(f"Unsupported context relation: {relation}")
        added = 0
        for block in candidates:
            bid = block["block_id"]
            if bid in seen:
                continue
            cost = token_estimate({"block": block, "evidence_refs": evidence_refs([block])})
            if used + cost > extra_token_budget:
                unresolved.append({"window_id": window["window_id"], "block_id": bid, "reason": "context_budget"})
                continue
            chosen.append(block); seen.add(bid); used += cost; added += 1
        if not added and not candidates:
            unresolved.append({"window_id": window["window_id"], "reason": "context_not_found", "relation": relation})
    return {"blocks": chosen, "evidence_refs": evidence_refs(chosen), "unresolved": unresolved, "token_estimate": used}

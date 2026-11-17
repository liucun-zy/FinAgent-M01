"""Versioned local adaptation of MinerU content contracts, independent of transport.

V1 and V2 are consumer renderings; middle/model artifacts are not interchangeable.
Every emitted block points to its original JSON entry. Unsupported or unreadable
content remains inspectable and produces a diagnostic instead of losing siblings.
"""
from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import re

NORMALIZATION_VERSION = "mineru-content-contract-v5-table-title-scope"
TEXT_TYPES = {"text", "paragraph", "header", "footer", "page_header", "page_footer",
              "aside_text", "page_aside_text", "page_footnote", "ref_text", "phonetic",
              "image_caption", "image_footnote", "table_caption", "table_footnote",
              "chart_caption", "chart_footnote", "code_caption", "code_footnote"}
TITLE_TYPES = {"title", "doc_title", "paragraph_title"}
FORMULA_TYPES = {"equation", "equation_interline", "interline_equation"}
VISUAL_TYPES = {"image", "chart", "seal"}
NAVIGATION_TYPES = {"page_number", "index"}
CONTENT_KEYS = ("text", "paragraph_content", "title_content", "math_content", "code_content",
                "algorithm_content", "item_content", "page_header_content", "page_footer_content",
                "page_number_content", "page_aside_text_content", "page_footnote_content",
                "seal_content", "content")


def _text(value):
    """Join textual spans only; metadata, coordinates and paths are not facts."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text(v) for v in value)
    if isinstance(value, dict):
        for key in CONTENT_KEYS:
            if key in value:
                return _text(value[key])
    return ""


def _strings(value):
    if isinstance(value, list):
        return [text for item in value if (text := _text(item)).strip()]
    text = _text(value)
    return [text] if text.strip() else []


def _payload(entry):
    return entry["content"] if isinstance(entry.get("content"), dict) else entry


def _body(entry):
    payload = _payload(entry)
    return _text(payload)


def _table_html(entry):
    p = _payload(entry)
    for key in ("table_body", "html", "content"):
        value = p.get(key)
        if isinstance(value, str) and re.search(r"<table\b|<tr\b", value, re.I):
            return value
        if isinstance(value, str) and '|' in value:
            from .table_structure import markdown_table_spans
            if len(list(markdown_table_spans(value))) == 1:
                return value
    return ""


class _CellText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells, self.current = [], None
    def handle_starttag(self, tag, attrs):
        if tag in {"td", "th"}:
            self.current = []
    def handle_data(self, text):
        if self.current is not None:
            self.current.append(text)
    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.current is not None:
            value = re.sub(r"\s+", "", "".join(self.current))
            if value:
                self.cells.append(value)
            self.current = None


def _cells(html):
    parser = _CellText(); parser.feed(html)
    return parser.cells


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _load_entries(directory):
    """Prefer the established V1 rendering; never concatenate duplicate formats."""
    for pattern, schema in (("*content_list.json", "content_list_v1"),
                            ("*content_list_v2.json", "content_list_v2")):
        paths = sorted(directory.rglob(pattern))
        if paths:
            if len(paths) != 1:
                raise ValueError(f"Ambiguous MinerU {schema}: expected one rendering, found {len(paths)}")
            path = paths[0]; data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError(f"Invalid MinerU {schema}: expected an array")
            entries = []
            if schema == "content_list_v2":
                for page, items in enumerate(data):
                    if not isinstance(items, list):
                        entries.append((items, page, f"/{page}"))
                        continue
                    entries.extend((item, page, f"/{page}/{i}") for i, item in enumerate(items))
            else:
                entries = [(item, item.get("page_idx") if isinstance(item, dict) else None, f"/{i}")
                           for i, item in enumerate(data)]
            return path, schema, entries
    # The hosted layout.json is documented as middle.json. Only postprocessed
    # para_blocks/blocks are used; preproc/model hypotheses are never mixed in.
    paths = (sorted(directory.rglob("middle_json.json")) or sorted(directory.rglob("*_middle.json"))
             or sorted(directory.rglob("layout.json")) or sorted(directory.rglob("structured_content.json")))
    if len(paths) != 1:
        raise ValueError("No unambiguous MinerU content-list or postprocessed middle artifact")
    path = paths[0]; data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("MinerU middle/structured content must be an object")
    if "pdf_info" in data:
        pages, key, schema = data["pdf_info"], "pdf_info", "middle_legacy"
    elif data.get("schema") == "docvortex.middle" and data.get("schema_version") == "2.0":
        pages, key, schema = data["pages"], "pages", "docvortex.middle:2.0"
    elif path.name == "structured_content.json" and "schema" not in data and isinstance(data.get("pages"), list):
        pages, key, schema = data["pages"], "pages", "structured_content"
    else:
        raise ValueError("Unsupported MinerU intermediate schema; raw model output is not a text rendering")
    entries = []
    for pi, page in enumerate(pages):
        if not isinstance(page, dict):
            entries.append((page, pi, f"/{key}/{pi}"))
            continue
        page_idx = page.get("page_idx", pi)
        block_key = "para_blocks" if key == "pdf_info" else "blocks"
        for bi, block in enumerate(page.get(block_key, [])):
            entries.append((_safe_middle_entry(block), page_idx, f"/{key}/{pi}/{block_key}/{bi}"))
        for bi, block in enumerate(page.get("discarded_blocks", [])):
            entries.append((_safe_middle_entry(block), page_idx, f"/{key}/{pi}/discarded_blocks/{bi}"))
    return path, schema, entries


def _safe_middle_entry(block):
    if not isinstance(block, dict):
        return block
    try:
        return _middle_entry(block)
    except (TypeError, ValueError, AttributeError, KeyError) as error:
        return {"type": "malformed_middle_block", "content": _text(block.get("content")),
                "_middle_raw": block, "_conversion_error": type(error).__name__}


def _middle_entry(block):
    """Adapt old line/span blocks and current typed children without mixing model data."""
    entry = {k: v for k, v in block.items() if k in {"type", "bbox", "angle", "anchor", "sub_type", "level", "text_level"}}
    kind = entry.get("type", "unknown")
    if not isinstance(kind, str):
        kind = entry["type"] = "invalid_type"
    spans = [span for node in _walk(block) for line in node.get("lines", []) for span in line.get("spans", [])]
    children = block.get("children", block.get("blocks", []))
    body = next((child for child in children if child.get("type") in {kind+"_body", "algorithm_body"}), {})
    if kind in {"table", "chart"}:
        html = _text(body.get("content")) or next((s["html"] for s in spans if isinstance(s.get("html"), str) and s["html"].strip()), "")
        entry["table_body" if kind == "table" else "content"] = html
    elif kind in {"list", "index"}:
        entry["list_items"] = [_text(child.get("content")) or "".join(_text(s) for l in child.get("lines", []) for s in l.get("spans", []))
                               for child in children]
    else:
        entry["text"] = (_text(block.get("content")) or _text(body.get("content"))
                         or "\n".join("".join(_text(s) for s in line.get("spans", [])) for line in block.get("lines", [])))
    for prefix in ("image", "table", "chart", "code", "algorithm"):
        for suffix in ("caption", "footnote"):
            entry[prefix+"_"+suffix] = ["".join(_text(s) for l in node.get("lines", []) for s in l.get("spans", []))
                                      or _text(node.get("content")) for node in _walk(block) if node.get("type") == prefix+"_"+suffix]
    path = body.get("image_path") or body.get("image_source", {}).get("path") or next((s.get("image_path") for s in spans if s.get("image_path")), None)
    if path:
        entry["img_path"] = path
    entry["_middle_raw"] = block
    return entry


def _continuations(directory, entries):
    """Prove deleted cross-page placeholders against the emitted merged table.

    An empty table alone is insufficient. Require a matching postprocessed
    lines_deleted block plus preprocessed cell content contained by a prior
    emitted table. Preserve the complete page/region provenance on that table.
    """
    layout_paths = list(directory.rglob("layout.json")) or list(directory.rglob("*_middle.json"))
    if len(layout_paths) != 1:
        return {}, {}
    try:
        data = json.loads(layout_paths[0].read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}, {}
    if not isinstance(data, dict):
        return {}, {}
    pages = {p["page_idx"]: p for p in data.get("pdf_info", [])
             if isinstance(p, dict) and isinstance(p.get("page_idx"), int)}
    targets, segments, emitted = {}, {}, []
    for index, (entry, page_idx, pointer) in enumerate(entries):
        if not isinstance(entry, dict) or entry.get("type") != "table" or not isinstance(page_idx, int):
            continue
        html = _table_html(entry)
        if html:
            emitted.append((index, page_idx, _cells(html)))
            continue
        page = pages.get(page_idx, {}); size = page.get("page_size")
        bbox = entry.get("bbox")
        if not (isinstance(size, list) and len(size) == 2 and all(isinstance(x, (int, float)) and x > 0 for x in size)
                and isinstance(bbox, list) and len(bbox) == 4 and all(isinstance(x, (int, float)) for x in bbox)):
            continue
        def matching(block):
            if not isinstance(block, dict):
                return False
            bbox = block.get("bbox")
            if (block.get("type") != "table" or not isinstance(bbox, list) or len(bbox) != 4
                    or not all(isinstance(x, (int, float)) for x in bbox)):
                return False
            normalized = [round(bbox[i] / size[i % 2] * 1000) for i in range(4)]
            return all(abs(a-b) <= 4 for a,b in zip(normalized, entry["bbox"]))
        para = [b for b in page.get("para_blocks", []) if matching(b)]
        pre = [b for b in page.get("preproc_blocks", []) if matching(b)]
        if len(para) != 1 or len(pre) != 1 or not any(n.get("lines_deleted") for n in _walk(para[0])):
            continue
        piece = next((n["html"] for n in _walk(pre[0]) if isinstance(n.get("html"), str) and n["html"].strip()), "")
        cells = _cells(piece)
        if not cells:
            continue
        # Cross-page merges can join a cell fragment to its predecessor; an
        # exact substring of an emitted cell is accepted only with the deleted
        # placeholder evidence above, never as a general semantic matcher.
        target = next((i for i,p,merged in reversed(emitted) if p is not None and p < page_idx
                       and all(any(cell in whole for whole in merged) for cell in cells)), None)
        if target is None:
            continue
        targets[index] = target
        segments.setdefault(target, []).append({"page_index": page_idx, "bbox": entry["bbox"],
            "parse_file": layout_paths[0].name, "content_json_pointer": pointer,
            "evidence": "matching middle para table lines_deleted and preproc cells in merged body"})
    return targets, segments


def normalization_diagnostics(blocks):
    return [{**d, "block_id": block["block_id"], "locator": block["locator"],
             "raw_entry_ref": block.get("raw_entry_ref")}
            for block in blocks for d in block.get("normalization", {}).get("diagnostics", [])]


def normalize_content(directory, artifact, out):
    directory = Path(directory)
    path, schema, entries = _load_entries(directory)
    continuations, merged_segments = _continuations(directory, entries)
    office = artifact["profile"]["extension"] in {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
    relative = str(path.relative_to(directory))
    for index, (raw, page, pointer) in enumerate(entries):
        entry = raw if isinstance(raw, dict) else {"type": "invalid_entry"}
        kind = entry.get("type", "unknown")
        if not isinstance(kind, str):
            kind = "invalid_type"
        loc = {"space": "parsed_office" if office else "page", "content_index": index,
               "parse_file": relative, "json_pointer": pointer, "mineru_type": kind,
               "mineru_schema": schema}
        if page is not None:
            loc["page_index"] = page
        if "bbox" in entry:
            loc.update(bbox=entry["bbox"], bbox_space="mineru_content_list_0_1000" if schema.startswith("content_list") else "mineru_middle")
        for key in ("anchor", "angle", "sub_type"):
            if key in entry:
                loc[key] = entry[key]
        if index in merged_segments:
            loc["merged_segments"] = merged_segments[index]
            loc["page_indices"] = sorted({page, *(s["page_index"] for s in merged_segments[index])})
            loc["page_index_role"] = "merged_table_anchor"
        start = len(out.items)
        diagnostics = []
        def diagnostic(code, message, *, review=True):
            diagnostics.append({"code": code, "message": message, "severity": "warning" if review else "info", "requires_review": review})
        def retained(kind_name="unparsed", role=None):
            block = out.add(kind_name, "", loc, role=role)
            block["extraction_eligible"] = False
            block["raw_entry"] = raw.get("_middle_raw", raw) if isinstance(raw, dict) else raw
            return block
        p = _payload(entry)
        try:
            if index in continuations:
                loc["merged_into_content_index"] = continuations[index]
                retained("metadata", "merged_table_continuation")
                diagnostic("merged_table_continuation", "Deleted continuation is present in an earlier merged table body; not duplicated", review=False)
            elif kind in NAVIGATION_TYPES:
                b = out.add("metadata", _body(entry) or "\n".join(_strings(p.get("list_items"))), loc, role="document_navigation")
                b["extraction_eligible"] = False
            elif kind in TEXT_TYPES or kind in TITLE_TYPES:
                text = _body(entry)
                if text.strip():
                    level = entry.get("text_level") or p.get("level") or (1 if kind in TITLE_TYPES else None)
                    if level is not None and (not isinstance(level, int) or level < 1):
                        diagnostic("invalid_heading_level", "Invalid heading level; readable text retained without inferred hierarchy")
                        level = None
                    out.text("heading" if level else "text", text, loc, level=level,
                             role=kind if kind not in {"text", "paragraph", "title"} else None)
                else:
                    retained("metadata", "empty_text")
                    declared_empty = any(key in p and ((isinstance(p[key], str) and not p[key].strip()) or p[key] == []) for key in CONTENT_KEYS)
                    diagnostic("empty_text", "Textual element declares empty text" if declared_empty else "Textual element has no readable content", review=not declared_empty)
            elif kind in FORMULA_TYPES:
                text = _body(entry)
                if text.strip():
                    out.text("formula", text, {**loc, "formula_kind": p.get("math_type", entry.get("text_format", "recognized_math"))})
                else:
                    retained("image", "requires_visual_read"); diagnostic("unreadable_formula", "Formula has no recognized text")
            elif kind in {"list", "index"}:
                items = p.get("list_items", [])
                if not isinstance(items, list):
                    raise TypeError("list_items must be an array")
                for li, item in enumerate(items):
                    text = _text(item)
                    if text.strip():
                        out.text("list", text, {**loc, "list_index": li}, role="reference_item" if p.get("list_type") == "reference_list" or entry.get("sub_type") == "ref_text" else None)
                    elif item:
                        unreadable = retained()
                        unreadable["locator"] = {**loc, "list_index": li}
                        diagnostic("unreadable_list_item", "Nonempty list item has no recognized text; raw item retained")
                if len(out.items) == start:
                    retained(); diagnostic("unreadable_list", "List has no readable items")
            elif kind in {"table", *VISUAL_TYPES}:
                html = _table_html(entry)
                prefix = kind
                captions, notes = _strings(p.get(prefix+"_caption")), _strings(p.get(prefix+"_footnote"))
                if html:
                    before, headings = len(out.items), list(out.headings)
                    try:
                        out.table(html, loc, captions, notes)
                        if re.search(r"<img\b", html, re.I):
                            diagnostic("embedded_visual_content", "Table contains image content requiring visual reading")
                    except (ValueError, TypeError) as error:
                        del out.items[before:]; out.headings = headings
                        diagnostic("malformed_table", f"Table structure unreadable: {type(error).__name__}; retained readable text and raw entry")
                        plain = " | ".join(_cells(html))
                        if plain:
                            out.text("text", plain, loc, role="unstructured_table_text")
                        else:
                            retained("unparsed", "requires_visual_read")
                else:
                    text = _body(entry)
                    if kind != "table" and text.strip():
                        out.text("text", text, loc, role=kind+"_content")
                    else:
                        block = retained("image", "requires_visual_read")
                        block["asset_path"] = entry.get("img_path") or (p.get("image_source") or {}).get("path")
                        diagnostic("unreadable_table" if kind == "table" else "visual_content_not_read",
                                   "No structured text body; original image/raw element remains available")
                    for ni, text in enumerate(captions + notes):
                        out.text("text", text, {**loc, "annotation_index": ni}, role=kind+"_annotation")
            elif kind in {"code", "algorithm"}:
                prefix = "algorithm" if kind == "algorithm" else "code"
                text = _body(entry) or _text(entry.get("code_body"))
                for ni, text_note in enumerate(_strings(p.get(prefix+"_caption"))):
                    out.text("text", text_note, {**loc, "caption_index": ni}, role=prefix+"_caption")
                if text.strip():
                    out.text("code", text, {**loc, "language": p.get("code_language", entry.get("language"))}, role=prefix)
                else:
                    retained(); diagnostic("unreadable_code", "Code or algorithm body has no readable content")
                for ni, text_note in enumerate(_strings(p.get(prefix+"_footnote"))):
                    out.text("text", text_note, {**loc, "footnote_index": ni}, role=prefix+"_footnote")
            else:
                text = _body(entry)
                if text.strip():
                    out.text("text", text, loc, role="unsupported_content_text")
                else:
                    retained()
                diagnostic("unsupported_type", f"Unmapped MinerU type {kind!r}; raw entry retained")
        except (TypeError, ValueError, AttributeError, KeyError) as error:
            retained()
            diagnostic("malformed_element", f"Element contract error: {type(error).__name__}; raw element retained")
        for block in out.items[start:]:
            block["normalization_version"] = NORMALIZATION_VERSION
            block["raw_entry_ref"] = {"parse_file": relative, "json_pointer": pointer}
        if diagnostics:
            block = out.items[start]
            block["normalization"] = {"diagnostics": diagnostics}
            block["raw_entry"] = raw.get("_middle_raw", raw) if isinstance(raw, dict) else raw
    return out.items

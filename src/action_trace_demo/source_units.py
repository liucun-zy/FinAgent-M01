"""Small source-scoped extraction packets over existing parsed evidence.

The model reads one representation of an element. Stable source IDs, full
locators and the short-anchor translation stay on the program side. This
follows the element/context separation used by Docling and Unstructured;
parsing and evidence storage remain the existing M01 implementation.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import re

from .evidence_windows import evidence_refs, token_estimate


SOURCE_UNIT_VERSION = "source-elements-v5-table-title-scope"


def _id(prefix, *parts):
    return prefix + sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def _span_ref(block, start, end):
    """An exact slice of parser text, retaining the original block/locator."""
    return {"evidence_ref_id": _id("EV_S_", block["block_id"], start, end),
            "source_version_id": block["source_version_id"], "block_id": block["block_id"],
            "parent_evidence_ref_id": "EV_" + block["block_id"],
            "locator": {**block["locator"], "block_char_start": start, "block_char_end": end},
            "raw_content": block["content"][start:end], "role": "span"}


def _sentence_spans(text, max_chars):
    """Sentence/line boundaries with exact character offsets, then bounded cuts."""
    begin = 0
    boundaries = [m.end() for m in re.finditer(r"[。！？!?][\"'”’」』]*|\.(?=\s|$)|\n+", text)]
    if not boundaries or boundaries[-1] != len(text):
        boundaries.append(len(text))
    for boundary in boundaries:
        start, end = begin, boundary
        begin = boundary
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        while start < end:
            cut = min(end, start + max_chars)
            # For overlong sentences prefer a nearby clause/word boundary;
            # either choice retains the exact source range.
            if cut < end:
                candidates = list(re.finditer(r"[,，;；:：\s]", text[start:cut]))
                if candidates and candidates[-1].end() >= max_chars // 2:
                    cut = start + candidates[-1].end()
            yield start, cut
            start = cut


def _unique(refs):
    return list({ref["evidence_ref_id"]: ref for ref in refs}.values())


def _block_context(block):
    """Headers are visible as cells, never a second copy of HTML markup."""
    cells = block.get("evidence_refs", [])
    if block["kind"] == "table" and cells:
        return cells
    return [_span_ref(block, 0, len(block["content"]))]


def _unit(block, primary, contexts, *, kind, spans=(), fields=()):
    identity = [r["evidence_ref_id"] for r in primary]
    uid = _id("SU_", SOURCE_UNIT_VERSION, block["source_version_id"], identity)
    contexts = [r for r in _unique(contexts) if r["evidence_ref_id"] not in identity]
    refs = _unique([*primary, *contexts])
    main = [block["block_id"]]
    context_blocks = list(dict.fromkeys(r["block_id"] for r in contexts if r["block_id"] not in main))
    return {"unit_id": uid, "source_version_id": block["source_version_id"], "kind": kind,
            "primary_spans": list(spans), "primary_ref_ids": identity,
            "context_ref_ids": [r["evidence_ref_id"] for r in contexts],
            "evidence_ref_ids": [r["evidence_ref_id"] for r in refs], "evidence_refs": refs,
            "fields": list(fields), "order_index": block["order_index"],
            "locator": deepcopy(block['locator']),
            "table_context": deepcopy(block.get('table_context', {})),
            "table_note_bindings": deepcopy(block.get('table_note_bindings', [])),
            "window": {"window_id": uid, "source_version_id": block["source_version_id"],
                       "main_block_ids": main, "context_block_ids": context_blocks,
                       "available_context_refs": [], "primary_spans": list(spans),
                       "token_estimate": token_estimate([r["raw_content"] for r in refs])}}


def build_source_units(blocks, max_unit_chars=1800, context_chars=1200):
    """Return stable sentence, table-row and record units without source mixing.

    Required structural context (titles, headers, captions, notes) is retained.
    The context limit applies to optional adjacent prose only. Request packing
    subsequently checks the actual rendered packet against the transport limit.
    """
    if max_unit_chars < 1:
        raise ValueError("max_unit_chars must be positive")
    by_id = {b["block_id"]: b for b in blocks}
    by_source = {}
    for block in blocks:
        by_source.setdefault(block["source_version_id"], []).append(block)
    units = []
    for source_blocks in by_source.values():
        source_blocks = sorted(source_blocks, key=lambda b: b["order_index"])
        for position, block in enumerate(source_blocks):
            if block.get("structural_role") == "table_header" or not block.get("extraction_eligible", True):
                continue
            contexts = []
            # A mixed row can carry its own row header, not just data cells.
            contexts.extend(r for r in block.get("evidence_refs", []) if r.get("role") == "header")
            for bid in block.get("context_refs", []):
                context = by_id.get(bid)
                if context and context["source_version_id"] == block["source_version_id"]:
                    contexts.extend(_block_context(context))
            for binding in block.get('table_note_bindings', []):
                contexts.extend(_block_context(by_id[binding['block_id']]))
            required = {r["block_id"] for r in contexts}
            used = 0
            # Adjacent passages retain a following qualifier or antecedent,
            # without pulling another table or a different section into a row.
            for neighbor_position in (position - 1, position + 1):
                if not 0 <= neighbor_position < len(source_blocks):
                    continue
                neighbor = source_blocks[neighbor_position]
                if not neighbor.get("extraction_eligible", True) or neighbor["block_id"] in required or neighbor["kind"] in {"table", "record"}:
                    continue
                if neighbor_position > position and neighbor["kind"] == "heading":
                    continue
                if neighbor.get("parent_block_id") != block.get("parent_block_id") and neighbor["kind"] != "heading":
                    continue
                if used + len(neighbor["content"]) <= context_chars:
                    contexts.extend(_block_context(neighbor))
                    used += len(neighbor["content"])
            original = evidence_refs([block])
            if block["kind"] in {"table", "record"}:
                field_refs = [r for r in original if r.get("role") in {"cell", "field"}]
                kind = "table_row" if block["kind"] == "table" else "record"
                # Rows and records stay atomic: splitting fields would discard
                # the structural identity needed to interpret those fields.
                # The request packer explicitly reports an oversized atomic
                # unit instead of silently dropping cells or record fields.
                primary = field_refs or [original[0]]
                unit = _unit(block, primary, contexts, kind=kind, fields=field_refs,
                             spans=[{"block_id": block["block_id"], "start": 0, "end": len(block["content"])}])
                unit["parent_ref"] = original[0]
                units.append(unit)
                continue
            spans = list(_sentence_spans(block["content"], max_unit_chars))
            refs = [_span_ref(block, a, z) for a, z in spans]
            for i, ((start, end), ref) in enumerate(zip(spans, refs)):
                neighbors = [refs[j] for j in (i - 1, i + 1) if 0 <= j < len(refs)
                             and len(refs[j]["raw_content"]) <= context_chars]
                unit = _unit(block, [ref], [*contexts, *neighbors],
                    kind="heading" if block["kind"] == "heading" else "passage",
                    spans=[{"block_id": block["block_id"], "start": start, "end": end}])
                if block.get("structural_role") == "requires_visual_read":
                    unit["unreadable_reason"] = "visual_content_requires_image_read"
                units.append(unit)
    return units


def render_packet(units):
    """Bind packet-local short anchors to original evidence, rendering it once."""
    if len({u["source_version_id"] for u in units}) > 1:
        raise ValueError("A source packet cannot mix source versions")
    refs = {r["evidence_ref_id"]: r for u in units for r in u["evidence_refs"]}
    anchors, primary_aliases, rendered_units, internal = {}, {}, [], {}
    # Reserve primary aliases before rendering context, so an adjacent unit
    # appears once and can be referenced directly as context by its neighbor.
    for index, unit in enumerate(units, 1):
        short = f"U{index}"
        fields = unit.get("fields", [])
        aliases = []
        if fields:
            for column, ref in enumerate(fields, 1):
                alias = f"{short}.{'C' if unit['kind'] == 'table_row' else 'F'}{column}"
                anchors[alias] = ref
                primary_aliases.setdefault(ref["evidence_ref_id"], alias)
                aliases.append(alias)
            anchors[short] = unit["parent_ref"]
        else:
            ref = refs[unit["primary_ref_ids"][0]]
            anchors[short] = ref
            primary_aliases.setdefault(ref["evidence_ref_id"], short)
            aliases.append(short)
        internal[short] = {**deepcopy(unit), "primary_anchor_ids": aliases, "short_id": short}
    context, context_aliases = [], {}
    def context_alias(rid):
        if rid in primary_aliases:
            return primary_aliases[rid]
        if rid not in context_aliases:
            alias = f"A{len(context_aliases) + 1}"
            context_aliases[rid] = alias
            ref = refs[rid]
            anchors[alias] = ref
            entry = {"id": alias, "text": ref["raw_content"]}
            if ref.get('locator',{}).get('header_candidate'):
                entry.update(role='header_candidate', basis='untyped_leading_row', status='unconfirmed')
            context.append(entry)
        return context_aliases[rid]
    for short, unit in internal.items():
        item = {"id": short, "kind": unit["kind"]}
        fields = unit.get("fields", [])
        if fields:
            rendered = []
            for alias, ref in zip(unit["primary_anchor_ids"], fields):
                field = {"id": alias, "text": ref["raw_content"]}
                if unit["kind"] == "record":
                    field["path"] = ref["locator"]["json_pointer"]
                else:
                    loc = ref["locator"]
                    headers = [rid for rid in loc['header_ref_ids'] if rid in unit['context_ref_ids']] if 'header_ref_ids' in loc else [rid for rid in unit["context_ref_ids"]
                        if refs[rid].get("role") == "header"
                        and refs[rid]["locator"].get("table_id") == loc.get("table_id")
                        and refs[rid]["locator"].get("column_index", -1) <= loc["column_index"]
                        < refs[rid]["locator"].get("column_index", -1) + refs[rid]["locator"].get("colspan", 1)]
                    field["headers"] = [context_alias(rid) for rid in headers]
                    candidates = [rid for rid in unit['context_ref_ids'] if rid in loc.get('candidate_header_ref_ids', [])]
                    if candidates:
                        field['header_candidates'] = [context_alias(rid) for rid in candidates]
                rendered.append(field)
            item["cells" if unit["kind"] == "table_row" else "fields"] = rendered
        else:
            item["text"] = anchors[short]["raw_content"]
        item["context"] = [context_alias(rid) for rid in unit["context_ref_ids"]
                           if refs[rid].get("role") != "header"]
        if unit.get('table_note_bindings'):
            item['table_notes'] = [{k: v for k, v in binding.items() if k not in {'block_id', 'logical_table_id'}} |
                {'refs': [context_alias(rid) for rid in unit['context_ref_ids']
                          if refs[rid]['block_id'] == binding['block_id']]}
                for binding in unit['table_note_bindings']]
        if unit.get('table_context', {}).get('link_path'):
            item['table_continuation'] = {k: unit['table_context'][k] for k in ('status', 'column_mapping')}
        if unit.get("unreadable_reason"):
            item["unreadable_reason"] = unit["unreadable_reason"]
        unit["context_anchor_ids"] = list(dict.fromkeys([*item["context"], *[
            header for field in item.get("cells", []) for header in field["headers"] + field.get('header_candidates', [])],
            *[a for note in item.get('table_notes', []) for a in note['refs']]]))
        rendered_units.append(item)
    # Include primary row/record parents for persistence without exposing their
    # duplicate HTML/JSON bytes to the model.
    all_refs = _unique([*refs.values(), *anchors.values()])
    for unit in internal.values():
        unit["evidence_ref_ids"] = list(dict.fromkeys([*unit["evidence_ref_ids"],
            *[anchors[a]["evidence_ref_id"] for a in [unit["short_id"], *unit["primary_anchor_ids"],
                                                     *unit["context_anchor_ids"]]]]))
        by_ref_id = {r["evidence_ref_id"]: r for r in all_refs}
        unit["evidence_refs"] = [by_ref_id[rid] for rid in unit["evidence_ref_ids"]]
    return {"version": SOURCE_UNIT_VERSION, "source_version_id": units[0]["source_version_id"] if units else None,
            "model_input": {"context": context, "units": rendered_units}, "anchors": anchors,
            "units": internal, "evidence_refs": all_refs,
            "windows": [u["window"] for u in units]}


def split_unit(unit):
    """Bisect one passage's exact span; table rows/record fields remain atomic."""
    if unit["kind"] not in {"passage", "heading"} or len(unit["primary_ref_ids"]) != 1:
        return []
    refs = {r["evidence_ref_id"]: r for r in unit["evidence_refs"]}
    ref = refs[unit["primary_ref_ids"][0]]
    text = ref["raw_content"]
    if len(text) < 2:
        return []
    midpoint = len(text) // 2
    boundaries = [m.end() for m in re.finditer(r"[。！？.!?,，;；\s]", text)]
    if boundaries:
        choices = [p for p in boundaries if 0 < p < len(text)]
        if choices:
            midpoint = min(choices, key=lambda n: abs(n - midpoint))
    children = []
    base = ref["locator"].get("block_char_start", 0)
    for start, end in ((0, midpoint), (midpoint, len(text))):
        child_ref = {**deepcopy(ref), "evidence_ref_id": _id("EV_S_", ref["block_id"], base + start, base + end),
            "raw_content": text[start:end], "locator": {**ref["locator"],
                "block_char_start": base + start, "block_char_end": base + end}}
        child = deepcopy(unit)
        child["unit_id"] = _id("SU_", SOURCE_UNIT_VERSION, unit["source_version_id"], [child_ref["evidence_ref_id"]])
        child["primary_spans"] = [{"block_id": ref["block_id"], "start": base + start, "end": base + end}]
        child["primary_ref_ids"] = [child_ref["evidence_ref_id"]]
        child["evidence_refs"] = [child_ref, *[r for r in unit["evidence_refs"] if r["evidence_ref_id"] != ref["evidence_ref_id"]]]
        child["evidence_ref_ids"] = [r["evidence_ref_id"] for r in child["evidence_refs"]]
        child["window"] = {**child["window"], "window_id": child["unit_id"], "primary_spans": child["primary_spans"],
            "token_estimate": token_estimate([r["raw_content"] for r in child["evidence_refs"]])}
        child["parent_unit_id"] = unit["unit_id"]
        children.append(child)
    return children

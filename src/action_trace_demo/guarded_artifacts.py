"""Versioned native documents. Only this operator can materialize a business artifact.

The action DAG is the authority. Content-addressed blobs retain every version.
A durable intent bridges the interval between a filesystem rename and creation
of the new head. Only trusted task setup can seed a checkpoint or bind a purpose.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from typing import Any
from html import escape

from .artifact_proofs import (admissible_text, verify_native_assertions, witnessed_fraction, literal_matches, direct_context_match, binding_context, calculation_matches, is_business_claim, measured_assertion, mismatched_binding, proof_scope, supporting_quote)
from .native_annotations import extract_native_annotations
from .pdf_table_witness import pdf_table_witness
from .typed_table_witness import typed_table_witness
from .native_grounding import (all_parts, amounts_match, claim_quantities, document_amount_unit,
                              document_quantities, entity_anchor, metrics, quantities, same_calendar_date, semantic_labels)
from .temporal_dag import rank_probes
from .tools import write_artifact
from .workbook_evidence import recalculate_workbook

SUPPORTED = {".xlsx", ".docx", ".pptx", ".pdf", ".txt", ".md"}
MAX_BYTES = 32 * 1024 * 1024
MAX_SLOTS = 100_000


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _path(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Expected a relative artifact path")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(x.startswith(".") for x in path.parts):
        raise ValueError("Expected a canonical non-hidden relative artifact path")
    if path.suffix.lower() not in SUPPORTED:
        raise ValueError("Unsupported artifact format")
    return value


def _scalar(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _decimal(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def _equal(left, right):
    if type(left) is type(right) and left == right:
        return True
    a, b = _decimal(left), _decimal(right)
    return a is not None and b is not None and a == b


def _matches(actual, wanted, mode):
    if actual is None:
        return False
    if mode == "equals":
        return _equal(actual, wanted) or same_calendar_date(wanted, actual)
    if mode != "contains" or not isinstance(actual, str):
        return False
    if all_parts(wanted,actual):
        return True
    number = _decimal(wanted)
    if number is not None and number in _amounts(actual):
        return True
    # Distinguish "Case 1" from "Case 10", or 35 from 350.
    pattern = r"(?<!\w)" + re.escape(str(wanted)) + r"(?!\w)"
    return bool(re.search(pattern, actual, re.I)) or same_calendar_date(wanted, actual)


def _slots(path, cached=False):
    """Business locators into bytes, including native notes and hidden stories."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("Expected a regular native file no larger than 32 MiB")
    suffix, slots = path.suffix.lower(), {}
    if suffix == ".xlsx":
        from openpyxl import load_workbook
        book = load_workbook(path, data_only=cached, read_only=True)
        try:
            for sheet in book:
                if sheet.max_row and sheet.max_column and sheet.max_row * sheet.max_column > MAX_SLOTS:
                    raise ValueError("Worksheet exceeds inspection limit")
                for row in sheet:
                    for cell in row:
                        if cell.value is not None:
                            slots[f"{sheet.title}!{cell.coordinate}"] = _scalar(cell.value)
        finally:
            book.close()
    elif suffix == ".docx":
        from docx import Document
        doc = Document(path)
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        # Keep existing paragraph/table locators while preserving their true
        # interleaved order in the DOCX body (signing text versus annex).
        para_number = table_number = 0
        for element in doc.element.body:
            if element.tag.endswith("}p"):
                para_number += 1
                text = Paragraph(element, doc).text
                if text:
                    slots[f"p:{para_number}"] = text
            elif element.tag.endswith("}tbl"):
                table_number += 1
                for ri, row in enumerate(Table(element, doc).rows, 1):
                    for ci, cell in enumerate(row.cells, 1):
                        if cell.text:
                            slots[f"t:{table_number}:{ri}:{ci}"] = cell.text
    elif suffix == ".pptx":
        from pptx import Presentation
        for si, slide in enumerate(Presentation(path).slides, 1):
            for i, shape in enumerate(slide.shapes, 1):
                if shape.has_text_frame and shape.text:
                    slots[f"slide:{si}:shape:{i}"] = shape.text
                if shape.has_table:
                    for ri, row in enumerate(shape.table.rows, 1):
                        for ci, cell in enumerate(row.cells, 1):
                            if cell.text:
                                slots[f"slide:{si}:shape:{i}:cell:{ri}:{ci}"] = cell.text
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                slots[f"slide:{si}:notes"] = slide.notes_slide.notes_text_frame.text
    elif suffix == ".pdf":
        from pypdf import PdfReader
        for i, page in enumerate(PdfReader(path).pages, 1):
            slots[f"page:{i}"] = page.extract_text() or ""
    elif suffix in {".txt", ".md"}:
        slots = {f"line:{i}": line for i, line in enumerate(path.read_text().splitlines(), 1)}
    else:
        raise ValueError("Unsupported artifact format")
    if suffix in {".docx", ".pptx", ".xlsx", ".pdf"}:
        annotations = extract_native_annotations(path)
        if not annotations["complete"]:
            raise ValueError("Native annotation inspection incomplete")
        for record in annotations["entries"]:
            if record["text"] and record["kind"] not in ("presenter_notes", "slide_text_including_hidden_content"):
                slots["native:" + record["part"]] = record["text"]
    if len(slots) > MAX_SLOTS:
        raise ValueError("Native file exceeds inspection limit")
    return slots


def _material_slots(slots):
    # Numbers, long prose and status assertions need located claim evidence.
    # Formula outputs are verified by recalculation; literal formula operands
    # remain part of the formula's evidence or derivation.
    statuses = re.compile(r"\b(?:approved|waived|pending|confirmed|rejected|selected|closed|signed|executed|withdrawn)\b", re.I)
    return {k for k, v in slots.items() if (isinstance(v, (float, int)) or
            (isinstance(v, str) and not v.startswith("=") and
             (len(v.strip()) >= 50 or re.search(r"\d", v) or statuses.search(v))))}


def _amounts(value):
    return {q.value for q in quantities(value)}


def _slot(slots, locator):
    if locator in slots:
        return slots[locator]
    m=re.fullmatch(r"(page:\d+)#line:([1-9]\d*)",locator)
    if m and m[1] in slots:
        lines=str(slots[m[1]]).splitlines()
        line=int(m[2])-1
        if line<len(lines):
            return lines[line]
    return None


def _financial_support(actual,wanted,claim,mode,document_unit=None, *, rule=None, context=""):
    if rule and isinstance(actual,str) and actual.startswith("="):
        return literal_matches(actual,wanted,rule,claim,context)
    if rule and rule.get("calculation_receipt"):
        matched = calculation_matches(actual,wanted,rule["unit"],document_unit,context)
        return bool(matched)
    number = _decimal(wanted)
    if number is not None:
        matched=direct_context_match(actual,wanted,claim,rule,document_unit,context)
        if matched is not None:
            return bool(matched)
    if number is None or not isinstance(actual,str):
        return _matches(actual,wanted,mode)
    if mode == "equals" and not re.fullmatch(
            r"\s*(?:(?:US\$|USD|EUR|GBP|CNY|RMB|[$€£])\s*)?[-+]?[\d,]+(?:\.\d+)?"
            r"\s*(?:%|bps?|x|thousands?|millions?|billions?|mm|bn)?\s*",actual,re.I):
        return _matches(actual,wanted,mode)
    if witnessed_fraction(wanted,claim):
        return any(q.dimension=="rate" and q.canonical==number for q in document_quantities(actual,document_unit))
    source_values=[q for q in claim_quantities(claim) if q.value==number]
    native_values=document_quantities(actual,document_unit)
    if source_values and native_values:
        return any(amounts_match(n,s) for n in native_values for s in source_values)
    return _matches(actual,wanted,mode)


def _supported_amounts(raw, expected, claim, document_unit=None, *, rule=None, context=""):
    if rule and rule.get("calculation_receipt"):
        return calculation_matches(raw,expected,rule["unit"],document_unit,context)
    # One field binding authorizes its selected scalar, not every other
    # number occurring in the same pricing row or source paragraph.
    covered = _amounts(expected)
    number = _decimal(expected)
    if number is not None:
        matched=direct_context_match(raw,expected,claim,rule,document_unit,context)
        if matched is not None:
            return matched
    if number is not None:
        anchors = [q for q in claim_quantities(claim)
                   if q.value == number or (witnessed_fraction(expected,claim) and q.dimension=="rate" and q.canonical==number)]
        covered.update(q.value for q in document_quantities(raw,document_unit) if any(amounts_match(q,s) for s in anchors))
    return covered


def _anchored(value, claim):
    quote = claim["quote"]
    if witnessed_fraction(value,claim) or _matches(quote, value, "contains"):
        return True
    number = _decimal(value)
    if number is None:
        return False
    if number in _amounts(quote):
        return True
    # Normalize only an explicitly specified denomination, not arbitrary factors.
    field = str(claim.get("field","")).casefold()
    if re.search(r"usd[_ ]?(?:000|k)|k[_ ]?usd|thousand",field):
        multipliers = {"billion": 1_000_000, "bn": 1_000_000, "million": 1_000, "mm": 1_000}
        return any((_decimal(a) or 0) * multipliers[b.casefold()] == number
                   for a,b in re.findall(r"(?<!\w)(\d[\d,]*(?:\.\d+)?)\s*(billion|bn|million|mm)\b",quote,re.I))
    return False


def _patch(path, changes):
    """Touch just the named native slots, checking every before-image."""
    if not changes:
        return set()
    seen, before = set(), _slots(path)
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".pptx"}:
        doc = {}
    elif suffix == ".docx":
        from docx import Document
        doc = Document(path)
    elif suffix in {".txt", ".md"}:
        doc = path.read_text().splitlines(keepends=True)
    else:
        raise ValueError(f"Local patch for {suffix} requires a natively supported locator")
    for edit in changes:
        key = edit["locator"]
        empty_cell = (suffix == ".xlsx" and key not in before and edit.get("before") is None
                      and isinstance(key,str) and re.fullmatch(r"[^!]+![A-Z]{1,3}[1-9]\d{0,6}",key))
        if key in seen or (key not in before and not empty_cell) or "before" not in edit or not _equal(before.get(key), edit["before"]):
            raise ValueError(f"Stale, absent, or repeated slot: {key}")
        seen.add(key)
        old = before.get(key)
        if "old_text" in edit:
            needle = edit["old_text"]
            if not isinstance(old, str) or not isinstance(needle, str) or not needle or old.count(needle) != 1:
                raise ValueError(f"Substring is absent or ambiguous at {key}")
            value = old.replace(needle, str(edit["new_text"]), 1)
        else:
            value = edit["after"]
        if isinstance(value, (dict, list)) or value is None:
            raise ValueError("Artifact slots require scalar replacements")
        if suffix in {".xlsx", ".pptx"}:
            doc[key] = value
        elif suffix == ".docx":
            parts = key.split(":")
            if parts[0] == "p" and len(parts) == 2:
                paragraph = doc.paragraphs[int(parts[1]) - 1]
            elif parts[0] == "t" and len(parts) == 4:
                t, r, c = (int(x) - 1 for x in parts[1:])
                cell = doc.tables[t].rows[r].cells[c]
                if len(cell.paragraphs) != 1:
                    raise ValueError("Multi-paragraph table cell requires separate edits")
                paragraph = cell.paragraphs[0]
            else:
                raise ValueError(f"Unsupported DOCX edit locator {key}")
            # Keep untouched runs/styles. Multi-run replacement is supported
            # only if it fits a single run, avoiding wholesale reformatting.
            runs = paragraph.runs
            if len(runs) == 1 and runs[0].text == old:
                runs[0].text = str(value)
            elif "old_text" in edit and sum(run.text.count(edit["old_text"]) for run in runs) == 1:
                run = next(run for run in runs if edit["old_text"] in run.text)
                run.text = run.text.replace(edit["old_text"], str(edit["new_text"]), 1)
            else:
                raise ValueError(f"Text crosses formatting runs at {key}")
        else:
            i = int(key.split(":")[1]) - 1
            line = doc[i]
            doc[i] = str(value) + line[len(line.rstrip("\r\n")):]
    if suffix == ".xlsx":
        _rewrite_xlsx(path, doc)
    elif suffix == ".pptx":
        from .tools import patch_presentation_slots
        patch_presentation_slots(path, doc)
    elif suffix in {".txt", ".md"}:
        path.write_text("".join(doc))
    else:
        doc.save(path)
    after = _slots(path)
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    if changed - seen:
        raise ValueError(f"Patch would change protected slots: {sorted(changed - seen)[:10]}")
    if suffix == ".xlsx" and any(not _equal(after.get(key),value) for key,value in doc.items()):
        raise ValueError("Patched workbook does not match its requested after-images")
    if suffix == ".pptx" and any(after.get(key, "") != str(value) for key,value in doc.items()):
        raise ValueError("Patched presentation does not match requested after-images")
    return seen


def _schema(name, description, props, required=()):
    return {"name": name, "description": description, "parameters": {"type": "object", "properties": props,
            "required": list(required), "additionalProperties": False}}


_FORMATS = {
    ".pdf": {"example":{"title":"Report", "pages":[{"text":"Financial conclusion and source-supported amounts."}]},
             "alternative":"Use sections with heading, paragraphs (list of strings), and optional table (row arrays).",
             "locator":"page:1 or page:1#line:12 (actual 1-based rendered page/text line; artifact_read reveals pagination)"},
    ".docx":{"example":{"title":"Memo","sections":[{"heading":"Analysis","paragraphs":["Evidence-based conclusion."],
             "table":[["Measure","Amount"],["Price","35"]]}]}, "locator":"p:1 (includes title and headings), t:1:2:2 (table,row,column)"},
    ".xlsx":{"example":{"sheets":[{"name":"Inputs","rows":[["Measure","Price"],["Senior spread",35]],
              "cells":{"B3":"=B2/10000"}}]}, "locator":"Inputs!B2 (sheet name and cell)"},
    ".pptx":{"example":{"slides":[{"title":"Terms","bullets":["Current terms and evidence."],"notes":"Business notes."}]},
             "locator":"slide:1:shape:1 (title), slide:1:shape:2 (body), slide:1:notes"},
    ".txt":{"example":{"text":"Evidence-based conclusion."},"locator":"line:1"},
    ".md":{"example":{"text":"Evidence-based conclusion."},"locator":"line:1"},
}


def _render_native(path, content):
    suffix = path.suffix.lower()
    if not isinstance(content,dict):
        raise ValueError(f"content must be an object; expected {_FORMATS[suffix]}")
    if suffix == ".pdf" and "pages" in content:
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer
        pages = content["pages"]
        if not isinstance(pages,list) or not pages:
            raise ValueError("PDF content.pages must be a nonempty array of {text: ...}")
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        flow = []
        for i,page in enumerate(pages):
            value = page["text"] if isinstance(page,dict) and "text" in page else page
            if not isinstance(value,str) or not value.strip():
                raise ValueError("Every PDF page requires nonempty text")
            if i:
                flow.append(PageBreak())
            for line in value.splitlines():
                if line.strip():
                    font = "STSong-Light" if re.search(r"[\u3400-\u9fff]",line) else "Helvetica"
                    flow.append(Paragraph(escape(line),ParagraphStyle("body",fontName=font,fontSize=10,leading=13)))
                else:
                    flow.append(Spacer(1,5))
        SimpleDocTemplate(str(path),rightMargin=42,leftMargin=42,topMargin=42,bottomMargin=42).build(flow)
        return
    if suffix in {".txt",".md"}:
        if not isinstance(content.get("text"),str) or not content["text"].strip():
            raise ValueError("Text/Markdown content requires nonempty text")
        path.write_text(content["text"])
        return
    key = {".pdf":"sections",".docx":"sections",".xlsx":"sheets",".pptx":"slides"}[suffix]
    if not isinstance(content.get(key),list) or not content[key]:
        raise ValueError(f"{suffix} content requires nonempty {key}. Example: {_FORMATS[suffix]['example']}")
    if key == "sections" and any(not isinstance(s,dict) or not set(s)&{"heading","paragraphs","table"} for s in content[key]):
        raise ValueError("Each section requires heading, paragraphs, or table")
    write_artifact(path,content)


def _append_native(path, additions):
    if not additions:
        return set()
    before = _slots(path)
    if path.suffix.lower() == ".docx":
        from docx import Document
        document = Document(path)
        for item in additions:
            if set(item) == {"paragraph"} and isinstance(item["paragraph"], str) and item["paragraph"].strip():
                document.add_paragraph(item["paragraph"])
            elif set(item) == {"table"}:
                rows = item["table"]
                if (not isinstance(rows, list) or not rows or len(rows)>500 or
                        not isinstance(rows[0],list) or not 1<=len(rows[0])<=35
                        or any(not isinstance(row,list) or len(row)!=len(rows[0]) for row in rows)):
                    raise ValueError("Append a nonempty rectangular Word table")
                table = document.add_table(rows=len(rows),cols=len(rows[0]))
                for i,row in enumerate(rows):
                    for j,value in enumerate(row):
                        if not isinstance(value,(str,int,float)):
                            raise ValueError("Use scalar strings or numbers in Word tables")
                        table.cell(i,j).text=str(value)
            else:
                raise ValueError("Append a Word paragraph with {paragraph: text} or table with {table: [[...]]}")
        document.save(path)
    elif path.suffix.lower() == ".pptx":
        from pptx import Presentation
        from pptx.util import Inches
        deck = Presentation(path)
        for item in additions:
            if not item.get("title") or set(item) - {"title","bullets","notes","table"}:
                raise ValueError("Append a slide with title and optional bullets, notes and table")
            slide = deck.slides.add_slide(deck.slide_layouts[6])
            title = slide.shapes.add_textbox(Inches(.4), Inches(.25), deck.slide_width-Inches(.8), Inches(.8))
            title.text = item["title"]
            content_top = Inches(1.15)
            if item.get("bullets"):
                body = slide.shapes.add_textbox(Inches(.4), content_top, deck.slide_width-Inches(.8),
                                                Inches(2) if item.get("table") else deck.slide_height-content_top-Inches(.4))
                body.text_frame.word_wrap = True
                body.text = "\n".join(item["bullets"])
                content_top += Inches(2)
            if item.get("table"):
                rows = item["table"]
                cols = len(rows[0])
                if not cols or any(len(row) != cols for row in rows):
                    raise ValueError("Append a rectangular slide table")
                table = slide.shapes.add_table(len(rows), cols, Inches(.4), content_top, deck.slide_width-Inches(.8),
                                              deck.slide_height-content_top-Inches(.4)).table
                for ri,row in enumerate(rows):
                    for ci,value in enumerate(row):
                        table.cell(ri,ci).text = str(value)
            if item.get("notes"):
                slide.notes_slide.notes_text_frame.text = item["notes"]
        deck.save(path)
    else:
        raise ValueError("Additions append Word paragraphs or PowerPoint slides. Use blank cells for workbook additions.")
    after = _slots(path)
    if any(k not in after or after[k] != v for k,v in before.items()):
        raise ValueError("Appending content changed a protected native slot")
    return set(after) - set(before)


def _rewrite_xlsx(path, changes):
    """Surgical OOXML updates preserve untouched sheets, annotations, styles and shapes."""
    from lxml import etree as ET
    from zipfile import ZipFile
    from openpyxl.utils.cell import coordinate_to_tuple
    from openpyxl.worksheet.cell_range import CellRange
    from posixpath import normpath

    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    office = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    by_sheet = {}
    for locator, value in changes.items():
        sheet, coord = locator.rsplit("!", 1)
        r,c = coordinate_to_tuple(coord)
        if not (1 <= r <= 1048576 and 1 <= c <= 16384):
            raise ValueError("Cell outside Excel worksheet bounds")
        by_sheet.setdefault(sheet,{})[coord] = value
    patched = path.with_name(".patched.xlsx")
    with ZipFile(path) as source, ZipFile(patched,"w") as output:
        workbook = ET.fromstring(source.read("xl/workbook.xml"))
        relationships = ET.fromstring(source.read("xl/_rels/workbook.xml.rels"))
        content = ET.fromstring(source.read("[Content_Types].xml"))
        rels = {r.get("Id"):r.get("Target") for r in relationships}
        parts = {}
        sheets = workbook.find(ns+"sheets")
        for sheet in sheets:
            target = rels[sheet.get(office+"id")]
            parts[sheet.get("name")] = target.lstrip("/") if target.startswith("/") else normpath("xl/"+target)
        updates = {}
        for name, edits in by_sheet.items():
            if name in parts:
                root = ET.fromstring(source.read(parts[name]))
            else:
                if (not name.strip() or len(name)>31 or re.search(r"[\[\]:*?/\\]",name) or "'" in (name[0],name[-1])
                        or name.casefold() in {n.casefold() for n in parts}):
                    raise ValueError("Invalid or ambiguous new sheet name")
                sheet_id = 1 + max((int(s.get("sheetId")) for s in sheets), default=0)
                rid, count = "rIdNewSheet", 0
                while rid in rels:
                    count += 1
                    rid = "rIdNewSheet" + str(count)
                part = f"xl/worksheets/added{sheet_id}.xml"
                while part in source.namelist() or part in updates:
                    sheet_id += 1
                    part = f"xl/worksheets/added{sheet_id}.xml"
                parts[name] = part
                rels[rid] = part[3:]
                ET.SubElement(sheets,ns+"sheet",name=name,sheetId=str(sheet_id)).set(office+"id",rid)
                ET.SubElement(relationships,"{"+ET.QName(relationships).namespace+"}Relationship",
                              Id=rid,Type=office[1:-1]+"/worksheet",Target=part[3:])
                ET.SubElement(content,content.tag[:-len("Types")]+"Override",PartName="/"+part,
                              ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml")
                updates.update({"xl/workbook.xml":workbook,"xl/_rels/workbook.xml.rels":relationships,"[Content_Types].xml":content})
                root = ET.Element(ns+"worksheet",nsmap={None:ns[1:-1]})
                ET.SubElement(root,ns+"sheetData")
            data = root.find(ns+"sheetData")
            rows = {int(row.get("r")):row for row in data.iter(ns+"row")}
            dim = root.find(ns+"dimension")
            bounds = CellRange(dim.get("ref")) if dim is not None else None
            for coord,value in sorted(edits.items(), key=lambda e:coordinate_to_tuple(e[0])):
                ri,ci = coordinate_to_tuple(coord)
                row = rows.get(ri)
                if row is None:
                    row = rows[ri] = ET.Element(ns+"row",r=str(ri))
                    next_row = next((v for v in data if v.tag==ns+"row" and int(v.get("r"))>ri),None)
                    data.insert(data.index(next_row),row) if next_row is not None else data.append(row)
                cell = next((c for c in row if c.tag==ns+"c" and c.get("r")==coord),None)
                if cell is None:
                    cell = ET.Element(ns+"c",r=coord)
                    following = next((c for c in row if c.tag!=ns+"c" or coordinate_to_tuple(c.get("r"))[1]>ci),None)
                    row.insert(row.index(following),cell) if following is not None else row.append(cell)
                for child in list(cell):
                    if child.tag in {ns+"f",ns+"v",ns+"is"}:
                        cell.remove(child)
                if isinstance(value,bool):
                    cell.set("t","b")
                    ET.SubElement(cell,ns+"v").text = "1" if value else "0"
                elif isinstance(value,(int,float)):
                    cell.set("t","n")
                    ET.SubElement(cell,ns+"v").text = str(value)
                elif str(value).startswith("="):
                    cell.set("t","n")
                    ET.SubElement(cell,ns+"f").text = value[1:]
                else:
                    cell.set("t","inlineStr")
                    node = ET.SubElement(ET.SubElement(cell,ns+"is"),ns+"t")
                    node.set("{http://www.w3.org/XML/1998/namespace}space","preserve")
                    node.text = str(value)
                if bounds is not None:
                    bounds = bounds.union(CellRange(coord))
            if dim is not None:
                dim.set("ref", str(bounds))
            updates[parts[name]] = root
        for member in source.infolist():
            updated = updates.pop(member.filename,None)
            output.writestr(member,ET.tostring(updated,encoding="utf-8") if updated is not None else source.read(member))
        for name,root in updates.items():
            output.writestr(name,ET.tostring(root,encoding="utf-8"))
    patched.replace(path)


class ArtifactOperator:
    def __init__(self, workspace, dag, *, native_read_strategy=None, self_review=False):
        self.workspace, self.dag = Path(workspace).resolve(), dag
        if native_read_strategy not in {None, "decision_value", "relevance"}:
            raise ValueError("Unknown offline native reading policy")
        # Set by the paired research harness; unavailable in the model tool schema.
        self.native_read_strategy = native_read_strategy
        self.self_review = self_review
        self.root = self.workspace / "artifacts"
        self.store = self.workspace / ".artifact_versions"
        for directory in (self.root, self.store):
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink():
                raise ValueError("Symlinked artifact stores are unsupported")

    @contextmanager
    def _lock(self):
        with (self.store / ".lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self._recover()
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def materialized(self, path):
        result = self.root / _path(path)
        cur = result
        while cur != self.root:
            if cur.is_symlink():
                raise ValueError("Symlink in artifact path")
            cur = cur.parent
        return result

    def _last(self, kind, key, value):
        return next((n for n in reversed(self.dag.nodes(kind)) if n["payload"].get(key) == value), None)

    def head(self, path):
        return self._last("artifact_version", "path", _path(path))

    def _contract(self, path):
        return self._last("artifact_contract", "path", _path(path))

    def _blob(self, path, digest):
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid content hash")
        file = self.store / (digest + Path(path).suffix.lower())
        if not file.is_file() or file.is_symlink() or _sha(file) != digest:
            raise ValueError("Version blob integrity failure")
        return file

    def _save_blob(self, source):
        digest = _sha(source)
        dst = self.store / (digest + source.suffix.lower())
        if dst.exists():
            self._blob(str(source), digest)
        else:
            with tempfile.NamedTemporaryFile(dir=self.store, delete=False) as temporary:
                temporary.write(source.read_bytes())
            os.replace(temporary.name, dst)
        return digest

    def _recover(self):
        versions = {n["payload"].get("intent") for n in self.dag.nodes("artifact_version")}
        for intent in self.dag.nodes("artifact_commit_intent"):
            if intent["id"] not in versions:
                self._finish(intent)

    def _finish(self, intent):
        data = intent["payload"]
        dst = self.materialized(data["path"])
        wanted = data["sha256"]
        observed = _sha(dst) if dst.is_file() else None
        if observed != wanted:
            if observed != data["previous_sha256"]:
                raise ValueError("Output changed outside the committed artifact history")
            dst.parent.mkdir(parents=True, exist_ok=True)
            if wanted is None:
                dst.unlink()
            else:
                source = self._blob(data["path"], wanted)
                with tempfile.NamedTemporaryFile(dir=dst.parent, delete=False) as temp:
                    temp.write(source.read_bytes())
                os.replace(temp.name, dst)
        if (dst.is_file() and _sha(dst) != wanted) or (wanted is not None and not dst.is_file()):
            raise OSError("Published file does not match verified bytes")
        return self.dag.append("artifact_version", {**data, "intent": intent["id"]}, [intent["id"]])

    def seed(self, path, file, *, purpose, description, parents=(), entity=None, stage=0):
        path = _path(path)
        with self._lock():
            source = Path(file)
            if source.suffix.lower() != Path(path).suffix.lower():
                raise ValueError("Source and destination formats differ")
            _slots(source)
            digest = self._save_blob(source)
            old = self.head(path)
            if old:
                if old["payload"]["sha256"] != digest:
                    raise ValueError("Trusted seed cannot overwrite a version")
                return old
            intent = self.dag.append("artifact_commit_intent", {"path": path, "sha256": digest,
                "previous_sha256": None, "purpose": purpose, "description": description, "entity": entity or path,
                "action": "seed", "stage": stage, "bindings": []}, list(parents))
            return self._finish(intent)

    def _rule(self, entry, *, trusted_derivations=(), purpose=None):
        entry = dict(entry)
        node = self.dag.get(entry["claim_id"])
        if not node or node["kind"] != "claim":
            raise ValueError("Each native slot requires a released ledger claim")
        claim = node["payload"]
        value = claim.get("value")
        for part in entry.get("value_path", []):
            if isinstance(value, dict):
                value = value[part]
            elif isinstance(value, list):
                value = value[int(part)]
            else:
                raise ValueError("Invalid value_path for claim")
        if value is None:
            value = claim["claim"]
        if not isinstance(value, (str, int, float, bool)) or value == "":
            raise ValueError("Choose a scalar ledger value using value_path")
        from .business_calculation import verified_calculation
        calculation = verified_calculation(self.dag, node)
        receipt = None
        if node["id"] in trusted_derivations:
            for parent in node["parents"]:
                candidate = self.dag.get(parent)
                if candidate["kind"] != "native_projection":
                    continue
                p = candidate["payload"]
                if (p.get("locator") == entry["locator"] and p.get("purpose") == purpose
                        and _equal(p.get("output"),value) and p.get("method")
                        and re.fullmatch("[0-9a-f]{64}",p.get("source_sha256",""))
                        and any(self.dag.get(i)["kind"] in {"source","claim"} for i in candidate["parents"])):
                    receipt = parent
                    break
        if not (receipt or calculation) and (claim.get("source_key","").startswith("artifact-observation:") or not _anchored(value,claim)):
            raise ValueError(f"{node['id']} at {entry.get('locator')}: scalar {str(value)[:100]!r} needs a "
                 "witness in the released quote. Select a grounded ledger value/value_path, or record "
                 "a new atomic claim using the actual original span (including units and column context).")
        if "expected" in entry and not _equal(value, entry["expected"]):
            raise ValueError("Expected value conflicts with the selected ledger value")
        mode = entry.get("match", "equals")
        if mode not in {"equals", "contains"}:
            raise ValueError("Supported matches are equals and contains")
        if not isinstance(entry.get("locator"), str) or not entry["locator"]:
            raise ValueError("Expected a native locator")
        original_unit = None
        invalid_unit = False
        source_witnesses = []
        if not calculation and claim.get("value")==value and not isinstance(value,bool) and _decimal(value) is not None:
            from .business_calculation import _operand
            declared = claim.get("scope",{}).get("unit",claim.get("scope",{}).get("units",""))
            canonical = re.fullmatch(r"(USD|EUR|GBP|CNY)\s*(000|k|m|mm|mn|bn|thousands?|millions?|billions?)?",str(declared),re.I)
            if canonical:
                scale=(canonical[2] or "").lower()
                suffix="000" if scale.startswith(("k","0","t")) else "m" if scale.startswith("m") else "bn" if scale.startswith("b") else ""
                unit=canonical[1].upper()+suffix
                try:
                    _,_,source_witnesses=_operand(self.dag,{"name":"bound_native_input","claim_id":node["id"],"unit":unit},{node["id"]})
                except ValueError:
                    invalid_unit = True
                else:
                    original_unit=unit
        return {"locator": entry["locator"], "claim_id": node["id"], "value_path": entry.get("value_path", []),
                "match": mode, "expected": value, "projection_receipt": receipt,
                **({"original_unit":original_unit,"unit_witnesses":source_witnesses} if original_unit else {}),
                **({"unit_validation_failed":True} if invalid_unit else {}),
                **({"calculation_receipt":calculation["id"],"unit":calculation["payload"]["result_unit"]}
                   if calculation else {})}

    def bind(self, path, *, purpose, rules=(), description, parents=(), stage=0, allow_delete=False, entity=None,
             trusted_derivations=(), valid_at=None, semantic_rules=()):
        with self._lock():
            if not purpose or not description:
                raise ValueError("Trusted stage contract needs purpose and description")
            rules = [self._rule(rule, trusted_derivations=trusted_derivations, purpose=purpose) for rule in rules]
            semantic_rules = [dict(rule) for rule in semantic_rules]
            for rule in semantic_rules:
                cp = self.dag.get(rule["claim_id"])
                if cp["kind"] != "claim" or not is_business_claim(cp):
                    raise ValueError("Semantic rules require a released business claim")
                if "expected" in rule and not _equal(rule["expected"], cp["payload"]["value"]):
                    raise ValueError("Semantic rule conflicts with its evidence")
                rule.setdefault("expected", cp["payload"]["value"])
            if allow_delete and not any(self.dag.get(i)["kind"] == "claim" for i in parents):
                raise ValueError("Retraction requires a stage-authorized evidence claim")
            old = self._contract(path)
            ids = list(dict.fromkeys([*parents, *(r["claim_id"] for r in rules + semantic_rules), *([old["id"]] if old else [])]))
            return self.dag.append("artifact_contract", {"path": _path(path), "purpose": purpose, "rules": rules, "semantic_rules": semantic_rules,
                    "description": description, "stage": stage, "valid_at":valid_at,
                    "allow_delete": bool(allow_delete), "entity": entity}, ids)

    def _review_inputs(self, claim_ids):
        from .business_calculation import verified_calculation
        witnesses = []
        for cid in claim_ids:
            calculation = verified_calculation(self.dag,self.dag.get(cid))
            if calculation:
                witnesses.extend(self._review_inputs(calculation["payload"]["inputs"]))
            else:
                witnesses.append(cid)
        return list(dict.fromkeys(witnesses))

    def _applicable(self, contract):
        at = contract["payload"].get("valid_at")
        # Retrospective reports can cover many periods. An unspecified
        # business date preserves the fact's own historical period; a
        # point-in-time contract explicitly enforces its valid_at interval.
        return {n["id"] for n in self.dag.claim_at(valid_at=at)}

    @staticmethod
    def catalog():
        s = {"type": "string"}
        scalar = {"oneOf": [{"type": t} for t in ("string", "number", "boolean")]}
        rule = {"type": "object", "properties": {"locator": s, "claim_id": s, "match": {"type": "string","enum":["equals","contains"]},
            "value_path": {"type":"array","items":s}}, "required":["locator","claim_id"], "additionalProperties":False}
        edit = {"type":"object","properties":{"locator":s,"before":{"oneOf":[scalar,{"type":"null"}]},"after":scalar,"old_text":s,"new_text":s},
                "required":["locator","before"],"additionalProperties":False}
        additions = {"type":"array","items":{"type":"object","properties":{"paragraph":s,"title":s,
                            "bullets":{"type":"array","items":s},"notes":s,
                            "table":{"type":"array","items":{"type":"array","items":s}}},
                          "additionalProperties":False}}
        bindings = {"type":"array","items":rule}
        changes = {"type":"array","items":edit}
        return [
            _schema("artifact_format","Read the exact native renderer schema and binding address examples before writing.",
                    {"path":s},["path"]),
            _schema("artifact_findings", "Read one category of this checked proposal in pages. conflicts list the originally bound ledger ID and value path; reasons include missing obligations and unsupported assertions. bindings returns the existing dynamic edges.", {
                "proposal_id":s, "category":{"type":"string","enum":["reasons","conflicts","bindings","suggested_changes","coverage_gaps"]},
                "offset":{"type":"integer","minimum":0},"limit":{"type":"integer","minimum":1,"maximum":12}}, ["proposal_id","category"]),
            _schema("artifact_binding_candidates","Find existing ledger values matching real staged native slots. Review their entity and meaning; add approved bindings via artifact_repair. Shows original evidence and numbers still requiring a source.",
                    {"proposal_id":s,"locator":s,"offset":{"type":"integer"},"limit":{"type":"integer"}},["proposal_id"]),
            _schema("artifact_list", "List published native versions and trusted destination purposes.", {}),
            _schema("artifact_read", "Read native slots and actual hash. Supports Excel sheet!A1, Word p:1 / t:1:1:1, slide:1:notes, PDF page:1. Omit locator or pass * to page all real slots; use Sheet! to page a worksheet. A full cell address returns one value. offset and limit are slot counts.", {
                "path":s,"version_id":s,"proposal_id":s,"locator":s,"offset":{"type":"integer"},"limit":{"type":"integer"}}, ["path"]),
            _schema("artifact_propose", "Stage native content. PDF: {pages:[{text:'...'}]}; Word: {title,sections:[{heading,paragraphs:[string],table:[[string]]}]}; Excel: {sheets:[{name,rows:[[value]],cells:{A1:value}}]}; slides: {slides:[{title,bullets:[string],table:[[string]],notes:string}]}. For binding addresses and original evidence use artifact_format, artifact_read and artifact_binding_candidates. A proposal never publishes.", {
                "action":{"type":"string","enum":["create","copy","export","patch","delete"]}, "path":s, "source":s, "description":s,
                "base_sha256":s,"source_sha256":s,"bindings":bindings,"claim_ids":{"type":"array","items":s},
                "changes":changes,"additions":additions,"content":{"type":"object","additionalProperties":True}}, ["action","path","description"]),
            _schema("artifact_repair", "Use import_sheets to add a released workbook's missing sheets and their formula dependencies. Create a new proposal by making located edits on a staged version. Preserve all protected slots. To correct a misbound edge use replace_binding and supply the corresponding grounded bindings. For an empty worksheet cell or new sheet set before:null. Append Word paragraphs/tables using additions:[{paragraph:...},{table:[[...]]}], or slides [{title:...,bullets:[...],table:[[...]],notes:...}].", {
                "proposal_id":s,"description":s,"changes":changes,"additions":additions,"bindings":bindings,
                "import_sheets":{"type":"object","properties":{"source_version_id":s,
                    "sheets":{"type":"array","minItems":1,"items":s},"expected_sha256":s},
                    "required":["source_version_id","sheets"],"additionalProperties":False},
                "replace_binding":{"type":"object","properties":{"locator":s,"old_claim_id":s,"new_claim_id":s,
                    "old_value_path":{"type":"array","items":s},"new_value_path":{"type":"array","items":s}},
                    "required":["locator","old_claim_id","new_claim_id"],"additionalProperties":False},
                "probe_budget":{"type":"number"}}, ["proposal_id","description"]),
            _schema("artifact_review", "Read another original through the current ledger and action graph, scoped to this staged proposal.", {"proposal_id":s, "query":s},["proposal_id","query"]),
            _schema("artifact_commit", "Commit a ready native version. In ledger-review mode copy a short exact review_quote from the self_review.original and give review_note for this proposal.", {"proposal_id":s,"review_id":s,"review_quote":s,"review_note":s},["proposal_id"]),
        ]

    def call(self, name, args):
        with self._lock():
            if name == "artifact_format":
                fmt = _FORMATS[Path(_path(args["path"])).suffix.lower()]
                return {**fmt,"binding":{"locator":"<native location>","claim_id":"<ledger claim id>",
                     "match":"contains (prose) or equals (atomic slot)","value_path":["optional object field"]},
                    "workflow":"Stage the full content, inspect its real locations, retrieve binding candidates, "
                     "then add selected grounded bindings with artifact_repair and submit the verified proposal."}
            if name == "artifact_findings":
                proposal = self.dag.get(args["proposal_id"])
                if proposal["kind"] != "artifact_check":
                    raise ValueError("Select a checked native proposal")
                entries = proposal["payload"].get(args["category"], [])
                start = args.get("offset", 0)
                page = entries[start:start+args.get("limit", 6)]
                end = start + len(page)
                return {"proposal_id":proposal["id"],"path":proposal["payload"]["path"],"category":args["category"],
                        "offset":start,"total":len(entries),"next_offset":end if end<len(entries) else None,"items":page}
            if name == "artifact_binding_candidates":
                return self._binding_candidates(args)
            if name == "artifact_list":
                heads = {n["payload"]["path"]: n for n in self.dag.nodes("artifact_version")}
                contracts = {n["payload"]["path"]: n for n in self.dag.nodes("artifact_contract")}
                return {"artifacts":[{"path": p,"version_id": v["id"],"sha256":v["payload"]["sha256"],
                        "purpose":v["payload"]["purpose"]} for p,v in heads.items() if v["payload"]["sha256"]],
                        "destinations":[{"path":p,"purpose":v["payload"]["purpose"],"rules":v["payload"]["rules"]}
                                        for p,v in contracts.items()]}
            if name == "artifact_read":
                return self._read(args)
            if name == "artifact_propose":
                return self._propose(args)
            if name == "artifact_repair":
                return self._repair(args)
            if name == "artifact_commit":
                return self._commit(**args)
            if name == "artifact_review":
                if not self.self_review:
                    raise ValueError("The current stage did not enable graph self-review")
                return self._review_artifact(self.dag.get(args["proposal_id"]),args["query"],additional=True)
            raise ValueError(f"Unknown artifact tool: {name}")

    def _read(self, args):
        path = _path(args["path"])
        if args.get("proposal_id") and args.get("version_id"):
            raise ValueError("For a staged file pass path and proposal_id only. Omit version_id; it is only for published files.")
        if args.get("proposal_id"):
            version = self.dag.get(args["proposal_id"])
            expected_kind = "artifact_check"
        else:
            version = self.dag.get(args["version_id"]) if args.get("version_id") else self.head(path)
            expected_kind = "artifact_version"
        if not version or version["kind"] != expected_kind or version["payload"]["path"] != path:
            raise ValueError("Unknown native version for this path")
        digest = version["payload"]["sha256"]
        if digest is None:
            raise ValueError("Tombstone has no materialized bytes; specify a historical version")
        slots = _slots(self._blob(path, digest))
        key = args.get("locator")
        workbook = Path(path).suffix.lower() == ".xlsx"
        sheet_slots = {}
        if workbook:
            for loc, value in slots.items():
                if "!" not in loc or loc.startswith("native:"):
                    continue
                sheet, coordinate = loc.rsplit("!", 1)
                if re.fullmatch(r"[A-Z]+[1-9]\d*", coordinate):
                    sheet_slots.setdefault(sheet, []).append((loc, value))
        requested_sheet = key[:-1] if workbook and key and key.endswith("!") else None
        offset = max(0, int(args.get("offset", 0)))
        limit = max(1, min(100, int(args.get("limit", 25))))
        slide = (key.rstrip(":") + ":") if isinstance(key,str) and re.fullmatch(r"slide:[1-9]\d*:?",key) else None
        slide_slots = [(k,v) for k,v in slots.items() if slide and k.startswith(slide)] if slide else []
        if not key or key == "*" or requested_sheet in sheet_slots or slide_slots:
            available = slide_slots or (sheet_slots[requested_sheet] if requested_sheet in sheet_slots else list(slots.items()))
            selected = available[offset:offset + limit]
            next_offset = offset + len(selected) if offset + len(selected) < len(available) else None
        else:
            value = _slot(slots, key)
            if value is None:
                sheet = key.rsplit("!", 1)[0] if workbook and "!" in key else None
                if sheet in sheet_slots:
                    example = sheet_slots[sheet][0][0]
                    guidance = f"Use locator={sheet + '!'!r} to page this worksheet. A real cell is {example}."
                elif sheet_slots:
                    guidance = "Use locator='*' for the index. Worksheet names include " + ", ".join(list(sheet_slots)[:8])
                else:
                    guidance = "Omit locator to page the file's actual slots."
                raise ValueError(f"Missing native locator: {key}. {guidance}")
            selected = [(key, value)]
            next_offset = None
        result = {"version_id": version["id"], "path": path, "sha256": digest, "total_slots": len(slots),
                  "next_offset": next_offset, "slots": [{"locator": k, "value": v} for k, v in selected]}
        if sheet_slots and (not key or key == "*" or requested_sheet is not None):
            result["worksheets"] = [{"name": name, "populated_slots": len(entries), "first_locator": entries[0][0]}
                                    for name, entries in sheet_slots.items()]
        return result

    def _binding_candidates(self, args):
        proposal = self.dag.get(args["proposal_id"])
        if proposal["kind"] != "artifact_check" or not proposal["payload"].get("sha256"):
            raise ValueError("Expected a staged native proposal")
        p=proposal["payload"]
        slots=_slots(self._blob(p["path"],p["sha256"]))
        document_unit=document_amount_unit(slots)
        rules=self.dag.get(p["contract_id"])["payload"]["rules"] + p["bindings"]
        if args.get("locator"):
            keys=[args["locator"]]
            if _slot(slots,keys[0]) is None:
                raise ValueError("No such native slot: use artifact_read on this proposal to find the actual locator")
        else:
            keys=sorted(_material_slots(slots))
        offset=max(0,int(args.get("offset",0)))
        keys=keys[offset:offset+max(1,min(8,int(args.get("limit",3))))]
        claims=self.dag.claim_at(valid_at=self.dag.get(p["contract_id"])["payload"].get("valid_at"))

        def leaves(value, route=()):
            if len(route)>8:
                return
            if isinstance(value,dict):
                for k,v in value.items():
                    yield from leaves(v,(*route,str(k)))
            elif isinstance(value,list):
                for i,v in enumerate(value):
                    yield from leaves(v,(*route,str(i)))
            elif value is not None and value!="":
                yield list(route),value

        results=[]
        for key in keys:
            actual=_slot(slots,key)
            existing={r["claim_id"] for r in rules if r["locator"]==key}
            covered=set()
            for r in rules:
                if r["locator"]==key:
                    cp=self.dag.get(r["claim_id"])["payload"]
                    if _financial_support(actual,r["expected"],cp,r["match"],document_unit,rule=r,context=binding_context(slots,key)):
                        covered|=_supported_amounts(actual,r["expected"],cp,document_unit,rule=r,context=binding_context(slots,key))
            numbers=_amounts(actual)-covered
            suggestions=[]
            for claim in claims:
                cp=claim["payload"]
                if not is_business_claim(claim):
                    continue
                for route,v in leaves(cp["value"] if cp["value"] is not None else cp["claim"]):
                    mode="equals" if _equal(actual,v) else "contains"
                    if not _financial_support(actual,v,cp,mode,document_unit) or not _anchored(v,cp):
                        continue
                    row={"binding":{"locator":key,"claim_id":claim["id"],"match":mode,"value_path":route},
                        "entity":cp["entity"],"field":cp["field"],"value":v,"source_id":cp["source_id"],
                        "original_excerpt":cp["quote"][:250],"already_bound":claim["id"] in existing}
                    gain=len(numbers&_supported_amounts(actual,v,cp,document_unit))
                    if not row["already_bound"]:
                        suggestions.append((gain,int(mode=="equals"),row))
            suggestions.sort(key=lambda x:(x[0],x[1]),reverse=True)
            results.append({"locator":key,"native_text":str(actual)[:500],"numbers_requiring_binding":sorted(map(str,numbers)),
                     "candidate_bindings":[entry for _,__,entry in suggestions[:20]]})
        return {"proposal_id":proposal["id"],"slots":results,
          "instruction":"Only select candidates whose entity, period and meaning match the native slot. "
          "For uncovered facts use list_sources/read_source and record_claim with a grounded value, "
          "then call artifact_repair(proposal_id,description,bindings)."}

    def _require(self, path):
        version = self.head(path)
        if not version or not version["payload"]["sha256"]:
            raise ValueError(f"No current native version: {path}")
        return version

    def _propose(self, args):
        path, action = _path(args["path"]), args["action"]
        contract = self._contract(path)
        if not contract:
            raise ValueError(f"No released task purpose for {path}")
        if not str(args.get("description","")).strip():
            raise ValueError("Describe the proposed business action")
        old = self.head(path)
        expected = old["payload"]["sha256"] if old else None
        if args.get("base_sha256", expected) != expected:
            raise ValueError("Destination version changed. For a new path omit base_sha256. Use source_sha256 for the file being copied.")
        source = None
        if action in {"patch","delete"}:
            source = self._require(path)
        elif action in {"copy","export"}:
            source = self._require(_path(args["source"]))
        elif action != "create":
            raise ValueError("Unsupported action")
        elif expected:
            raise ValueError("A published native version exists; propose a located patch to amend it")
        if source and args.get("source_sha256",source["payload"]["sha256"]) != source["payload"]["sha256"]:
            raise ValueError("Source version changed")
        bindings = [self._rule(r) for r in args.get("bindings", [])]
        supplied_claims = list(args.get("claim_ids", []))
        if any(self.dag.get(i)["kind"] != "claim" for i in supplied_claims):
            raise ValueError("Invalid claim support")
        parents = list(dict.fromkeys([contract["id"], *[n["id"] for n in (old,source) if n], *supplied_claims,
                                      *(r["claim_id"] for r in bindings)]))
        payload = {"action":action,"path":path,"old_id":old["id"] if old else None,"previous_sha256":expected,
            "source_id":source["id"] if source else None,"contract_id":contract["id"],
            "purpose":contract["payload"]["purpose"],"stage":contract["payload"]["stage"],"description":args["description"],
            "bindings":bindings,"changed":[],"input_claim_ids":supplied_claims}
        attempt = self.dag.append("artifact_proposal", payload, parents)
        if action == "delete":
            ready = contract["payload"]["allow_delete"]
            if self.self_review:
                payload["proof_scope"] = proof_scope(contract,[],getattr(self,"review_policy","ledger_graph"))
                payload["consumed_claim_ids"] = [i for i in contract["parents"]
                                                 if self.dag.get(i)["kind"]=="claim" and is_business_claim(self.dag.get(i))]
                ready = ready and bool(payload["consumed_claim_ids"])
            return self._verdict(payload, attempt, None, "ready" if ready else "unresolved",
                reasons=[] if ready else ["Released contract has not authorized retirement of this artifact"])
        with tempfile.TemporaryDirectory(dir=self.store) as tmp:
            native = Path(tmp) / Path(path).name
            if action == "create":
                _render_native(native, args["content"])
            else:
                original = source["payload"]
                if Path(original["path"]).suffix.lower() != native.suffix.lower():
                    raise ValueError("Copy/export may not rename a file into a different format")
                shutil.copyfile(self._blob(original["path"],original["sha256"]), native)
            if action != "create" and (args.get("changes") or args.get("additions")):
                observed = self._inspect(payload,attempt,contract,native)
                return self._repair({"proposal_id":observed["proposal_id"],"description":args["description"],
                                     "changes":args.get("changes",[]),"additions":args.get("additions",[])})
            touched = _patch(native, args.get("changes", []))
            touched |= _append_native(native, args.get("additions", []))
            if action == "create":
                slots = _slots(native)
                previous = _slots(self._blob(path, expected)) if expected else {}
                touched |= {k for k in previous.keys() | slots.keys() if previous.get(k) != slots.get(k)}
            payload["changed"] = sorted(touched)
            return self._inspect(payload, attempt, contract, native)

    def _repair(self, args):
        prior = self.dag.get(args["proposal_id"])
        if not prior or prior["kind"] != "artifact_check" or not prior["payload"]["sha256"]:
            raise ValueError("A native staged proposal is required")
        p = prior["payload"]
        if self._contract(p["path"])["id"] != p["contract_id"]:
            raise ValueError("Purpose changed; make a fresh proposal")
        payload = {k:p[k] for k in ("action","path","old_id","previous_sha256","source_id","contract_id","purpose","stage","bindings","changed")}
        if not str(args.get("description","")).strip():
            raise ValueError("Describe the local repair")
        payload["input_claim_ids"] = list(p.get("input_claim_ids",[]))
        payload["description"] = args["description"]
        bindings = [self._rule(r) for r in args.get("bindings",[])]
        prior_bindings = []
        for previous in p["bindings"]:
            current = previous
            if (previous.get("original_unit") is None and not previous.get("unit_validation_failed")
                    and not previous.get("projection_receipt") and not previous.get("calculation_receipt")):
                node = self.dag.get(previous["claim_id"])
                scope = node["payload"].get("scope", {})
                if is_business_claim(node) and scope.get("unit", scope.get("units", "")) and not previous.get("value_path"):
                    # Rebuild only the current proposal's monetary witness from
                    # the immutable original. Old versions keep their records.
                    try:
                        checked = self._rule(previous)
                    except ValueError:
                        checked = {}
                    current = {**previous, **{k: checked[k] for k in ("original_unit", "unit_witnesses", "unit_validation_failed") if k in checked}}
            prior_bindings.append(current)
        replacement = args.get("replace_binding")
        if replacement:
            def edge_key(r):
                return (r["locator"],r["claim_id"],tuple(r.get("value_path",[])))
            old_pair = (replacement["locator"], replacement["old_claim_id"],tuple(replacement.get("old_value_path",[])))
            new_pair = (replacement["locator"], replacement["new_claim_id"],tuple(replacement.get("new_value_path",[])))
            if (old_pair == new_pair or not any(edge_key(r) == old_pair for r in prior_bindings)
                    or not any(edge_key(r) == new_pair for r in bindings)):
                raise ValueError("An edge correction requires an existing dynamic binding and its located replacement")
            slots = _slots(self._blob(p["path"],p["sha256"]))
            new = next(r for r in bindings if edge_key(r) == new_pair)
            cp = self.dag.get(new["claim_id"])
            witness, rejected = self._table_witness_checker(slots, Path(p["path"]).suffix.lower())(new, cp["payload"])
            if rejected or not is_business_claim(cp) or (not witness and (
                    mismatched_binding(slots, new["locator"], {**cp["payload"],"value_path":new.get("value_path",[])})
                    or not _financial_support(_slot(slots,new["locator"]),new["expected"],cp["payload"],new["match"],
                                              document_amount_unit(slots),rule=new,context=binding_context(slots,new["locator"])))):
                raise ValueError("Replacement must independently support the actual slot before changing its edge")
            prior_bindings = [r for r in prior_bindings if edge_key(r) != old_pair]
            if replacement["old_claim_id"] in payload["input_claim_ids"] and not any(
                    r["claim_id"]==replacement["old_claim_id"] for r in prior_bindings):
                payload["input_claim_ids"].remove(replacement["old_claim_id"])
        payload["bindings"] = prior_bindings + bindings
        payload["import_receipts"] = list(p.get("import_receipts",[]))
        payload["repair_of"] = prior["id"]
        payload["repair_before_sha256"] = p["sha256"]
        payload["replaced_binding"] = replacement
        edits = args.get("changes",[])
        decisions, missing = self._authorize_repair(prior, edits, args.get("probe_budget",24))
        if missing:
            pending = self.dag.append("artifact_repair_held", {"proposal_id":prior["id"],"reasons":missing,
                "description":args["description"],"purpose":p["purpose"]},[prior["id"],*decisions])
            return {"status":"unresolved","proposal_id":prior["id"],"decision_id":pending["id"],
                    "reasons":missing,"conflicts":p["conflicts"],"suggested_changes":p["suggested_changes"]}
        attempt = self.dag.append("artifact_repair_proposal", payload,
                                  [prior["id"], *decisions, *(r["claim_id"] for r in bindings)])
        with tempfile.TemporaryDirectory(dir=self.store) as tmp:
            native = Path(tmp) / Path(p["path"]).name
            shutil.copyfile(self._blob(p["path"],p["sha256"]), native)
            before = _slots(native)
            touched = set()
            if args.get("import_sheets"):
                from .native_sheet_import import import_released_sheets
                details = import_released_sheets(self,native,stage=p["stage"],destination=p["path"],**args["import_sheets"])
                receipt = self.dag.append("native_sheet_import", details,
                            [attempt["id"],details["source_version_id"],details["input_source_id"],
                             *(identifier for proof in details.get("external_proofs",[])
                               for identifier in (proof["source_version_id"],proof["source_id"]))])
                payload["import_receipts"].append(receipt["id"])
                touched.update(details["changed_locators"])
            touched |= _patch(native,edits)
            touched |= _append_native(native, args.get("additions", []))
            after = _slots(native)
            payload["repair_changed"] = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
            payload["changed"] = sorted(set(p["changed"]) | touched)
            return self._inspect(payload, attempt, self.dag.get(p["contract_id"]), native)

    def _authorize_repair(self, prior, edits, budget):
        """Buy witnesses for the requested changes before mutating staged bytes.

        File bytes were measured during preflight. A preserved hash and matching
        before-image discharge that hypothesis. The remaining question is which
        source authorizes the different use. Reuse original windows shared by
        several slots; a trusted computed projection also requires the source
        workbook version against which its derivation was constructed.
        """
        from .active_diagnosis import inspect
        if isinstance(budget,bool) or not isinstance(budget,(int,float)) or not 0 <= budget <= 24:
            raise ValueError("probe_budget must be between zero and 24")
        p = prior["payload"]
        editable = {e["locator"]:e for e in edits}
        decisions, missing, remaining = [], [], float(budget)
        contract = self.dag.get(p["contract_id"])
        rules = contract["payload"]["rules"]
        slots = _slots(self._blob(p["path"],p["sha256"]))
        active = self._applicable(contract)
        for issue in p["conflicts"]:
            loc = issue["locator"]
            if loc not in editable:
                continue
            cid = issue.get("conflict_id")
            if not cid or not _equal(_slot(slots,loc),issue["actual"]):
                missing.append(f"{loc}: verified before-image required")
                continue
            if issue.get("kind") == "binding_error":
                missing.append(f"{loc}: correct the ledger binding; the native value is protected")
                continue
            if issue.get("finance_receipt_id"):
                rec = self.dag.get(issue["finance_receipt_id"])
                edit = editable[loc]
                if (rec["kind"] != "native_finance_check" or rec["payload"]["input_sha256"] != p["sha256"]
                        or not _equal(edit.get("after"),issue["expected"]) or remaining < 1):
                    missing.append(f"{loc}: repair needs the computed relation for these bytes and its exact after-image")
                    continue
                evidence = self.dag.get(issue["new_claim_id"])["payload"]
                probe = self.dag.probe(cid,evidence["source_id"],evidence["quote"],"Inspect native relation precedent",
                                       start=evidence["start"])
                decisions.append(self.dag.append("diagnosis_step", {"comparison_id":cid, "target_id":prior["id"],
                        "state":"verified_formula_relation", "purpose":p["purpose"], "locator":loc,
                        "inputs":issue["inputs"],"before":issue["actual"],"after":issue["expected"]},
                        [prior["id"], rec["id"],probe["id"]])["id"])
                remaining -= 1
                continue
            # A model-supplied association may request a probe but cannot
            # authorize its own correction. The general diagnosis must support
            # a unique action if the trusted task adapter supplied no binding.
            rule = next((r for r in rules if r["locator"]==loc and r["claim_id"]==issue["new_claim_id"]),None)
            if rule is None and self.self_review:
                from .live_review import review
                from .active_diagnosis import _decided
                comparison = self.dag.get(cid)
                pair = comparison["payload"]
                probes = [n["id"] for n in self.dag.nodes("probe") if n["payload"]["comparison_id"] == cid]
                supported, target_id = _decided(self.dag,comparison,probes)
                if len(supported)!=1 and remaining >= 1:
                    earlier = [n for n in self.dag.nodes("ledger_self_review") if n["payload"]["target_id"] == cid]
                    query = " ".join([pair["old"]["entity"],pair["new"]["field"] or loc,
                                      pair["new"]["claim"],p["purpose"]])
                    original = review(self.dag,cid,query,[cid],previous=earlier,stage=p["stage"],
                                      purpose_role="business_evidence",policy=getattr(self,"review_policy","ledger_graph"))
                    if original:
                        rp = original["payload"]
                        probe = self.dag.probe(cid,rp["source_id"],rp["text"],"Read original before changing this binding",
                                               start=rp["start"],review_id=original["id"])
                        probes.append(probe["id"])
                        supported, target_id = _decided(self.dag,comparison,probes)
                        remaining -= 1
                step = self.dag.append("diagnosis_step", {"comparison_id":cid, "locator":loc,
                            "purpose":p["purpose"],"target_id":target_id,"supported_repairs":supported,
                            "evidence_ids":probes,"state":"evidence_supported_repair" if len(supported)==1 else "unresolved"},
                            [prior["id"],cid,*probes])
                decisions.append(step["id"])
                if set(supported) not in ({"use"},{"value"},{"scope"}):
                    missing.append(f"{loc}: original evidence does not distinguish a safe native repair")
                continue
            if rule is None:
                old_spent = sum(1 + (n["payload"]["end"]-n["payload"]["start"])/1000
                    for n in self.dag.nodes("probe") if n["payload"]["comparison_id"]==cid)
                step = inspect(self.dag,cid,budget=remaining+old_spent)
                decisions.append(step["decision_step"])
                remaining -= max(0,step["spent"]-old_spent)
                support = set(step["supported_repairs"])
                if support not in ({"use"},{"value"},{"scope"}):
                    missing.append(f"{loc}: original evidence does not distinguish a safe native repair")
                continue
            claim = self.dag.get(rule["claim_id"])["payload"]
            if rule["claim_id"] not in active:
                missing.append(f"{loc}: bound claim is no longer admissible")
                continue
            target = self.dag.get(p["source_id"]) if p["source_id"] else None
            receipt_id = rule["projection_receipt"]
            if receipt_id:
                receipt = self.dag.get(receipt_id)["payload"]
                if target and receipt["source_sha256"] != target["payload"]["sha256"]:
                    missing.append(f"{loc}: projection was derived from another version")
                    continue
            mark = f'{claim["source_id"]}:{claim["start"]}:{claim["end"]}'
            cached = self._last("artifact_evidence_read","window",mark)
            cost = 0.01 if cached else 1 + (claim["end"]-claim["start"])/1000
            hypotheses = [{"id":"current_support","repair":"repair_current_use"},
                          {"id":"missing_support","repair":"retain_and_hold"}]
            plans = [
                {"id":"source_window","cost":cost,
                 "outcomes":{"current_support":"current value or traceable projection","missing_support":"absent"}},
                {"id":"reread_historical_before_image","cost":1,
                 "outcomes":{"current_support":"same observed value","missing_support":"same observed value"}}]
            read_strategy = self.native_read_strategy
            # Both full-run arms use the same deterministic source check for a
            # trusted projection. Only discretionary evidence selection varies.
            if read_strategy:
                from .ablation_selection import rank_native_windows
                pair = self.dag.get(cid)["payload"]
                candidates = [{"id": plan_id, **{k: pair[s][k] for k in
                              ("locator", "description", "source_id", "start", "end")}} for plan_id, s in (
                    ("source_window", "new"), ("reread_historical_before_image", "old"))]
                # Only the original proposal and use are visible at selection time.
                query = " ".join([p["description"], p["purpose"], loc])
                choices = rank_native_windows(read_strategy, query, candidates, hypotheses, plans)
                if remaining < 1:
                    missing.append(f"{loc}: fixed one-window read cost exceeds remaining budget")
                    continue
                cost, cached = 1, None
                if choices[0]["id"] == "reread_historical_before_image":
                    before = pair["old"]
                    excerpt = self.dag.read_excerpt(before["source_id"], before["start"], before["end"]-before["start"])
                    probe = self.dag.probe(cid, before["source_id"], excerpt,
                        "Re-inspect historical native observation for this copy", start=before["start"])
                    remaining -= cost
                    decision = self.dag.append("diagnosis_step", {"comparison_id":cid, "purpose":p["purpose"],
                        "target_id":prior["id"], "locator":loc, "probe_ranking":choices,
                        "chosen":choices[0]["id"], "state":"unresolved", "supported_repairs":[],
                        "evidence_ids":[probe["id"]], "budget":budget, "remaining":remaining},
                        [prior["id"], contract["id"], probe["id"]])
                    decisions.append(decision["id"])
                    missing.append(f"{loc}: the before-image confirms the old value but cannot authorize the proposed use")
                    continue
            else:
                choices = rank_probes(hypotheses, plans, budget=remaining)
            if not choices or (read_strategy is None and choices[0]["gain_per_cost"] <= 0):
                missing.append(f"{loc}: insufficient budget for the distinguishing source window")
                continue
            if cached:
                observed = cached["payload"]["quote"]
            else:
                observed = self.dag.read_excerpt(claim["source_id"],claim["start"],claim["end"]-claim["start"])
                cached = self.dag.append("artifact_evidence_read",{"window":mark,"quote":observed,
                        "source_id":claim["source_id"],"locator":claim["locator"]},[claim["source_id"]])
            remaining -= cost
            if observed != claim["quote"] or (not receipt_id and not _anchored(rule["expected"],claim)):
                missing.append(f"{loc}: source did not confirm the required value")
                continue
            probe = self.dag.probe(cid,claim["source_id"],observed,
                        "Read current business evidence for purpose-bound native edit",start=claim["start"])
            decision = self.dag.append("diagnosis_step",{"comparison_id":cid,"purpose":p["purpose"],
                "target_id":prior["id"],"locator":loc,"probe_ranking":choices,"chosen":"source_window",
                "state":"evidence_supported_repair","supported_repairs":["use"],"evidence_ids":[probe["id"]],
                "budget":budget,"remaining":remaining,"shared_read":cached["id"]},
                [prior["id"],contract["id"],probe["id"],cached["id"],*([receipt_id] if receipt_id else [])])
            decisions.append(decision["id"])
        return decisions,missing

    def _old_entity(self, native_slots, key, actual, payload):
        source = self.dag.get(payload["source_id"])["payload"] if payload["source_id"] else {}
        context = str(actual)
        if "!" in key:
            sheet, cell = key.rsplit("!",1)
            row = re.search(r"[0-9]+$", cell)
            if row:
                context = str(native_slots.get(f"{sheet}!A{row[0]}","")) + " " + context
        candidates = re.findall(r"\b(?:case|scenario|option|tranche)\s+[A-Z0-9]+\b", context, re.I)
        normalized = {name.casefold():name for name in candidates}
        if len(normalized) == 1:
            return next(iter(normalized.values()))
        return source.get("entity") or source.get("path") or payload["path"]

    def _observed_conflict(self, native_slots, rule, payload, attempt):
        key, current = rule["locator"], _slot(native_slots,rule["locator"])
        if current is None:
            return {"locator":key,"expected":rule["expected"],"actual":None,"claim_id":rule["claim_id"],
                    "reason":"native slot absent; no original sentence available for old side"}
        text = str(current)
        if not text.strip():
            return {"locator":key,"expected":rule["expected"],"actual":current,"claim_id":rule["claim_id"],
                    "reason":"native slot empty; no original sentence available for old side"}
        # Source is the bytes of the staged file, never the declared description.
        locator = f"{payload['path']}@{payload['sha256']}#{key}"
        source = self.dag.release_source(payload["stage"],locator,text,source_key="artifact-observation:"+locator)
        old_entity = self._old_entity(native_slots,key,current,payload)
        field = self.dag.get(rule["claim_id"])["payload"].get("field")
        old = self.dag.record_claim(source["id"],old_entity,f"{key} contains {text}",
            f"Observed in the proposed {payload['action']} at {key}",text,
            field=field or key,value=current,scope={"artifact_observation":payload["sha256"], "locator":key},parents=[attempt["id"]])
        observation = self.dag.append("artifact_observation",{"purpose":payload["purpose"],"path":payload["path"],
            "sha256":payload["sha256"],"locator":key,"value":current},[attempt["id"],old["id"]])
        comparison = self.dag.compare(old["id"],rule["claim_id"],payload["description"],
                                      purpose=payload["purpose"],parents=[attempt["id"],observation["id"]])
        return {"locator":key,"new_claim_id":rule["claim_id"],"old_claim_id":old["id"],
                "conflict_id":comparison["id"],"actual":current,"expected":rule["expected"]}

    def _business_checks(self, p, attempt, contract, slots, is_workbook):
        from .task_obligations import check_obligations
        from .native_finance import inspect_workbook, compare_term_rules, released_cash_observations

        obligations = check_obligations(self.dag, p["stage"], p["path"], slots)
        p["obligation_checks"] = obligations["checks"]
        p["proof_scope"] = proof_scope(contract, obligations["checks"], getattr(self, "review_policy", "ledger_graph"))
        mirror = self._mirror_source(p, contract)
        if mirror:
            source = mirror["payload"]
            for key in ("bindings","consumed_claim_ids","analysis_input_ids","finance_receipt_id","coverage_gaps","recalculation"):
                if key in source:
                    p[key] = source[key]
            p["proof_reused_from"] = mirror["id"]
            return [], [], [], True
        reasons = [{"task_obligation":r} for r in obligations["blocking"]]
        if not is_workbook:
            return [], [], reasons, False
        term_rules = contract["payload"].get("semantic_rules", [])
        claims = self.dag.claim_at(valid_at=contract["payload"].get("valid_at"))
        selection = [n for n in claims if n["payload"]["field"] == "selected_scenario"
                     and n["payload"]["scope"].get("purpose")
                     and (n["id"] in contract["parents"] or n["id"] in p["input_claim_ids"]
                          or n["id"] in {r["claim_id"] for r in term_rules})]
        cases = {str(n["payload"]["value"]) for n in selection}
        if len(cases)>1:
            reasons.append("Conflicting applicable scenario instructions require a ledger decision")
        cash_sources = released_cash_observations(slots,contract["payload"]["rules"]+p["bindings"],claims)
        data = inspect_workbook(slots,selected_scenario=next(iter(cases)) if len(cases)==1 else None,
                                cash_evidence=cash_sources)
        term_result = compare_term_rules(data, term_rules, slots) if term_rules else {"checks":[],"findings":[]}
        checks = data["checks"] + term_result["checks"]
        findings = data["findings"] + term_result["findings"]
        ids = [n["id"] for n in selection]
        ids += [c["claim_id"] for c in term_result["checks"] + term_result["findings"] if c.get("claim_id")]
        ids += sorted(set(data["cash_observation_claims"].values()))
        p["consumer_claim_ids"] = sorted(set(ids))
        receipt = self.dag.append("native_finance_check", {**data, "checks":checks, "findings":findings,
                    "path":p["path"],"stage":p["stage"],"purpose":p["purpose"],"input_sha256":p["sha256"]},
                    [attempt["id"],contract["id"],*ids])
        p["finance_receipt_id"] = receipt["id"]
        if data["applicable"]:
            p["financial_precedents"] = sorted({key for refs in data["dependencies"].values() for key in refs})
        conflicts, suggestions = [], []
        for f in findings:
            edit = f.get("repair")
            issue = {k:v for k,v in f.items() if k!="repair"}
            issue["finance_receipt_id"] = receipt["id"]
            if not edit:
                reasons.append({"native_finance":issue})
                continue
            loc = edit["locator"]
            old = str(_slot(slots, loc))
            inputs = [i for i in f.get("inputs", []) if isinstance(i, str) and _slot(slots,i) is not None]
            if not inputs or _slot(slots,loc) is None:
                reasons.append({"native_finance":issue})
                continue
            # A formula identity derives from the observed reference and period labels.
            # Its authority and method are explicit. The original quotation is the
            # actual precedent from the inspected workbook, never a synthetic formula.
            cp = self._native_observation(p, loc, old, _slot(slots,loc), "Observed native expression", attempt)
            first = inputs[0]
            if f.get("claim_id"):
                new = self.dag.get(f["claim_id"])
            else:
                new = self._native_observation(p, first, str(_slot(slots,first)), edit["after"],
                                               f["reason"], receipt, derived_locator=loc)
            pair = self.dag.compare(cp["id"], new["id"], f["reason"], purpose=p["purpose"],
                                    parents=[receipt["id"]])
            conflicts.append({**issue,"locator":loc,"actual":edit["before"],"expected":edit["after"],
                              "old_claim_id":cp["id"],"new_claim_id":new["id"],"conflict_id":pair["id"]})
            suggestions.append(edit)
        return conflicts, suggestions, reasons, False

    def _native_observation(self, p, loc, quote, value, description, parent, derived_locator=None):
        label = f"{p['path']}@{p['sha256']}#{loc}"
        src = self.dag.release_source(p["stage"],label,quote,source_key="artifact-observation:"+label)
        return self.dag.record_claim(src["id"],p["path"],f"{derived_locator or loc} = {value}",description,
                    quote,field=derived_locator or loc,value=value,scope={"artifact_observation":p["sha256"],
                    "locator":derived_locator or loc,"derivation":bool(derived_locator)},parents=[parent["id"]])

    def _mirror_source(self, p, contract):
        if (p["action"] not in {"copy","export"} or not p.get("source_id") or p.get("changed")
                or p.get("repair_of") or p.get("bindings")):
            return None
        source = self.dag.get(p["source_id"])
        q = source["payload"]
        if (not q.get("proof_id") or p["sha256"] != q.get("sha256") or p["proof_scope"] != q.get("proof_scope")
                or set(p.get("input_claim_ids",[])) - set(q.get("input_claim_ids",[]))):
            return None
        self.validate_head(q["path"])
        return source

    def _verified_rate_binding(self, rule, claim, fiscal_checks):
        """Reuse the scenario-register join for released rates on this exact native cell."""
        from .native_finance import scenario, number
        metric=str(claim.get("field","")).replace("_"," ").casefold()
        for c in fiscal_checks.get(rule["locator"],()):
            if c["field"] not in {"amortization_pct","sweep_pct"} or number(rule["expected"])!=number(c["expected"]):
                continue
            if c["claim_id"]==rule["claim_id"] and not rule.get("value_path"):
                return True
            # Aliases require the same exact original span, scenario, and rate
            # semantics, not proximity of numbers in a multi-case register row.
            primary=self.dag.get(c["claim_id"])["payload"]
            if not all(claim.get(k)==primary.get(k) for k in ("source_id","start","end","quote")):
                continue
            case=scenario(claim.get("scope",{}).get("scenario")) or scenario(claim.get("entity"))
            if case!=c["entity"] or not re.search(r"pct|percent|[%]",metric+" "+str(claim.get("scope",{}).get("units",""))):
                continue
            if (c["field"]=="amortization_pct" and re.search(r"amorti[sz]",metric) or
                c["field"]=="sweep_pct" and "sweep" in metric):
                return True
        return False

    def _table_witness_checker(self, slots, suffix):
        """Check explicit native row/column evidence against immutable inputs once per operation."""
        books, source_heads = {}, {}
        def source_seed(claim):
            sid = claim["source_id"]
            if sid not in source_heads:
                head = self.head(claim["locator"])
                source_heads[sid] = None
                if head and head["payload"].get("action") == "seed":
                    intent = self.dag.get(head["payload"]["intent"])
                    if sid in intent["parents"]:
                        source_heads[sid] = head
            return source_heads[sid]

        def load_source_book(claim):
            from openpyxl import load_workbook
            sid = claim["source_id"]
            if sid not in books:
                head = source_seed(claim)
                if not head:
                    return None
                blob = self._blob(claim["locator"], head["payload"]["sha256"])
                books[sid] = load_workbook(blob, read_only=False)
            return books[sid]

        def check(rule, claim):
            if not (self.self_review and suffix == ".pptx" and is_business_claim({"payload": claim})
                    and not rule.get("calculation_receipt")):
                return None, None
            source = self.dag.get(claim["source_id"])["payload"]
            witness, rejected = typed_table_witness(slots, rule, claim, source, load_source_book)
            if witness:
                original = source_seed(claim)
                if original:
                    witness["source_native_version_id"] = original["id"]
                    witness["source_native_sha256"] = original["payload"]["sha256"]
            return witness, rejected

        return check

    def _inspect(self, p, attempt, contract, native):
        p["sha256"] = self._save_blob(native)
        slots = _slots(native)
        conflicts, suggestions, reasons = [], [], []
        if self.self_review:
            conflicts, suggestions, reasons, reused = self._business_checks(p,attempt,contract,slots,native.suffix.lower()==".xlsx")
            if reused:
                return self._verdict(p,attempt,p["sha256"],"ready",calc=p.get("recalculation"))
        document_unit=document_amount_unit(slots)
        if not contract["payload"]["rules"]:
            new_links = self._infer_numeric_bindings(slots,p["bindings"],contract,document_unit)
            if new_links:
                p["bindings"] = [*p["bindings"],*new_links]
        rules = contract["payload"]["rules"] + p["bindings"]
        active = self._applicable(contract)
        fiscal_checks={}
        if p.get("finance_receipt_id"):
            finance=self.dag.get(p["finance_receipt_id"])["payload"]
            if finance["input_sha256"]==p["sha256"]:
                for row in finance["checks"]:
                    if row.get("rule")=="released_term" and row.get("status")=="verified":
                        fiscal_checks.setdefault(row["locator"],[]).append(row)
        check_table = self._table_witness_checker(slots, native.suffix.lower())
        rejected_rules = []
        for rule in rules:
            if rule["claim_id"] not in active:
                reasons.append({"claim_id":rule["claim_id"],"reason":"Claim is pending or superseded"})
                continue
            cp = self.dag.get(rule["claim_id"])["payload"]
            binding_error = (self.self_review and rule in p["bindings"]
                             and mismatched_binding(slots, rule["locator"], {**cp,"value_path":rule.get("value_path",[])}))
            witness, rejected = check_table(rule, cp)
            if witness:
                p.setdefault("typed_table_witnesses", []).append({
                    "claim_id": rule["claim_id"], "source_id": cp["source_id"], "locator": rule["locator"],
                    "value_path": rule.get("value_path", []), **witness,
                })
                continue
            if rejected:
                rejected_rules.append(rule)
                binding_error = True
            if not binding_error and self._verified_rate_binding(rule,cp,fiscal_checks):
                continue
            if (self.self_review and native.suffix.lower()==".pdf" and _decimal(rule["expected"]) is not None
                    and not binding_error and not rule.get("calculation_receipt")
                    and is_business_claim({"payload":cp})):
                witness = pdf_table_witness(slots,rule,cp,self.dag.get(cp["source_id"])["payload"]["text"])
                if witness:
                    p.setdefault("pdf_table_witnesses",[]).append({"claim_id":rule["claim_id"],**witness})
                    continue
            if binding_error or not _financial_support(_slot(slots,rule["locator"]),rule["expected"],cp,rule["match"],document_unit,
                                                       rule=rule,context=binding_context(slots,rule["locator"])):
                conflict = self._observed_conflict(slots, rule, p, attempt)
                conflict["kind"] = "binding_error" if binding_error else "state_or_binding_error"
                if rejected:
                    conflict["binding_reason"] = rejected
                if rule in p["bindings"]:
                    conflict["observed_native_claim_id"] = conflict.get("old_claim_id")
                    conflict["bound_ledger_claim_id"] = rule["claim_id"]
                    conflict["bound_value_path"] = rule.get("value_path",[])
                    valid = []
                    actual_slot = _slot(slots,rule["locator"])
                    for r in rules:
                        if r is rule or r["locator"]!=rule["locator"] or (r["claim_id"],r.get("value_path",[])) == (rule["claim_id"],rule.get("value_path",[])):
                            continue
                        cp2=self.dag.get(r["claim_id"])["payload"]
                        alternate, unsafe = check_table(r, cp2)
                        if alternate or (not unsafe and not mismatched_binding(slots,r["locator"],{**cp2,"value_path":r.get("value_path",[])})
                                and _financial_support(actual_slot,r["expected"],cp2,r["match"],document_unit,
                                                       rule=r,context=binding_context(slots,r["locator"]))):
                            valid.append(r)
                    replacement = valid[0] if len(valid)==1 else None
                    conflict["replace_binding"] = {"locator":rule["locator"],"old_claim_id":rule["claim_id"],
                        "old_value_path":rule.get("value_path",[]), "new_claim_id":replacement["claim_id"] if replacement else "<select grounded claim_id>",
                        "new_value_path":replacement.get("value_path",[]) if replacement else []}
                    if replacement:
                        conflict["replacement_binding"] = {k:replacement[k] for k in ("locator","claim_id","value_path","match")}
                conflicts.append(conflict)
                if not binding_error and conflict.get("actual") is not None and rule["match"] == "equals":
                    suggestions.append({"locator":rule["locator"],"before":conflict["actual"],"after":rule["expected"]})
        by_locator = {}
        for rule in rules:
            by_locator.setdefault(rule["locator"].split("#line:")[0], []).append(rule)
        changed = set(p["changed"])
        # A host-defined projection checks the destination's explicitly bound
        # facts; edits to further material slots always require fresh coverage.
        # Without a projection require witnesses throughout the materialized file.
        required = _material_slots(slots)
        if contract["payload"]["rules"]:
            required &= changed
        missing = sorted(required - by_locator.keys())
        # An original slide deck, memo or workbook may include analysis, labels and
        # forecasts with no mechanically checkable scalar contract. Record such
        # coverage gaps for the candidate's explicit review. A contradicting
        # binding, damaged historical slot or invalid phase contract still blocks.
        original_inputs=set(p.get("input_claim_ids",[]))
        cited = original_inputs | {r["claim_id"] for r in rules} | set(p.get("consumer_claim_ids",[]))
        business_cited = {i for i in cited if is_business_claim(self.dag.get(i))}
        bound = {r["claim_id"] for r in rules if r["claim_id"] in business_cited}
        if self.self_review:
            evidence_rules = [r for r in rules if r not in rejected_rules]
            if p.get("finance_receipt_id"):
                for check in self.dag.get(p["finance_receipt_id"])["payload"]["checks"]:
                    if check.get("claim_id") and check.get("status")=="verified":
                        evidence_rules.append({"locator":check["locator"],"claim_id":check["claim_id"],
                                               "expected":check["expected"],"semantic_term":True})
            checked = verify_native_assertions(self.dag,slots,evidence_rules)
            for witness in p.get("typed_table_witnesses",[]):
                if not witness["atomic_cell"]:
                    continue
                for assertion in checked:
                    if (assertion["kind"]=="amount" and assertion["locator"]==witness["locator"]
                            and assertion["fragment"]==witness["native_text"]):
                        assertion["claim_ids"]=list(dict.fromkeys([*assertion["claim_ids"],witness["claim_id"]]))
                        assertion["status"]="verified"
            p["assertion_checks"] = checked
            for assertion in checked:
                if not assertion["claim_ids"]:
                    reasons.append({"locator":assertion["locator"],"assertion":assertion["kind"],
                                    "text":assertion["fragment"][:350],
                                    "reason":"Bind this exact native assertion to a claim that witnesses its entity, event and value. A condition or proposed preparation does not establish execution."})
        if self.self_review:
            # Direct monetary claims require a located witness or an executable derivation.
            # Other analysis remains separately disclosed even when supported by original inputs.
            for loc in missing:
                if measured_assertion(slots[loc]):
                    reasons.append({"locator":loc,"reason":"Unbound financial result needs a located original-backed claim"})
            for loc, bound_rules in by_locator.items():
                if (measured_assertion(slots.get(loc)) and not any(
                        r["claim_id"] in business_cited or r.get("projection_receipt") or r.get("calculation_receipt")
                        for r in bound_rules)):
                    reasons.append({"locator":loc,"reason":"The financial assertion requires business evidence or a verified calculation"})
            if p.get("financial_precedents"):
                precedents = set(p["financial_precedents"])
                used = {r["claim_id"] for r in rules if r["locator"] in precedents}
                report = self.dag.get(p["finance_receipt_id"])["payload"]
                used |= {c["claim_id"] for c in report["checks"]
                         if c.get("claim_id") and c["locator"] in precedents}
                if not (used & business_cited):
                    reasons.append({"reason":"No released business claim is linked to a formula precedent",
                                    "examples":sorted(precedents)[:12]})
            p["consumed_claim_ids"] = sorted((bound | set(p.get("consumer_claim_ids",[]))) & business_cited)
            # Guidance and analytical judgements with no asserted financial
            # result can cite their inputs. Do not certify those inputs as a
            # verified native fact. Preserve the distinction in the receipt.
            p["analysis_input_ids"] = []
            if not p["consumed_claim_ids"] and not checked and not any(measured_assertion(v) for v in slots.values()):
                p["analysis_input_ids"] = sorted(i for i in business_cited if admissible_text(self.dag.get(i)["payload"]))
        reviewed_authorship = (self.self_review and bool(cited) and cited <= active
                               and not contract["payload"]["rules"] and bool(required)
                               and bool(business_cited))
        if self.self_review and not original_inputs <= active:
            reasons.append("One or more supplied ledger inputs are pending or superseded")
        if missing:
            gap={"unbound_material_slots":missing[:100],"total":len(missing)}
            if reviewed_authorship:
                p["coverage_gaps"]=[gap]
            else:
                reasons.append(gap)
        if not rules and not reviewed_authorship and not p.get("consumer_claim_ids"):
            reasons.append("Supply an active, original-backed input via claim_ids or bind a factual native slot")

        for key in sorted(required & by_locator.keys()):
            raw = slots[key]
            if isinstance(raw,str):
                observed = _amounts(raw)
                if self.self_review and measured_assertion(raw):
                    # Scenario labels, report years and numbered list items
                    # describe a use; they are not financial outputs.
                    observed = {q.value for q in document_quantities(raw,document_unit)
                                if q.dimension is not None or q.scale != 1}
                supported = set()
                for rule in by_locator[key]:
                    cp = self.dag.get(rule["claim_id"])["payload"]
                    supported |= _supported_amounts(raw, rule["expected"], cp, document_unit,rule=rule,context=binding_context(slots,key))
                supported.update(Decimal(w["displayed_number"]) for w in p.get("typed_table_witnesses",[])
                                 if w["atomic_cell"] and w["locator"]==key and w["native_text"]==str(raw).strip())
                if observed - supported:
                    gap={"locator":key, "uncovered_numbers":sorted(str(x) for x in observed - supported)}
                    if reviewed_authorship and not measured_assertion(raw):
                        p.setdefault("coverage_gaps",[]).append(gap)
                    else:
                        reasons.append(gap)
        # Overwrite/create cannot silently erase substantive passages.
        if changed and p.get("source_id"):
            source = self.dag.get(p["source_id"])["payload"]
            prior_slots = _slots(self._blob(source["path"],source["sha256"]))
            lost = sorted(key for key in _material_slots(prior_slots) & changed
                          if key not in slots or slots[key] is None or slots[key] == "")
            if lost:
                if self.self_review:
                    p["removed_native_slots"] = lost
                else:
                    reasons.append({"removed_material_slots_require_retirement":lost[:100],"total":len(lost)})
        calc = None
        if not reasons and not conflicts and native.suffix.lower() == ".xlsx" and (
                changed or p["action"] == "create") and any(isinstance(v,str) and v.startswith("=") for v in slots.values()):
            calc = self._recalculate(native, p, slots)
            if calc["status"] != "complete":
                reasons.append({"recalculation":calc})
            else:
                p["sha256"] = self._save_blob(native)
                if self.self_review and p.get("finance_receipt_id"):
                    from .native_finance import inspect_workbook
                    receipt = self.dag.get(p["finance_receipt_id"])
                    verified = inspect_workbook(_slots(native),cached_slots=_slots(native,cached=True))
                    # Check the exact submitted bytes including the independently
                    # recomputed caches; the prior receipt retains scoped source terms.
                    reported = {(r["rule"],r["locator"]) for r in receipt["payload"]["findings"]}
                    reasons += [{"native_finance":f} for f in verified["findings"]
                                if (f["rule"],f["locator"]) not in reported]
                    p["finance_receipt_id"] = self.dag.append("native_finance_check", {
                        **receipt["payload"],"input_sha256":p["sha256"],"previous_input_sha256":receipt["payload"]["input_sha256"],
                        "recalculated_checks":verified["checks"],"recalculated_findings":verified["findings"]},
                        [receipt["id"]])["id"]
        if self.self_review and not (p.get("consumed_claim_ids") or p.get("analysis_input_ids")):
            reasons.append("No released business evidence is bound to the checked result. Bind a factual slot or its calculation inputs.")
        state = "requires_repair" if conflicts else "unresolved" if reasons else "ready"
        return self._verdict(p, attempt, p["sha256"], state, conflicts, suggestions, reasons, calc)

    def _infer_numeric_bindings(self, slots, already, contract, document_unit=None):
        """Link an amount only with a unique entity, metric, and unit match.

        Use previously admitted graph claims and originals. This cannot create
        an authority from an artifact's own output. Highly ambiguous tables
        remain tasks for an explicit, located evidence binding.
        """
        active=self.dag.claim_at(valid_at=contract["payload"].get("valid_at"))
        index={}
        def numeric_leaves(value,path=()):
            if isinstance(value,dict):
                for key,x in value.items():
                    yield from numeric_leaves(x,(*path,str(key)))
            elif isinstance(value,list):
                for i,x in enumerate(value):
                    yield from numeric_leaves(x,(*path,str(i)))
            elif (num:=_decimal(value)) is not None:
                yield path,value,num
        for n in active:
            cp=n["payload"]
            if not is_business_claim(n):
                continue
            roles=metrics(cp["field"]) | metrics(cp["claim"])
            labels=semantic_labels(cp["field"] or cp["claim"])
            if not roles and len(labels)<2:
                continue
            src=self.dag.get(cp["source_id"])["payload"]
            nearby=src["locator"]+" "+src["text"][max(0,cp["start"]-250):cp["end"]+160]
            if not entity_anchor(cp["entity"],nearby):
                continue
            original=claim_quantities(cp)
            for vp,raw_value,value in numeric_leaves(cp["value"]):
                for q in original:
                    if q.value==value:
                        index.setdefault(q.canonical,[]).append((n,q,roles,labels,vp,raw_value))
        bound={(r["locator"],r["claim_id"],tuple(r.get("value_path",()))) for r in already}
        links=[]
        for loc,raw in slots.items():
            if loc.startswith("native:") or not isinstance(raw,str) or raw.startswith("="):
                continue
            for q in document_quantities(raw,document_unit):
                matches={}
                for n,source,roles,labels,vp,raw_value in index.get(q.canonical,[]):
                    cp=n["payload"]
                    if not amounts_match(q,source) or not entity_anchor(cp["entity"],q.context):
                        continue
                    if not (roles & metrics(q.context) or len(labels & semantic_labels(q.context))>=2):
                        continue
                    k=(cp["entity"],cp["field"],str(cp["scope"]),vp,str(raw_value))
                    matches[k]=(n,vp,raw_value)
                if len(matches)!=1:
                    continue
                hit,vp,raw_value=next(iter(matches.values()))
                pair=(loc,hit["id"],tuple(vp))
                if pair not in bound:
                    bound.add(pair)
                    links.append({"locator":loc,"claim_id":hit["id"],"expected":raw_value,"value_path":list(vp),
                                  "match":"contains","projection_receipt":None,"method":"unique entity/metric/unit match"})
        return links

    def _recalculate(self, native, payload, original_slots):
        receipt = recalculate_workbook(native, self.store / "recalculation")
        if receipt["status"] != "complete" or not receipt["no_errors_verified"]:
            return {"status":"unresolved","backend":receipt["backend"],"reason":receipt.get("reason","Formula count or cached error check failed"),
                    "error_count":receipt.get("observed_backend_error_count")}
        # Only copy validated caches back. A LibreOffice round trip can silently
        # modify styles, labels, named ranges and even formula syntax.
        derived = Path(receipt["derived_path"])
        _merge_formula_caches(native, derived)
        if _slots(native) != original_slots:
            raise ValueError("Recalculation changed protected native content")
        return {"status":"complete","backend":receipt["backend"],"backend_version":receipt["backend_version"],
                "formula_count":receipt["derived"]["formula_count"],"derived_sha256":receipt["derived_sha256"]}

    def _protected_points(self, p, digest, conflicts):
        from .protected_replacement import assess_replacement
        def read_slots(path, sha):
            return _slots(self._blob(path, sha))
        return assess_replacement(self.dag, p, self.dag.get(p["contract_id"])["payload"],
                                  read_slots(p["path"], digest) if digest else {}, digest,
                                  conflicts, read_slots)

    def _verdict(self, p, attempt, digest, state, conflicts=(), suggestions=(), reasons=(), calc=None):
        if self.self_review:
            protected = self._protected_points(p,digest,conflicts)
            p["protected_replacement"] = protected
            lost = set(p.get("removed_native_slots", []))
            if lost:
                source_sha = self.dag.get(p["source_id"])["payload"]["sha256"]
                accounted = {row["locator"] for row in protected["retained"] + protected["retired"]
                             if row["origin_sha256"] == source_sha}
                lost -= accounted
                if lost:
                    reasons = [*reasons, {"removed_material_slots_require_retirement":sorted(lost)[:100],"total":len(lost)}]
                    if state == "ready":
                        state = "unresolved"
            if protected["missing"]:
                reasons = [*reasons, *({"protected_point_missing":row} for row in protected["missing"])]
                if state == "ready":
                    state = "unresolved"
        cids = [c["conflict_id"] for c in conflicts if "conflict_id" in c]
        uses = list(p.get("consumed_claim_ids",[])) + list(p.get("analysis_input_ids",[]))
        uses.extend(o["obligation_id"] for o in p.get("obligation_checks",[]))
        uses.extend(p[k] for k in ("finance_receipt_id","proof_reused_from") if p.get(k))
        uses.extend(p.get("import_receipts", []))
        preservation = p.get("protected_replacement", {})
        uses.extend(row["origin_check"] for row in preservation.get("points", []) if row.get("origin_check"))
        uses.extend(row["revision_id"] for row in preservation.get("retired", []))
        if p.get("repair_of"):
            prior = self.dag.get(p["repair_of"])
            before = prior["payload"]
            # The claim ids of derived relations are versioned. A remaining finding
            # at the same locator/rule is still open even if both sides got new ids.
            cleared = [c for c in before["conflicts"] if not any(
                i.get("locator")==c.get("locator") and i.get("rule")==c.get("rule") for i in conflicts)]
            changed = p.get("repair_changed",[])
            category = ("binding_replaced" if p.get("replaced_binding") else "native_state_changed" if changed
                        else "evidence_updated" if p["bindings"] != before["bindings"] else "rechecked")
            p["repair_outcome"] = {"before_id":prior["id"],"before_sha256":before["sha256"],"after_sha256":digest,
                "kind":category,"changed_slots":changed,"cleared_conflict_ids":[c.get("conflict_id") for c in cleared],
                "cleared_obligation_ids":[o["obligation_id"] for o in before.get("obligation_checks",[])
                    if o["status"]!="satisfied" and any(v["obligation_id"]==o["obligation_id"] and v["status"]=="satisfied"
                                                       for v in p.get("obligation_checks",[]))],
                "decision":"accept" if state=="ready" else "defer","remaining_conflicts":len(conflicts)}
        decision = self.dag.append("artifact_check", {**p,"sha256":digest,"status":state,"conflicts":list(conflicts),
            "suggested_changes":list(suggestions),"reasons":list(reasons),"recalculation":calc},
            list(dict.fromkeys([attempt["id"], *cids, *uses, *(r["claim_id"] for r in p["bindings"])])))
        response={"proposal_id":decision["id"], **{k: decision["payload"][k] for k in
                ("status","path","purpose","sha256","conflicts","suggested_changes","reasons","recalculation")}}
        if reasons and digest and p["action"]=="create":
            response["next_step"]="Inspect this proposal with artifact_read(path,proposal_id); locate facts in the actual native file. Add matching claim bindings with artifact_repair; for a new draft submit corrected content as a fresh artifact_propose."
            if any(isinstance(row,dict) and ("unbound_material_slots" in row or "uncovered_numbers" in row) for row in reasons):
                response["candidate_bindings"]=self._binding_candidates({"proposal_id":decision["id"],"limit":1})["slots"]
        if self.self_review:
            response["coverage_gaps"]=p.get("coverage_gaps",[])
            response["obligations"] = [{"kind":o["kind"],"status":o["status"],"locators":o.get("locators",[])[:5]}
                                       for o in p.get("obligation_checks",[])]
            if p.get("repair_outcome"):
                response["repair_outcome"] = p["repair_outcome"]
            response["self_review"] = ({"status":"proof_reused","from_version":p["proof_reused_from"],
                    "instruction":"Commit this identical same-use mirror using proposal_id."} if p.get("proof_reused_from")
                else self._review_artifact(decision))
        # Keep the common review response bounded. Every omitted record stays
        # addressable in this proposal by kind and offset, including witnesses.
        if self.self_review:
            pages = {}
            for category, limit in (("reasons",4),("conflicts",4),("coverage_gaps",3),("suggested_changes",12)):
                rows = response.get(category, [])
                if len(rows) > limit:
                    pages[category] = {"total":len(rows),"next_offset":limit}
                    response[category] = rows[:limit]
            if pages:
                response["remaining_findings"] = pages
                response["findings_tool"] = "Read artifact_findings(proposal_id,category,offset) for the next page, or category=bindings for the existing ledger edge IDs."
        return response

    def _review_artifact(self, proposal, query=None, additional=False):
        from .live_review import present, review
        if proposal["kind"]!="artifact_check":
            raise ValueError("Review a staged and checked native proposal")
        p=proposal["payload"]
        contract=self.dag.get(p["contract_id"])["payload"]
        # Use the proposal's actual input edges and trusted current-purpose
        # facts. Never use the staged file as its own business authority.
        anchors=list(dict.fromkeys([*p.get("input_claim_ids",[]),
                *(r["claim_id"] for r in contract["rules"]),
                *(r["claim_id"] for r in p["bindings"])]))
        prior=[r for r in self.dag.nodes("ledger_self_review") if r["payload"]["target_id"]==proposal["id"]]
        if prior and not additional:
            return self._review_display(proposal,prior[-1])
        # A newly mapped acceptance criterion is a new review context, even
        # when the same original window is still the appropriate evidence.
        mappings = [n for n in self.dag.nodes('work_item_update')
                    if n['payload'].get('state') == 'mapped'
                    and any(t['path'] == p['path'] for t in n['payload'].get('targets', []))
                    and self.dag.get(n['payload']['request_id'])['payload']['stage'] == p['stage']]
        if additional and prior and mappings and mappings[-1]['seq'] > prior[-1]['seq']:
            prior = []
        business = [i for i in (p.get("consumed_claim_ids",[]) or p.get("analysis_input_ids",[]))
                    if is_business_claim(self.dag.get(i))]
        if not business:
            business = [i for i in anchors if is_business_claim(self.dag.get(i))]
        business = self._review_inputs(business)
        evidence = [self.dag.get(i)["payload"] for i in business[:4]]
        target_query = query or " ".join(f"{c['entity']} {c['field']} {c['claim']}" for c in evidence) or p["purpose"]
        # Start from the actual checked consumer. A bare claim's citation alone
        # cannot establish that the file used the evidence. The ledger arm
        # receives the same query and source universe without traversing it.
        node=review(self.dag,proposal["id"],target_query,[proposal["id"]],stage=p["stage"],previous=prior,
                    purpose_role="business_evidence",policy=getattr(self,"review_policy","ledger_graph"))
        return self._review_display(proposal,node)

    def _review_display(self, proposal, node):
        from .live_review import present
        view = present(node)
        if not node:
            return view
        p,window = proposal["payload"],node["payload"]
        candidates = p.get("consumed_claim_ids") or p.get("analysis_input_ids", [])
        offers = []
        for cid in candidates:
            native = [r["locator"] for r in p.get("bindings",[]) if r["claim_id"]==cid]
            native += [r["locator"] for r in self.dag.get(p["contract_id"])["payload"]["rules"] if r["claim_id"]==cid]
            if p.get("finance_receipt_id"):
                native += [c["locator"] for c in self.dag.get(p["finance_receipt_id"])["payload"]["checks"] if c.get("claim_id")==cid]
            for source_id in self._review_inputs([cid]):
                src = self.dag.get(source_id)["payload"]
                if (src["source_id"]!=window["source_id"] or src["start"]<window["start"] or src["end"]>window["end"]
                        or not admissible_text(src)):
                    continue
                offers.append({"consumer_claim_id":cid,"source_claim_id":source_id,"review_quote":src["quote"],
                               "source_locator":src["locator"],"start":src["start"],"end":src["end"],
                               "native_locators":sorted(set(native)),"role":"checked_consumer" if native else "analysis_input"})
        view["witnesses"] = offers[:8]
        view["signature_guidance"] = ("Use a returned review_quote if it supports your decision, or select another window. "
                                      "Analysis inputs only support the explicitly disclosed interpretation and limitations.")
        return view

    def _ensure_uncontested(self, check):
        p = check["payload"]
        current = self._protected_points(p,p["sha256"],p["conflicts"])
        if current["missing"]:
            first = current["missing"][0]
            raise ValueError(f"Protected {first['claim_id']} at {first['locator']} disappeared; "
                             "restore its verified use or record an evidence-backed revision")
        same = [n for n in self.dag.nodes("artifact_check") if n["payload"]["path"]==p["path"]
                and n["payload"]["contract_id"]==p["contract_id"] and n["payload"]["sha256"]==p["sha256"]]
        failures = [n for n in same if n["payload"]["status"]!="ready"]
        if not failures:
            return
        chain = set()
        parent = check["payload"].get("repair_of")
        while parent:
            chain.add(parent)
            parent = self.dag.get(parent)["payload"].get("repair_of")
        bindings = {(r["locator"], r["claim_id"],tuple(r.get("value_path",[]))) for r in p["bindings"]}
        replaced = set()
        for n in [check, *(self.dag.get(i) for i in chain)]:
            edge = n["payload"].get("replaced_binding")
            if edge:
                replaced.add((edge["locator"],edge["old_claim_id"],tuple(edge.get("old_value_path",[]))))
        for n in failures:
            former = {(r["locator"],r["claim_id"],tuple(r.get("value_path",[]))) for r in n["payload"]["bindings"]}
            # A later successful check of the same bytes can discharge a
            # duplicate failed branch only when it tested every extra witness.
            # Model-supplied business assertions cannot silently disappear.
            covered = (n["seq"] < check["seq"] and former <= bindings | replaced
                       and set(n["payload"].get("input_claim_ids",[])) <= set(p.get("input_claim_ids",[])))
            if n["id"] not in chain and not covered:
                raise ValueError(f"Same-version check {n['id']} is unresolved; repair that branch before submission")

    def _verify_current_proof_scope(self, payload, contract):
        current = proof_scope(contract, payload.get("obligation_checks", []),
                              getattr(self, "review_policy", "ledger_graph"))
        if payload.get("proof_scope") != current:
            raise ValueError("Business evidence checks changed. Revalidate this version through a fresh native proposal.")

    def validate_head(self, path):
        """Recheck the actual head and the still-applicable evidence for stage submission."""
        path = _path(path)
        head = self._require(path)
        p = head["payload"]
        contract = self._contract(path)
        if not contract or contract["id"] != p.get("contract_id"):
            raise ValueError("Committed artifact must be revalidated for the current contract")
        file = self.materialized(path)
        if not file.is_file() or _sha(file) != p["sha256"]:
            raise ValueError("Materialized file does not match validated version")
        if self.self_review:
            self._verify_current_proof_scope(p,contract)
            if not p.get("proof_id"):
                raise ValueError("Current self-review requires a business proof for the committed file")
            self._ensure_uncontested(self.dag.get(p["proposal_id"]))
            proof = self.dag.get(p["proof_id"])
            if (proof["payload"].get("sha256") != p["sha256"]
                    or proof["payload"].get("proof_scope") != p["proof_scope"]):
                raise ValueError("Committed proof belongs to a different version or use")
        from .dependency_queries import DependencyQueries
        dependencies = DependencyQueries(self.dag).dependencies_of(head)
        if any(not dependency['applicable'] for dependency in dependencies):
            raise ValueError("A committed consumer depends on a superseded or pending claim")
        return head

    def _commit(self, proposal_id, review_id=None, review_quote=None, review_note=None):
        done = self._last("artifact_version","proposal_id",proposal_id)
        if done:
            if self.self_review and self.head(done["payload"]["path"])["id"] == done["id"] and done["payload"].get("sha256"):
                self.validate_head(done["payload"]["path"])
            return {"status":"committed","node_id":done["id"],"path":done["payload"]["path"],"sha256":done["payload"]["sha256"]}
        check = self.dag.get(proposal_id)
        if not check or check["kind"] != "artifact_check" or check["payload"]["status"] != "ready":
            raise ValueError("Commit requires a ready, verified native proposal")
        p = check["payload"]
        contract = self._contract(p["path"])
        if contract["id"] != p["contract_id"]:
            raise ValueError("Purpose or evidence changed; recheck required")
        from .dependency_queries import DependencyQueries
        dependencies = DependencyQueries(self.dag).dependencies_of(check)
        if self.self_review:
            self._verify_current_proof_scope(p,contract)
            self._ensure_uncontested(check)
        if any(not dependency['applicable'] for dependency in dependencies):
            raise ValueError("Source ledger claim changed; recheck required")
        old = self.head(p["path"])
        if (old["id"] if old else None) != p["old_id"]:
            raise ValueError("Destination head changed; recheck required")
        if p["source_id"]:
            original = self.dag.get(p["source_id"])
            if self._require(original["payload"]["path"])["id"] != original["id"]:
                raise ValueError("Source head changed; recheck required")
        # Check native destination before recording an intent so an externally
        # modified file cannot leave a permanently unfulfillable intent.
        dest = self.materialized(p["path"])
        if (_sha(dest) if dest.is_file() else None) != p["previous_sha256"]:
            raise ValueError("Materialized head differs from graph")
        if p["sha256"]:
            self._blob(p["path"],p["sha256"])
        decision_parents=[proposal_id]
        if self.self_review and p.get("proof_reused_from"):
            original = self.validate_head(self.dag.get(p["proof_reused_from"])["payload"]["path"])
            if original["id"] != p["source_id"] or original["payload"]["sha256"] != p["sha256"]:
                raise ValueError("Mirror source changed. Prepare a fresh copy proposal")
            proof = self.dag.append("artifact_mirror_proof",{"source_version":original["id"],"proposal_id":proposal_id,
                "sha256":p["sha256"],"proof_scope":p["proof_scope"],"purpose":p["purpose"]},
                [proposal_id,original["id"],original["payload"]["proof_id"]])
            p["proof_id"] = proof["id"]
            decision_parents.append(proof["id"])
        elif self.self_review:
            if not review_id:
                raise ValueError("Read the proposal self_review and supply review_id, review_quote and review_note")
            checked=self.dag.get(review_id)
            if (checked["kind"]!="ledger_self_review" or checked["payload"]["target_id"]!=proposal_id
                    or not isinstance(review_quote,str) or not review_quote.strip()
                    or review_quote not in checked["payload"]["text"] or not (review_note or "").strip()):
                raise ValueError("Supply this proposal's actual review ID, an exact original quote and a review decision")
            candidates = p.get("consumed_claim_ids",[]) or p.get("analysis_input_ids",[])
            if p["action"] == "delete":
                candidates = [i for i in contract["parents"] if self.dag.get(i)["kind"]=="claim"]
            support = supporting_quote(self.dag,checked["payload"],review_quote,self._review_inputs(candidates))
            if not support:
                raise ValueError("The exact original must support a business claim consumed by the checked proposal")
            attestation=self.dag.append("artifact_review_decision",{"proposal_id":proposal_id,"review_id":review_id,
                      "quote":review_quote,"description":review_note,"stage":p["stage"],"purpose":p["purpose"],
                      "sha256":p["sha256"],"proof_scope":p.get("proof_scope"),"witness_claim_id":support,
                      "evidence_role":"checked_consumer" if p.get("consumed_claim_ids") else "analysis_input",
                      "calculation_receipts":[r["calculation_receipt"] for r in p["bindings"] if r.get("calculation_receipt")],
                      "coverage_gaps":p.get("coverage_gaps",[])},[proposal_id,review_id,support])
            p["proof_id"] = attestation["id"]
            decision_parents.append(attestation["id"])
        intent = self.dag.append("artifact_commit_intent", {**p,"proposal_id":proposal_id}, decision_parents)
        committed = self._finish(intent)
        return {"status":"committed","node_id":committed["id"],"path":p["path"],"sha256":p["sha256"]}


def _merge_formula_caches(native, derived):
    """Install verified scalar formula values by logical sheet name and address.

    openpyxl's writer uses one c/f/v sequence per local formula. Preserve every
    other XML member, all formulas and all non-formula input values verbatim.
    """
    from zipfile import ZipFile
    from lxml import etree as ET
    sheets = _slots(derived, cached=True)
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    office = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    staged = native.with_name(".cached.xlsx")
    with ZipFile(native) as source, ZipFile(staged,"w") as output:
        workbook = ET.fromstring(source.read("xl/workbook.xml"))
        rels = {r.get("Id"):r.get("Target") for r in ET.fromstring(source.read("xl/_rels/workbook.xml.rels"))}
        parts = {}
        for sheet in workbook.iter(ns+"sheet"):
            target = rels[sheet.get(office+"id")]
            parts[target.lstrip("/") if target.startswith("/") else "xl/"+target] = sheet.get("name")
        for member in source.infolist():
            raw = source.read(member)
            if member.filename in parts:
                root = ET.fromstring(raw)
                changed = False
                for cell in root.iter(ns+"c"):
                    if cell.find(ns+"f") is None:
                        continue
                    key = f"{parts[member.filename]}!{cell.get('r')}"
                    if key not in sheets:
                        raise ValueError(f"Recalculated formula has no observable cache: {key}")
                    value = sheets[key]
                    slot = cell.find(ns+"v")
                    if slot is None:
                        slot = ET.SubElement(cell, ns+"v")
                    if isinstance(value,bool):
                        cell.set("t","b"); slot.text = "1" if value else "0"
                    elif isinstance(value,(int,float)):
                        cell.set("t","n"); slot.text = str(value)
                    elif isinstance(value,str):
                        cell.set("t","str"); slot.text = value
                    else:
                        raise ValueError(f"Unsupported formula cache type at {key}")
                    changed = True
                if changed:
                    raw = ET.tostring(root, encoding="utf-8")
            output.writestr(member,raw)
    os.replace(staged,native)

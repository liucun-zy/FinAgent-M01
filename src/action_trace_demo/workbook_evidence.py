"""Bounded OOXML inspection and isolated LibreOffice-derived calculation evidence.

These are evidence helpers, not an Excel compatibility claim or a rubric judge.
Callers must enforce their own stage-visible path allowlist. No original file is
written, and the executable/arguments are never supplied by a model tool call.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile


S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
P = "{http://schemas.openxmlformats.org/package/2006/relationships}"
POLICY_VERSION = "ooxml_workbook_evidence_v1"
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_XML_BYTES = 32 * 1024 * 1024
MAX_PACKAGE_BYTES = 128 * 1024 * 1024
MAX_MEMBERS = 10000
MAX_TEXT_CHARS = 16 * 1024 * 1024
REMOTE_FORMULA = re.compile(r"(?i)(?:\b(?:WEBSERVICE|HYPERLINK|IMAGE|RTD|CALL|REGISTER(?:\.ID)?|DDE|EVALUATE|EXEC|FOPEN|FREAD|FWRITE|SQL\.REQUEST)\s*\(|\|[^!]*!|\[[^\]]+\.xlsx?\]|\[\d+\][^!]*!|(?:https?|ftp|file)://)")


class WorkbookEvidenceError(ValueError):
    """Unavailable evidence must not be interpreted as an error-free workbook."""


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _original(path: Path) -> tuple[bytes, str]:
    if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".xlsx":
        raise WorkbookEvidenceError("Expected a regular non-symlink XLSX file")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise WorkbookEvidenceError("Workbook exceeds 32 MiB package limit")
    data = path.read_bytes()
    return data, hashlib.sha256(data).hexdigest()


def _archive(data: bytes) -> ZipFile:
    archive = ZipFile(BytesIO(data))
    members = archive.infolist()
    if len(members) > MAX_MEMBERS or len({m.filename for m in members}) != len(members):
        archive.close(); raise WorkbookEvidenceError("Too many or duplicate ZIP members")
    if sum(m.file_size for m in members) > MAX_PACKAGE_BYTES:
        archive.close(); raise WorkbookEvidenceError("Uncompressed package exceeds limit")
    for member in members:
        name = member.filename
        if name.startswith("/") or "\\" in name or ".." in name.split("/") or member.flag_bits & 1:
            archive.close(); raise WorkbookEvidenceError("Noncanonical or encrypted ZIP member")
    return archive


def _xml(archive: ZipFile, name: str) -> ET.Element:
    member = archive.getinfo(name)
    if member.file_size > MAX_XML_BYTES:
        raise WorkbookEvidenceError("XML component exceeds limit: " + name)
    raw = archive.read(member)
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise WorkbookEvidenceError("DTD/entity declarations are unsupported")
    return ET.fromstring(raw)


def _target(base: str, target: str) -> str:
    path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(base), target))
    if path.startswith("../") or path == ".." or "\\" in path:
        raise WorkbookEvidenceError("Noncanonical relationship target")
    return path


def _rels(archive: ZipFile, part: str) -> list[dict]:
    folder, name = posixpath.split(part)
    path = posixpath.join(folder, "_rels", name + ".rels")
    if path not in archive.namelist(): return []
    return [dict(row.attrib) for row in _xml(archive, path).findall(P + "Relationship")]


def _text(element: ET.Element | None) -> str:
    if element is None: return ""
    chunks = []
    def visit(node):
        if node.tag == S + "rPh": return
        if node.tag.rsplit("}", 1)[-1] == "t": chunks.append(node.text or ""); return
        for child in node: visit(child)
    visit(element)
    return "".join(chunks)


def _coord(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})", value)
    if not match: raise WorkbookEvidenceError("Invalid cell/range coordinate")
    column = 0
    for char in match[1]: column = column * 26 + ord(char) - 64
    row = int(match[2])
    if column > 16384 or row > 1048576: raise WorkbookEvidenceError("Cell coordinate exceeds Excel limits")
    return row, column


def _scan(data: bytes, *, max_cells: int, max_formulas: int, max_errors: int,
          max_comments: int = 5000, max_text_chars: int = MAX_TEXT_CHARS) -> tuple[dict, dict, str]:
    if any(isinstance(x, bool) or not isinstance(x, int) or x < 1 for x in (max_cells, max_formulas, max_errors, max_comments, max_text_chars)):
        raise WorkbookEvidenceError("Inspection bounds must be positive integers")
    cells = []; formulas = []; errors = []; comments = []; sheets = []; warnings = []; truncations = []
    qualifying = []; total_literal = 0; formula_count = 0; error_count = 0; comment_count = 0
    unsafe = []; external_relationships = []; external_refs = []; comment_sheets = {}
    with _archive(data) as archive:
        names = archive.namelist()
        workbook = _xml(archive, "xl/workbook.xml")
        calc = workbook.find(S + "calcPr")
        calc_attrs = dict(calc.attrib) if calc is not None else {}
        calc_pr = {"attributes": calc_attrs, "iterate_present": "iterate" in calc_attrs,
                   "iteration_enabled": calc_attrs.get("iterate", "0").lower() in {"1", "true"},
                   "iteration_default_when_absent": False, "cycle_analysis": "not_performed; flags and absence of cached errors do not prove acyclicity"}
        links = sorted(n for n in names if n.startswith("xl/externalLinks/") and n.endswith(".xml") and "/_rels/" not in n)
        link_components = sorted(n for n in names if n.startswith("xl/externalLinks/"))
        for part in names:
            if part.endswith(".rels"):
                for row in _xml(archive, part).findall(P + "Relationship"):
                    if row.attrib.get("TargetMode") == "External":
                        external_relationships.append({"part": part, **row.attrib})
        for reference in workbook.findall(S + "externalReferences/" + S + "externalReference"):
            external_refs.append(dict(reference.attrib))
        active_parts = sorted(n for n in names if any(term in n.lower() for term in ("vbaproject", "macrosheet", "activex", "oleobject", "connections.xml", "querytables/")))
        if links or external_refs: unsafe.append("external_workbook_links")
        if external_relationships: unsafe.append("external_relationships_are_not_fetched")
        if active_parts: unsafe.append("active_content_or_data_connections")
        shared = []
        if "xl/sharedStrings.xml" in names:
            shared = [_text(si) for si in _xml(archive, "xl/sharedStrings.xml").findall(S + "si")]
        relation_by_id = {r["Id"]: r for r in _rels(archive, "xl/workbook.xml")}
        defined_names = [{"name": node.attrib.get("name"), "attributes": dict(node.attrib), "value": node.text or ""}
                         for node in workbook.findall(S + "definedNames/" + S + "definedName")]
        if any(REMOTE_FORMULA.search(n["value"]) for n in defined_names): unsafe.append("active_or_remote_defined_name")
        for sheet in workbook.findall(S + "sheets/" + S + "sheet"):
            name = sheet.attrib["name"]
            relation = relation_by_id[sheet.attrib[R + "id"]]
            if relation.get("TargetMode") == "External": raise WorkbookEvidenceError("External sheet target")
            part = _target("xl/workbook.xml", relation["Target"])
            if not relation.get("Type", "").endswith("/worksheet"):
                warnings.append("non_worksheet_sheet:" + name)
                sheets.append({"sheet": name, "part": part, "state": sheet.attrib.get("state", "visible"), "status": "requires_review"})
                continue
            root = _xml(archive, part)
            merges = []
            for merge in root.findall(S + "mergeCells/" + S + "mergeCell"):
                refs = merge.attrib["ref"].split(":")
                first = _coord(refs[0]); last = _coord(refs[-1])
                merges.append((first, last))
            if len(merges) > 1000:
                warnings.append("merged_range_limit:" + name); merges = merges[:1000]; truncations.append("merged_ranges")
            for rel in _rels(archive, part):
                if rel.get("Type", "").rsplit("/", 1)[-1] in {"comments", "threadedComment", "threadedComments"}:
                    comment_sheets.setdefault(_target(part, rel["Target"]), []).append(name)
            sheet_cells = 0; sheet_formulas = 0; sheet_errors = 0; sheet_qualifying = 0; sheet_truncated = False
            seen = set()
            for cell in root.findall(S + "sheetData/" + S + "row/" + S + "c"):
                if len(cells) >= max_cells:
                    sheet_truncated = True
                    if "cells" not in truncations: truncations.append("cells")
                    break
                coordinate = cell.attrib["r"]
                rc = _coord(coordinate)
                if coordinate in seen: raise WorkbookEvidenceError("Duplicate worksheet cell")
                seen.add(coordinate)
                formula = cell.find(S + "f")
                value = cell.find(S + "v")
                kind = cell.attrib.get("t", "n")
                raw_value = value.text if value is not None else None
                literal = None
                if kind == "s":
                    if raw_value is None: raise WorkbookEvidenceError("Shared string cell has no index")
                    index = int(raw_value)
                    if not 0 <= index < len(shared): raise WorkbookEvidenceError("Invalid shared string index")
                    literal = shared[index]
                elif kind == "inlineStr": literal = _text(cell.find(S + "is"))
                elif kind == "str": literal = raw_value or ""
                merged_continuation = any(a[0] <= rc[0] <= b[0] and a[1] <= rc[1] <= b[1] and rc != a for a, b in merges)
                row = {"sheet": name, "coordinate": coordinate, "cell_type": kind,
                       "value": literal if literal is not None else raw_value,
                       "formula": None if formula is None else (formula.text or ""),
                       "formula_attributes": {} if formula is None else dict(formula.attrib),
                       "merged_continuation": merged_continuation}
                if formula is not None:
                    formula_count += 1; sheet_formulas += 1
                    if len(formulas) < max_formulas: formulas.append(row)
                    elif "formula_records" not in truncations: truncations.append("formula_records")
                    if REMOTE_FORMULA.search(formula.text or ""): unsafe.append("active_or_remote_formula")
                elif literal is not None and not merged_continuation:
                    total_literal += 1
                    if len(literal) > 150:
                        qualifying.append({"sheet": name, "coordinate": coordinate, "characters": len(literal)})
                        sheet_qualifying += 1
                if merged_continuation and literal:
                    warnings.append("value_in_non_anchor_merged_cell:" + name + "!" + coordinate)
                if kind == "e":
                    error_count += 1; sheet_errors += 1
                    if len(errors) < max_errors: errors.append(row)
                    elif "error_records" not in truncations: truncations.append("error_records")
                cells.append(row); sheet_cells += 1
            sheets.append({"sheet": name, "part": part, "state": sheet.attrib.get("state", "visible"),
                           "status": "requires_review" if sheet_truncated else "complete", "observed_cells": sheet_cells,
                           "observed_formula_cells": sheet_formulas, "observed_error_cells": sheet_errors,
                           "observed_qualifying_literal_cells": sheet_qualifying, "merged_ranges": len(merges), "truncated": sheet_truncated})
        comment_parts = sorted(n for n in names if n.endswith(".xml") and (posixpath.basename(n).lower().startswith("comments") or "/comments/" in n.lower() or "/threadedcomments/" in n.lower()))
        for part in comment_parts:
            root = _xml(archive, part)
            authors = [n.text or "" for n in root.findall(S + "authors/" + S + "author")]
            for node in root.iter():
                local = node.tag.rsplit("}", 1)[-1]
                if local not in {"comment", "threadedComment"}: continue
                comment_count += 1
                body = _text(node)
                if not body:
                    body = "".join(n.text or "" for n in node if n.tag.rsplit("}", 1)[-1] == "text")
                author_id = node.attrib.get("authorId")
                author = authors[int(author_id)] if author_id and author_id.isdigit() and int(author_id) < len(authors) else author_id
                row = {"part": part, "sheets": comment_sheets.get(part, []), "coordinate": node.attrib.get("ref"),
                       "kind": local, "author": author, "text": body}
                if len(comments) < max_comments: comments.append(row)
                elif "comment_records" not in truncations: truncations.append("comment_records")
    complete_counts = "cells" not in truncations and "merged_ranges" not in truncations and not any(w.startswith("non_worksheet_sheet") for w in warnings)
    detail = {"cells": cells, "formula_cells": formulas, "stored_error_cells": errors, "comments": comments,
              "defined_names": defined_names, "external_relationships": external_relationships}
    lines = ["OOXML evidence; stored values are caches until a documented backend recalculates them."]
    for row in cells:
        lines.append(json.dumps({"sheet": row["sheet"], "coordinate": row["coordinate"], "type": row["cell_type"],
                                "formula": row["formula"], "formula_attributes": row["formula_attributes"], "stored_value": row["value"]}, ensure_ascii=False))
    for row in comments: lines.append("COMMENT " + json.dumps(row, ensure_ascii=False))
    for row in defined_names: lines.append("DEFINED_NAME " + json.dumps(row, ensure_ascii=False))
    text = "\n".join(lines)
    if len(text) > max_text_chars:
        text = text[:max_text_chars]; truncations.append("text_characters")
    def sample(row):
        return {k: (v[:400] + " [sample truncated]" if isinstance(v, str) and len(v) > 400 else v) for k, v in row.items()}
    summary = {"status": "requires_review" if truncations or warnings else "complete", "policy_version": POLICY_VERSION,
        "format": "xlsx", "truncated": bool(truncations), "truncations": sorted(set(truncations)),
        "counts_complete": complete_counts, "sheet_count": len(sheets), "sheets": sheets, "calcPr": calc_pr,
        "external_link_parts": links, "external_link_components": link_components, "external_references": external_refs,
        "external_relationship_count": len(external_relationships), "external_relationship_samples": [sample(r) for r in external_relationships[:50]],
        "active_content_parts": active_parts, "recalculation_blockers": sorted(set(unsafe)),
        "observed_cell_count": len(cells), "formula_count": formula_count if complete_counts else None,
        "observed_formula_count": formula_count, "stored_error_count": error_count if complete_counts else None,
        "observed_stored_error_count": error_count, "formula_samples": [sample(r) for r in formulas[:50]],
        "stored_error_samples": [sample(r) for r in errors[:100]], "comment_count": comment_count,
        "comment_samples": [sample(r) for r in comments[:50]],
        "literal_text_cell_stats": {"profile": "unicode_codepoints_including_whitespace_and_punctuation_gt_150",
            "threshold_chars": 150, "comparison": ">", "formula_results_excluded": True, "merged_cells_counted_once": True,
            "observed_literal_text_cells": total_literal, "qualifying_count": len(qualifying) if complete_counts else None,
            "observed_qualifying_count": len(qualifying), "qualifying_samples": qualifying[:50], "normalization": "none"},
        "sample_policy": "bounded samples only; use detail JSON/text for complete processed content",
        "warnings": sorted(set(warnings)), "text_complete": not truncations,
        "limitations": ["Stored formula values are not evidence of fresh recalculation.",
                        "No dependency-cycle proof is attempted; iteration flags and errors require contextual review.",
                        "Shared/array formula XML is reported with attributes, without inventing expanded formulas."]}
    return summary, detail, text


def inspect_workbook(path: Path, *, cache_dir: Path | None = None, max_cells: int = 100000,
                     max_formulas: int = 20000, max_errors: int = 5000, max_comments: int = 5000,
                     max_text_chars: int = MAX_TEXT_CHARS) -> dict:
    """Inspect original package, formula caches, literal text and comment evidence."""
    path = Path(path)
    digest = None
    try:
        data, digest = _original(path)
        summary, detail, text = _scan(data, max_cells=max_cells, max_formulas=max_formulas,
            max_errors=max_errors, max_comments=max_comments, max_text_chars=max_text_chars)
        summary.update(original_sha256=digest, original_unchanged=_hash(path) == digest)
        if not summary["original_unchanged"]: raise WorkbookEvidenceError("Original changed while inspecting")
        if cache_dir is not None:
            cache_identity = hashlib.sha256(json.dumps([digest, max_cells, max_formulas, max_errors, max_comments, max_text_chars, _hash(Path(__file__))]).encode()).hexdigest()
            destination = Path(cache_dir) / cache_identity
            destination.mkdir(parents=True, exist_ok=True)
            detail_path = destination / "inspection.json"; text_path = destination / "inspection.txt"
            _write_json(detail_path, {"summary": summary, **detail})
            temporary_text = text_path.with_suffix(".txt.tmp")
            temporary_text.write_text(text, encoding="utf-8"); temporary_text.replace(text_path)
            summary.update(detail_json_path=str(detail_path.resolve()), detail_json_sha256=_hash(detail_path),
                           inspection_text_path=str(text_path.resolve()), inspection_text_sha256=_hash(text_path))
        return summary
    except (WorkbookEvidenceError, BadZipFile, KeyError, ValueError, OSError, ET.ParseError) as exc:
        return {"status": "unavailable", "original_sha256": digest, "error_type": type(exc).__name__,
                "reason": str(exc)[:500], "truncated": False, "no_errors_verified": False}


def _force_calculation(data: bytes, path: Path) -> dict:
    # Preserve all package members; change only calculation flags in this
    # disposable input. In particular, do not enable/change iteration.
    with _archive(data) as source:
        workbook = _xml(source, "xl/workbook.xml")
        calc = workbook.find(S + "calcPr")
        old = dict(calc.attrib) if calc is not None else {}
        changes = {"calcMode": "auto", "fullCalcOnLoad": "1", "forceFullCalc": "1"}
        raw = source.read("xl/workbook.xml")
        # XML serialization can rewrite namespace prefixes and orphan prefixes
        # named in mc:Ignorable. Patch this one start tag while retaining all
        # other workbook XML bytes, including declarations and extension data.
        tag_pattern = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?calcPr\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*>")
        tags = list(tag_pattern.finditer(raw))
        if calc is not None:
            if len(tags) != 1: raise WorkbookEvidenceError("Ambiguous calcPr XML tags")
            match = tags[0]; tag = match[0]
            for key, value in changes.items():
                attribute = re.compile(rb"(?<![\w:.-])" + key.encode() + rb"\s*=\s*([\"'])(.*?)\1")
                replacement = key.encode() + b'="' + value.encode() + b'"'
                if attribute.search(tag): tag = attribute.sub(lambda _: replacement, tag, count=1)
                else:
                    end = tag.rfind(b"/>") if tag.endswith(b"/>") else len(tag) - 1
                    tag = tag[:end] + b" " + replacement + tag[end:]
            modified = raw[:match.start()] + tag + raw[match.end():]
        else:
            close = re.search(rb"</(?P<prefix>(?:[A-Za-z_][\w.-]*:)?)workbook\s*>", raw)
            if not close: raise WorkbookEvidenceError("Cannot locate workbook closing tag")
            tag = b"<" + close["prefix"] + b'calcPr calcMode="auto" fullCalcOnLoad="1" forceFullCalc="1"/>'
            modified = raw[:close.start()] + tag + raw[close.start():]
        with ZipFile(path, "w", compression=ZIP_DEFLATED) as target:
            for member in source.infolist():
                target.writestr(member, modified if member.filename == "xl/workbook.xml" else source.read(member))
    return {"changed_component": "xl/workbook.xml", "original_calcPr": old, "derived_input_calcPr": {**old, **changes},
            "iteration_preserved": True, "original_package_other_members_preserved": True,
            "xml_modification": "only calcPr start-tag attributes changed; all other workbook XML bytes preserved"}


def _probe(path: Path) -> None:
    """Fixed, expired-cache probe; no user-provided formulas or commands."""
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        archive.writestr("_rels/.rels", f'<Relationships xmlns="{P[1:-1]}"><Relationship Id="rId1" Type="{R[1:-1]}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{S[1:-1]}" xmlns:r="{R[1:-1]}"><sheets><sheet name="Probe" sheetId="1" r:id="rId1"/></sheets><calcPr calcMode="auto" fullCalcOnLoad="1" forceFullCalc="1"/></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{P[1:-1]}"><Relationship Id="rId1" Type="{R[1:-1]}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{S[1:-1]}"><sheetData><row r="1"><c r="A1"><f>1+2</f><v>999</v></c></row><row r="2"><c r="A2"><f>1/0</f><v>999</v></c></row></sheetData></worksheet>')


@contextmanager
def _lock(path: Path):
    with path.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try: yield
        finally: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def recalculate_workbook(path: Path, cache_dir: Path, *, timeout: int = 120, max_cells: int = 100000,
                         max_formulas: int = 20000, max_errors: int = 5000, max_comments: int = 5000,
                         max_text_chars: int = MAX_TEXT_CHARS, soffice_path: str | Path | None = None) -> dict:
    """Recalculate a derived copy with a fixed headless command and fresh profile.

    External relationships/active content are not opened. The default executable
    is discovered by the host; an override is trusted host configuration, never a
    Judge argument. A stale-cache probe must refresh in the same invocation.
    """
    path = Path(path); digest = None
    result = {"status": "unavailable", "backend": "libreoffice_headless", "backend_version": None,
              "derived_path": None, "derived_sha256": None, "derived_text_path": None,
              "derived_text_sha256": None, "no_errors_verified": False,
              "limitations": ["LibreOffice results can differ from Microsoft Excel; no Excel execution is claimed.",
                              "No structural cycle proof or financial correctness judgment is performed."]}
    try:
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 120:
            raise WorkbookEvidenceError("Timeout must be an integer from 1 to 120 seconds")
        data, digest = _original(path); result["original_sha256"] = digest
        cache = Path(cache_dir).resolve(); cache.mkdir(parents=True, exist_ok=True)
        original = inspect_workbook(path, cache_dir=cache / "original", max_cells=max_cells,
            max_formulas=max_formulas, max_errors=max_errors, max_comments=max_comments, max_text_chars=max_text_chars)
        result["original"] = original
        if original["status"] != "complete" or original.get("recalculation_blockers"):
            result.update(status="requires_review", reason="Original inspection is incomplete or has external/active-content recalculation blockers")
            return result
        executable = str(soffice_path or shutil.which("soffice") or shutil.which("libreoffice") or "")
        if not executable or Path(executable).name not in {"soffice", "libreoffice"} or not Path(executable).is_file():
            raise WorkbookEvidenceError("LibreOffice executable unavailable")
        version = subprocess.run([executable, "--version"], capture_output=True, text=True, check=True, timeout=min(timeout, 15))
        result["backend_version"] = version.stdout.strip()[:500]
        identity = hashlib.sha256(json.dumps({"original": digest, "backend": result["backend_version"],
            "helper": _hash(Path(__file__)), "bounds": [max_cells, max_formulas, max_errors, max_comments, max_text_chars],
            "timeout": timeout}, sort_keys=True).encode()).hexdigest()
        job = cache / identity; job.mkdir(exist_ok=True)
        result["cache_identity"] = identity
        with _lock(job / ".lock"):
            receipt_path = job / "result.json"
            if receipt_path.is_file():
                stored = json.loads(receipt_path.read_text(encoding="utf-8"))
                for key, hash_key in (("derived_path", "derived_sha256"), ("derived_text_path", "derived_text_sha256")):
                    if stored.get(key) and _hash(Path(stored[key])) != stored[hash_key]:
                        raise WorkbookEvidenceError("Derived cache hash changed")
                if _hash(path) != digest: raise WorkbookEvidenceError("Original changed before cache reuse")
                return {**stored, "cache_hit": True}
            # Incomplete jobs are preserved; subsequent work gets a distinct
            # attempt directory rather than replacing a partial derived file.
            attempt = job / ("attempt_" + str(time.time_ns())); attempt.mkdir()
            input_dir = attempt / "input"; input_dir.mkdir()
            output_dir = attempt / "output"; output_dir.mkdir()
            profile = attempt / "profile"; (profile / "user").mkdir(parents=True)
            (profile / "user/registrymodifications.xcu").write_text('''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>0</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop><prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop></item>
</oor:items>''', encoding="utf-8")
            derived_input = input_dir / "workbook.xlsx"
            modification = _force_calculation(data, derived_input)
            probe_input = input_dir / "_recalc_probe.xlsx"; _probe(probe_input)
            command = [executable, "-env:UserInstallation=" + profile.as_uri(), "--headless", "--nologo", "--nodefault", "--norestore",
                       "--convert-to", "xlsx:Calc MS Excel 2007 XML", "--outdir", str(output_dir), str(derived_input), str(probe_input)]
            completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
            derived_path = output_dir / "workbook.xlsx"
            probe_path = output_dir / "_recalc_probe.xlsx"
            if not derived_path.is_file() or not probe_path.is_file(): raise WorkbookEvidenceError("LibreOffice did not produce expected derived workbooks")
            probe_summary, probe_detail, _ = _scan(probe_path.read_bytes(), max_cells=10, max_formulas=10, max_errors=10)
            probe_values = {r["coordinate"]: r for r in probe_detail["cells"]}
            verified = (probe_values.get("A1", {}).get("value") == "3" and probe_values.get("A2", {}).get("cell_type") == "e"
                        and probe_values["A2"]["value"] == "#DIV/0!")
            result["recalculation_verification"] = {"expired_cache_probe_refreshed": verified, "probe_original_values": {"A1": "999", "A2": "999"},
                "probe_derived_values": {k: {"type": r["cell_type"], "value": r["value"]} for k, r in probe_values.items()},
                "method": "fresh LibreOffice profile OOXMLRecalcMode=0 (always hard recalc); disposable input calcPr auto/fullCalcOnLoad/forceFullCalc; same-invocation expired-cache probe",
                "configuration_source": "https://github.com/LibreOffice/core/blob/master/officecfg/registry/schema/org/openoffice/Office/Calc.xcs"}
            if not verified: raise WorkbookEvidenceError("Expired-cache recalculation probe was not verified")
            derived = inspect_workbook(derived_path, cache_dir=attempt / "derived_inspection", max_cells=max_cells,
                max_formulas=max_formulas, max_errors=max_errors, max_comments=max_comments, max_text_chars=max_text_chars)
            result["derived"] = derived
            text_path = attempt / "recalculated.txt"
            header = {"evidence_kind": "derived_libreoffice_recalculation", "original_sha256": digest,
                "backend": result["backend"], "backend_version": result["backend_version"],
                "derived_sha256": _hash(derived_path), "fresh_recalculation_probe_verified": verified,
                "note": "stored_value below is the backend calculated value; original Candidate bytes are unchanged"}
            raw_text = Path(derived["inspection_text_path"]).read_text(encoding="utf-8") if derived.get("inspection_text_path") else ""
            text_path.write_text(json.dumps(header, ensure_ascii=False) + "\n" + raw_text, encoding="utf-8")
            review = derived["status"] != "complete" or derived.get("formula_count") != original.get("formula_count")
            result.update(status="requires_review" if review else "complete", original_unchanged=_hash(path) == digest,
                derived_path=str(derived_path.resolve()), derived_sha256=_hash(derived_path), derived_text_path=str(text_path.resolve()),
                derived_text_sha256=_hash(text_path), derived_text_complete=derived.get("text_complete", False),
                derived_input_sha256=_hash(derived_input), derived_input_modification=modification,
                formula_count_preserved=derived.get("formula_count") == original.get("formula_count"),
                observed_backend_error_count=derived.get("stored_error_count"),
                # Complete execution can legitimately expose formula errors.
                no_errors_verified=derived.get("counts_complete") is True and derived.get("stored_error_count") == 0,
                truncated=original.get("truncated", False) or derived.get("truncated", False),
                stdout=completed.stdout[:2000], stderr=completed.stderr[:2000], cache_hit=False)
            if not result["original_unchanged"]: raise WorkbookEvidenceError("Original changed during recalculation")
            _write_json(receipt_path, result)
        return result
    except (WorkbookEvidenceError, BadZipFile, KeyError, ValueError, OSError, ET.ParseError, subprocess.SubprocessError) as exc:
        result.update(status="unavailable", error_type=type(exc).__name__, reason=str(exc)[:500], no_errors_verified=False)
        return result
    finally:
        if digest is not None:
            unchanged = path.is_file() and not path.is_symlink() and _hash(path) == digest
            result["original_unchanged"] = unchanged
            if not unchanged: result.update(status="unavailable", reason="Original hash changed", no_errors_verified=False)

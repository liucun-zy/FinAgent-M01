"""Read-only document evidence statistics; no rubric decisions or model calls.

The DeliverBench wording says Han characters and fullwidth punctuation count as
0.5 words, then all remaining whitespace-separated tokens count as one. It does
not publish Unicode ranges. This module exposes its explicit interpretation and
Unicode database version, rather than claiming an official counter exists.
"""
from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
import posixpath
import unicodedata
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile


POLICY_VERSION = "docx_han_fullwidth_punctuation_whitespace_v1"
COUNT_PROFILES = {
    "han_fullwidth_punctuation_half_whitespace": "Han and fullwidth punctuation each count 0.5; replace both with spaces; count all other whitespace tokens",
    "han_half_whitespace": "Han each count 0.5; replace Han with spaces; count all other whitespace tokens",
    "han_half_no_punctuation_tokens": "Han each count 0.5; replace Han with spaces; count remaining whitespace tokens only if they contain a Unicode letter or number",
    "han_half_drop_fullwidth_punctuation_whitespace": "Han each count 0.5; replace Han and fullwidth punctuation with spaces; count other whitespace tokens; punctuation adds no half-word",
}
# Same Han coverage as the official language_guard.CJK expression. That guard
# is a language-presence check, not a word counter; punctuation is added here.
HAN_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x20000, 0x323AF))
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
MAX_DOCX_BYTES = 32 * 1024 * 1024
MAX_DOCUMENT_XML_BYTES = 32 * 1024 * 1024


class EvidenceStatsError(ValueError):
    """Statistics are unavailable; this must not become a grading miss."""


def count_text(text: str, *, profile: str = "han_fullwidth_punctuation_half_whitespace") -> dict:
    """Count exact text; do not apply normalization or a hidden tokenizer.

    Fullwidth punctuation means Unicode punctuation category P* with East Asian
    Width F or W. Fullwidth letters/digits, Kana and Hangul are not Han or
    punctuation; they remain in the whitespace-token stream. Ideographic spaces
    remain whitespace and do not themselves contribute half a word.
    """
    if not isinstance(text, str): raise TypeError("text must be a string")
    if profile not in COUNT_PROFILES: raise EvidenceStatsError("Unknown counting profile")
    han = 0; punctuation = 0; remainder = []
    for char in text:
        point = ord(char)
        if any(start <= point <= end for start, end in HAN_RANGES):
            han += 1; remainder.append(" ")
        elif unicodedata.category(char).startswith("P") and unicodedata.east_asian_width(char) in {"F", "W"}:
            punctuation += 1
            remainder.append(char if profile in {"han_half_whitespace", "han_half_no_punctuation_tokens"} else " ")
        else:
            remainder.append(char)
    fragments = "".join(remainder).split()
    kept = [word for word in fragments if any(char.isalnum() for char in word)] if profile == "han_half_no_punctuation_tokens" else fragments
    tokens = len(kept)
    cjk = han + (punctuation if profile == "han_fullwidth_punctuation_half_whitespace" else 0)
    return {"han_characters": han, "fullwidth_punctuation_characters": punctuation,
            "cjk_characters": cjk, "other_whitespace_tokens": tokens,
            "weighted_word_count": tokens + cjk / 2,
            "weighted_word_count_times_two": 2 * tokens + cjk,
            "count_profile": profile, "pure_punctuation_tokens_excluded": len(fragments) - tokens,
            "text_characters": len(text),
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def _count_variants(text: str) -> dict:
    variants = {name: count_text(text, profile=name) for name in COUNT_PROFILES}
    values = [v["weighted_word_count"] for v in variants.values()]
    return {"count_profiles": variants, "count_profile_descriptions": COUNT_PROFILES,
            "range_across_count_profiles": [min(values), max(values)],
            "range_note": "different rubric wordings require different profiles; this range is not an official tolerance or confidence interval"}


def _body_text(document: ET.Element) -> tuple[str, dict]:
    body = document.find(W + "body")
    if body is None: raise EvidenceStatsError("DOCX document has no body")
    paragraphs = []; counts = {"paragraphs_including_headings": 0, "tables": 0, "table_cells": 0}
    unsupported = set()

    def paragraph_text(node: ET.Element) -> str:
        chunks = []
        def visit(element: ET.Element) -> None:
            if element.tag in {W + "del", W + "moveFrom"}: return
            if element.tag in {W + "txbxContent", W + "altChunk", W + "sym", MC + "AlternateContent"}:
                unsupported.add(element.tag.rsplit("}", 1)[-1]); return
            if element.tag == W + "t": chunks.append(element.text or ""); return
            if element.tag in {W + "tab", W + "br", W + "cr"}: chunks.append(" "); return
            if element.tag == W + "noBreakHyphen": chunks.append("\u2011"); return
            if element.tag == W + "softHyphen": chunks.append("\u00ad"); return
            # Field instructions (instrText) and footnote/comment references have
            # no w:t here. Displayed field result runs are included as body text.
            for child in element: visit(child)
        visit(node)
        return "".join(chunks)

    def walk(node: ET.Element) -> None:
        if node.tag in {W + "del", W + "moveFrom"}: return
        if node.tag in {W + "txbxContent", W + "altChunk", MC + "AlternateContent"}:
            unsupported.add(node.tag.rsplit("}", 1)[-1]); return
        if node.tag == W + "p":
            counts["paragraphs_including_headings"] += 1
            paragraphs.append(paragraph_text(node)); return
        if node.tag == W + "tbl": counts["tables"] += 1
        if node.tag == W + "tc": counts["table_cells"] += 1
        for child in node: walk(child)
    walk(body)
    # Count each XML cell once, including nested tables. python-docx row.cells
    # aliases merged cells and can accidentally count their contents repeatedly.
    return "\n".join(paragraphs), {**counts, "unsupported_constructs": sorted(unsupported)}


def _docx_stats(path: Path) -> dict:
    """Parse DOCX body directly, preserving Candidate bytes and excluding parts.

    Callers must first enforce their own stage-visible path allowlist. A result
    with status=requires_review is not sufficient evidence for an exact-count
    criterion, because unsupported embedded content was not silently counted.
    """
    path = Path(path)
    if path.is_symlink() or not path.is_file(): raise EvidenceStatsError("Document is missing or a symlink")
    if path.suffix.lower() != ".docx": raise EvidenceStatsError("Exact body word statistics support DOCX only")
    if path.stat().st_size > MAX_DOCX_BYTES: raise EvidenceStatsError("DOCX exceeds statistics size limit")
    original = path.read_bytes(); digest = hashlib.sha256(original).hexdigest()
    try:
        with ZipFile(path) as archive:
            names = archive.namelist()
            if names.count("word/document.xml") != 1: raise EvidenceStatsError("Missing or duplicate DOCX document.xml")
            member = archive.getinfo("word/document.xml")
            if member.file_size > MAX_DOCUMENT_XML_BYTES: raise EvidenceStatsError("DOCX document.xml exceeds statistics size limit")
            xml = archive.read(member)
        # ElementTree does not fetch external entities. Reject declarations as
        # well so crafted entity expansion cannot change the counting contract.
        if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
            raise EvidenceStatsError("DTD/entity declarations are not supported")
        text, structure = _body_text(ET.fromstring(xml))
    except (BadZipFile, KeyError, ET.ParseError, OSError) as exc:
        raise EvidenceStatsError("Cannot parse DOCX: " + type(exc).__name__) from None
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise EvidenceStatsError("Document changed during statistics extraction")
    return {"status": "requires_review" if structure["unsupported_constructs"] else "complete",
            "file_sha256": digest, "format": "docx", "policy_version": POLICY_VERSION,
            "policy": {"included": "body paragraphs, headings and all table-cell text, including nested tables; each XML cell once",
                       "excluded": "headers, footers, footnotes, endnotes, comments, field instructions, deleted/move-from revision text",
                       "han_ranges": [[hex(start), hex(end)] for start, end in HAN_RANGES],
                       "fullwidth_punctuation": "Unicode category P* and East Asian Width F or W",
                       "unicode_database_version": unicodedata.unidata_version,
                       "other_tokenization": "replace counted characters with ASCII spaces, then Python str.split() with no argument",
                       "normalization": "none; run text concatenated, paragraph boundaries/line breaks/tabs separate tokens",
                       "scope_note": "rubric has no published Unicode ranges; this explicit implementation is not an official benchmark counter"},
            "structure": structure, **count_text(text), **_count_variants(text)}


def _pptx_stats(path: Path) -> dict:
    """Read slide-owned text/table cells, not speaker notes, layouts or charts."""
    pns = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
    ans = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    rns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    relns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    original = path.read_bytes(); slides = []
    try:
        with ZipFile(BytesIO(original)) as archive:
            def xml(name: str) -> ET.Element:
                member = archive.getinfo(name)
                if member.file_size > MAX_DOCUMENT_XML_BYTES: raise EvidenceStatsError("PPTX XML exceeds statistics size limit")
                raw = archive.read(member)
                if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper(): raise EvidenceStatsError("DTD/entity declarations are not supported")
                return ET.fromstring(raw)
            presentation = xml("ppt/presentation.xml")
            relations = {row.attrib["Id"]: row for row in xml("ppt/_rels/presentation.xml.rels").findall(relns + "Relationship")}
            for number, slide_id in enumerate(presentation.findall(pns + "sldIdLst/" + pns + "sldId"), 1):
                relation = relations[slide_id.attrib[rns + "id"]]
                if relation.attrib.get("TargetMode") == "External": raise EvidenceStatsError("External slide target is unsupported")
                target = relation.attrib["Target"]
                member = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("ppt", target))
                if not member.startswith("ppt/slides/"): raise EvidenceStatsError("Noncanonical slide target")
                slide = xml(member); chunks = []; unsupported = set(); shapes = 0; cells = 0
                def paragraphs(container: ET.Element) -> None:
                    for paragraph in container.findall(ans + "p"):
                        fragments = []
                        for element in paragraph.iter():
                            if element.tag == ans + "t": fragments.append(element.text or "")
                            elif element.tag == ans + "br": fragments.append(" ")
                        chunks.append("".join(fragments))
                def shape(node: ET.Element) -> None:
                    nonlocal shapes, cells
                    if node.tag == MC + "AlternateContent": unsupported.add("AlternateContent"); return
                    nv = node.find(".//" + pns + "cNvPr")
                    if nv is not None and nv.attrib.get("hidden") in {"1", "true"}: return
                    if node.tag == pns + "grpSp":
                        for child in node: shape(child)
                        return
                    tx = node.find(pns + "txBody")
                    if tx is not None: shapes += 1; paragraphs(tx)
                    if node.tag == pns + "graphicFrame":
                        tables = node.findall(".//" + ans + "tbl")
                        data = node.find(".//" + ans + "graphicData")
                        if not tables and data is not None and not data.attrib.get("uri", "").endswith("/chart"):
                            unsupported.add("graphicData:" + data.attrib.get("uri", "unspecified"))
                        for table in tables:
                            for row in table.findall(ans + "tr"):
                                for cell in row.findall(ans + "tc"):
                                    cells += 1; tx = cell.find(ans + "txBody")
                                    if tx is not None: paragraphs(tx)
                tree = slide.find(pns + "cSld/" + pns + "spTree")
                if tree is None: raise EvidenceStatsError("PPTX slide has no shape tree")
                for child in tree: shape(child)
                text = "\n".join(chunks)
                slides.append({"slide": number, "status": "requires_review" if unsupported else "complete",
                    "slide_xml": member, "text_shapes": shapes, "table_cells": cells,
                    "unsupported_constructs": sorted(unsupported), **_count_variants(text)})
    except (BadZipFile, KeyError, ET.ParseError, OSError) as exc:
        raise EvidenceStatsError("Cannot parse PPTX: " + type(exc).__name__) from None
    return {"status": "requires_review" if any(s["status"] != "complete" for s in slides) else "complete",
            "format": "pptx", "file_sha256": hashlib.sha256(original).hexdigest(),
            "policy_version": "pptx_slide_owned_text_v1", "slide_count": len(slides), "slides": slides,
            "scope": "slide-owned text boxes/placeholders and table cells, recursing groups; excludes hidden shapes, notes, inherited layout/master text, pictures and chart internals; SmartArt/diagrams/OLE or other non-table graphics require review",
            "count_profile_for_chinese_punctuation_excluded": "han_half_drop_fullwidth_punctuation_whitespace",
            "unicode_database_version": unicodedata.unidata_version}


def document_stats(path: Path) -> dict:
    """Deterministic evidence only; caller must enforce stage/path allowlisting.

    DOCX profiles serve the differing rubric token rules. Markdown includes its
    entire text, tables and code blocks. PPTX gives per-slide profiles. PDF gives
    raw page count only: identifying/excluding covers or TOCs requires review.
    """
    path = Path(path)
    if path.is_symlink() or not path.is_file(): raise EvidenceStatsError("Document is missing or a symlink")
    if path.stat().st_size > MAX_DOCX_BYTES: raise EvidenceStatsError("Document exceeds statistics size limit")
    suffix = path.suffix.lower()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    if suffix == ".docx": result = _docx_stats(path)
    elif suffix == ".pptx": result = _pptx_stats(path)
    elif suffix in {".md", ".txt"}:
        try: text = path.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError) as exc: raise EvidenceStatsError("Cannot read text: " + type(exc).__name__) from None
        result = {"status": "complete", "format": suffix[1:], "file_sha256": before,
                  "policy_version": "whole_text_count_profiles_v1", "scope": "entire text including tables, code blocks and Markdown syntax; no rendering or markup removal",
                  "unicode_database_version": unicodedata.unidata_version, **_count_variants(text)}
    elif suffix == ".pdf":
        from pypdf import PdfReader
        try:
            reader = PdfReader(BytesIO(path.read_bytes()))
            result = {"status": "complete", "format": "pdf", "file_sha256": before,
                      "policy_version": "raw_pdf_page_count_v1", "actual_page_count": len(reader.pages),
                      "cover_or_contents_pages_excluded": False,
                      "scope": "physical PDF pages only; caller must inspect pages to identify covers/TOCs before rubric-specific exclusions"}
        except Exception as exc: raise EvidenceStatsError("Cannot parse PDF: " + type(exc).__name__) from None
    else: raise EvidenceStatsError("Statistics support DOCX, Markdown/text, PPTX and PDF only")
    if hashlib.sha256(path.read_bytes()).hexdigest() != before: raise EvidenceStatsError("Document changed during statistics extraction")
    return result

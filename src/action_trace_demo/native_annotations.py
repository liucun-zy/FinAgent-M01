"""Read-only native annotation/story text, separate from document properties.

This complements body extraction; it does not judge prohibited content, render
images, inspect metadata generators, or modify a submitted financial file.
"""
from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile


POLICY_VERSION = "native_annotations_v1"


def _xml_root(raw: bytes) -> ET.Element:
    if len(raw) > 32 * 1024 * 1024: raise ValueError("Annotation XML exceeds size limit")
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper(): raise ValueError("DTD/entities unsupported")
    return ET.fromstring(raw)


def _xml_text(raw: bytes) -> str:
    root = _xml_root(raw); chunks = []
    def visit(node):
        local = node.tag.rsplit("}", 1)[-1]
        if local in {"t", "text", "delText", "instrText"}:
            chunks.append(node.text or "")
            for child in node: visit(child)
            return
        if local in {"br", "cr", "tab"}: chunks.append(" ")
        for child in node: visit(child)
        if local in {"p", "comment", "cm", "threadedComment"}: chunks.append("\n")
    visit(root)
    return "".join(chunks).strip()


def _comment_drawing(raw):
    """Return VML textbox content only when every shape is a legacy worksheet note."""
    root = _xml_root(raw)
    vml = "{urn:schemas-microsoft-com:vml}"
    excel = "{urn:schemas-microsoft-com:office:excel}"
    shapes = list(root.iter(vml+"shape"))
    if not shapes or any(node.tag in {vml+"image",vml+"imagedata",vml+"group"} for node in root.iter()):
        return None
    text = []
    for shape in shapes:
        client = shape.find(excel+"ClientData")
        if client is None or client.get("ObjectType") != "Note":
            return None
        for box in shape.iter(vml+"textbox"):
            text.append("".join(box.itertext()).strip())
    return "\n".join(s for s in text if s)


def extract_native_annotations(path: Path, *, max_text_chars: int = 2_000_000, max_parts: int = 2000) -> dict:
    path = Path(path)
    result = {"status": "unavailable", "complete": False, "policy_version": POLICY_VERSION,
              "source_sha256": None, "entries": [], "package_parts": [], "selected_parts": [],
              "unsupported": [], "truncated": False, "text": "", "text_sha256": None,
              "scope": "native annotation/supporting story text; ordinary docProps generator metadata excluded; existing body evidence remains separately necessary"}
    if min(max_text_chars, max_parts) < 1: raise ValueError("Extraction limits must be positive")
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Expected regular non-symlink document within 32 MiB")
        original = path.read_bytes(); digest = hashlib.sha256(original).hexdigest()
        result.update(source_sha256=digest, format=path.suffix.lower().lstrip("."))
        used = 0
        def add(part, kind, text, **metadata):
            nonlocal used
            if len(result["entries"]) >= max_parts:
                result["truncated"] = True; return
            full_length = len(text)
            available = max(0, max_text_chars - used)
            if full_length > available: result["truncated"] = True
            retained = text[:available]; used += len(retained)
            result["entries"].append({"part": part, "kind": kind, "text": retained,
                "original_text_chars": full_length, "truncated": len(retained) != full_length, **metadata})
        suffix = path.suffix.lower()
        if suffix in {".docx", ".pptx", ".xlsx"}:
            with ZipFile(BytesIO(original)) as archive:
                members = archive.infolist(); names = [m.filename for m in members]
                if len(names) > 10000 or len(set(names)) != len(names): raise ValueError("Too many or duplicate ZIP parts")
                if sum(m.file_size for m in members) > 128 * 1024 * 1024: raise ValueError("Uncompressed package exceeds limit")
                if any(n.startswith("/") or ".." in n.split("/") or "\\" in n for n in names): raise ValueError("Noncanonical ZIP part")
                result["package_parts"] = sorted(names)
                selected = []; categories = set()
                drawing_text = {}
                for name in sorted(names):
                    lower = name.lower(); kind = None
                    if suffix == ".docx" and lower.startswith("word/"):
                        base = lower.rsplit("/", 1)[-1]
                        if re.fullmatch(r"header[0-9]*\.xml", base): kind = "header"
                        elif re.fullmatch(r"footer[0-9]*\.xml", base): kind = "footer"
                        elif base == "footnotes.xml": kind = "footnotes"
                        elif base == "endnotes.xml": kind = "endnotes"
                        elif "comment" in base and base.endswith(".xml") and "/_rels/" not in lower: kind = "comments"
                    elif suffix == ".pptx":
                        if re.fullmatch(r"ppt/notesslides/notesslide[0-9]+\.xml", lower): kind = "presenter_notes"
                        elif lower.startswith("ppt/") and "comment" in lower and lower.endswith(".xml") and "/_rels/" not in lower: kind = "comments"
                        elif re.fullmatch(r"ppt/slides/slide[0-9]+\.xml", lower):
                            if archive.getinfo(name).file_size > 32 * 1024 * 1024: raise ValueError("Slide XML exceeds limit")
                            root = _xml_root(archive.read(name))
                            hidden_slide = root.attrib.get("show") in {"0", "false"}
                            hidden_shapes = any(n.tag.rsplit("}", 1)[-1] == "cNvPr" and n.attrib.get("hidden") in {"1", "true"} for n in root.iter())
                            if hidden_slide or hidden_shapes: kind = "slide_text_including_hidden_content"
                    elif suffix == ".xlsx" and lower.startswith("xl/") and "comment" in lower and lower.endswith(".xml") and "/_rels/" not in lower:
                        kind = "comments"
                    elif suffix == ".xlsx" and re.fullmatch(r"xl/drawings/commentsdrawing[0-9]+\.vml",lower):
                        text = _comment_drawing(archive.read(name))
                        if text is not None:
                            drawing_text[name] = text
                            kind = "comment_drawing"
                    if kind: selected.append((name, kind)); categories.add(kind)
                result["selected_parts"] = [name for name, _ in selected]
                expected = {".docx": {"header", "footer", "footnotes", "endnotes", "comments"},
                            ".pptx": {"presenter_notes", "comments", "slide_text_including_hidden_content"},
                            ".xlsx": {"comments"}}[suffix]
                result["absent_categories"] = sorted(expected - categories)
                if len(selected) > max_parts: result["truncated"] = True
                for name, kind in selected[:max_parts]:
                    member = archive.getinfo(name)
                    if member.file_size > 32 * 1024 * 1024: raise ValueError("Selected XML part exceeds limit")
                    raw = archive.read(member)
                    text = drawing_text[name] if name in drawing_text else _xml_text(raw)
                    add(name, kind, text, part_sha256=hashlib.sha256(raw).hexdigest())
                # Unsupported binary/embedded annotation representations must
                # not silently become an assertion that comments are absent.
                result["unsupported"] = sorted(n for n in names if ("comment" in n.lower() or "notesslide" in n.lower())
                    and n not in drawing_text and not n.lower().endswith((".xml", ".rels")) and not n.endswith("/"))
        elif suffix == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(original)); annotation_count = 0
            result["page_count"] = len(reader.pages)
            for number, page in enumerate(reader.pages, 1):
                annotations = page.get("/Annots", [])
                if hasattr(annotations, "get_object"): annotations = annotations.get_object()
                for index, reference in enumerate(annotations):
                    annotation_count += 1
                    annotation = reference.get_object()
                    fields = {}
                    for key in ("/Contents", "/T", "/Subj", "/RC", "/NM", "/TU", "/V", "/DV"):
                        if key in annotation:
                            value = annotation[key]
                            if isinstance(value, (str, int, float)): fields[key] = str(value)
                            elif value is not None: result["unsupported"].append(f"page{number}/annotation{index}:{key}:non_scalar")
                    action = annotation.get("/A")
                    if hasattr(action, "get_object"): action = action.get_object()
                    if action and action.get("/URI") is not None: fields["/A/URI"] = str(action["/URI"])
                    subtype = str(annotation.get("/Subtype", "unknown"))
                    part = f"page{number}/annotation{index}"
                    result["package_parts"].append(part); result["selected_parts"].append(part)
                    if subtype not in {"/Text", "/FreeText", "/Highlight", "/Underline", "/StrikeOut", "/Squiggly", "/Caret", "/Popup", "/Link", "/Widget"}:
                        result["unsupported"].append(part + ":" + subtype)
                    if annotation.get("/AP") and not fields and subtype in {"/FreeText", "/Widget"}:
                        result["unsupported"].append(part + ":appearance_only_text_unparsed")
                    add(part, "pdf_annotation", "\n".join(k + ": " + v for k, v in fields.items()), page=number, subtype=subtype)
            result["annotation_count"] = annotation_count
            result["absent_categories"] = ["pdf_annotations"] if not annotation_count else []
        else:
            raise ValueError("Supported formats are DOCX, PPTX, XLSX and PDF")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest: raise ValueError("Source changed while extracting")
        result["text"] = "\n\n".join("[" + row["kind"] + " | " + row["part"] + "]\n" + row["text"] for row in result["entries"])
        result["text_sha256"] = hashlib.sha256(result["text"].encode()).hexdigest()
        result["complete"] = not result["truncated"] and not result["unsupported"]
        result["status"] = "complete" if result["complete"] else "requires_review"
        result["empty_native_annotations"] = len(result["selected_parts"]) == 0
        return result
    except Exception as exc:
        result.update(status="unavailable", complete=False, error_type=type(exc).__name__, reason=str(exc)[:300])
        return result

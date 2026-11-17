"""Append the original worksheet and all its internal precedent sheets to a staged XLSX.

Only a released artifact version is accepted. The destination remains unchanged
on an unresolved import. Its pre-existing worksheet and annotation parts are
retained byte for byte. The resulting hash describes a proposal, not a commit.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
from urllib.parse import unquote
import posixpath
import re
import tempfile
from zipfile import ZipFile, ZIP_DEFLATED

from lxml import etree as ET
from openpyxl import load_workbook
from openpyxl.formula import Tokenizer
from openpyxl.formula.tokenizer import TokenizerError

from .guarded_artifacts import _sha, _slots, MAX_BYTES, MAX_SLOTS
from .tools import read_document

M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
P = "{http://schemas.openxmlformats.org/package/2006/relationships}"
T = "{http://schemas.openxmlformats.org/package/2006/content-types}"
ADDR = re.compile(r"\$?[A-Z]{1,3}\$?[1-9]\d{0,6}",re.I)
MAX_XML = 64 * 1024 * 1024


def _xml(z,part):
    if z.getinfo(part).file_size > MAX_XML:
        raise ValueError("Workbook XML part exceeds inspection limit")
    return ET.fromstring(z.read(part),ET.XMLParser(resolve_entities=False,no_network=True))


def _bytes(node):
    return ET.tostring(node,encoding="UTF-8",xml_declaration=True,standalone=True)


def _package(z):
    names=z.namelist()
    if len(names)!=len(set(names)) or sum(i.file_size for i in z.infolist())>MAX_XML*3:
        raise ValueError("Workbook contains duplicate members or exceeds decompression limit")
    work=_xml(z,"xl/workbook.xml")
    rels=_xml(z,"xl/_rels/workbook.xml.rels")
    types=_xml(z,"[Content_Types].xml")
    edges={rel.get("Id"):rel for rel in rels}
    sheets={}
    for sheet in work.find(M+"sheets"):
        rel=edges[sheet.get(R+"id")]
        if not rel.get("Type","").endswith("/worksheet") or rel.get("TargetMode")=="External":
            raise ValueError("Only internal worksheets can be imported")
        target=rel.get("Target")
        path=target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl",target))
        if not path.startswith("xl/worksheets/") or path not in names:
            raise ValueError("Unresolved worksheet mapping")
        sheets[sheet.get("name")]=(sheet,path)
    return work,rels,types,sheets


def _canonical(el):
    # Registry equality is independent of namespace prefix choices.
    return (el.tag,tuple(sorted(el.attrib.items())),el.text,tuple(_canonical(c) for c in el))


def _worksheet_dependencies(ws, names, external=None):
    refs=set()
    if ws.max_row*ws.max_column>MAX_SLOTS:
        raise ValueError("Released worksheet exceeds import inspection limit")
    for row in ws:
        for cell in row:
            if cell.data_type != "f":
                continue
            if not isinstance(cell.value,str):
                raise ValueError(f"Unsupported formula at {ws.title}!{cell.coordinate}")
            try:
                tokens=Tokenizer(cell.value).items
            except (TokenizerError,ValueError,IndexError) as err:
                raise ValueError("Cannot verify Excel formula at "+ws.title+"!"+cell.coordinate) from err
            for t in tokens:
                if t.type=="FUNC" and t.subtype=="OPEN" and t.value.upper().rstrip("(") in {
                    "INDIRECT","OFFSET","HYPERLINK","WEBSERVICE","RTD","DDE","CALL","REGISTER.ID","SHEET","SHEETS"}:
                    raise ValueError(f"Unresolved dynamic or external dependency at {ws.title}!{cell.coordinate}: {t.value}")
                if t.type!="OPERAND" or t.subtype!="RANGE":
                    continue
                operand=t.value
                if "!" in operand:
                    prefix,operand=operand.rsplit("!",1)
                    name=prefix.strip("'").replace("''","'").casefold()
                    if (name not in names and external is not None and
                            not any(not ADDR.fullmatch(x) for x in operand.split(":")) and operand.count(":")<=1
                            and external.register(prefix,ws.title,cell.coordinate)):
                        continue
                    if name not in names:
                        raise ValueError(f"External or unknown dependency {t.value} at {ws.title}!{cell.coordinate}")
                    refs.add(names[name])
                if any(not ADDR.fullmatch(x) for x in operand.split(":")) or operand.count(":")>1:
                    raise ValueError(f"Unresolved name or structured range {t.value} at {ws.title}!{cell.coordinate}")
    return sorted(refs-{ws.title})


def _closure(book, requested, external=None):
    if not requested or not isinstance(requested,list) or not all(isinstance(s,str) and s for s in requested):
        raise ValueError("Specify one or more source worksheet names")
    names={s.title.casefold():s.title for s in book}
    pending=[names[s.casefold()] if s.casefold() in names else None for s in requested]
    if None in pending:
        raise ValueError("Unknown source worksheet")
    graph={}
    checked_cells=0
    while pending:
        name=pending.pop()
        if name not in graph:
            ws=book[name]
            checked_cells+=ws.max_row*ws.max_column
            if checked_cells>MAX_SLOTS:
                raise ValueError("Source worksheet dependency closure exceeds inspection limit")
            graph[name]=_worksheet_dependencies(ws,names,external)
            pending.extend(graph[name])
    return graph


class Styles:
    """Append source styles without renumbering destination style identifiers."""
    def __init__(self,source,target):
        self.source=source
        self.target=target
        self.maps={}

    def map(self,kind,index):
        key=(kind,index)
        if key in self.maps:
            return self.maps[key]
        a=self.source.find(M+kind)
        b=self.target.find(M+kind)
        if a is None or (kind != "numFmts" and not 0<=index<len(a)):
            raise ValueError(f"Undefined worksheet style {kind} {index}")
        if b is None:
            if kind!="numFmts":
                raise ValueError(f"Unsupported target registry {kind}")
            b=ET.Element(M+kind)
            self.target.insert(0,b)
        if kind=="numFmts":
            source=next((part for part in a if int(part.get("numFmtId"))==index),None)
            if source is None:
                raise ValueError("Undefined custom number format")
            fmt=source.get("formatCode")
            old=next((part for part in b if part.get("formatCode")==fmt),None)
            if old is None:
                new=max([163,*(int(x.get("numFmtId")) for x in b)])+1
                old=deepcopy(source)
                old.set("numFmtId",str(new))
                b.append(old)
            value=int(old.get("numFmtId"))
        else:
            element=deepcopy(a[index])
            if kind in {"cellXfs","cellStyleXfs"}:
                for name,section in (("fontId","fonts"),("fillId","fills"),("borderId","borders"),("xfId","cellStyleXfs"),("numFmtId","numFmts")):
                    if name in element.attrib:
                        i=int(element.get(name))
                        if name=="numFmtId" and i<164:
                            continue
                        if section==kind:
                            raise ValueError("Recursive workbook style")
                        element.set(name,str(self.map(section,i)))
            canonical=_canonical(element)
            value=next((i for i,c in enumerate(b) if _canonical(c)==canonical),None)
            if value is None:
                value=len(b)
                b.append(element)
        self.maps[key]=value
        b.set("count",str(len(b)))
        return value


def _normalized_sheet(root,source_strings,styles):
    # Keep all source formulas and native worksheet content. Eliminate only
    # references to source workbook-level shared string/style identifiers.
    for el in list(root.iter()):
        if el.tag in {M+"conditionalFormatting",M+"dataValidations",M+"extLst",M+"oleObjects",M+"controls",
                       M+"drawing",M+"legacyDrawing",M+"tableParts",M+"hyperlinks"}:
            raise ValueError(f"Worksheet feature needs its own dependency mapping: {ET.QName(el).localname}")
        if el.tag==M+"c":
            el.set("s", str(styles.map("cellXfs",int(el.get("s","0")))))
            if el.get("t")=="s":
                val=el.find(M+"v")
                i=int(val.text) if val is not None else -1
                if not 0<=i<len(source_strings):
                    raise ValueError("Missing source shared string")
                shared=deepcopy(source_strings[i])
                shared.tag=M+"is"
                el.replace(val,shared)
                el.set("t","inlineStr")
        elif el.tag in {M+"row",M+"col"}:
            attr="s" if el.tag==M+"row" else "style"
            if attr in el.attrib:
                el.set(attr,str(styles.map("cellXfs",int(el.get(attr)))))
        elif el.tag==M+"phoneticPr" and "fontId" in el.attrib:
            el.set("fontId",str(styles.map("fonts",int(el.get("fontId")))))
    return root


def _exact_sheet(root):
    # Compare complete native contents after both registries have been mapped
    # into the destination. XML serialization and namespace prefixes may vary.
    return _canonical(root)


def _resolved_style(ws,cell):
    return (str(cell.font),str(cell.border),str(cell.fill),str(cell.alignment),str(cell.protection),
            cell.number_format,cell.quotePrefix,cell.pivotButton)


def _signature(ws,external=None):
    # Used only after checking same worksheet titles. Digest actual values,
    # expanded formulas, comments, formatting, dimensions and native features.
    digest=hashlib.sha256()
    def put(value):
        digest.update(repr(value).encode("utf8")+b"\x00")
    for row in ws:
        for c in row:
            if c.value is None and not c.has_style and not c.comment:
                continue
            put((c.coordinate,c.data_type,external.formula(c.value,ws.title,c.coordinate) if external else c.value,_resolved_style(ws,c),
                 (c.comment.author,c.comment.text) if c.comment else None))
    put(tuple(sorted(str(x) for x in ws.merged_cells.ranges)))
    put(tuple((i,row.height,row.hidden,row.outlineLevel) for i,row in sorted(ws.row_dimensions.items())))
    put(tuple((i,col.width,col.min,col.max,col.hidden,col.outlineLevel) for i,col in sorted(ws.column_dimensions.items())))
    put((str(ws.freeze_panes),ws.sheet_state))
    return digest.hexdigest()


def _source(operator,version_id,stage,expected_sha):
    version=operator.dag.get(version_id)
    if not version or version["kind"]!="artifact_version":
        raise ValueError("Source must be a registered released native artifact version")
    v=version["payload"]
    path=v["path"]
    if not (path.startswith("source/") and Path(path).suffix.lower()==".xlsx" and v["action"]=="seed" and v["stage"]<=stage):
        raise ValueError("Worksheet import requires a released original, available by this stage")
    if expected_sha is not None and expected_sha!=v["sha256"]:
        raise ValueError("Source version sha256 changed")
    intent=operator.dag.get(v["intent"])
    authority=[operator.dag.get(i) for i in intent["parents"]]
    sources=[n for n in authority if n and n["kind"]=="source" and n["payload"]["locator"]==path and n["payload"]["stage"]<=stage]
    if len(sources)!=1 or operator.head(path)["id"]!=version_id:
        raise ValueError("Source snapshot has no unique released-text authority")
    file=operator._blob(path,v["sha256"])
    if _sha(operator.materialized(path))!=v["sha256"] or read_document(file)!=sources[0]["payload"]["text"]:
        raise ValueError("Source materialization or released text does not match its immutable version")
    return file,version,sources[0]


class ReleasedLinks:
    """Internalize an external reference only if its native worksheet is already an exact released copy."""
    def __init__(self,op,src,dest,stage):
        self.op=op
        self.src=src
        self.dest=dest
        self.stage=stage
        self.links={}
        self.proofs={}
        self.registrations={}
        self.uses=[]
        with ZipFile(src) as z:
            work,rels,_,_=_package(z)
            rel_by_id={r.get("Id"):r for r in rels}
            for index,ref in enumerate(work.findall(M+"externalReferences/"+M+"externalReference"),1):
                rel=rel_by_id.get(ref.get(R+"id"))
                if rel is None or not rel.get("Type","").endswith("/externalLink"):
                    continue
                target=rel.get("Target","")
                part=target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl",target))
                if not re.fullmatch(r"xl/externalLinks/[^/]+\.xml",part) or part not in z.namelist():
                    continue
                book=_xml(z,part).find(M+"externalBook")
                if book is None: continue
                refs=posixpath.join(posixpath.dirname(part),"_rels",posixpath.basename(part)+".rels")
                if refs not in z.namelist(): continue
                ref=next((r for r in _xml(z,refs) if r.get("Id")==book.get(R+"id")
                          and r.get("Type","").endswith("/externalLinkPath") and r.get("TargetMode")=="External"),None)
                # A pure relative basename may match a released source. Never
                # dereference or search filesystem/network paths from a workbook.
                filename=unquote(ref.get("Target","")) if ref is not None else ""
                if not re.fullmatch(r"[^/:\\?#%]+\.xlsx",filename,re.I): continue
                names={s.get("val") for s in book.findall(M+"sheetNames/"+M+"sheetName")}
                self.links[str(index)]=(filename,names)

    @staticmethod
    def reference(prefix):
        raw=prefix.strip("'").replace("''","'")
        return re.fullmatch(r"\[(\d+)\](.+)",raw)

    def register(self,prefix,consumer,cell):
        m=self.reference(prefix)
        if not m or m[1] not in self.links:
            return False
        filename,sheets=self.links[m[1]]
        if m[2] not in sheets:
            return False
        key=(m[1],m[2])
        if key not in self.proofs:
            matches=[v for v in self.op.dag.nodes("artifact_version")
                     if v["payload"].get("action")=="seed" and v["payload"]["path"].startswith("source/")
                     and v["payload"]["stage"]<=self.stage and Path(v["payload"]["path"]).name.casefold()==filename.casefold()]
            if len(matches)!=1:
                return False
            file,v,authority=_source(self.op,matches[0]["id"],self.stage,matches[0]["payload"]["sha256"])
            # The same-name sheet and its complete available dependency closure
            # must already be present, unmodified, including formatting.
            book=load_workbook(file,read_only=True)
            try: graph=_closure(book,[m[2]])
            finally: book.close()
            _,missing,reused=_append(file,self.dest,graph)
            if missing or set(reused)!=set(graph):
                raise ValueError("Import the verified external workbook's sheet closure first: "+filename+" / "+m[2])
            self.proofs[key]={"ordinal":m[1],"source_file":filename,"sheet":m[2],"source_version_id":v["id"],
                              "source_id":authority["id"],"source_sha256":v["payload"]["sha256"],"reused_sheets":reused}
        self.registrations[(consumer,cell)]=True
        return True

    def formula(self,text,consumer,cell,record=False):
        if not isinstance(text,str) or not text.startswith("=") or (consumer,cell) not in self.registrations:
            return text
        formula=[]
        for token in Tokenizer(text).items:
            value=token.value
            if token.type=="OPERAND" and token.subtype=="RANGE" and "!" in value:
                prefix,address=value.rsplit("!",1)
                ref=self.reference(prefix)
                if ref and (ref[1],ref[2]) in self.proofs:
                    value="'"+ref[2].replace("'","''")+"'!"+address
            formula.append(value)
        updated="="+"".join(formula)
        if record and updated!=text:
            self.uses.append({"consumer":consumer+"!"+cell,"original_formula":text,"local_formula":updated})
        return updated


def _append(source,dest,graph,external=None):
    changes={}
    imported=[]
    reused=[]
    with ZipFile(source) as original, ZipFile(dest) as output:
        _,_,_,srcs=_package(original)
        work,rels,types,dsts=_package(output)
        target_styles=_xml(output,"xl/styles.xml")
        styles=Styles(_xml(original,"xl/styles.xml"),target_styles)
        strings=list(_xml(original,"xl/sharedStrings.xml")) if "xl/sharedStrings.xml" in original.namelist() else []
        source_book=load_workbook(source,rich_text=True)
        target_book=load_workbook(dest,rich_text=True)
        try:
            if source_book.epoch!=target_book.epoch:
                raise ValueError("Workbooks use different date systems")
            existing={k.casefold():k for k in dsts}
            taken=set(output.namelist())
            last_id=max(int(s.get("sheetId")) for s,_ in dsts.values())
            occupied_ids={r.get("Id") for r in rels}
            for name,(definition,part) in srcs.items():
                if name not in graph:
                    continue
                rel_part=posixpath.join(posixpath.dirname(part),"_rels",posixpath.basename(part)+".rels")
                if rel_part in original.namelist():
                    raise ValueError("Linked annotations, drawings, tables or other source sheet resources need explicit import support: "+name)
                content=_normalized_sheet(_xml(original,part),strings,styles)
                if external is not None:
                    for cell in content.iter(M+"c"):
                        f=cell.find(M+"f")
                        if f is not None and f.text and (name,cell.get("r")) in external.registrations:
                            if f.get("t")=="shared":
                                raise ValueError("Shared external formula group needs explicit mapping")
                            f.text=external.formula("="+f.text,name,cell.get("r"),record=True)[1:]
                incumbent=existing.get(name.casefold())
                if incumbent:
                    incumbent_part=dsts[incumbent][1]
                    incumbent_rels=posixpath.join(posixpath.dirname(incumbent_part),"_rels",posixpath.basename(incumbent_part)+".rels")
                    target_strings=(list(_xml(output,"xl/sharedStrings.xml"))
                                    if "xl/sharedStrings.xml" in output.namelist() else [])
                    same_registry=Styles(deepcopy(target_styles),deepcopy(target_styles))
                    incumbent_xml=_normalized_sheet(_xml(output,incumbent_part),target_strings,same_registry)
                    # If a prior import expanded its XML references to target
                    # IDs, retain those existing IDs during this comparison.
                    if (incumbent!=name or incumbent_rels in output.namelist()
                            or _signature(source_book[name],external)!=_signature(target_book[incumbent])
                            or _exact_sheet(content)!=_exact_sheet(incumbent_xml)):
                        raise ValueError("Different same-name worksheet already exists: "+name)
                    reused.append(name)
                    continue
                imported.append(name)
                last_id+=1
                idx=last_id
                while f"xl/worksheets/sheet{idx}.xml" in taken or f"rId{idx}" in occupied_ids:
                    idx+=1
                newpart=f"xl/worksheets/sheet{idx}.xml"
                rid=f"rId{idx}"
                occupied_ids.add(rid)
                taken.add(newpart)
                changes[newpart]=_bytes(content)
                node=deepcopy(definition)
                node.set("sheetId",str(last_id))
                node.set(R+"id",rid)
                work.find(M+"sheets").append(node)
                ET.SubElement(rels,P+"Relationship",Id=rid,Type=R[1:-1]+"/worksheet",Target=newpart[3:])
                ET.SubElement(types,T+"Override",PartName="/"+newpart,ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml")
            if imported:
                changes.update({"xl/workbook.xml":_bytes(work),"xl/_rels/workbook.xml.rels":_bytes(rels),
                                 "[Content_Types].xml":_bytes(types),"xl/styles.xml":_bytes(target_styles)})
        finally:
            source_book.close()
            target_book.close()
    return changes,imported,reused


def import_released_sheets(operator, staged_path, *, source_version_id, sheets, stage,
                          expected_sha256=None, destination=None):
    """Preflight and append atomically under the caller's artifact transaction."""
    source,version,authority=_source(operator,source_version_id,stage,expected_sha256)
    dest=Path(staged_path)
    if dest.suffix.lower()!=".xlsx" or dest.is_symlink() or not dest.is_file() or dest.stat().st_size>MAX_BYTES:
        raise ValueError("Destination must be an existing staged Excel workbook")
    # Actual source and public destination cannot be modified by this helper.
    if not dest.resolve().is_relative_to(operator.store.resolve()) or dest.parent.resolve()==operator.store.resolve():
        raise ValueError("Imports must target a temporary staged artifact")
    before_sha=_sha(dest)
    before=_slots(dest)
    external=ReleasedLinks(operator,source,dest,stage)
    w=load_workbook(source,read_only=True)
    try:
        graph=_closure(w,sheets,external)
    finally:
        w.close()
    parts,imported,reused=_append(source,dest,graph,external)
    receipt={"source_version_id":version["id"],"input_source_id":authority["id"],"source_path":version["payload"]["path"],
             "source_sha256":version["payload"]["sha256"],"stage":stage,"destination":destination,
             "requested_sheets":sheets,"source_dependencies":graph,"imported_sheets":imported,"reused_sheets":reused,
             "previous_sha256":before_sha,"sha256":before_sha,"changed_locators":[]}
    receipt["external_proofs"]=list(external.proofs.values())
    receipt["localized_formula_uses"]=external.uses
    if not imported:
        for proof in external.proofs.values():
            _source(operator,proof["source_version_id"],stage,proof["source_sha256"])
        _source(operator,source_version_id,stage,version["payload"]["sha256"])
        if _sha(dest)!=before_sha:
            raise ValueError("Destination changed during content reuse")
        return receipt
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        out=Path(tmp)/dest.name
        with ZipFile(dest) as old, ZipFile(out,"w",compression=ZIP_DEFLATED) as new:
            for member in old.infolist():
                new.writestr(member,parts.pop(member.filename) if member.filename in parts else old.read(member.filename))
            for name,body in parts.items():
                new.writestr(name,body)
        after=_slots(out)
        changed=sorted(k for k in after.keys()|before.keys() if after.get(k)!=before.get(k))
        if any(k.rsplit("!",1)[0] not in imported for k in changed):
            raise ValueError("Append would change protected cells or annotations")
        src_book=load_workbook(source,rich_text=True)
        result=load_workbook(out,rich_text=True)
        try:
            for name in graph:
                if _signature(src_book[name],external)!=_signature(result[name]):
                    raise ValueError("Written worksheet differs from source: "+name)
            local={sheet:list(dep) for sheet,dep in graph.items()}
            for proof in external.proofs.values():
                bridge=proof["sheet"]
                for (consumer,cell) in external.registrations:
                    formula=src_book[consumer][cell].value
                    if isinstance(formula,str) and "["+proof["ordinal"]+"]"+bridge in formula:
                        local[consumer]=sorted(set(local[consumer])|{bridge})
                local.update(_closure(result,[bridge]))
            if _closure(result,sheets)!=local:
                raise ValueError("Written workbook has a different formula dependency closure")
        finally:
            src_book.close()
            result.close()
        for proof in external.proofs.values():
            _source(operator,proof["source_version_id"],stage,proof["source_sha256"])
        _source(operator,source_version_id,stage,version["payload"]["sha256"])
        if _sha(dest)!=before_sha:
            raise ValueError("Destination changed during import")
        receipt.update(sha256=_sha(out),changed_locators=changed)
        os.replace(out,dest)
    return receipt

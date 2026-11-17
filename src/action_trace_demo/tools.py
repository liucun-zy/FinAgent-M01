"""Restricted workspace tools: no host shell, arbitrary Python, or network tool."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from html import escape
from pathlib import Path
from typing import Any


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_document(path: Path) -> str:
    suffix = path.suffix.lower()
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("file exceeds 32 MiB tool limit")
    if suffix == ".pdf":
        from pypdf import PdfReader
        return "\n".join(f"[page {i+1}]\n{p.extract_text() or ''}" for i,p in enumerate(PdfReader(path).pages))
    if suffix == ".docx":
        from docx import Document
        doc=Document(path)
        lines=[f"[paragraph {i+1}] {p.text}" for i,p in enumerate(doc.paragraphs) if p.text.strip()]
        for ti,t in enumerate(doc.tables):
            lines += [f"[table {ti+1} row {ri+1}] "+" | ".join(c.text for c in r.cells) for ri,r in enumerate(t.rows)]
        return "\n".join(lines)
    if suffix == ".xlsx":
        from openpyxl import load_workbook
        book=load_workbook(path,read_only=True,data_only=False)
        lines=[]
        for sheet in book:
            lines.append(f"[sheet {sheet.title}]")
            for row in sheet:
                vals=[f"{c.coordinate}={c.value}" for c in row if c.value is not None]
                if vals:lines.append(" | ".join(vals))
                if len(lines)>25000:raise ValueError("workbook exceeds readable cell limit")
        book.close()
        return "\n".join(lines)
    if suffix == ".pptx":
        from pptx import Presentation
        deck=Presentation(path)
        lines=[]
        for i,slide in enumerate(deck.slides):
            lines.append(f"[slide {i+1}]")
            for shape in slide.shapes:
                if shape.has_text_frame:lines.append(shape.text)
                if shape.has_table:
                    lines += [" | ".join(c.text for c in row.cells) for row in shape.table.rows]
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                note=slide.notes_slide.notes_text_frame.text.strip()
                if note:
                    lines.append("[speaker notes] "+note)
        return "\n".join(lines)
    return path.read_text(encoding="utf-8-sig")


def write_artifact(path: Path, content: dict[str,Any]) -> None:
    """Deterministic document writers; spreadsheet formulas are not recalculated."""
    suffix=path.suffix.lower()
    title=str(content.get("title",path.stem))
    sections=content.get("sections",[])
    if suffix==".pdf":
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle,PageBreak
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        styles=getSampleStyleSheet()
        cjk=bool(re.search(r"[\u3400-\u9fff]",json.dumps(content,ensure_ascii=False)))
        for name in ["Normal","Title","Heading2"]:
            styles[name].fontName="STSong-Light" if cjk else "Helvetica" if name=="Normal" else "Helvetica-Bold"
        styles["Normal"].leading=15
        flow=[Paragraph(escape(title),styles['Title']),Spacer(1,12)]
        for s in sections:
            if s.get('page_break'):
                flow.append(PageBreak())
            if s.get('heading'):flow.append(Paragraph(escape(str(s['heading'])),styles['Heading2']))
            for text in s.get('paragraphs',[]):
                flow += [Paragraph(escape(str(text)).replace('\n','<br/>'),styles['Normal']),Spacer(1,6)]
            if s.get('table'):
                rows=[[Paragraph(escape(str(v)),styles['Normal']) for v in row] for row in s['table']]
                if rows:
                    width=(595-80)/max(len(r) for r in rows)
                    table=Table(rows,colWidths=[width]*max(len(r) for r in rows),repeatRows=1)
                    table.setStyle(TableStyle([('GRID',(0,0),(-1,-1),0.3,colors.grey),('VALIGN',(0,0),(-1,-1),'TOP')]))
                    flow += [table,Spacer(1,8)]
        SimpleDocTemplate(str(path),rightMargin=40,leftMargin=40,topMargin=40,bottomMargin=40).build(flow)
    elif suffix==".docx":
        from docx import Document
        from docx.shared import Inches, Pt
        d=Document();d.add_heading(title,0)
        d.styles['Normal'].font.name='Calibri'
        d.styles['Normal'].font.size=Pt(10)
        for section in d.sections:
            section.top_margin=section.bottom_margin=Inches(0.65)
        for s in sections:
            if s.get('page_break'):
                d.add_page_break()
            if s.get('heading'):d.add_heading(str(s['heading']),level=1)
            for t in s.get('paragraphs',[]):d.add_paragraph(str(t))
            if s.get('table'):
                rows=s['table'];table=d.add_table(rows=0,cols=max(len(r) for r in rows));table.style='Table Grid'
                for row in rows:
                    cells=table.add_row().cells
                    for i,v in enumerate(row):cells[i].text=str(v)
        d.save(path)
    elif suffix==".xlsx":
        from openpyxl import Workbook
        from openpyxl.styles import Font
        from openpyxl.workbook.properties import CalcProperties
        wb=Workbook();wb.remove(wb.active)
        for spec in content.get('sheets',[]):
            ws=wb.create_sheet(str(spec['name'])[:31])
            for row in spec.get('rows',[]):ws.append(row)
            for addr,value in spec.get('cells',{}).items():ws[addr]=value
            if ws.max_row:
                for c in ws[1]:c.font=Font(bold=True)
            ws.freeze_panes='A2'
        if not wb.worksheets:raise ValueError('xlsx requires at least one sheet')
        wb.calculation=CalcProperties(fullCalcOnLoad=True)
        wb.save(path)
    elif suffix==".pptx":
        from pptx import Presentation
        from pptx.util import Inches, Pt
        prs=Presentation()
        prs.slide_width=Inches(13.333)
        prs.slide_height=Inches(7.5)
        for s in content.get('slides',[]):
            slide=prs.slides.add_slide(prs.slide_layouts[1]);slide.shapes.title.text=str(s.get('title',''))
            shape=slide.placeholders[1]
            shape.left=Inches(0.55);shape.top=Inches(1.45);shape.width=Inches(12.2)
            shape.height=Inches(2.0 if s.get('table') else 5.25)
            tf=shape.text_frame;tf.clear();tf.word_wrap=True
            for i,text in enumerate(s.get('bullets',[])):
                p=tf.paragraphs[0] if i==0 else tf.add_paragraph();p.text=str(text);p.font.size=Pt(17)
            if s.get('table'):
                rows=s['table']
                cols=max(len(row) for row in rows)
                table_shape=slide.shapes.add_table(len(rows),cols,Inches(0.55),Inches(3.55),Inches(12.2),Inches(3.1))
                for ri,row in enumerate(rows):
                    for ci,value in enumerate(row):
                        cell=table_shape.table.cell(ri,ci);cell.text=str(value)
                        for paragraph in cell.text_frame.paragraphs:
                            paragraph.font.size=Pt(12)
                            paragraph.font.bold=ri==0
            if s.get('notes'):slide.notes_slide.notes_text_frame.text=str(s['notes'])
        prs.save(path)
    else:raise ValueError('supported artifact extensions: pdf/docx/xlsx/pptx')


def patch_presentation_slots(path: Path, replacements: dict[str,Any]) -> None:
    """Replace explicitly named text slots of an existing presentation.

    The guarded caller verifies before-images and compares all other native
    slots after saving. Every target is resolved before any XML is changed.
    Shape geometry, other shapes, charts and surrounding stories stay in the
    original package. Paragraph/run styling is reused when lengths permit.
    """
    from pptx import Presentation
    deck=Presentation(path)
    targets=[]
    for locator,value in replacements.items():
        parts=locator.split(':')
        if len(parts)<3 or parts[0]!='slide':
            raise ValueError(f'Unsupported presentation locator: {locator}')
        try:
            slide=deck.slides[int(parts[1])-1] if int(parts[1])>0 else None
            if slide is None:
                raise IndexError
            if len(parts)==3 and parts[2]=='notes' and slide.has_notes_slide:
                frame=slide.notes_slide.notes_text_frame
            elif len(parts) in {4,7} and parts[2]=='shape':
                shape=slide.shapes[int(parts[3])-1] if int(parts[3])>0 else None
                if shape is None:
                    raise IndexError
                if len(parts)==4 and shape.has_text_frame:
                    frame=shape.text_frame
                elif len(parts)==7 and parts[4]=='cell' and shape.has_table:
                    ri,ci=int(parts[5])-1,int(parts[6])-1
                    if min(ri,ci)<0:
                        raise IndexError
                    frame=shape.table.cell(ri,ci).text_frame
                else:
                    raise ValueError(f'Presentation slot is not text: {locator}')
            else:
                raise ValueError(f'Unsupported presentation locator: {locator}')
        except (IndexError,KeyError,ValueError) as exc:
            raise ValueError(f'Presentation text slot does not exist: {locator}') from exc
        if frame is None:
            raise ValueError(f'Presentation text frame unavailable: {locator}')
        targets.append((frame,str(value)))
    for frame,text in targets:
        lines=text.split('\n')
        if len(lines)==len(frame.paragraphs):
            for paragraph,line in zip(frame.paragraphs,lines):
                if paragraph.text==line:
                    continue
                if len(paragraph.runs)==1:
                    paragraph.runs[0].text=line
                else:
                    paragraph.text=line
        else:
            frame.text=text
    deck.save(path)


class WorkspaceTools:
    def __init__(self,workspace:Path,run_dir:Path,trace,stage:int,*,read_only:bool=False):
        self.workspace=workspace.resolve();self.run_dir=run_dir;self.trace=trace;self.stage=stage;self.read_only=read_only
        self.artifact_log=run_dir/'artifact_events.jsonl'
        self.delivery_log=run_dir/'source_deliveries.jsonl'
        self.artifact_events=[]
        if self.artifact_log.exists():
            self.artifact_events=[x for x in (json.loads(s) for s in self.artifact_log.read_text().splitlines()) if x['stage']<=stage]
        self.delivered={}
        visibility=run_dir/'source_visibility.json'
        self.source_visibility=json.loads(visibility.read_text()) if visibility.exists() else None
        if self.delivery_log.exists():
            for line in self.delivery_log.read_text().splitlines():
                item=json.loads(line)
                if item['stage']<=stage:self.delivered[item['source_id']]=item['stage']

    def _path(self,value:str,*,write:bool=False)->Path:
        value=value.removeprefix('/workspace/').lstrip('/') if value.startswith('/workspace/') else value
        rel=Path(value)
        if rel.is_absolute() or '..' in rel.parts:raise PermissionError('path outside candidate workspace')
        if tuple(rel.parts[:2])==('working','current'):
            rel=Path('working','checkpoints',f'T{self.stage:02d}',*rel.parts[2:])
        path=(self.workspace/rel).resolve()
        path.relative_to(self.workspace)
        if not rel.parts or rel.parts[0] not in {'source','working','deliverables'}:
            raise PermissionError('only source, working, deliverables are visible')
        cursor=self.workspace
        for part in rel.parts:
            cursor=cursor/part
            if cursor.is_symlink():raise PermissionError('symlinks are not accessible')
        if not write and path.is_file() and not self._visible(path):
            raise PermissionError('file is not visible at this stage cutoff')
        if write:
            if self.read_only:raise PermissionError('query session is read-only')
            allowed=[self.workspace/'working'/'checkpoints'/f'T{self.stage:02d}',self.workspace/'deliverables'/'final']
            if not any(path.is_relative_to(p) for p in allowed):raise PermissionError('write only current checkpoint or final deliverables')
            path.parent.mkdir(parents=True,exist_ok=True)
        return path

    def _visible(self,path:Path)->bool:
        rel=path.relative_to(self.workspace)
        if rel.parts[:2]==('working','checkpoints') and len(rel.parts)>2:
            match=re.fullmatch(r'T(\d+)',rel.parts[2])
            if match and int(match.group(1))>self.stage:return False
        if rel.parts and rel.parts[0]=='source' and self.source_visibility is not None:
            return self.source_visibility.get(rel.as_posix(),10**9)<=self.stage
        return True

    @staticmethod
    def _append(path:Path,row:dict)->None:
        with path.open('a',encoding='utf-8') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')

    def _evidence(self,path:Path,text:str,start:int,max_chars:int,*,logical_path:str|None=None)->dict:
        start=max(0,start);max_chars=max(1,min(max_chars,16000));chunk=text[start:start+max_chars]
        rel=logical_path or str(path.relative_to(self.workspace));sid='src_'+hashlib.sha256((rel+'\0'+str(start)+'\0'+chunk).encode()).hexdigest()[:20]
        self.trace.register_source(sid,f'{rel}#chars={start}:{start+len(chunk)}',chunk,self.stage)
        self.delivered[sid]=self.stage
        self._append(self.delivery_log,{'stage':self.stage,'source_id':sid,'path':rel,'start':start,'end':start+len(chunk),'sha256':hashlib.sha256(chunk.encode()).hexdigest()})
        return {'source_id':sid,'path':rel,'start':start,'end':start+len(chunk),'total_chars':len(text),'text':chunk,'truncated':start+len(chunk)<len(text)}

    def _file_change(self,path:Path,op:str,args:dict,writer)->dict:
        refs=args.get('source_ids',[]);claims=args.get('claim_ids',[])
        if not isinstance(args.get('description'),str) or not args['description'].strip():
            raise ValueError('artifact edits require a nonempty change description')
        if not refs and not claims:raise ValueError('artifact edits require source_ids or claim_ids and a change description')
        if any(s not in self.delivered for s in refs):raise PermissionError('source was not delivered in this visible run')
        visible={x['claim_id'] for x in self.trace.ledger(self.stage)}
        if any(c not in visible for c in claims):raise PermissionError('claim is not in the visible ledger')
        aid='file_'+uuid.uuid4().hex[:16]
        before=digest(path) if path.is_file() else None
        versions=self.run_dir/'artifact_versions'/aid;versions.mkdir(parents=True)
        if path.is_file():shutil.copy2(path,versions/('before'+path.suffix))
        staged=versions/('after'+path.suffix)
        if op!='delete':
            writer(staged)
            if not staged.is_file():raise ValueError('artifact writer produced no file')
            shutil.copy2(staged,path)
        elif path.exists():path.unlink()
        after=digest(path) if path.is_file() else None
        event={'id':aid,'type':'artifact_action','stage':self.stage,'op':op,'path':str(path.relative_to(self.workspace)),
               'before_sha256':before,'after_sha256':after,'source_ids':refs,'claim_ids':claims,
               'description':args.get('description',''),'structural_status':'passed','semantic_status':'model_assessed'}
        if args.get('copy_source_path'):event['copy_source_path']=args['copy_source_path']
        self._append(self.artifact_log,event)
        self.artifact_events.append(event)
        return {'status':'written' if op!='delete' else 'deleted','action_id':aid,'path':event['path'],'sha256':after}

    def call(self,name:str,args:dict)->dict:
        if name=='list_files':
            p=self._path(args.get('path','source'))
            files=[q for q in p.rglob('*') if q.is_file()] if p.is_dir() else [p]
            return {'files':[{'path':str(q.relative_to(self.workspace)),'bytes':q.stat().st_size} for q in sorted(files) if not q.is_symlink() and self._visible(q)][:300]}
        if name=='read_file':
            p=self._path(args['path']);text=read_document(p)
            return self._evidence(p,text,int(args.get('start',0)),int(args.get('max_chars',12000)))
        if name=='search_files':
            root=self._path(args.get('path','source'));query=args['query'].lower();hits=[]
            for p in sorted(root.rglob('*')):
                if not p.is_file() or p.is_symlink() or not self._visible(p):continue
                try:text=read_document(p)
                except Exception:continue
                at=text.lower().find(query)
                if at>=0:hits.append(self._evidence(p,text,max(0,at-300),1500))
                if len(hits)>=int(args.get('limit',5)):break
            return {'hits':hits}
        if name=='record_claims':
            if self.read_only:raise PermissionError('query session cannot change claims')
            return self.trace.propose(args['edits'],stage=self.stage,rationale=args.get('description',''),read_claim_ids=args.get('read_claim_ids',[]))
        if name=='recall_claims':
            limit=max(1,min(int(args.get('limit',8)),50))
            result=self.trace.lookup(args['query'],stage=self.stage,limit=limit)
            claim_ids={x['claim_id'] for x in result.get('claims',[])}
            terms=set(re.findall(r'[a-z0-9_]+',args['query'].lower()))
            ranked=[]
            for event in self.artifact_events:
                score=len(terms & set(re.findall(r'[a-z0-9_]+',(event['path']+' '+event.get('description','')).lower())))
                linked=bool(claim_ids & set(event.get('claim_ids',[])))
                if score or linked:ranked.append((int(linked),score,event))
            ranked.sort(key=lambda row:row[:2],reverse=True)
            result['artifact_actions']=[r[2] for r in ranked[:limit]]
            return result
        if name=='expand_actions':
            requested=args['action_ids'];result=self.trace.expand([x for x in requested if not x.startswith('file_')],stage=self.stage)
            selected=[e for e in self.artifact_events if e['id'] in requested]
            result['artifact_actions']=selected
            paths={e['path'] for e in selected}
            result['related_artifact_actions']=[e for e in self.artifact_events if e['path'] in paths and e['id'] not in requested]
            return result
        if name=='read_evidence':return self.trace.read_source(args['source_id'],stage=self.stage,start=int(args.get('start',0)),max_chars=min(int(args.get('max_chars',8000)),16000))
        if name=='read_artifact_version':
            aid=args['action_id'];side=args.get('version','after')
            event=next((e for e in self.artifact_events if e['id']==aid),None)
            if event is None or side not in {'before','after'}:raise PermissionError('artifact action/version is not visible')
            ext=Path(event['path']).suffix
            p=self.run_dir/'artifact_versions'/aid/(side+ext)
            if not p.is_file():return {'error':'artifact_version_missing','action_id':aid,'version':side}
            if digest(p)!=event.get(side+'_sha256'):raise ValueError('artifact version hash mismatch')
            return self._evidence(p,read_document(p),int(args.get('start',0)),int(args.get('max_chars',8000)),logical_path=f'artifact_version/{aid}/{side}{ext}')
        if name=='write_text':
            p=self._path(args['path'],write=True)
            if p.suffix.lower() in {'.pdf','.docx','.xlsx','.pptx'}:raise ValueError('use write_artifact for binary office formats')
            return self._file_change(p,'write_text',args,lambda temp:temp.write_text(args['text'],encoding='utf-8'))
        if name=='write_artifact':
            p=self._path(args['path'],write=True)
            return self._file_change(p,'write_artifact',args,lambda temp:write_artifact(temp,args['content']))
        if name=='copy_file':
            src=self._path(args['source']);p=self._path(args['path'],write=True)
            return self._file_change(p,'copy',{**args,'copy_source_path':str(src.relative_to(self.workspace))},lambda temp:shutil.copy2(src,temp))
        if name=='delete_file':
            p=self._path(args['path'],write=True)
            return self._file_change(p,'delete',args,lambda temp:None)
        raise ValueError('unknown tool')


def function(name:str,description:str,properties:dict,required:list[str]|None=None)->dict:
    return {'type':'function','function':{'name':name,'description':description,'parameters':{'type':'object','properties':properties,'required':required or [],'additionalProperties':False}}}


def tool_specs(*,read_only:bool=False)->list[dict]:
    string={'type':'string'};strings={'type':'array','items':string};integer={'type':'integer'}
    specs=[
        function('list_files','List only files already released to the candidate workspace.',{'path':string}),
        function('read_file','Read PDF/DOCX/XLSX/PPTX/text with source_id and character offsets; paginate until needed evidence is obtained. XLSX formulas are not recalculated.',{'path':string,'start':integer,'max_chars':integer},['path']),
        function('search_files','Search actually visible documents for a phrase; returns source-grounded excerpts.',{'path':string,'query':string,'limit':integer},['query']),
        function('recall_claims','Retrieve current and historical ledger claims with related update actions.',{'query':string,'limit':integer},['query']),
        function('expand_actions','Inspect old/new claim comparisons, cited evidence and accepted/rejected actions.',{'action_ids':strings},['action_ids']),
        function('read_evidence','Read an already registered source excerpt by source_id.',{'source_id':string,'start':integer,'max_chars':integer},['source_id']),
        function('read_artifact_version','Inspect an actually saved before/after version of a visible file action, with hash validation.',{'action_id':string,'version':{'enum':['before','after']},'start':integer,'max_chars':integer},['action_id','version']),
    ]
    if read_only:return specs
    edits={'type':'array','items':{'type':'object','properties':{'op':{'enum':['create','update','retract','noop']},'entity':string,'field':string,'value':{},'old_claim_id':string,'source_ids':strings,'quote':string,'valid_from':string,'valid_to':string,'scope':string,'description':string},'required':['op','entity','field','source_ids','quote','description']}}
    refs={'source_ids':strings,'claim_ids':strings,'description':string}
    specs += [
        function('record_claims','Before using financial findings in deliverables, record the claim, exact original quote and comparison with old_claim_id when updating. Guard validates citation structure, not semantic truth. Keep NOOP comparisons too.',{'edits':edits,'description':string,'read_claim_ids':strings},['edits']),
        function('write_text','Write UTF-8 text only in current checkpoint or final. Cite delivered sources/claims and explain changes.',{'path':string,'text':string,**refs},['path','text','description']),
        function('write_artifact','Create pdf/docx with content={title,sections:[{heading,paragraphs:[string],table:[[string]]}]}; xlsx with {sheets:[{name,rows:[[value]],cells:{A1:value}}]}; pptx with {slides:[{title,bullets:[string],notes:string}]}. This deterministic renderer executes no code. Workbook formulas are saved but not recalculated.',{'path':string,'content':{'type':'object'},**refs},['path','content','description']),
        function('copy_file','Copy an earlier artifact into current checkpoint/final while preserving history.',{'source':string,'path':string,**refs},['source','path','description']),
        function('delete_file','Delete only a current/final artifact; archive old bytes and retain the action.',{'path':string,**refs},['path','description']),
    ]
    return specs

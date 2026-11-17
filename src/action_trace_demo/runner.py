"""Persistent candidate workspace, explicit tool loop, and auditable local runs."""
from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

from .benchmark import DeliverBenchCase
from .model import ModelClient
from .responder import ResponderBridge
from .observation import compact_tool_result
from .tools import WorkspaceTools, tool_specs
from .trace import TraceStore


SYSTEM = """You are the Candidate financial project agent in a local DeliverBench research demo.
Work only through the provided restricted workspace tools. Materials arrive by stage;
future instructions, authored references, evaluation rubrics and simulator internals
are unavailable. Never try to retrieve them. Do not infer a completed action merely
from a plan or copy an unsupported old conclusion. Preserve distinctions between
facts, assumptions, decisions, business dates, and information cutoffs.

Maintain an evidence-linked ledger with record_claims as you interpret material.
For material revisions, recall existing claims, identify old_claim_id and preserve
the old/new comparison and a short decision description. Each factual claim cites
source_ids and an exact quote from an actually read excerpt. Preserve useful NOOP
comparisons. Do not invent IDs or describe a citation check as semantic verification.
Use the ledger and action graph to reuse earlier work, and read original material
when a decision's scope, timing, or support is uncertain.

Produce the requested professional deliverables under /workspace/working/current/
or /workspace/deliverables/final/. Older checkpoints and sources are read-only.
Artifact edits require source_ids or claim_ids plus a short description of the change.
Use write_artifact for actual PDF/DOCX/XLSX/PPTX files. It is a deterministic renderer,
not a code execution tool. It does NOT calculate spreadsheet formulas. Quantitative
conclusions must explain assumptions and limitations; do not claim recalculation.
Follow the project's conventions and output language in the released source.
When finished, state exactly what was delivered and any unresolved limitations.
Do not claim an official benchmark score or verified acceptance.
"""

QUERY_SYSTEM = """You are auditing a Candidate project's evidence-linked state.
Answer the user's question from the visible ledger, action records and source excerpts.
Read-only tools enforce the selected stage cutoff. Start with recall_claims; inspect
the update action when scope, version, time or support can change the answer. Seek
contradictory or corrective branches as well as support. Read source context when
needed. An action's description is a model assertion, not proof. Cite claim/action/
source IDs, state uncertainties, and do not repair the project during this query.
"""


def save(path:Path,data)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    temp.replace(path)


def append(path:Path,data)->None:
    with path.open('a',encoding='utf-8') as f:f.write(json.dumps(data,ensure_ascii=False)+'\n')


def json_safe_completion_message(message:dict)->dict:
    result={'role':'assistant','content':message.get('content')}
    if message.get('tool_calls'):result['tool_calls']=message['tool_calls']
    return result


def memory_packet(tools:WorkspaceTools)->str:
    rows=tools.trace.ledger(tools.stage)
    heads=[]
    for claim in rows[-80:]:
        head={k:claim.get(k) for k in ['claim_id','entity','field','value','scope','valid_from','valid_to','action_id','source_ids']}
        encoded=json.dumps(head.get('value'),ensure_ascii=False)
        if len(encoded)>2000:
            head.pop('value',None);head['value_preview']=encoded[:2000];head['value_truncated']=True
        heads.append(head)
    while heads and len(json.dumps(heads,ensure_ascii=False))>60000:heads.pop(0)
    return json.dumps({'memory_kind':'model_assessed_ledger_not_verified_truth','ledger_heads':heads,
        'omitted_claim_count':max(0,len(rows)-len(heads)),
        'available_claim_ids':[r['claim_id'] for r in rows],
        'access':'Use recall_claims/expand_actions/read_evidence and the persistent workspace to recover previous details.'},ensure_ascii=False)


def bound_context(messages:list[dict],tools:WorkspaceTools,limit:int,log:Path)->list[dict]:
    if not limit or len(json.dumps(messages,ensure_ascii=False))<=limit:return messages
    request=next((m for m in reversed(messages) if m.get('role')=='user' and str(m.get('content','')).startswith('<stage_request')),None)
    if request is None:raise RuntimeError('context limit reached without a recoverable stage request')
    latest=next((i for i in range(len(messages)-1,-1,-1) if messages[i].get('role')=='assistant'),len(messages))
    # Preserve the newest complete assistant/tool exchange. Older exchanges
    # already reached the model and remain in the immutable raw call log.
    tail=messages[latest:]
    compact=[messages[0],{'role':'user','content':'<project_memory>'+memory_packet(tools)+'</project_memory>'},request,*tail]
    if len(json.dumps(compact,ensure_ascii=False))>limit:
        raise RuntimeError('latest tool exchange exceeds configured context budget; no silent truncation')
    append(log,{'task':'context_compaction','stage':tools.stage,'before_chars':len(json.dumps(messages,ensure_ascii=False)),
                'after_chars':len(json.dumps(compact,ensure_ascii=False)),'policy':'ledger_and_latest_complete_exchange'})
    return compact


def run_loop(client,tools:WorkspaceTools,messages:list[dict],*,max_turns:int,task:str,log:Path,mode:str='graph_router',context_limit_chars:int=0,turn_offset:int=0,force_final_turn:bool=False)->dict:
    specs=tool_specs(read_only=tools.read_only)
    if tools.read_only and mode=='flat_trace':specs=[s for s in specs if s['function']['name']!='expand_actions']
    allowed_tools={s['function']['name'] for s in specs}
    for turn in range(max_turns):
        messages=bound_context(messages,tools,context_limit_chars,log)
        forced_final=(tools.read_only or force_final_turn) and turn==max_turns-1
        if forced_final:
            if tools.read_only:
                final_instruction='The query tool budget is exhausted. Give your final answer now using only the evidence already delivered; explicitly identify anything still unsupported. Do not request more tools or claim a full audit.'
            else:
                final_instruction='This is the last remaining Candidate turn within the configured stage budget. No tools are available on this turn. Give your final response using only the evidence and files already produced. State accurately which deliverables are complete and which issues or missing deliverables remain unresolved. Do not claim that missing files were generated, that unsupported conclusions were verified, or that the stage was accepted. Do not request more tools.'
            messages.append({'role':'user','content':final_instruction})
        r=client.complete(messages,tools=None if forced_final else specs,task=task)
        choices=r.get('choices') or []
        if not choices:raise RuntimeError('model returned no completion choice')
        message=json_safe_completion_message(choices[0].get('message') or {})
        messages.append(message)
        save(log.parent/'conversation.json',messages)
        append(log,{'task':task,'stage':tools.stage,'turn':turn+turn_offset,'role':'assistant','message':message,'finish_reason':choices[0].get('finish_reason')})
        if choices[0].get('finish_reason')=='length':
            return {'status':'output_truncated','answer':message.get('content'),'turns':turn+1,'messages':messages}
        calls=message.get('tool_calls') or []
        if not calls:
            return {'status':'responded','answer':message.get('content') or '', 'turns':turn+1,'messages':messages,'budget_forced_final':forced_final}
        for call in calls:
            name=call.get('function',{}).get('name','')
            arguments={}
            try:
                if forced_final or name not in allowed_tools:raise PermissionError('tool is not available in this session')
                arguments=json.loads(call['function'].get('arguments') or '{}')
                if not isinstance(arguments,dict):raise ValueError('tool arguments must be an object')
                result=tools.call(name,arguments)
                original_result=result
                result=compact_tool_result(name,result)
                if result.get('observation_compacted'):
                    raw_dir=log.parent/'raw_tool_outputs';raw_dir.mkdir(exist_ok=True)
                    save(raw_dir/(call['id']+'.json'),original_result)
            except Exception as exc:
                # Tool exceptions are local protocol failures; no credential or
                # arbitrary host traceback is returned to the evaluated model.
                result={'error':type(exc).__name__,'detail':str(exc)[:800]}
            text=json.dumps(result,ensure_ascii=False)
            messages.append({'role':'tool','tool_call_id':call['id'],'content':text})
            save(log.parent/'conversation.json',messages)
            append(log,{'task':task,'stage':tools.stage,'turn':turn+turn_offset,'tool_call_id':call['id'],'tool':name,'arguments':arguments,'result':result})
    return {'status':'turn_limit','answer':None,'turns':max_turns,'messages':messages}


def _expected_present(info:dict,tools:WorkspaceTools)->tuple[list[str],list[str]]:
    expected=info.get('expected_outputs',[])
    found=[];missing=[]
    for item in expected:
        try:
            path=tools._path(str(item))
            (found if path.is_file() and path.stat().st_size else missing).append(str(item))
        except (ValueError,PermissionError):missing.append(str(item))
    return found,missing


def export_run(run_dir:Path,trace:TraceStore,client:ModelClient,status:dict)->dict:
    files=trace.export()
    artifact_path=run_dir/'artifact_events.jsonl'
    artifact_events=[json.loads(s) for s in artifact_path.read_text().splitlines()] if artifact_path.exists() else []
    # Keep the core graph untouched; augment an export for document revisions.
    graph_path=run_dir/'graph.json'
    graph=json.loads(graph_path.read_text()) if graph_path.exists() else {'nodes':[],'edges':[]}
    visible_stage=max([int(graph.get('stage',0)),int(status.get('current_stage',0))]+[e['stage'] for e in artifact_events])
    graph['stage']=visible_stage
    save(run_dir/'ledger.json',{'stage':visible_stage,'claims':trace.ledger(visible_stage),'semantic_correctness_verified':False})
    nodes=graph.setdefault('nodes',[]);edges=graph.setdefault('edges',[])
    if isinstance(nodes,list) and isinstance(edges,list):
        last_file_version={}
        for event in artifact_events:
            nodes.append({**event,'kind':'artifact_action'})
            def edge(source,target,kind,basis='runtime_observed'):
                edges.append({'from':source,'to':target,'kind':kind,'stage':event['stage'],'evidence_basis':basis})
            for cid in event.get('claim_ids',[]):edge(cid,event['id'],'claimed_used_by','model_declared')
            for sid in event.get('source_ids',[]):edge(sid,event['id'],'claimed_justification','model_declared')
            if event.get('before_sha256'):
                prior=last_file_version.get(event['path'])
                if prior is None:
                    prior=event['id']+':before'
                    nodes.append({'id':prior,'kind':'artifact_version','path':event['path'],'sha256':event['before_sha256'],'stage':event['stage']})
                edge(prior,event['id'],'reads_previous_version')
            copy_source=last_file_version.get(event.get('copy_source_path',''))
            if copy_source:edge(copy_source,event['id'],'copies_from')
            if event.get('after_sha256'):
                current=event['id']+':after'
                nodes.append({'id':current,'kind':'artifact_version','path':event['path'],'sha256':event['after_sha256'],'stage':event['stage']})
                edge(event['id'],current,'produces');last_file_version[event['path']]=current
            else:last_file_version.pop(event['path'],None)
        save(run_dir/'action_graph.json',graph)
    metrics=client.metrics();metrics['artifact_actions']=len(artifact_events)
    responder_metrics=run_dir/'private_responder/metrics.json'
    if responder_metrics.exists():
        metrics['responder']=json.loads(responder_metrics.read_text())
    save(run_dir/'metrics.json',metrics)
    save(run_dir/'run_status.json',status)
    from .report import render_report
    report=render_report(run_dir)
    return {'run_dir':str(run_dir),'status':status,'metrics':metrics,'exports':files,'report':str(report)}


def run_case(case_root:Path,run_dir:Path,config:dict,*,through_stage:int=1)->dict:
    run_dir=run_dir.resolve()
    if run_dir.exists() and any(run_dir.iterdir()):raise ValueError('run directory must be new; use a new run ID rather than overwrite evidence')
    run_dir.mkdir(parents=True,exist_ok=True)
    workspace=run_dir/'workspace'
    bridge=ResponderBridge(case_root,run_dir,config) if config.get('enable_responder',True) else None
    case=DeliverBenchCase(case_root,workspace,responder=bridge)
    trace=TraceStore(run_dir)
    client=ModelClient(config,run_dir)
    candidate_turn_budget=int(config.get('candidate_max_turns_per_stage',35))
    candidate_force_final_turn=bool(config.get('candidate_force_final_turn',False))
    save(run_dir/'run_manifest.json',{'schema_version':'0.2','mode':'local_candidate_only_research',
         'official_deliverbench_score':False,'model':client.model,'case_root':str(case_root),
         'requested_through_stage':through_stage,'input_rmb_per_million':0.8,'output_rmb_per_million':4.0,
         'tool_capability':'restricted_file_and_document_tools_no_shell','created_unix':time.time(),
         'context_policy':config.get('context_policy','full'),
         'context_limit_chars':config.get('context_limit_chars',0),
         'candidate_max_turns_per_stage':candidate_turn_budget,
         'candidate_force_final_turn':candidate_force_final_turn,
         'candidate_final_turn_policy':'last_budgeted_turn_summary_without_tools' if candidate_force_final_turn else 'unreserved_turn_budget',
         'candidate_final_summary_within_turn_budget':True})
    status={'state':'initializing','completed_stages':[],'official_score':None}
    messages=[{'role':'system','content':SYSTEM}]
    log=run_dir/'agent_trajectory.jsonl'
    try:
        opening=case.initialize()
        visibility={path:0 for path in opening.get('released_files',[])}
        save(run_dir/'source_visibility.json',visibility)
        messages.append({'role':'user','content':opening['instruction']+'\nAcknowledge the project conventions briefly before the first stage.'})
        response=client.complete(messages,task='opening',max_tokens=512)
        m=json_safe_completion_message(response['choices'][0]['message']);messages.append(m)
        append(log,{'task':'opening','message':m})
        for stage in range(1,through_stage+1):
            info=case.release(stage)
            visibility.update({path:stage for path in info.get('released_files',[])})
            save(run_dir/'source_visibility.json',visibility)
            tools=WorkspaceTools(workspace,run_dir,trace,stage)
            status.update(state='running',current_stage=stage)
            save(run_dir/'run_status.json',status)
            prompt=info['instruction']+'\nNewly released files: '+json.dumps(info.get('released_files',[]),ensure_ascii=False)
            if config.get('context_policy')=='stage_ledger':
                recent=[]
                for previous in status['completed_stages'][-3:]:
                    row=json.loads((run_dir/'stage_results'/f'T{previous:02d}.json').read_text())
                    recent.append({'stage':previous,'completion_note':str(row.get('response',''))[:4000]})
                messages=[{'role':'system','content':SYSTEM},{'role':'user','content':'<project_memory>'+memory_packet(tools)+'\nRecent completion notes: '+json.dumps(recent,ensure_ascii=False)+'</project_memory>'}]
                append(log,{'task':'stage_context','stage':stage,'policy':'stage_ledger','completed_stages':status['completed_stages'][:]})
            messages.append({'role':'user','content':f'<stage_request id="T{stage:02d}">\n'+prompt+'\n</stage_request>'})
            remaining=candidate_turn_budget
            for attempt in range(3):
                result=run_loop(client,tools,messages,max_turns=remaining,task=f'T{stage:02d}',log=log,context_limit_chars=int(config.get('context_limit_chars',0)),force_final_turn=candidate_force_final_turn)
                messages=result['messages'];remaining-=result['turns']
                found,missing=_expected_present(info,tools)
                if result['status']!='responded':
                    status.update(state='candidate_'+result['status'],missing_outputs=missing);break
                if not missing:
                    frozen=case.freeze(stage)
                    if frozen.get('submission_complete') is False:
                        status.update(state='incomplete_submission',freeze=frozen);break
                    status['completed_stages'].append(stage);save(run_dir/'stage_results'/f'T{stage:02d}.json',{'response':result['answer'],'found_outputs':found,'freeze':frozen})
                    break
                messages.append({'role':'user','content':'The requested deliverables are still missing or empty: '+json.dumps(missing)+'. Complete them before ending this stage.'})
            else:status.update(state='incomplete_submission',missing_outputs=missing)
            save(run_dir/'conversation.json',messages)
            if stage not in status['completed_stages']:break
        else:status.update(state='requested_stages_completed')
    except Exception as exc:
        status.update(state='failed',error_type=type(exc).__name__,error=str(exc)[:1000])
        if not (run_dir/'conversation.json').exists():save(run_dir/'conversation.json',messages)
    else:save(run_dir/'conversation.json',messages)
    return export_run(run_dir,trace,client,status)


def ask_run(run_dir:Path,config:dict,question:str,*,stage:int|None=None,mode:str='graph_router')->dict:
    status=json.loads((run_dir/'run_status.json').read_text())
    completed=status.get('completed_stages',[])
    cutoff=stage if stage is not None else max(completed or [status.get('current_stage',0)])
    if cutoff<1 or cutoff>max(completed or [0]):raise ValueError('query cutoff must be a completed stage')
    qid='q_'+uuid.uuid4().hex[:12];qdir=run_dir/'queries'/qid;qdir.mkdir(parents=True)
    # Give each query a physical historical view and independent trace fork.
    # Filtering only graph nodes would still leak later source files or final
    # artifacts through read_file/list_files in the persistent workspace.
    manifest=json.loads((run_dir/'run_manifest.json').read_text())
    DeliverBenchCase(Path(manifest['case_root']),run_dir/'workspace').status()
    visibility=json.loads((run_dir/'source_visibility.json').read_text())
    query_workspace=qdir/'workspace';query_workspace.mkdir()
    for rel,released in visibility.items():
        if released<=cutoff:
            dest=query_workspace/rel;dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(run_dir/'workspace'/rel,dest)
    for prior in range(1,cutoff+1):
        rel=Path('working/checkpoints')/f'T{prior:02d}'
        if (run_dir/'workspace'/rel).exists():shutil.copytree(run_dir/'workspace'/rel,query_workspace/rel)
    frozen_final=run_dir/'freezes/workspace'/f'T{cutoff:02d}'/'evidence/deliverables/final'
    if frozen_final.exists():shutil.copytree(frozen_final,query_workspace/'deliverables/final')
    save(qdir/'source_visibility.json',visibility)
    for name in ['trace_events.jsonl','source_deliveries.jsonl','artifact_events.jsonl']:
        if (run_dir/name).exists():
            rows=[json.loads(line) for line in (run_dir/name).read_text().splitlines()]
            visible_rows=[row for row in rows if row.get('stage',0)<=cutoff]
            (qdir/name).write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in visible_rows),encoding='utf-8')
            if name=='artifact_events.jsonl':
                for row in visible_rows:
                    original=run_dir/'artifact_versions'/row['id']
                    if original.exists():shutil.copytree(original,qdir/'artifact_versions'/row['id'])
    trace=TraceStore(qdir);client=ModelClient(config,run_dir)
    tools=WorkspaceTools(query_workspace,qdir,trace,cutoff,read_only=True)
    messages=[{'role':'system','content':QUERY_SYSTEM},{'role':'user','content':question}]
    if mode=='always_source':messages[0]['content']+=' Always read original source evidence before answering.'
    if mode=='fixed_depth':messages[0]['content']+=' Expand the relevant action once; use original evidence only if still necessary.'
    result=run_loop(client,tools,messages,max_turns=int(config.get('query_max_turns',6)),task=qid,log=qdir/'trajectory.jsonl',mode=mode)
    output={'question_id':qid,'question':question,'stage_cutoff':cutoff,'routing_mode':mode,'answer':result['answer'],'status':result['status'],'turns':result['turns'],'budget_forced_final':result.get('budget_forced_final',False),'official_score':None}
    save(qdir/'answer.json',output)
    metrics=json.loads((run_dir/'metrics.json').read_text()) if (run_dir/'metrics.json').exists() else {}
    metrics.update(client.metrics());save(run_dir/'metrics.json',metrics)
    from .report import render_report
    render_report(run_dir)
    return output

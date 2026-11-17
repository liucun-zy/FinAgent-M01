"""Exact continuation after the observed graph-context transport failure only."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import uuid

from .benchmark import DeliverBenchCase
from .model import ModelClient
from .observation import compact_tool_result
from .recovery import restore_last_exchange
from .responder import ResponderBridge
from .runner import SYSTEM,_expected_present,append,export_run,memory_packet,run_loop,save
from .tools import WorkspaceTools
from .trace import TraceStore


def resume_context_failure(run_dir:Path,config:dict,*,through_stage:int=13)->dict:
    run_dir=run_dir.resolve()
    status=json.loads((run_dir/'run_status.json').read_text())
    if status.get('state')!='failed' or status.get('error_type')!='RuntimeError' or 'context budget' not in status.get('error',''):
        raise ValueError('Only the recorded oversized-context infrastructure failure is eligible for this recovery')
    recovered=restore_last_exchange(run_dir)
    current=status['current_stage']
    if recovered['task']!=f'T{current:02d}':raise ValueError('Recovery task does not match interrupted stage')
    manifest=json.loads((run_dir/'run_manifest.json').read_text())
    case_root=Path(manifest['case_root'])
    bridge=ResponderBridge(case_root,run_dir,config)
    case=DeliverBenchCase(case_root,run_dir/'workspace',responder=bridge)
    observed=case.status()
    if observed['current_stage']!=current:raise ValueError('Workspace checkpoint changed')
    audit=run_dir/'recoveries'/('context_'+uuid.uuid4().hex[:10]);audit.mkdir(parents=True)
    shutil.copy2(run_dir/'run_status.json',audit/'original_run_status.json')
    if (run_dir/'conversation.json').exists():shutil.copy2(run_dir/'conversation.json',audit/'original_conversation.json')
    messages=recovered.pop('messages');save(audit/'reconstructed_conversation.json',messages)
    save(audit/'recovery_manifest.json',{**recovered,'kind':'technical_context_transport_recovery','candidate_outputs_regenerated':False,
         'policy_change':'bounded graph observations with explicit previews; no source content truncation'})
    names={c['id']:c['function']['name'] for m in messages if m.get('role')=='assistant' for c in m.get('tool_calls',[])}
    for message in messages:
        if message.get('role')=='tool' and message.get('tool_call_id') in names:
            raw=json.loads(message['content']);message['content']=json.dumps(compact_tool_result(names[message['tool_call_id']],raw),ensure_ascii=False)
    save(run_dir/'conversation.json',messages)
    status.update(state='running',recovery_count=status.get('recovery_count',0)+1,recovery_audit=str(audit.relative_to(run_dir)))
    status.pop('error',None);status.pop('error_type',None);save(run_dir/'run_status.json',status)
    trace=TraceStore(run_dir);client=ModelClient(config,run_dir)
    visibility=json.loads((run_dir/'source_visibility.json').read_text());log=run_dir/'agent_trajectory.jsonl'
    append(log,{'task':'technical_recovery','stage':current,'recovery_audit':str(audit.relative_to(run_dir)),'completed_turns':recovered['completed_turns']})
    try:
        for stage in range(current,through_stage+1):
            if stage==current:
                info={'instruction':(case_root/'task/steps'/f'T{stage:02d}'/'instruction.md').read_text(),
                      'expected_outputs':case._expected_outputs(f'T{stage:02d}')}
                remaining=int(config.get('candidate_max_turns_per_stage',35))-recovered['completed_turns']
            else:
                info=case.release(stage);visibility.update({p:stage for p in info['released_files']});save(run_dir/'source_visibility.json',visibility)
                tools=WorkspaceTools(run_dir/'workspace',run_dir,trace,stage)
                recent=[]
                for previous in status['completed_stages'][-3:]:
                    row=json.loads((run_dir/'stage_results'/f'T{previous:02d}.json').read_text());recent.append({'stage':previous,'completion_note':str(row.get('response',''))[:4000]})
                messages=[{'role':'system','content':SYSTEM},{'role':'user','content':'<project_memory>'+memory_packet(tools)+'\nRecent completion notes:'+json.dumps(recent,ensure_ascii=False)+'</project_memory>'},
                          {'role':'user','content':f'<stage_request id="T{stage:02d}">\n'+info['instruction']+'\nNewly released files:'+json.dumps(info['released_files'],ensure_ascii=False)+'\n</stage_request>'}]
                remaining=int(config.get('candidate_max_turns_per_stage',35))
            status.update(state='running',current_stage=stage);save(run_dir/'run_status.json',status)
            tools=WorkspaceTools(run_dir/'workspace',run_dir,trace,stage)
            for attempt in range(3):
                offset=int(config.get('candidate_max_turns_per_stage',35))-remaining
                result=run_loop(client,tools,messages,max_turns=remaining,task=f'T{stage:02d}',log=log,
                                context_limit_chars=int(config.get('context_limit_chars',0)),turn_offset=offset)
                messages=result['messages'];remaining-=result['turns'];found,missing=_expected_present(info,tools)
                if result['status']!='responded':status.update(state='candidate_'+result['status'],missing_outputs=missing);break
                if not missing:
                    frozen=case.freeze(stage)
                    if not frozen['submission_complete']:status.update(state='incomplete_submission');break
                    status['completed_stages'].append(stage)
                    save(run_dir/'stage_results'/f'T{stage:02d}.json',{'response':result['answer'],'found_outputs':found,'freeze':frozen});break
                messages.append({'role':'user','content':'Required output missing or empty: '+json.dumps(missing)+'. Complete before ending this stage.'})
            else:status.update(state='incomplete_submission',missing_outputs=missing)
            if stage not in status['completed_stages']:break
        else:status.update(state='requested_stages_completed')
    except Exception as exc:
        status.update(state='failed',error_type=type(exc).__name__,error=str(exc)[:1000])
    return export_run(run_dir,trace,client,status)


def main():
    p=argparse.ArgumentParser();p.add_argument('--run-dir',type=Path,required=True);p.add_argument('--config',type=Path,required=True)
    a=p.parse_args();print(json.dumps(resume_context_failure(a.run_dir,json.loads(a.config.read_text())),ensure_ascii=False,indent=2))


if __name__=='__main__':main()

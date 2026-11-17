"""Score frozen stages as they become available; never feed results to Candidate."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from .evaluation import score_stage
from .runner import save


def evaluate_sequence(case_root:Path,run_dir:Path,config:dict,eval_root:Path,*,wait:bool=False)->dict:
    eval_root.mkdir(parents=True,exist_ok=True)
    manifest={'case_root':str(case_root.resolve()),'run_dir':str(run_dir.resolve()),
              'run_manifest_sha256':hashlib.sha256((run_dir/'run_manifest.json').read_bytes()).hexdigest(),
              'judge_configuration_sha256':hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest(),
              'adapter_sha256':hashlib.sha256(Path(__file__).with_name('evaluation.py').read_bytes()).hexdigest()}
    manifest_path=eval_root/'sequence_manifest.json'
    if manifest_path.exists() and json.loads(manifest_path.read_text())!=manifest:
        raise ValueError('evaluation sequence inputs/configuration changed; preserve existing results and use a new directory')
    save(manifest_path,manifest)
    results=[]
    for stage in range(1,14):
        unit=f'T{stage:02d}'
        while True:
            status=json.loads((run_dir/'run_status.json').read_text())
            if stage in status.get('completed_stages',[]):break
            if not wait or status.get('state') not in {'initializing','running'}:
                summary={'mode':'local_rubric_scoring','official_harbor_run':False,'state':'candidate_incomplete',
                         'results':results,'next_unavailable_stage':unit,'candidate_status':status,'FINAL':{'status':'not_run'}}
                save(eval_root/'summary.json',summary)
                return summary
            save(eval_root/'progress.json',{'state':'waiting_for_frozen_stage','next_stage':unit,'completed_evaluations':len(results)})
            time.sleep(10)
        output=eval_root/unit
        existing=output/'evaluation_result.json'
        if existing.exists():
            result=json.loads(existing.read_text())
        else:
            save(eval_root/'progress.json',{'state':'judging','current_unit':unit,'completed_evaluations':len(results)})
            result=score_stage(case_root,run_dir,unit,config,output)
        results.append(result)
        save(eval_root/'summary.json',{'mode':'local_rubric_scoring','official_harbor_run':False,'state':'in_progress','results':results,'FINAL':{'status':'not_run'}})
        print(json.dumps({'unit':unit,'status':result.get('status'),'local_score':result.get('local_score')},ensure_ascii=False),flush=True)
    output=eval_root/'FINAL'
    final=json.loads((output/'evaluation_result.json').read_text()) if (output/'evaluation_result.json').exists() else score_stage(case_root,run_dir,'FINAL',config,output)
    summary={'mode':'local_rubric_scoring','official_harbor_run':False,'state':'finished','results':results,'FINAL':final}
    # Retain all failed/ungradable units. A mean is meaningful only for a
    # complete numeric process evaluation; FINAL is always kept independent.
    numeric=[r.get('local_score') for r in results]
    summary['process_mean']=sum(numeric)/13 if all(isinstance(x,(int,float)) for x in numeric) else None
    summary['complete_numeric_process_scores']=all(isinstance(x,(int,float)) for x in numeric)
    save(eval_root/'summary.json',summary)
    save(eval_root/'progress.json',{'state':'finished','units_evaluated':14})
    return summary


def main()->None:
    p=argparse.ArgumentParser()
    p.add_argument('--case-root',type=Path,required=True);p.add_argument('--run-dir',type=Path,required=True)
    p.add_argument('--config',type=Path,required=True);p.add_argument('--eval-dir',type=Path,required=True);p.add_argument('--wait',action='store_true')
    a=p.parse_args()
    print(json.dumps(evaluate_sequence(a.case_root.resolve(),a.run_dir.resolve(),json.loads(a.config.read_text()),a.eval_dir.resolve(),wait=a.wait),ensure_ascii=False,indent=2))


if __name__=='__main__':main()

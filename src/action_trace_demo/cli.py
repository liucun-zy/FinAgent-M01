"""Command-line entry points for a local research run, not official scoring."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main()->None:
    parser=argparse.ArgumentParser(description='Action-trace DeliverBench local research demo')
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ['run','ask','probe','evaluate']:
        p=sub.add_parser(name)
        p.add_argument('--config',type=Path,required=True)
        p.add_argument('--run-dir',type=Path,required=True)
        if name in {'run','ask'}:
            p.add_argument('--legacy',action='store_true',help='Use the archived baseline agent')
        if name=='run':
            p.add_argument('--case-root',type=Path,required=True)
            p.add_argument('--review-policy',choices=('ledger_graph','ledger_only'),default='ledger_graph')
            p.add_argument('--through-stage',type=int,default=1,choices=range(1,14))
        if name=='ask':
            p.add_argument('--valid-at');p.add_argument('--as-of',type=int)
            p.add_argument('--question',required=True);p.add_argument('--stage',type=int)
            p.add_argument('--mode',choices=['flat_trace','fixed_depth','graph_router','always_source'],default='graph_router')
        if name=='evaluate':
            p.add_argument('--case-root',type=Path)
            p.add_argument('--stage',required=True,help='T01..T13 or FINAL')
            p.add_argument('--eval-dir',type=Path,required=True)
    p=sub.add_parser('report');p.add_argument('--run-dir',type=Path,required=True)
    from .ingestion import add_commands, run_command
    add_commands(sub)
    args=parser.parse_args()
    if args.command in {'ingest','resume-ingestion','build-ledger','ledger-evidence','ingestion-result',
                        'generate-candidates','candidate-context','maintain-ledger','maintenance-result','current-ledger'}:
        print(json.dumps(run_command(args),ensure_ascii=False,indent=2))
        return
    if args.command=='report':
        from .report import render_report
        if (args.run_dir/'operator').is_dir():
            from .dsh_project import export_graph
            print(export_graph(args.run_dir/'operator')['report'])
        else:
            print(render_report(args.run_dir))
        return
    config=json.loads(args.config.read_text())
    if args.command=='run':
        if args.legacy:
            from .runner import run_case
            result=run_case(args.case_root,args.run_dir,config,through_stage=args.through_stage)
        else:
            from .dsh_project import project_case
            result=project_case(args.case_root.resolve(),args.run_dir.resolve(),args.config.resolve(),args.through_stage,
                                config.get('candidate_max_turns_per_stage',35),review_policy=args.review_policy)
    elif args.command=='evaluate':
        from .evaluation import score_stage
        case_root=args.case_root
        if case_root is None:
            case_root=Path(json.loads((args.run_dir/'run_manifest.json').read_text())['case_root'])
        result=score_stage(case_root,args.run_dir,args.stage,config,args.eval_dir)
    elif args.command=='ask':
        if args.legacy:
            from .runner import ask_run
            result=ask_run(args.run_dir,config,args.question,stage=args.stage,mode=args.mode)
        else:
            from .dsh_project import ask
            from .temporal_dag import TemporalDAG
            root=args.run_dir.resolve()/'operator'
            if not root.is_dir():
                parser.error('This run has no DSH operator workspace. For a baseline run specify --legacy.')
            cutoff=args.as_of
            if args.stage is not None:
                if cutoff is not None:
                    parser.error('Specify stage or event cutoff')
                ends=[n['seq'] for n in TemporalDAG(root).nodes('stage_submission') if n['payload']['stage']==args.stage]
                if not ends:
                    parser.error('Stage has no submitted DSH checkpoint')
                cutoff=max(ends)
            result=ask(root,args.question,args.config.resolve(),cutoff,args.valid_at,config.get('query_max_turns',20))
    else:
        from .model import ModelClient
        c=ModelClient(config,args.run_dir)
        r=c.complete([{'role':'user','content':'Reply with exactly API_OK.'}],task='probe',max_tokens=64)
        result={'model':c.model,'content':r['choices'][0]['message'].get('content'),'metrics':c.metrics()}
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()

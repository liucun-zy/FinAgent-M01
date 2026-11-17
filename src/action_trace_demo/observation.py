"""Bound graph observations, retaining IDs for explicit follow-up reads."""
from __future__ import annotations

from copy import deepcopy
import json


def compact_tool_result(name:str,result:dict,*,limit_chars:int=32000)->dict:
    raw=json.dumps(result,ensure_ascii=False)
    if len(raw)<=limit_chars:return result
    if name not in {'recall_claims','expand_actions','record_claims'}:
        return result
    out=deepcopy(result);omitted={}

    def entry(row:dict)->dict:
        r={}
        for key,value in row.items():
            if key in {'raw_edit','structural_checks','quote_matches'}:
                r[key+'_omitted']=True;continue
            encoded=json.dumps(value,ensure_ascii=False)
            if key=='value' and len(encoded)>1200:
                r['value_preview']=encoded[:1200];r['value_truncated']=True
            elif isinstance(value,str) and len(value)>600:
                r[key]=value[:600];r[key+'_truncated']=True
            elif isinstance(value,dict) and len(encoded)>1500:
                r[key+'_preview']=encoded[:1500];r[key+'_truncated']=True
            else:r[key]=value
        return r

    for key in ['claims','actions','comparisons','artifact_actions','related_artifact_actions']:
        rows=out.get(key)
        if not isinstance(rows,list):continue
        cap=6 if key=='claims' else 8
        if len(rows)>cap:omitted[key]=len(rows)-cap
        identifiers=[r.get('action_id',r.get('claim_id',r.get('comparison_id',r.get('id')))) for r in rows if isinstance(r,dict)]
        if identifiers:out[key+'_available_ids']=identifiers
        out[key]=[entry(r) if isinstance(r,dict) else r for r in rows[:cap]]
    out['observation_compacted']=True
    out['original_character_count']=len(raw)
    out['omitted_list_items']=omitted
    out['continuation_hint']='Descriptions/quotes/values may be previews. Expand specific action IDs or read cited original source excerpts before relying on omitted details.'
    if len(json.dumps(out,ensure_ascii=False))>limit_chars:
        # Return only routing identifiers, not a silently truncated JSON blob.
        out={k:v for k,v in out.items() if k.endswith('_available_ids') or k in {'stage','observation_compacted','original_character_count','continuation_hint'}}
        out['detail_status']='graph observation too large; request a smaller ID subset'
    return out

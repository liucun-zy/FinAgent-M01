"""Candidate-side field mapping for a selected refinancing scenario; no rubric inputs.

Selection and terms are joined from separate released sources. This adapter
understands the register's column semantics and native unit labels. Amounts,
case numbers, timestamps and cell coordinates are resolved from actual inputs.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
import re

from .guarded_artifacts import ArtifactOperator, _slots
from .temporal_dag import TemporalDAG
from .native_finance import inspect_workbook, scenario

CASE = re.compile(r"\bCase\s+(\d+)\b",re.I)


def _date_value(text):
    text=text.strip().rstrip(".")
    for pattern in ("%Y-%m-%d","%d %B %Y","%d %b %Y"):
        try:
            return datetime.strptime(text,pattern).date().isoformat()
        except ValueError:
            pass
    return None


def directive_date(source):
    # The signature date dates the drafting instruction. Expiry and other
    # future dates in its prose do not date the selection.
    lines=source["payload"]["text"].splitlines()
    for i,line in enumerate(lines[:-1]):
        if line.startswith("[table") and "]" in line and lines[i+1].startswith("[table"):
            cols=[x.strip().lower() for x in line.split("]",1)[1].split("|")]
            if "date" in cols:
                vals=lines[i+1].split("]",1)[-1].split("|")
                pos=cols.index("date")
                if pos<len(vals) and (day:=_date_value(vals[pos])):
                    return day
        m=re.match(r"^(?:date|issued on)\s*[:=]\s*(.+)$",line,re.I)
        if m and (day:=_date_value(m[1])):
            return day
    return _date_value(str(source["payload"].get("published_at") or "").split("T")[0])


def _register(source):
    p=source["payload"]
    lines=p["text"].splitlines()
    label_lines=[l for l in lines if "structure labels" in l.lower()]
    if len(label_lines)!=1:
        return
    labels=label_lines[0]
    def cells(line):
        return {m[1]:m[2].strip() for m in re.finditer(r"(?:^|\|)\s*([A-Z]+)\d+=([^|]+)",line)}
    header_cols=[k for l in lines for k,v in cells(l).items()
                 if re.fullmatch(r"(?:requested )?(?:revised|updated) (?:position|terms?)",v,re.I)]
    if len(set(header_cols))==1:
        column=header_cols[0]
    else:
        candidates=[k for k,v in cells(labels).items() if re.match(r"(?:Case|Option|Scenario)\s+\d+\s*/",v,re.I)]
        if len(candidates)!=1:
            return
        column=candidates[0]
    names=re.findall(r"\d+", cells(labels).get(column,""))
    if len(names)<2 or len(set(names))!=len(names):
        return
    output={}
    for field,label in (("facility_usd000",r"total facility \(\$000s\)"),("spread_bps",r"credit spread")):
        row=next((l for l in lines if re.search(label,l,re.I) and "approved for next draft" in l.lower()),None)
        if not row:
            return
        raw=cells(row).get(column)
        if not raw:
            return
        values=re.findall(r"\d[\d,]*(?:\.\d+)?",raw)
        if len(values)!=len(names):
            return
        decimals=[Decimal(x.replace(",","")) for x in values]
        output[field]=(row,dict(zip(names,[int(x) if x == int(x) else float(x) for x in decimals])))
    for field,label in (("amortization_pct",r"annual amorti[sz]ation"),("sweep_pct",r"excess cash flow sweep")):
        row=next((l for l in lines if re.search(label,l,re.I) and "approved for next draft" in l.lower()),None)
        raw=cells(row).get(column) if row else None
        values=re.findall(r"(\d+(?:\.\d+)?)\s*%",raw) if raw else []
        if len(values)==len(names):
            output[field]=(row,dict(zip(names,[float(v) for v in values])))
    return labels,output


def _selected(sources):
    choices=[]
    for s in sources:
        p=s["payload"]
        if not str(p["locator"]).startswith(("source/","instructions/")):
            continue
        for line in p["text"].splitlines():
            m=re.search(r"\b(?:this instruction selects|selected financing (?:basis|scenario) is)\s+(?:the\s+)?(?:all[- ]debt\s+)?((?:Case|Scenario|Option)\s+\d+)\b",line,re.I)
            if m:
                choices.append((p["stage"],scenario(m[1]),line,s))
    if not choices:
        return
    latest=[x for x in choices if x[0]==max(c[0] for c in choices)]
    return latest[-1] if len({r[1] for r in latest})==1 else None


def bind_current_model(root,ctx):
    dag=TemporalDAG(root)
    op=ArtifactOperator(root,dag)
    sources=dag.nodes("source")
    selection=_selected(sources)
    if not any(Path(path).suffix.lower()==".xlsx" for path in ctx["expected"]) and not selection:
        return {}
    registers=[(s,_register(s)) for s in sources if str(s["payload"]["locator"]).startswith("source/") and
               ".xlsx" in str(s["payload"]["locator"]).lower()]
    registers=[r for r in registers if r[1]]
    if not registers:
        return {}
    stage, name, directive, selected_source=selection or (ctx["stage"], None, None, None)
    current=[r for r in registers if r[0]["payload"]["stage"]==max(x[0]["payload"]["stage"] for x in registers)]
    if len({tuple(sorted((field,tuple(values.items())) for field,(_,values) in reg[1].items())) for _,reg in current})!=1:
        dag.append("projection_gap",{"stage":ctx["stage"],"reason":"competing current financing registers"},
                   [ctx["request_id"],*(s["id"] for s,_ in current)])
        return {}
    term_source,(labels,table)=current[-1]
    if name and CASE.search(name)[1] not in table["spread_bps"][1]:
        # A selection and the released terms disagree. Preserve the register
        # without silently substituting a different current financing basis.
        dag.append("projection_gap",{"stage":ctx["stage"],"reason":"selected scenario absent from released register",
                   "selected":name},[ctx["request_id"],selected_source["id"],term_source["id"]])
        selection=None
        name=None
    facts={}
    group=dag.record_claim(term_source["id"],"Refinancing scenarios","Ordered scenarios in revised register",
             "The revised column lists scenario order for both pricing and principal.",labels,field="case_order",
             value=list(table["spread_bps"][1]),scope={"basis":"working register"})
    for field,(row,values) in table.items():
        for number_i,v in values.items():
            entity=f"Case {number_i}"
            facts[entity,field]=dag.record_claim(term_source["id"],entity,f"{entity} {field} = {v}",
                "Join the approved revised value column to the scenario order in the same register.",
                row,field=field,value=v,scope={"scenario":entity},supports=[[group["id"],term_source["id"]]])
    selected_from=directive_date(selected_source) if selected_source else None
    chosen=None
    if selection:
        chosen=dag.record_claim(selected_source["id"],name,f"Current drafting instructions select {name}",
                "The current instruction selects a drafting basis; it does not establish funding.",directive,
                field="selected_scenario",value=name,valid_from=selected_from,scope={"purpose":"current signing preparation"})
    supports=[group["id"]]
    if chosen:
        amount=facts[name,"facility_usd000"]
        spread=facts[name,"spread_bps"]
        supports=[chosen["id"],amount["id"],spread["id"]]
    versions=dag.nodes("artifact_version")
    outputs={}
    projection_receipts={}
    for target in ctx["expected"]:
        ext=Path(target).suffix.lower()
        if ext not in {".xlsx",".docx"}:
            continue
        old=next((v for v in reversed(versions) if Path(v["payload"]["path"]).name==Path(target).name
                  and v["payload"]["path"]!=target and v["payload"]["stage"]<ctx["stage"]),None)
        contract=op._contract(target)
        if not contract:
            raise ValueError("Cannot project terms without a released artifact contract")
        scope=contract["payload"]["description"]
        reuse_key=(contract["payload"]["purpose"],scope,old["id"] if old else None)
        if reuse_key in projection_receipts:
            existing=projection_receipts[reuse_key]
            rules=existing["payload"]["rules"]
            bound=op.bind(target,purpose=existing["payload"]["purpose"],description=existing["payload"]["description"],
                          rules=rules,semantic_rules=existing["payload"].get("semantic_rules",[]),
                          trusted_derivations=[r["claim_id"] for r in rules if r.get("projection_receipt")],
                          parents=existing["parents"],stage=ctx["stage"],entity=existing["payload"].get("entity"))
            outputs[target]=bound["id"]
            continue
        native=_slots(op._blob(old["payload"]["path"],old["payload"]["sha256"])) if old else {}
        rules=[]
        derived_ids=[]
        purpose="current financing basis for signing preparation"
        if ext==".xlsx":
            view=inspect_workbook(native)
            vertical_rows=any(isinstance(v,str) and CASE.search(v) and
                              re.search(r"spread|commitment",v,re.I)
                              for k,v in native.items() if re.search(r"!A[0-9]+$",k))
            # Historical single-scenario input schemas retain their explicit
            # current-use projection below. Multi-scenario tables are joined
            # by entity and metric, independently of sheet or column position.
            if not (old and selection and vertical_rows and len(view["scenario_slots"]) == 1):
                model_request=bool(re.search(r"financing (?:structures?|models?)|debt service|scenario models?|signing preparation",scope,re.I))
                model_destination=bool(re.search(r"financ|scenario|model",Path(target).stem,re.I))
                if not view["scenario_slots"] and not (model_request and model_destination):
                    continue
                sem=[{"entity":entity,"field":field,"claim_id":cp["id"],"expected":cp["payload"]["value"],
                      "when_scenario_present":True}
                     for (entity,field),cp in facts.items()]
                parents=[ctx["request_id"],*supports]
                if old:
                    provenance=dag.append("native_projection",{
                        "path":target, "source_sha256":old["payload"]["sha256"],"purpose":contract["payload"]["purpose"],
                        "method":"resolve released terms by native scenario header, field, unit and downstream consumers",
                        "source_slots":view["scenario_slots"],"schedules":view["schedules"],
                        "stage":ctx["stage"],"input_ids":[r["claim_id"] for r in sem]},
                        [old["id"],*supports,*(r["claim_id"] for r in sem)])
                    parents.append(provenance["id"])
                bound=op.bind(target,purpose=contract["payload"]["purpose"],semantic_rules=sem,
                              description=scope,parents=parents,stage=ctx["stage"],entity=name)
                outputs[target]=bound["id"]
                projection_receipts[reuse_key]=bound
                continue
        if not selection or not old:
            continue

        def derived(loc,value,old_text,extra=()):
            old_source=dag.release_source(ctx["stage"],f'{old["payload"]["path"]}@{old["payload"]["sha256"]}#{loc}',
                      str(old_text),source_key=f'native:{old["payload"]["sha256"]}:{loc}')
            evidence=[old_source["id"], *supports, *extra]
            receipt=dag.append("native_projection", {"locator":loc, "output":value, "purpose":purpose,
                "source_sha256":old["payload"]["sha256"], "method":"project selected scenario into current-use native slot",
                "stage":ctx["stage"],"input_ids":evidence}, [old["id"],*evidence])
            cp=dag.record_claim(selected_source["id"],name,f"Current presentation at {loc}", 
                "Project selected terms into this native slot; retain dated facts outside its current-use scope.",
                directive,field="presentation",value=value,valid_from=selected_from,scope={"destination":target,"locator":loc},
                parents=[receipt["id"]],supports=[evidence])
            derived_ids.append(cp["id"])
            rules.append({"locator":loc,"claim_id":cp["id"]})
        if ext==".xlsx":
            # Identify the semantic rows and verify destination units before conversion.
            row_keys={}
            for key,v in native.items():
                if not isinstance(v,str) or "!" not in key:
                    continue
                sheet,addr=key.rsplit("!",1)
                m=re.fullmatch(r"A(\d+)",addr)
                case=CASE.search(v)
                if not m or not case:
                    continue
                b,c=f"{sheet}!B{m[1]}",f"{sheet}!C{m[1]}"
                units=str(native.get(c,"")).lower()
                field="spread_bps" if "spread" in v.lower() and units=="bps" else (
                    "facility_usd000" if "commitment" in v.lower() and units in {"usd000","$000s","kusd"} else None)
                if field:
                    row_keys[field]=(key,b,case[0])
            if set(row_keys)!={"spread_bps","facility_usd000"}:
                continue
            old_case=row_keys["spread_bps"][2]
            if row_keys["facility_usd000"][2].lower()!=old_case.lower():
                continue
            old_spread=native[row_keys["spread_bps"][1]]
            base=next((native.get(k.rsplit("!",1)[0]+"!B"+k.rsplit("!A",1)[1]) for k,v in native.items()
                       if "!A" in k and isinstance(v,str) and "base rate reference" in v.lower()),None)
            for field,(key,value_key,_) in row_keys.items():
                derived(key,CASE.sub(name,native[key],count=1),native[key])
                rules.append({"locator":value_key,"claim_id":facts[name,field]["id"]})
            for k,v in native.items():
                if not isinstance(v,str) or k in {i[0] for i in row_keys.values()}:
                    continue
                revised=v
                # A statement describing the active workbook scenario, not comparisons or alternatives.
                if re.search(re.escape(old_case)+r"\s+senior.only scenario",v,re.I):
                    revised=re.sub(re.escape(old_case),name,v,flags=re.I)
                    revised=revised.replace("not commitment, approval or selection","selected for drafting; not commitment or approval")
                if isinstance(base,(int,float)) and isinstance(old_spread,(int,float)) and "illustrative" in v.lower():
                    old_rate=Decimal(str(base))*100+Decimal(str(old_spread))/100
                    new_rate=Decimal(str(base))*100+Decimal(str(spread["payload"]["value"]))/100
                    revised=revised.replace(f"{old_rate:.2f}%",f"{new_rate:.2f}%")
                if revised!=v:
                    derived(k,revised,v)
        elif ext==".docx":
            # When the memo covers multiple historical scenarios, preserve those
            # paragraphs as a dated annex and give the present view an explicit scope.
            if not any(isinstance(v,str) and re.search(r"BaseSpread is Case \d+",v,re.I) for v in native.values()):
                continue
            loc="p:1"
            if loc not in native:
                continue
            txt=(f"Current signing preparation — {name}. Facility USD {amount['payload']['value']:,} thousand; "
                 f"spread {spread['payload']['value']} bps. Drafting basis, subject to conditions. "
                 f"Historical scenario memorandum follows — {native[loc]}")
            derived(loc,txt,native[loc])
        if rules:
            bound=op.bind(target,purpose=purpose,trusted_derivations=derived_ids,
                    rules=rules,description=directive,parents=[ctx["request_id"],*supports],stage=ctx["stage"],entity=name)
            outputs[target]=bound["id"]
            projection_receipts[reuse_key]=bound
    return outputs

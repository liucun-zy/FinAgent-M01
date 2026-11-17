"""Source-bound operating and debt-service calculations for a released lender mandate."""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal

from .business_calculation import _unit, calculate, reporting_dates, verified_calculation
from .live_review import source_role
from .native_finance import NativeValues, _formula
from .native_grounding import quantities
from .task_obligations import _lines, _money_unit


def _scalar(value):
    return int(value) if value == value.to_integral_value() else float(value)


def _field(text):
    return re.sub(r"\W+", " ", text).strip()


def _display(amount):
    return format(Decimal(str(amount)), "f").rstrip("0").rstrip(".") if "." in str(amount) else str(amount)


def _sources(dag, obligation, refs):
    nodes={}
    for ref in refs:
        source=dag.get(ref["source_id"])
        p=source["payload"]
        if (source["kind"]!="source" or source["seq"]>=obligation["seq"] or
                p["stage"]>obligation["payload"]["stage"] or not p["locator"].startswith("source/") or
                source_role(dict(p,locator=p["locator"]))!="business_evidence" or p["locator"]!=ref["locator"] or
                p["text"][ref["start"]:ref["end"]]!=ref["quote"] or
                hashlib.sha256(p["text"].encode()).hexdigest()!=p["sha256"]):
            raise ValueError("Capacity input is unavailable or differs from the released original")
        nodes[source["id"]]=source
    return nodes


def _same_money(refs, currency):
    units={u for ref in refs if (u:=_money_unit(ref["quote"]))}
    if len(units)!=1 or next(iter(units))[0]!=currency:
        raise ValueError("No single released currency and scale for the selected cash or earnings input")
    return next(iter(units))[1]


def _workbook_value(source,location):
    slots,refs,sheets={},{},{}
    sheet=None
    for line in _lines(source):
        text=line["quote"]
        if m:=re.fullmatch(r"\[sheet (.+)\]",text):
            sheet=m[1]
            sheets[sheet.casefold()]=sheet
        elif sheet:
            for address,value in re.findall(r"(?:^|\s\|\s)([A-Z]+[1-9]\d*)=(.*?)(?=\s\|\s|$)",text):
                loc=sheet+"!"+address
                slots[loc]=value
                refs[loc]=line
    amount=NativeValues(slots,sheets,None).get(location)
    if amount is None:
        raise ValueError("A source workbook formula has an unsupported or unavailable input")
    visited,queue=set(),[location]
    while queue:
        loc=queue.pop()
        if loc in visited:
            continue
        if loc not in refs or len(visited)>=1200:
            raise ValueError("Could not verify the complete released workbook dependency range")
        visited.add(loc)
        if str(slots[loc]).startswith("="):
            deps,_,full=_formula(slots[loc],loc.rsplit("!",1)[0],sheets)
            if not full:
                raise ValueError("Unresolved source workbook dependency")
            queue.extend(deps)
    return amount,[refs[k] for k in sorted(visited)],{k:slots[k] for k in sorted(visited)}


def _literal(ref, currency):
    money=[q for q in quantities(ref["quote"]) if q.dimension in {currency,"dollar" if currency=="USD" else currency}]
    if len(money)!=1:
        raise ValueError("No unique released cash amount in the selected source line")
    return money[0].canonical


def _base_plan(base, sources):
    """Reperform each registered type of group cash or earnings observation."""
    refs=base["sources"]
    currency=base["currency"]
    wanted=Decimal(base["amount"])
    if loc:=base.get("native_source"):
        value,inputs,slots=_workbook_value(sources[refs[0]["source_id"]],loc)
        value*=_same_money([refs[0]],currency)
        plan=[("source",value,refs[0],"source workbook at "+loc)]
        evidence=[*refs,*inputs]
        detail={"native_source":loc,"native_slots":slots}
        expression="source"
    elif base["metric"]=="cash":
        value=_literal(refs[0],currency)
        plan=[("source",value,refs[0],"released consolidated cash flow")]
        expression,evidence,detail="source",refs,{}
    else:
        header=next((r for r in refs if "|" in r["quote"] and
                     all(re.fullmatch(r"FY\s*\d{4}",s.strip(),re.I) for s in r["quote"].split("|")[1:])),None)
        if not header:
            raise ValueError("No released fiscal header for the segment EBITDA bridge")
        years=[re.sub(r"\s","",x).upper() for x in header["quote"].split("|")]
        period="FY"+base["year"]
        if years.count(period)!=1:
            raise ValueError("Period is ambiguous in the released operating table")
        col=years.index(period)
        scale=_same_money([r for r in refs if r["source_id"]==header["source_id"] and
                          re.search(r"segment adjusted EBITDA\s*\(",r["quote"],re.I)],currency)
        plan=[]
        for r in refs:
            cells=r["quote"].split("|")
            if (r["source_id"]==header["source_id"] and r["start"]>header["end"] and len(cells)==len(years)
                    and all(re.fullmatch(r"\s*[\d,.]+\s*",x) for x in cells[1:])):
                val=Decimal(cells[col].strip().replace(",",""))*scale
                plan.append((f"segment_{len(plan)+1}",val,r,"segment EBITDA: "+cells[0].strip()))
        cost=next((r for r in refs if re.search(r"unallocated\s+(?:corporate|group)\s+overhead\s+was",r["quote"],re.I)),None)
        if not cost or not plan:
            raise ValueError("The operating perimeter or unallocated corporate costs cannot be reconstructed")
        values=re.findall(r"([\d,]+(?:\.\d+)?)\s+in\s+FY\s*"+base["year"]+r"\b",cost["quote"],re.I)
        if len(values)!=1:
            raise ValueError("No unique headquarters cost for the chosen fiscal period")
        expense_scale=_same_money([r for r in refs if r["source_id"]==cost["source_id"] and r!=cost],currency)
        expense=Decimal(values[0].replace(",",""))*expense_scale
        value=sum(piece[1] for piece in plan)-expense
        expression="+".join(p[0] for p in plan)+"-overhead"
        plan.append(("overhead",expense,cost,"unallocated corporate overhead"))
        detail,evidence={},refs
    if value!=wanted:
        raise ValueError("Recomputed operating evidence differs from the registered capacity observation")
    return plan,expression,evidence,detail


def _service_plan(term):
    refs=term["sources"]
    cur=term["currency"]
    if "debt_service" in term:
        val=_literal(refs[0],cur)
        if val!=Decimal(term["debt_service"]) or val<=0:
            raise ValueError("Incorrect amount or period in the released debt-service observation")
        return [("service",val,refs[0],"reported annual debt service")],"service",val

    rows=[]
    for r in refs:
        m=re.fullmatch(r"\[table (\d+) row \d+\] (.*)",r["quote"])
        if m:
            rows.append((m[1],[c.strip() for c in m[2].split("|")],r))
    header=next((c for _,c,_ in rows if term["scenario"] in c[1:]),None)
    if not header or header.count(term["scenario"])!=1 or len({t for t,_,_ in rows})!=1:
        raise ValueError("Original facility table does not identify one selected option column")
    ix=header.index(term["scenario"])
    scale=_same_money([refs[0]],cur)
    plan=[]
    for key,pattern in (("principal",r"total (?:commitment|facility)|principal amount"),
                        ("spread",r"(?:credit )?spread(?: over base rate)?"),("amort",r"(?:scheduled|annual) amorti[sz]ation")):
        match=[(c,r) for _,c,r in rows if re.fullmatch(pattern,c[0],re.I)]
        if len(match)!=1 or len(match[0][0])!=len(header):
            raise ValueError("A required facility term is ambiguous or missing")
        cells,ref=match[0]
        raw=cells[ix]
        if key=="principal":
            val=Decimal(raw.replace(",",""))*scale
        else:
            rates=[q.canonical for q in quantities(raw) if q.dimension=="rate"]
            if len(rates)!=1:
                raise ValueError("The selected percentage is not explicit in its original column")
            val=rates[0]
        if val!=Decimal(term[key]) or (key=="principal" and val<=0):
            raise ValueError("Financing term disagrees with its original selected option")
        plan.append((key,val,ref,cells[0]))
    floor=plan[0][1]*(plan[1][1]+plan[2][1])
    if floor<=0:
        raise ValueError("The available terms do not establish a positive debt-service floor")
    return plan,"principal*(spread+amort)",floor


def _routes(dag,ctx,obligation_id):
    current=[n for n in dag.nodes("task_obligation") if n["payload"].get("kind")=="platform_debt_capacity"
             and n["payload"]["request_id"]==ctx["request_id"] and n["payload"]["stage"]==ctx["stage"]
             and set(n["payload"]["paths"])&set(ctx["expected"])]
    if obligation_id:
        current=[n for n in current if n["id"]==obligation_id]
    out=[]
    for node in current:
        detail=node["payload"]["detail"]
        for base in detail["bases"]:
            for term in detail["terms"]:
                if base["currency"]!=term["currency"] or base["year"]!=term.get("year",base["year"]):
                    continue
                direct="debt_service" in term
                if direct and base["metric"]!="cash":
                    continue
                method=("cfads_to_scheduled_service" if direct else
                    "group_cash_to_service_floor" if base["metric"]=="cash" else "group_earnings_to_service_floor")
                out.append((node,base,term,method))
    return out


def _source_claim(dag,node,datum,entity,period,unit):
    name,value,ref,description=datum
    scope={"capacity_obligation":node["id"],"metric":description,"unit":unit,"period":period}
    pyvalue=_scalar(value)
    for c in dag.claim_at():
        p=c["payload"]
        if (p["entity"]==entity and p["field"]==description and p["scope"]==scope and p["source_id"]==ref["source_id"]
                and p["start"]==ref["start"] and p["value"]==pyvalue):
            return c
    c=dag.record_claim(ref["source_id"],entity,description,description+" read in the released source column.",
        ref["quote"],start=ref["start"],field=description,value=pyvalue,scope=scope,parents=[node["id"]])
    if c["payload"]["pending"]:
        raise ValueError("A released input has an unresolved ledger conflict")
    return c


def _record(dag,ctx,node,entity,period,field,plan,expression,value,unit,refs,method,detail,purpose):
    """Commit the independently evaluated source operation with the ordinary receipt schema."""
    operands=[]
    for datum in plan:
        original_unit="fraction" if datum[0] in {"spread","amort"} else unit
        c=_source_claim(dag,node,datum,entity,period,original_unit)
        operands.append({"name":datum[0],"claim_id":c["id"],"unit":original_unit})
    ids=[o["claim_id"] for o in operands]
    seen=set()
    evidence=[]
    for ref in refs:
        key=(ref["source_id"],ref["start"],ref["end"])
        if key not in seen:
            seen.add(key)
            evidence.append(ref)
    source_ids=list(dict.fromkeys(ref["source_id"] for ref in evidence))
    source_heads={i:dag.get(i)["payload"] for i in source_ids}
    first=dag.get(ids[0])["payload"]
    rec=dag.append("calculation_receipt",{"stage":ctx["stage"],"request_id":ctx["request_id"],"purpose":purpose,
        "entity":entity,"field":field,"expression":expression,"operands":operands,"inputs":ids,
        "result":_scalar(value),"result_unit":unit,"exact_decimal":str(value),"valid_from":None,"valid_to":None,
        "fiscal_period":period,"source_stage_by_input":{i:p["stage"] for i,p in source_heads.items()},
        "original_source_sha256":{i:p["sha256"] for i,p in source_heads.items()},"latest_input_seq":max(int(i[1:]) for i in ids),
        "source_evidence":evidence,"method":method,**detail},[ctx["request_id"],node["id"],*ids,*source_ids])
    c=dag.record_claim(first["source_id"],entity,field+" = "+str(value)+" "+unit,
        "Source-bound calculation "+rec["id"],first["quote"],start=first["start"],field=field,value=_scalar(value),
        scope={"purpose":purpose,"unit":unit,"fiscal_period":period},supports=[ids],parents=[rec["id"]])
    if c["payload"]["pending"] or not verified_calculation(dag,c):
        raise ValueError("The recomputed capacity projection conflicts with the current ledger")
    return {"claim_id":c["id"],"receipt_id":rec["id"],"value":_scalar(value),"unit":unit}


def capacity_calculation(dag,ctx,*,action,obligation_id=None,fiscal_period=None,scenario=None,method=None):
    routes=_routes(dag,ctx,obligation_id)
    if fiscal_period:
        period,_,_=reporting_dates(fiscal_period)
        routes=[r for r in routes if "FY"+r[1]["year"]==period]
    if scenario:
        routes=[r for r in routes if r[2]["scenario"].casefold()==scenario.strip().casefold()]
    if method:
        routes=[r for r in routes if r[3]==method]
    if action=="list":
        return {"status":"options" if routes else "insufficient_evidence","options":[
            {"obligation_id":n["id"],"fiscal_period":"FY"+b["year"],"scenario":t["scenario"],"method":m,
             "currency":b["currency"],"operating_source_ids":list(dict.fromkeys(r["source_id"] for r in b["sources"])),
             "financing_source_ids":list(dict.fromkeys(r["source_id"] for r in t["sources"]))} for n,b,t,m in routes],
             "next":"Select one supported fiscal_period, scenario and method with action=derive. Bind each returned measure to a distinct native table cell."}
    if action!="derive" or not (fiscal_period and scenario) or len(routes)!=1:
        raise ValueError("List available capacity routes, then select one fiscal_period, scenario and unique method for this request")
    n,base,term,method=routes[0]
    sources=_sources(dag,n,[*base["sources"],*term["sources"]])
    basis_plan,base_expression,base_evidence,native=_base_plan(base,sources)
    debt_plan,debt_expression,service=_service_plan(term)
    period="FY"+base["year"]
    scenario=term["scenario"]
    unit=base["currency"]
    entity="Platform "+scenario
    purpose=f"{n['payload']['purpose']} | {period} | {scenario} | {method}"
    key=hashlib.sha256(json.dumps([n["id"],period,scenario,method,base,term],sort_keys=True).encode()).hexdigest()
    for prior in reversed(dag.nodes("capacity_result")):
        if prior["payload"].get("key")==key:
            active={c["id"] for c in dag.claim_at()}
            cached=prior["payload"]["result"]
            if all(row["claim_id"] in active and verified_calculation(dag,dag.get(row["claim_id"])) for row in cached["outputs"]):
                return {**cached,"capacity_result_id":prior["id"]}
    direct="debt_service" in term
    basis_label="Group CFADS" if base["metric"]=="cash" else "Group adjusted EBITDA"
    payment_label="Annual debt service" if direct else "Annual debt-service floor"
    result_label="Cash after debt service" if direct else "Residual upper bound"
    value=Decimal(base["amount"])
    basis=_record(dag,ctx,n,entity,period,basis_label,basis_plan,base_expression,value,unit,base_evidence,
                  "dated operating evidence",native,purpose)
    payment=_record(dag,ctx,n,entity,period,payment_label,debt_plan,debt_expression,service,unit,term["sources"],
                    "disclosed debt service" if direct else "annual full-draw spread and amortization",{},purpose)
    remainder=calculate(dag,ctx,entity=entity,field=result_label,purpose=purpose,expression="operating-service",
         operands=[dict(name="operating",claim_id=basis["claim_id"],unit=unit),dict(name="service",claim_id=payment["claim_id"],unit=unit)],
         fiscal_period=period,result_unit=unit)
    if remainder["pending"]:
        raise ValueError("The remaining capacity is pending ledger review")
    outputs=[dict(item,label=label) for item,label in zip((basis,payment,remainder),(basis_label,payment_label,result_label))]
    conditions=("Same-period consolidated cash and scheduled debt service." if direct else
        "Full-draw annual illustration excluding the base rate. Fees and cash sweep excluded. "+
        ("Actual cash available also depends on cash tax, capex and working capital. EBITDA is an earnings proxy." if base["metric"]=="earnings" else
         "Cash remaining after the partial debt-service floor is an upper bound."))
    columns=["Financing basis","Fiscal period",basis_label,payment_label,result_label,"Conditions"]
    row=["Platform "+scenario,period,*[unit+" "+_display(i["value"]) for i in outputs],conditions]
    result={"status":"computed","obligation_id":n["id"],"fiscal_period":period,"scenario":scenario,"method":method,
        "purpose":purpose,"outputs":outputs,"table":{"headers":columns,"row":row,"binding_columns":[
            {"column":i,"claim_id":item["claim_id"],"match":"contains"} for i,item in enumerate(outputs,3)]},
        "limitations":conditions,"next":"Append the table or use existing native cells. Bind the three displayed amounts using their claim IDs and actual cell locators, then review the proposed bytes."}
    bundle=dag.append("capacity_result",{"key":key,"result":result,"stage":ctx["stage"],
        "request_id":ctx["request_id"],"obligation_id":n["id"]},[ctx["request_id"],n["id"],*[i['claim_id'] for i in outputs]])
    return {**result,"capacity_result_id":bundle["id"]}

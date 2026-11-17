"""Evidence and version identities used by native artifact checks."""
from __future__ import annotations

import hashlib
import json
import re

from .event_witness import PREDICATES, witnesses_action
from .native_grounding import amounts_match, claim_quantities, dates, document_quantities, metrics, semantic_labels
from dataclasses import replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def is_business_claim(node):
    from .live_review import source_role
    return source_role(node["payload"]) == "business_evidence"


def proof_scope(contract, obligations, policy):
    p = contract["payload"]
    identity = {k: p.get(k) for k in ("stage", "purpose", "valid_at", "rules", "semantic_rules",
                                      "description", "allow_delete")}
    identity.update(policy=policy, obligations=sorted(c["obligation_id"] for c in obligations),
                    checker="native_business_evidence_v2")
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def measured_assertion(text):
    """Financial results require evidence. Identifiers, headings and explicit analyst estimates do not establish facts."""
    if not isinstance(text, str) or text.startswith("="):
        return False
    if re.search(r"\b(?:analyst estimates?|illustrative assumption|assume for illustration)\b", text, re.I):
        return False
    factual_state = re.search(r"\b(?:is|was|has been|were|have been)\s+(?:formally\s+)?"
                              r"(?:approved|waived|confirmed|selected|closed|signed|executed|withdrawn|funded)\b",
                              text, re.I)
    return bool(factual_state or (metrics(text) and re.search(r"(?:\d[\d,.]*\s*(?:%|bps?\b|[x×]\b|million|billion|thousand)"
                                             r"|(?:[$€£]|\b(?:USD|EUR|GBP)\s*)\s*\d)", text, re.I)))


_AMOUNT_HEADER = re.compile(r"(?<![A-Za-z])(?:US\$|USD|EUR|GBP|CNY|[$€£])\s*(?:in\s+)?(?:000s?|k|m|mm|mn|bn|millions?|thousands?|billions?)\b",re.I)


def _fraction_consumers(slots, locator):
    # Only an actual reference to this input added to a basis-point rate
    # converted to a fraction witnesses its representation.
    from .native_finance import _references
    names={k.rsplit("!",1)[0].casefold():k.rsplit("!",1)[0] for k in slots if "!" in k}
    ref=r"(?:(?:'[^']+'|[A-Za-z_][\w.]*)!)?\$?[A-Z]{1,3}\$?[1-9]\d*"
    pattern=re.compile(r"^=\s*("+ref+r")\s*\+\s*("+ref+r")\s*/\s*10000(?:\.0*)?\s*$",re.I)
    confirmed=[]
    for k,v in slots.items():
        if "!" not in k or not isinstance(v,str) or not (m:=pattern.fullmatch(v)):
            continue
        consumer_sheet=k.rsplit("!",1)[0]
        try:
            base=_references(m[1],consumer_sheet,names)
            spread=_references(m[2],consumer_sheet,names)
        except ValueError:
            continue
        if base != [locator] or not spread or len(spread)!=1:
            continue
        ss,addr=spread[0].rsplit("!",1)
        row=re.search(r"\d+$",addr)[0]
        if re.search(r"\bbps?\b|basis points?",str(slots.get(ss+"!A"+row,"")),re.I):
            confirmed.append(k)
    return confirmed


def contextual_quantities(value, document_unit, context):
    qs=document_quantities(value,document_unit)
    if not re.fullmatch(r"[-+]?\d[\d,]*(?:\.\d+)?",str(value).strip()) or len(qs)!=1:
        return qs
    q=qs[0]
    if "[native rate fraction:" in context:
        return [replace(q,dimension="rate",scale=Decimal(1))]
    headers={s.strip().casefold().replace(" ","") for s in _AMOUNT_HEADER.findall(context)}
    if len(headers)==1:
        raw=next(iter(headers))
        currency=re.match(r"US\$|USD|EUR|GBP|CNY|[$€£]",raw,re.I)[0].upper()
        dimension={"US$":"USD","$":"dollar","€":"EUR","£":"GBP"}.get(currency,currency)
        scale=raw.lower()[len(currency):]
        factor=10**6 if scale.startswith("m") else 10**9 if scale.startswith("b") else 1000
        return [replace(q,dimension=dimension,scale=Decimal(factor))]
    if not headers and re.search(r"\bbps?\b|basis points?",context,re.I):
        return [replace(q,dimension="rate",scale=Decimal(".0001"))]
    return qs


def _amount_metrics(text):
    roles = metrics(text)
    if re.search(r"\bincrement(?:al)?\b.*?\b(?:vs|versus|compared to)\b", text, re.I):
        return {"commitment_increment"}
    if re.search(r"\bcommitments?\b", text, re.I):
        roles.add("facility")
    if re.search(r"\bbase[ _-]rates?\b", text, re.I):
        roles.add("base_rate")
    return roles


def direct_context_match(actual,wanted,claim,rule,document_unit,context):
    # A return value of None means the cell has no grounded contextual
    # conversion. Other text and ordinary scalar checks keep their own policies.
    if not re.fullmatch(r"[-+]?\d[\d,]*(?:\.\d+)?",str(actual).strip()):
        return None
    try:
        wanted = Decimal(str(wanted).replace(",", ""))
    except InvalidOperation:
        return None
    if "[native rate fraction:" in context:
        if "base_rate" in _amount_metrics(context):
            if "base_rate" not in _amount_metrics(claim["quote"]):
                return set()
            period_start = re.search(r"\b(\d{4}-\d{2}-\d{2})\s+(?:to|through)\s+\d{4}-\d{2}-\d{2}", context, re.I)
            if period_start and not dates(period_start[1]).intersection(dates(claim["quote"])):
                return set()
        return {q.value for q in contextual_quantities(actual,document_unit,context)
                for s in claim_quantities(claim) if s.dimension=="rate" and
                s.value==wanted and q.canonical==s.canonical}
    if not _AMOUNT_HEADER.search(context):
        return None
    if rule and rule.get("original_unit"):
        return calculation_matches(actual,wanted,rule["original_unit"],document_unit,context,allow_rounding=False)
    # If source units are explicit on the original number they need no ledger
    # scope or other text. A bare table amount must first prove its native unit.
    if "|" in claim["quote"] or (rule and rule.get("unit_validation_failed")):
        return set()
    explicit=document_quantities(claim["quote"],None)
    return {q.value for q in contextual_quantities(actual,document_unit,context)
            for s in explicit if s.value==wanted and s.dimension is not None and amounts_match(q,s)}


def binding_context(slots, locator):
    if "!" in locator:
        sheet,address=locator.rsplit("!",1)
        cell=re.fullmatch(r"([A-Z]+)(\d+)",address)
        if cell:
            label=str(slots.get(f"{sheet}!A{cell[2]}",""))
            header=str(slots.get(f"{sheet}!{cell[1]}1",""))
            unit=str(slots.get(f"{sheet}!C{cell[2]}","")).strip().casefold()
            proof=""
            if cell[1]=="B" and unit in {"%","percent","percentage","per cent"} and re.search(r"rate|spread|interest",label,re.I):
                used=_fraction_consumers(slots,locator)
                if used:
                    proof=" [native rate fraction: "+", ".join(used)+"]"
            # A row-specific non-monetary unit supersedes a sheet title such as
            # "USD thousands except percentages and multiples."
            local = label + " " + header
            sheet_unit = str(slots.get(sheet + "!A1", ""))
            if proof or re.search(r"\bbps?\b|basis points?|[%]|\bmultiple\b|\(x\)",local,re.I):
                sheet_unit = ""
            return f"{sheet} {sheet_unit} {label} {header}" + proof
    if ":cell:" in locator or re.fullmatch(r"t:\d+:\d+:\d+",locator):
        table,row,col=locator.rsplit(":",2)
        label=str(slots.get(f"{table}:{row}:1","")).strip()
        column=str(slots.get(f"{table}:1:{col}","")).strip()
        corner=str(slots.get(f"{table}:1:1",""))
        if re.fullmatch(r"[A-Z]|\d+",label):
            typ=re.search(r"\b(case|option|scenario)\b",corner,re.I)
            if typ:
                label=typ[1]+" "+label
        return f"{label} | {column}"
    return str(slots.get(locator, ""))


def mismatched_binding(slots, locator, claim):
    """A disjoint, identifiable metric gives positive evidence of a binding error."""
    context = binding_context(slots, locator)
    scenarios = _scenario_labels(context)
    intended = _scenario_labels(str(claim["entity"]) + " " + str(claim.get("field", "")))
    if len(scenarios) == 1 and len(intended) == 1 and intended.isdisjoint(scenarios):
        return True
    observed = _amount_metrics(context)
    route = " ".join(claim.get("value_path",())).replace("_"," ")
    route_metrics = metrics(route)
    wanted = route_metrics or _amount_metrics(claim.get("field") or "") or _amount_metrics(claim["claim"])
    route_cases = _scenario_labels(route)
    if scenarios and route_cases and scenarios.isdisjoint(route_cases):
        return True
    return bool(observed and wanted and observed.isdisjoint(wanted))


def supporting_quote(dag, review, quote, claim_ids):
    """A checked quote must witness a used business claim at its actual source offset."""
    for cid in claim_ids:
        node = dag.get(cid)
        c = node["payload"]
        if not is_business_claim(node) or not admissible_text(c) or c["source_id"] != review["source_id"]:
            continue
        lo = max(review["start"], c["start"])
        hi = min(review["end"], c["end"])
        if lo >= hi:
            continue
        for match in re.finditer(re.escape(quote), review["text"]):
            start = review["start"] + match.start()
            end = start + len(quote)
            overlap = review["text"][max(start, lo)-review["start"]:min(end, hi)-review["start"]] if start < hi and end > lo else ""
            if not overlap.strip():
                continue
            value = c.get("value")
            if not isinstance(value, (dict, list)) and value is not None and re.search(
                    r"(?<!\w)" + re.escape(str(value)) + r"(?!\w)", overlap, re.I):
                return cid
            if c["quote"] in quote or len(semantic_labels(overlap) & semantic_labels(c["quote"])) >= 3:
                return cid
    return None


def calculation_matches(actual, wanted, unit, document_unit, context, *, allow_rounding=True):
    """Match a computed result only at a native amount with compatible denomination and display precision."""
    from .business_calculation import _unit
    dimension, factor = _unit(unit)
    u = unit.casefold()
    dimension = dimension or ("rate" if u in {"%","pct","percent","bps","bp","basispoints","fraction","ratio"}
                               else "multiple" if u in {"x","multiple"} else "count")
    expected = Decimal(str(wanted)) * factor
    matches = set()
    for q in contextual_quantities(actual,document_unit,context):
        qdim, scale = q.dimension, q.scale
        if qdim is None:
            if re.search(r"\bbps?\b|basis points?", context, re.I):
                qdim,scale = "rate",Decimal("0.0001")
            elif re.search(r"[%]|\bpercent\b",context,re.I):
                qdim,scale = "rate",Decimal("0.01")
            elif re.search(r"\bmultiple\b|\(x\)",context,re.I):
                qdim,scale = "multiple",Decimal(1)
            elif dimension == "count":
                qdim = "count"
            else:
                native_unit = re.search(r"\b(USD|EUR|GBP|CNY)\s*(000s?\b|k\b|thousands?\b|millions?\b|m\b|bn\b|billions?\b)?",
                                        context,re.I)
                if native_unit:
                    qdim = native_unit[1].upper()
                    s = (native_unit[2] or "").lower()
                    scale = Decimal(1000 if s.startswith(("0","k","t")) else
                                    10**6 if s.startswith("m") else 10**9 if s.startswith("b") else 1)
        if qdim == "dollar":
            qdim = "USD"
        if qdim != dimension:
            continue
        display = expected / scale
        # Only a nonzero displayed quantity with decimal places admits
        # rounding. A zero or bare integer must agree exactly.
        exact = q.value == display
        rounded = (q.value and q.value.as_tuple().exponent < 0 and
                   display.quantize(Decimal(1).scaleb(q.value.as_tuple().exponent),rounding=ROUND_HALF_UP)==q.value)
        if exact or (allow_rounding and rounded):
            matches.add(q.value)
    return matches

# Business events that change the meaning of a financing communication.
# A document date, agenda deadline or blank signature field alone is not
# an assertion that an event took place.
_EVENT = {
    "circulation": r"\b(?:circulat(?:ed|ion)|distributed|issued|sent)\b",
    "prepared_basis": r"\b(?:prepar(?:e|ed|ation)|execution direction|draft(?:ed|ing)? basis|mandated structure|selected structure)\b",
    "withdrawal": PREDICATES["withdrawal"].pattern,
    "conditional": PREDICATES["conditional"].pattern,
    "execution": r"\b(?:was|is|were|are|has|have|had|remains?|be|been|never|not|(?:is|was|were|are|has|have|had)n['’]t)\s+(?:(?:not|yet|never|formally|already|been|fully|previously)\s+)*"+PREDICATES["execution"].pattern,
}
_TOPIC = frozenset({"invitation","syndication","participation","tranche","term","loan","commitment","facility","letter","lender","refinancing"})
_EXCLUDE = {"historical","project","current","basis","direction","records","record","page","issued","stated","acknowledgement",
            "prepared","preparation","documentation","subject","conditional","approval","financing","draft","structure","withdrew",
            "withdrawal","withdrawn","tranche","preferred","current","executed","signed","execution","selected","consolidated",
            "annual","expense","platform","illustrative","credit","before","after","interest","spread","million","thousand",
            "billion","usd","bps","percent","revenue","leverage","metric","coverage","ratios"}
_CASE = re.compile(r"\b(?:case|scenario|option)(?:\s+[A-Z]|\s*\d+)\b",re.I)
_UNIT = re.compile(r"\b(?:bps?|basis points?|USD(?:000|m)?|multiple|millions?|thousands?)\b|[%$]",re.I)
_STUB = re.compile(r"(?:_{2,}|\u2610|\[\s*\])")
_FRAG = re.compile(r"(?<=[.!?;])\s+(?=[A-Z])|\n+")


def _topics(t):
    return _TOPIC.intersection(semantic_labels(t))


def _scenario_labels(text):
    # The original may say "Scenario" while a consumer uses "Case".
    return {re.search(r"[a-z]$|\d+$", match[0], re.I)[0].casefold() for match in _CASE.finditer(text)}


def _identities(t):
    cases = _scenario_labels(t)
    names = {s.casefold() for s in re.findall(r"(?<!\w)[A-Z][A-Za-z]{2,}\b",t)
             if s.lower() not in _EXCLUDE | _TOPIC and s.lower() in semantic_labels(t)
             and s not in {"The","This","Use","Any","Cash","Gross","Total","Senior","Opening","Headroom","Credit","Historic",
                           "Facility","Interest","Leverage","Revenue","Northbound","USD","EUR","GBP","IC","DSCR","CFADS","EBITDA"}}
    return cases,names


def _row_context(slots, loc):
    if "!" in loc or ":cell:" in loc or re.fullmatch(r"t:\d+:\d+:\d+",loc):
        return binding_context(slots,loc)
    return ""


def scoped_assertions(slots):
    from .native_grounding import dates
    assertions = []
    for loc, raw in slots.items():
        if loc.startswith("native:") or (isinstance(raw,str) and raw.startswith("=")):
            continue
        context = _row_context(slots,loc)
        for frag in _FRAG.split(str(raw)):
            frag = frag.strip()
            if not frag or _STUB.search(frag):
                continue
            line = (context + " " + frag).strip()
            topics = _topics(line)
            kinds = set()
            if any(q.dimension is not None or q.scale != 1 for q in document_quantities(frag,None)) and metrics(line):
                kinds.add("amount")
            if topics:
                for event,pattern in _EVENT.items():
                    if not re.search(pattern,line,re.I):
                        continue
                    if event=="circulation" and not dates(frag):
                        continue
                    if event=="prepared_basis" and not _CASE.search(frag):
                        continue
                    kinds.add(event)
                if dates(frag) and re.search(r"\b(?:historical|dated|circulated|issued|sent)\b",line,re.I):
                    kinds.add("circulation")
            if context and re.fullmatch(r"[-+]?\d[\d,.]*(?:\s*(?:%|bps?|x))?",frag,re.I) and _amount_metrics(context) and (_UNIT.search(context+" "+frag) or "[native rate fraction:" in context):
                kinds.add("amount")
            for kind in sorted(kinds):
                assertions.append({"locator":loc,"kind":kind,"fragment":frag,"context":context})
    return assertions


def _supports_event(assertion, node, rule):
    from .native_grounding import dates
    p=node["payload"]
    if not is_business_claim(node) or not admissible_text(p):
        return False
    kind = assertion["kind"]
    frag = assertion["fragment"]
    context = assertion["context"]
    if kind=="amount":
        # The ordinary quantity checker separately enforces the located value
        # and denomination. Verify the semantic currency of its row/column.
        target = _amount_metrics(context+" "+frag)
        named_metric = _amount_metrics(p["field"]) or _amount_metrics(p["claim"])
        if target and named_metric and not target.intersection(named_metric):
            return False
        if "[native rate fraction:" in context:
            # Consumer worksheet names and column headers such as "Value"
            # identify workbook slots. The operand quote supplies the named
            # rate and period; direct_context_match checks both below.
            cases,names = _identities(context.split("[native rate fraction:",1)[0])
            names = set()
        else:
            cases,names = _identities(context+" "+frag)
    else:
        if kind in PREDICATES:
            if not witnesses_action(frag,p["quote"],kind):
                return False
        elif not re.search(_EVENT[kind],p["quote"],re.I):
            return False
        if kind=="circulation" and not dates(frag) <= dates(p["quote"]):
            return False
        candidates = _topics(frag+" "+context)
        if not candidates.intersection(_topics(p["quote"])) and kind not in {"prepared_basis","withdrawal"}:
            return False
        if kind=="conditional":
            key = {v for v,pat in (("credit",r"credit approval"),("documentation",r"definitive documentation"),
                                  ("internal",r"internal approval")) if re.search(pat,frag,re.I)}
            if key and not all(k in p["quote"].lower() for k in key):
                return False
        relevant = frag
        if kind=="withdrawal":
            relevant = frag[re.search(_EVENT[kind],frag,re.I).start():]
        elif kind=="prepared_basis":
            indicator = re.search(_EVENT[kind],frag,re.I)
            relevant = frag[indicator.start():] if indicator else frag
            relevant = re.split(r"\s+and\s+(?:exclude|omit|withdraw|retain)",relevant,maxsplit=1,flags=re.I)[0]
        cases,names = _identities(relevant)
    evidence = (p["quote"]+" "+p["entity"]).casefold()
    if kind=="amount":
        evidence += " "+str(p["field"]).casefold()
    actual_cases = _scenario_labels(evidence)
    if cases and not cases <= actual_cases:
        return False
    if names and not cases and not any(re.search(r"\b"+re.escape(n)+r"\b",evidence) for n in names):
        return False
    # A scalar bound for a wholly different field cannot certify the event
    # simply because another sentence on the same source page mentioned it.
    expected = str(rule["expected"]).casefold().strip()
    if kind=="amount":
        if rule.get("semantic_term"):
            return True
        if rule.get("calculation_receipt"):
            return bool(calculation_matches(frag, rule["expected"],rule["unit"],None,context+" "+frag))
        if rule.get("original_unit") or "[native rate fraction:" in context:
            matched=direct_context_match(frag,rule["expected"],p,rule,None,context)
            if matched is not None:
                return bool(matched)
        from .native_grounding import amounts_match, claim_quantities
        try:
            number = Decimal(str(rule["expected"]))
        except Exception:
            return expected in frag.casefold() and any(q.dimension for q in document_quantities(expected,None))
        sources = [q for q in claim_quantities(p) if q.value==number or
                   (witnessed_fraction(rule["expected"],p) and q.dimension=="rate" and q.canonical==number)]
        targets = document_quantities(frag,None)
        if context and re.fullmatch(r"[-+]?\d[\d,.]*",frag):
            # Numeric table cells may rely on an explicit row or column unit.
            if re.search(r"\bbps?\b|basis points?",context,re.I):
                targets=document_quantities(frag+" bps",None)
            elif "%" in context or "percent" in context.casefold():
                targets=document_quantities(frag+"%",None)
            else:
                header = re.search(r"(USD|EUR|GBP|CNY)\s*(?:in\s+)?(millions?|thousands?|billions?|000s?\b)",context,re.I)
                if header:
                    scale="thousand" if header[2].startswith("0") else header[2]
                    targets=document_quantities(header[1]+" "+frag+" "+scale,None)
        return any(amounts_match(q,s) for q in targets for s in sources)
    if expected in frag.casefold():
        return True
    return bool(dates(expected) & dates(frag) or
                len(semantic_labels(expected) & semantic_labels(frag))>=2)


def admissible_text(p):
    # A title can identify a document. It cannot support a business conclusion.
    q = str(p.get("quote","")).strip()
    if not q or q.startswith("#") and "\n" not in q:
        return False
    return len(semantic_labels(q))>=2 and not re.fullmatch(r"\[(?:title|heading)[^\]]*\].*",q,re.I)


def verify_native_assertions(dag,slots,rules):
    located = {}
    for rule in rules:
        located.setdefault(rule["locator"].split("#line:")[0],[]).append(rule)
    results = []
    for a in scoped_assertions(slots):
        supported=[]
        for r in located.get(a["locator"],[]):
            if "#line:" in r["locator"]:
                index=int(r["locator"].split("#line:")[1])-1
                lines=str(slots[a["locator"]]).splitlines()
                if index>=len(lines) or a["fragment"] not in lines[index]:
                    continue
            node = dag.get(r["claim_id"])
            if _supports_event(a,node,r):
                supported.append(r["claim_id"])
        results.append({**a,"claim_ids":supported,"status":"verified" if supported else "needs_witness"})
    return results


def witnessed_fraction(value, claim):
    """Recover canonical rate stored as a fraction when the source prints percent or bps."""
    from .native_grounding import quantities
    if not isinstance(value,(int,float,Decimal)) or isinstance(value,bool):
        return False
    unit=str(claim.get("scope",{}).get("unit","")).strip().casefold()
    labels = str(claim.get("field",""))+" "+str(claim.get("claim",""))
    if unit not in {"fraction","ratio","%","percent","pct","bps","bp"} or not re.search(
            r"rate|fee|spread|amorti[sz]|sweep|growth|margin|yield|coupon|percent",labels,re.I):
        return False
    rates=[q for q in quantities(claim["quote"]) if q.dimension=="rate"]
    val=Decimal(str(value))
    # If the exact number also occurs as a displayed rate in a separate column,
    # its representation is ambiguous; the caller must bind a narrower claim.
    return abs(val)<1 and any(q.canonical==val for q in rates) and not any(q.value==val and q.canonical!=val for q in rates)


def literal_matches(actual, value, rule, claim, context):
    """A constant formula is an input value, but formulas reading other cells must be checked as computations."""
    raw = str(actual)
    if not re.fullmatch(r"=\s*[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?\s*",raw):
        return False
    try:
        same = Decimal(raw[1:].strip())==Decimal(str(value))
    except Exception:
        return False
    if not same:
        return False
    field = " ".join(rule.get("value_path",[])).replace("_"," ") or str(claim.get("field",""))
    if not metrics(context).intersection(metrics(field)):
        return False
    monetary = re.search(r"\b(USD|EUR|GBP|CNY)\s*(?:in\s+)?(000s?\b|millions?|thousands?|billions?|k\b)", context, re.I)
    label = str(claim.get("scope",{}).get("units",claim.get("scope",{}).get("unit",""))) + " "+field
    if monetary:
        currency = monetary[1].casefold()
        scale = monetary[2].casefold()
        scales = (r"thousands?|000s?\b|\bk\b" if scale.startswith(("0","t","k")) else
                  r"millions?|\bmn\b|\bmm\b" if scale.startswith("m") else r"billions?|\bbn\b")
        return bool((currency in label.lower() or currency in claim["quote"].lower()) and
                    re.search(scales,label,re.I))
    if re.search(r"\bbps?\b",context,re.I):
        return bool(re.search(r"bps?|basis points?",field+" "+claim["quote"],re.I))
    return False

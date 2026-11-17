"""Executable scalar calculations over released ledger operands, with units."""
import ast
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import re

from .native_grounding import quantities


# Canonical units also appear in the model-facing schema. Aliases are lossless
# spelling changes; bare $, k, "units" and unstated FX are intentionally ambiguous.
_SCALE_ALIASES = {"": ("",), "000": ("000","000s","k","thousand","thousands"),
                  "m": ("m","mm","mn","million","millions"), "bn": ("bn","billion","billions")}
_RATE_ALIASES = {"fraction": ("fraction","ratio"), "x": ("x","multiple","times"),
                 "count": ("count",), "%": ("%","pct","percent","per cent","percentage"),
                 "bps": ("bps","bp","basis point","basis points")}
_CURRENCIES = ("USD","EUR","GBP","CNY")
UNIT_NAMES = [*(c+s for c in _CURRENCIES for s in _SCALE_ALIASES),*_RATE_ALIASES]
_UNIT_GUIDE = ", ".join(UNIT_NAMES)
_FY = re.compile(r"\bFY[ -]?(\d{4})\b",re.I)
_COLUMN_FY = re.compile(r"FY[ -]?(\d{4})[AEF]?",re.I)
_MONEY_TITLE = re.compile(r"(?<!\w)(USD|US\$|\$|EUR|GBP|CNY)\s*(000s?|thousands?|millions?|billions?|bn|mm|mn|k|m)?(?!\w)",re.I)


def _canonical_unit(label):
    raw=re.sub(r"\s+"," ",str(label).strip().casefold())
    for canonical,aliases in _RATE_ALIASES.items():
        if raw in aliases:
            return canonical
    m=re.fullmatch(r"(usd|eur|gbp|cny)\s*(?:in\s+)?([a-z0-9]*)",raw)
    if m:
        for suffix,aliases in _SCALE_ALIASES.items():
            if m[2] in aliases:
                return m[1].upper()+suffix
    raise ValueError("Use a supported unit: "+_UNIT_GUIDE+" (e.g. USD thousands = USD000)")


def _unit(label):
    name=_canonical_unit(label)
    if name in _RATE_ALIASES:
        return "", Decimal({ "%":"0.01","bps":"0.0001" }.get(name,"1"))
    return name[:3], Decimal({"":1,"000":1000,"m":10**6,"bn":10**9}[name[3:]])


def _quantity_kind(label):
    unit=_canonical_unit(label)
    if unit[:3] in _CURRENCIES:
        return unit[:3]
    return "multiple" if unit=="x" else "count" if unit=="count" else "rate"


def reporting_dates(fiscal_period=None,valid_from=None,valid_to=None):
    """FY is an accounting dimension, never an inferred calendar interval."""
    years=set()
    if fiscal_period:
        m=_FY.fullmatch(str(fiscal_period).strip())
        if not m:
            raise ValueError("Use FY plus four digits for fiscal_period, e.g. FY2025")
        years.add("FY"+m[1])
    dates=[]
    for day in (valid_from,valid_to):
        if day and (m:=_FY.fullmatch(day.strip())):
            years.add("FY"+m[1])
            dates.append(None)
        else:
            if day:
                try:
                    date.fromisoformat(day)
                except ValueError as exc:
                    raise ValueError("Use YYYY-MM-DD for a business-effective date and FYyyyy for fiscal_period") from exc
            dates.append(day)
    if len(years)>1:
        raise ValueError("Different fiscal periods require an explicit period comparison")
    return next(iter(years),None),*dates


def _claim_period(p):
    scope=p.get("scope",{})
    declared={m[1] for k in ("period","fiscal_period","fiscal_year") if (m:=_FY.fullmatch(str(scope.get(k,"")).strip()))}
    field=set(_FY.findall(str(p.get("field","")).replace("_"," ")))
    if len(declared|field)>1:
        raise ValueError("Claim field and scope identify different reporting periods")
    return next(("FY"+year for year in declared|field),None)


def _source_witness(claim,line,role):
    start=line.start()
    return {"source_id":claim["source_id"],"role":role,"start":start,"end":line.end(),"quote":line[0]}


def _split_line(raw):
    raw=re.sub(r"^\[table \d+ row \d+\]\s*","",raw)
    return [re.sub(r"^[A-Z]+\d+=","",x.strip()) for x in raw.split("|")]


def _source_blocks(body):
    active=None
    for line in re.finditer(r"(?m)^[^\r\n]+",body):
        token=re.match(r"^\[(slide \d+|sheet [^\]]+|table \d+)\b",line[0])
        if token and (not active or active[0]!=token[1]):
            if active:
                yield active[1]
            active=(token[1],[])
        if active:
            active[1].append(line)
    if active:
        yield active[1]


def _words(text):
    return " ".join(re.findall(r"[a-z0-9]+",str(text).casefold()))


def _metric_words(text):
    return set(_words(text).split())-{"segment","segments","group","platform","and","margin","measure","metric","adjusted"}


def _time_cell(source,claim,requested):
    """Locate one released row and exact FY column inside a native table.

    A same-document second table may witness the missing unit only if it
    repeats that segment, fiscal year, metric and amount in its own table.
    A global denomination alone cannot prove the row, metric or period.
    """
    body=source["text"]
    blocks=list(_source_blocks(body))
    identity=" ".join([_words(claim["entity"]),_words(claim["field"]),_words(claim["claim"])])
    entity=_words(claim["entity"])
    field=_words(str(claim["field"])+" "+str(claim.get("scope",{}).get("metric","")))
    selected=[]
    for block in blocks:
        if block[-1].end()<claim["start"] or block[0].start()>claim["end"]:
            continue
        header=None
        for j,line in enumerate(block):
            cells=_split_line(line[0])
            years=[_COLUMN_FY.fullmatch(s) for s in cells[1:]]
            if years and all(years):
                header=(j,line,["FY"+y[1] for y in years])
                continue
            if not header or "|" not in line[0]:
                header=None
                continue
            hi,h,years=header
            if len(cells)!=1+len(years) or not (line.start()<claim["end"] and line.end()>claim["start"]):
                continue
            label=_words(cells[0])
            if requested is None:
                raise ValueError("Supply fiscal_period or anchor the claim to a specific FY column")
            if years.count(requested)!=1:
                raise ValueError("Reporting period does not identify exactly one source column")
            headlabel=_split_line(h[0])[0]
            title=h if _metric_words(headlabel)&_metric_words(field)-{"segment","measure","line"} else block[max(1,hi-1)]
            label_is_metric=bool(_metric_words(label)&{"ebitda","revenue","profit","capex","cfads","cash","interest","debt","expense","overhead","tax","sales","income"})
            # A row entity and its measure are independent constraints. A
            # number in a different row of the same quoted block is insufficient.
            named_row=bool(label and re.search(r"\b"+re.escape(label)+r"\b",entity+" "+field))
            if not named_row:
                # For group-labeled claims, a fully named segment in the
                # claim itself is admissible only if its field names that
                # segment too (e.g. "FY2025 Metro EBITDA", "Metro Fiber").
                tokens=label.split()
                named_row=bool(tokens and tokens[0] in field.split() and
                    re.search(r"\b"+re.escape(label)+r"\b",identity) and
                    all(t in field.split() or t in _words(claim["claim"]).split() for t in tokens))
            if not named_row:
                continue
            if not label_is_metric and label not in entity:
                if not re.search(r"\b"+re.escape(entity)+r"\b",_words(body)):
                    raise ValueError("Claimed business entity is absent from this reporting source")
            if label_is_metric:
                if not _metric_words(label)&_metric_words(field):
                    continue
            elif not _metric_words(title[0])&_metric_words(field):
                continue
            ix=1+years.index(requested)
            if not _NUMBER.fullmatch(cells[ix]):
                raise ValueError("The selected fiscal table slot is not a released numeric observation")
            selected.append((block,title,h,line,cells[0],cells[ix],requested))
    if not selected:
        # An anchored multi-year table must not fall back to a numeric search
        # across the whole quote, even if an unrelated amount equals the claim.
        for block in blocks:
            if block[-1].end()>=claim["start"] and block[0].start()<=claim["end"]:
                overlaps=any("|" in ln[0] and ln.start()<claim["end"] and ln.end()>claim["start"] for ln in block)
                if overlaps and any(sum(bool(_COLUMN_FY.fullmatch(c)) for c in _split_line(ln[0]))>=2 for ln in block):
                    raise ValueError("No matching entity, metric and fiscal column in the released table")
        return None
    if len(selected)!=1:
        raise ValueError("Choose an unambiguous entity row and fiscal column in the original table")
    block,title,header,row,label,val,period=selected[0]
    witnesses=[_source_witness(claim,ln,role) for ln,role in ((title,"metric"),(header,"fiscal_header"),(row,"entity_row"))]
    def denomination(raw):
        units={_unit(("USD" if m[1].upper() in ("$","US$") else m[1])+(m[2] or ""))
               for m in _MONEY_TITLE.finditer(raw)}
        if len(units)>1:
            raise ValueError("The source table reports competing currency denominations")
        return next(iter(units),None)
    scale=denomination(row[0].split("|")[0]) or denomination(title[0]) or denomination(header[0].split("|")[0])
    if scale is None:
        units={}
        for alt in blocks:
            if alt is block or not any(re.search(r"\b"+re.escape(_words(label))+r"\b",_words(ln[0]))
                                       for ln in alt[1:3] if "|" not in ln[0]):
                continue
            headings=None
            for ln in alt:
                cells=_split_line(ln[0])
                fy=[_COLUMN_FY.fullmatch(x) for x in cells[1:]]
                if fy and all(fy):
                    headings=(ln,["FY"+x[1] for x in fy])
                elif headings and len(cells)==len(headings[1])+1:
                    if (headings[1].count(period)!=1 or _metric_words(cells[0]).isdisjoint(_metric_words(title[0])) or
                            not re.search(r"\bEBITDA\b",cells[0],re.I) and "ebitda" in _metric_words(title[0])):
                        continue
                    if "margin" in _words(cells[0]).split() or "%" in cells[0]:
                        continue
                    other=cells[headings[1].index(period)+1]
                    den=denomination(cells[0])
                    if den and _NUMBER.fullmatch(other) and Decimal(other.replace(",",""))==Decimal(val.replace(",","")):
                        units[den]=[_source_witness(claim,alt[1],"corroborating_entity"),
                            _source_witness(claim,headings[0],"corroborating_period"),_source_witness(claim,ln,"metric_unit_and_value")]
                else:
                    headings=None
        if len(units)>1:
            raise ValueError("Competing denominations in scope-matched source tables")
        if units:
            scale,refs=next(iter(units.items()))
            witnesses.extend(refs)
    return val,scale,witnesses


_NUMBER=re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


def _table_cell(source, claim):
    """A released Word table row plus its real scenario header restricts a scalar."""
    body = source["text"]
    start = body.rfind("\n", 0, claim["start"]) + 1
    end = body.find("\n",claim["end"])
    line = body[start:end if end >= 0 else len(body)]
    row = re.fullmatch(r"\[table (\d+) row (\d+)\] (.*)",line)
    if not row:
        return claim["quote"], []
    prefix = f"[table {row[1]} row "
    current_cells = [c.strip() for c in row[3].split("|")]
    identity = " ".join([claim["entity"], str(claim["field"]), str(claim["scope"].get("scenario", ""))])
    header = None
    for h in re.finditer(r"(?m)^" + re.escape(prefix) + r"\d+\] ([^\n]*)",body[:start]):
        labels = [c.strip() for c in h[1].split("|")]
        if len(labels)==len(current_cells) and sum(bool(re.fullmatch(r"(?:option|scenario|case)\s+\w+", x,re.I)) for x in labels)>=2:
            header=(h, labels)
    if header is None:
        return claim["quote"], []
    h, labels=header
    cols = [i for i,label in enumerate(labels) if label and
            re.search(r"(?<!\w)"+re.escape(label)+r"(?!\w)",identity,re.I)]
    if len(cols)!=1:
        raise ValueError("Choose the scenario given by the original table header for this row")
    ix = cols[0]
    if not current_cells[ix] or current_cells[ix] not in claim["quote"]:
        raise ValueError("Anchor the selected column's value in its released row")
    witness=[{"source_id":claim["source_id"],"start":h.start(),"end":h.end(),"quote":h[0]}]
    return current_cells[ix],witness


def _declared_scale(source, claim):
    # Only a unique explicit all-amounts declaration can supply a missing unit.
    pattern = r"\b(?:all (?:financial )?(?:amounts|figures)|all figures)\s+(?:are )?(?:(?:stated|shown|expressed) )?in\s+(?:(USD|US dollars?|dollars?|EUR|GBP|CNY)\s+(thousands?|millions?|billions?)|(thousands?|millions?|billions?) of (dollars?|US dollars?|euros?|pounds?))"
    found = {}
    for m in re.finditer(pattern,source["text"],re.I):
        raw, scale = (m[1] or m[4]).lower(),(m[2] or m[3]).lower()
        unit = {"usd":"USD","dollar":"USD","dollars":"USD","us dollar":"USD","us dollars":"USD",
                "eur":"EUR","euro":"EUR","euros":"EUR","gbp":"GBP","pound":"GBP","pounds":"GBP","cny":"CNY"}[raw]
        factor = 10**3 if scale.startswith("thousand") else 10**6 if scale.startswith("million") else 10**9
        found[(unit, factor)] = {"source_id":claim["source_id"],"start":m.start(),"end":m.end(),"quote":m[0]}
    return next(iter(found.items())) if len(found)==1 else (None,None)


def _operand(dag, item, active, fiscal_period=None):
    cid = item["claim_id"]
    if cid not in active:
        raise ValueError("Calculation input is pending, superseded or outside its effective period")
    n = dag.get(cid)
    p = n["payload"]
    from .live_review import source_role
    if source_role(p) != "business_evidence":
        raise ValueError("A business calculation input requires an original business fact")
    dim, factor = _unit(item["unit"])
    try:
        scalar = Decimal(str(p["value"]).replace(",","")) * factor
        if not scalar.is_finite():
            raise InvalidOperation
    except InvalidOperation as exc:
        raise ValueError("Use separate atomic numeric claims for principal, spread and other inputs; an object is not a scalar") from exc
    witnesses = []
    receipt = verified_calculation(dag, n)
    if receipt:
        old_dim, old_factor = _unit(receipt["payload"]["result_unit"])
        if old_dim != dim or old_factor != factor or _quantity_kind(item["unit"]) != _quantity_kind(receipt["payload"]["result_unit"]):
            raise ValueError("Derived operand denomination differs from its executable receipt")
    else:
        source = dag.get(p["source_id"])["payload"]
        if source["text"][p["start"]:p["end"]] != p["quote"]:
            raise ValueError("Calculation input quote differs from the released bytes")
        reporting = fiscal_period or _claim_period(p)
        native = _time_cell(source,p,reporting)
        if native:
            span, denomination, witnesses = native
            if denomination is None:
                denomination, global_unit = _declared_scale(source,p)
                if global_unit:
                    witnesses.append(global_unit)
            if (not denomination or not dim or denomination[0]!=dim or
                    Decimal(span.replace(",",""))*denomination[1]!=scalar):
                raise ValueError(f"Operand {item['name']} disagrees with the reported entity, fiscal column, value or denomination")
            return scalar, _quantity_kind(item["unit"]), witnesses
        span, witnesses = _table_cell(source,p)
        qs = quantities(span)
        expected = {dim}
        if dim == "USD":
            expected.add("dollar")
        if not dim:
            expected = {{"count":None, "multiple":"multiple", "rate":"rate"}[_quantity_kind(item["unit"])]}
        matching = [q for q in qs if q.dimension in expected and q.canonical == scalar]
        if not matching and dim and re.search(r"\b"+dim+r"\b",p["quote"],re.I):
            matching = [q for q in qs if q.dimension=="amount" and q.canonical==scalar]
        if not matching:
            # A table's explicit header establishes scale if it occurs in
            # the anchored quotation. An arbitrary model-supplied scope cannot.
            units = re.search(r"(?:USD|US\$|\$|EUR|GBP|CNY)\s*(?:in\s+)?(?:000s?\b|thousands?\b|millions?\b|billions?\b)",
                              p["quote"],re.I)
            if units:
                t = units[0].casefold()
                scale = 10**9 if "billion" in t else 10**6 if "million" in t else 1000
                curr = next((c for c in ("EUR","GBP","CNY") if c.casefold() in t), "USD")
                matching = [q for q in qs if dim == curr and q.dimension in (None,dim,"dollar")
                            and q.scale==1 and q.value * scale == scalar]
        if not matching and dim:
            declaration, witness = _declared_scale(source,p)
            if declaration == (dim,factor) and any(q.dimension in (None,dim,"dollar") and q.value*factor==scalar for q in qs):
                matching = [witness]
                witnesses.append(witness)
        if not matching:
            raise ValueError(f"Operand {item['name']} does not match its original column and denomination; "
                             "cite the source term with its unit or the row of a labeled scenario table")
    return scalar, _quantity_kind(item["unit"]), witnesses


def verified_calculation(dag, claim):
    for pid in claim["parents"]:
        node = dag.get(pid)
        if node["kind"] == "calculation_receipt":
            p, c = node["payload"], claim["payload"]
            if (p["entity"]==c["entity"] and p["field"]==c["field"] and p["result"]==c["value"]
                    and [p["inputs"]] == c["supports"] and p["purpose"] == c["scope"].get("purpose")):
                return node
    return None


def _compute(expr, values):
    if isinstance(expr, ast.Name) and expr.id in values:
        return values[expr.id]
    if isinstance(expr, ast.Constant) and type(expr.value) is int and expr.value in {0,1}:
        return Decimal(expr.value), ""
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.USub):
        value, dim = _compute(expr.operand,values)
        return -value, dim
    if isinstance(expr, ast.BinOp):
        a, left = _compute(expr.left,values)
        b, right = _compute(expr.right,values)
        ratios = {"rate", "multiple", "quotient"}
        if isinstance(expr.op,(ast.Add,ast.Sub)):
            compatible = (left == right or
                          (not left and (not a or right in ratios)) or
                          (not right and (not b or left in ratios)))
            if compatible:
                return (a+b if isinstance(expr.op,ast.Add) else a-b), left or right
        elif isinstance(expr.op,ast.Mult):
            if left in ratios or not left:
                return a*b, right or left
            if right in ratios or not right:
                return a*b, left
        elif isinstance(expr.op,ast.Div) and b:
            if left == right and left:
                return a/b, "quotient"
            if right in ratios or not right:
                return a/b, left
    raise ValueError("Unsupported expression, incompatible units or division by zero")


def calculate(dag, ctx, *, expression, operands, entity, field, purpose, result_unit, valid_from=None, valid_to=None, fiscal_period=None):
    if not entity.strip() or not field.strip():
        raise ValueError("Give the entity and field for the calculation")
    fiscal_period,valid_from,valid_to=reporting_dates(fiscal_period,valid_from,valid_to)
    result_unit=_canonical_unit(result_unit)
    if not (1 <= len(operands) <= 12 and len(expression)<=250) or not purpose.strip():
        raise ValueError("Give a purpose, a short arithmetic expression and up to 12 named inputs")
    names = [o["name"] for o in operands]
    active = {n["id"] for n in dag.claim_at(valid_at=valid_from)}
    if len(set(names)) != len(names) or any(not re.fullmatch(r"[a-z][a-z_0-9]{0,30}", name) for name in names):
        raise ValueError("Use each expression identifier as its operand name (e.g. beta). Put fiscal_period and entity outside that identifier")
    try:
        tree = ast.parse(expression,mode="eval")
    except (SyntaxError,ValueError) as exc:
        raise ValueError("Expected arithmetic on the named original-backed operands") from exc
    if set(names)!={n.id for n in ast.walk(tree) if isinstance(n,ast.Name)} or len(list(ast.walk(tree)))>90:
        raise ValueError("Expression and named operands must agree")
    claims=[dag.get(o["claim_id"]) for o in operands]
    periods={p for n in claims if (p:=_claim_period(n["payload"]))}
    if fiscal_period:
        periods.add(fiscal_period)
    if len(periods)>1:
        raise ValueError("Operand reporting periods differ; choose facts from the requested FY")
    fiscal_period=next(iter(periods),None)
    def scenario_ids(text):
        return {m.upper() for m in re.findall(r"\b(?:Option|Case|Scenario)[ -]+([a-z]|[0-9]+)\b",text,re.I)}
    intended_scenarios = scenario_ids(entity+" "+field.replace("_"," ")+" "+purpose)
    input_scenarios=set()
    for n in claims:
        current=n["payload"]
        current_scenarios = scenario_ids(current["entity"]+" "+str(current["scope"].get("scenario","")))
        input_scenarios.update(current_scenarios)
        # Entity hierarchy is only admissible when the original identifies
        # that legal parent and the operand explicitly names it.
        if _words(entity)!=_words(current["entity"]):
            parent=_words(entity)
            body=dag.get(current["source_id"])["payload"]["text"]
            original_entity = re.search(r"\b"+re.escape(parent)+r"\b",_words(body))
            related_segment = _words(current["entity"]).startswith(parent+" ")
            applicable_option = len(current_scenarios)==1 and intended_scenarios==current_scenarios
            if not (parent and original_entity and (related_segment or applicable_option)):
                raise ValueError("A calculation cannot relabel an operand as a different entity; record the evidenced parent or scenario")
    if len(input_scenarios)>1 or (intended_scenarios and input_scenarios and intended_scenarios!=input_scenarios):
        raise ValueError("Distinct financing scenarios require separate calculations and purpose-bound receipts")
    with localcontext() as dc:
        dc.prec = 28
        observed = {o["name"]:_operand(dag,o,active,fiscal_period) for o in operands}
        value, dim = _compute(tree.body,{k: v[:2] for k,v in observed.items()})
        unit_and_column_evidence = {k:v[2] for k,v in observed.items() if v[2]}
        _, factor = _unit(result_unit)
        result_kind = _quantity_kind(result_unit)
        # A ratio between two like amounts can be displayed as a percentage
        # or a multiple. A count cannot borrow a rate source or vice versa.
        if dim != result_kind and not (dim=="quotient" and result_kind in {"rate","multiple"}):
            raise ValueError("Output denomination has the wrong dimension")
        display = value/factor
        if abs(display)>Decimal("1e30"):
            raise ValueError("Derived value outside supported scalar range")
    result = int(display) if display == display.to_integral_value() else float(display)
    ids = list(dict.fromkeys(o["claim_id"] for o in operands))
    inputs = [dag.get(i)["payload"] for i in ids]
    # The common business interval of the operands bounds the result.
    starts = [p["valid_from"] for p in inputs if p["valid_from"]]
    ends = [p["valid_to"] for p in inputs if p["valid_to"]]
    lo = max(starts+([valid_from] if valid_from else []), default=None)
    hi = min(ends+([valid_to] if valid_to else []), default=None)
    if lo and hi and lo>=hi:
        raise ValueError("Inputs have no common business-effective period")
    rec = dag.append("calculation_receipt", {"stage":ctx["stage"], "purpose":purpose, "entity":entity, "field":field,
                     "expression":expression,"operands":operands,"inputs":ids,"result":result,"result_unit":result_unit,
                     "unit_and_column_evidence":unit_and_column_evidence,"exact_decimal":str(display),
                     "fiscal_period":fiscal_period,"latest_input_seq":max(n["seq"] for n in claims),
                     "source_stage_by_input":{n["id"]:n["payload"]["source_stage"] for n in claims},"valid_from":lo,"valid_to":hi},[ctx["request_id"],*ids])
    first = inputs[0]
    claim = dag.record_claim(first["source_id"], entity, f"{field} = {result} {result_unit}",
               "Computed from the individually cited inputs in "+rec["id"],first["quote"],start=first["start"],
               field=field,value=result,scope={"purpose":purpose,"unit":result_unit,**({"fiscal_period":fiscal_period} if fiscal_period else {})},
               valid_from=lo,valid_to=hi,supports=[ids],parents=[rec["id"]])
    return {"claim_id":claim["id"],"receipt_id":rec["id"],"value":result,"unit":result_unit,"inputs":ids,
            "pending":claim["payload"]["pending"],"fiscal_period":fiscal_period}

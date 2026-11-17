"""Turn released instructions into version-independent requirements on native outputs.

A requirement records who requested work and which released materials define its
scope. An observed row, clause or group of formulas only proves structural
coverage. The artifact guard separately checks business inputs and calculations.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath

from openpyxl.utils.cell import column_index_from_string

from .native_finance import inspect_workbook, projected_subrow, scenario
from .native_grounding import amounts_match, quantities


def _has(pattern, text):
    return re.search(pattern, text, re.I) is not None


def _lines(node):
    payload = node["payload"]
    body = payload.get("text", payload.get("instruction", ""))
    for match in re.finditer(r"[^\r\n]+", body):
        value = match[0].strip()
        start = match.start() + len(match[0]) - len(match[0].lstrip())
        if value:
            yield {"source_id": node["id"], "locator": payload.get("locator", "stage instruction"),
                   "quote": value, "start": start, "end": start + len(value)}


def _matching(sources, pattern):
    for source in reversed(sources):
        for line in _lines(source):
            if _has(pattern, line["quote"]):
                return line
    return None


_CASE = re.compile(r"\b(?:option|case|scenario)\s+([A-Z]|\d+)\b", re.I)


def _scenario_terms(sources):
    # Prefer the most recently released explicit list in a financing context.
    # Expand shorthand such as "Case 1 / 2 / 3" only when the source uses it.
    for source in reversed(sources):
        for line in _lines(source):
            quote = line["quote"]
            if not _has(r"financ|structur|option|facilit", quote):
                continue
            short = re.search(r"\b(case|option|scenario)\s+([A-Z]|\d+)((?:\s*/\s*(?:[A-Z]|\d+)\b)+)", quote, re.I)
            if short:
                # A change register can hold both old and new labels on one row.
                short = list(re.finditer(short.re, quote))[-1]
                label, first, rest = short.groups()
                labels = [label + " " + x for x in [first, *re.findall(r"/\s*([A-Z]|\d+)", rest, re.I)]]
            else:
                labels = list(dict.fromkeys(m[0] for m in _CASE.finditer(quote)))
            if 2 <= len(labels) <= 12:
                return line, labels
    return None, []


def _order_rows(sources):
    """Read institution, order and status by actual released table headers."""
    for source in reversed(sources):
        headers, rows = {}, []
        amount_header = ""
        for line in _lines(source):
            quote = line["quote"]
            if quote.startswith("[sheet"):
                if rows:
                    break
                headers = {}
            cells = dict(re.findall(r"(?:^|\|\s*)([A-Z]+)\d+=([^|]+)", quote))
            cells = {c: v.strip() for c, v in cells.items()}
            if not cells:
                continue
            if any(_has(r"^(institution|lender|account)(?: name)?$", v) for v in cells.values()):
                headers = {}
                for col, value in cells.items():
                    if _has(r"^(?:institution|lender|account)(?: name)?$", value):
                        headers["entity"] = col
                    elif _has(r"status", value):
                        headers["status"] = col
                    elif _has(r"commitment|order amount", value):
                        headers["amount"] = col
                        amount_header = value
                continue
            if not {"entity", "status"} <= headers.keys():
                continue
            name = cells.get(headers["entity"])
            status = cells.get(headers["status"])
            if name and status:
                rows.append({"entity": name, "status": status,
                             "amount": cells.get(headers.get("amount")), "amount_header": amount_header,
                             "source": line})
        if rows:
            return rows
    return []


def _workbook_source(sources, instruction):
    # The requested predecessor's version and format select a model. A newer
    # tax-input schedule or order book is not a predecessor of the core model.
    version = re.search(r"\b(v\d+)\s+(?:core\s+)?(?:financing\s+)?model", instruction, re.I)
    for node in reversed(sources):
        name = PurePosixPath(node["payload"]["locator"]).name
        if name.lower().endswith(".xlsx") and _has("model", name):
            if version and not _has(r"\b" + re.escape(version[1]) + r"\b", name.replace("_", " ")):
                continue
            return node
    return None


def register_obligations(dag, stage_node, released_source_ids, expected):
    """Called after trusted stage release. Never accesses an evaluator or case directory."""
    stage = stage_node["payload"]["stage"]
    released_source_ids = list(released_source_ids)
    cutoff = max([stage_node["seq"], *(dag.get(i)["seq"] for i in released_source_ids)])
    sources = [n for n in dag.nodes("source", as_of=cutoff) if n["payload"]["stage"] <= stage
               and n["payload"]["locator"].startswith("source/")]
    task = next((n for n in dag.nodes("source", as_of=cutoff)
                 if n["id"] in released_source_ids and n["payload"]["locator"].startswith("instructions/")), stage_node)
    lines = [line for line in _lines(task)
             if not _has(r"^(Materials|Deliverables|Information cut-off)\s*:", line["quote"])]
    if not lines:
        if stage_node["kind"] == "stage":
            from .work_items import WorkItems
            WorkItems(dag).register(stage_node, task, expected)
        return []
    instruction = "\n".join(line["quote"] for line in lines)
    prior = dag.nodes("task_obligation")
    registered = []
    by_name = defaultdict(list)
    for path in expected:
        by_name[PurePosixPath(path).name].append(path)
    for name, paths in by_name.items():
        def add(kind, evidence, *, detail=None, parent=None, purpose=None):
            evidence = list({(x["source_id"], x["start"], x["end"]): x for x in evidence if x}.values())
            if not evidence:
                return
            existing = next((n for n in prior if n["payload"]["request_id"] == stage_node["id"]
                             and n["payload"]["paths"] == paths and n["payload"]["kind"] == kind
                             and n["payload"]["evidence"] == evidence), None)
            if existing:
                registered.append(existing)
                return
            payload = {"kind": kind, "stage": stage, "request_id": stage_node["id"], "paths": paths,
                       "purpose": purpose or name, "evidence": evidence, "detail": detail or {}}
            parents = [stage_node["id"], *(e["source_id"] for e in evidence)]
            if parent:
                parents.append(parent)
            registered.append(dag.append("task_obligation", payload, parents))

        suffix = PurePosixPath(name).suffix.casefold()
        add("task_request", lines)
        decision = next((q for q in lines if _has(r"committee", q["quote"])
                         and _has(r"decid|decision", q["quote"]) and _has(r"refinanc|financing", q["quote"])), None)
        if decision and suffix == ".pptx":
            clause, labels = _scenario_terms(sources)
            if labels:
                add("quantified_alternatives", [decision, clause], detail={"alternatives": labels},
                    purpose="committee financing decision")
        capacity = next((q for q in lines for sentence in re.split(r"[.!?;]",q["quote"])
                         if _has(_CAPACITY_REQUEST,sentence) and _has(_GROUP,sentence)
                         and not _has(r"\b(?:subsidiary|OpCo)\W+(?:s |and |only )?(?:debt.service |capacity to)",sentence)),None)
        if (capacity and suffix in {".pptx",".docx"} and _has(r"\b(?:lend(?:er|ing)|credit approv|underwrit)",instruction)
                and _has(r"\b(?:proposed|prospective|indicative|contemplated) (?:debt|financing|refinancing|facilit)",instruction)
                and not _has(r"(?:do not|not required to|outside (?:the )?scope to) (?:assess|evaluate|judge|analyse|analyze)|(?:out of scope|not requested)",capacity["quote"])):
            bases,terms = _capacity_evidence(sources)
            terms += _direct_capacity(sources)
            references=[q for obj in [*bases,*terms] for q in obj["sources"]]
            add("platform_debt_capacity",[capacity,*references],detail={"bases":bases,"terms":terms},
                purpose="lender assessment of platform debt service under proposed financing")
        debt = next((q for q in lines if _has(r"debt[ -]servic(?:e|ing) capacity|capacity to service debt", q["quote"])), None)
        if debt and suffix == ".xlsx":
            clause, labels = _scenario_terms(sources)
            if labels and _has(r"compar", instruction):
                add("model_alternatives", [debt, clause], detail={"alternatives": labels})
            add("cash_available_for_debt_service", [debt])
            add("debt_service_schedule", [debt])
            base = _workbook_source(sources, instruction)
            if base:
                for kind, pattern in _OPERATING.items():
                    line = _matching([base], pattern)
                    if line:
                        add(kind, [debt, line])
            for kind, pattern in _DEBT.items():
                # Only contract terms can make scheduled payments an obligation.
                terms = [s for s in sources if _has(r"term.sheet|financing.feedback|loan.agreement|credit.agreement",
                                                    s["payload"]["locator"])]
                line = _matching(terms, pattern)
                if line:
                    if kind == "cash_sweep":
                        contract = next(s for s in terms if s["id"] == line["source_id"])
                        scoped = [q for q in _lines(contract) if scenario(q["quote"]) and _has(pattern, q["quote"])]
                        add(kind, [debt, line, *scoped])
                    else:
                        add(kind, [debt, line])
        elif suffix == ".xlsx":
            # An instruction to carry a prior workbook forward also carries its
            # analysis requirements. Select the corresponding prior path or the
            # named core predecessor, keeping each requirement's source lineage.
            ancestors = []
            if _has(r"(?:from|pull|carry|earlier|hand.?over|prior)", instruction):
                ancestors = [n for n in prior if n["payload"]["stage"] < stage and
                             any(PurePosixPath(p).name == name for p in n["payload"]["paths"])]
                if not ancestors and _has(r"from the .{0,24}core model", instruction):
                    ancestors = [n for n in prior if n["payload"]["kind"] == "cash_available_for_debt_service"]
                    if ancestors:
                        predecessor_paths = ancestors[-1]["payload"]["paths"]
                        ancestors = [n for n in prior if n["payload"]["paths"] == predecessor_paths]
            seen = set()
            for previous in reversed(ancestors):
                p = previous["payload"]
                if p["kind"] == "task_request" or p["kind"] in seen:
                    continue
                # A standalone scenario workbook may select one maintained case.
                if p["kind"] == "model_alternatives" and _has(r"standalone scenario", instruction):
                    continue
                seen.add(p["kind"])
                add(p["kind"], [lines[0], *p["evidence"]], detail=p["detail"], parent=previous["id"],
                    purpose=p["purpose"])
        if suffix != ".docx" or not _has(r"commitment", name) or not _has(r"(?:signing|execution) instruction", instruction):
            continue
        # Only the referenced, current instruction authorizes these uses. Other
        # released business documents are evidence, never independent commands.
        directions = [s for s in sources if s["id"] in released_source_ids and
                      _has(r"instruction", s["payload"]["locator"])]
        template = _matching(directions, r"commitment[ -]letter template|template for .{0,20}commitment")
        if template:
            add("fillable_commitment", [lines[0], template], purpose="individual lender template")
        annex = _matching(directions, r"syndicate.{0,60}(?:annex|appendix)|(?:annex|appendix).{0,60}syndicate")
        if annex:
            accounts = _order_rows(sources)
            add("order_annex", [lines[0], annex, *(row["source"] for row in accounts)],
                detail={"accounts": [{k: v for k, v in row.items() if k != "source"} for row in accounts]},
                purpose="internal indicative order annex")
    if stage_node["kind"] == "stage":
        from .work_items import WorkItems
        WorkItems(dag).register(stage_node, task, expected)
    return registered


_OPERATING = {"operating_revenue": r"\b(?:total |segment )?revenue\b",
              "depreciation": r"depreciation|D&A", "capital_expenditure": r"capex|capital (?:expenditure|programme?)",
              "cash_tax": r"(?:cash |income |default )?tax(?:es| rate)?\b"}
_DEBT = {"scheduled_amortization": r"scheduled amorti[sz]ation|annual amorti[sz]ation",
         "cash_sweep": r"(?:cash flow|cash|ecf)[ -]sweep"}
_FINANCIAL_OUTCOME = r"interest(?: cost| expense| coverage| sensitivity| burden| payable| payments?)?|leverage|headroom|debt[ -]service|DSCR|cash cover"
_DEFER = r"\b(?:not (?:yet )?(?:calculat|quantif|establish|model|comput|project|determin)|(?:to be|yet to be) (?:calculated|modelled|quantified)|(?:will|must|shall|need(?:s)? to) (?:calculate|return|provide|model)|TBD|awaiting|request(?:ed)? (?:that|further))"
_CASH = r"\bCFADS\b|cash (?:flow )?(?:available|available cash flow) (?:for|to) (?:the )?(?:debt|service)|cash available (?:before|after) debt service"


def _native_groups(slots):
    """Group table cells by actual row, keeping precise individual locators."""
    rows = defaultdict(list)
    for locator, value in slots.items():
        if locator.startswith("native:") or locator.endswith(":notes"):
            continue
        address = re.fullmatch(r"(.+)!([A-Z]+)(\d+)", locator)
        if address:
            rows[("xlsx", address[1], int(address[3]))].append((column_index_from_string(address[2]), locator, value))
            continue
        cell = re.fullmatch(r"(.*:cell:|t:\d+:)(\d+):(\d+)", locator)
        if cell:
            rows[("table", cell[1], int(cell[2]))].append((int(cell[3]), locator, value))
            continue
        for i, line in enumerate(str(value).splitlines()):
            rows[("text", locator, i)].append((0, locator, line))
    return {key: sorted(entries) for key, entries in rows.items()}


def _computed(rows, pattern):
    for (typ, sheet, row), values in rows.items():
        if typ != "xlsx":
            continue
        labels = [loc for _, loc, v in values if isinstance(v, str) and len(v) < 95 and _has(pattern, v)
                  and not v.startswith("=") and not _has(_DEFER, v)]
        # Require an executable dependency, so a pasted number, '=0' or an
        # input assumption with no output consumer cannot satisfy a derived row.
        formulas = [loc for _, loc, v in values if isinstance(v, str) and v.startswith("=")
                    and _has(r"\$?[A-Z]{1,3}\$?[1-9]\d*\b", v)]
        if labels and formulas:
            return labels + formulas
        if labels:
            next_row=rows.get(("xlsx",sheet,row+1),[])
            if projected_subrow({col:val for col,_,val in values}, {col:val for col,_,val in next_row}):
                measured=[loc for _,loc,val in next_row if isinstance(val,str) and val.startswith("=")
                          and _has(r"\$?[A-Z]{1,3}\$?[1-9]\d*\b", val)]
                if measured:
                    return labels + [min(next_row)[1]] + measured
    return []


_CURRENCY_UNIT = re.compile(r"(?<![a-z0-9])(?P<currency>US\$|USD|EUR|GBP|CNY|RMB|[$€£])\s*(?:in\s+)?"
                            r"(?P<scale>billions?|millions?|thousands?|000s?|bn|mm|mn|[kbm])?(?![a-z0-9])",re.I)
_FEE_COLUMN = re.compile(r"^(?:(?:calculated|estimated)\s+)?(?:(?:closing|arrangement|up[- ]?front)\s+)?"
                         r"fee(?:\s+(?:rate|amount))?\s*(?:\([^()]*\)|%)?\s*$",re.I)
_ORDER_AMOUNT = re.compile(r"^(?:(?:indicative|final|lender|original)\s+)*(?:order|commitment)(?:\s+amount)?\b|"
                           r"^amount\b",re.I)
_NUMBER = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


def _money_unit(text):
    units=set()
    for match in _CURRENCY_UNIT.finditer(text):
        currency={"$":"USD","US$":"USD","€":"EUR","£":"GBP","RMB":"CNY"}.get(
            match["currency"].upper(),match["currency"].upper())
        scale=(match["scale"] or "").casefold()
        factor=(10**9 if scale.startswith("b") else 10**6 if scale.startswith("m")
                else 1000 if scale.startswith(("0","t","k")) else 1)
        units.add((currency,Decimal(factor)))
    return next(iter(units)) if len(units)==1 else None


def _computed_fee(rows,key,values,labels):
    """A percentage and facility are inputs. The separately calculated fee is an outcome."""
    if key[0]!="table" or key[2]<=1:
        return None
    header={c:str(v).strip() for c,_,v in rows.get(("table",key[1],1),[])}
    options=[c for c,v in header.items() if re.search(r"\b(?:case|option|scenario|alternative)\b",v,re.I)]
    if len(options)!=1:
        return None
    data={c:(str(v).strip(),loc) for c,loc,v in values}
    raw=data.get(options[0],("",None))[0]
    target=[l for l in labels if raw.casefold()==l.casefold() or
            re.fullmatch(r"(?:option|case|scenario)\s+"+re.escape(raw),l,re.I)]
    if len(target)!=1:
        return None
    cols={role:[] for role in ("facility","pct","fee")}
    for col,text in header.items():
        val,loc=data.get(col,("",None))
        if loc is None:
            continue
        unit=_money_unit(text)
        if re.search(r"\b(?:commitment|facility|principal)\b",text,re.I) and unit:
            cols["facility"].append((val,loc,unit))
        elif _FEE_COLUMN.fullmatch(text):
            if unit:
                cols["fee"].append((val,loc,unit))
            elif val.endswith("%") or re.search(r"%|\bpercent(?:age)?\b|\bper cent\b",text,re.I):
                cols["pct"].append((val,loc,None))
    if not all(len(v)==1 for v in cols.values()):
        return None
    principal,rate,amount=(cols[k][0] for k in ("facility","pct","fee"))
    if (principal[2][0]!=amount[2][0] or not _NUMBER.fullmatch(principal[0])
            or not _NUMBER.fullmatch(rate[0].removesuffix("%").strip()) or not _NUMBER.fullmatch(amount[0])):
        return None
    try:
        a=Decimal(principal[0].replace(",",""))*principal[2][1]
        r=Decimal(rate[0].rstrip("%").strip())/100
        fee=Decimal(amount[0].replace(",",""))*amount[2][1]
    except InvalidOperation:
        return None
    if a>0 and 0<r<1 and a*r==fee:
        return target[0],[data[options[0]][1],principal[1],rate[1],amount[1]]
    return None


def _alternative_hits(rows, labels, workbook):
    hits = {label: [] for label in labels}
    columns = defaultdict(list)
    for key, values in sorted(rows.items()):
        row = " | ".join(str(v) for _, _, v in values)
        history = {col: " ".join(columns[(key[:2], col)]) for col, _, _ in values}
        if key[0] != "text":
            for col, _, value in values:
                if isinstance(value, str) and len(value) < 110 and not value.startswith("="):
                    # Retain only headers, never use previous option rows as column labels.
                    is_scenario_header = sum(bool(_CASE.fullmatch(str(v).strip())) for _, _, v in values) >= 2
                    if (key[0] == "table" and key[2] == 1) or is_scenario_header:
                        columns[(key[:2], col)].append(value)
        if _has(_DEFER, row):
            continue
        if not workbook and (fee:=_computed_fee(rows,key,values,labels)):
            hits[fee[0]].extend(fee[1])
        for col, loc, value in values:
            context = history[col]
            for label in labels:
                pattern = r"\b" + re.escape(label).replace(r"\ ", r"\s*") + r"\b"
                in_row = _has(pattern, row)
                # One row may contain several options in independent columns.
                # Only the option next to this value, or its own column header,
                # can claim the value. A sibling option with no output stays open.
                if key[0] != "text":
                    preceding = [(c, v) for c, _, v in values if c <= col and _CASE.search(str(v))]
                    if len({m[0].casefold() for m in _CASE.finditer(row)}) > 1:
                        nearest = str(preceding[-1][1]) if preceding else ""
                        labels_near = {m[0].casefold() for m in _CASE.finditer(nearest)}
                        in_row = len(labels_near) == 1 and _has(pattern, nearest)
                in_column = _has(pattern, context)
                if not (in_row or in_column):
                    continue
                text = str(value)
                if workbook:
                    # Compare actual financing inputs, not case names alone.
                    if key[0] == "xlsx" and (isinstance(value, (int, float)) or text.startswith("=")) and (
                        _has(r"facilit|commitment|pricing|spread|interest|leverage", row + " " + context)):
                        hits[label].append(loc)
                else:
                    if not _has(_FINANCIAL_OUTCOME, row + " " + context):
                        continue
                    if key[0] == "text":
                        # Multiple options on one bullet need local, individual
                        # quantitative outcomes rather than a single shared fact.
                        clauses = re.split(r"[;\n]|(?=\b(?:Option|Case|Scenario)\s+(?:[A-Z]|\d+)\b)", text,
                                           flags=re.I)
                        text = next((c for c in clauses if _has(pattern, c)), "")
                    if not text or _has(pattern, text) and len(text.strip()) == len(label):
                        continue
                    text = re.sub(pattern, "", text, flags=re.I)
                    if _has(r"(?:USD|EUR|GBP|\$)\s*[\d,.]+|[\d,.]+\s*(?:x\b|%|bp[s]?\b|million|billion|mm\b|bn\b)", text) or (
                            key[0] == "table" and re.fullmatch(r"[\d,.]+", text.strip())):
                        hits[label].append(loc)
    return hits


def _money_cell(raw, header):
    # A bare scalar obtains both its currency and multiplier from its own
    # header. In a headerless order row, the business amount must be explicit.
    text=str(raw).strip()
    unit=_money_unit(header)
    values=quantities(text)
    if len(values)!=1:
        return None
    value=values[0]
    if _NUMBER.fullmatch(text):
        if not unit:
            return None
        return replace(value,dimension=unit[0],scale=unit[1])
    if value.dimension not in {"dollar","USD","EUR","GBP","CNY"}:
        return None
    explicit="USD" if value.dimension=="dollar" else value.dimension
    if unit and explicit!=unit[0]:
        return None
    return replace(value,dimension=explicit)


def _order_amount_candidates(rows,key,entries,account):
    header = rows.get((key[0],key[1],1),[]) if key[0]=="table" else []
    # A recognized order table must use its designated order value, even
    # when the note or intended hold repeats the expected amount.
    if any(_has(r"^(?:institution|account|lender)(?: name)?$",str(v).strip()) for _,_,v in header):
        columns=[(col,str(v)) for col,_,v in header if _ORDER_AMOUNT.match(str(v).strip())
                 and not _has(r"\b(?:hold|allocation|minimum|cap|status|qualifier|note)\b",str(v))]
        if len(columns)!=1:
            return []
        col,caption=columns[0]
        return [_money_cell(value,caption) for c,_,value in entries if c==col]
    # A prose line or a table without headings can use a single explicit
    # currency amount. Exclude holds, notes and other disjoint disclosures.
    out=[]
    for _,_,value in entries:
        text=str(value).replace(account["entity"],"")
        if not _has(r"\b(?:hold|allocation amount|minimum|threshold)\b",text):
            measured=_money_cell(text,"")
            if measured:
                out.append(measured)
    return out if len(out)==1 else []


def _account_rows(rows, accounts, *, require_amount=True):
    hits, missing = [], []
    for account in accounts:
        found = []
        for key, entries in rows.items():
            row = " | ".join(str(x[2]) for x in entries)
            if not _has(r"(?<!\w)" + re.escape(account["entity"]) + r"(?!\w)", row):
                continue
            status = account["status"]
            if not _has(re.escape(status), row) or (
                    _has(r"^final$", status) and _has(r"(?:no|not|awaiting|pending) (?:a )?final", row)):
                continue
            if require_amount and account.get("amount"):
                expected=_money_cell(account["amount"],account.get("amount_header",""))
                observed=_order_amount_candidates(rows,key,entries,account)
                if not expected or not any(q and amounts_match(expected,q) for q in observed):
                    continue
            found.extend(x[1] for x in entries)
        if found:
            hits.extend(found)
        else:
            missing.append(account["entity"])
    return hits, missing

def _annex_rows(rows):
    # Only an actual section heading establishes an internal-annex boundary.
    # A body instruction ("see internal annex") or quoted boilerplate is prose.
    active = False
    annex, body, headers = {}, {}, []
    for key, entries in rows.items():
        text = " ".join(str(v) for _, _, v in entries).strip()
        if key[0] == "text":
            heading = len(text) < 170 and _has(r"^(?:(?:strictly )?(?:private|confidential|internal)(?:[- ]only| use only| reference)?[ :|–—-]*)?(?:annex(?:ure)?|appendix)\b|^internal (?:reference )?(?:syndicate |order )?(?:annex|appendix)\b", text)
            if heading and not _has(r"\b(?:to be|will be|is required|requested|pending|not (?:provided|included)|see above)\b", text):
                active = _has(r"\binternal(?:[- ]only| use only| reference)?\b", text)
                if active:
                    headers.append(entries[0][1])
                continue
            if active and _has(r"^(?:signature blocks?|signatures?|execution page|terms and conditions)$", text):
                active = False
        (annex if active else body)[key] = entries
    return headers, annex, body


_GROUP = r"\b(?:platform|group|consolidated|hold(?:ing company|co))\b"
_CAPACITY_REQUEST = r"\b(?:capacity|ability) to (?:service|repay|meet) (?:its |the )?debt\b|\bdebt[ -]servic(?:e|ing) capacity\b"
_CASH_BASIS = r"\b(?:CFADS|cash (?:flow )?available (?:for|to service) debt(?: service)?)\b"
_PERIOD = re.compile(r"\bFY\s*(20\d\d)(?:[AEF])?\b", re.I)


def _metric_witness(source, quote):
    start = source["payload"]["text"].index(quote)
    return {"source_id":source["id"],"locator":source["payload"]["locator"],
            "quote":quote,"start":start,"end":start+len(quote)}


def _platform_cash_inputs(sources):
    """Release-only operating bases. Do not promote a segment total to group cash."""
    candidates=[]
    for source in sources:
        p=source["payload"]
        text=p["text"]
        if "/old/" in p["locator"].lower() or not _has(_GROUP,text):
            continue
        lines=list(_lines(source))
        for line in lines:
            statement=re.sub(r"^\[(?:paragraph|slide) \d+\]\s*","",line["quote"])
            if not _has(_GROUP,statement) or not _has(_CASH_BASIS,statement) or _has(_DEFER,statement):
                continue
            years=_PERIOD.findall(statement)
            if _has(r"\b(?:subsidiary|segment|OpCo|excluded|unverified|not available)\b",statement):
                continue
            money=[q for q in quantities(statement) if q.dimension in {"USD","EUR","GBP","CNY","dollar"}]
            if len(years)==len(money)==1:
                q=money[0]
                candidates.append((p["stage"],{"metric":"cash","year":years[0],"currency":"USD" if q.dimension=="dollar" else q.dimension,
                                              "amount":str(q.canonical),"sources":[line]}))
        # A dated aggregate EBITDA can support a labelled partial proxy.
        # Only accept an actual released aggregate; exclude subsidiary rows.
        if not p["locator"].lower().endswith(".xlsx"):
            continue
        sheet=None
        all_slots, row_info, columns, periods, scope={}, {}, {}, {}, {}
        for line in lines:
            raw=line["quote"]
            if m:=re.fullmatch(r"\[sheet (.+)\]",raw):
                sheet=m[1]
                continue
            if not sheet:
                continue
            cells=dict(re.findall(r"(?:^|\s\|\s)([A-Z]+\d+)=(.*?)(?=\s\|\s|$)",raw))
            for address,value in cells.items():
                all_slots[sheet+"!"+address]=value
            if not cells:
                continue
            label=str(next(iter(cells.values())))
            if (len(cells)==1 and _has(_GROUP,label) and not periods.get(sheet)
                    and not re.search(r"[=|]",label)):
                scope[sheet]=line
            dated={re.match(r"[A-Z]+",a)[0]:_PERIOD.fullmatch(v).group(1) for a,v in cells.items() if _PERIOD.fullmatch(v)}
            if len(dated)>=1:
                periods[sheet]=dated
                columns[sheet]=line
            if not periods.get(sheet) or sheet not in scope:
                continue
            if re.fullmatch(r"(?:adjusted |underlying )?EBITDA|(?:platform|consolidated|group) (?:adjusted )?EBITDA",label,re.I):
                metric="earnings"
            elif re.fullmatch(_CASH_BASIS,label,re.I):
                metric="cash"
            else:
                continue
            for address in list(cells)[1:]:
                col=re.match(r"[A-Z]+",address)[0]
                if col in periods[sheet]:
                    row_info[sheet+"!"+address]=(metric,periods[sheet][col],[scope[sheet],columns[sheet],line])
        # Closed arithmetic uses the project's non-executing native evaluator.
        # An external workbook, unverified cached result or unresolved reference
        # does not supply group earnings.
        units=[(line,_money_unit(line["quote"])) for line in lines if _has(r"all (?:figures|amounts) (?:are )?in",line["quote"])]
        if not units or len({u for _,u in units})!=1 or not units[0][1]:
            continue
        name_map={k.split("!",1)[0].casefold():k.split("!",1)[0] for k in all_slots}
        from .native_finance import NativeValues
        calc=NativeValues(all_slots,name_map,None)
        for loc,(metric,year,refs) in row_info.items():
            value=calc.get(loc)
            if value is not None:
                currency,factor=units[0][1]
                candidates.append((p["stage"],{"metric":metric,"year":year,"currency":currency,"amount":str(value*factor),
                                  "sources":[units[0][0],*refs],"native_source":loc}))
    return candidates


def _platform_earnings_proxy(sources):
    """Reconstruct reported historic group earnings only when all segments and corporate overhead have evidence."""
    overhead=defaultdict(list)
    for source in sources:
        p=source["payload"]
        if "/old/" in p["locator"].lower() or not _has(r"quality\s+of\s+earnings",p["text"]):
            continue
        groups=list(re.finditer(r"unallocated\s+(?:corporate|group)\s+overhead\s+was\s+([^.;]{8,200})",p["text"],re.I))
        # An unlabelled paragraph may use the report's monetary reporting unit
        # only when all of its financial table unit declarations agree.
        units={_money_unit(m[0]) for m in re.finditer(r"\((?:USD|EUR|GBP|CNY|\$)\s*(?:000s?|thousands?|millions?|bn|mm)\)",p["text"],re.I)}
        if len(units)!=1 or None in units:
            continue
        curr,factor=next(iter(units))
        unitline=next(q for q in _lines(source) if _money_unit(q["quote"])==(curr,factor))
        for m in groups:
            for number,year in re.findall(r"(\d[\d,]*(?:\.\d+)?)\s+in\s+FY\s*(20\d\d)",m[1],re.I):
                overhead[year].append((curr,Decimal(number.replace(",",""))*factor,
                    [_metric_witness(source,m[0]),unitline]))
    result=[]
    for source in sources:
        p=source["payload"]
        if "/old/" in p["locator"].lower() or not p["locator"].lower().endswith(".pptx"):
            continue
        body=p["text"]
        if not _has(r"segment results are presented before unallocated (?:group|corporate) costs",body):
            continue
        lines=list(_lines(source))
        for i,heading in enumerate(lines):
            if not re.fullmatch(r"segment (?:adjusted )?EBITDA(?: and margin)?",heading["quote"],re.I) or i+1>=len(lines):
                continue
            header=lines[i+1]
            cells=header["quote"].split("|")
            years=[_PERIOD.fullmatch(x.strip()) for x in cells[1:]]
            if not years or not all(years):
                continue
            segment_rows=[]
            for row in lines[i+2:]:
                fields=[c.strip() for c in row["quote"].split("|")]
                if len(fields)!=len(cells) or not all(_NUMBER.fullmatch(v) for v in fields[1:]):
                    break
                segment_rows.append((fields,row))
            perimeter=next((q for q in lines if _has(r"\b(?:three|four|two|\d+) reportable segments\b",q["quote"])),None)
            names={r[0][0].lower() for r in segment_rows}
            count=({"two":2,"three":3,"four":4} | {str(n):n for n in range(2,13)})
            size=next((n for word,n in count.items() if perimeter and _has(r"\b"+word+" reportable segments",perimeter["quote"])),None)
            denominators=[q for q in lines if _has(r"segment adjusted EBITDA\s*\(",q["quote"])]
            units={_money_unit(q["quote"]) for q in denominators}
            if len(denominators)<len(names) or len(units)!=1 or None in units or len(names)!=len(segment_rows) or len(names)!=size:
                continue
            currency,scale=next(iter(units))
            for col,year in enumerate(years,1):
                y=year[1]
                costs=overhead[y]
                if len({(u,a) for u,a,_ in costs})!=1 or costs[0][0]!=currency:
                    continue
                value=sum(Decimal(r[0][col].replace(",",""))*scale for r in segment_rows)-costs[0][1]
                refs=[heading,perimeter,header,denominators[0],*(r[1] for r in segment_rows),*costs[0][2]]
                result.append((p["stage"],{"metric":"earnings","year":y,"currency":currency,"amount":str(value),"sources":refs}))
    return result


def _proposed_payments(sources):
    for source in reversed(sources):
        text=source["payload"]["text"]
        if not _has(r"term.sheet|loan.agreement|credit.agreement",source["payload"]["locator"]) or not _has(_GROUP,text):
            continue
        lines=list(_lines(source))
        unit=next((q for q in lines if _has(r"all amounts.*(?:thousands?|millions?|\$000)",q["quote"])),None)
        parsed=_money_unit(unit["quote"]) if unit else None
        if not parsed:
            continue
        current={}
        for q in lines:
            match=re.fullmatch(r"\[table (\d+) row \d+\] (.*)",q["quote"])
            if not match:
                continue
            values=[c.strip() for c in match[2].split("|")]
            table=current.setdefault(match[1],{})
            if all(_CASE.fullmatch(x) for x in values[1:]) and len(values)>1:
                table['labels']=(q,values[1:])
            if re.fullmatch(r"total (?:commitment|facility)|principal amount",values[0],re.I):
                table['amount']=(q,values[1:])
            if re.fullmatch(r"(?:credit )?spread(?: over base rate)?",values[0],re.I):
                table['spread']=(q,values[1:])
            if re.fullmatch(r"(?:scheduled|annual) amorti[sz]ation",values[0],re.I):
                table['amort']=(q,values[1:])
        result=[]
        for table in current.values():
            if set(table)!={"labels","amount","spread","amort"} or len({len(v[1]) for v in table.values()})!=1:
                continue
            currency,factor=parsed
            for i,label in enumerate(table["labels"][1]):
                amount=table["amount"][1][i]
                if not _NUMBER.fullmatch(amount):
                    continue
                principal=Decimal(amount.replace(",",""))*factor
                spread=quantities(table["spread"][1][i])
                amort=quantities(table["amort"][1][i])
                if not re.search(r"per annum|annual(?:ly)?|p\.?a\.?",table["amort"][1][i]+" "+table["amort"][0]["quote"].split("|")[0],re.I):
                    continue
                if not all(len(q)==1 and q[0].dimension=="rate" and q[0].canonical>=0 for q in (spread,amort)):
                    continue
                result.append({"scenario":label,"currency":currency,"principal":str(principal),
                               "spread":str(spread[0].canonical),"amort":str(amort[0].canonical),
                               "sources":[unit,*(t[0] for t in table.values())]})
        if result:
            return result
    return []


def _capacity_evidence(sources):
    data=_platform_cash_inputs(sources)+_platform_earnings_proxy(sources)
    bases=[]
    for key in sorted({(x["year"],x["metric"],x["currency"]) for _,x in data}):
        matched=[(st,x) for st,x in data if (x["year"],x["metric"],x["currency"])==key]
        latest=max(st for st,_ in matched)
        chosen=[x for st,x in matched if st==latest]
        if len({Decimal(x["amount"]) for x in chosen})==1:
            bases.append(chosen[0])
    return bases,_proposed_payments(sources)


def _direct_capacity(sources):
    result=[]
    for node in sources:
        if "/old/" in node["payload"]["locator"].lower():
            continue
        for line in _lines(node):
            quote=line["quote"]
            if _has(r"\b(?:subsidiary|segment|OpCo|historical|excluded|unverified)\b",quote):
                continue
            if not (_has(_GROUP,quote) and _has(r"\b(?:cash debt service|scheduled debt service|total debt service|annual debt service)\b",quote)):
                continue
            years=_PERIOD.findall(quote)
            matches=list(_CASE.finditer(quote))
            money=[q for q in quantities(quote) if q.dimension in {"USD","EUR","GBP","CNY","dollar"}]
            if len(years)==len(matches)==len(money)==1 and not _has(_DEFER,quote):
                q=money[0]
                if q.canonical>0:
                    result.append({"stage":node["payload"]["stage"],"scenario":matches[0][0],"year":years[0],
                                   "currency":"USD" if q.dimension=="dollar" else q.dimension,
                                   "debt_service":str(q.canonical),"sources":[line]})
    current=[]
    for key in sorted({(x["scenario"].lower(),x["year"],x["currency"]) for x in result}):
        candidates=[t for t in result if (t["scenario"].lower(),t["year"],t["currency"])==key]
        latest=max(t["stage"] for t in candidates)
        applicable=[t for t in candidates if t["stage"]==latest]
        if len({Decimal(t["debt_service"]) for t in applicable})==1:
            current.append(applicable[0])
    return current


def _capacity_check(detail,rows):
    """Read native display rows, preserving the slide or table-row consumer."""
    bases,terms=detail["bases"],detail["terms"]
    supported=[]
    for base in bases:
        for term in terms:
            if base["currency"]!=term["currency"] or term.get("year",base["year"])!=base["year"]:
                continue
            if "debt_service" in term and base["metric"]!="cash":
                continue
            supported.append((base,term))
    groups=defaultdict(list)
    for (kind,key,index),entries in rows.items():
        if kind=="text" and (slide:=re.match(r"(slide:\d+):",key)):
            groups[("slide",slide[1])].extend(entries)
        elif kind=="table":
            groups[(kind,key,index)].extend(entries)
    for (kind,key,index),entries in rows.items():
        if kind!="table" or index<=1:
            continue
        header={c:str(v) for c,_,v in rows.get((kind,key,1),[])}
        expanded=[]
        for col,loc,val in entries:
            heading=header.get(col,"")
            text=str(val).strip()
            if heading:
                text=heading+" "+text
                qty=quantities(str(val))
                # A bare numeric native table cell can obtain a denomination
                # only from its own explicit current column header.
                if len(qty)==1 and qty[0].dimension is None and _NUMBER.fullmatch(str(val).strip()):
                    unit=_money_unit(heading)
                    if unit:
                        text=heading+" "+unit[0]+" "+str(qty[0].value*unit[1])
                    elif _has(r"[(]x[)]",heading):
                        text=heading+" "+str(val)+"x"
            expanded.append((col,loc,text))
        groups[(kind,key,index)]=expanded
    for where,items in groups.items():
        joined=" | ".join(str(v) for _,_,v in items)
        # A quantitative native consumer is tied to one case and period. A
        # multi-column comparison can instead express each result on its own
        # row, or in an explicit statement within a multi-case slide.
        options=list(dict.fromkeys(m[0].casefold() for m in _CASE.finditer(joined)))
        years=set(_PERIOD.findall(joined))
        if len(options)!=1 or len(years)!=1 or not _has(_GROUP,joined):
            continue
        pieces=[(loc,s.strip()) for _,loc,raw in items for s in re.split(r"[;\n|]|(?<=[a-z0-9x.])\.\s+(?=[A-Z])",str(raw)) if s.strip()]
        def measured(pattern,value,dimension):
            for loc,part in pieces:
                if not _has(pattern,part) or _has(_DEFER,part) or _has(r"\b(?:target|threshold|minimum covenant|not verified|not calculated|do not use|wrong|withdrawn|superseded)\b",part):
                    continue
                observed=[q for q in quantities(part) if q.dimension==dimension or dimension=="USD" and q.dimension=="dollar"]
                if len(observed)==1:
                    q=observed[0]
                    # Use native precision, bounded to at most half a unit
                    # of the displayed decimal (e.g. 4.74x or USD9.1m).
                    rounding=Decimal(10)**q.value.as_tuple().exponent*q.scale/2
                    if abs(q.canonical-value)<=rounding:
                        return loc
            return None
        for base,term in supported:
            if options!=[term["scenario"].casefold()] or years!={base["year"]}:
                continue
            currency=base["currency"]
            available=Decimal(base["amount"])
            metric=_CASH_BASIS if base["metric"]=="cash" else r"\b(?:adjusted )?EBITDA\b"
            basis=measured(metric,available,currency)
            if not basis:
                continue
            # No subsidiary-only denominator or subsidiary cashflow becomes
            # platform cash merely because another shape names the platform.
            basis_text=next((part for loc,part in pieces if loc==basis and _has(metric,part) and
                                not _has(r"\b(?:subsidiary|segment|OpCo)\b",part)),None)
            if not basis_text:
                continue
            direct="debt_service" in term
            if direct:
                service=Decimal(term["debt_service"])
                payment=measured(r"\b(?:scheduled|total|annual|cash) debt service\b",service,currency)
                outcome=measured(r"\b(?:DSCR|debt.service coverage)\b",available/service,"multiple") or measured(
                    r"\b(?:surplus|shortfall|cash after debt service)\b",available-service,currency)
                evidence=[basis,payment,outcome]
            else:
                # At an undisclosed base rate the spread and scheduled
                # principal give only a lower bound on annual obligations.
                # EBITDA less that floor is an upper bound, never CFADS/DSCR.
                principal=Decimal(term["principal"])
                service=principal*(Decimal(term["spread"])+Decimal(term["amort"]))
                base_claim = r"(?:base|benchmark)[ -]rate\s*(?:is|at|of|:|=|assumed at)?\s*\d[\d.]*\s*%"
                if _has(base_claim,joined):
                    continue
                if not service or not _has(r"before (?:the )?base.rate|exclud(?:es|ing) (?:the )?base.rate|base.rate (?:excluded|unconfirmed|unknown|unavailable)",joined):
                    continue
                if not _has(r"\b(?:annual|annualised|annualized|first.year)\b",joined) or not _has(r"\b(?:assuming|assume[ds]?|pro forma|illustrative|full[ -]draw|fully drawn)\b",joined):
                    continue
                if _has(r"\b(?:DSCR|debt.service coverage)\s*(?:is|of|=|:|at)\s*\d",joined):
                    continue
                if base["metric"]=="earnings" and not (_has(r"\b(?:upper bound|ceiling|proxy|before cash)\b",joined)
                      and _has(r"\b(?:tax|taxes|capex|capital expenditure|working capital)\b",joined)):
                    continue
                payment=measured(r"debt.service floor|minimum annual debt service|annual (?:interest and principal|spread and amorti[sz]ation)|spread.plus.amorti[sz]ation",service,currency)
                outcome=measured(r"ceiling|upper bound|headroom (?:proxy|before)|residual (?:proxy|before)",available-service,currency)
                # Explicitly label a ratio over a floor as an upper bound.
                outcome=outcome or measured(r"(?:coverage|ratio) (?:ceiling|upper bound)",available/service,"multiple")
                evidence=[basis,payment,outcome]
            if all(evidence):
                return {"state":"delivered","observed_consumer":str(where),"locators":list(dict.fromkeys(evidence)),
                        "scenario":term["scenario"],"fiscal_year":base["year"],"metric":base["metric"],
                        "method":"cash / scheduled debt service or cash less service" if direct else "group basis less (or divided by) annual spread plus amortization, full draw; base rate excluded",
                        "source_inputs":[*base["sources"],*term["sources"]]}
    return {"state":"executable_but_missing" if supported else "insufficient_evidence","locators":[],
            "executable_routes":[{"fiscal_year":b["year"],"scenario":t["scenario"],"metric":b["metric"],
                "service_basis":"direct" if "debt_service" in t else "full-draw spread plus amortization floor"} for b,t in supported]}


def _sweep_terms(dag, stage, path, obligation):
    """Only the applicable, released contract can exempt a funded case.

    A primitive 0 in candidate bytes cannot remove a release obligation.
    Contracts made before this check can still contribute their original
    case-specific text. Conflicting directions remain unresolved.
    """
    released = [q for q in obligation["evidence"] if q.get("locator","").startswith("source/")]
    ids = {q["source_id"] for q in released}
    states = defaultdict(set)
    for evidence in released:
        quote = evidence["quote"]
        labels = re.findall(r"\b(?:case|scenario|option)\s*[-:#]?\s*\d+\b", quote, re.I)
        if len(labels) != 1:
            continue
        case = scenario(labels[0])
        # Only unqualified directions count. A release can describe an old
        # rate, a possible future amendment or an exception in the same line.
        if re.search(r"\b(?:if|unless|previously|historical|prior|proposed|may|except|could|subject to|consider|drafting|formerly)\b", quote, re.I):
            continue
        metric = r"(?:excess cash flow|cash|ecf)[ -]sweep"
        label = re.escape(labels[0])
        no_sweep = r"(?:no " + metric + "|" + metric + r" (?:is )?not applicable)"
        if re.fullmatch(r"\s*(?:" + label + r"\s*[:=–—-]?\s*(?:has |requires )?" + no_sweep +
                        "|" + no_sweep + r" (?:applies )?(?:for|to|under) " + label + r")[.! ]*", quote, re.I):
            states[case].add("exempt")
        if re.search(r"\b" + metric + r" (?:is )?(?:mandatory|required|applies)\b", quote, re.I):
            states[case].add("required")
        percent = re.search(r"\b" + metric + r"\s*(?:(?:rate|of|is|applies|at)\s+|[():=]\s*)*([0-9]+(?:\.[0-9]+)?)\s*%", quote, re.I)
        if percent:
            if Decimal(percent[1]) > 0:
                states[case].add("required")
            elif re.fullmatch(r"\s*" + label + r"\s*[:=–—-]?\s*" + metric +
                              r"\s*(?:(?:rate|of|is|at)\s+|[():=]\s*)*0+(?:\.0+)?\s*%[.! ]*",quote,re.I):
                states[case].add("exempt")
    contracts=[q for q in dag.nodes("artifact_contract") if q["payload"].get("path")==path and q["payload"].get("stage")==stage]
    if contracts:
        date = contracts[-1]["payload"].get("valid_at")
        active={n["id"]:n["payload"] for n in dag.claim_at(valid_at=date)} if hasattr(dag,"claim_at") else {}
        for rule in contracts[-1]["payload"].get("semantic_rules",[]):
            claim=active.get(rule["claim_id"],{})
            if (rule.get("field")!="sweep_pct" or claim.get("field")!="sweep_pct" or claim.get("source_id") not in ids or
                    not str(claim.get("locator","")).startswith("source/")):
                continue
            value=claim.get("value")
            entity=scenario(claim.get("scope",{}).get("scenario") or claim.get("entity"))
            if (entity and entity==scenario(rule.get("entity")) and value==rule.get("expected")
                    and claim.get("source_stage",stage)<=stage and type(value) in (int,float) and value>=0):
                states[entity].add("exempt" if value==0 else "required")
    return {case for case,items in states.items() if items=={"exempt"}}, {case for case,items in states.items() if len(items)>1}


def check_obligations(dag, stage, path, slots, *, inspection=None):
    checks = []
    selected = [n for n in dag.nodes("task_obligation") if n["payload"]["stage"] == stage and path in n["payload"]["paths"]]
    rows = _native_groups(slots) if selected else {}
    for n in selected:
        p = n["payload"]
        kind, detail = p["kind"], p["detail"]
        status, locators, observation, hint = "satisfied", [], "", ""
        if kind == "task_request":
            status = "review_required"
            observation = "Released task recorded; its qualitative requirements need a source-grounded review."
        elif kind in {"quantified_alternatives", "model_alternatives"}:
            hits = _alternative_hits(rows, detail["alternatives"], kind == "model_alternatives")
            missing = [label for label, found in hits.items() if not found]
            locators = [loc for found in hits.values() for loc in found]
            if missing:
                status = "missing"
                observation = "No located quantitative comparison for " + ", ".join(missing)
            else:
                observation = "Located a quantitative comparison for each released financing alternative."
            hint = "Use the released terms for each alternative. Show the decision-useful financial outcome, its inputs and assumptions; a request for future analysis does not produce the comparison."
        elif kind == "platform_debt_capacity":
            capacity_result=_capacity_check(detail,rows)
            locators=capacity_result["locators"]
            if capacity_result["state"]=="delivered":
                observation="Located a source-grounded platform-level calculation in the lender document."
            elif capacity_result["state"]=="insufficient_evidence":
                status,observation="unmapped","Released inputs do not establish an executable scope, period and term. Identify the missing evidence."
            else:
                status,observation="missing","A period- and financing-specific platform assessment can be calculated but no supported native consumer was verified."
            hint="Use a supported period, platform cash or qualified earnings proxy, and one applicable proposed financing basis. Explain any missing rate or cash deductions; do not invent a rate or state partial coverage as DSCR."
        elif kind == "debt_service_schedule":
            if inspection is None:
                inspection = inspect_workbook(slots)
            for schedule in inspection["schedules"]:
                locators.extend([schedule["opening"], schedule["closing"]])
            if not locators:
                status, observation = "unmapped", "No multi-period debt schedule with paired opening and closing balances."
            hint = "Model beginning principal, payments and closing principal for each forecast period, referencing the actual financing inputs."
        elif kind=="cash_sweep":
            if inspection is None:
                inspection=inspect_workbook(slots)
            exemptions, conflicts = _sweep_terms(dag,stage,path,p)
            routes=[dict(r) for r in inspection["sweep_projections"]]
            for rec in routes:
                if rec["scenario"] in exemptions and rec["basis"]=="scheduled_scenario" and rec["status"] in {"unconsumed","other_consumers_only"}:
                    rec.update(basis="comparison_only",status="exempt_facility_estimate_not_booked")
            intended=[rec for rec in routes if rec["basis"]=="scheduled_scenario"]
            def funding_case(sched):
                return next(iter(sched["funding"])) if len(sched["funding"])==1 else sched["entity"]
            applicable=[s for s in inspection["schedules"] if funding_case(s) not in exemptions]
            expected={pair["closing"] for sched in applicable for pair in sched["period_balances"].values()}
            exempt_debits = [r for r in routes if r["scenario"] in exemptions and r["status"]=="deducted" and r["amount"]!=0]
            ambiguous = any(r["basis"]=="unresolved" or
                            (r["scenario"] in exemptions and r["status"]=="dependency_not_verified_as_debit")
                            for r in routes)
            all_exempt=bool(inspection["schedules"]) and not applicable
            row_counts={}
            for rec in intended:
                row_counts.setdefault(rec["ending_locator"],[]).append(rec)
            covered={end for end,items in row_counts.items() if len(items)==1 and items[0]["status"]=="deducted"}
            gaps=sorted(expected-covered)
            locators=[r["amount_locator"] for r in routes]+sorted(expected)
            if conflicts & {funding_case(s) for s in inspection["schedules"]}:
                status,observation="unmapped","Conflicting released sweep terms for a mapped funding scenario."
            elif exempt_debits:
                status,observation="unmapped","Ending principal consumes a sweep despite an applicable released no-sweep term."
            elif all_exempt and not ambiguous:
                observation="All mapped facilities have explicit released no-sweep terms."
            elif not routes:
                status,observation="missing","No period sweep amounts and consumer routes were located."
            elif gaps or not expected or ambiguous:
                status="unmapped"
                observation=f"Verified deductions at {len(covered)}/{len(expected)} scheduled ending balances. Standalone comparison estimates are preserved."
            else:
                observation=f"Verified a sweep deduction at all {len(expected)} scheduled ending balances."
            hint="Inspect the per-period sweep routes. Trace the required cash payment to the same scenario's ending principal; keep independent estimates and historical comparisons."
        elif kind in {*_OPERATING, *_DEBT, "cash_available_for_debt_service"}:
            pattern = (_OPERATING | _DEBT).get(kind, _CASH)
            if kind == "cash_tax":
                pattern = r"cash tax|tax(?:es)? (?:paid|payable)|^\s*(?:income )?tax(?:es)?\s*$"
            if kind == "scheduled_amortization":
                pattern = r"amorti[sz]ation|scheduled (?:amort\.?|repayment|principal)"
            locators = _computed(rows, pattern)
            if kind in _DEBT and locators:
                if inspection is None:
                    inspection = inspect_workbook(slots)
                key = "scheduled_repayment"
                if not any(c.get(key) for c in inspection["coverage"].values()):
                    status = "unmapped"
                    observation = "Payment row could not be mapped to a native debt schedule."
            if not locators:
                status = "missing"
                observation = "No labelled, formula-driven output for " + kind.replace("_", " ")
            elif status == "satisfied":
                observation = "Located a labelled derived row and referenced formulas."
            hint = "Build or restore the operating/debt schedule using evidenced inputs and link it to the capacity analysis."
        elif kind == "fillable_commitment":
            for group in rows.values():
                row = " | ".join(str(x[2]) for x in group)
                if _has(r"(?:commitment|committed|subscription)(?: amount)?|amount (?:of|committed)", row) and _has(
                    r"_{2,}|\[\s*(?:\u25cf|amount|insert[^\]]*|lender[^\]]*|\s*)\s*\]|\u25cf|\$\s*[.…]{2,}", row):
                    locators.extend(x[1] for x in group)
            if not locators:
                status = "missing"
                observation = "No fillable commitment amount for the individual signing lender."
            hint = "Provide an individual commitment-amount field alongside the template's lender terms; retain the source-backed minimum."
        elif kind == "order_annex":
            if not detail["accounts"]:
                status = "unmapped"
                observation = "Referenced syndicate lacks an identifiable released order table."
            else:
                headers, annex, body = _annex_rows(rows)
                locators, missing = _account_rows(annex, detail["accounts"])
                leaked, _ = _account_rows(body, detail["accounts"], require_amount=False)
                locators = headers + locators
                if not headers:
                    status, observation = "missing", "No separately designated internal reference annex."
                elif leaked:
                    status = "missing"
                    observation = "Indicative account order/status rows also occur outside the internal annex: " + ", ".join(leaked)
                elif missing:
                    status = "missing"
                    observation = "Order annex lacks institution, status or amount on the same row for: " + ", ".join(missing)
            hint = "Use the released indicative order table to itemize final orders and pending credit accounts in the internal annex."
        else:
            status, observation = "unmapped", "No native check available for the recorded requirement."
        source = p["evidence"][0]
        checks.append({"obligation_id": n["id"], "kind": kind, "status": status, "purpose": p["purpose"],
                       "source_id": source["source_id"], "quote": source["quote"], "locators": list(dict.fromkeys(locators)),
                       "observation": observation or "Required native structure located.", "repair_hint": hint,
                       "sources": list(dict.fromkeys(e["source_id"] for e in p["evidence"]))})
        if kind=="platform_debt_capacity":
            checks[-1]["capacity_result"]=capacity_result
        if kind=="cash_sweep":
            checks[-1]["sweep_routes"]=[{k:r[k] for k in ("scenario","year","amount_locator","amount","ending_locator","ending_candidates",
                                "matched_schedule","basis","status","used_by")} for r in routes]
            checks[-1]["unverified_ending_balances"]=gaps
            checks[-1]["released_no_sweep_cases"]=sorted(exemptions)
            checks[-1]["conflicting_sweep_cases"]=sorted(conflicts)
    from .work_items import WorkItems
    request_ids = list(dict.fromkeys(n["payload"]["request_id"] for n in selected
                                    if n["payload"].get("request_id")))
    work_items = [item for request_id in request_ids if dag.get(request_id)["kind"] == "stage"
                  for item in WorkItems(dag).list(request_id)
                  if path in item["paths"] and not item.get("superseded_mapping")]
    return {"checks": checks, "blocking": [c for c in checks if c["status"] in {"missing", "unmapped"}],
            "work_items": work_items}

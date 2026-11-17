"""Financial invariants measured from native cells and actual formula operands.

The guard stores observations together with the hash of the inspected file. Rules
here have no case, stage, answer, path or cell-coordinate constants.
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from openpyxl.formula import Tokenizer
from openpyxl.formula.tokenizer import TokenizerError
from openpyxl.utils import column_index_from_string, get_column_letter, range_boundaries


SCENARIO = re.compile(r"\b(?:case|scenario|option)\s*[-:#]?\s*(\d+)\b", re.I)
ADDRESS = re.compile(r"[A-Za-z]{1,3}[1-9]\d{0,6}")
PERIOD = re.compile(r"(?:FY\s*)?(?:19|20)\d\d\s*(?:[AEF]|actual|forecast|budget)?", re.I)
FACILITY = re.compile(r"\b(?:(?:senior|total)\s+(?:senior\s+)?(?:facility|commitment)|facility\s+(?:size|amount)|(?:loan|debt)\s+(?:principal|size|amount))\b", re.I)
SPREAD = re.compile(r"\b(?:credit|senior|pricing|interest)\s+spread\b|\bspread\b", re.I)
OPENING = re.compile(r"\b(?:opening|beginning|start[- ]of[- ](?:year|period))\b", re.I)
CLOSING = re.compile(r"\b(?:closing|ending|end[- ]of[- ](?:year|period))\b", re.I)
BALANCE = re.compile(r"\b(?:debt|balance|principal|loan|borrowings?)\b", re.I)
PAYMENT = re.compile(r"\b(?:scheduled\s+(?:amort|principal|repayment)|mandatory amorti[sz]ation)\b|\bscheduled amorti[sz]ation\b", re.I)
ILLUSTRATIVE = re.compile(r"\billustrative\b|\bstandalone\b|\bsensitivit(?:y|ies)\b|\bcomparison\b|\bnot applied\b", re.I)
SWEEP = re.compile(r"\b(?:cash|ecf|excess cash flow)\s+sweep\b", re.I)


def scenario(text):
    match = SCENARIO.search(str(text))
    return f"Case {int(match[1])}" if match else None


def number(value):
    try:
        result = Decimal(str(value).strip().lstrip("=").replace(",", ""))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _ref(sheet, cell):
    return f"{sheet}!{cell.replace('$', '').upper()}"


def _references(value, sheet, sheets):
    if "!" in value:
        prefix, value = value.rsplit("!", 1)
        canonical = sheets.get(prefix.strip("'").replace("''", "'").casefold())
        if canonical is None:
            return None
        sheet = canonical
    value = value.replace("$", "").upper()
    ref = value.split(":")
    if len(ref) > 2 or any(not ADDRESS.fullmatch(i) for i in ref):
        return None
    if len(ref) == 1:
        return [_ref(sheet, value)]
    left, top, right, bottom = range_boundaries(value)
    if (right - left + 1) * (bottom - top + 1) > 4096 or right < left or bottom < top:
        return None
    return [_ref(sheet, f"{get_column_letter(c)}{r}") for c in range(left, right + 1)
            for r in range(top, bottom + 1)]


def _formula(value, sheet, sheets):
    """Return real precedents and a linear signature for provable identities.

    Unresolved names, external books, dynamic references and unsupported
    functions never constitute proof of a roll-forward identity.
    """
    if not isinstance(value, str) or not value.startswith("="):
        return [], None, False
    try:
        tokens = Tokenizer(value).items
    except (TokenizerError, IndexError, ValueError):
        return [], None, False
    deps, symbols, expr = [], {}, []
    complete = True
    linear = True
    for token in tokens:
        if token.type in {"WSPACE","WHITE-SPACE"}:
            # Excel's intersection operator is a space between two operands.
            expr.append(" ")
        elif token.type == "OPERAND" and token.subtype == "RANGE":
            refs = _references(token.value, sheet, sheets)
            if refs is None:
                complete = False
                linear = False
                continue
            deps.extend(refs)
            variables = []
            for ref in refs:
                symbols.setdefault(ref, f"x{len(symbols)}")
                variables.append(symbols[ref])
            expr.append(",".join(variables))
        elif token.type == "FUNC":
            if token.subtype == "OPEN" and token.value.upper() != "SUM(":
                linear = False
                if token.value.upper() in {"INDIRECT(", "OFFSET(", "INDEX(", "CHOOSE("}:
                    complete = False
            expr.append(token.value.upper())
        elif token.type == "PAREN" or token.value in {"+", "-", "*", "/", ","} or token.subtype == "NUMBER":
            expr.append(token.value)
        else:
            linear = False
    signature = None
    if linear and complete and len(tokens) < 512:
        try:
            tree = ast.parse("".join(expr), mode="eval").body
            signature = _linear(tree, {v: k for k, v in symbols.items()})
        except (SyntaxError, ValueError, ArithmeticError, RecursionError):
            pass
    return list(dict.fromkeys(deps)), signature, complete


class NativeValues:
    """Evaluate the small audited numeric formula language without running Excel code.

    Unsupported formulas, nondeterminism, circular dependencies, and missing
    inputs return unknown. Cached results may only be supplied by the caller
    after recalculating these exact bytes.
    """
    functions = {"SUM":sum, "MIN":min, "MAX":max, "AVERAGE":lambda x:sum(x)/len(x)}

    def __init__(self, slots, sheets, verified_cache):
        self.slots = slots
        self.sheets = sheets
        self.memo = {}
        self.visiting = set()
        self.cache = verified_cache or {}

    def get(self, loc):
        if loc in self.memo:
            return self.memo[loc]
        if loc in self.visiting or len(self.visiting) > 80:
            return None
        value = self.slots.get(loc)
        result = number(value)
        if result is None and isinstance(value,str) and value.startswith("="):
            self.visiting.add(loc)
            try:
                result = self._evaluate(value,loc.rsplit("!",1)[0])
            except (SyntaxError, IndexError, KeyError, ValueError, TypeError, ArithmeticError, RecursionError, TokenizerError):
                result = None
            finally:
                self.visiting.discard(loc)
        if result is None:
            result = number(self.cache.get(loc))
        self.memo[loc] = result
        return result

    def _evaluate(self, formula, sheet):
        if len(formula) > 4096:
            return None
        parts, inputs = [], {}
        for token in Tokenizer(formula).items:
            if token.type == "OPERAND" and token.subtype == "RANGE":
                refs = _references(token.value,sheet,self.sheets)
                if not refs:
                    return None
                names = []
                for ref in refs:
                    val = self.get(ref)
                    if val is None:
                        return None
                    name = f"x{len(inputs)}"
                    inputs[name] = val
                    names.append(name)
                parts.append(",".join(names))
            elif token.type == "FUNC":
                if token.subtype == "OPEN" and token.value.upper()[:-1] not in self.functions:
                    return None
                parts.append(token.value.upper())
            elif token.type in {"PAREN","WSPACE","WHITE-SPACE"} or token.value in {"+","-","*","/",","} or token.subtype == "NUMBER":
                parts.append(token.value)
            else:
                return None
        expr=ast.parse("".join(parts),mode="eval").body
        def evaluate(n):
            if isinstance(n,ast.Constant) and type(n.value) in (int,float):
                return Decimal(str(n.value))
            if isinstance(n,ast.Name):
                return inputs[n.id]
            if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in self.functions and not n.keywords:
                return self.functions[n.func.id]([evaluate(a) for a in n.args])
            if isinstance(n,ast.UnaryOp) and isinstance(n.op,(ast.USub,ast.UAdd)):
                return evaluate(n.operand)*(-1 if isinstance(n.op,ast.USub) else 1)
            if isinstance(n,ast.BinOp):
                left,right=evaluate(n.left),evaluate(n.right)
                if isinstance(n.op,ast.Add): return left+right
                if isinstance(n.op,ast.Sub): return left-right
                if isinstance(n.op,ast.Mult): return left*right
                if isinstance(n.op,ast.Div): return left/right
            raise ValueError("Unsupported numeric formula")
        value = evaluate(expr)
        return value if value.is_finite() and abs(value)<Decimal("1e100") else None


def _add(left, right, factor=Decimal(1)):
    terms = dict(left)
    for key, value in right.items():
        terms[key] = terms.get(key, 0) + value * factor
    return {k: v for k, v in terms.items() if v}


def _linear(node, variables):
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return _add({}, {"": Decimal(str(node.value))})
    if isinstance(node, ast.Name) and node.id in variables:
        return {variables[node.id]: Decimal(1)}
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return _add({}, _linear(node.operand, variables), -1 if isinstance(node.op, ast.USub) else 1)
    if isinstance(node, ast.BinOp):
        left, right = _linear(node.left, variables), _linear(node.right, variables)
        if isinstance(node.op, (ast.Add, ast.Sub)):
            return _add(left, right, -1 if isinstance(node.op, ast.Sub) else 1)
        if isinstance(node.op, ast.Mult):
            if not (right.keys() - {""}):
                return _add({}, left, right.get("", 0))
            if not (left.keys() - {""}):
                return _add({}, right, left.get("", 0))
        if isinstance(node.op, ast.Div) and set(right) == {""}:
            return _add({}, left, Decimal(1) / right[""])
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "SUM" and not node.keywords:
        total = {}
        for a in node.args:
            total = _add(total, _linear(a, variables))
        return total
    raise ValueError("Unsupported relation")


def _cells(slots):
    sheets = defaultdict(lambda: defaultdict(dict))
    for loc, value in slots.items():
        if "!" not in loc or loc.startswith("native:"):
            continue
        name, addr = loc.rsplit("!", 1)
        if ADDRESS.fullmatch(addr):
            col, row = re.fullmatch(r"([A-Z]+)(\d+)", addr.upper()).groups()
            sheets[name][int(row)][column_index_from_string(col)] = value
    return sheets


def _at(sheet, col, row):
    return _ref(sheet, f"{get_column_letter(col)}{row}")


def _issue(rule, loc, actual, expected, inputs, reason, *, kind="unverified", repair=None):
    entry = dict(rule=rule, locator=loc, actual=actual, expected=expected, inputs=list(inputs),
                 reason=reason, kind=kind)
    if repair is not None:
        entry["repair"] = {"locator": loc, "before": actual, "after": repair}
    return entry


def _check(rule, loc, actual, expected, inputs):
    return dict(rule=rule, locator=loc, actual=actual, expected=expected, inputs=list(inputs), status="verified")


def _role(label):
    if BALANCE.search(label) and not re.search(r"/|\b(?:interest|ratio|leverage|coverage)\b", label, re.I):
        if OPENING.search(label):
            return "opening"
        if CLOSING.search(label):
            return "closing"
    if not re.search(r"/|\b(?:coverage|rate|ratio|fee)\b|%", label, re.I):
        if PAYMENT.search(label):
            return "amortization"
        if SWEEP.search(label):
            return "sweep"
        if re.search(r"\b(?:new draws?|drawdowns?|new borrowings|issuances?)\b", label, re.I):
            return "draw"
    return None


def _metric(label):
    if re.fullmatch(r"(?:(?:scheduled|annual|mandatory)\s+)?amorti[sz]ation(?:\s*\((?:annual|%|rate)\))?",label.strip(),re.I):
        return "amortization_pct"
    if re.fullmatch(r"(?:excess cash flow|ECF|cash) sweep(?: rate|\s*\(%\))?",label.strip(),re.I):
        return "sweep_pct"
    return "spread_bps" if SPREAD.search(label) else "facility_usd000" if FACILITY.search(label) else None


def _scale(field, text):
    if field in {"amortization_pct","sweep_pct"}:
        return 100 if "%" in text or re.search(r"\bpercent(?:age)?\b",text,re.I) else None
    if field == "spread_bps":
        if re.search(r"\bbps?\b|basis points?", text, re.I):
            return 1
        return None
    if re.search(r"(?:USD|US\$|\$)\s*(?:in\s+)?(?:thousands?|000s?\b|k\b)|\bkUSD\b", text, re.I):
        return 1
    if re.search(r"\bUSD\s*(?:millions?|mm\b)", text, re.I):
        return 1000
    return None


def _read_tables(rows, sheet, result, consumed):
    tables = []
    vertical = []
    growth = []
    for row, values in sorted(rows.items()):
        present = sorted(values)
        label_col = present[0]
        label = str(values[label_col])
        for col in present:
            raw = values[col]
            if not isinstance(raw, str) or raw.startswith("="):
                continue
            if SCENARIO.fullmatch(raw.strip()):
                if not tables or tables[-1][0] != row:
                    tables.append((row, {}))
                tables[-1][1][col] = scenario(raw)
        field = _metric(label)
        if field and scenario(label) and len(present) > 2:
            loc = _at(sheet, present[1], row)
            unit = _scale(field, str(values[present[2]]) + " " + label)
            if unit is not None:
                vertical.append((scenario(label), field, loc, unit))
        m = re.fullmatch(r"(.+?)\s+(?:annual\s+)?(?:growth|growth rate)(?:\s*\(.*?\))?", label, re.I)
        if m and len(present) > 1:
            loc = _at(sheet, present[1], row)
            value = number(values[present[1]])
            if value is not None and loc in consumed:
                # Excel rates use fractional inputs; multi-percent integers
                # require an explicit percent-of-100 convention.
                if abs(value) <= 1:
                    growth.append(dict(field="growth_rate",entity=m[1].strip(),locator=loc,
                                       value=float(value),unit="fraction"))
    entries = list(vertical)
    for i, (header_row, cols) in enumerate(tables):
        if len(cols) < 2:
            continue
        next_row = tables[i+1][0] if i + 1 < len(tables) else max(rows)+1
        for row, values in sorted(rows.items()):
            if row <= header_row or row >= next_row:
                continue
            texts = [str(v) for c,v in values.items() if c < min(cols)]
            label = " ".join(texts)
            field = _metric(label)
            if not field or label.lower().startswith(("max ", "minimum ", "interest ")):
                continue
            header = " ".join(str(v) for c,v in rows[header_row].items() if c not in cols)
            unit = _scale(field, label + " " + header)
            if unit is None and field in {"amortization_pct","sweep_pct"}:
                # A row of ratios without an explicit unit is ambiguous.
                # A consuming rate formula or an explicit workbook legend
                # must establish the scale before enforcing a source term.
                continue
            if unit is None:
                result["findings"].append(_issue("scenario_units", _at(sheet,min(cols),row), label, field,
                     [_at(sheet,min(cols),header_row)], "Scenario field has no verified denomination.", kind="mapping_error"))
                continue
            for col, entity in cols.items():
                entries.append((entity, field, _at(sheet,col,row),unit))
    result["fields"].extend(growth)
    return entries


def _reach(loc, deps, stop=()):
    pending, seen = [loc], set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        if current not in stop:
            pending.extend(deps.get(current, ()))
    return seen


def inspect_workbook(slots, *, selected_scenario=None, cached_slots=None, cash_evidence=()):
    result = dict(applicable=False, mapping_status="not_applicable", scenario_slots={}, dependencies={},
                  fields=[], findings=[], checks=[], schedules=[], coverage={}, unresolved_formula_references=[])
    sheets = _cells(slots)
    names = {name.casefold():name for name in sheets}
    computed = NativeValues(slots,names,cached_slots)
    signatures, deps, complete = {}, result["dependencies"], {}
    for loc, value in slots.items():
        if "!" in loc and isinstance(value, str) and value.startswith("="):
            deps[loc], signatures[loc], complete[loc] = _formula(value,loc.rsplit("!",1)[0],names)
            if not complete[loc]:
                result["unresolved_formula_references"].append(loc)
    entries = []
    consumed = {loc for values in deps.values() for loc in values}
    for sheet, rows in sheets.items():
        entries.extend(_read_tables(rows,sheet,result,consumed))
    counts = defaultdict(list)
    for entity, field, loc, factor in entries:
        counts[entity,field].append(loc)
        raw = slots.get(loc)
        val = computed.get(loc)
        result["fields"].append(dict(field=field, entity=entity, locator=loc, unit=field.split("_")[-1],
                                     value=float(val * factor) if val is not None else None, factor=factor))
    for (entity, field), locs in counts.items():
        if len(locs) == 1:
            result["scenario_slots"].setdefault(entity, {})[field] = locs[0]
        else:
            result["findings"].append(_issue("scenario_mapping",locs[0],locs,field,locs,
                      "Multiple native slots claim the same scenario and field.",kind="mapping_error"))
    _cashflow_checks(result,sheets,slots,deps,signatures,computed,cash_evidence)
    for sheet, rows in sheets.items():
        _schedules(result,sheet,rows,slots,deps,signatures,complete,selected_scenario,computed)
    result["sweep_projections"]=_sweep_projections(result,sheets,slots,deps,signatures,computed)
    if selected_scenario:
        _current_use(result,slots,selected_scenario)
    has_debt = any(re.search(r"\b(?:debt (?:service|schedule)|financing (?:cases|scenarios))\b", name, re.I) for name in sheets)
    if has_debt and not entries and not result["schedules"]:
        result["findings"].append(_issue("financing_layout", next(iter(sheets)), sorted(sheets),
                 "mapped scenario terms or debt schedule",[],
                 "Financial schedule is present but no inspectable input or balance layout was identified.",kind="mapping_error"))
    if entries or result["schedules"] or result["findings"] or result["checks"]:
        result["applicable"] = True
        result["mapping_status"] = "mapped"
        if any(f["kind"] == "mapping_error" for f in result["findings"]):
            result["mapping_status"] = "partial" if entries or result["schedules"] else "unmapped"
    return result


def _current_use(result, slots, selected):
    declarations=[]
    for loc,raw in slots.items():
        if not isinstance(raw,str) or raw.startswith("=") or loc.startswith("native:"):
            continue
        for sentence in re.split(r"[.;\n]",raw):
            if re.search(r"\b(?:historical|archived|previous|former|not|never)\b",sentence,re.I):
                continue
            if re.search(r"\b(?:current|active|selected)\s+(?:signing|execution|drafting)(?:\s+(?:basis|model|preparation|scenario))?\b|\b(?:workbook|model) (?:used|prepared) for signing\b",sentence,re.I):
                declarations.append(loc)
    if not declarations:
        return
    uses=[(s["opening"],name,loc) for s in result["schedules"] for name,loc in s["funding"].items()]
    cases={name for _,name,_ in uses}
    expected=scenario(selected)
    if len(cases)==1 and expected and expected not in cases:
        target=uses[0][0]
        result["findings"].append(_issue("current_use_selected_scenario",target,next(iter(cases)),expected,
                    [*declarations,*(loc for _,_,loc in uses)],
                    "The model declares current execution use but its debt schedule consumes another scenario.",
                    kind="mapping_error"))
    elif len(cases)>1:
        result["findings"].append(_issue("current_use_selected_scenario",declarations[0],sorted(cases),expected,
                    [*declarations,*(loc for _,_,loc in uses)],
                    "The workbook declares current execution use but does not identify which debt schedule represents it.",kind="unverified"))
    elif len(cases)==1 and expected in cases:
        result["checks"].append(_check("current_use_selected_scenario",uses[0][0],expected,expected,
                                       [*declarations,*(loc for _,_,loc in uses)]))


def _cash_metric(label):
    label = re.sub(r"\s*\(\s*(?:usd|eur|gbp)\s*(?:thousands?|millions?|000s?)?\s*\)","",str(label),flags=re.I).casefold()
    # A percentage inside a parenthetical assumption (e.g. a tax rate)
    # qualifies an absolute cash amount. A pure percent, % of revenue, ratio
    # or margin label continues to denote a rate rather than money.
    trimmed=re.sub(r"\s*\([^()]*(?:\d\s*%|less |minus |based on |assuming |uses |federal)[^()]*\)\s*$",
                   "",label,flags=re.I)
    if re.search(r"\b(?:cash tax(?:es)?|cfads|cash available for debt service|capital expenditure|capex proxy)\b",trimmed,re.I):
        label=trimmed
    if ("/" in label or "%" in label or re.search(r"\b(?:ratio|coverage|growth|rate|margin|multiple|assumption)\b",label)):
        return None
    if re.search(r"\bcfads\b|cash (?:flow )?available for debt service",label):
        return "cfads"
    if re.fullmatch(r"(?:(?:case|scenario)\s+\d+\s+)?(?:cash (?:interest|finance costs)|interest (?:paid|expense|proxy))",label.strip()):
        return "interest_paid"
    if _role(label.rstrip(".")) == "amortization":
        return "principal_paid"
    if re.search(r"\b(?:cash from operations|cash flow from operating activities|net (?:cash from|operating cash flow)|operating cash flow|after.tax cash from operations)\b",label):
        return "operating_cash"
    if re.search(r"\b(?:capex|capital expenditures?|capital spending)\b",label):
        return "capex"
    if re.search(r"\b(?:cash tax(?:es)?|tax(?:es)? paid|simple tax|cash income tax|income tax(?:es)?|current taxes?)\b",label):
        return "tax"
    if re.search(r"\b(?:ebitda|ebit|pre.tax earnings|taxable (?:income|profit))\b",label):
        return "earnings"
    if re.search(r"\b(?:excess cash flow|distributable cash|available cash(?: flow)?|cash available for sweep|post.debt.service cash|cash after debt service)\b",label) and "sweep rate" not in label and "cash flow sweep" not in label:
        return "available_cash"
    return None


def projected_subrow(header, detail):
    """Pair a financial section with its adjacent data row under fiscal-year columns."""
    if not header or not detail:
        return False
    first=min(header)
    if first!=min(detail) or not isinstance(detail[first],str):
        return False
    # A row describing another metric cannot inherit the previous heading.
    if not re.fullmatch(r"(?:platform consolidated|consolidated(?: total)?|platform total|group total|total)",detail[first].strip(),re.I):
        return False
    years={col for col,value in header.items() if col>first and PERIOD.fullmatch(str(value).strip())}
    if len(years)<2:
        return False
    numeric={col for col,value in detail.items() if col>first and (number(value) is not None or str(value).startswith("="))}
    return numeric==years and any(str(detail[col]).startswith("=") for col in years)


def _period_and_unit(rows, sheet, col, row):
    """Bind year and denomination to a specific numeric column and block."""
    period = None
    denomination = None
    year_header = None
    def unit(text):
        patterns=(("USD",r"(?:USD|US dollars?|US\$)"),("EUR",r"(?:EUR|euros?)"),("GBP",r"(?:GBP|pounds?)"))
        matched=[]
        for curr,code in patterns:
            for m in re.finditer(rf"(?:\b{code})\s*(?:in\s+)?(thousands?|000s?|k\b|millions?|mm\b|billions?|bn\b|units?\b)?",str(text),re.I):
                scale=m[1].lower() if m[1] else ""
                multiplier=1000000000 if scale.startswith("b") else 1000000 if scale.startswith("m") else 1000 if scale.startswith(("t","0","k")) else 1
                matched.append((curr,multiplier))
        return matched[0] if len(set(matched))==1 else None
    label=str(rows[row][min(rows[row])])
    denomination=unit(label)
    for header in sorted((r for r in rows if r < row),reverse=True):
        raw=str(rows[header].get(col,"")).strip()
        if PERIOD.fullmatch(raw):
            period=int(re.search(r"\d{4}",raw)[0])
            year_header=header
            break
    if year_header is not None and denomination is None:
        # Current table header or its nearest explicit units declaration.
        for header in sorted((r for r in rows if r<=year_header),reverse=True):
            cells=rows[header]
            left=[str(cells[k]) for k in cells if k<col]
            candidates={u for x in left if (u:=unit(x))}
            if len(candidates)==1:
                denomination=next(iter(candidates))
                break
            if candidates:
                break
            if header!=year_header and any(PERIOD.fullmatch(str(v).strip()) for v in cells.values()):
                break
    return (period,denomination)


def _expansion(loc, boundaries, signatures):
    """Reduce affine identities without treating constants or aliases as evidence."""
    memo={}
    visiting=set()
    def expand(ref,depth=0):
        if ref!=loc and ref in boundaries:
            return {ref:Decimal(1)}
        if ref in memo:
            return memo[ref]
        if depth>50 or ref in visiting or ref not in signatures or signatures[ref] is None:
            return {ref:Decimal(1)}
        visiting.add(ref)
        terms={}
        for sub,coefficient in signatures[ref].items():
            part=expand(sub,depth+1) if sub else {"":Decimal(1)}
            terms=_add(terms,part,coefficient)
        visiting.remove(ref)
        memo[ref]=terms
        return terms
    return expand(loc)


def released_cash_observations(slots, bindings, claims):
    """Certify only active scalar originals explicitly bound to the native cash cell.

    An observation must state the role, entity, financial period, value and
    currency denomination in the released quotation itself. Model-authored
    notes, instructions, calculated snapshots, and values copied out of the
    candidate cannot authorize a boundary cash amount.
    """
    from .artifact_proofs import is_business_claim
    from .native_grounding import document_quantities, entity_anchor
    sources={n["id"]:n for n in claims if is_business_claim(n)
             and n["payload"]["locator"].startswith("source/")}
    observations=[]
    for binding in bindings:
        claim=sources.get(binding["claim_id"])
        loc=binding.get("locator","")
        if not claim or loc not in slots or not ADDRESS.fullmatch(loc.rsplit("!",1)[-1]):
            continue
        cp=claim["payload"]
        raw=number(slots[loc])
        if raw is None or number(cp.get("value"))!=raw:
            continue
        quote=str(cp["quote"])
        role=_cash_metric(str(cp.get("field","")).replace("_"," "))
        years=set(re.findall(r"\b(?:FY\s*|fiscal year\s+|year ending\s+)((?:19|20)\d{2})\b",quote,re.I))
        if role not in {"cfads","operating_cash","available_cash"} or len(years)!=1 or not entity_anchor(cp["entity"],quote):
            continue
        if _cash_metric(quote)!=role:
            continue
        quantities=[q for q in document_quantities(quote,None) if q.value==raw and q.dimension in {"USD","EUR","GBP"}]
        units={(q.dimension,int(q.scale)) for q in quantities}
        if len(units)!=1:
            continue
        observations.append(dict(locator=loc,field=role,value=float(raw),period=int(next(iter(years))),
                                 unit=list(next(iter(units))),claim_id=claim["id"],source_id=cp["source_id"]))
    return observations


def _cashflow_checks(result,sheets,slots,deps,signatures,values,cash_evidence):
    metrics,dimensions={},{}
    legends=set()
    for sheet,rows in sheets.items():
        if not re.fullmatch(r"read[ _-]?me|cover|model (?:guide|notes)",sheet,re.I):
            continue
        for row,cols in rows.items():
            for value in cols.values():
                for block in re.split(r"[|;\n]",str(value)):
                    m=re.fullmatch(r"\s*(?:units?\s*[:=]\s*)?(USD|EUR|GBP)\s*(thousands?|millions?|billions?|000s?|mm|bn|k)(?:\s+unless (?:otherwise )?(?:stated|noted))?[.]?\s*",block,re.I)
                    if m:
                        scale=m[2].casefold()
                        factor=1000000000 if scale.startswith("b") else 1000000 if scale.startswith("m") else 1000
                        legends.add((m[1].upper(),factor))
    workbook_unit = next(iter(legends)) if len(legends)==1 else None
    for sheet,rows in sheets.items():
        for row,cols in rows.items():
            label_col=min(cols)
            metric=_cash_metric(cols[label_col])
            if not metric and row-1 in rows and projected_subrow(rows[row-1],cols):
                prior_label=rows[row-1][min(rows[row-1])]
                metric=_cash_metric(prior_label)
            if not metric:
                continue
            for col,raw in cols.items():
                if col>label_col and (number(raw) is not None or str(raw).startswith("=")):
                    loc=_at(sheet,col,row)
                    metrics[loc]=metric
                    year,unit=_period_and_unit(rows,sheet,col,row)
                    dimensions[loc]=(year,unit or workbook_unit)
    result["cashflow_slots"]=metrics
    confirmed_cfads=set()
    confirmed_available=set()
    confirmed_evidence={}
    # These tuples are supplied only by a trusted caller after verifying the
    # original-bound value, denomination and exact period against this file.
    for proof in cash_evidence:
        loc=proof.get("locator")
        if loc not in dimensions or not proof.get("claim_id") or not proof.get("source_id"):
            continue
        period,unit=dimensions[loc]
        if (period is not None and unit and period==proof.get("period")
                and list(unit)==proof.get("unit") and values.get(loc) is not None and number(proof.get("value"))==values.get(loc)
                and metrics[loc]==proof.get("field") and metrics[loc] in {"cfads","operating_cash","available_cash"}):
            confirmed_evidence[loc]=proof["claim_id"]
    def signed_leaves(loc,roles):
        boundaries={source for source,role in metrics.items() if role in roles}
        terms=_expansion(loc,boundaries,signatures)
        year,unit=dimensions[loc]
        if year is None or unit is None or not terms or any(r not in boundaries or dimensions[r]!=(year,unit) for r in terms):
            return None
        return terms

    def supported(loc,roles,income_roles,outlay_roles):
        terms=signed_leaves(loc,roles)
        if not terms:
            return False,[]
        actual=values.get(loc)
        expected=Decimal(0)
        counted={}
        for src,coeff in terms.items():
            role=metrics[src]
            val=values.get(src)
            if val is None:
                return False,sorted(terms)
            counted[role]=counted.get(role,0)+1
            if role in income_roles:
                if coeff != 1:
                    return False,sorted(terms)
                expected+=val
            elif role in outlay_roles:
                # Positive expenses are deducted; signed negative costs added.
                if abs(coeff)!=1 or (val and val*coeff>0):
                    return False,sorted(terms)
                expected-=abs(val)
            else:
                return False,sorted(terms)
        # One explicitly aggregated input per role. Combining segmented rows
        # with a total risks double counting and requires an explicit bridge.
        if any(v!=1 for v in counted.values()) or set(counted)!=roles:
            return False,sorted(terms)
        return actual is not None and actual==expected,sorted(terms)

    for loc,metric in metrics.items():
        if metric!="cfads":
            continue
        inputs=[]
        for income,expenses in (({"operating_cash"},{"capex"}),({"earnings"},{"capex","tax"})):
            ok,read=supported(loc,income|expenses,income,expenses)
            inputs+=read
            if ok:
                confirmed_cfads.add(loc)
                result["checks"].append(_check("cfads_cash_bridge",loc,slots[loc],
                        "same-period same-unit full cash bridge",read))
                break
        else:
            if loc in confirmed_evidence:
                confirmed_cfads.add(loc)
                result["checks"].append(_check("cfads_released_observation",loc,slots[loc],
                     float(values.get(loc)),[confirmed_evidence[loc]]))
            else:
                result["findings"].append(_issue("cfads_cash_bridge",loc,slots[loc],
                     "same-period operating cash less full capex, or earnings less full tax and capex",
                     sorted(set(inputs)), "A same-period, same-denomination cash-flow identity was not verified."))

    for loc,metric in metrics.items():
        if metric!="available_cash":
            continue
        if loc in confirmed_evidence:
            confirmed_available.add(loc)
            continue
        income={"cfads"}
        expenses={"interest_paid","principal_paid"}
        ok,read=supported(loc,income|expenses,income,expenses)
        if ok and all(src in confirmed_cfads for src in read if metrics[src]=="cfads"):
            confirmed_available.add(loc)
            result["checks"].append(_check("distributable_cash_bridge",loc,slots[loc],
                     "verified CFADS less scheduled interest and principal for the period",read))
    result["verified_cfads_inputs"]=sorted(confirmed_cfads)
    result["verified_sweep_cash_inputs"]=sorted(confirmed_available)
    result["cash_observation_claims"]=confirmed_evidence


def _period_columns(rows, row, cols):
    for header_row in sorted((r for r in rows if r < row), reverse=True):
        years = {}
        for col in cols:
            label = str(rows[header_row].get(col, "")).strip()
            if PERIOD.fullmatch(label):
                years[col] = int(re.search(r"\d{4}",label)[0])
        if len(years) >= 2:
            return sorted(years.items(), key=lambda item:item[0])
    return []


def _verify_sweep_sensitivity(sweep, slots, refs, deps):
    """Perturb verified inputs in memory to distinguish a real cap from a label.

    Exercise surplus cash, no available cash and no remaining principal.
    Each evaluation reads original formulas; no workbook or cache is written.
    """
    cash, rate, start, repayment, interest = refs
    if cash==start or cash==rate or cash in repayment or rate in repayment or start in repayment:
        return False
    sheet_names={loc.rsplit("!",1)[0].casefold():loc.rsplit("!",1)[0] for loc in deps}
    for loc in [*refs[:3],*repayment,*interest]:
        sheet=loc.rsplit("!",1)[0]
        sheet_names[sheet.casefold()]=sheet
    for balance,available,percentage,interest_due in ((100,80,0.5,5),(100,1000,0.5,5),(100,-10,0.5,0),
                                                      (20,80,0.5,5),(100,80,0.25,15),(100,80,0.5,90)):
        # Values are stress inputs for the invariant, never replacement facts.
        overridden=dict(slots)
        overridden.update({start:balance,cash:available,rate:percentage})
        for num,loc in enumerate(repayment):
            overridden[loc]=20 if num==0 else 0
        for loc in interest:
            overridden[loc]=interest_due
        actual=NativeValues(overridden,sheet_names,None).get(sweep)
        net=available-(interest_due+(20 if repayment else 0) if interest else 0)
        wanted=min(max(0,balance-(20 if repayment else 0)),max(0,net)*percentage)
        if actual is None or abs(actual-Decimal(str(wanted)))>Decimal("0.000001"):
            return False
    return True


def _sweep_projections(result,sheets,slots,deps,signatures,values):
    """Pair sweep *amounts* and period-end principal through native consumers.

    A rate assumption or a standalone alternative has no obligation to change
    the current balance. An observed dependency alone cannot prove a debit:
    opening debt must lose the payment across feasible sweep input values.
    """
    consumers={}
    for target,inputs in deps.items():
        for origin in inputs:
            consumers.setdefault(origin,[]).append(target)
    rate_cells={fields["sweep_pct"]:case for case,fields in result["scenario_slots"].items()
                if fields.get("sweep_pct")}
    opening_cells={loc for schedule in result["schedules"] for pair in schedule["period_balances"].values()
                   for loc in (pair["opening"],pair["closing"])}
    sheet_names={n.casefold():n for n in sheets}
    projections=[]
    for sheet,rows in sheets.items():
        for row,data in rows.items():
            label=str(data[min(data)])
            if _role(label)!="sweep":
                continue
            periods=_period_columns(rows,row,set(data))
            # Excludes term inputs under case headers and literal percentages
            # even when the row contains the word "sweep".
            for column,year in periods:
                source=_at(sheet,column,row)
                if source not in deps or source in rate_cells:
                    continue
                ids={rate_cells[k] for k in _reach(source,deps,stop=opening_cells) if k in rate_cells}
                named=scenario(label)
                selected=next(iter(ids)) if len(ids)==1 else None
                role_conflict=bool(named and selected and named!=selected) or len(ids)>1
                case=named or selected
                matches=[]
                for sched in result["schedules"]:
                    if year not in sched["period_balances"]:
                        continue
                    usage=next(iter(sched["funding"])) if len(sched["funding"])==1 else sched["entity"]
                    if case==usage or (case is None and usage is None and source.rsplit("!",1)[0]==sched["opening"].rsplit("!",1)[0]):
                        matches.append(sched)
                amount=values.get(source)
                record={"scenario":case,"year":year,"amount_locator":source,"label_locator":_at(sheet,min(data),row),
                        "declared_illustrative":bool(ILLUSTRATIVE.search(label)),
                        "native_formula":slots[source],"amount":float(amount) if amount is not None else None,"rate_scenarios":sorted(ids),"used_by":consumers.get(source,[]),
                        "ending_candidates":[q["period_balances"][year]["closing"] for q in matches],
                        "ending_locator":None,"matched_schedule":None,"basis":"unresolved","status":"ambiguous"}
                if not matches and case in result["scenario_slots"] and not role_conflict and record["declared_illustrative"]:
                    record.update(basis="comparison_only",status="comparison_estimate_no_matching_schedule")
                if len(matches)==1 and not role_conflict:
                    sched=matches[0]
                    pair=sched["period_balances"][year]
                    target=pair["closing"]
                    record.update(basis="scheduled_scenario",matched_schedule=sched["opening"],ending_locator=target,
                                  ending_formula=slots.get(target),status="unconsumed")
                    # Stop at this period's opening balance: last year's
                    # sweep is not a payment in the current year's closing.
                    inputs=_reach(target,deps,stop={pair["opening"]})
                    if source in inputs:
                        # Stress the actual amount, holding other inputs fixed.
                        # A zero term, cancellation or multiplication by the
                        # wrong sign is an edge but no verified repayment.
                        test=[0,1,10]
                        opening=values.get(pair["opening"])
                        if opening is not None and opening>0:
                            test=[Decimal(0),opening/100,opening/10]
                        runs=[]
                        for val in test:
                            view=NativeValues(slots,sheet_names,None)
                            view.memo[source]=Decimal(val)
                            runs.append(view.get(target))
                        significant=any(v is not None and v>0 for v in runs)
                        debit=(all(v is not None for v in runs) and significant and
                               all(abs(v-max(Decimal(0),runs[0]-x))<=Decimal("0.000001") for x,v in zip(test,runs)))
                        # A linear debit is still provable when the schedule
                        # has amortized to zero in this particular period.
                        affine=_expansion(target,{source,pair["opening"]},signatures)
                        if affine.get(source)==-1 or debit:
                            record["status"]="deducted"
                        else:
                            record["status"]="dependency_not_verified_as_debit"
                    elif record["used_by"]:
                        record["status"]="other_consumers_only"
                projections.append(record)
    # A same-case sensitivity does not compete with a verified booked
    # payment. Never demote a row that actually influences the balance.
    deducted={r["ending_locator"] for r in projections if r["status"]=="deducted"}
    for r in projections:
        if (r["ending_locator"] in deducted and r["declared_illustrative"]
                and r["status"] in {"unconsumed","other_consumers_only"}):
            r.update(basis="comparison_only",status="supplemental_estimate_not_booked")
    return projections


def _schedules(result, sheet, rows, slots, deps, signatures, complete, selected, cache):
    groups = defaultdict(lambda:defaultdict(list))
    for r, values in rows.items():
        c = min(values)
        label = str(values[c])
        role = _role(label)
        if role and any(isinstance(v,(float,int)) or str(v).startswith("=") for k,v in values.items() if k != c):
            groups[scenario(label)][role].append(r)
    # A schedule often labels its opening row with the case and omits the
    # repeated case prefix on subsequent rows. Carry the identity only across
    # an unambiguous sheet with direct balance references in every period.
    names=[name for name in groups if name]
    if None in groups and len(names)==1:
        named=groups[names[0]]
        unlabelled=groups[None]
        opening=named.get("opening",[])
        closing=unlabelled.get("closing",[])
        if len(opening)==len(closing)==1 and not named.get("closing") and not unlabelled.get("opening"):
            columns=[col for col in rows[closing[0]] if col>min(rows[closing[0]])]
            if columns and all(_at(sheet,col,opening[0]) in deps.get(_at(sheet,col,closing[0]),[]) for col in columns):
                for role,positions in unlabelled.items():
                    named[role].extend(positions)
                del groups[None]
    schedules = result["schedules"]
    for entity, roles in groups.items():
        if "opening" not in roles and "closing" not in roles:
            continue
        label_row = (roles["opening"] or roles["closing"])[0]
        label_loc = _at(sheet,min(rows[label_row]),label_row)
        if not roles["opening"] and len(roles["closing"])==1:
            # A summary that directly re-exports the ending balance is a
            # consumer of a schedule. A missing opening there is not a defect.
            closing_row=roles["closing"][0]
            amounts=[_at(sheet,col,closing_row) for col in rows[closing_row] if col>min(rows[closing_row])]
            aliases=all(signatures.get(k) and len(signatures[k])==1 and next(iter(signatures[k].values()))==1
                        and next(iter(signatures[k])).rsplit("!",1)[0]!=sheet for k in amounts)
            if aliases and amounts:
                result["checks"].append(_check("closing_balance_summary",label_loc,rows[closing_row][min(rows[closing_row])],
                    "direct cross-sheet balance references",amounts))
                continue
        if len(roles["opening"]) != 1 or len(roles["closing"]) != 1:
            result["findings"].append(_issue("debt_schedule_mapping",label_loc,dict(roles),
                    "unique opening and closing balances for this schedule",[label_loc],
                    "Debt balances could not be paired unambiguously.",kind="mapping_error"))
            continue
        op, cl = roles["opening"][0], roles["closing"][0]
        cols = set(rows[op]) | set(rows[cl])
        periods = _period_columns(rows,min(op,cl),cols)
        if len(periods) < 2 or any(b[1] != a[1]+1 for a,b in zip(periods,periods[1:])):
            result["findings"].append(_issue("debt_period_mapping",label_loc,periods,
                    "consecutive, uniquely dated periods",[label_loc],
                    "The available annual headers do not establish contiguous periods.",kind="mapping_error"))
            continue
        opening = _at(sheet,periods[0][0],op)
        upstream = _reach(opening,deps)
        funding = {entity:table["facility_usd000"] for entity,table in result["scenario_slots"].items()
                   if table.get("facility_usd000") in upstream}
        schedule = dict(entity=entity, opening=opening, funding=funding, closing=_at(sheet,periods[0][0],cl),
                        periods=[year for col,year in periods],
                        period_balances={year:{"opening":_at(sheet,col,op),"closing":_at(sheet,col,cl)}
                                         for col,year in periods})
        schedules.append(schedule)
        if result["scenario_slots"]:
            expected = entity
            if not expected and selected and len(groups) == 1:
                expected = scenario(selected)
            if len(funding) == 1:
                used = next(iter(funding))
                inputs = [opening,funding[used]]
                if expected and expected != used:
                    result["findings"].append(_issue("scenario_consumer",opening,used,expected,inputs,
                          "Opening debt consumes a different facility from its schedule designation.",kind="mapping_error"))
                else:
                    result["checks"].append(_check("scenario_consumer",opening,used,expected or used,inputs))
            else:
                result["findings"].append(_issue("scenario_consumer",opening,sorted(funding),expected,
                         list(deps.get(opening,[])), "The actual opening balance has no unique source scenario.",kind="mapping_error"))
        terms = result["scenario_slots"].get(next(iter(funding)),{}) if len(funding) == 1 else {}
        for index,(col,year) in enumerate(periods):
            start, end = _at(sheet,col,op), _at(sheet,col,cl)
            if index:
                prev_col = periods[index-1][0]
                prev = _at(sheet,prev_col,cl)
                target = {prev:Decimal(1)}
                sig = signatures.get(start)
                # A recomputation of the preceding closing balance has the
                # same identity when it uses exactly its expression.
                verified = sig == target or (sig is not None and sig == signatures.get(prev) and bool(sig))
                if verified:
                    result["checks"].append(_check("opening_equals_prior_closing",start,slots.get(start),
                                         "="+prev.rsplit("!",1)[1],[prev,*deps.get(start,())]))
                else:
                    expected = "="+prev.rsplit("!",1)[1]
                    # Repair is authorized only for a proven discrepancy with
                    # a simple local balance expression. Unmapped external
                    # adjustments require review of their meaning first.
                    local = {_at(sheet,prev_col,r) for rr in roles.values() for r in rr}
                    proven = sig is not None and complete.get(start) and set(sig) <= local | {""} and bool(signatures.get(prev))
                    measured = number(cache.get(start)) if cache is not None else None
                    comparison = number(cache.get(prev)) if cache is not None else None
                    if measured is not None and comparison is not None and measured != comparison:
                        proven = proven or (complete.get(start) and set(deps.get(start,())) <= local)
                    issue = _issue("opening_equals_prior_closing",start,slots.get(start),expected,[prev,*deps.get(start,())],
                                   "Next opening debt must carry the preceding closing principal.",
                                   kind="state_error" if proven else "unverified",
                                   repair=expected if proven else None)
                    if measured is not None and comparison is not None:
                        issue.update(observed_value=float(measured), previous_closing_value=float(comparison))
                    result["findings"].append(issue)
            movements = {role:[_at(sheet,col,r) for r in roles.get(role,())]
                         for role in ("amortization","sweep","draw")}
            if movements["sweep"]:
                # Check booked outflows in the movement identity; do not add
                # an adjacent labelled stress estimate to the balance again.
                ancestors=_reach(end,deps,stop={start})
                booked=[loc for loc in movements["sweep"] if loc in ancestors]
                if booked or not terms.get("sweep_pct") or cache.get(terms["sweep_pct"])==0:
                    alternatives={_at(sheet,col,r) for r in roles["sweep"]
                                  if ILLUSTRATIVE.search(str(rows[r][min(rows[r])]))}
                    movements["sweep"]=[loc for loc in movements["sweep"] if loc in booked or loc not in alternatives]
            movement_slots = [v for locs in movements.values() for v in locs]
            if movement_slots:
                expected = {start:Decimal(1), **{loc:Decimal(1 if role=="draw" else -1)
                            for role,locs in movements.items() for loc in locs}}
                actual = signatures.get(end)
                if actual == expected:
                    result["checks"].append(_check("debt_movements",end,slots.get(end),list(expected),[start,*movement_slots]))
                else:
                    # A capped/conditional formula may be valid. A separate
                    # numerical proof checks the exact period including zeros.
                    all_inputs = [start,*movement_slots]
                    values = {loc:number(cache.get(loc)) if cache else None for loc in [end,*all_inputs]}
                    if all(v is not None for v in values.values()):
                        wanted = sum(values[k]*v for k,v in expected.items())
                        numeric_ok = abs(wanted-values[end]) <= Decimal("0.000001") and all(loc in _reach(end,deps) for loc in all_inputs)
                    else:
                        numeric_ok = False
                    if numeric_ok:
                        result["checks"].append(_check("debt_movements",end,float(values[end]),float(wanted),all_inputs))
                    else:
                        result["findings"].append(_issue("debt_movements",end,slots.get(end),list(expected),all_inputs,
                           "Could not verify closing principal including scheduled repayments, sweep and draws.",
                           kind="state_error" if actual is not None and set(actual)<=set(expected) else "unverified"))
            if cache:
                opening_value = number(cache.get(start))
                principal, percentage = terms.get("facility_usd000"), terms.get("amortization_pct")
                if principal and percentage and len(movements["amortization"]) == 1:
                    repayment=movements["amortization"][0]
                    par, rate, paid = cache.get(principal), cache.get(percentage), cache.get(repayment)
                    ancestors = _reach(repayment,deps)
                    if None not in (par,rate,paid,opening_value):
                        amount=min(opening_value,par*rate)
                        inputs=[principal,percentage,start]
                        ok=abs(amount-paid)<=Decimal("0.000001") and principal in ancestors and percentage in ancestors
                        if ok:
                            result["checks"].append(_check("scheduled_repayment_basis",repayment,float(paid),float(amount),inputs))
                        else:
                            result["findings"].append(_issue("scheduled_repayment_basis",repayment,slots.get(repayment),float(amount),inputs,
                                      "Scheduled principal must follow the facility and its annual amortization assumption, capped by outstanding debt.",
                                      kind="state_error" if abs(amount-paid)>Decimal("0.000001") else "mapping_error"))
                sweep_rate=terms.get("sweep_pct")
                if len(movements["sweep"]) == 1:
                    sweep=movements["sweep"][0]
                    sources=_reach(sweep,deps,stop={start,*movements["amortization"]})-{sweep}
                    cash_inputs=[k for k in sources if result["cashflow_slots"].get(k) in {"available_cash","cfads"}]
                    # Sweep cash must reflect cash available after scheduled
                    # debt service. CFADS alone is before debt service.
                    after_service=[k for k in cash_inputs if k in result["verified_sweep_cash_inputs"]]
                    inline_interest=[]
                    if not after_service:
                        cfads=[k for k in cash_inputs if k in result["verified_cfads_inputs"]]
                        interest=[k for k in sources if result["cashflow_slots"].get(k)=="interest_paid"
                                  and k.rsplit("!",1)[0]==sheet and re.match(r"[A-Z]+",k.rsplit("!",1)[1])[0]==get_column_letter(col)]
                        if len(cfads)==len(interest)==1:
                            after_service=cfads
                            inline_interest=interest
                    inputs=[*cash_inputs,start,*movements["amortization"],*inline_interest,*([sweep_rate] if sweep_rate else [])]
                    rate_used=bool(sweep_rate and sweep_rate in sources)
                    principal_used=start in sources and set(movements["amortization"])<=sources
                    sweep_formula=str(slots.get(sweep,""))
                    cap_used=bool(re.search(r"\b(?:MIN|IF)\s*\(",sweep_formula,re.I))
                    actual=cache.get(sweep)
                    balance=cache.get(start)
                    amorts=[cache.get(k) for k in movements["amortization"]]
                    cash=cache.get(after_service[0]) if len(after_service)==1 else None
                    rate=cache.get(sweep_rate) if sweep_rate else None
                    expected=None
                    interest_values=[cache.get(k) for k in inline_interest]
                    if after_service and None not in (balance,rate,cash,*amorts,*interest_values):
                        net=cash-sum(interest_values)-sum(amorts) if inline_interest else cash
                        expected=max(Decimal(0),min(max(Decimal(0),balance-sum(amorts)),max(Decimal(0),net)*rate))
                    executed=actual is not None and expected is not None and abs(actual-expected)<=Decimal("0.000001")
                    sensitive = (after_service and rate_used and principal_used and cap_used and executed
                                 and _verify_sweep_sensitivity(sweep,slots,(after_service[0],sweep_rate,start,movements["amortization"],inline_interest),deps))
                    if sensitive:
                        result["checks"].append(_check("cash_sweep",sweep,slots.get(sweep),float(expected),inputs))
                    else:
                        result["findings"].append(_issue("cash_sweep",sweep,slots.get(sweep),
                            float(expected) if expected is not None else "available cash times sweep percentage capped by remaining principal",
                            inputs,"Cash sweep lacks a verified after-service cash base, scenario rate, balance cap or result.",
                            kind="state_error" if expected is not None and actual is not None and actual!=expected else "unverified"))
                amounts = [number(cache.get(loc)) for loc in movements["amortization"]+movements["sweep"]]
                if opening_value is not None and amounts and all(v is not None for v in amounts) and not movements["draw"]:
                    paid = sum(amounts)
                    if min(amounts)<0 or paid>opening_value + Decimal("0.000001"):
                        result["findings"].append(_issue("repayment_cap",end,float(paid),float(opening_value),
                           [start,*movement_slots],"Repayments exceed outstanding debt or are negative.",kind="state_error"))
                    else:
                        result["checks"].append(_check("repayment_cap",end,float(paid),float(opening_value),[start,*movement_slots]))
        # Without a separately modeled sweep flow, an assumed percentage
        # alone does not prove that cash actually reduces outstanding debt.
        result["coverage"][opening] = {"scheduled_repayment": bool(roles.get("amortization")),
                                       "sweep_payment":bool(roles.get("sweep")),
                                       "years":len(periods)}


def compare_term_rules(inspection, semantic_rules, slots):
    """Locate a released source term by entity and metric in the actual bytes."""
    result = {"checks":[], "findings":[]}
    for rule in semantic_rules:
        entity, field = rule["entity"],rule["field"]
        if rule.get("field") in {"sweep_pct","amortization_pct"} and not any(r["field"]==field for r in inspection["fields"]):
            # Standalone structures need not repeat a multi-case percentage
            # table. Required debt-service *flows* are checked as obligations.
            continue
        if (rule.get("when_scenario_present") and inspection["scenario_slots"]
                and entity not in inspection["scenario_slots"]):
            # A standalone scenario need not reproduce independent alternatives.
            continue
        matching = [r for r in inspection["fields"] if r["field"] == field and r["entity"] == entity]
        if len(matching) != 1:
            result["findings"].append(_issue("released_term_mapping", entity, [r["locator"] for r in matching],
                  field,[], "No unique, unit-compatible native slot maps to the released financing term.",kind="mapping_error"))
            result["findings"][-1]["claim_id"]=rule["claim_id"]
            continue
        m = matching[0]
        loc, expected = m["locator"],number(rule["expected"])
        actual = number(m["value"])
        if actual is not None and actual == expected:
            row = _check("released_term", loc, slots.get(loc),rule["expected"],[loc])
            result["checks"].append(row)
        else:
            # A source-backed literal may be corrected locally. Do not
            # replace formulas with constants when they have real precedents.
            repl = None
            if expected is not None and actual is not None and not inspection["dependencies"].get(loc):
                val = expected / Decimal(m["factor"])
                repl = int(val) if int(val) == val else float(val)
                if str(slots.get(loc)).startswith("="):
                    repl = "="+str(repl)
            row = _issue("released_term",loc,slots.get(loc),rule["expected"],[loc],
                      "Native financing term does not verify against the released scenario register.",
                      kind="state_error" if actual is not None else "unverified",repair=repl)
            result["findings"].append(row)
        row.update(claim_id=rule["claim_id"], entity=entity, field=field)
    return result


def inspect_consistency(workbook_slots, document_slots, *, workbook_name=None, document_name=None):
    """Check an assumptions memorandum against the model it explicitly describes.

    Dates, term sheets and other documents with no assertion about the model's
    basis do not inherit its scenario. A current signing direction may differ
    legitimately from a historical workbook choice.
    """
    inspection = inspect_workbook(workbook_slots)
    result = {"checks":[], "findings":[]}
    if not inspection["applicable"] and not inspection["fields"]:
        return result
    scenario_inputs = [(r["opening"],next(iter(r["funding"]))) for r in inspection["schedules"] if len(r["funding"]) == 1]
    actual_cases = {case for loc,case in scenario_inputs}
    short_names = [r"workbook",r"model"]
    if workbook_name:
        stem = Path(workbook_name).stem
        short_names.append(re.escape(stem))
        # Published scenario models commonly have a distinctive name suffix.
        token = stem.split()[-1]
        if len(token)>=4 and not token.lower() in {"model","workbook","forecast","version"}:
            short_names.append(re.escape(token))
    subject = "(?:"+"|".join(short_names)+")"
    basis = re.compile(r"\b"+subject+r"\s+(?:(?:workbook|model)\s+)?(?:is|uses|models|was built (?:as|for)|is based on)\s+"
                     r"(?:(?:an?|the|historical|main|active|senior.only|all.debt)\s+)*("+SCENARIO.pattern+r")",re.I)
    for loc,raw in document_slots.items():
        if not isinstance(raw,str) or loc.startswith("native:"):
            continue
        for m in basis.finditer(raw):
            stated = scenario(m[1])
            inputs = [r for r,_ in scenario_inputs]
            if not actual_cases:
                result["findings"].append(_issue("document_model_basis",loc,m[0],None,inputs,
                            "The model basis asserted in the document could not be traced to a debt consumer.",kind="mapping_error"))
            elif len(actual_cases)>1:
                result["findings"].append(_issue("document_model_basis",loc,m[0],sorted(actual_cases),inputs,
                    "The document names a single model basis, but multiple debt schedules consume different facilities. Specify the schedule and use.",
                    kind="unverified"))
            elif len(actual_cases) == 1:
                actual = next(iter(actual_cases))
                if stated == actual:
                    result["checks"].append(_check("document_model_basis",loc,m[0],actual,inputs))
                else:
                    result["findings"].append(_issue("document_model_basis",loc,m[0],actual,inputs,
                           "The named model's debt formula consumes a different scenario than the document claims.",kind="state_error"))
    memo = bool(document_name and re.search(r"(?:model|financ).{0,24}assumption|assumption.{0,24}memo",Path(document_name).stem,re.I))
    if not memo:
        return result
    rates = [r for r in inspection["fields"] if r["field"] == "growth_rate"]
    for r in rates:
        entity = re.escape(r["entity"]).replace(r"\ ",r"\s+")
        match = re.compile(r"\b"+entity+r"\b(?:\s+(?:annual|revenue|EBITDA|growth|rate|assumption|is|of|at|held|remains|rises|grows|by|to|assumed)){0,9}"
                          r"\s*[:=(–,-]?\s*([-+]?\d+(?:\.\d+)?)\s*%",re.I)
        witnesses = []
        for loc,v in document_slots.items():
            if isinstance(v,str) and not loc.startswith("native:"):
                # Native Word tables keep labels and rates in different cells.
                if loc.startswith("t:") and re.fullmatch(entity+r"(?:\s+(?:annual|revenue|EBITDA|growth|rate|assumption))*", v.strip(), re.I):
                    table, row, col = loc[2:].split(":")
                    for other,value in document_slots.items():
                        prefix=f"t:{table}:{row}:"
                        if not other.startswith(prefix) or other==loc or not isinstance(value,str):
                            continue
                        column=other.rsplit(":",1)[1]
                        heading=str(document_slots.get(f"t:{table}:1:{column}",""))
                        if "growth" not in (v+" "+heading).casefold():
                            continue
                        parsed=re.fullmatch(r"\s*([-+]?\d+(?:\.\d+)?)\s*%\s*",value)
                        if parsed:
                            witnesses.append((other,parsed))
                witnesses.extend((loc,m) for m in match.finditer(v) if
                                 "growth" in v[max(0,m.start()-100):m.end()+75].lower()
                                 or re.search(r"\b(?:grows|growth)\b",m[0],re.I))
        for loc,m in witnesses:
            rate = Decimal(m[1])/100
            if rate == number(r["value"]):
                result["checks"].append(_check("document_growth_assumption",loc,m[0],r["value"],[r["locator"]]))
            else:
                result["findings"].append(_issue("document_growth_assumption",loc,m[0],r["value"],[r["locator"]],
                       "Document states a different growth assumption from the one the forecast consumes.",kind="state_error"))
        if not witnesses:
            result["findings"].append(_issue("document_growth_disclosure",r["entity"],None,r["value"],[r["locator"]],
                   "No matching disclosure of this consumed growth assumption was verified in the assumptions memorandum."))
    return result

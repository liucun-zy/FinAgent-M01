"""Keep source-verified business points across sibling native proposals.

A point is a scalar at a semantic consumer. Failed bindings, tasks, ordinary
analysis, and unbound claims are not evidence. History remains append-only.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, InvalidOperation
import re

from .artifact_proofs import binding_context, contextual_quantities, is_business_claim
from .native_grounding import document_amount_unit, metrics, quantities, semantic_labels


def _scope(contract):
    return [contract.get(k) for k in ("path", "stage", "purpose", "valid_at", "entity")]


def _channel(locator):
    if locator.startswith("native:"):
        return "annotation"
    if locator.endswith(":notes"):
        return "notes"
    return "body"


def _table(locator):
    return bool("!" in locator or ":cell:" in locator or re.fullmatch(r"t:\d+:\d+:\d+", locator))


def _contexts(slots, loc):
    if _table(loc):
        context = binding_context(slots, loc)
        if ":cell:" in loc or re.fullmatch(r"t:\d+:\d+:\d+", loc):
            stem, _, col = loc.rsplit(":",2)
            corner = str(slots.get(f"{stem}:1:1", ""))
            head = str(slots.get(f"{stem}:1:{col}", "")).strip()
            tag = re.search(r"\b(option|case|scenario)s?\b", corner, re.I)
            if tag and re.fullmatch(r"[A-H]|\d+",head):
                context += " " + tag[1] + " " + head
            explicit = re.search(r"(?:USD|EUR|GBP|CNY|[$€£])\s*(?:000s?|thousands?|millions?|billions?|[kmb]n?)\b", context,re.I)
            if not explicit and not _roles(context) & {"sweep","amortization","fee","spread","leverage","ratio"}:
                context += " " + corner
        return context, ""
    if loc.startswith("slide:"):
        title = slots.get(loc.split(":shape:")[0] + ":shape:1", "")
    else:
        title = ""
    return "", str(title)


def _parts(slots, loc):
    key, _, line = loc.partition("#line:")
    raw = slots.get(key)
    if raw is None:
        return []
    if line:
        lines = str(raw).splitlines()
        if not line.isdigit() or not 0 < int(line) <= len(lines):
            return []
        raw = lines[int(line)-1]
    if _table(loc):
        return [str(raw)]
    return [s.strip() for s in re.split(r"\n+|(?<=[.!?])\s+(?=[A-Z])", str(raw)) if s.strip()]


def _number(v):
    try:
        n = Decimal(str(v).replace(",", "").strip())
    except InvalidOperation:
        return None
    return n if n.is_finite() else None


def _cases(text):
    return {m[1].lower() for m in re.finditer(r"\b(?:case|scenario|option)[ _-]+([A-H]|\d+)\b", text, re.I)}


def _periods(text):
    return {m[0].upper().replace(" ", "") for m in re.finditer(r"\b(?:FY\s*\d{4}|Q[1-4]\s*\d{4})\b", text, re.I)}


def _roles(text):
    text = str(text).replace("_", " ").lower()
    roles = metrics(text)
    for key, pattern in (("sweep", r"\bsweep"), ("facility", r"\bcommitment"),
                         ("tenor", r"\btenor"), ("headroom", r"\bheadroom"),
                         ("floor", r"\b(?:minimum|floor)"), ("cap", r"\b(?:maximum|cap)"),
                         ("net debt", r"\bnet debt"), ("ratio", r"\bdebt service\s+coverage")):
        if re.search(pattern, text):
            roles.add(key)
    return roles


def _native_quantities(fragment, context, document_unit):
    result = contextual_quantities(fragment, document_unit, context)
    # The unit must travel with the value or be present in its own row/column.
    # This normalizes a witnessed point for preservation. It cannot authorize
    # an original binding or turn a unitless proposal into a proved fact.
    if _number(fragment) is not None and len(result) == 1 and result[0].dimension is None:
        if re.search(r"%|\bpercent(?:age)?\b|\bper cent\b", context, re.I):
            result = quantities(str(fragment) + "%")
        elif re.search(r"\bratio\b|\([x×]\)", context, re.I):
            result = quantities(str(fragment) + "x")
    return result


def _dim(q):
    return "USD" if q.dimension == "dollar" else q.dimension


def _producer(dag, r):
    node = dag.get(r["claim_id"])
    p = node["payload"]
    if not is_business_claim(node):
        return None
    source = dag.get(p["source_id"])
    if source["kind"] != "source" or source["payload"]["text"][p["start"]:p["end"]] != p["quote"]:
        return None
    return p


def _denied_amount(text, q):
    return bool(re.search(r"\b(?:not|never|instead of|incorrectly (?:reported|shown) as)\s*(?:[$€£]|USD|EUR|GBP)?\s*$",
                          text[:q.start],re.I))


def _native_quant(qs, r, claim):
    expected = _number(r["expected"])
    if expected is None:
        return None
    from .native_grounding import claim_quantities
    sources = claim_quantities(claim)
    matches = []
    for q in qs:
        if not q.dimension:
            continue
        # Family/currency equality is strict; a dollar figure cannot stand
        # for a rate merely because the source row also contains that number.
        if any(s.value == expected and _dim(s) == _dim(q) and s.canonical == q.canonical for s in sources):
            matches.append(q)
        elif q.value == expected and _dim(q) == "multiple" and re.search(r"\bratio\b|\bto 1(?:\.0+)?\b",
                                                                       claim["quote"], re.I):
            matches.append(q)
        elif r.get("calculation_receipt"):
            from .business_calculation import _quantity_kind, _unit
            _, factor = _unit(r["unit"])
            if _dim(q) == _quantity_kind(r["unit"]) and q.canonical == expected * factor:
                matches.append(q)
    unique = {(_dim(q), q.canonical): q for q in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _typed_quantity(witness, frag):
    # Typed witnesses are emitted by the original-to-native table verifier
    # for this exact staged version, never supplied by the candidate.
    native = witness["native_text"].strip()
    if not native or native not in frag:
        return None
    value = _number(witness["displayed_number"])
    monetary = re.fullmatch(r"(USD|EUR|GBP|CNY) x ([0-9]+)", witness["native_unit"])
    if monetary:
        dim, scale = monetary[1], Decimal(monetary[2])
    elif witness["native_unit"] in {"percent","bps"}:
        dim = "rate"
        scale = Decimal(".01") if witness["native_unit"] == "percent" else Decimal(".0001")
    else:
        return None
    q = next((q for q in quantities(native) if q.value == value), None)
    return replace(q,dimension=dim,scale=scale) if q else None


def _consumer_signature(slots, loc):
    sheet, _, cell = loc.rpartition("!")
    if not sheet:
        return ""
    column, row = re.fullmatch(r"([A-Z]+)(\d+)", cell).groups()
    label, header = slots.get(f"{sheet}!A{row}"), slots.get(f"{sheet}!{column}1")
    return (str(sheet), str(label or ""), str(header or "")) if label or header else ("locator",loc)


def _readers(dependencies, slots, loc):
    return sorted({tuple(_consumer_signature(slots, consumer)) for consumer, inputs in dependencies.items()
                   if loc in inputs and "!" in consumer})


def _collect(dag, p, slots, conflicts, contract, sha, as_of=None):
    finance = dag.get(p["finance_receipt_id"])["payload"] if p.get("finance_receipt_id") else {}
    if finance.get("input_sha256") != sha:
        finance = {}
    verified = {}
    for a in p.get("assertion_checks", []):
        if a["status"] == "verified":
            for cid in set(a["claim_ids"]):
                verified.setdefault((a["locator"],cid),[]).append(a)
    typed = {(x["locator"],x["claim_id"],tuple(x.get("value_path",[]))):x for x in p.get("typed_table_witnesses",[])}
    terms = [r for r in finance.get("checks",[]) if r.get("claim_id") and r.get("status")=="verified" and r.get("rule")=="released_term"]
    failed = {(r.get("locator"), r.get("bound_ledger_claim_id", r.get("new_claim_id")),
               tuple(r.get("bound_value_path", []))) for r in conflicts}
    result = {}
    doc_unit = document_amount_unit(slots)
    rules = contract.get("rules",[]) + p.get("bindings",[]) + terms
    if not rules:
        return []
    active = {c["id"] for c in dag.claim_at(as_of=as_of,valid_at=contract.get("valid_at"))}
    for rule in rules:
        cid, loc = rule["claim_id"], rule["locator"]
        path = tuple(rule.get("value_path", []))
        key = (loc,cid,path)
        if cid not in active or key in failed or rule.get("unit_validation_failed") and key not in typed:
            continue
        checks = verified.get((loc.split("#line:")[0],cid),[])
        term = rule in terms
        strong = key in typed or term
        if not checks and not strong and not rule.get("calculation_receipt"):
            continue
        claim = _producer(dag,rule)
        if not claim:
            continue
        context, title = _contexts(slots,loc)
        for frag in _parts(slots,loc):
            events = [a for a in checks if a["fragment"].strip() == frag]
            if not (events or strong or rule.get("calculation_receipt")):
                continue
            qs = _native_quantities(frag,context,doc_unit)
            quantity = _typed_quantity(typed[key],frag) if key in typed else _native_quant(qs, rule, claim)
            if quantity and _denied_amount(frag,quantity):
                quantity = None
            kinds = {a["kind"] for a in events} - {"amount"}
            if quantity is None and not kinds:
                continue
            semantic = " ".join(map(str,path)) or str(claim.get("field",""))
            if term:
                semantic = str(rule.get("field",semantic))
            roles = _roles(semantic)
            here = context+" "+frag
            role = roles & _roles(here)
            if key in typed:
                role = _roles(typed[key]["metric"]) or {typed[key]["metric"]}
            # For bounds "minimum"/"maximum" identifies the side of a test.
            # A broad historical assertion cannot create a new metric.
            if not role and quantity and not strong:
                continue
            scenario = _cases(str(claim["entity"])+" "+str(claim["field"])+" "+semantic)
            if not scenario and (claim.get("scope",{}).get("option") or claim.get("scope",{}).get("scenario")):
                scenario = _cases("option "+str(claim["scope"].get("option") or claim["scope"]["scenario"]))
            entities = semantic_labels(claim["entity"]) - {"inc","llc","holdings","opco","digital","infrastructure",
                                                         "segment","refinancing","bank","facility"}
            owner = entities & semantic_labels(title+" "+here)
            period = _periods(str(claim.get("scope",{}).get("period",""))) & _periods(title+" "+here)
            if key in typed:
                witness = typed[key]
                period |= _periods(witness.get("period",""))
                row_owner = witness.get("row_label")
                if row_owner:
                    owner |= semantic_labels(row_owner)
            readers = _readers(finance.get("dependencies",{}), slots,loc)
            for kind in (["amount"] if quantity is not None else []) + sorted(kinds):
                point = {"claim_id":cid,"value_path":list(path),"kind":kind,"origin_sha256":sha,
                         "locator":loc,"fragment":frag,"context":context,"channel":_channel(loc),
                         "roles":sorted(role), "scenarios":sorted(scenario),"entities":sorted(owner),
                         "periods":sorted(period),"readers":[list(s) for s in readers],"source_id":claim["source_id"],
                         "start":claim["start"],"end":claim["end"]}
                if key in typed:
                    witness = typed[key]
                    point.update(start=witness["source_start"],end=witness["source_end"],
                                 original_locator=witness.get("source_location"))
                if kind=="amount":
                    point.update(dimension=_dim(quantity),canonical=str(quantity.canonical.normalize()))
                result.setdefault(_key(point),point)
    return list(result.values())


def _key(point):
    return (point["claim_id"],tuple(point["value_path"]),point["kind"],point["channel"],
            point.get("fragment") if point["kind"]!="amount" else None,
            point.get("dimension"),point.get("canonical"),tuple(point["scenarios"]),tuple(map(tuple,point["readers"])))


def _find(point, slots, doc_unit, dependencies):
    for loc in sorted(slots,key=lambda key:key != point["locator"]):
        if _channel(loc) != point["channel"]:
            continue
        context, title = _contexts(slots,loc)
        for frag in _parts(slots,loc):
            nearby = context+" "+frag
            scope = nearby+" "+title
            if (point["periods"] and not set(point["periods"]) <= _periods(scope)
                    or point["entities"] and not set(point["entities"]) <= semantic_labels(scope)):
                continue
            if point["scenarios"] and _cases(nearby) != set(point["scenarios"]):
                continue
            if not set(map(tuple,point["readers"])) <= set(_readers(dependencies,slots,loc)):
                continue
            if frag==point["fragment"] and context==point["context"] and (
                    point["kind"]!="amount" or any(_dim(q)==point["dimension"] and q.canonical==Decimal(point["canonical"])
                          for q in _native_quantities(frag,context,doc_unit))):
                return loc
            if point["kind"]!="amount" or not point["roles"]:
                continue
            for q in _native_quantities(frag,context,doc_unit):
                if _dim(q)!=point["dimension"] or q.canonical!=Decimal(point["canonical"]) or _denied_amount(frag,q):
                    continue
                # Avoid associating an adjacent, different metric's number
                # with this point just because a sentence contains them both.
                before = frag[:q.start]
                after = frag[q.end:]
                before = re.split(r"[;|\n]|\band\b|\bversus\b|\bvs\.?\s", before, flags=re.I)[-1]
                after = re.split(r"[;|\n]|\band\b|\bversus\b|\bvs\.?\s",after,flags=re.I)[0]
                if re.search(r"\d",after):
                    after = re.split(r"\d",after,maxsplit=1)[0]
                if re.search(r"\d",before):
                    before = re.split(r"\d+",before)[-1]
                if set(point["roles"]) <= _roles(context+" "+before+" "+after):
                    return loc
    return None


def assess_replacement(dag, p, contract, slots, sha, conflicts, read_slots, *, as_of=None):
    """Assess an actual byte version; never publish, amend or repair a DAG event."""
    scope = _scope(contract)
    prior = {}
    prior_nodes = [n for n in dag.nodes("artifact_check",as_of=as_of)
                   if n["payload"]["path"]==p["path"] and n["payload"]["purpose"]==p["purpose"]
                   and n["payload"]["stage"]==p["stage"]]
    for n in reversed(prior_nodes):
        q = n["payload"]
        c = dag.get(q["contract_id"])["payload"]
        if _scope(c)!=scope:
            continue
        receipt = q.get("protected_replacement")
        if receipt is None:
            if not q.get("sha256"):
                continue
            old_slots = read_slots(q["path"],q["sha256"])
            observed = _collect(dag,q,old_slots,q.get("conflicts",[]),c,q["sha256"],as_of=n["seq"])
        else:
            observed = receipt["points"]
        for point in observed:
            prior.setdefault(_key(point),{**point,"origin_check":point.get("origin_check") or n["id"]})
        if receipt:
            break   # Each new receipt includes all earlier proved points in scope.
    finance = dag.get(p["finance_receipt_id"])["payload"] if p.get("finance_receipt_id") else {}
    dependencies = finance.get("dependencies",{}) if finance.get("input_sha256")==sha else {}
    current = _collect(dag,p,slots,conflicts,contract,sha,as_of=as_of) if sha else []
    current_by_key = {_key(point):point for point in current}
    active = {n["id"] for n in dag.claim_at(as_of=as_of,valid_at=contract.get("valid_at"))} if prior else set()
    revisions = {n["payload"]["old_id"]:n for n in dag.nodes("revision",as_of=as_of)
                 if n["payload"]["status"]=="accepted" and n["payload"]["evidence_ids"]
                 and n["payload"]["new_id"] in active and n["payload"]["old_id"] not in active
                 and n["payload"]["purpose"] in (None, contract["purpose"])
                 and is_business_claim(dag.get(n["payload"]["new_id"]))} if prior else {}
    doc_unit = document_amount_unit(slots)
    checked, missing, carried, retired = [], [], {}, []
    for key, point in prior.items():
        rev = revisions.get(point["claim_id"])
        if rev and (rev["payload"]["relation"]=="retract" or
                    rev["payload"]["relation"] in ("value","scope") and
                    any(q["claim_id"]==rev["payload"]["new_id"] and q["channel"]==point["channel"]
                        and q["value_path"]==point["value_path"] and q["kind"]==point["kind"]
                        and q["readers"]==point["readers"] for q in current)):
            retired.append({"claim_id":point["claim_id"],"revision_id":rev["id"],"locator":point["locator"],
                            "origin_sha256":point["origin_sha256"]})
            continue
        counterpart = current_by_key.get(key)
        if not counterpart and point["kind"] != "amount":
            from .native_grounding import dates
            candidates = [fresh for fresh in current if fresh["kind"]==point["kind"] and fresh["claim_id"]==point["claim_id"]
                          and fresh["value_path"]==point["value_path"] and fresh["channel"]==point["channel"]
                          and fresh["scenarios"]==point["scenarios"] and fresh["readers"]==point["readers"]
                          and dates(point["fragment"])==dates(fresh["fragment"])
                          and semantic_labels(point["fragment"]) <= semantic_labels(fresh["fragment"])]
            counterpart = candidates[0] if len(candidates)==1 else None
        # Even a new binding for the same number cannot erase the historical
        # entity/period or turn a displayed point into a different role.
        if counterpart and all(set(point[k]) <= set(counterpart[k]) for k in ("roles","entities","periods")):
            location = counterpart["locator"]
        else:
            location = _find(point,slots,doc_unit,dependencies)
        record = {k:point[k] for k in ("claim_id","value_path","source_id","start","end","origin_check","origin_sha256","locator")}
        if point.get("original_locator"):
            record["original_locator"] = point["original_locator"]
        if location:
            checked.append({**record,"present_at":location})
        else:
            missing.append({**record,"kind":point["kind"],"roles":point["roles"],"dimension":point.get("dimension"),
                            "canonical":point.get("canonical"),"scenarios":point["scenarios"],"readers":point["readers"]})
        carried[key] = point
    for key,point in current_by_key.items():
        carried.setdefault(key,point)
    return {"scope":scope,"sha256":sha,"points":list(carried.values()),"retained":checked,"missing":missing,
            "retired":retired}

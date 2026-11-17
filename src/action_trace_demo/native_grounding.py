"""Parse scalar financial evidence while preserving units and original offsets."""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
import re

_NUMBER = re.compile(r"(?<![\w.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?!\d|\.\d)")
_MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_DATE = rf"(?:\b\d{{1,2}}\s+{_MONTH}(?:\s+\d{{4}})?\b|\b{_MONTH}\s+\d{{1,2}}(?:,?\s+\d{{4}})?\b|\b\d{{4}}-\d{{2}}-\d{{2}}\b)"
_META = re.compile(rf"(?:\b(?:information|data|evidence)\s+cut[- ]?off|\bas (?:of|at)|\bprepared(?: on)?|\bfieldwork\s+(?:closed|finished|ended)(?: on)?|\breport date)\s*:?\s*{_DATE}|"
                   r"\bpage\s+\d+(?:\s*(?:of|/)\s*\d+)?\b|\b(?:version|revision)\s+\d+(?:\.\d+)*\b|"
                   r"(?m:^\s*\d{1,2}[.)]\s+)|(?:\$|USD\s*)0{3}s?\b|\b(?:FY|Q)[ -]?\d{1,4}\b|\b20\d{2}/\d{2}\b", re.I)
_SCALE = re.compile(r"\s*(?:in\s+)?(?:(?:USD|US\$|EUR|GBP|CNY|RMB|dollars?)\s*)?(thousands?|millions?|billions?|bn|mm|mn|k|m)\b",re.I)
_CURRENCY = re.compile(r"(?:USD|US\$|EUR|GBP|CNY|RMB|\$|€|£|¥)",re.I)
_METRIC = {
    "debt service":r"debt[ -]+service", "revenue":r"revenues?|sales", "spread":r"spreads?",
    "ebitda":r"EBITDA", "interest":r"interest (?:expense|paid|payment|coverage|rate)",
    "facility":r"facilit(?:y|ies)|senior commitment", "amortization":r"amorti[sz]ation",
    "leverage":r"leverage", "ratio":r"\bDSCR\b", "capex":r"capex|capital expenditure",
    "cash":r"cash (?:balance|flow|available)", "premium":r"premium", "deductible":r"deductible",
    "insurance":r"coverage limit|insured sum|insured value|policy limit", "fee":r"\bfees?\b",
}


@dataclass(frozen=True)
class Quantity:
    value: Decimal
    dimension: str | None
    scale: Decimal
    start: int
    end: int
    context: str

    @property
    def canonical(self):
        return self.value * self.scale


def quantities(text):
    text=str(text)
    skip=[m.span() for m in _META.finditer(text)]
    output=[]
    for m in _NUMBER.finditer(text):
        if any(a <= m.start() < b for a,b in skip):
            continue
        left=text[max(0,m.start()-30):m.start()]
        right=text[m.end():m.end()+32]
        val=Decimal(m[0].replace(",",""))
        dimension=None
        scale=Decimal(1)
        # Prefer the unit attached to this number. Restrict prefix searches
        # to the end of the preceding text to avoid borrowing a prior unit.
        if re.match(r"\s*(?:bps?\b|basis points?\b)",right,re.I):
            dimension,scale="rate",Decimal("0.0001")
        elif re.match(r"\s*(?:%|per\s+cent\b|percent(?:age)?\b|pct\b)",right,re.I):
            dimension,scale="rate",Decimal("0.01")
        elif re.match(r"\s*(?:x|times)\b",right,re.I):
            dimension="multiple"
        else:
            suffix_currency=_CURRENCY.match(right.lstrip().removeprefix("in ").lstrip())
            prefix_currency=re.search(r"(USD|US\$|EUR|GBP|CNY|RMB|\$|€|£|¥)\s*$",left,re.I)
            token=(suffix_currency or prefix_currency)
            if token:
                currency=token[0].strip().upper()
                dimension={"US$":"USD","$":"dollar","€":"EUR","£":"GBP","¥":"yuan","RMB":"CNY"}.get(currency,currency)
            scale_match=_SCALE.match(right)
            if scale_match:
                unit=scale_match[1].lower()
                scale=Decimal(1_000_000_000 if unit.startswith("b") else 1_000_000 if unit.startswith("m") else 1_000)
            if dimension is None and scale_match:
                dimension="amount"
        a=max(0,m.start()-160)
        b=min(len(text),m.end()+130)
        output.append(Quantity(val,dimension,scale,m.start(),m.end(),text[a:b]))
    return output


def compatible(a,b):
    if a.dimension==b.dimension:
        return True
    if {a.dimension,b.dimension} <= {"USD","dollar"}:
        return True
    return a.scale==b.scale==1 and (a.dimension is None or b.dimension is None)


def amounts_match(a,b):
    # Unspecified dollar currencies cannot authorize an exchange-rate conversion.
    return compatible(a,b) and a.canonical==b.canonical


def claim_quantities(claim):
    """Use a recorded field's denomination for bare amounts in its original quote.

    Explicit suffixes in the original, such as million or percent, retain
    their own scale. A period, an entity name or a number's magnitude cannot
    establish the denomination. The caller retains the claim/source relation
    so this normalization never creates an independent witness.
    """
    result=quantities(claim.get("quote", ""))
    scope=claim.get("scope",{})
    explicit=scope.get("units",scope.get("unit","")) if isinstance(scope,dict) else ""
    label=str(explicit or claim.get("field", "")).casefold().replace("_", " ")
    currency=next((dimension for token,dimension in (("usd","USD"),("us$","USD"),("$","dollar"),
                    ("eur","EUR"),("€","EUR"),("gbp","GBP"),("£","GBP"),("cny","CNY"),("rmb","CNY"))
                   if token in label),None)
    scale=None
    if re.search(r"(?:\bthousands?\b|(?<!\d)0{3}s?\b|\b(?:usd|eur|gbp)\s*k\b)",label):
        scale=Decimal(1000)
    elif re.search(r"\b(?:millions?|mn|mm)\b",label):
        scale=Decimal(1_000_000)
    elif re.search(r"\b(?:billions?|bn)\b",label):
        scale=Decimal(1_000_000_000)
    if currency is None or scale is None:
        return result
    output=[]
    for q in result:
        same_currency=q.dimension==currency or {q.dimension,currency}<={"USD","dollar"}
        if q.dimension in {None,"amount"} or same_currency:
            dimension=currency if q.dimension in {None,"amount"} else q.dimension
            output.append(replace(q,dimension=dimension,scale=scale if q.scale==1 else q.scale))
        else:
            output.append(q)
    return output


def document_amount_unit(slots):
    """Find a unique, document-wide denomination declared in actual native text.

    Local table-unit cells and prose with entity/segment qualifiers do not
    establish a document-wide unit. Each accepted declaration is a complete
    sentence and retains the native locator used to observe it.
    """
    currency=r"(?P<currency>USD|US dollars?|US\$|\$|EUR|euros?|€|GBP|pounds? sterling|£|CNY|RMB)"
    scale=r"(?P<scale>thousands?|millions?|billions?|\$?0{3}s?)"
    amount_lead=r"(?:(?:all\s+)?(?:financial\s+|monetary\s+)?(?:amounts|figures)\s+(?:are\s+)?(?:stated\s+|shown\s+|expressed\s+)?(?:in\s+)?)?"
    pattern=re.compile(amount_lead+currency+r"\s+"+scale+r"\s+unless\s+(?:otherwise\s+)?(?:stated|noted|specified)\.?",re.I)
    found={}
    for locator,value in slots.items():
        if not isinstance(value,str) or value.startswith('='):
            continue
        for sentence in re.split(r'(?<=[.;])\s+|\n',value):
            match=pattern.fullmatch(sentence.strip())
            if not match:
                continue
            curr=match['currency'].upper()
            dimension='USD' if curr in {'USD','US DOLLAR','US DOLLARS','US$','$'} else (
                'EUR' if curr in {'EUR','EURO','EUROS','€'} else 'GBP' if curr in {'GBP','POUND STERLING','POUNDS STERLING','£'} else 'CNY')
            raw=match['scale'].lower()
            multiplier=1_000_000_000 if raw.startswith('b') else 1_000_000 if raw.startswith('m') else 1000
            found.setdefault((dimension,multiplier),[]).append({'locator':locator,'quote':sentence.strip()})
    if len(found)!=1:
        return None
    (dimension,scale),witnesses=next(iter(found.items()))
    return {'dimension':dimension,'scale':scale,'declarations':witnesses}


def document_quantities(actual,unit_context):
    """Apply the file's declared scale only to currency-labelled bare amounts."""
    result=quantities(actual)
    if not unit_context:
        return result
    dimension=unit_context['dimension']
    output=[]
    for q in result:
        same=q.dimension==dimension or {q.dimension,dimension}<={'USD','dollar'}
        if same and q.scale==1:
            output.append(replace(q,dimension=dimension,scale=Decimal(unit_context['scale'])))
        else:
            output.append(q)
    return output


def metrics(text):
    return {kind for kind,pattern in _METRIC.items() if re.search(pattern,str(text),re.I)}


def entity_anchor(name,context):
    excluded={"company","corporation","holdings","group","limited","inc","llc","project","fund","platform",
              "scenario","agent","issuer","entity","financial","report"}
    tokens=[t for t in re.findall(r"[\w]+",str(name).casefold()) if len(t)>2 and t not in excluded]
    words=set(re.findall(r"[\w]+",str(context).casefold()))
    return bool(tokens and tokens[0] in words)

_DATE_CANON = re.compile(_DATE,re.I)
_STOP = set("the of for on a an to and or at in with is was are were alpha company portfolio status programme program "
            "current amount claims source summary note issue report project selected number original there material "
            "received recommended review information financial as provided".split())


def semantic_labels(text):
    return {w for w in re.findall(r"[a-z]{3,}",str(text).lower()) if w not in _STOP}


def dates(text):
    """Comparable date representations; an absent year is not inferred."""
    import datetime
    result=set()
    for m in _DATE_CANON.finditer(str(text)):
        txt=m[0].replace(",","")
        for fmt in ("%Y-%m-%d","%d %B %Y","%d %b %Y","%B %d %Y","%b %d %Y","%d %B","%d %b","%B %d","%b %d"):
            try:
                parsed=datetime.datetime.strptime(txt,fmt)
            except ValueError:
                continue
            result.add((parsed.year if "%Y" in fmt else None,parsed.month,parsed.day))
            break
    return result


def same_calendar_date(value,text):
    """Accept an alternative spelling of one complete, unambiguous date."""
    if not isinstance(value,str) or not _DATE_CANON.fullmatch(value.strip()):
        return False
    expected=dates(value)
    observed=dates(text)
    return (len(expected)==len(observed)==1 and expected==observed
            and all(year is not None for year,month,day in expected))


def structured_parts(value):
    """Conjunctive status/date or text clauses; all retained components must match."""
    if not isinstance(value,str) or ";" not in value:
        return None
    parts=[]
    for part in value.split(";"):
        p=part.strip()
        if not p:
            return None
        ds=dates(p)
        if ds:
            remainder=_DATE_CANON.sub("",p).strip().lower()
            if remainder not in {"","target","target date","due","due date"}:
                return None
            parts.append(("date",ds))
        else:
            parts.append(("text",p))
    return parts


def all_parts(value,text):
    parts=structured_parts(value)
    if not parts:
        return False
    return all((v<=dates(text) if kind=="date" else
                bool(re.search(r"(?<!\w)"+re.escape(v)+r"(?!\w)",text,re.I))) for kind,v in parts)

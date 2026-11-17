"""Ground a PDF amount in a single printed fiscal-year table cell on both sides."""
from decimal import Decimal
import re

from .native_grounding import entity_anchor, metrics


_YEAR = re.compile(r"(?<!\w)(?:FY\s*)?(20\d\d)(?!\w)", re.I)
_UNIT = re.compile(r"\b(USD|EUR|GBP|CNY)\s*(?:in\s+)?(000s?|thousands?|millions?|billions?)\b|([$€£])\s*(000s?|thousands?|millions?|billions?)\b", re.I)
_NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?![\w.])")
_BASIS = re.compile(r"\b(covenant|certified|consolidated|pro forma|projected|forecast|budgeted|adjusted)\b", re.I)


def _norm(value):
    return " ".join(re.findall(r"[\w]+",str(value).casefold().replace("_", " ")))


def _unit(line):
    matches = _UNIT.findall(line)
    if len(matches) != 1:
        return None
    code, named, sign, amount = matches[0]
    # An unqualified dollar is compatible with the same printed dollar convention.
    # Do not use it to convert distinct currencies.
    currency = code.upper() or sign
    scale = (named or amount).casefold()
    power = 6 if scale.startswith("million") else 9 if scale.startswith("billion") else 3
    return currency, power


def _basis(text):
    return {x.casefold() for x in _BASIS.findall(text)}


def _cells(text):
    """Require one explicit denomination and an ordered period header above contiguous labelled rows."""
    lines = text.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1]+len(line))
    for i, line in enumerate(lines):
        years = _YEAR.findall(line)
        unit = _unit(line)
        if not unit or len(years)<2 or len(years)!=len(set(years)):
            continue
        extra_columns = _YEAR.sub("",_UNIT.sub("",line))
        if re.search(r"\d|%",extra_columns):
            continue
        named = metrics(line)
        title = line
        if not named:
            # An extracted source table may put the metric in the immediately
            # preceding paragraph, with a short preparation/date caption.
            title = next((t for t in reversed(lines[max(0,i-6):i]) if metrics(t)), "")
            named = metrics(title)
        if len(named)!=1:
            continue
        for index in range(i+1,len(lines)):
            row = lines[index]
            numbers = list(_NUMBER.finditer(row))
            if len(numbers)!=len(years) or not numbers:
                break
            label = _norm(row[:numbers[0].start()])
            if not label or re.search(r"[^\s|]",row[numbers[-1].end():]):
                break
            if any(re.search(r"[^\s|]",row[a.end():b.start()]) for a,b in zip(numbers,numbers[1:])):
                break
            for period, m in zip(years,numbers):
                yield {"period":period,"row":label,"metric":next(iter(named)),"unit":unit,"basis":_basis(title),
                       "value":Decimal(m[0].replace(",","")),"line":index+1,"header":i+1,"start":starts[index]+m.start(),
                       "end":starts[index]+m.end(),"header_text":line.strip(),"row_text":row.strip()}


def pdf_table_witness(slots,rule,claim,source_text):
    loc = re.fullmatch(r"(page:[1-9]\d*)(?:#line:([1-9]\d*))?",rule["locator"])
    if not loc or claim.get("source_key","").startswith(("native:", "artifact-observation:")):
        return None
    if not str(claim.get("locator","")).split("#",1)[0].casefold().endswith(".pdf"):
        return None
    raw = slots.get(loc[1])
    scope = claim.get("scope",{})
    period = set(_YEAR.findall(str(scope.get("period",""))))
    roles = metrics(claim.get("field",""))
    if len(period)!=1 or len(roles)!=1 or not isinstance(raw,str):
        return None
    start,end=claim["start"],claim["end"]
    if source_text[start:end]!=claim["quote"]:
        return None
    value = Decimal(str(rule["expected"]).replace(",",""))
    identity = _norm(" ".join(rule.get("value_path",())) or " ".join(
        str(claim.get(k,"")) for k in ("entity","field","claim")))
    # Page markers come from the trusted native PDF reader; projected document
    # offsets correspond to the recorded claim's actual quote offsets.
    pages=list(re.finditer(r"(?m)^\[page (\d+)\]\n",source_text))
    page_index=next((i for i,m in enumerate(pages) if m.end()<=start and end<=(pages[i+1].start() if i+1<len(pages) else len(source_text))),None)
    if page_index is None:
        return None
    page=pages[page_index]
    source_page=source_text[page.end():pages[page_index+1].start() if page_index+1<len(pages) else len(source_text)]
    # A table continued on a later PDF page can inherit the report issuer
    # named in full on its title page. A source citation alone is insufficient.
    cover = source_text[pages[0].end():pages[1].start() if len(pages)>1 else len(source_text)]
    named_document = _norm(claim["entity"]) in _norm(cover)
    if not (named_document or entity_anchor(claim["entity"],source_page)) or not entity_anchor(claim["entity"],raw):
        return None
    source_cells=[c for c in _cells(source_page) if c["period"] in period and c["value"]==value and c["metric"] in roles
                  and start<=c["start"]+page.end() and c["end"]+page.end()<=end
                  and re.search(r"\b"+re.escape(c["row"])+r"\b",identity)]
    if len(source_cells)!=1:
        return None
    old=source_cells[0]
    matches=[c for c in _cells(raw) if (not loc[2] or int(loc[2])==c["line"])
             and all(c[k]==old[k] for k in ("period","row","unit","basis","metric","value"))]
    if len(matches)!=1:
        return None
    new=matches[0]
    # An independently printed amount elsewhere on the page needs its own
    # binding. A whole-page locator cannot disambiguate repeated numbers.
    if not loc[2] and sum(Decimal(m[0].replace(",",""))==value for m in _NUMBER.finditer(raw))!=1:
        return None
    return {"source_id":claim["source_id"], "source_page":int(page[1]), "source_locator":claim["locator"],
            "source_start":old["start"]+page.end(),"source_end":old["end"]+page.end(),
            "source_header":old["header_text"],"source_header_line":old["header"],"source_row":old["row_text"],
            "native_locator":f"{loc[1]}#line:{new['line']}","native_start":new["start"],"native_end":new["end"],
            "native_header":new["header_text"],"native_header_line":new["header"],"native_row":new["row_text"],"period":new["period"],"metric":new["metric"],
            "row_label":new["row"],"currency":new["unit"][0],"scale":10**new["unit"][1],"value":str(value)}

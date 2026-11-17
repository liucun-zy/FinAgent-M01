"""Match released table cells to financial values displayed in native slides."""
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
import re

from .business_calculation import _declared_scale
from .native_grounding import entity_anchor


_OPTION = re.compile(r"\b(?:option|case|scenario)\s+([a-z]|\d+)\b", re.I)
_NUM = r"[-+]?\d[\d,]*(?:\.\d+)?"
_CASH_UNIT = re.compile(r"(?:(USD|EUR|GBP|CNY|\$)\s*(000s?|thousands?|millions?|billions?))\b", re.I)
_TERMS = {
    "commitment": r"\b(?:total |facility )?commitment\b",
    "sweep": r"\b(?:cash flow |cash )?sweep\b",
    "fee": r"\b(?:arrangement )?fee\b",
    "amortization": r"\b(?:scheduled )?amorti[sz]ation\b",
    "spread": r"\b(?:credit )?spread\b",
}


def _normalize(text):
    return " ".join(str(text).replace("_", " ").casefold().split())


def _term(text):
    text = _normalize(text)
    found = [k for k, pat in _TERMS.items() if re.search(pat, text)]
    return found[0] if len(found) == 1 else None


def _money_unit(text):
    matches = {
        (
            code.upper().replace("$", "USD"),
            1000
            if unit.lower().startswith(("0", "thousand"))
            else 10**6
            if unit.lower().startswith("million")
            else 10**9,
        )
        for code, unit in _CASH_UNIT.findall(text)
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _basis(text):
    return set(re.findall(r"\b(?:pro forma|forecast|budget|projected|covenant|certified)\b", _normalize(text)))


def _amount(text):
    if not re.fullmatch(_NUM, str(text).strip()):
        return None
    return Decimal(str(text).strip().replace(",", ""))


def _option(text):
    found = set(_OPTION.findall(_normalize(text)))
    return next(iter(found)).upper() if len(found) == 1 else None


def _qualifiers(text):
    """Cash sweep and fee percentages may share values but use different denominators."""
    result = set()
    for m in re.finditer(
        r"\b(excess cash flow|free cash flow|gross revenue|net revenue|EBITDA|commitment|outstanding principal|outstanding balance)\b",
        text,
        re.I,
    ):
        result.add(m[0].casefold())
    return result


def _term_table(slots, rule, claim, source):
    dest = re.fullmatch(r"(slide:(\d+):shape:\d+):cell:(\d+):(\d+)", rule["locator"])
    if not dest or int(dest[3]) <= 1 or int(dest[4]) <= 1:
        return None, None
    stem, slide, row, column = dest.groups()
    row_label = str(slots.get(f"{stem}:cell:{row}:1", ""))
    route = rule.get("value_path", [])
    wanted = _term(route[-1] if route else claim.get("field", ""))
    actual = _term(row_label)
    option = _option(claim.get("field", ""))
    if not option or wanted is None:
        return None, None
    # All scope-sensitive scalar rules in an options table must agree with its
    # actual row and column. Equal values in two options cannot identify a column.
    header = str(slots.get(f"{stem}:cell:1:{column}", "")).strip()
    table_title = str(slots.get(f"{stem}:cell:1:1", ""))
    selected = _option(header)
    if (
        not selected
        and re.search(r"\b(?:options?|cases?|scenarios?)\b", table_title, re.I)
        and re.fullmatch(r"[A-Z]|\d+", header)
    ):
        selected = header
    if selected != option or wanted != actual:
        return None, "The target row or financing option differs from this released term."
    if wanted not in {"commitment", "sweep", "fee", "amortization", "spread"}:
        return None, None
    dest_text = str(slots.get(rule["locator"], "")).strip()
    is_percent = wanted in {"sweep", "fee", "amortization"}
    if is_percent and "%" not in dest_text and not re.search(r"%|\bpercent(?:age)?\b", row_label, re.I):
        return None, "A bare financing percentage requires a percent mark or an explicit percent row heading."
    rows = defaultdict(dict)
    for m in re.finditer(r"(?m)^\[table (\d+) row (\d+)\] ([^\n]*)", source["text"]):
        spans = []
        pos = m.start(3)
        for cell_text in m[3].split("|"):
            spans.append((cell_text.strip(), pos, pos + len(cell_text)))
            pos += len(cell_text) + 1
        rows[int(m[1])][int(m[2])] = spans
    witnesses = []
    defined, unit_evidence = _declared_scale(source, claim)
    raw_value = _amount(rule["expected"])
    if raw_value is None:
        return None, None
    for table_id, table in rows.items():
        heads = table.get(1, [])
        choices = [i for i, (name, _, _) in enumerate(heads) if _option(name) == option]
        if len(choices) != 1:
            continue
        col = choices[0]
        for source_row, cells in table.items():
            if (
                source_row == 1
                or len(cells) != len(heads)
                or col >= len(cells)
                or not cells
                or _term(cells[0][0]) != wanted
            ):
                continue
            val, start, end = cells[col]
            if not (claim["start"] <= start and end <= claim["end"]):
                continue
            numeric_text = (
                re.sub(r"\s*(?:per annum|p\.a\.)$", "", dest_text, flags=re.I)
                if wanted == "amortization"
                else dest_text
            )
            if numeric_text != dest_text and not re.search(r"per annum|p\.a\.", val, re.I):
                continue
            target_number = re.fullmatch(r"(" + _NUM + r")\s*(%|bps?)?", numeric_text, re.I)
            if not target_number:
                continue
            suffix = (target_number[2] or "").lower()
            if (
                (is_percent and suffix not in {"", "%"})
                or (wanted == "spread" and suffix not in {"", "bp", "bps"})
                or (wanted == "commitment" and suffix)
            ):
                return None, "The destination cell displays a unit inconsistent with the term's dimension."
            if is_percent and not _qualifiers(row_label) <= _qualifiers(cells[0][0] + " " + val):
                return None, "The destination percentage specifies a different basis from the source term."
            printed = Decimal(target_number[1].replace(",", ""))
            if is_percent:
                match = re.match(r"^(" + _NUM + r")\s*%(?:\s|$)", val)
                unit = "percent"
                if not match or _amount(match[1]) != raw_value or printed != raw_value:
                    continue
            elif wanted == "spread":
                match = re.fullmatch(r"(" + _NUM + r")\s*bps?", val, re.I)
                unit = "bps"
                if (
                    not match
                    or _amount(match[1]) != raw_value
                    or printed != raw_value
                    or not re.search(r"bps?|basis points?", dest_text + " " + row_label, re.I)
                ):
                    continue
            else:
                source_amount = _amount(val)
                dest_unit = _money_unit(row_label) or _money_unit(table_title)
                if (
                    not defined
                    or not dest_unit
                    or source_amount != raw_value
                    or defined[0] != dest_unit[0]
                    or source_amount * defined[1] != printed * dest_unit[1]
                ):
                    continue
                unit = f"{dest_unit[0]} x {dest_unit[1]}"
            title = slots.get(f"slide:{slide}:shape:1", "")
            completed = re.search(
                r"^\s*(?:executed|committed|approved|funded)\s+(?:terms|financing|facility|commitment)"
                r"|\b(?:was|is|has been|terms are|fully)\s+(?:formally\s+)?(?:executed|committed|funded|approved)\b",
                str(title),
                re.I,
            )
            if completed and "indicative" in source["text"].lower():
                return None, "The source is indicative while the destination asserts a completed transaction."
            if not entity_anchor(
                claim["entity"], str(slots.get("slide:1:shape:1", "")) + " " + str(title)
            ) or not entity_anchor(claim["entity"], source["text"]):
                continue
            witnesses.append(
                {
                    "source_location": f"table:{table_id}:row:{source_row}:column:{col+1}",
                    "source_start": start,
                    "source_end": end,
                    "source_text": source["text"][start:end],
                    "source_row": cells[0][0],
                    "source_column": heads[col][0],
                    "source_unit": unit_evidence if wanted == "commitment" else val,
                    "native_text": dest_text,
                    "native_row": row_label,
                    "native_column": option,
                    "metric": wanted,
                    "native_unit": unit,
                    "displayed_number": str(printed),
                    "atomic_cell": True,
                }
            )
    return (
        (witnesses[0], None)
        if len(witnesses) == 1
        else (None, "A unique scoped source cell does not support this financing term.")
    )


def _metrics(label):
    return {
        "revenue": "revenue",
        "adjusted ebitda": "adjusted EBITDA",
        "ebitda": "EBITDA",
        "net income": "net income",
        "operating income": "operating income",
    }.get(_normalize(label).removeprefix("segment "))


def _model_text(raw, segment, metric, period, unit):
    """Recover a printed prefix and its explicitly defined parallel slash columns."""
    matches = []
    for line in str(raw).splitlines():
        prefix, sep, body = line.partition(":")
        if (
            not sep
            or set(re.findall(r"\bFY\s*(20\d\d)\b", prefix, re.I)) != {period}
            or _money_unit(prefix) != unit
            or not re.search(r"\bactuals?\b", prefix, re.I)
            or _basis(prefix)
        ):
            continue
        columns = None
        for section in body.split(";"):
            vals = section.strip().rstrip(".").split("/")
            named = [re.fullmatch(r"(.+?)\s+(" + _NUM + r")", x.strip()) for x in vals]
            if columns is None:
                if not all(named) or len(vals) < 2:
                    break
                first = re.fullmatch(
                    r"(.+?)\s+(adjusted EBITDA|revenue|net income|operating income|EBITDA)", named[0][1], re.I
                )
                if not first:
                    break
                row = first[1]
                columns = [_metrics(first[2]), *[_metrics(n[1]) for n in named[1:]]]
                if None in columns or len(columns) != len(set(columns)):
                    break
                measured = [_amount(n[2]) for n in named]
            else:
                if len(vals) != len(columns) or not named[0] or any(_amount(s) is None for s in vals[1:]):
                    break
                row = named[0][1]
                measured = [_amount(named[0][2]), *[_amount(s) for s in vals[1:]]]
            if _normalize(row) == _normalize(segment) and metric in columns:
                matches.append(
                    (measured[columns.index(metric)], section.strip(), prefix, columns.index(metric) + 1)
                )
    return matches[0] if len(matches) == 1 else None


def _excel_option_rate(slots, rule, claim, source, book):
    """Translate the stored Excel fraction only when the original cell really displays a percent."""
    where = re.fullmatch(r"(slide:\d+:shape:\d+):cell:(\d+):(\d+)", rule["locator"])
    if not where or int(where[2]) <= 1 or int(where[3]) <= 1:
        return None, None
    stem, row, col = where.groups()
    route = rule.get("value_path", [])
    metric = _term(route[-1] if route else claim["field"])
    if metric not in {"sweep", "fee", "amortization"}:
        return None, None
    option = _option(claim["field"]) or _option(str(claim.get("scope", {}).get("scenario", "")))
    if not option:
        return None, None
    label = str(slots.get(f"{stem}:cell:{row}:1", ""))
    header = str(slots.get(f"{stem}:cell:1:{col}", ""))
    displayed = str(slots.get(rule["locator"], "")).strip()
    if metric != _term(label) or option != _option(header):
        return None, "The target row or financing option differs from the source."
    if not re.search(r"%|\bpercent(?:age)?\b", displayed + " " + label, re.I):
        return None, "The destination has no displayed percentage or percent header."
    shown = re.fullmatch(r"(" + _NUM + r")\s*%?", displayed)
    if (
        not shown
        or not entity_anchor(claim["entity"], slots.get("slide:1:shape:1", ""))
        or not entity_anchor(claim["entity"], source["text"])
    ):
        return None, None
    sheet = None
    witnesses = []
    # Capture exact cell-coordinate occurrences within the immutable text quote.
    for line in re.finditer(r"(?m)^([^\n]+)", source["text"]):
        sheet_marker = re.fullmatch(r"\[sheet (.+?)\]", line[0])
        if sheet_marker:
            sheet = sheet_marker[1]
            continue
        if (
            not sheet
            or sheet not in book.sheetnames
            or line.start() > claim["end"]
            or line.end() < claim["start"]
        ):
            continue
        ws = book[sheet]
        for match in re.finditer(r"(?:^|\|\s*)([A-Z]+\d+)=(" + _NUM + r")(?=\s*(?:\||$))", line[0]):
            lo, hi = line.start() + match.start(2), line.start() + match.end(2)
            if not claim["start"] <= lo < hi <= claim["end"]:
                continue
            cell = ws[match[1]]
            # Literal/escaped % is a printed suffix without Excel scaling.
            fmt = re.sub(r'"[^"]*"|\\.', "", cell.number_format or "")
            if cell.data_type != "n" or not re.fullmatch(r"[#0,.]+%", fmt):
                continue
            stored = _amount(cell.value)
            if (
                stored != _amount(match[2])
                or stored != _amount(rule["expected"])
                or stored * 100 != _amount(shown[1])
            ):
                continue
            source_label = str(ws.cell(cell.row, 1).value)
            if (
                cell.column <= 1
                or _term(source_label) != metric
                or not _qualifiers(label) <= _qualifiers(source_label)
            ):
                continue
            header_cell = next(
                (
                    ws.cell(i, cell.column)
                    for i in range(cell.row - 1, 0, -1)
                    if _option(ws.cell(i, cell.column).value)
                ),
                None,
            )
            if not header_cell or _option(header_cell.value) != option:
                continue
            witnesses.append(
                {
                    "source_location": sheet + "!" + cell.coordinate,
                    "source_header": sheet + "!" + header_cell.coordinate,
                    "source_row": str(ws.cell(cell.row, 1).value),
                    "source_column": str(header_cell.value),
                    "source_text": match[2],
                    "source_start": lo,
                    "source_end": hi,
                    "source_format": cell.number_format,
                    "source_stored_value": str(stored),
                    "source_unit": "Excel numeric fraction with a percentage number format",
                    "metric": metric,
                    "native_row": label,
                    "native_column": option,
                    "native_text": displayed,
                    "native_unit": "percent",
                    "displayed_number": str(_amount(shown[1])),
                    "atomic_cell": True,
                }
            )
    return (
        (witnesses[0], None)
        if len(witnesses) == 1
        else (None, "No unique source cell proves this displayed percentage.")
    )


def _model(slots, rule, claim, source, book):
    dest = rule["locator"]
    route = rule.get("value_path", [])
    if len(route) != 2 or not re.fullmatch(r"slide:\d+:shape:\d+", dest):
        return None, None
    segment, field = route
    metric = _metrics(field)
    periods = set(re.findall(r"\bFY\s*(20\d\d)\b", str(claim.get("scope", {}).get("period", "")), re.I))
    if not metric or len(periods) != 1:
        return None, None
    period = next(iter(periods))
    unit, unit_evidence = _declared_scale(source, claim)
    if not unit:
        return None, None
    target = _model_text(slots.get(dest, ""), segment, metric, period, unit)
    if not target:
        structured = any(
            ":" in line
            and re.search(r"\bFY\s*20\d\d\b", line.partition(":")[0], re.I)
            and re.search(r"\b(?:actuals?|forecast|pro forma)\b", line.partition(":")[0], re.I)
            and "/" in line.partition(":")[2]
            for line in str(slots.get(dest, "")).splitlines()
        )
        if structured:
            return None, "The stated period, basis, unit or row/column does not match the quoted model cell."
        return None, None
    value, clause, prefix, column_index = target
    if value != _amount(rule["expected"]):
        return None, "The amount in the selected destination row and column differs from the ledger scalar."
    if not entity_anchor(claim["entity"], slots.get("slide:1:shape:1", "")) or not entity_anchor(
        claim["entity"], source["text"]
    ):
        return None, None
    # The released text supplies quote offsets; the corresponding immutable OOXML
    # sheet supplies actual column labels, original scalar type and number format.
    sheet = None
    witnesses = []
    for line in re.finditer(r"(?m)^([^\n]+)", source["text"]):
        header = re.fullmatch(r"\[sheet (.+?)\]", line[0])
        if header:
            sheet = header[1]
            continue
        if not sheet or sheet not in book.sheetnames:
            continue
        if not (claim["start"] <= line.start() and line.end() <= claim["end"]):
            continue
        ws = book[sheet]
        coordinates = re.findall(r"(?:^|\|\s*)([A-Z]+)(\d+)=([^|\n]+)", line[0])
        if not coordinates or coordinates[0][0] != "A" or _normalize(coordinates[0][2]) != _normalize(segment):
            continue
        row = int(coordinates[0][1])
        if _normalize(ws.cell(row, 1).value) != _normalize(segment):
            continue
        heading = None
        for ri in range(row - 1, max(0, row - 100), -1):
            year_columns = [c.column for c in ws[ri] if re.fullmatch(r"FY\s*" + period, str(c.value), re.I)]
            if year_columns:
                heading = (ri, year_columns)
                break
        if not heading or len(heading[1]) != 1 or _metrics(ws.cell(heading[0], 1).value) != metric:
            continue
        cell = ws.cell(row, heading[1][0])
        if cell.data_type != "n" or _amount(cell.value) != value or "%" in str(cell.number_format):
            continue
        money_in_format = {
            symbol
            for symbol in ("€", "£", "$", "USD", "EUR", "GBP", "CNY")
            if symbol in str(cell.number_format)
        }
        if any({"$": "USD", "€": "EUR", "£": "GBP"}.get(s, s) != unit[0] for s in money_in_format):
            continue
        span = re.search(r"(?<![\w])" + cell.coordinate + r"=(" + _NUM + r")(?=\s*(?:\||$))", line[0])
        if not span or _amount(span[1]) != value:
            continue
        witnesses.append(
            {
                "source_location": sheet + "!" + cell.coordinate,
                "source_header": sheet + "!" + ws.cell(heading[0], heading[1][0]).coordinate,
                "source_start": line.start() + span.start(1),
                "source_end": line.start() + span.end(1),
                "source_text": span[1],
                "source_format": cell.number_format,
                "source_unit": unit_evidence,
                "metric": metric,
                "period": "FY" + period,
                "row_label": segment,
                "native_text": clause,
                "native_header": prefix,
                "native_column": column_index,
                "native_unit": f"{unit[0]} x {unit[1]}",
                "displayed_number": str(value),
                "atomic_cell": False,
            }
        )
    return (
        (witnesses[0], None)
        if len(witnesses) == 1
        else (None, "Cannot match this model amount to a single quoted native cell.")
    )


def typed_table_witness(slots, rule, claim, source, load_book):
    if source["text"][claim["start"] : claim["end"]] != claim["quote"]:
        return None, None
    if not rule["locator"].startswith("slide:") or _amount(rule["expected"]) is None:
        return None, None
    suffix = Path(source["locator"]).suffix.lower()
    if suffix == ".docx":
        return _term_table(slots, rule, claim, source)
    if suffix == ".xlsx":
        route = rule.get("value_path", [])
        if ":cell:" in rule["locator"]:
            metric = _term(route[-1] if route else claim["field"])
            if metric not in {"sweep", "fee", "amortization"}:
                return None, None
        elif len(route) != 2 or not _metrics(route[1]) or rule["match"] != "contains":
            return None, None
        book = load_book(claim)
        if book:
            if ":cell:" in rule["locator"]:
                return _excel_option_rate(slots, rule, claim, source, book)
            return _model(slots, rule, claim, source, book)
    return None, None

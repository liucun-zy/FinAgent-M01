"""Conservative unit checks returning the precise source basis actually used."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
import unicodedata


def _text(value):
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def _number(value):
    text = _text(value).replace(",", "")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?", text):
        return None
    try:
        number = Decimal(text)
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


_SCALES = {"": Decimal(1), "千": Decimal(1000), "万": Decimal(10000),
           "萬": Decimal(10000), "百万": Decimal(1000000), "百萬": Decimal(1000000),
           "亿": Decimal(100000000), "億": Decimal(100000000),
           "thousand": Decimal(1000), "million": Decimal(1000000), "billion": Decimal(1000000000)}
_CURRENCIES = {"cny": ("人民币元", "人民幣元", "人民币", "人民幣", "cny", "rmb", "元", "¥"),
               "usd": ("美元", "美金", "usd", "us$"),
               "eur": ("欧元", "eur", "€"), "gbp": ("英镑", "gbp", "£"),
               "hkd": ("港元", "港币", "hkd"), "jpy": ("日元", "jpy")}
_ATOMS = {"台": "device", "device": "device", "devices": "device",
          "item": "item", "items": "item", "unit": "item", "units": "item",
          "件": "piece", "piece": "piece", "pieces": "piece", "个": "count",
          "人": "person", "person": "person", "persons": "person", "people": "person",
          "年": "year", "year": "year", "years": "year",
          "月": "month", "month": "month", "months": "month",
          "日": "day", "天": "day", "day": "day", "days": "day",
          "小时": "hour", "时": "hour", "hour": "hour", "hours": "hour",
          "股": "share", "share": "share", "shares": "share",
          "吨": "tonne", "tonne": "tonne", "tonnes": "tonne"}


@dataclass(frozen=True)
class Unit:
    numerator: str
    denominator: str | None
    multiplier: Decimal


def _atom(text):
    if text in ("", "1", "ratio", "dimensionless", "无量纲", "倍", "比例"):
        return "ratio", Decimal(1)
    if text in ("%", "percent", "percentage", "百分比"):
        return "ratio", Decimal("0.01")
    for currency, aliases in _CURRENCIES.items():
        for alias in aliases:
            if text.endswith(alias) and text[:-len(alias)] in _SCALES:
                return currency, _SCALES[text[:-len(alias)]]
    for scale in sorted(_SCALES, key=len, reverse=True):
        if scale and text.startswith(scale) and text[len(scale):] in _ATOMS:
            return _ATOMS[text[len(scale):]], _SCALES[scale]
    return _ATOMS.get(text, "literal:" + text), Decimal(1)


def _unit(text):
    text = re.sub(r"\s+per\s+|/\s*每", "/", _text(text or ""))
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"(?<=\S)每(?=\S)", "/", text)
    numerator, separator, denominator = text.partition("/")
    n, scale = _atom(numerator)
    d, divisor = _atom(denominator) if separator else (None, Decimal(1))
    return Unit(n, d, scale / divisor)


def _compatible(left, right):
    if left == right:
        return True
    # A generic item can express a physical count. A person, share, or time
    # denominator never becomes an item merely because it is also countable.
    return "item" in (left, right) and {left, right} <= {"item", "device", "piece", "count"}


def _table_units(claim, cited_refs, provided_refs):
    """Associate only a cited numeric cell/row with its delivered column header."""
    raw = _observed_raw(claim)
    raw_number = _number(raw.get("text", "")) if isinstance(raw, dict) else _number(raw or "")
    value = claim.get("value") or {}
    actual_number = _number(value.get("normalized", ""))
    actual_unit = _unit(value.get("unit"))
    cited_ids = {r["evidence_ref_id"] for r in cited_refs}
    row_blocks = {r["block_id"] for r in cited_refs if r.get("locator", {}).get("table_id")
                  and "column_index" not in r.get("locator", {})}
    cells = [r for r in provided_refs if r.get("role") == "cell" and (
        r["evidence_ref_id"] in cited_ids or r.get("block_id") in row_blocks)]
    numeric_cells = [(r, _number(r.get("raw_content", ""))) for r in cells]
    numeric_cells = [(r, n) for r, n in numeric_cells if n is not None]
    candidates = []
    for cell, number in numeric_cells:
        loc = cell.get("locator", {})
        if not loc.get("table_id") or "column_index" not in loc:
            continue
        for header in provided_refs:
            hloc = header.get("locator", {})
            if 'header_ref_ids' in loc and header['evidence_ref_id'] not in loc['header_ref_ids']:
                continue
            if not (header.get("role") == "header" and
                    header.get("source_version_id") == cell.get("source_version_id") and
                    hloc.get("table_id") == loc["table_id"] and
                    hloc.get("column_index", -1) <= loc["column_index"] <
                    hloc.get("column_index", -1) + hloc.get("colspan", 1)):
                continue
            for label in re.findall(r"\(([^()]+)\)", _text(header.get("raw_content", ""))):
                unit = _unit(label)
                if unit.numerator.startswith("literal:"):
                    continue
                anchored = (raw_number is not None and raw_number == number) or (
                    raw_number is None and (len(numeric_cells) == 1 or (
                        actual_number is not None and number * unit.multiplier == actual_number * actual_unit.multiplier)))
                if anchored:
                    candidates.append((label, number, "provided column header " + header["evidence_ref_id"],
                                       cell["evidence_ref_id"], header["evidence_ref_id"]))
    # A row citation can contain the same scalar in different columns. Without
    # an unambiguous value/unit binding, keep the raw contract but do not borrow
    # competing header units. A single explicitly cited cell remains checked.
    if (len({(_unit(label), number) for label, number, *_ in candidates}) == 1
            and len({cell_ref for _, _, _, cell_ref, _ in candidates}) == 1):
        yield from candidates


_CURRENCY_PATTERN = "(?:" + "|".join(re.escape(a) for aliases in _CURRENCIES.values() for a in aliases) + ")"
_COUNT_PATTERN = "(?:" + "|".join(re.escape(a) for a in sorted(_ATOMS, key=len, reverse=True)) + ")"
_SCALE_PATTERN = "(?:" + "|".join(re.escape(s) for s in sorted(_SCALES, key=len, reverse=True) if s) + ")?"
_UNIT_PATTERN = f"(?:{_SCALE_PATTERN}\\s*(?:{_CURRENCY_PATTERN}|{_COUNT_PATTERN})|%)(?:\\s*(?:/|每)\\s*{_COUNT_PATTERN})?"
_NUMBER_UNIT = re.compile(r"(?<![A-Za-z0-9_.])([+-]?(?:\d[\d,]*(?:\.\d+)?|\.\d+))\s*(" + _UNIT_PATTERN + r")", re.I)


def _prose_units(claim, cited_refs):
    raw = _observed_raw(claim)
    raw_text = raw.get("text", "") if isinstance(raw, dict) else raw or ""
    raw_number = _number(raw_text)
    if _text(raw_text) and raw_number is None:
        single_raw = _NUMBER_UNIT.fullmatch(_text(raw_text))
        if not single_raw:
            # A program-located phrase may include a currency prefix or condition.
            # Use its sole literal number+unit, never an unrelated paragraph number.
            located = [o for o in claim.get("source_observations", [])
                       if o.get("status") == "located" and o.get("field") == "value"]
            matches = list(_NUMBER_UNIT.finditer(_text(raw_text))) if located else []
            if len(matches) != 1:
                return  # Opaque/composite text cannot anchor a different numeric fact.
            single_raw = matches[0]
        raw_number = _number(single_raw[1])
    value = claim.get("value") or {}
    actual_number, actual_unit = _number(value.get("normalized", "")), _unit(value.get("unit"))
    candidates, numeric_mentions = [], 0
    bound_values = set((claim.get("field_evidence") or {}).get("value", []))
    for ref in cited_refs:
        if bound_values and ref["evidence_ref_id"] not in bound_values:
            continue
        if ref.get("locator", {}).get("table_id"):
            continue  # Table units require a column association, never proximity.
        for number_text, label in _NUMBER_UNIT.findall(_text(ref.get("raw_content", ""))):
            numeric_mentions += 1
            number, unit = _number(number_text), _unit(label)
            if raw_number == number or (raw_number is None and actual_number is not None and
                                       number * unit.multiplier == actual_number * actual_unit.multiplier):
                candidates.append((label, number, "cited evidence " + ref["evidence_ref_id"],
                                   ref["evidence_ref_id"], ref["evidence_ref_id"]))
    # Equal numbers can denote different facts in a paragraph. Without a unique
    # unit/value association, do not pick one or borrow a unit from another fact.
    if (raw_number is not None or numeric_mentions == 1) and len({
            (_unit(label), number) for label, number, *_ in candidates}) == 1:
        yield from candidates


def _observed_raw(claim):
    observations = [o for o in claim.get("source_observations", [])
                    if o.get("field") == "value" and o.get("status") == "located"]
    if len(observations) == 1:
        return {"text": observations[0]["text"]}
    return claim.get("raw_value")


def _unit_roles(unit):
    roles = ["unit"]
    if unit.numerator in _CURRENCIES:
        roles.append("currency")
    if unit.multiplier != 1:
        roles.append("scale")
    if unit.denominator is not None:
        roles.append("denominator")
    return roles


def _number_phrase(ref, number, unit):
    text = ref["raw_content"]
    if _number(text) == number:
        return text
    phrases = [match[1] for match in _NUMBER_UNIT.finditer(text)
               if _number(match[1]) == number and _unit(match[2]) == unit]
    return phrases[0] if len(phrases) == 1 else None


def _denominator_basis(claim, denominator, cited_refs, value_ref):
    """Find a count/time dimension in the actually cited source context.

    Absence of a denominator in a currency literal does not mean the source
    asserted a total. Its interpretation may use an explicit count or per-unit
    expression elsewhere in the same source, with that basis retained.
    """
    bound = set((claim.get("field_evidence") or {}).get("unit", []))
    source_id = value_ref.get("source_version_id") if value_ref else None
    for ref in cited_refs:
        if bound and ref["evidence_ref_id"] not in bound:
            continue
        if source_id and ref.get("source_version_id") != source_id:
            continue
        text = _text(ref.get("raw_content", ""))
        units = [_unit(label) for _, label in _NUMBER_UNIT.findall(text)]
        units.extend(_unit(label) for label in re.findall(r"\(([^()]+)\)", text)
                     if ref.get("role") == "header")
        units.extend(_unit("1/" + label) for label in re.findall(
            r"(?:/|每|\bper\s+)\s*(" + _COUNT_PATTERN + r")", text))
        if any(_compatible(denominator, u.denominator) or
               (u.denominator is None and _compatible(denominator, u.numerator)) for u in units):
            return ref
    return None


def validate_numeric_unit(claim, evidence_refs, provided_evidence_refs=None):
    """Validate explicit dimensions/scales and return only adopted source refs.

    Real cell/header/prose constraints take precedence over model raw metadata.
    Legacy raw-only claims retain their dimensional consistency check, explicitly
    marked model_raw_only; it does not establish that raw text was in the source.
    """
    result = {"used_evidence_refs": [], "normalization": []}
    value = claim.get("value") or {}
    if value.get("kind") != "number":
        return result
    actual = _unit(value.get("unit"))
    constraints = list(_table_units(claim, evidence_refs, provided_evidence_refs or evidence_refs))
    constraints.extend(_prose_units(claim, evidence_refs))
    result["input_basis"] = "source" if constraints else "model_raw_only"
    if not constraints:
        raw = claim.get("raw_value")
        if isinstance(raw, dict) and raw.get("unit"):
            constraints.append((raw["unit"], _number(raw.get("text", "")), "raw_value.unit", None, None))
        raw_text = raw.get("text", "") if isinstance(raw, dict) else raw or ""
        single_raw = _NUMBER_UNIT.fullmatch(_text(raw_text))
        if single_raw:
            constraints.append((single_raw[2], _number(single_raw[1]), "raw_value.text", None, None))
    actual_number = _number(value.get("normalized", ""))
    used = set()
    refs = {r["evidence_ref_id"]: r for r in provided_evidence_refs or evidence_refs}
    for label, number, source, number_ref, unit_ref in constraints:
        expected = _unit(label)
        contextual = expected.denominator is None and actual.denominator is not None
        denominator_ref = (_denominator_basis(claim, actual.denominator, evidence_refs, refs.get(number_ref))
                           if contextual else None)
        if not _compatible(expected.numerator, actual.numerator) or (
                not _compatible(expected.denominator, actual.denominator) and denominator_ref is None):
            raise ValueError(f"Unit consistency: value.unit {value.get('unit')!r} disagrees with "
                             f"{source} {label!r}; retain currency and denominator when rescaling.")
        if number is not None and actual_number is not None and (
                number * expected.multiplier != actual_number * actual.multiplier):
            raise ValueError(f"Unit consistency: normalized number differs from {source} after unit scaling; "
                             "convert the number together with the unit, without changing the source value.")
        if number_ref:
            used.add((number_ref, "value"))
            used.update((unit_ref, role) for role in _unit_roles(expected))
            result["normalization"].append({"value_ref_id": number_ref, "unit_ref_id": unit_ref,
                "source_number": str(number), "source_number_phrase": _number_phrase(refs[number_ref], number, expected),
                "source_unit": label,
                "multiplier": str(expected.multiplier / actual.multiplier),
                "normalized_number": value.get("normalized"), "normalized_unit": value.get("unit"),
                "contextual_denominator": contextual})
        if denominator_ref:
            used.add((denominator_ref["evidence_ref_id"], "denominator"))
    if not constraints and actual.denominator is not None:
        denominator_ref = _denominator_basis(claim, actual.denominator, evidence_refs, None)
        if denominator_ref is None:
            raise ValueError("Unit consistency: normalized denominator has no cited source basis; "
                             "cite its count/time context or leave the unit unresolved.")
        used.add((denominator_ref["evidence_ref_id"], "denominator"))
    result["used_evidence_refs"] = [{"ref_id": ref, "role": role} for ref, role in sorted(used)]
    return result

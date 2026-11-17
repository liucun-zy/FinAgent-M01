"""Read source observations inside supplied evidence, without model/provider calls.

OS-02: LangExtract 1.7.2 Resolver.align and UnicodeTokenizer (Apache-2.0).
The library locates tokens; this adapter establishes uniqueness and always reads
back the original slice. No normalized value is presented as a source quote.
"""
from __future__ import annotations

from functools import lru_cache

from langextract.core.data import Extraction
from langextract.core.tokenizer import UnicodeTokenizer
from langextract.resolver import Resolver


@lru_cache(maxsize=1)
def _alignment_tools():
    return Resolver(), UnicodeTokenizer()


def align_phrase(phrase: str, evidence: dict, *, field="value") -> dict:
    """Locate one phrase in one evidence ref; repeated token matches are unknown."""
    text = evidence["raw_content"]
    result = {"field": field, "evidence_ref_id": evidence["evidence_ref_id"],
              "source_version_id": evidence["source_version_id"],
              "locator": evidence.get("locator", {}), "status": "unlocated",
              "method": "langextract", "proposed_text": phrase}
    if not phrase.strip():
        return result
    resolver, tokenizer = _alignment_tools()
    source_tokens = tokenizer.tokenize(text).tokens
    phrase_tokens = tokenizer.tokenize(phrase).tokens
    def words(tokens, source):
        return [source[t.char_interval.start_pos:t.char_interval.end_pos] for t in tokens]
    haystack, needle = words(source_tokens, text), words(phrase_tokens, phrase)
    matches = [i for i in range(len(haystack) - len(needle) + 1)
               if needle and haystack[i:i + len(needle)] == needle]
    if len(matches) > 1:
        return {**result, "status": "ambiguous", "occurrences": len(matches)}
    aligned = next(resolver.align([Extraction(extraction_class=field, extraction_text=phrase)],
        text, token_offset=0, enable_fuzzy_alignment=False, accept_match_lesser=False,
        tokenizer_inst=tokenizer))
    interval = aligned.char_interval
    if interval is None:
        return result
    start, end = interval.start_pos, interval.end_pos
    return {**result, "status": "located", "start": start, "end": end,
            "text": text[start:end], "alignment_status": aligned.alignment_status.value,
            "verbatim_proposal": text[start:end] == phrase}


def _whole_ref(ref, kind):
    text = ref["raw_content"]
    return {"field": "value", "value_kind": kind, "status": "located",
            "method": "explicit_evidence", "evidence_ref_id": ref["evidence_ref_id"],
            "source_version_id": ref["source_version_id"], "locator": ref.get("locator", {}),
            "start": 0, "end": len(text), "text": text}


def build_source_observations(claim: dict, evidence_refs: list[dict], entities=None) -> list[dict]:
    """Observe value/mention text; ambiguous or missing positions remain explicit.

    A field/cell reference is already a location. For prose, model phrases and
    local entity names are search hints only. A whole paragraph does not become
    a precise numeric or entity-value observation.
    """
    refs = {r["evidence_ref_id"]: r for r in evidence_refs}
    value = claim.get("value") or {}
    kind = value.get("kind", "text")
    binding = (claim.get("field_evidence") or {}).get("value", [])
    selected = [refs[r] for r in binding or claim.get("evidence_ref_ids", []) if r in refs]
    selected = [r for r in selected if r.get("role") != "header"]
    raw = claim.get("raw_value")
    raw_text = raw.get("text") if isinstance(raw, dict) else raw
    phrases = [raw_text] if isinstance(raw_text, str) and raw_text.strip() else []
    exact = [r for r in selected if r.get("role") in {"cell", "field"}]
    if len(exact) == 1 and (binding or len(selected) == 1) and not (
            kind in {"text", "state", "boolean"} and phrases):
        observation = _whole_ref(exact[0], kind)
        if kind == "entity":
            observation["local_entity_id"] = value.get("local_entity_id")
        return [observation]
    if kind in {"text", "state", "boolean"} and not phrases:
        return [_whole_ref(r, kind) for r in selected]
    if kind == "entity":
        entity = next((e for e in entities or [] if e.get("local_entity_id") == value.get("local_entity_id")), {})
        phrases += [entity.get("surface_name"), entity.get("canonical_name"), *entity.get("aliases", [])]
    elif kind in {"number", "date"}:
        phrases += [value.get("original"), value.get("normalized")]
        if kind == "number":
            phrases += [n.get("source_number_phrase") for n in (claim.get("unit_validation") or {}).get("normalization", [])]
    phrases = list(dict.fromkeys(p for p in phrases if isinstance(p, str) and p.strip()))
    located, uncertain = {}, []
    for ref in selected:
        for phrase in phrases:
            found = align_phrase(phrase, ref)
            found["value_kind"] = kind
            if kind == "entity":
                found["local_entity_id"] = value.get("local_entity_id")
            if found["status"] == "located":
                located[(found["evidence_ref_id"], found["start"], found["end"])] = found
            elif found["status"] == "ambiguous":
                uncertain.append(found)
    # A longer proposal (e.g. '8,000 元') and its numeric subphrase are one
    # location; prefer the full actual observation, never concatenate inferred units.
    locations = list(located.values())
    locations = [x for x in locations if not any(x is not y and x["evidence_ref_id"] == y["evidence_ref_id"]
                 and y["start"] <= x["start"] and x["end"] <= y["end"] for y in locations)]
    if len(locations) == 1 and not uncertain:
        return locations
    if locations or uncertain:
        return [{"field": "value", "value_kind": kind, "status": "ambiguous",
                 "candidate_observations": locations + uncertain,
                 "evidence_ref_ids": [r["evidence_ref_id"] for r in selected]}]
    return [{"field": "value", "value_kind": kind, "status": "unlocated",
             "evidence_ref_ids": [r["evidence_ref_id"] for r in selected]}]

"""Matched-cost evidence selection for the repair ablation.

A separate, as-of index can publish a conventional full-text BM25 score per
window to both rankers. The rankers never receive unpurchased source bodies.
The no-index fallback orders file locators and existing headings by BM25.
"""
from collections import Counter
import hashlib
import json
import math
import re
import unicodedata

from .active_diagnosis import diagnosis_hypotheses, diagnosis_metadata
from .temporal_dag import rank_probes


def _tokens(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    words = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", text)
    tokens = []
    for word in words:
        if "\u3400" <= word[0] <= "\u9fff" and len(word) > 1:
            tokens.extend(word[i:i + 2] for i in range(len(word) - 1))
        else:
            tokens.append(word)
    return tokens


def _metadata(candidates):
    seen = set()
    addresses = set()
    rows = []
    for window in candidates:
        wid, locator = window["id"], window["locator"]
        if not isinstance(wid, str) or not wid or wid in seen:
            raise ValueError("Candidate window IDs must be unique nonempty strings")
        if not isinstance(locator, str) or not locator.strip():
            raise ValueError("Each window must have a source locator")
        seen.add(wid)
        description = window.get("description", "")
        if not isinstance(description, str):
            raise ValueError("Candidate description must be public text")
        address = (locator, window.get("start", 0), window.get("end", 0))
        if address in addresses:
            raise ValueError("Duplicate candidate source and window offsets")
        addresses.add(address)
        fingerprint = hashlib.sha256(json.dumps(address, ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        row = {"id": wid, "locator": locator, "description": description, "tie_key": fingerprint,
               "start": address[1], "end": address[2], "source_id": window.get("source_id")}
        if "bm25_score" in window:
            score = window["bm25_score"]
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                raise ValueError("Indexed BM25 score must be finite")
            row["bm25_score"] = score
        rows.append(row)
    return rows


def _bm25(query, rows, source_bodies=None):
    """Okapi BM25 with corpus-local IDF, k1=1.2, b=0.75 and binary query terms."""
    docs = [Counter(_tokens(" ".join([row["locator"], row["description"],
                source_bodies[row["id"]] if source_bodies is not None else ""]))) for row in rows]
    lengths = [sum(doc.values()) for doc in docs]
    mean_len = sum(lengths) / len(docs) or 1
    df = Counter(term for doc in docs for term in doc)
    terms = sorted(set(_tokens(query)))
    ranked = []
    for row, doc, length in zip(rows, docs, lengths):
        scores = {}
        for term in terms:
            tf = doc[term]
            if tf:
                idf = math.log1p((len(docs) - df[term] + 0.5) / (df[term] + 0.5))
                scores[term] = idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / mean_len))
        ranked.append({**row, "score": sum(scores.values()), "matched_terms": scores, "cost": 1})
    return sorted(ranked, key=lambda x: (-x["score"], x["tie_key"]))


def shared_bm25_index(query, candidates, bodies_by_window_id):
    """Publish only lexical scores for the released candidate set, never excerpts.

    Corpus statistics and document frequencies use this conflict's searchable
    windows, which must be frozen at its cutoff by the index builder. Both
    policies receive identical scores. Indexing costs are separate from the
    model's online budget for opening originals.
    """
    rows = _metadata(candidates)
    ids = {row["id"] for row in rows}
    if ids != set(bodies_by_window_id) or any(not isinstance(t, str) for t in bodies_by_window_id.values()):
        raise ValueError("Index corpus must exactly match the candidate list with text for each window")
    return {r["id"]:r["score"] for r in _bm25(query, rows, bodies_by_window_id)} if rows else {}


def rank_windows(strategy, query, candidates):
    """Return a reproducible complete ranking; the runner caps and meters actual reads.

    All metadata windows cost one read. Use the deployed generic minimax
    ranking and its original tie break for decision value. For equal lexical
    scores the common final tie break hashes (source locator, start, end),
    independent of the label and the order in which examples were authored.
    """
    if strategy not in {"relevance", "decision_value"}:
        raise ValueError("Unknown reading strategy")
    rows = _metadata(candidates)
    if not rows:
        return []
    indexed = ["bm25_score" in row for row in rows]
    if any(indexed) and not all(indexed):
        raise ValueError("All candidates must share the same BM25 indexing policy")
    if strategy == "relevance":
        if all(indexed):
            scored = [{**row, "score":row["bm25_score"], "cost":1} for row in rows]
            return sorted(scored, key=lambda row: (-row["score"], row["tie_key"]))
        return _bm25(query, rows)
    plans = [{**row, "cost": 1,
              **diagnosis_metadata(row["locator"] + " " + row["description"])} for row in rows]
    return _value_order(diagnosis_hypotheses(), plans)


def _value_order(hypotheses, plans):
    ranked = rank_probes(hypotheses, plans)
    return sorted(({**row, "score": row["gain_per_cost"]} for row in ranked),
                  key=lambda row: (-row["score"], row["worst_remaining_repairs"],
                                   row["cost"], row["tie_key"]))


def rank_native_windows(strategy, query, candidates, hypotheses, plans):
    """Same two-window contrast with the native operator's trusted hypotheses."""
    rows = _metadata(candidates)
    if strategy == "relevance":
        return _bm25(query, rows)
    if strategy != "decision_value":
        raise ValueError("Unknown reading strategy")
    possible = {plan["id"]: plan["outcomes"] for plan in plans}
    return _value_order(hypotheses,
            [{**row, "cost": 1, "outcomes": possible[row["id"]]} for row in rows])

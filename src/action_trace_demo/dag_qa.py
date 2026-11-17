"""Question-scoped citation and derivation receipts, with answer semantics kept explicit."""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
import re
import json

from .dependency_queries import DependencyQueries


def terms(text):
    text = str(text).lower()
    result = re.findall(r"[a-z0-9]+", text)
    for block in re.findall(r"[\u3400-\u9fff]+", text):
        result.extend(block[i:i + 2] for i in range(max(1, len(block) - 1)))
    return set(result)


def ledger_search(dag, query, cutoff, valid_at=None, scope=None, limit=10):
    candidates = dag.claim_at(as_of=cutoff, valid_at=valid_at, scope=scope or None)
    q = terms(query)
    docs = [terms(" ".join(str(n["payload"].get(k, "")) for k in
                           ("entity", "claim", "field", "value", "scope", "description"))) for n in candidates]
    frequency = Counter(term for doc in docs for term in doc)
    def key(item):
        index, node = item
        return (sum(1 / frequency[t] for t in docs[index] & q), node["seq"])
    ranked = sorted(enumerate(candidates), key=key, reverse=True)
    # Also permit discovery by listing the most recent claims when query is empty.
    return [node for idx, node in ranked if not q or docs[idx] & q][:min(25, max(1, limit))]


def preview(node):
    p = node["payload"]
    sides = {k: {f: p[k].get(f) for f in ("entity","claim","description","quote","source_id","locator")}
             for k in ("old","new") if isinstance(p.get(k),dict)}
    return {"id": node["id"], "kind": node["kind"], "parents": node["parents"], **sides,
            **{k: p[k] for k in ("entity", "claim", "field", "value", "scope", "description",
                                 "relation", "status", "purpose", "valid_from", "valid_to", "path",
                                 "locator", "source_id", "action", "quote", "old_id", "new_id") if k in p}}


class GraphQA:
    def __init__(self, dag, context):
        self.dag = dag
        self.ctx = context
        self.question_id = context["request_id"]
        self.cutoff = context["as_of"]
        self.dependencies = DependencyQueries(dag)

    def dependency_context(self):
        return {"as_of": self.cutoff, "valid_at": self.ctx.get("valid_at"),
                "scope": self.ctx.get("scope") or {}, "purpose": self.ctx.get("purpose")}

    def receipt(self, id_, kind):
        node = self.dag.get(id_)
        if node["kind"] != kind or node["payload"].get("question_id") != self.question_id:
            raise ValueError("Receipt belongs to another question or routing step")
        return node

    def record(self, kind, payload, parents):
        return self.dag.append(kind, {"question_id": self.question_id, **payload},
                               list(dict.fromkeys([self.question_id, *parents])))

    def retrieve(self, query=None, limit=10):
        rows = ledger_search(self.dag, query or self.ctx["question"], self.cutoff,
                             self.ctx.get("valid_at"), self.ctx.get("scope"), limit)
        rec = self.record("qa_retrieval", {"claim_ids": [x["id"] for x in rows]}, [x["id"] for x in rows])
        summaries=[]
        for n in rows:
            p=n["payload"]
            label=str(p.get("claim",""))
            value=json.dumps(p.get("value"), ensure_ascii=False)
            summaries.append({"id":n["id"], "entity":p["entity"],"field":p.get("field"),
                              "claim": label[:500], "value_preview":value[:320], "scope":p.get("scope")})
        return {"retrieval_id":rec["id"], "claims":summaries}

    def route(self, retrieval_id, claim_ids):
        rec = self.receipt(retrieval_id, "qa_retrieval")
        ids = list(dict.fromkeys(claim_ids))
        if not set(ids) <= set(rec["payload"]["claim_ids"]) or len(ids) > 12:
            raise ValueError("Choose at most twelve claims from this retrieval")
        frozen = self.dag.nodes(as_of=self.cutoff)
        by_id = {n["id"]: n for n in frozen}
        context = self.dependency_context()
        connected = set(ids)
        predecessor = {}
        related = {}
        for cid in ids:
            for consumer in self.dependencies.consumers_of(cid, context, transitive=True):
                child = consumer["node"]
                if child["kind"].startswith("qa_") or child["kind"] in {"question", "stage", "source"}:
                    continue
                dependency_ids = consumer.get("dependency_ids", [])
                predecessor.setdefault(child["id"], next((i for i in dependency_ids if i in connected), cid))
                connected.add(child["id"])
                related[child["id"]] = child
        actions = sorted((n for n in related.values() if n["kind"] != "claim"), key=lambda n: n["seq"])
        support_bundles, support_paths, provenance_paths, templates = {}, {}, {}, []
        for cid in ids:
            tree = self.dependencies.support_bundles(cid, context)
            support_bundles[cid] = tree
            paths = {}
            for leaf in support_leaves(tree):
                sid = leaf["source_id"]
                paths.setdefault(sid, leaf.get("path", [sid, cid]))
                template = {"claim_id": cid, "source_id": sid, "quote": leaf.get("quote", ""),
                            "start": leaf.get("start", 0)}
                if template["quote"] and template not in templates:
                    templates.append(template)
            support_paths[cid] = paths
            source = by_id[cid]["payload"]["source_id"]
            # The claim's own citation remains inspectable provenance. Only
            # explicit support bundles establish its derivation.
            provenance_paths[cid] = {source: [source, cid]}
        # For a disputed route require the sources on both sides that are material
        # to the selected claim. Generic source ancestry remains available on demand.
        witnesses = {}
        for n in actions:
            if n["kind"] != "comparison":
                continue
            sides = [n["payload"][k] for k in ("old", "new")]
            # A downstream use may have many sibling checks. Cite conflicts on
            # the selected fact/derivation rather than every co-delivered file.
            if not any(side["claim_id"] in ids for side in sides):
                continue
            for side in sides:
                sid = side["source_id"]
                witnesses.setdefault(sid, set()).add((side["start"], side["end"]))
        required = sorted(witnesses)
        source_ids = sorted({sid for paths in support_paths.values() for sid in paths}
                            | {sid for paths in provenance_paths.values() for sid in paths} | set(required))
        ranges = {sid: sorted(v) for sid,v in witnesses.items()}
        route = self.record("qa_route", {"claims": ids, "actions": [a["id"] for a in actions],
                            "sources": source_ids, "required_sources": required, "witness_ranges": ranges,
                            "support_paths":support_paths, "support_bundles": support_bundles,
                            "provenance_paths": provenance_paths,
                            "consumer_predecessor":predecessor}, [rec["id"], *ids, *(a["id"] for a in actions)])
        return {"route_id": route["id"], "claims": [preview(by_id[x]) for x in ids],
                "actions": [preview(a) for a in actions[-30:]], "action_count": len(actions),
                "sources": [{"id": s, "locator": by_id[s]["payload"].get("locator")} for s in source_ids],
                "required_sources": required, "witness_ranges": ranges, "as_of": self.cutoff,
                "support_bundles": support_bundles, "citation_templates": templates,
                "guarantees": {"citation_valid": "Exact excerpts must belong to the routed source version.",
                               "derivation_complete": "One complete alternative must include every required premise and a verified transformation.",
                               "answer_supported": "Natural-language answer semantics require a separate verification."}}

    def read(self, route_id, source_id, start=0, size=3500):
        route = self.receipt(route_id, "qa_route")
        if source_id not in route["payload"]["sources"]:
            raise ValueError("Original must belong to this evidence path")
        source = self.dag.get(source_id)["payload"]
        body = source["text"]
        start = max(0, int(start))
        chunk = body[start:start + min(9000, max(64, size))]
        if not chunk.strip():
            raise ValueError("Empty source range")
        rec = self.record("qa_probe", {"route_id": route_id, "source_id": source_id,
                          "start": start, "end": start + len(chunk)}, [route_id, source_id])
        return {"probe_id": rec["id"], "source_id": source_id, "locator": source["locator"],
                "text": chunk, "next_offset": start + len(chunk), "total_chars": len(body)}

    def submit(self, route_id, answer, citations, probe_ids=(), unresolved=False):
        route = self.receipt(route_id, "qa_route")["payload"]
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("An answer or explicit evidence gap is required")
        reads = defaultdict(list)
        # The recorded route is authoritative even if the model omits a handle
        # for a source it actually inspected on this same question.
        probe_ids=list(dict.fromkeys([*probe_ids, *(n["id"] for n in self.dag.nodes("qa_probe")
                       if n["payload"]["route_id"]==route_id and n["payload"]["question_id"]==self.question_id)]))
        for probe_id in probe_ids:
            probe = self.receipt(probe_id, "qa_probe")["payload"]
            if probe["route_id"] != route_id:
                raise ValueError("Source inspection belongs to another route")
            reads[probe["source_id"]].append((probe["start"], probe["end"]))
        verified = all(any(a <= lo and b >= hi for a,b in reads[sid])
                       for sid, ranges in route["witness_ranges"].items() for lo,hi in ranges)
        if not unresolved and (not citations or not verified):
            raise ValueError("Resolved answer needs citations and outstanding original-source checks")
        evidence_paths = []
        located_citations = []
        # Older route receipts remain readable, but their flattened paths must
        # be reconstructed into support groups before checking a new answer.
        bundles = {cid: self.dependencies.support_bundles(cid, self.dependency_context())
                   for cid in route["claims"]}
        for citation in citations:
            cid, sid, quote = (citation[k] for k in ("claim_id", "source_id", "quote"))
            if cid not in route["claims"] or sid not in route["sources"] or not quote.strip():
                raise ValueError("Citation must connect a retrieved claim to a routed source")
            leaves = support_leaves(bundles[cid])
            support_path = next((leaf.get("path") for leaf in leaves if leaf["source_id"] == sid), None)
            original_source = self.dag.get(cid)["payload"]["source_id"]
            provenance_path = [sid, cid] if sid == original_source else None
            path = support_path or provenance_path
            if not path:
                raise ValueError(f"{cid} has no support path to {sid}. For this claim use a quote from source "
                                 f"{self.dag.get(cid)['payload']['source_id']} in the route citation_templates.")
            body = self.dag.get(sid)["payload"]["text"]
            if "start" in citation:
                start = citation["start"]
                if type(start) is not int or start < 0 or body[start:start + len(quote)] != quote:
                    raise ValueError("Citation start must locate this exact quote in the routed source")
            else:
                start = body.find(quote)
                if start >= 0 and body.find(quote, start + 1) >= 0:
                    start = None
            end = start + len(quote) if start is not None and start >= 0 else None
            reported_quotes = [self.dag.get(cid)["payload"].get("quote", "")]
            reported_quotes.extend(leaf.get("quote", "") for leaf in leaves
                                   if leaf["source_id"] == sid)
            inspected = any(quote in body[a:b] for a, b in reads[sid])
            if quote not in body or (not any(quote in text for text in reported_quotes) and not inspected):
                raise ValueError(f"For {cid} the supplied text is not a verbatim excerpt at {sid}. "
                                 "Use the route citation_templates or copy the exact text from inspect_original.")
            evidence_paths.append({"claim_id":cid,"source_id":sid,"quote":quote,"nodes":path,
                                   "start": start, "end": end, "location_ambiguous": start is None,
                                   "role": "support" if support_path else "anchoring_provenance"})
            located_citations.append({**citation, "start": start, "end": end})
        coverage = {cid: support_coverage(tree, located_citations, self.dag, cid) for cid, tree in bundles.items()}
        complete = aggregate_completeness([c["derivation_complete"] for c in coverage.values()])
        checks = {"citation_valid": bool(citations), "derivation_complete": complete,
                  "answer_supported": None, "support_checks": coverage,
                  "guarantees": {"citation_valid": "Exact source and quote membership was checked.",
                                 "derivation_complete": "Every required premise and transformation in one alternative was checked.",
                                 "answer_supported": "Natural-language answer semantics have not been verified."}}
        if not unresolved and complete is not True:
            rec = self.record("qa_answer_check", {"route_id": route_id, "status": "requires_evidence",
                              "answer": answer, "citations": citations, "evidence_paths": evidence_paths,
                              "as_of": self.cutoff, **checks}, [route_id, *probe_ids])
            return {"id": rec["id"], "committed": False, **rec["payload"]}
        rec = self.record("qa_answer", {"answer": answer, "citations": citations, "evidence_paths":evidence_paths,
                          "route_id":route_id, "status": "unresolved" if unresolved else "evidence_complete",
                          "as_of": self.cutoff, **checks}, [route_id, *probe_ids])
        return {"id": rec["id"], "committed": True, **rec["payload"]}


def support_leaves(tree):
    if tree["kind"] == "source":
        return [tree]
    return [leaf for group in tree.get("groups", []) for item in group["required"]
            for leaf in support_leaves(item)]


def aggregate_completeness(states):
    if not states or False in states:
        return False
    return None if None in states else True


def citation_witnesses(citation, leaf, dag, target_id):
    if citation["source_id"] != leaf["source_id"]:
        return False
    if citation["claim_id"] not in {target_id, leaf.get("claim_id")}:
        return False
    anchored = leaf.get("quote", "")
    quoted = citation["quote"]
    start, end = citation.get("start"), citation.get("end")
    if start is None or end is None:
        return False
    lo, hi = leaf.get("start"), leaf.get("end")
    if lo is None or hi is None:
        return False
    if anchored and start <= lo and hi <= end:
        return True
    if not anchored or not (lo <= start and end <= hi) or quoted not in anchored:
        return False
    claim_id = leaf.get("claim_id")
    if not claim_id:
        return False
    value = dag.get(claim_id)["payload"].get("value")
    # Short scalar citations remain usable, while a shared entity name cannot
    # stand for two distinct premises from the same source document.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        amounts = re.findall(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?![\w.])", quoted.replace(",", ""))
        return any(Decimal(amount) == Decimal(str(value)) for amount in amounts)
    if isinstance(value, str):
        marker = str(value).replace(",", "").casefold()
        fragment = quoted.replace(",", "").casefold()
        return bool(marker and re.search(r"(?<!\w)" + re.escape(marker) + r"(?!\w)", fragment))
    return False


def support_coverage(tree, citations, dag, target_id):
    if not tree.get("available", False):
        return {"id": tree["id"], "derivation_complete": False, "reason": "unavailable_at_question_context"}
    if tree["kind"] == "source":
        covered = any(citation_witnesses(citation, tree, dag, target_id) for citation in citations)
        ambiguous = any(citation["source_id"] == tree["source_id"] and citation.get("start") is None
                        for citation in citations)
        return {"id": tree["id"], "claim_id": tree.get("claim_id"), "source_id": tree["source_id"],
                "start": tree.get("start"), "end": tree.get("end"),
                "derivation_complete": covered, "reason": "cited_premise" if covered else
                "ambiguous_premise_citation" if ambiguous else "missing_premise_citation"}
    groups = []
    for group in tree.get("groups", []):
        required = [support_coverage(item, citations, dag, target_id) for item in group["required"]]
        inputs = aggregate_completeness([item["derivation_complete"] for item in required])
        transform = group.get("transformation", tree.get("transformation", {}))
        verified = transform.get("verified") is True
        complete = False if inputs is False else True if inputs is True and verified else None
        groups.append({"group_id": group["group_id"], "required": required, "inputs_complete": inputs,
                       "transformation_verified": verified, "transformation": transform,
                       "derivation_complete": complete})
    states = [group["derivation_complete"] for group in groups]
    complete = True if True in states else None if None in states else False
    return {"id": tree["id"], "groups": groups, "derivation_complete": complete}

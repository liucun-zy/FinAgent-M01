"""Evidence-window reranking using observed edges in an as-of action DAG.

A window is eligible only if a path from a task anchor reaches a real claim
whose complete, source-backed quote is inside that window. Traversal walks
backward from use to prerequisite. Edge records retain their original
orientation (prerequisite to use). Reading a document at some point in time
does not, by itself, ground a particular window.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json


# The final, separately checked source -> claim citation adds one more edge.
MAX_DEPENDENCIES = 3

# Parent kind, stored edge kind, child kind. Generic "input" is accepted only
# for these domain-specific, recorded use/edit operations.
DEPENDENCY_TYPES = frozenset({
    ("claim", "supports", "claim"),
    ("claim", "derived_from", "claim"),
    ("claim", "input", "historical_decision"),
    ("claim", "input", "historical_artifact_action"),
    ("historical_artifact_action", "input", "historical_artifact_action"),
    ("historical_artifact_action", "input", "artifact_commit_intent"),
    ("artifact_commit_intent", "produces_version", "artifact_version"),
    ("artifact_version", "copies_from", "artifact_proposal"),
    ("artifact_version", "replaces", "artifact_proposal"),
    ("claim", "requires", "artifact_proposal"),
    ("claim", "observed_input", "native_finance_check"),
    ("claim", "uses_claim", "artifact_check"),
    ("native_finance_check", "uses_native_finance_check", "artifact_check"),
    ("artifact_proposal", "uses_artifact_proposal", "artifact_check"),
    ("artifact_repair_proposal", "uses_artifact_repair_proposal", "artifact_check"),
    ("claim", "uses_claim", "artifact_repair_proposal"),
    ("artifact_check", "input", "artifact_commit_intent"),
    ("claim", "input", "artifact_observation"),
    ("claim", "old_claim", "comparison"),
    ("claim", "new_claim", "comparison"),
    ("artifact_observation", "input", "comparison"),
})
CITATION_TYPES = {"supports", "cites_original"}


def _digest(value):
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()


def _index(view):
    nodes = {n["id"]: n for n in view["nodes"]}
    if len(nodes) != len(view["nodes"]) or any(n["seq"] > view["cutoff"] for n in nodes.values()):
        raise ValueError("Prefix contains a duplicate or a future node")
    reverse = defaultdict(list)
    citations = {}
    edge_ids = set()
    for raw in view["edges"]:
        p, c, kind = raw["from"], raw["to"], raw["kind"]
        parent, child = nodes[p], nodes[c]
        if parent["seq"] >= child["seq"] or ("parents" in child and p not in child["parents"]):
            raise ValueError("An edge is not a chronological recorded dependency")
        eid = f"{p}/{kind}/{c}"
        if eid in edge_ids:
            raise ValueError("Duplicate action DAG edge")
        edge_ids.add(eid)
        edge = {"edge_id": eid, "from": p, "kind": kind, "to": c}
        explicit = child.get('supports', child.get('payload', {}).get('supports'))
        derived = parent['kind']=='claim' and child['kind']=='claim' and kind in {'derived_from','supports'}
        proven = explicit is not None and any(p in group for group in explicit)
        # Old exports used derived_from as a catch-all for claim parents.
        # A missing support annotation cannot turn that legacy label into proof.
        eligible = not derived or (proven if explicit is not None or kind=='derived_from' else True)
        if eligible and (parent["kind"], kind, child["kind"]) in DEPENDENCY_TYPES:
            reverse[c].append(edge)
        if (parent["kind"] == "source" and kind in CITATION_TYPES and child["kind"] == "claim"
                and child.get("source_id") == p):
            citations[c, p] = edge
    return nodes, reverse, citations, edge_ids


def _paths(view, order):
    nodes, reverse, citations, edge_ids = _index(view)
    if set(view["windows"]) != set(order) or len(set(order)) != len(order) or not order:
        raise ValueError("Use the exact nonempty frozen BM25 candidate list")
    by_claim = defaultdict(list)
    for wid, matches in view["windows"].items():
        for location in matches:
            c, s = location["claim_id"], location["source_id"]
            if c not in nodes or s not in nodes:
                raise ValueError("Candidate quote endpoint lies outside the prefix")
            a, b = location["start"], location["end"]
            if not (0 <= location["window_start"] <= a < b <= location["window_end"]):
                raise ValueError("The candidate does not contain the complete claim quote")
            if not (0 <= location["source_start"] <= a < b <= location["source_end"]):
                raise ValueError("Claim is outside its cited original chunk")
            # No citation edge means no graph evidence, even with a matching file
            # or an externally supplied span mapping.
            cite = citations.get((c, s))
            if cite is not None:
                by_claim[c].append((wid, cite, location))
    paths = {wid: [] for wid in order}
    for anchor in view["anchors"]:
        start = anchor["id"]
        if start not in nodes or nodes[start]["kind"] in {"source", "stage"}:
            raise ValueError("A source or stage hub cannot anchor an action-graph route")
        stack = [(start, [])]
        while stack:
            node_id, travelled = stack.pop()
            for wid, cite, location in by_claim[node_id]:
                edges = [cite, *reversed(travelled)]
                paths[wid].append({"anchor":start, "claim_id":node_id, "source_id":cite["from"],
                    "edges":edges, "hops":len(edges), "quote_location":location})
            if len(travelled) < MAX_DEPENDENCIES:
                for edge in reverse[node_id]:
                    stack.append((edge["from"], travelled + [edge]))
    for routes in paths.values():
        routes.sort(key=lambda r: (r["hops"], r["anchor"], tuple(e["edge_id"] for e in r["edges"])))
    return paths, edge_ids


def _order(bm25, paths, removed=frozenset()):
    distances = {}
    for wid, routes in paths.items():
        for route in routes:  # paths already sorted by number of dependencies
            if all(edge["edge_id"] not in removed for edge in route["edges"]):
                distances[wid] = route["hops"]
                break
    if not distances:
        return list(bm25), "no_anchored_quote_path", []
    best = min(distances.values())
    leaders = [wid for wid in bm25 if distances.get(wid) == best]
    if len(leaders) != 1:
        return list(bm25), "competing_equally_short_paths_keep_bm25", leaders
    winner = leaders[0]
    return [winner, *(w for w in bm25 if w != winner)], "unique_shortest_anchored_quote_path", leaders


def rerank(bm25_ids, view):
    """Return unchanged or promoted order plus auditable graph intervention records.

    Holding matching and anchors fixed, an edge deletion invalidates all and only
    the complete routes containing that edge. Filtering enumerated paths thus
    computes the same answer as re-traversing the graph after each intervention.
    """
    baseline = list(bm25_ids)
    paths, edge_ids = _paths(view, baseline)
    ordered, reason, leaders = _order(baseline, paths)
    winner = ordered[0]
    changed = winner != baseline[0]
    empty, _, _ = _order(baseline, paths, edge_ids)
    if empty != baseline:
        raise AssertionError("Deleting all graph edges must reproduce the full BM25 order")
    individual = []
    cut = []
    if changed:
        winner_edges = {e["edge_id"] for path in paths[winner] for e in path["edges"]}
        for edge in sorted(winner_edges):
            new_order, why, _ = _order(baseline, paths, {edge})
            individual.append({"removed_edge":edge, "winner":new_order[0], "reason":why,
                               "restores_baseline":new_order[0] == baseline[0]})
        # Start with all citation edges, then prune while preserving restoration
        # of the full BM25 list. Revisit remaining edges because reintroducing
        # evidence can also produce a tie; no global minimum claim is made.
        cut = {p["edges"][0]["edge_id"] for routes in paths.values() for p in routes}
        while True:
            removable = next((e for e in sorted(cut) if _order(baseline, paths, cut-{e})[0] == baseline), None)
            if removable is None:
                break
            cut.remove(removable)
        cut = sorted(cut)
    recovered, recovery_reason, _ = _order(baseline, paths, set(cut))
    return {"bm25_order":baseline, "order":ordered, "selected":winner, "baseline":baseline[0],
            "changed":changed, "reason":reason, "tied_best":leaders, "cutoff":view["cutoff"],
            "anchors":view["anchors"], "node_count":len(view["nodes"]), "edge_count":len(edge_ids),
            "edge_set_sha256":_digest(sorted(edge_ids)), "paths":paths,
            "counterfactual":{"empty_graph_winner":empty[0], "empty_graph_order":empty,
                "individual":individual, "joint":{"removed_edges":cut, "winner":recovered[0],
                "restores_baseline":recovered == baseline, "reason":recovery_reason}}}

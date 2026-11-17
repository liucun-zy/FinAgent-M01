"""Versioned evidence, ledger and decision operators used by the DSH host.

Edges always point from an observed parent to a later event. A semantic edit
creates a new event referencing both versions. It never rewrites an old edge.
Knowledge time is the integer event sequence. Business intervals are [from,to).
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _day(value):
    if value is None:
        return None
    return date.fromisoformat(value).isoformat()


def _seq(node_id):
    if not isinstance(node_id, str) or not re.fullmatch(r"D[0-9]{9,}", node_id):
        raise ValueError(f"Invalid DAG node id: {node_id!r}")
    value = int(node_id[1:])
    if node_id != f"D{value:09d}" or value < 1 or value >= 2**63:
        raise ValueError(f"Invalid DAG node id: {node_id!r}")
    return value


def _ident(seq):
    return f"D{seq:09d}"


def _cutoff(value):
    if value is None:
        return 2**63 - 1
    if type(value) != int or value < 0 or value >= 2**63:
        raise ValueError("as_of must be a non-negative SQLite event sequence")
    return value


def _ids(values):
    if isinstance(values, str):
        values = [values]
    return list(dict.fromkeys(values))


def _interval(payload, at):
    if at is None:
        return True
    return ((not payload.get("valid_from") or payload["valid_from"] <= at)
            and (not payload.get("valid_to") or at < payload["valid_to"]))


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _scope(value):
    if value is None:
        return {}
    if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
        raise ValueError("scope must be an object")
    return dict(value)


def _quote(source, quote, start=None):
    _text(quote, "quote")
    p = source["payload"]
    if source["kind"] != "source":
        raise ValueError("Evidence requires a released source")
    text = p["text"]
    if start is None:
        start = text.find(quote)
        if start >= 0 and text.find(quote, start + 1) >= 0:
            raise ValueError("Ambiguous original quote: include more context or specify start")
    if type(start) is not int or start < 0 or not text.startswith(quote, start):
        raise ValueError("Original quote does not occur at the supplied source location")
    return {"source_id": source["id"], "source_key": p["source_key"], "locator": p["locator"],
            "start": start, "end": start + len(quote), "quote": text[start:start+len(quote)],
            "source_sha256": p["sha256"]}


def rank_probes(hypotheses, probes, *, budget=None):
    """Rank observable partitions by reduction in worst-case repair regret / cost.

    Defaults to unit loss for picking another repair. Hypothesis authors can
    instead specify a full action cost vector. Compatible explanations mapping
    to the same repair do not create decision value merely by duplicating IDs.
    """
    if not hypotheses:
        raise ValueError("At least one candidate explanation is required")
    by_id = {_text(h["id"], "hypothesis id"): h for h in hypotheses}
    if len(by_id) != len(hypotheses):
        raise ValueError("Duplicate candidate explanation")
    repairs = {_text(h["repair"], "repair") for h in hypotheses}
    actions = sorted(repairs.union(*(set(h.get("losses", {})) for h in hypotheses)))
    losses = {}
    for key, h in by_id.items():
        vec = {a: float(h.get("losses", {}).get(a, int(a != h["repair"]))) for a in actions}
        if any(not math.isfinite(v) or v < 0 for v in vec.values()):
            raise ValueError("Repair losses must be finite and nonnegative")
        best = min(vec.values())
        losses[key] = {a: v - best for a, v in vec.items()}

    def regret(keys):
        return min(max(losses[k][action] for k in keys) for action in actions)

    initial = regret(by_id)
    rows, seen = [], set()
    for q in probes:
        probe_id = _text(q["id"], "probe id")
        if probe_id in seen:
            raise ValueError("Duplicate probe id")
        seen.add(probe_id)
        cost = float(q["cost"])
        if not math.isfinite(cost) or cost <= 0:
            raise ValueError("Probe cost must be finite and positive")
        if budget is not None and cost > budget:
            continue
        outcomes = q["outcomes"]
        if set(outcomes) != set(by_id):
            raise ValueError("A probe partition must predict outcomes for every explanation")
        groups = defaultdict(list)
        for key, observation in outcomes.items():
            groups[_text(observation, "predicted observation")].append(key)
        residual = max(regret(keys) for keys in groups.values())
        rows.append({**q, "risk_before": initial, "risk_after_worst_case": residual,
                     "gain_per_cost": (initial - residual) / cost,
                     "worst_remaining_repairs": max(len({by_id[k]["repair"] for k in keys})
                                                    for keys in groups.values())})
    return sorted(rows, key=lambda q: (-q["gain_per_cost"], q["worst_remaining_repairs"],
                                       q["cost"], q["id"]))


class TemporalDAG:
    rank_probes = staticmethod(rank_probes)
    _internal = {"source", "claim", "comparison", "probe", "revision"}

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.path = self.workspace / "temporal_dag.sqlite3"
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS event (
                    seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS event_kind ON event(kind, seq);
                CREATE INDEX IF NOT EXISTS event_m01_extraction_scope ON event(
                    json_extract(payload,'$.arrival_id'),
                    json_extract(payload,'$.window_group_id'), seq)
                    WHERE kind='m01_extraction';
                CREATE TABLE IF NOT EXISTS dependency (
                    parent INTEGER NOT NULL REFERENCES event(seq),
                    child INTEGER NOT NULL REFERENCES event(seq),
                    PRIMARY KEY(parent,child), CHECK(parent < child));
                CREATE INDEX IF NOT EXISTS child_parents ON dependency(child,parent);
                CREATE TABLE IF NOT EXISTS released (
                    source_key TEXT PRIMARY KEY, seq INTEGER UNIQUE NOT NULL REFERENCES event(seq),
                    locator TEXT NOT NULL, stage INTEGER NOT NULL, chars INTEGER NOT NULL);
                CREATE TRIGGER IF NOT EXISTS event_immutable BEFORE UPDATE ON event
                    BEGIN SELECT RAISE(ABORT,'immutable event'); END;
                CREATE TRIGGER IF NOT EXISTS event_persistent BEFORE DELETE ON event
                    BEGIN SELECT RAISE(ABORT,'immutable event'); END;
                CREATE TRIGGER IF NOT EXISTS dep_immutable BEFORE UPDATE ON dependency
                    BEGIN SELECT RAISE(ABORT,'immutable edge'); END;
                CREATE TRIGGER IF NOT EXISTS dep_persistent BEFORE DELETE ON dependency
                    BEGIN SELECT RAISE(ABORT,'immutable edge'); END;
            """)

    @contextmanager
    def _db(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _unpack(db, row):
        parents = [_ident(r[0]) for r in db.execute(
            "SELECT parent FROM dependency WHERE child=? ORDER BY parent", (row["seq"],))]
        return {"id": _ident(row["seq"]), "seq": row["seq"], "kind": row["kind"],
                "payload": json.loads(row["payload"]), "parents": parents}

    @classmethod
    def _get(cls, db, node_id):
        row = db.execute("SELECT * FROM event WHERE seq=?", (_seq(node_id),)).fetchone()
        if row is None:
            raise ValueError(f"Unknown DAG node: {node_id}")
        return cls._unpack(db, row)

    def get(self, node_id):
        with self._db() as db:
            return self._get(db, node_id)

    def source_index(self, as_of=None):
        """Metadata projection for planning a read without fetching document bodies."""
        with self._db() as db:
            rows = db.execute("SELECT seq,locator,stage,chars FROM released WHERE seq<=? ORDER BY seq",
                              (_cutoff(as_of),))
            return [{"id": _ident(row["seq"]), "locator": row["locator"], "stage": row["stage"],
                     "chars": row["chars"]} for row in rows]

    def read_excerpt(self, source_id, start, size):
        if type(start) != int or type(size) != int or start < 0 or size <= 0:
            raise ValueError("Invalid original excerpt range")
        with self._db() as db:
            row = db.execute("""SELECT substr(json_extract(payload,'$.text'),?,?) FROM event
                                WHERE seq=? AND kind='source'""", (start+1, size, _seq(source_id))).fetchone()
            if not row or not row[0]:
                raise ValueError("No released text at the requested location")
            return row[0]

    def nodes(self, kind=None, as_of=None):
        with self._db() as db:
            rows = db.execute("SELECT * FROM event WHERE seq<=? AND (? IS NULL OR kind=?) ORDER BY seq",
                              (_cutoff(as_of), kind, kind))
            return [self._unpack(db, r) for r in rows]

    @classmethod
    def _append(cls, db, kind, payload, parents=()):
        _text(kind, "kind")
        if not isinstance(payload, dict):
            raise ValueError("Node payload must be an object")
        parent_ids = sorted({_seq(p) for p in _ids(parents)})
        for sequence in parent_ids:
            if not db.execute("SELECT 1 FROM event WHERE seq=?", (sequence,)).fetchone():
                raise ValueError(f"Unknown or future parent: {_ident(sequence)}")
        seq = db.execute("INSERT INTO event(kind,payload) VALUES(?,?)",
                         (kind, _json(payload))).lastrowid
        db.executemany("INSERT INTO dependency(parent,child) VALUES(?,?)",
                       ((sequence, seq) for sequence in parent_ids))
        return cls._get(db, _ident(seq))

    def append(self, kind, payload, parents=()):
        if kind in self._internal:
            raise ValueError(f"The {kind} operator requires its validated method")
        with self._db(write=True) as db:
            if kind == "stage":
                stage = payload.get("stage")
                if type(stage) != int or stage < 0:
                    raise ValueError("A stage requires a non-negative integer")
                latest = db.execute("""SELECT seq,json_extract(payload,'$.stage') FROM event
                                       WHERE kind='stage' ORDER BY seq DESC LIMIT 1""").fetchone()
                if latest and stage < latest[1]:
                    raise ValueError("Stage event cannot precede an already opened stage")
                if latest:
                    parents = [_ident(latest[0]), *_ids(parents)]
            return self._append(db, kind, payload, parents)

    def ancestors(self, ids: Iterable[str], as_of=None):
        order = [_seq(x) for x in _ids(ids)]
        if not order:
            return []
        cutoff = _cutoff(as_of)
        if any(i > cutoff for i in order):
            raise ValueError("Root is outside the requested knowledge cutoff")
        with self._db() as db:
            marks = ",".join("?" for _ in order)
            if db.execute(f"SELECT COUNT(*) FROM event WHERE seq IN ({marks})", order).fetchone()[0] != len(order):
                raise ValueError("Unknown graph traversal root")
            rows = db.execute(f"""
                WITH RECURSIVE walked(seq) AS (
                    SELECT seq FROM event WHERE seq IN ({marks})
                    UNION SELECT d.parent FROM dependency d JOIN walked w ON d.child=w.seq)
                SELECT e.* FROM event e JOIN walked w ON w.seq=e.seq ORDER BY e.seq""", order)
            return [self._unpack(db, row) for row in rows]

    def release_source(self, stage, locator, text, *, source_key=None, published_at=None):
        if type(stage) != int or stage < 0:
            raise ValueError("stage must be a nonnegative integer")
        _text(locator, "source locator")
        if not isinstance(text, str):
            raise ValueError("Released evidence must be text")
        digest = hashlib.sha256(text.encode()).hexdigest()
        source_key = _text(source_key or (locator + "#" + digest), "source key")
        with self._db(write=True) as db:
            existing = db.execute("SELECT seq FROM released WHERE source_key=?", (source_key,)).fetchone()
            if existing:
                node = self._get(db, _ident(existing[0]))
                previous = node["payload"]
                if (previous["sha256"] != digest or previous["locator"] != locator
                        or previous["published_at"] != published_at or stage < previous["stage"]):
                    raise ValueError("A released source version is immutable")
                return node
            current = db.execute("""SELECT max(stage) FROM (
                SELECT max(stage) AS stage FROM released UNION ALL
                SELECT max(json_extract(payload,'$.stage')) FROM event WHERE kind='stage')""").fetchone()[0]
            if current is not None and stage < current:
                raise ValueError("Release order cannot move backwards; use current stage for late evidence")
            opening = db.execute("""SELECT seq FROM event WHERE kind='stage'
                AND json_extract(payload,'$.stage')=? ORDER BY seq DESC LIMIT 1""", (stage,)).fetchone()
            node = self._append(db, "source", {"stage": stage, "locator": locator, "text": text,
                    "source_key": source_key, "published_at": published_at, "sha256": digest},
                    [_ident(opening[0])] if opening else [])
            db.execute("INSERT INTO released VALUES(?,?,?,?,?)", (source_key, node["seq"], locator, stage, len(text)))
            return node

    def record_claim(self, source_id, entity, claim, description, quote, *, field=None, value=None,
                     scope=None, valid_from=None, valid_to=None, parents=(), supports=None, start=None,
                     pending=None, candidate_ids=()):
        for key, value_text in (("entity", entity), ("claim", claim), ("description", description)):
            _text(value_text, key)
        if field is not None:
            _text(field, "field")
        vf, vt = _day(valid_from), _day(valid_to)
        if vf and vt and vf >= vt:
            raise ValueError("Business validity must be a nonempty half-open interval")
        with self._db(write=True) as db:
            source = self._get(db, source_id)
            evidence = _quote(source, quote, start)
            metadata = _scope(scope)
            candidates = _ids(candidate_ids)
            for candidate in candidates:
                if self._get(db, candidate)["kind"] != "claim":
                    raise ValueError("A conflict candidate must be a claim")
            if field and value is not None:
                # Prevent silent overwrites even if callers omit candidates.
                for existing in self._claim_at(db, None, vf, None):
                    p = existing["payload"]
                    if (p["entity"] == entity and p["field"] == field and p["scope"] == metadata
                            and p["value"] is not None and p["value"] != value
                            and _overlap(p, {"valid_from": vf, "valid_to": vt})):
                        candidates.append(existing["id"])
            candidates = _ids(candidates)
            # OR over independent derivations, AND within each derivation.
            # The anchoring citation is provenance; it is not automatically a
            # sufficient derivation for a conclusion that needs other claims.
            groups = [[source_id]] if supports is None else supports
            if not isinstance(groups, (list, tuple)) or not groups:
                raise ValueError("A claim requires at least one support group")
            groups = [_ids(g) if isinstance(g, (list, tuple)) else [] for g in groups]
            if any(not g for g in groups):
                raise ValueError("Support groups must be nonempty arrays")
            for item in {p for group in groups for p in group}:
                if self._get(db, item)["kind"] not in {"claim", "source"}:
                    raise ValueError("A support group must refer to claims or released sources")
            current_stage = db.execute("""SELECT max(stage) FROM (
                SELECT max(stage) AS stage FROM released UNION ALL
                SELECT max(json_extract(payload,'$.stage')) FROM event WHERE kind='stage')""").fetchone()[0]
            current_event = db.execute("""SELECT seq FROM event WHERE kind='stage'
                AND json_extract(payload,'$.stage')=? ORDER BY seq DESC LIMIT 1""", (current_stage,)).fetchone()
            stage_parents = [_ident(current_event[0])] if current_event else []
            return self._append(db, "claim", {"entity": entity, "claim": claim, "description": description,
                "field": field, "value": value, "scope": metadata, "valid_from": vf,
                "valid_to": vt, **evidence, "stage": current_stage, "source_stage": source["payload"]["stage"],
                "supports": groups,
                "pending": bool(pending or candidates), "candidate_ids": candidates},
                [source_id, *stage_parents, *candidates, *_ids(parents), *(p for group in groups for p in group)])

    @staticmethod
    def _side(node):
        if node["kind"] != "claim":
            raise ValueError("Each conflict side must be an evidence-anchored claim")
        return {"claim_id": node["id"], "known_at": node["seq"], **{k: node["payload"][k] for k in (
            "entity", "claim", "field", "value", "description", "quote", "locator", "source_id",
            "start", "end", "source_sha256", "scope", "valid_from", "valid_to")}}

    def compare(self, old_id, new_id, description, *, purpose=None, parents=()):
        _text(description, "comparison description")
        if old_id == new_id:
            raise ValueError("A comparison needs two distinct claim versions")
        with self._db(write=True) as db:
            old, new = self._get(db, old_id), self._get(db, new_id)
            return self._append(db, "comparison", {
                "old": self._side(old), "new": self._side(new), "description": description,
                "purpose": purpose, "hypotheses": ["value", "scope", "use", "coexist", "retract", "unresolved"]},
                [old_id, new_id, *_ids(parents)])

    def probe(self, comparison_id, source_id, quote, description, *, start=None, review_id=None):
        _text(description, "probe description")
        with self._db(write=True) as db:
            if self._get(db, comparison_id)["kind"] != "comparison":
                raise ValueError("Probe must be tied to a comparison")
            source = self._get(db, source_id)
            witness = _quote(source, quote, start)
            parents = [comparison_id,source_id]
            if review_id:
                review = self._get(db, review_id)
                if (review["kind"]!="ledger_self_review" or review["payload"]["target_id"]!=comparison_id
                        or review["payload"]["source_id"]!=source_id or review["payload"]["start"]!=witness["start"]
                        or review["payload"]["end"]!=witness["end"]):
                    raise ValueError("Review receipt does not correspond to this exact conflict and original")
                parents.append(review_id)
            return self._append(db, "probe", {**witness, "description": description,
                "comparison_id": comparison_id}, parents)

    @classmethod
    def _claim_at(cls, db, as_of, valid_at, scope):
        cutoff = _cutoff(as_of)
        day = _day(valid_at)
        wanted = _scope(scope)
        claims, decisions = [], []
        for row in db.execute("SELECT * FROM event WHERE seq<=? AND kind IN ('claim','revision') ORDER BY seq",
                              (cutoff,)):
            n = cls._unpack(db, row)
            if n["kind"] == "claim":
                claims.append(n)
            elif n["payload"]["status"] == "accepted":
                decisions.append(n["payload"])
        admitted_by = defaultdict(set)
        for decision in decisions:
            admitted_by[decision["new_id"]].add(decision["old_id"])
        admitted = {n["id"] for n in claims if not n["payload"].get("pending", False)
                    or (admitted_by[n["id"]] and set(n["payload"]["candidate_ids"]) <= admitted_by[n["id"]])}
        withdrawn = set()
        for d in decisions:
            if d["new_id"] not in admitted or d["relation"] not in {"value", "scope", "retract"}:
                continue
            if day is None or _interval({"valid_from": d["effective_from"],
                                         "valid_to": d["effective_to"]}, day):
                withdrawn.add(d["old_id"])
        # Derived conclusions lose their use in this view when every support
        # group contains a withdrawn, pending or temporally inapplicable claim.
        withheld = withdrawn | {n["id"] for n in claims if n["id"] not in admitted
                                 or not _interval(n["payload"], day)}
        usable = []
        alternatives = any(len(n['payload']['supports']) > 1 for n in claims)
        if alternatives:
            from .dependency_queries import group_witness
            index = {n['id']: n for n in (cls._unpack(db, row) for row in
                     db.execute('SELECT * FROM event WHERE seq<=? ORDER BY seq', (cutoff,)))}
        for n in claims:
            p = n["payload"]
            groups = p['supports']
            if len(groups) > 1:
                groups = [g for g in groups if group_witness(n, g, index)['verified']]
            if n["id"] in withheld or all(any(i in withheld for i in g) for g in groups):
                withheld.add(n["id"])
            elif all(p["scope"].get(k) == v for k, v in wanted.items()):
                usable.append(n)
        return usable

    def claim_at(self, as_of=None, valid_at=None, scope=None):
        """Return adjudicated ledger heads, optionally at a business date."""
        with self._db() as db:
            return self._claim_at(db, as_of, valid_at, scope)

    def candidates(self, entity, field, scope=None, *, as_of=None, valid_from=None,
                   valid_to=None, source_id=None):
        """Retrieve by field even when entity alignment is absent or uncertain."""
        entity = _text(entity, "entity")
        wanted = _scope(scope)
        vf, vt = _day(valid_from), _day(valid_to)
        tokens = set(re.findall(r"\w+", entity.casefold()))
        ranked = []
        for node in self.claim_at(as_of):
            p = node["payload"]
            same_field = bool(field and p["field"] and p["field"].casefold() == field.casefold())
            same_entity = p["entity"].casefold() == entity.casefold()
            if not (same_field or (same_entity and not field)):
                continue
            names = set(re.findall(r"\w+", p["entity"].casefold()))
            score = (int(same_entity), int(same_field), int(p["scope"] == wanted),
                     sum(wanted.get(k) == v for k, v in p["scope"].items()),
                     int(_overlap(p, {"valid_from": vf, "valid_to": vt})),
                     len(names & tokens), int(p["source_id"] == source_id), node["seq"])
            ranked.append((score, node))
        return [n for _, n in sorted(ranked, key=lambda x: x[0], reverse=True)]

    @classmethod
    def _affected(cls, db, changed_ids, as_of=None, valid_at=None):
        from .dependency_queries import DependencyQueries
        nodes = [cls._unpack(db, row) for row in db.execute(
            'SELECT * FROM event WHERE seq<=? ORDER BY seq', (_cutoff(as_of),))]
        changed = set(_ids(changed_ids))
        query = DependencyQueries(nodes=nodes, claim_view=lambda **kw: cls._claim_at(
            db, kw.get('as_of'), kw.get('valid_at'), None))
        report = query.impact(changed, {'as_of':as_of, 'valid_at':valid_at, 'include_consumers': False})
        return [n['id'] for n in nodes if n['kind']=='claim' and n['id'] in report['invalidated'] and n['id'] not in changed]

    def affected(self, changed_ids, as_of=None, valid_at=None):
        """Descendant claims which lose all independent derivations."""
        with self._db() as db:
            return self._affected(db, changed_ids, as_of, valid_at)

    def supported_relations(self, comparison_id, evidence_ids, *, target_id=None, purpose=None):
        """Read-only evidentiary feasibility used by the active diagnostic policy."""
        with self._db() as db:
            comparison = self._get(db, comparison_id)
            if comparison["kind"] != "comparison":
                raise ValueError("Expected an open comparison")
            c = comparison["payload"]
            observed = []
            for i in _ids(evidence_ids):
                n = self._get(db, i)
                if n["kind"] != "probe" or n["payload"]["comparison_id"] != comparison_id:
                    raise ValueError("Probe does not belong to this comparison")
                observed.append(n["payload"])
            target = self._get(db, target_id) if target_id else None
            results = {}
            for relation in c["hypotheses"]:
                reason = _witness_reason(relation, c["old"], c["new"], observed, target, purpose, c["purpose"])
                if reason:
                    results[relation] = reason
            return results

    def revise(self, comparison_id, relation, description, *, evidence_ids=(), target_id=None,
               purpose=None):
        """Commit an evidence-supported graph/state change or persist an unresolved decision.

        Evidence IDs are actual source reads in this comparison, not model verdicts.
        Revisions made by this generic operator accept a small set of checkable
        witnesses. Domain adapters can do richer native inspection for use actions.
        """
        if relation not in {"value", "scope", "use", "coexist", "retract", "unresolved"}:
            raise ValueError("Unknown candidate relation")
        _text(description, "revision description")
        evidence_ids = _ids(evidence_ids)
        with self._db(write=True) as db:
            comp = self._get(db, comparison_id)
            if comp["kind"] != "comparison":
                raise ValueError("A revision must resolve a comparison")
            c = comp["payload"]
            old, new = c["old"], c["new"]
            target = self._get(db, target_id) if target_id else None
            witnesses = []
            for i in evidence_ids:
                node = self._get(db, i)
                if node["kind"] != "probe" or node["payload"]["comparison_id"] != comparison_id:
                    raise ValueError("A witness must be an observed probe for this exact conflict")
                witnesses.append(node["payload"])
            prior = db.execute("""SELECT * FROM event WHERE kind='revision'
                AND json_extract(payload,'$.comparison_id')=?
                AND json_extract(payload,'$.status')='accepted' ORDER BY seq DESC LIMIT 1""",
                (comparison_id,)).fetchone()
            if prior:
                result = self._unpack(db, prior)
                if (result["payload"]["relation"] == relation and result["payload"]["purpose"] == purpose
                        and result["payload"]["target_id"] == target_id):
                    return result
                raise ValueError("This comparison already has a committed revision; open a new comparison")
            reason = _witness_reason(relation, old, new, witnesses, target, purpose, c["purpose"])
            if relation in {"value", "scope", "retract"} and reason:
                active = {n["id"] for n in self._claim_at(db, None, new["valid_from"], None)}
                if old["claim_id"] not in active:
                    reason = ""
            accepted = bool(reason)
            impacted = self._affected(db, [old["claim_id"]], valid_at=new['valid_from']) if accepted and relation in {"scope","value","retract"} else []
            # Report actual published consumers, including their exact native
            # slots. A file with an independent derivation is still an inspection
            # target; an impact notice never rewrites or retires historical bytes.
            changed = {old["claim_id"], *impacted}
            consumers = []
            if accepted and relation in {"scope", "value", "retract", "use"}:
                from .dependency_queries import DependencyQueries
                nodes = [self._unpack(db, row) for row in db.execute('SELECT * FROM event ORDER BY seq')]
                query = DependencyQueries(nodes=nodes, claim_view=lambda **kw: self._claim_at(
                    db, kw.get('as_of'), kw.get('valid_at'), None))
                notices = query.impact([old['claim_id']])['consumers']
                for row in notices:
                    v = row['node']['payload']
                    if v.get('contract_id') and v.get('sha256'):
                        consumers.append({k: row[k] for k in ('version_id','path','sha256','purpose',
                                         'bindings','dependency_ids','roles','locators')})
            details = {"comparison_id": comparison_id, "old_id": old["claim_id"], "new_id": new["claim_id"],
                       "relation": relation, "status": "accepted" if accepted else "unresolved",
                       "description": description, "evidence_ids": evidence_ids, "purpose": purpose,
                       "target_id": target_id, "witness": reason or "no_discriminating_witness",
                       "effective_from": new["valid_from"],
                       "effective_to": new["valid_to"] if relation == "value" else None,
                       "affected": impacted, "native_consumers":consumers}
            return self._append(db, "revision", details,
                                [comparison_id, *evidence_ids, *([target_id] if target_id else [])])


def _overlap(a, b):
    return not ((a["valid_to"] and b["valid_from"] and a["valid_to"] <= b["valid_from"])
                or (b["valid_to"] and a["valid_from"] and b["valid_to"] <= a["valid_from"]))


def _contains(quote, value):
    if value is None or isinstance(value, (dict, list)):
        return False
    literal = str(value).strip()
    if not literal:
        return False
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(literal) + r"(?![a-z0-9])", quote,
                          re.IGNORECASE | re.ASCII))


def _correction(quote, old, new):
    # Require a directional witness explicitly assigning old → new. Merely
    # finding both values (e.g. in a scenario table) cannot establish this.
    a, b = re.escape(str(old["value"])), re.escape(str(new["value"]))
    if not (_contains(quote, old["value"]) and _contains(quote, new["value"])
            and _contains(quote, new["entity"])):
        return False
    if re.search(r"\b(not|never|hypothetical|proposed|if|would|could)\b|尚未|并未|假设|拟将",
                 quote, re.IGNORECASE):
        return False
    return bool(re.search(r"(?:correct(?:ed|s)?|revis(?:ed|es)|restat(?:ed|es))\s+(?:from\s+)?"
                          + a + r"\s+to\s+" + b + r"(?!\d|\.\d)"
                          + r"|(?:由|从)\s*" + a + r"\s*(?:更正|修正|訂正|调整)为\s*" + b
                          + r"(?!\d|\.\d)", quote, re.IGNORECASE))


def _equivalent_representations(old, new):
    """Both originals must explicitly state the equivalent scalar, in context."""
    from .native_grounding import amounts_match, dates, quantities, same_calendar_date
    left, right = str(old["value"]), str(new["value"])
    if same_calendar_date(left, right) and same_calendar_date(right, left):
        return dates(old["quote"]) == dates(left) and dates(new["quote"]) == dates(right)
    scalars = [quantities(v) for v in (left, right)]
    if any(len(values) != 1 or values[0].dimension is None for values in scalars):
        return False
    a, b = scalars[0][0], scalars[1][0]
    if not amounts_match(a, b):
        return False
    for side, value in ((old, a), (new, b)):
        if not _contains(side["quote"], side["entity"]):
            return False
        # Do not treat a matching amount in a multi-scenario table as an assignment.
        comparable = [q for q in quantities(side["quote"]) if q.dimension == value.dimension]
        if len(comparable) != 1 or not amounts_match(value, comparable[0]):
            return False
    return True


def _witness_reason(relation, old, new, probes, target, purpose, compared_purpose):
    if not probes or relation == "unresolved":
        return ""
    # A witness read must touch this claim's own anchored sentence or a context
    # that contains it. An unrelated line in a long document is not support.
    fresh = [p["quote"] for p in probes if (
        (p["source_id"] == new["source_id"] and p["start"] < new["end"] and new["start"] < p["end"])
        or (p["source_id"] != new["source_id"] and _seq(p["source_id"]) > _seq(new["source_id"])
            and _contains(p["quote"], new["entity"]) and _contains(p["quote"], new["field"])))]
    same_key = (old["entity"] == new["entity"] and old["field"] and old["field"] == new["field"]
                and old["scope"] == new["scope"])
    if relation == "value" and same_key and old["value"] != new["value"]:
        if any(_correction(q, old, new) for q in fresh):
            return "directed_correction_in_original"
    if relation == "scope" and same_key and new["valid_from"]:
        if old["valid_from"] and old["valid_from"] >= new["valid_from"]:
            return ""
        for q in fresh:
            if (_contains(q, new["entity"]) and _contains(q, new["value"])
                    and _contains(q, new["valid_from"])
                    and re.search(r"\beffective\b|生效|施行", q, re.IGNORECASE)
                    and not re.search(r"\b(not|if|proposed)\b|尚未|拟于", q, re.IGNORECASE)):
                return "effective_date_and_new_value_in_original"
    if relation == "retract" and same_key:
        from .event_witness import completed_withdrawal
        if any(completed_withdrawal(q,old["entity"],old["field"]) for q in fresh):
            return "withdrawal_of_identified_field_in_original"
    if relation == "coexist" and fresh:
        if (same_key and old["value"] != new["value"] and old.get("valid_from") == new.get("valid_from")
                and old.get("valid_to") == new.get("valid_to")
                and _equivalent_representations(old, new)):
            return "equivalent_source_backed_representation"
        if same_key and old["value"] == new["value"] and old["value"] is not None:
            return "same_value_and_scope"
        for key in old["scope"].keys() & new["scope"].keys():
            a, b = old["scope"][key], new["scope"][key]
            if a != b and _contains(old["quote"], a) and any(_contains(q, b) for q in fresh):
                return "distinct_anchored_scope_values"
        if (old["field"] and old["field"] == new["field"] and old["entity"] != new["entity"]
                and _contains(old["quote"], old["entity"]) and any(
                    _contains(q, new["entity"]) and _contains(q, old["entity"])
                    and re.search(r"\b(both|separate|alternatives|scenarios)\b|两种|分别|备选", q, re.IGNORECASE)
                    for q in fresh)):
            return "explicit_distinct_entities"
    if relation == "use" and target and purpose and purpose == compared_purpose:
        if (target["payload"].get("purpose") == purpose and old["claim_id"] in target["parents"]
                and target["kind"] in {"artifact_version", "artifact_observation", "use_binding"}):
            for q in fresh:
                if (_contains(q, new["entity"]) and re.search(
                        r"\b(selected|approved|designated|use)\b|选用|选定|采用", q, re.IGNORECASE)
                        and not re.search(r"\b(not|if|proposed)\b|尚未|未选", q, re.IGNORECASE)):
                    return "observed_use_and_scoped_selection"
    return ""

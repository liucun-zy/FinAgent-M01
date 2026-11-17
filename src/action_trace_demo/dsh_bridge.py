"""Trusted task configuration and model-facing operators for the DSH native executor."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile

from .dag_qa import GraphQA, ledger_search, preview, terms
from .guarded_artifacts import ArtifactOperator
from .temporal_dag import TemporalDAG
from .active_diagnosis import inspect as inspect_comparison
from .business_calculation import UNIT_NAMES, reporting_dates

STR = {"type": "string"}
INT = {"type": "integer"}
IDS = {"type": "array", "items": STR}
OBJ = {"type": "object", "additionalProperties": True}


def spec(name, description, properties, required=()):
    return {"name": name, "description": description, "parameters": {"type": "object",
            "properties": properties, "required": list(required), "additionalProperties": False}}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(f.name, path)


@contextmanager
def project_lock(root):
    with (root / ".operator.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def current(root):
    return json.loads((root / "task_context.json").read_text())


def short_node(n):
    return {"id": n["id"], "kind": n["kind"], **{k: v for k, v in n["payload"].items() if k != "text"}}


def open_question(root, question, cutoff=None, valid_at=None, scope=None):
    dag = TemporalDAG(root)
    all_nodes = dag.nodes()
    last = all_nodes[-1]["seq"] if all_nodes else 0
    cutoff = last if cutoff is None else cutoff
    if not 0 <= cutoff <= last:
        raise ValueError("Question cutoff outside known history")
    q = dag.append("question", {"question": question, "as_of": cutoff,
                   "valid_at": valid_at, "scope": scope or {}})
    ctx = {"mode": "qa", "request_id": q["id"], **q["payload"]}
    write_json(root / "task_context.json", ctx)
    return ctx


def handover_mirrors(expected, stage, contracts=None):
    """Return current requested checkpoint mirrors and their formal primary."""
    paths = set(expected)
    mapping = {}
    for primary in paths:
        if str(Path(primary).parent) != "deliverables/final":
            continue
        mirror = f"working/checkpoints/T{stage:02d}/" + Path(primary).name
        if mirror not in paths:
            continue
        if contracts is not None:
            a, b = contracts.get(primary), contracts.get(mirror)
            if not a or not b or a["stage"] != stage or b["stage"] != stage or a["purpose"] != b["purpose"]:
                continue
        mapping[mirror] = primary
    return dict(sorted(mapping.items()))


def open_stage(root, stage, instruction, expected, sources, parents=(), *, self_review=False, review_policy="ledger_graph"):
    dag = TemporalDAG(root)
    from .live_review import REVIEW_POLICIES
    if review_policy not in REVIEW_POLICIES:
        raise ValueError("Unknown self-review policy")
    stage_node = dag.append("stage", {"stage": stage, "instruction": instruction,
                            "expected": expected}, parents)
    # Release original documents in stable order. Knowledge sequence is distinct
    # from each document's asserted effective date.
    source_ids = []
    for locator, text in sources:
        node = dag.release_source(stage, locator, text, source_key=f"{stage}/{locator}")
        source_ids.append(node["id"])
    ctx = {"mode": "maintenance", "request_id": stage_node["id"], "stage": stage,
           "expected": expected, "released": source_ids, "self_review":self_review,
           "review_policy": review_policy}
    if self_review:
        from .task_obligations import register_obligations
        register_obligations(dag, stage_node, source_ids, expected)
    artifacts = ArtifactOperator(root, dag)
    mirrors = handover_mirrors(expected, stage)
    for path in expected:
        target = mirrors.get(path, path)
        artifacts.bind(path, purpose=f"stage {stage} requested deliverable: {target}",
                       description=instruction, rules=[], stage=stage, parents=[stage_node["id"]])
    write_json(root / "task_context.json", ctx)
    return ctx


QA_TOOLS = [
    spec("retrieve_ledger", "Recall candidate ledger claims for the actual question at its fixed cutoff.",
         {"query": STR, "limit": INT}),
    spec("route_graph", "Follow recalled claims through provenance, comparisons, revisions and actual use. Inspect required_sources.",
         {"retrieval_id": STR, "claim_ids": IDS}, ("retrieval_id", "claim_ids")),
    spec("inspect_original", "Read a source reachable on this route. Provide offsets when the needed sentence is later.",
         {"route_id": STR, "source_id": STR, "start": INT, "size": INT}, ("route_id", "source_id")),
    spec("submit_answer", "Commit the answer and its evidence. Set unresolved for missing evidence and specify the gap.",
         {"route_id": STR, "answer": STR, "citations": {"type":"array","items":{"type":"object",
          "properties":{"claim_id":STR,"source_id":STR,"quote":STR,"start":INT},"required":["claim_id","source_id","quote"]}},
          "probe_ids": IDS, "unresolved":{"type":"boolean"}}, ("route_id", "answer", "citations")),
]
MAINT_TOOLS = [
    spec("list_sources", "Discover previously released documents by locator or content terms.", {"query": STR, "limit": INT}),
    spec("read_source", "Read original released text by ID and offset.", {"source_id":STR,"start":INT,"size":INT}, ("source_id",)),
    spec("find_claims", "Search the ledger. detail=compact returns scoped fields, value previews and exact source offsets. detail=full preserves complete evidence. Supply claim_id to read that one complete entry.", {"query":STR,"limit":INT,"claim_id":STR,"detail":{"type":"string","enum":["compact","full"]}}),
    spec("record_claim", "Record an entity, claim, description and exact original quote. An update requires the old claim ID. Use a scalar value for an individual fact. Use fiscal_period for FYyyyy and ISO dates only for effective bounds.",
         {"source_id":STR,"entity":STR,"claim":STR,"description":STR,"quote":STR,"field":STR,"value":{},
          "scope":OBJ,"fiscal_period":STR,"valid_from":STR,"valid_to":STR,"old_claim_id":STR,"start":INT,
          "supports":{"type":"array","items":IDS}}, ("source_id","entity","claim","description","quote","field","value")),
    spec("inspect_conflict", "Inspect both sides and choose a directed source read by its ability to distinguish repair actions.",
         {"comparison_id":STR,"budget":INT}, ("comparison_id",)),
    spec("resolve_claim", "Record a grounded resolution. For insufficient evidence use unresolved; preserve legitimate scenarios with coexist/use.",
         {"comparison_id":STR,"relation":{"type":"string","enum":["value","scope","use","coexist","retract","unresolved"]},
          "description":STR,"evidence_ids":IDS,"purpose":STR,"target_id":STR}, ("comparison_id","relation","description")),
    spec("derive_amount", "Compute a derived financial amount from named original-backed scalar claim IDs. "
         "Use named scalar operands with +, -, *, /; percentages and bps are already normalized. Do not divide them by 100/10000 again. "
         "Use separate released claims for amortization, base and spread; use fiscal_period=FYyyyy independently of ISO effective dates. "
         "For a table cite its original row, entity, measure and fiscal header. Bind its output claim to the native result. "
         "Supported units (common unabbreviated variants are accepted): "+", ".join(UNIT_NAMES)+".",
         {"entity":STR,"field":STR,"purpose":STR,"result_unit":STR,"expression":STR,"fiscal_period":STR,"valid_from":STR,"valid_to":STR,
          "operands":{"type":"array","items":{"type":"object","properties":{"name":STR,"claim_id":STR,"unit":STR},
                      "required":["name","claim_id","unit"],"additionalProperties":False}}},
         ("entity","field","purpose","expression","operands","result_unit")),
    spec("list_work_items", "Read the original requested work, acceptance mappings, and persistent progress for this task.", {}),
    spec("map_work_item", "Map every part of one original requirement to explicit acceptance criteria and native targets. "
         "For decomposition supply exact clauses covering the original request, each with criterion and optional targets.",
         {"item_id":STR,"acceptance":{"oneOf":[STR,{"type":"object","properties":{"mechanical_obligations":IDS},
             "required":["mechanical_obligations"],"additionalProperties":False},{"type":"array","minItems":1,"items":{
             "type":"object","properties":{"quote":STR,"criterion":STR,"targets":{"type":"array","items":OBJ}},
             "required":["quote","criterion"],"additionalProperties":False}}]},
          "targets":{"type":"array","minItems":1,"items":{"type":"object",
             "properties":{"path":STR,"locator":STR},"required":["path","locator"],"additionalProperties":False}},
          "obligation_ids":IDS}, ("item_id","acceptance","targets")),
    spec("update_work_item", "Record mapped work progress. Generated work needs actual check IDs. Verified work needs "
         "ready native checks, fresh business source review IDs, and a description explicitly assessing this item's "
         "criterion at its mapped locations. Exempt/deferred work needs an exact authorizing instruction quote.",
         {"item_id":STR,"state":{"type":"string","enum":["waiting_evidence","executable","generated","verified","exempt","deferred"]},
          "description":STR,"check_id":{"oneOf":[STR,IDS]},"review_id":{"oneOf":[STR,IDS]},
          "evidence_ids":IDS,"source_id":STR,"quote":STR,"start":INT}, ("item_id","state","description")),
    spec("finish_stage", "Submit committed files after all required work has a verified delivery or an authorized disposition. Report material limitations.",
         {"description":STR}, ("description",)),
]


class Bridge:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.ctx = current(self.root)
        self.ingest = None
        self.m03 = None
        if self.ctx["mode"] == "m01_semantic":
            from .semantic_ingestion import SemanticTools
            self.ingest = SemanticTools(self.root, self.ctx)
            return
        if self.ctx["mode"] == "ingest":
            from .ingestion_tools import IngestionTools
            self.ingest = IngestionTools(self.root, self.ctx)
            return
        if self.ctx["mode"] == "m03_maintenance":
            from .maintenance_tools import MaintenanceTools
            self.m03 = MaintenanceTools(self.root, self.ctx)
            return
        self.dag = TemporalDAG(self.root)
        self.art = ArtifactOperator(self.root, self.dag, self_review=self.ctx.get("self_review",False))
        self.art.review_policy = self.ctx.get("review_policy", "ledger_graph")
        self.qa = GraphQA(self.dag, self.ctx) if self.ctx["mode"] == "qa" else None

    @staticmethod
    def _source_identity(node):
        from .live_review import source_role
        p = node["payload"]
        identity = {"source_role": source_role(p)}
        key = p.get("source_key", "").removeprefix("native:")
        if key.startswith("legacy-window:"):
            identity["legacy_source_id"] = key.removeprefix("legacy-window:")
        return identity

    def _preview_claim(self, node):
        row = preview(node)
        if self.art.review_policy == "ledger_only":
            row.pop("parents", None)
        row.update(self._source_identity(node))
        if row.get("legacy_source_id"):
            for parent_id in node["parents"]:
                parent = self.dag.get(parent_id)
                if parent["kind"] != "legacy_node":
                    continue
                old = parent["payload"]["original"]
                if old.get("kind") == "claim":
                    row["legacy_provenance"] = {"claim_id":old["id"], "known_at":old["created_event_sequence"],
                        "semantic_status":old["semantic_status"], "structural_pass":old["structural_pass"],
                        "quote_match_mode":old["quote_matches"][0]["match_mode"]}
                    break
        return row

    def _current_reviews(self, cid):
        cutoff = self.dag.get(self.ctx["request_id"])["seq"]
        reviews = [n for n in self.dag.nodes("ledger_self_review") if n["seq"] > cutoff
                   and n["payload"]["target_id"] == cid and n["payload"].get("policy") == self.art.review_policy]
        ids = {n["id"] for n in reviews}
        probes = [n["id"] for n in self.dag.nodes("probe") if n["seq"] > cutoff
                  and n["payload"]["comparison_id"] == cid and ids.intersection(n["parents"])]
        return reviews, probes

    def _mirror_primary(self, path):
        if not self.ctx.get("self_review") or not path.startswith("working/checkpoints/"):
            return None
        primary = "deliverables/final/" + Path(path).name
        if primary not in self.ctx["expected"] or path not in self.ctx["expected"]:
            return None
        contracts = {p: self.art._contract(p)["payload"] for p in (primary, path)}
        return handover_mirrors((primary, path), self.ctx["stage"], contracts).get(path)

    def _mirror_instruction(self, primary, mirror, head):
        latest = self.art._last("artifact_check", "path", primary)
        current = self.dag.get(self.ctx["request_id"])["seq"]
        hint = (f" Latest formal proposal {latest['id']} ({latest['payload']['status']})."
                if latest and latest["seq"] > current else " Stage a formal proposal to start its review.")
        if head:
            hint += (f" Use artifact_propose(action='copy', path={mirror!r}, source={primary!r}, "
                     f"source_sha256={head['payload']['sha256']!r}, description='Copy signed primary').")
        return (f"{mirror} is a mirror of {primary}. Apply changes and sign the formal primary first."
                f" Existing staged mirror edits can be read and re-proposed against the formal primary for a new check.{hint}")

    def _check_mirror_write(self, path, name, args, proposal=None):
        primary = self._mirror_primary(path)
        if not primary:
            return
        current = self.dag.get(self.ctx["request_id"])["seq"]
        head = self.art.head(primary)
        if not head or head["seq"] <= current or not head["payload"].get("proof_id"):
            raise ValueError(self._mirror_instruction(primary, path, None))
        try:
            head = self.art.validate_head(primary)
        except ValueError as exc:
            raise ValueError(f"{exc}. {self._mirror_instruction(primary, path, None)}") from exc
        if name == "artifact_propose":
            clean = (args.get("action") in {"copy","export"} and args.get("source")==primary
                     and args.get("source_sha256")==head["payload"]["sha256"]
                     and set(args.get("claim_ids",[])) <= set(head["payload"].get("input_claim_ids",[]))
                     and not any(args.get(k) for k in ("changes","additions","content","bindings")))
        else:
            p = proposal["payload"]
            clean = name=="artifact_commit" and exact_mirror_of(p, head)
        if not clean:
            raise ValueError(self._mirror_instruction(primary, path, head))

    def catalog(self):
        if self.ingest:
            return self.ingest.catalog()
        if self.m03:
            return self.m03.catalog()
        if self.ctx["mode"] == "paired_repair":
            from .ablation_task import tool_spec
            return [tool_spec(self.ctx)]
        if self.qa:
            return QA_TOOLS
        return MAINT_TOOLS + self.art.catalog()

    def call(self, name, args):
        allowed = {item["name"]: item for item in self.catalog()}
        if name not in allowed:
            raise PermissionError("Tool is unavailable in this task mode")
        if self.m03:
            # M03's single typed contract decodes and validates exactly once.
            return self.m03.call(name, args)
        if self.ingest:
            # The extraction tool validates the envelope and independent facts;
            # a per-claim error must not reject its valid siblings here.
            return self.ingest.call(name, args)
        from jsonschema import validate
        validate(args, allowed[name]["parameters"])
        if self.ctx["mode"] == "paired_repair":
            from .ablation_task import commit
            return commit(self.dag, self.ctx, **args)
        if self.qa:
            return {"retrieve_ledger":self.qa.retrieve,"route_graph":self.qa.route,
                    "inspect_original":self.qa.read,"submit_answer":self.qa.submit}[name](**args)
        if name in {'list_work_items', 'map_work_item', 'update_work_item'}:
            from .work_items import WorkItems
            work = WorkItems(self.dag)
            items = work.list(self.ctx['request_id'])
            if name == 'list_work_items':
                return {'request_id':self.ctx['request_id'], 'items':items}
            if args['item_id'] not in {item['id'] for item in items}:
                raise PermissionError('Work item belongs to another task request')
            result = work.map(**args) if name == 'map_work_item' else work.transition(**args)
            return short_node(result)
        if name.startswith("artifact_"):
            if name == "artifact_propose":
                if args.get("path") not in self.ctx["expected"]:
                    raise PermissionError("Destination is outside the released task's write contract")
                self._check_mirror_write(args["path"], name, args)
            if args.get("proposal_id") and name in {"artifact_read", "artifact_binding_candidates", "artifact_findings",
                                                     "artifact_repair", "artifact_commit", "artifact_review"}:
                proposal = self.dag.get(args["proposal_id"])
                pp = proposal["payload"]
                if (proposal["kind"] != "artifact_check" or proposal["seq"] <= self.dag.get(self.ctx["request_id"])["seq"]
                        or pp.get("path") not in self.ctx["expected"] or pp.get("stage") != self.ctx["stage"]):
                    raise PermissionError("Staged proposal belongs to another request or destination; use a published version for history")
                if name in {"artifact_repair", "artifact_commit"}:
                    self._check_mirror_write(pp["path"], name, args, proposal)
            return self.art.call(name, args)
        if name == "list_sources":
            q = terms(args.get("query", ""))
            rows = self.dag.nodes(kind="source")
            def score(n):
                p = n["payload"]
                return (3*len(q & terms(p["locator"]))+len(q & terms(p["text"])), n["seq"])
            rows = [n for n in sorted(rows, key=score, reverse=True) if not q or score(n)[0]]
            return {"sources":[{"id":n["id"], "locator":n["payload"]["locator"],
                    "chars":len(n["payload"]["text"]), **self._source_identity(n)} for n in rows[:max(1,min(args.get("limit",25),80))]]}
        if name == "read_source":
            n = self.dag.get(args["source_id"])
            if n["kind"] != "source":
                raise ValueError("Expected a released source")
            p = n["payload"]
            pos = max(0, args.get("start",0))
            text = p["text"][pos:pos+max(64,min(args.get("size",5000),12000))]
            return {"id":n["id"],"locator":p["locator"],"text":text,"start":pos,"total":len(p["text"]), **self._source_identity(n)}
        if name == "find_claims":
            if args.get("claim_id"):
                n = self.dag.get(args["claim_id"])
                if n["kind"] != "claim":
                    raise ValueError("Choose an existing ledger claim")
                return {"claims": [self._preview_claim(n)], "known_at_seq": n["seq"], "pending": n["payload"].get("pending",False)}
            rows = ledger_search(self.dag,args.get("query",""),None,limit=args.get("limit",10))
            if args.get("detail") != "compact":
                return {"claims":[self._preview_claim(n) for n in rows]}
            compact = []
            for n in rows:
                facts = n["payload"]
                value = facts["value"]
                entry = {k:facts.get(k) for k in ("entity","field","scope","source_id","locator","start","end","valid_from","valid_to")}
                entry.update(id=n["id"], known_at_seq=n["seq"], pending=facts.get("pending",False), **self._source_identity(n))
                if isinstance(value,(list,dict)) or (isinstance(value,str) and len(value)>100):
                    entry["value_preview"] = json.dumps(value,ensure_ascii=False)[:180]
                else:
                    entry["value"] = value
                compact.append(entry)
            return {"claims":compact, "detail":"compact", "instruction":"Pass one claim_id for its full statement, original quote and structured value."}
        if name == "record_claim":
            data = dict(args)
            fiscal,lo,hi=reporting_dates(data.pop("fiscal_period",None),data.get("valid_from"),data.get("valid_to"))
            if fiscal:
                scope=dict(data.get("scope",{}))
                for k in ("fiscal_period","period","fiscal_year"):
                    if k in scope and str(scope[k]).upper().replace(" ","").startswith("FY"):
                        if reporting_dates(str(scope[k]))[0]!=fiscal:
                            raise ValueError("The claim scope does not match its reporting period")
                data["scope"]={**scope,"fiscal_period":fiscal}
            data.update(valid_from=lo,valid_to=hi)
            old_id = data.pop("old_claim_id", None)
            previous = self.dag.claim_at()
            if old_id is not None and old_id not in {n["id"] for n in previous}:
                raise ValueError("Select an existing active claim to compare")
            old = [n for n in previous if n["id"] == old_id or (
                n["payload"].get("field") == data["field"] and n["payload"].get("entity") == data["entity"]
                and n["payload"].get("scope",{}) == data.get("scope",{}) and n["payload"].get("value") != data["value"])]
            candidate_ids = [o["id"] for o in old]
            related = self.dag.candidates(data["entity"], data["field"], data.get("scope"),
                valid_from=data.get("valid_from"), valid_to=data.get("valid_to"), source_id=data["source_id"])
            n = self.dag.record_claim(**data, pending=bool(old), candidate_ids=candidate_ids)
            comparisons = [self.dag.compare(o["id"],n["id"],data["description"],
                           purpose="current ledger maintenance")["id"] for o in old if o["id"] != n["id"]]
            return {"claim":self._preview_claim(n),"comparisons":comparisons,"possible_alignments":[self._preview_claim(r) for r in related[:6]]}
        if name == "inspect_conflict":
            if self.ctx.get("self_review"):
                return self._review_comparison(args["comparison_id"], args.get("budget",12))
            return inspect_comparison(self.dag, **args)
        if name == "resolve_claim":
            if self.ctx.get("self_review"):
                cid=args["comparison_id"]
                _, prior = self._current_reviews(cid)
                existing = set(args.get("evidence_ids", []))
                if existing - set(prior):
                    raise ValueError("Use source reads obtained for this conflict in the current request")
                if not prior:
                    rec=self._review_comparison(cid,12)
                    return {"status":"review_required","self_review":rec,
                            "instruction":"Inspect the original and resubmit your chosen relation with the recorded probe ID."}
                if args["relation"]!="unresolved" and not existing:
                    raise ValueError("Use the actual evidence_ids from inspect_conflict when resolving this claim")
            if self.ctx.get("self_review"):
                cutoff = self.dag.get(self.ctx["request_id"])["seq"]
                closed = any(n["seq"] <= cutoff and n["payload"]["comparison_id"] == args["comparison_id"]
                             and n["payload"]["status"] == "accepted" for n in self.dag.nodes("revision"))
                if closed:
                    raise ValueError("Historical resolution is recorded; compare active claims for a new decision")
            result = short_node(self.dag.revise(**args))
            if self.art.review_policy == "ledger_only":
                result.pop("affected", None)
                result.pop("native_consumers", None)
            return result
        if name == "derive_amount":
            from .business_calculation import calculate
            return calculate(self.dag, self.ctx, **args)
        if name == "finish_stage":
            paths = self.ctx["expected"]
            heads = [self.art.head(path) for path in paths]
            if any(not h or not self.art.materialized(p).is_file() for p,h in zip(paths,heads)):
                raise ValueError("Required current deliverables have not all been committed")
            from .tools import digest
            for path, head in zip(paths, heads):
                facts = head.get("payload", head)
                contracts=[n for n in self.dag.nodes("artifact_contract") if n["payload"]["path"] == path]
                if (not contracts or facts.get("contract_id") != contracts[-1]["id"]
                        or facts.get("purpose") != contracts[-1]["payload"]["purpose"]
                        or facts.get("stage") != self.ctx["stage"]):
                    raise ValueError("Committed artifact must be revalidated for the current purpose and claims")
                if not facts.get("sha256") or digest(self.art.materialized(path)) != facts["sha256"]:
                    raise ValueError("Materialized file does not match validated version")
            heads = [self.art.validate_head(path) for path in paths]
            checks = []
            from .work_items import WorkItems
            work = WorkItems(self.dag)
            if self.ctx.get("self_review"):
                heads = [self.art.validate_head(path) for path in paths]
                report = check_package(self.art, paths, heads, self.ctx["stage"])
                checked = self.dag.append("package_check", {"stage": self.ctx["stage"], **report},
                                          [self.ctx["request_id"], *(h["id"] for h in heads)])
                if report["blocking"]:
                    return {"committed": False, "status": "requires_repair", "check_id": checked["id"],
                            "findings": report["blocking"][:20]}
                checks.append(checked["id"])
            completion = work.completion(self.ctx['request_id'], heads)
            if completion['blocking']:
                return {'committed':False, 'status':'requires_interpretation' if any(
                    row['state']=='uninterpreted' for row in completion['blocking']) else 'requires_work',
                    'work_items':completion['blocking']}
            work_check = self.dag.append('work_item_check', {'stage':self.ctx['stage'],
                'request_id':self.ctx['request_id'], 'ready':True,
                'item_ids':[item['id'] for item in completion['items']]},
                [self.ctx['request_id'], *(h['id'] for h in heads)])
            checks.append(work_check['id'])
            ids = [h.get("id", h.get("node_id")) for h in heads]
            node = self.dag.append("stage_submission", {"stage":self.ctx["stage"],
                        "description":args["description"],"paths":paths,
                        "versions":{p:{"id":h["id"],"sha256":h["payload"]["sha256"]} for p,h in zip(paths,heads)}},
                        [self.ctx["request_id"], *ids, *checks])
            work.deliver(self.ctx['request_id'], node)
            return {"id":node["id"],"committed":True,"paths":paths}
        raise ValueError("Unsupported tool")


    def _review_comparison(self, cid, budget):
        from .live_review import review, present
        comparison=self.dag.get(cid)
        if comparison["kind"]!="comparison":
            raise ValueError("Expected a conflict comparison")
        p=comparison["payload"]
        prior, old_probes = self._current_reviews(cid)
        query=" ".join([p["description"],p["purpose"] or "",p["old"]["entity"],p["old"]["claim"],p["new"]["claim"]])
        node=None
        if len(prior)<max(0,min(budget,12)):
            node=review(self.dag,cid,query,[cid],previous=prior,stage=self.ctx["stage"],
                        policy=self.art.review_policy,purpose_role="business_evidence")
        if node:
            np=node["payload"]
            probe=self.dag.probe(cid,np["source_id"],np["text"],"Inspect the selected original for this ledger change",
                                 start=np["start"],review_id=node["id"])
            old_probes.append(probe["id"])
        return {"comparison":p,"self_review":present(node),"evidence_ids":old_probes,
                "supported_relations":self.dag.supported_relations(cid,old_probes,purpose=p["purpose"]),
                "budget":budget,"windows_read":len(prior)+int(node is not None)}


def exact_mirror_of(payload, primary):
    source = primary["payload"]
    same_proof_scope = payload.get("proof_scope") == source.get("proof_scope")
    return (payload.get("action") in {"copy","export"} and not payload.get("changed") and not payload.get("repair_of")
            and payload.get("source_id")==primary["id"] and payload.get("sha256")==source["sha256"]
            and (not same_proof_scope or payload.get("proof_reused_from")==primary["id"]))


def check_package(art, paths, heads, stage):
    from .guarded_artifacts import _slots
    from .native_finance import inspect_consistency
    versions = {p: {"version_id": h["id"], "sha256": h["payload"]["sha256"]} for p, h in zip(paths, heads)}
    blocking, checks = [], []
    logical = []
    contracts = {h["payload"]["path"]: art.dag.get(h["payload"]["contract_id"])["payload"] for h in heads}
    mirrors = handover_mirrors(paths, stage, contracts)
    by_path = dict(zip(paths, heads))
    for path, head in by_path.items():
        primary = mirrors.get(path)
        if not primary:
            logical.append(path)
            continue
        if not exact_mirror_of(head["payload"], by_path[primary]):
            blocking.append({"kind":"mirror_mismatch", "primary":primary, "mirror":path,
                             "source_sha256":versions[primary]["sha256"],
                             "instruction":"Copy the exact current signed primary again."})
    books = [p for p in logical if p.lower().endswith(".xlsx")]
    notes = [p for p in logical if p.lower().endswith(".docx") and "assumption" in Path(p).stem.casefold()]
    for book in books:
        if not notes:
            break
        slots = _slots(art.materialized(book))
        for note in notes:
            result = inspect_consistency(slots, _slots(art.materialized(note)),
                                         workbook_name=Path(book).stem, document_name=Path(note).stem)
            checks.append({"workbook": book, "document": note, "result": result})
            for finding in result["findings"]:
                if finding.get("blocking", True):
                    blocking.append({"workbook": book, "document": note, **finding})
    return {"versions": versions, "comparisons": checks, "blocking": blocking}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace",type=Path,required=True)
    root = ap.parse_args().workspace
    for line in sys.stdin:
        try:
            req = json.loads(line)
            with project_lock(root):
                b = Bridge(root)
                if req["op"] == "catalog":
                    result = {"tools": b.catalog(), "request_id": b.ctx["request_id"], "mode": b.ctx["mode"]}
                    if b.ingest:
                        result.update(b.ingest.host_settings())
                    if b.m03:
                        result.update(b.m03.host_settings())
                elif req["op"] == "call":
                    if req["args"].get("request_id") != b.ctx["request_id"]:
                        raise PermissionError("Task context changed; reopen the current host session")
                    result = b.call(req["args"]["name"],req["args"]["arguments"])
                else:
                    raise ValueError("Unknown bridge operation")
            reply = {"ok":True,"result":result}
        except Exception as exc:
            from jsonschema import ValidationError
            from .maintenance_contract import ProtocolError, protocol_issue
            issue = (exc.issue if isinstance(exc, ProtocolError) else
                     protocol_issue(exc) if isinstance(exc, ValidationError) else None)
            reply = {"ok":False,"error": (f"{issue['path']}: {issue['message']}" if issue else str(exc)[:1400])}
            if issue:
                reply["issue"] = issue
        print(json.dumps(reply,ensure_ascii=False),flush=True)

if __name__ == "__main__":
    main()

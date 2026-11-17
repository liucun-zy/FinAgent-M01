"""Persistent interpretation and delivery state for released task requirements.

Native files can be drafted while requirements are still being interpreted.
Stage completion reads this module's version-bound work state. All updates are
append-only events, so a later mapping or review never changes an earlier view.
"""
from __future__ import annotations

import re


STATES = frozenset({
    "uninterpreted", "mapped", "waiting_evidence", "executable", "generated",
    "verified", "delivered", "exempt", "deferred",
})
_TERMINAL = {"delivered", "exempt", "deferred"}
_CANDIDATE_STATES = {"waiting_evidence", "executable"}
_PUBLIC_FIELDS = ("source_id", "locator", "quote", "start", "end")


def _ids(value):
    if value is None:
        return []
    return list(dict.fromkeys([value] if isinstance(value, str) else value))


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value.strip()


def _body(source):
    return source["payload"].get("text", source["payload"].get("instruction", ""))


def _spans(source):
    """Keep each nonempty requested line, including unfamiliar business terms."""
    body = _body(source)
    for match in re.finditer(r"[^\r\n]+", body):
        raw = match[0]
        quote = raw.strip()
        if not quote or not re.search(r"\w", quote):
            continue
        if re.fullmatch(r"(?:#{1,6}\s+)?(?:materials|deliverables|information cut[- ]off)\s*:?[\s]*",
                        quote, re.I):
            continue
        start = match.start() + len(raw) - len(raw.lstrip())
        yield {"source_id": source["id"], "locator": source["payload"].get("locator", "stage instruction"),
               "quote": quote, "start": start, "end": start + len(quote)}


def _quote(source, quote, start=None):
    quote = _text(quote, "Authorization quote")
    body = _body(source)
    if start is None:
        found = [m.start() for m in re.finditer(re.escape(quote), body)]
        if len(found) != 1:
            raise ValueError("Authorization quote must have one exact source location or an explicit start")
        start = found[0]
    if type(start) is not int or start < 0 or body[start:start + len(quote)] != quote:
        raise ValueError("Authorization quote is not at the stated original location")
    return {"source_id": source["id"], "locator": source["payload"].get("locator", "stage instruction"),
            "quote": quote, "start": start, "end": start + len(quote)}


def _required_characters(text):
    return {i for i, char in enumerate(text) if char.isalnum()}


class WorkItems:
    """Interpret requirements, record reviews and check the current deliverables."""

    def __init__(self, dag):
        self.dag = dag

    def register(self, stage_node, instruction_source, expected):
        """Register raw request spans without deciding that unfamiliar work is optional."""
        if stage_node["kind"] != "stage":
            raise ValueError("Work items require a released stage request")
        if instruction_source["kind"] not in {"source", "stage"}:
            raise ValueError("Work requirements need their original instruction")
        existing = self.dag.nodes("work_item")
        result = []
        for evidence in _spans(instruction_source):
            found = next((n for n in existing
                          if n["payload"].get("request_id") == stage_node["id"]
                          and not n["payload"].get("parent_item_id")
                          and n["payload"]["requirement"] == evidence), None)
            if found:
                result.append(found)
                continue
            payload = {"request_id": stage_node["id"], "stage": stage_node["payload"]["stage"],
                       "requirement": evidence, "description": evidence["quote"],
                       "paths": sorted(set(expected)), "required": True,
                       "state": "uninterpreted"}
            parents = [stage_node["id"], instruction_source["id"]]
            result.append(self.dag.append("work_item", payload, parents))
        return result

    def _legacy(self, request, as_of=None):
        """Project old task_request records without mutating a frozen database."""
        obligations = [n for n in self.dag.nodes("task_obligation", as_of=as_of)
                       if n["payload"].get("request_id") == request["id"]
                       and n["payload"].get("kind") == "task_request"]
        if not obligations:
            return []
        evidence = {}
        paths = set()
        for obligation in obligations:
            paths.update(obligation["payload"]["paths"])
            for item in obligation["payload"]["evidence"]:
                key = (item["source_id"], item["start"], item["end"])
                evidence[key] = {k: item[k] for k in _PUBLIC_FIELDS}
        result = []
        for index, item in enumerate(evidence.values()):
            result.append({"id": f"legacy:{request['id']}:{index}", "seq": request["seq"],
                           "kind": "work_item", "parents": [request["id"], item["source_id"]],
                           "payload": {"request_id": request["id"], "stage": request["payload"]["stage"],
                                       "requirement": item, "description": item["quote"],
                                       "paths": sorted(paths), "required": True,
                                       "state": "uninterpreted", "legacy_projection": True}})
        return result

    def list(self, request_id, as_of=None):
        request = self.dag.get(request_id)
        if request["kind"] != "stage":
            raise ValueError("Expected a stage request")
        if as_of is not None and request["seq"] > int(str(as_of).removeprefix("D")):
            raise ValueError("Task request lies outside the knowledge cutoff")
        items = [n for n in self.dag.nodes("work_item", as_of=as_of)
                 if n["payload"]["request_id"] == request_id]
        if not items:
            items = self._legacy(request, as_of)
        by_id = {n["id"]: {"id": n["id"], "seq": n["seq"], **n["payload"]} for n in items}
        for update in self.dag.nodes("work_item_update", as_of=as_of):
            payload = update["payload"]
            item = by_id.get(payload["item_id"])
            if item is None:
                continue
            item.update(payload)
            item["last_update_id"] = update["id"]
            item["last_update_seq"] = update["seq"]
            if payload["state"] == "mapped":
                item["mapping_seq"] = update["seq"]
        for item in by_id.values():
            parent = by_id.get(item.get("parent_item_id"))
            if parent is not None and item["id"] not in parent.get("child_ids", []):
                item["superseded_mapping"] = True
        for item in reversed(list(by_id.values())):
            children = [by_id[c] for c in item.get("child_ids", []) if c in by_id]
            if not children:
                continue
            states = {c["state"] for c in children}
            all_delivered = states == {"delivered"}
            item["state"] = ("delivered" if all_delivered else
                             "verified" if states <= {"verified", "delivered"} else "mapped")
            item["child_states"] = {c["id"]: c["state"] for c in children}
            item["child_summary"] = {
                "all_delivered": all_delivered,
                "all_terminal": all(c.get("child_summary", {}).get("all_terminal", c["state"] in _TERMINAL)
                                    for c in children),
                "contains_authorized_disposition": any(
                    c["state"] in {"exempt", "deferred"}
                    or c.get("child_summary", {}).get("contains_authorized_disposition", False)
                    for c in children),
            }
        return list(by_id.values())

    def _resolve(self, item_id):
        if item_id.startswith("legacy:"):
            parts = item_id.split(":")
            if len(parts) != 3:
                raise ValueError("Invalid legacy work item")
            request_id = parts[1]
            before = next((item for item in self.list(request_id) if item["id"] == item_id), None)
            if before is None:
                raise ValueError("Unknown legacy work item")
            request = self.dag.get(request_id)
            source = self.dag.get(before["requirement"]["source_id"])
            self.register(request, source, before["paths"])
            return next(item for item in self.list(request_id)
                        if item["requirement"] == before["requirement"])
        node = self.dag.get(item_id)
        if node["kind"] != "work_item":
            raise ValueError("Expected a work item")
        return next(item for item in self.list(node["payload"]["request_id"])
                    if item["id"] == item_id)

    def _targets(self, item, targets):
        if not isinstance(targets, list) or not targets:
            raise ValueError("A mapped work item needs actual target locations")
        result = []
        for target in targets:
            if not isinstance(target, dict) or set(target) - {"path", "locator"}:
                raise ValueError("Each target needs only its released path and native locator")
            path = _text(target.get("path"), "Target path")
            locator = _text(target.get("locator"), "Target locator")
            if path not in item["paths"]:
                raise ValueError("Work target lies outside the request's released deliverables")
            if {"path": path, "locator": locator} not in result:
                result.append({"path": path, "locator": locator})
        return result

    def _obligations(self, item, identifiers, targets):
        result = []
        for identifier in _ids(identifiers):
            node = self.dag.get(identifier)
            payload = node["payload"]
            if (node["kind"] != "task_obligation" or payload["request_id"] != item["request_id"]
                    or payload["kind"] == "task_request"
                    or not any(t["path"] in payload["paths"] for t in targets)):
                raise ValueError("Mechanical obligation does not verify this work item's request and targets")
            result.append(identifier)
        return result

    def _update(self, item, state, description, **fields):
        payload = {"item_id": item["id"], "request_id": item["request_id"], "state": state,
                   "description": _text(description, "Work item update description"), **fields}
        parents = [item["id"], *fields.get("check_ids", []), *fields.get("review_ids", []),
                   *fields.get("evidence_ids", []), *fields.get("child_ids", [])]
        if fields.get("authorization"):
            parents.append(fields["authorization"]["source_id"])
        if fields.get("submission_id"):
            parents.append(fields["submission_id"])
        return self.dag.append("work_item_update", payload, parents)

    def map(self, item_id, acceptance, targets, obligation_ids=()):
        item = self._resolve(item_id)
        if item["state"] in _TERMINAL:
            raise ValueError("A terminal work item requires a new request before remapping")
        targets = self._targets(item, targets)
        if isinstance(acceptance, dict):
            if set(acceptance) != {"mechanical_obligations"}:
                raise ValueError("Mechanical acceptance contains only mechanical_obligations")
            identifiers = acceptance["mechanical_obligations"]
            if not isinstance(identifiers, list) or not identifiers:
                raise ValueError("Mechanical acceptance needs explicit obligation IDs")
            if obligation_ids and set(_ids(obligation_ids)) != set(identifiers):
                raise ValueError("Mechanical acceptance and mapped obligation IDs differ")
            obligations = self._obligations(item, identifiers, targets)
            original = item["requirement"]
            for identifier in obligations:
                evidence = self.dag.get(identifier)["payload"]["evidence"]
                if not any(e["source_id"] == original["source_id"]
                           and e["start"] <= original["start"] and e["end"] >= original["end"]
                           for e in evidence):
                    raise ValueError("Mechanical obligation does not cover this exact original requirement")
            return self._update(item, "mapped", original["quote"],
                                acceptance={"mechanical_obligations": obligations},
                                verification_mode="mechanical", targets=targets,
                                obligation_ids=obligations, check_ids=[], review_ids=[], versions=[], child_ids=[])
        obligations = self._obligations(item, obligation_ids, targets)
        if isinstance(acceptance, str):
            criterion = _text(acceptance, "Acceptance criterion")
            return self._update(item, "mapped", item["requirement"]["quote"],
                                acceptance=criterion, verification_mode="review", targets=targets, obligation_ids=obligations,
                                check_ids=[], review_ids=[], versions=[], child_ids=[])
        if not isinstance(acceptance, list) or not acceptance:
            raise ValueError("Acceptance needs a criterion or a complete list of exact request clauses")
        original = item["requirement"]["quote"]
        covered, parts = set(), []
        for part in acceptance:
            if not isinstance(part, dict) or set(part) - {"quote", "criterion", "targets"}:
                raise ValueError("Each split clause needs quote, criterion and optional targets")
            quote = _text(part.get("quote"), "Split requirement quote")
            matches = [m.start() for m in re.finditer(re.escape(quote), original)]
            if len(matches) != 1:
                raise ValueError("A split clause must have one exact location in its original request")
            start = matches[0]
            covered.update(range(start, start + len(quote)))
            criterion = _text(part.get("criterion"), "Split acceptance criterion")
            part_targets = self._targets(item, part.get("targets", targets))
            part_obligations = self._obligations(item, obligations, part_targets)
            parts.append((criterion, part_targets, part_obligations, quote, start))
        if not _required_characters(original) <= covered:
            raise ValueError("Split work items leave original requested content uninterpreted")
        children = []
        for criterion, part_targets, part_obligations, quote, offset in parts:
            requirement = {**item["requirement"], "quote": quote,
                           "start": item["requirement"]["start"] + offset,
                           "end": item["requirement"]["start"] + offset + len(quote)}
            payload = {"request_id": item["request_id"], "stage": item["stage"],
                       "requirement": requirement, "description": quote,
                       "paths": item["paths"], "required": True,
                       "parent_item_id": item["id"], "state": "uninterpreted"}
            child = self.dag.append("work_item", payload, [item["id"], requirement["source_id"]])
            mapped = self.map(child["id"], criterion, part_targets, part_obligations)
            children.append(mapped["payload"]["item_id"])
        return self._update(item, "mapped", "Original request explicitly decomposed into covered clauses",
                            acceptance="all child work items", verification_mode="review", targets=targets, obligation_ids=obligations,
                            child_ids=children, check_ids=[], review_ids=[], versions=[])

    def _check_targets(self, item, check_ids, *, ready):
        from .guarded_artifacts import ArtifactOperator, _slots

        if not check_ids:
            raise ValueError("Generated and verified work needs actual artifact check receipts")
        request = self.dag.get(item["request_id"])
        operator = ArtifactOperator(self.dag.workspace, self.dag)
        checks, slots = [], {}
        for identifier in check_ids:
            check = self.dag.get(identifier)
            payload = check["payload"]
            if (check["kind"] != "artifact_check" or check["seq"] <= request["seq"]
                    or payload.get("stage") != item["stage"]
                    or payload.get("path") not in item["paths"]
                    or not payload.get("sha256")):
                raise ValueError("Work receipt is not a native check for this request")
            if ready and payload.get("status") != "ready":
                raise ValueError("Verified work requires ready native checks")
            native = operator._blob(payload["path"], payload["sha256"])
            slots[identifier] = _slots(native)
            checks.append(check)
        versions = []
        for target in item["targets"]:
            found = []
            for check in checks:
                if check["payload"]["path"] != target["path"]:
                    continue
                key, _, line = target["locator"].partition("#line:")
                values = slots[check["id"]]
                value = values.get(key)
                if line:
                    lines = str(value or "").splitlines()
                    value = lines[int(line) - 1] if line.isdigit() and 0 < int(line) <= len(lines) else None
                if value is not None and str(value).strip():
                    found.append(check)
            if len(found) != 1:
                raise ValueError("Every acceptance target must exist in exactly one supplied native version")
            check = found[0]
            versions.append({"path": target["path"], "locator": target["locator"],
                             "check_id": check["id"], "sha256": check["payload"]["sha256"]})
        if ready:
            passed = {r["obligation_id"] for check in checks
                      for r in check["payload"].get("obligation_checks", []) if r["status"] == "satisfied"}
            if not set(item.get("obligation_ids", [])) <= passed:
                raise ValueError("One or more mapped mechanical obligations are still open")
        return checks, versions

    @staticmethod
    def _mechanical_targets(item, checks):
        identifiers = set(item["acceptance"]["mechanical_obligations"])
        for target in item["targets"]:
            key = target["locator"].split("#line:")[0]
            covered = any(
                check["payload"]["path"] == target["path"]
                and any(row["obligation_id"] in identifiers and row["status"] == "satisfied"
                        and (target["locator"] in row["locators"] or key in row["locators"])
                        for row in check["payload"].get("obligation_checks", []))
                for check in checks
            )
            if not covered:
                raise ValueError("A mapped target has no satisfied mechanical obligation at its exact native location")

    def _reviews(self, item, checks, review_ids):
        from .live_review import source_role

        if not review_ids:
            raise ValueError("Verify each mapped criterion against a fresh source-grounded review")
        by_check = {c["id"]: c for c in checks}
        reviewed = set()
        for identifier in review_ids:
            review = self.dag.get(identifier)
            payload = review["payload"]
            if review["seq"] <= item.get("mapping_seq", item["seq"]):
                raise ValueError("Work review predates the current acceptance mapping")
            if review["kind"] == "artifact_review_decision":
                check_id = payload.get("proposal_id")
                read = self.dag.get(payload["review_id"])
                if check_id in by_check and payload.get("sha256") != by_check[check_id]["payload"]["sha256"]:
                    raise ValueError("Work review attests a different native version")
            elif review["kind"] == "ledger_self_review":
                check_id = payload.get("target_id")
                read = review
            else:
                raise ValueError("Work verification needs an actual artifact source review")
            if check_id not in by_check or read["kind"] != "ledger_self_review":
                raise ValueError("Work review belongs to another checked artifact")
            source = self.dag.get(read["payload"]["source_id"])
            if source["kind"] != "source" or source_role(source["payload"]) != "business_evidence":
                raise ValueError("A task instruction cannot verify its own business completion")
            lo, hi = read["payload"]["start"], read["payload"]["end"]
            if source["payload"]["text"][lo:hi] != read["payload"]["text"]:
                raise ValueError("Work review no longer matches its original source window")
            reviewed.add(check_id)
        if set(by_check) != reviewed:
            raise ValueError("Each acceptance output needs its own source-grounded review")

    def _authorization(self, item, state, source_id, quote, start):
        if not source_id:
            raise ValueError("Scope exceptions require an explicit released instruction source")
        source = self.dag.get(source_id)
        payload = source["payload"]
        original = item["requirement"]
        request = self.dag.get(item["request_id"])
        trusted = (source["id"] == original["source_id"] or source["id"] == request["id"]
                   or source["kind"] == "source" and payload.get("locator", "").startswith("instructions/")
                   and payload.get("stage") == item["stage"])
        if not trusted:
            raise ValueError("Only the original request or released task instructions can authorize this disposition")
        evidence = _quote(source, quote, start)
        body = _body(source)
        left = list(re.finditer(r"[.!?。！？\n]", body[:evidence["start"]]))
        lo = left[-1].end() if left else 0
        tail = re.search(r"[.!?。！？\n]", body[evidence["end"]:])
        hi = (evidence["end"] if evidence["quote"][-1] in ".!?。！？\n" else
              evidence["end"] + tail.end() if tail else len(body))
        context = body[lo:hi]
        forbidden = (r"\b(?:do not|don't|must not|shall not|may not|cannot|can not|not)\s+(?:be\s+)?(?:defer\w*|postpone\w*|delay\w*)\b|(?:不得|不可|不能|禁止|不允许|不应).{0,12}(?:延期|暂缓)"
                     if state == "deferred" else
                     r"\b(?:do not|don't|must not|shall not|may not|cannot|can not|not)\s+(?:be\s+)?(?:omit\w*|exclud\w*|waiv\w*)\b|(?:不得|不可|不能|禁止|不允许).{0,12}(?:省略|豁免|取消)")
        if re.search(forbidden, context, re.I) or re.search(
                r"\b(?:if|unless|subject to|pending approval)\b|如果|除非|若|待批准|待审批", context, re.I):
            raise ValueError("The instruction prohibits or conditions this disposition")
        pattern = (r"\b(?:not required|not necessary|need not|do not (?:assess|evaluate|perform|prepare|provide|include|execute)|out of scope|waiv\w*|omit\w*|excluded)\b|无需|不必|免于|不要求|不纳入"
                   if state == "exempt" else
                   r"\b(?:may be deferred|can be deferred|defer\w*|postpone\w*|may be delayed|can be delayed)\b|允许延期|可以延期|可延期|允许暂缓|可暂缓|延期|暂缓")
        if not re.search(pattern, evidence["quote"], re.I):
            raise ValueError("The quoted instruction does not expressly authorize this disposition")
        stop = {"a", "an", "the", "and", "or", "in", "on", "of", "to", "for", "with", "this", "that",
                "from", "must", "shall", "should", "provide", "prepare", "include", "identify", "assess",
                "evaluate", "analysis", "report", "分析", "评估", "识别", "准备", "报告", "工作", "要求",
                "应当", "提供", "完成", "相关", "本次"}
        terms = {t.casefold() for t in re.findall(r"[A-Za-z][A-Za-z0-9_-]*", original["quote"])}
        for word in re.findall(r"[\u4e00-\u9fff]+", original["quote"]):
            terms.update(word[i:i + 2] for i in range(len(word) - 1))
        terms -= stop
        if terms and not any(term in evidence["quote"].casefold() for term in terms):
            raise ValueError("Authorization quote does not identify this requested work")
        return evidence

    def transition(self, item_id, state, description, *, check_id=None, review_id=None,
                   evidence_ids=(), source_id=None, quote=None, start=None):
        item = self._resolve(item_id)
        if state not in STATES:
            raise ValueError("Unknown work item state")
        if state in {"uninterpreted", "mapped", "delivered"}:
            raise ValueError("Use mapping or the host delivery operation for that transition")
        if item["state"] in _TERMINAL:
            raise ValueError("The work item already has a terminal disposition")
        if item.get("child_ids"):
            raise ValueError("Update the decomposed child work items individually")
        if state in {"exempt", "deferred"}:
            authorization = self._authorization(item, state, source_id, quote, start)
            return self._update(item, state, description, authorization=authorization)
        if not item.get("targets") or not item.get("acceptance"):
            raise ValueError("Interpret the original requirement and map acceptance targets first")
        evidence = _ids(evidence_ids)
        for identifier in evidence:
            node = self.dag.get(identifier)
            if node["kind"] not in {"source", "claim", "probe", "ledger_self_review"}:
                raise ValueError("Work evidence must refer to actual released material or a recorded read")
        if state in _CANDIDATE_STATES:
            return self._update(item, state, description, evidence_ids=evidence)
        checks = _ids(check_id)
        checked, versions = self._check_targets(item, checks, ready=state == "verified")
        reviews = _ids(review_id)
        if state == "verified":
            if item.get("verification_mode") == "mechanical":
                self._mechanical_targets(item, checked)
                if reviews:
                    self._reviews(item, checked, reviews)
            else:
                self._reviews(item, checked, reviews)
        return self._update(item, state, description, check_ids=checks, review_ids=reviews,
                            versions=versions, evidence_ids=evidence)

    def completion(self, request_id, heads):
        """Preflight required leaf items against the exact heads about to be submitted."""
        items = self.list(request_id)
        heads = list(heads.values()) if isinstance(heads, dict) else list(heads)
        versions = {n["payload"]["path"]: n for n in heads}
        blocking = []
        for item in items:
            if item.get("child_ids") or item.get("superseded_mapping") or not item.get("required", True):
                continue
            state = item["state"]
            reason = None
            if state not in {"verified", "delivered", "exempt", "deferred"}:
                reason = ("Interpret and map the original request before completion" if state == "uninterpreted"
                          else "The required work has no verified or authorized terminal result")
            elif state in {"verified", "delivered"}:
                targets = item.get("versions", [])
                if not targets or any(target["path"] not in versions
                                      or versions[target["path"]]["payload"]["sha256"] != target["sha256"]
                                      for target in targets):
                    reason = "The work review belongs to a different delivered byte version"
            elif not item.get("authorization"):
                reason = "The work disposition has no original instruction authorization"
            if reason:
                blocking.append({"item_id": item["id"], "state": state,
                                 "quote": item["requirement"]["quote"], "paths": item["paths"],
                                 "reason": reason})
        return {"request_id": request_id, "items": items, "blocking": blocking,
                "ready": not blocking}

    def deliver(self, request_id, submission):
        if submission["kind"] != "stage_submission" or request_id not in submission["parents"]:
            raise ValueError("Delivery needs the successful submission for this request")
        payload = submission["payload"]
        heads = [self.dag.get(row["id"]) for row in payload["versions"].values()]
        report = self.completion(request_id, heads)
        if report["blocking"]:
            raise ValueError("The submitted files do not satisfy their work item reviews")
        delivered = []
        for item in report["items"]:
            if item.get("child_ids") or item.get("superseded_mapping") or item["state"] != "verified":
                continue
            delivered.append(self._update(item, "delivered", "Verified work delivered in the current stage",
                                          submission_id=submission["id"]))
        return delivered

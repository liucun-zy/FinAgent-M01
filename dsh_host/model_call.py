"""One audited DSH model request, with a budget shared by project sessions."""
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path

from action_trace_demo.model import BudgetExceeded, ModelClient, SharedAttemptBudget


class IngestModelClient(ModelClient):
    def __init__(self, config, session, *, scope, attempt_tags=None, attempt_limits=None, **kwargs):
        super().__init__(config, session, **kwargs)
        self.shared_budget = SharedAttemptBudget(scope)
        self.attempt_tags = attempt_tags or {}
        self.attempt_limits = attempt_limits or {}

    def _check_budget(self, reservation):
        super()._check_budget(reservation)
        # ModelClient invokes this once for every attempt, including its retries.
        self.shared_budget.update(session=self.run_dir, sequence=self._sequence + 1,
                                  reservation=reservation, max_calls=self.max_calls,
                                  max_cost=self.max_cost_rmb, tags=self.attempt_tags, limits=self.attempt_limits)


def ingest_completion(client, payload):
    try:
        response = client.complete(payload["messages"], tools=payload["tools"],
                                   task=payload.get("task", "dsh_ingest"))
    finally:
        # A process crash leaves reservations charged. Normal completion replaces
        # reservations with the existing ModelClient's audited actual/unknown cost.
        shared = client.shared_budget.update()
    metrics = client.metrics()
    attempts = list(shared["attempts"].values())
    current = str(client.run_dir.relative_to(client.shared_budget.scope))
    metrics.update(shared_budget_scope=str(client.shared_budget.scope),
                   prior_session_calls=sum(a["session"] != current for a in attempts),
                   prior_session_accounted_rmb=sum(a["accounted_rmb"] for a in attempts if a["session"] != current),
                   shared_attempts_reserved_or_logged=len(attempts),
                   shared_pending_attempts=sum(a["status"] == "reserved" for a in attempts),
                   shared_accounted_rmb=sum(a["accounted_rmb"] for a in attempts))
    return {"completion": response, "usage": metrics}


def inherited_budget(scope, current):
    calls = 0
    cost = 0.0
    for log in scope.glob("*/model_calls.jsonl"):
        if log.parent == current:
            continue
        with log.open(encoding="utf8") as stream:
            for line in stream:
                if line.strip():
                    entry = json.loads(line)
                    calls += 1
                    cost += float(entry["budget_accounted_cost_rmb"])
    return calls, cost


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    session = Path(sys.argv[2]).resolve()
    scope = session.parent if session.parent.name == ".dsh" else session
    payload = json.load(sys.stdin)
    if config.get("input_budget_tokens") is not None:
        from action_trace_demo.evidence_windows import token_estimate
        estimated = token_estimate({"messages": payload["messages"], "tools": payload["tools"]})
        if estimated > config["input_budget_tokens"]:
            json.dump({"budget_error": f"Request input estimate {estimated} exceeds configured {config['input_budget_tokens']} tokens; no transport attempted"}, sys.stdout)
            return
    settings = config["model"] if isinstance(config.get("model"), dict) else config
    settings["extra_headers"] = {**payload["attribution"], **settings.get("extra_headers", {})}
    if config.get("ingest_parallel_budget") is True:
        stage = "m03" if payload.get("task") == "dsh_m03_maintenance" else "m01"
        tags = {"stage": stage, **config.get("attempt_tags", {})}
        limits = dict(config.get("attempt_limits", {}))
        if stage in config.get("transport_stage_limits", {}):
            limits["stage"] = config["transport_stage_limits"][stage]
        client = IngestModelClient(config, session, scope=scope, attempt_tags=tags, attempt_limits=limits)
        try:
            receipt = ingest_completion(client, payload)
        except BudgetExceeded as exc:
            receipt = {"budget_error": str(exc)}
        json.dump(receipt, sys.stdout, ensure_ascii=False)
        return
    # Keep transport retries, reservations and the append-only ledger under one
    # interprocess lock; parallel QA and stage runs cannot spend the same balance.
    with (scope / ".model-budget.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        client = ModelClient(config, session)
        configured_calls, configured_cost = client.max_calls, client.max_cost_rmb
        calls, cost = inherited_budget(scope, session) if scope != session else (0, 0.0)
        client.max_calls -= calls
        client.max_cost_rmb -= cost
        try:
            response = client.complete(payload["messages"], tools=payload["tools"],
                                       task=payload.get("task", "dsh_candidate"))
        except BudgetExceeded as exc:
            # The exception contains policy information only (no request or credentials).
            json.dump({"budget_error":str(exc)}, sys.stdout)
            return
        metrics = client.metrics()
        metrics.update(max_calls=configured_calls, max_cost_rmb=configured_cost,
                       shared_budget_scope=str(scope), prior_session_calls=calls,
                       prior_session_accounted_rmb=cost)
        json.dump({"completion":response, "usage":metrics},sys.stdout,ensure_ascii=False)


if __name__ == "__main__":
    main()

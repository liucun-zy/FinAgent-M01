"""Audited, budgeted Chat Completions Adapter for the Efunds endpoint.

The transport never logs credentials or request headers. Pricing is an estimate
using the caller's RMB-per-million-token rates, not a provider invoice. Missing
usage stays unknown and retains the pre-request budget reservation.
"""
from __future__ import annotations

import fcntl
import tempfile
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable


class BudgetExceeded(RuntimeError):
    """The next attempt does not fit the configured run budget."""


class SharedAttemptBudget:
    """Reserve each ingest transport attempt; the lock never covers the request."""

    def __init__(self, scope):
        self.scope = Path(scope)
        self.scope.mkdir(parents=True, exist_ok=True)
        self.path = self.scope / ".model-budget-reservations.json"

    def _reconcile(self):
        state = json.loads(self.path.read_text()) if self.path.exists() else {"attempts": {}}
        # The normal audit remains authoritative once an attempt has finished.
        # An unfinished last line is still covered by its durable reservation.
        logs = list(self.scope.glob("*/model_calls.jsonl"))
        if (self.scope / "model_calls.jsonl").exists():
            logs.append(self.scope / "model_calls.jsonl")
        for log in logs:
            for line in log.read_text().splitlines(keepends=True):
                if not line.endswith("\n") or not line.strip():
                    continue
                record = json.loads(line)
                key = self._key(log.parent, record["call_sequence"])
                state["attempts"][key] = {
                    **state["attempts"].get(key, {}),
                    "session": str(log.parent.relative_to(self.scope)),
                    "call_sequence": record["call_sequence"],
                    "accounted_rmb": record["budget_accounted_cost_rmb"],
                    "status": "logged",
                }
        return state

    def _key(self, session, sequence):
        return f"{Path(session).relative_to(self.scope)}:{sequence}"

    def _write(self, state):
        with tempfile.NamedTemporaryFile("w", dir=self.scope, delete=False, encoding="utf8") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stream.name, self.path)

    def update(self, *, session=None, sequence=None, reservation=None, max_calls=None, max_cost=None, tags=None, limits=None,
               recovery_slots=None):
        with (self.scope / ".model-budget.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self._reconcile()
            attempts = state["attempts"]
            holds = state.setdefault("recovery_slots", {})
            tags, limits = tags or {}, limits or {}
            def fits(count, own_slot=None):
                pending = [v for k, v in holds.items() if k != own_slot]
                if len(attempts) + len(pending) + count > max_calls:
                    raise BudgetExceeded("Maximum shared model call attempts reached")
                for name, limit in limits.items():
                    value = tags[name]
                    used = sum(a.get("tags", {}).get(name) == value for a in attempts.values())
                    used += sum(h.get("tags", {}).get(name) == value for h in pending)
                    if used + count > limit:
                        raise BudgetExceeded(f"Maximum {name} transport attempts reached")
            if recovery_slots is not None:
                fresh = [slot for slot in recovery_slots if slot not in holds]
                fits(len(fresh))
                for slot in fresh:
                    holds[slot] = {"tags": tags}
            if reservation is not None:
                key = self._key(session, sequence)
                if key in attempts:
                    raise BudgetExceeded("Unfinished reserved model attempt requires audit before resuming")
                slot = tags.get("recovery_slot")
                fits(1, slot)
                if sum(a["accounted_rmb"] for a in attempts.values()) + reservation > max_cost + 1e-12:
                    raise BudgetExceeded("Next model attempt exceeds the shared reserved RMB budget")
                holds.pop(slot, None)
                attempts[key] = {"session": str(Path(session).relative_to(self.scope)),
                                 "call_sequence": sequence, "accounted_rmb": reservation,
                                 "status": "reserved", "tags": tags}
            self._write(state)
            return state


class ModelCallError(RuntimeError):
    """A sanitized transport failure; inspect the audit log for type/status."""


DEFAULT_HEADERS = {
    "Efunds-User-Name": "xinyuwang",
    "Efunds-Acc-Token": "xinyuwang",
    "Efunds-Source": "Financial technology",
}


def _token_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _usage(response: dict[str, Any]) -> dict[str, Any]:
    usage = response.get("usage") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    return {
        "input_tokens": _token_count(usage.get("prompt_tokens", usage.get("input_tokens"))),
        "output_tokens": _token_count(usage.get("completion_tokens", usage.get("output_tokens"))),
        "cached_tokens": _token_count(prompt_details.get("cached_tokens")),
        "reasoning_tokens": _token_count(completion_details.get("reasoning_tokens")),
    }


class ModelClient:
    """Small Interface shared by the Candidate and diagnostic call paths.

    ``client`` and ``sleep`` are internal test seams; production callers only
    need ``config`` and ``run_dir``. SDK retries are disabled so each network
    attempt is separately budgeted and audited by this Adapter.
    """

    def __init__(
        self, config: dict[str, Any], run_dir: Path,
        *, client: Any = None, sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        settings = dict(config)
        if isinstance(config.get("model"), dict):
            settings.update(config["model"])
        budget = config.get("run_budget") or {}
        self.base_url = settings.get("endpoint") or settings.get("base_url") or "https://aigc.efunds.com.cn/v1"
        configured_model = settings.get("model")
        self.model = (
            configured_model if isinstance(configured_model, str) and configured_model
            else settings.get("model_name") or "gpt-6-luna"
        )
        self.temperature = settings.get("temperature", 1.0)
        self.max_output_tokens = int(settings.get("max_output_tokens", 8192))
        self.max_calls = int(settings.get("max_calls", budget.get("max_model_calls_including_retries", 200)))
        self.max_cost_rmb = float(settings.get("max_cost_rmb", budget.get("max_cost_rmb", 30.0)))
        self.input_rate = float(settings.get("input_rmb_per_million_tokens", 0.8))
        self.output_rate = float(settings.get("output_rmb_per_million_tokens", 4.0))
        self.max_retries = int(settings.get("max_transport_retries", 2))
        self.timeout = float(settings.get("request_timeout_seconds", 120))
        self.max_tokens_parameter = settings.get("max_tokens_parameter", "max_tokens")
        if self.max_tokens_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("max_tokens_parameter must be max_tokens or max_completion_tokens")
        if self.max_output_tokens < 1 or self.max_calls < 1 or self.max_cost_rmb < 0:
            raise ValueError("Invalid model budget")
        if self.input_rate < 0 or self.output_rate < 0 or self.max_retries < 0:
            raise ValueError("Invalid pricing or retry configuration")
        self.headers = dict(DEFAULT_HEADERS)
        self.headers.update(settings.get("extra_headers") or {})
        for key, value in self.headers.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("Header names and values must be strings")
            try:
                key.encode("ascii")
                value.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError("Header names and values must contain ASCII only") from exc
        self._api_key = self._load_api_key(settings, required=client is None)
        self._sleep = sleep
        self.run_dir = Path(run_dir)
        self.calls_dir = self.run_dir / "model_calls"
        self.calls_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.run_dir / "model_calls.jsonl"
        self._records: list[dict[str, Any]] = []
        if self.log_path.exists():
            for line in self.log_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    if not isinstance(record, dict) or "budget_accounted_cost_rmb" not in record:
                        raise ValueError("Existing model audit log has incompatible records")
                    self._records.append(record)
        self._sequence = max((int(r["call_sequence"]) for r in self._records), default=0)
        self._lock = threading.Lock()
        if client is None:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=self._api_key, base_url=self.base_url,
                timeout=self.timeout, max_retries=0,
            )
        else:
            self._client = client

    @staticmethod
    def _load_api_key(settings: dict[str, Any], *, required: bool) -> str:
        key = os.environ.get(settings.get("api_key_env", "FINAGENT_API_KEY"), "").strip()
        if not key and settings.get("api_key_file"):
            key = Path(settings["api_key_file"]).expanduser().read_text(encoding="utf-8").strip()
        if required and not key:
            raise ValueError("Model credential is not configured")
        return key

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return value.replace(self._api_key, "[REDACTED]") if self._api_key else value
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {
                key: ("[REDACTED]" if key.lower() in {
                    "authorization", "api_key", "api-key", "extra_headers", "headers",
                } else self._redact(item))
                for key, item in value.items()
            }
        return value

    def _write_json(self, path: Path, value: Any) -> None:
        path.write_text(json.dumps(self._redact(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _budget_reservation(self, payload: dict[str, Any], max_tokens: int) -> float:
        # Byte count plus framing allowance is deliberately conservative, not
        # an exact tokenizer result. Missing usage retains this estimate.
        input_upper_estimate = len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 256
        return (input_upper_estimate * self.input_rate + max_tokens * self.output_rate) / 1_000_000

    def _check_budget(self, reservation: float) -> None:
        if len(self._records) >= self.max_calls:
            raise BudgetExceeded("Maximum model call attempts reached")
        accounted = sum(record["budget_accounted_cost_rmb"] for record in self._records)
        if accounted + reservation > self.max_cost_rmb + 1e-12:
            raise BudgetExceeded("Next model attempt exceeds the reserved RMB budget")

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None)
        if status is not None:
            return status == 429 or 500 <= status <= 599
        return isinstance(exc, (TimeoutError, ConnectionError, OSError)) or type(exc).__name__ in {
            "APIConnectionError", "APITimeoutError", "ConnectError", "ReadTimeout",
            "ConnectTimeout", "WriteTimeout", "PoolTimeout", "RemoteProtocolError",
        }

    def complete(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None,
        task: str = "candidate", max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a standard ChatCompletion dictionary, preserving tool calls.

        Retries consume separate call attempts. Unknown usage is logged as
        null and budgeted using the reservation, never as a free/zero call.
        """
        with self._lock:
            output_limit = self.max_output_tokens if max_tokens is None else int(max_tokens)
            if output_limit < 1:
                raise ValueError("max_tokens must be positive")
            payload: dict[str, Any] = {
                "model": self.model, "messages": messages,
                "temperature": self.temperature, "stream": False,
                self.max_tokens_parameter: output_limit,
            }
            if tools:
                payload["tools"] = tools
            if response_format is not None:
                if response_format.get("type") not in {"json_object","json_schema"}:
                    raise ValueError("Structured responses require json_object or json_schema")
                payload["response_format"] = json.loads(json.dumps(response_format,allow_nan=False))
            reservation = self._budget_reservation(payload, output_limit)
            for attempt in range(self.max_retries + 1):
                self._check_budget(reservation)
                self._sequence += 1
                call_id = f"call_{self._sequence:06d}"
                request_path = self.calls_dir / f"{call_id}.request.json"
                response_path = self.calls_dir / f"{call_id}.response.json"
                if request_path.exists() or response_path.exists():
                    raise ModelCallError("Unfinished prior call files require audit before resuming")
                self._write_json(request_path, payload)
                started = time.perf_counter()
                response: dict[str, Any] | None = None
                failure: Exception | None = None
                try:
                    raw = self._client.chat.completions.create(**payload, extra_headers=self.headers)
                    response = raw if isinstance(raw, dict) else raw.model_dump(mode="json")
                    if not isinstance(response, dict):
                        raise TypeError("Chat completion must serialize to an object")
                except Exception as exc:
                    failure = exc
                elapsed_ms = round((time.perf_counter() - started) * 1000)
                token_usage = _usage(response or {})
                usage_complete = token_usage["input_tokens"] is not None and token_usage["output_tokens"] is not None
                estimated_cost = (
                    (token_usage["input_tokens"] * self.input_rate + token_usage["output_tokens"] * self.output_rate) / 1_000_000
                    if usage_complete else None
                )
                status_code = getattr(failure, "status_code", None) if failure else None
                record = {
                    "call_id": call_id, "call_sequence": self._sequence, "task": task,
                    "model": self.model, "attempt_in_request": attempt + 1,
                    "status": "failed" if failure else "succeeded",
                    "elapsed_ms": elapsed_ms, **token_usage,
                    "usage_complete": usage_complete,
                    "estimated_cost_rmb": estimated_cost,
                    "reserved_cost_rmb": reservation,
                    "budget_accounted_cost_rmb": estimated_cost if usage_complete else reservation,
                    "cost_basis": "provider_reported_tokens_at_configured_rates" if usage_complete else "unpriced_attempt_reservation",
                    "request_ref": str(request_path.relative_to(self.run_dir)),
                    "response_ref": str(response_path.relative_to(self.run_dir)) if response is not None else None,
                    "provider_response_id": (response or {}).get("id"),
                    "failure_type": type(failure).__name__ if failure else None,
                    "http_status": status_code,
                }
                if response is not None:
                    self._write_json(response_path, response)
                with self.log_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(self._redact(record), ensure_ascii=False) + "\n")
                self._records.append(record)
                if failure is None:
                    return response  # type: ignore[return-value]
                if not self._retryable(failure) or attempt >= self.max_retries:
                    suffix = f" (HTTP {status_code})" if status_code is not None else ""
                    raise ModelCallError(f"Model request failed: {type(failure).__name__}{suffix}") from None
                self._sleep(min(2 ** attempt, 8))
            raise AssertionError("unreachable")

    def metrics(self) -> dict[str, Any]:
        """Return complete totals only when every attempt reported token usage."""
        usage_complete = all(record["usage_complete"] for record in self._records)
        known_input = sum(record["input_tokens"] or 0 for record in self._records)
        known_output = sum(record["output_tokens"] or 0 for record in self._records)
        known_cost = sum(record["estimated_cost_rmb"] or 0 for record in self._records)
        return {
            "model": self.model,
            "calls": len(self._records),
            "successful_calls": sum(record["status"] == "succeeded" for record in self._records),
            "failed_calls": sum(record["status"] == "failed" for record in self._records),
            "usage_complete": usage_complete,
            "input_tokens": known_input if usage_complete else None,
            "output_tokens": known_output if usage_complete else None,
            "known_input_tokens": known_input,
            "known_output_tokens": known_output,
            "estimated_cost_rmb": known_cost if usage_complete else None,
            "known_cost_rmb": known_cost,
            "budget_accounted_rmb": sum(record["budget_accounted_cost_rmb"] for record in self._records),
            "unpriced_attempts": sum(not record["usage_complete"] for record in self._records),
            "elapsed_ms": sum(record["elapsed_ms"] for record in self._records),
            "pricing_unit": "RMB per 1,000,000 tokens",
            "input_rmb_per_million_tokens": self.input_rate,
            "output_rmb_per_million_tokens": self.output_rate,
            "max_calls": self.max_calls,
            "max_cost_rmb": self.max_cost_rmb,
        }

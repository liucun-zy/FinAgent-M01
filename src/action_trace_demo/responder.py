"""Isolated host bridge to DeliverBench's real artifact-dependent Responder.

The bridge does not implement business rules itself. It calls the published
``commit_event`` protocol with a separate, audited model client and exposes only
receipt-listed public files. It never creates native Candidate-success receipts.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
from typing import Any, Callable

from .benchmark import CaseProtocolError, _files, _inside, _sha
from .model import ModelClient


class ResponderBridgeError(CaseProtocolError):
    """A real response could not be safely committed and published."""


_ENV_LOCK = threading.RLock()


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


@contextmanager
def _backend_environment(configuration: dict[str, Any]):
    """Pin official cache metadata without changing the Candidate's credentials."""
    values = {
        "RESPONDER_BASE_URL": configuration["base_url"],
        "RESPONDER_MODEL": configuration["model"],
        "RESPONDER_API": "chat",
        "RESPONDER_REASONING_EFFORT": "client_managed",
        "RESPONDER_TIMEOUT_SEC": str(configuration["timeout_seconds"]),
        "RESPONDER_MAX_TRIES": "1",
    }
    with _ENV_LOCK:
        if os.environ.get("DELIVERBENCH_RESPONSE_FIXTURES"):
            raise ResponderBridgeError("Synthetic Responder fixture mode is forbidden")
        old = {key: os.environ.get(key) for key in values}
        os.environ.update(values)
        try:
            yield
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class ResponderBridge:
    """Callable ``bridge(workspace, stage_root) -> public_only_directory``.

    ``model_factory`` and ``commit_event_fn`` are internal seams for offline
    protocol tests. Production calls use ModelClient and official commit_event.
    No real private event definition is read until T11 is entered.
    """

    def __init__(
        self, case_root: Path, run_dir: Path, config: dict[str, Any], *,
        model_factory: Callable[..., Any] = ModelClient,
        commit_event_fn: Callable[..., Any] | None = None,
    ):
        self.case_root = Path(case_root).resolve()
        self.task_root = _inside(self.case_root, "task")
        self.run_dir = Path(run_dir).resolve()
        self.private_root = self.run_dir / "private_responder"
        if self.private_root.is_symlink():
            raise ResponderBridgeError("Private Responder storage cannot be a symlink")
        self.private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.private_root.chmod(0o700)
        self.config = dict(config)
        self.model_factory = model_factory
        self.commit_event_fn = commit_event_fn
        self._model: Any = None
        self._cached_calls = 0
        metrics_path = self.private_root / "metrics.json"
        self._prior_metrics = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.exists() else {}
        self._prior_cached_calls = int(self._prior_metrics.get("cached_structured_calls", 0))
        settings = dict(config)
        if isinstance(config.get("model"), dict):
            settings.update(config["model"])
        model = settings.get("model")
        self.configuration = {
            "backend": "action_trace_demo.ModelClient",
            "base_url": settings.get("endpoint") or settings.get("base_url") or "https://aigc.efunds.com.cn/v1",
            "model": model if isinstance(model, str) else settings.get("model_name", "gpt-6-luna"),
            "temperature": settings.get("temperature", 1.0),
            "max_output_tokens": settings.get("max_output_tokens", 8192),
            "timeout_seconds": int(settings.get("request_timeout_seconds", 120)),
            "transport_retries": settings.get("max_transport_retries", 2),
            "input_rmb_per_million_tokens": settings.get("input_rmb_per_million_tokens", 0.8),
            "output_rmb_per_million_tokens": settings.get("output_rmb_per_million_tokens", 4.0),
            "header_configuration_sha256": _digest(settings.get("extra_headers", {})),
            "bridge_code_sha256": _sha(Path(__file__)),
            "model_adapter_code_sha256": _sha(Path(__file__).with_name("model.py")),
            "structured_response_format": "strict_json_schema",
        }
        self.configuration_sha256 = _digest(self.configuration)

    def _official_commit(self) -> Callable[..., Any]:
        if self.commit_event_fn is not None:
            return self.commit_event_fn
        checkout = self.case_root.parent.parent
        source = checkout / "deliverbench-harbor" / "src"
        if not (source / "deliverbench_harbor" / "responses.py").is_file():
            raise ResponderBridgeError("The official DeliverBench Responder source is unavailable")
        if str(source) not in sys.path:
            sys.path.insert(0, str(source))
        return importlib.import_module("deliverbench_harbor.responses").commit_event

    def _event(self) -> dict[str, Any]:
        path = _inside(self.task_root, "responses/config.json")
        value = json.loads(path.read_text(encoding="utf-8"))
        events = [e for e in value.get("events", []) if e.get("id") == "P02_LENDER" and e.get("stage") == "T11"]
        if len(events) != 1 or events[0].get("kind") != "review":
            raise ResponderBridgeError("The expected T11 review event is unavailable")
        event = events[0]
        if not event.get("outputs") or not isinstance(event["outputs"], list):
            raise ResponderBridgeError("The Responder public output declaration is invalid")
        for relative in event["outputs"]:
            _inside(Path("/public"), relative)
        return event

    def _caller(self, system, payload, schema, audit_dir, name):
        """One official structured business call, cached by request and config."""
        import jsonschema

        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ResponderBridgeError("Invalid Responder audit call identifier")
        audit_dir = Path(audit_dir).resolve()
        if not audit_dir.is_relative_to(self.private_root):
            raise ResponderBridgeError("Responder audit escaped its private directory")
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit_dir.chmod(0o700)
        request_sha256 = _digest({"system": system, "payload": payload, "schema": schema})
        cached = audit_dir / f"{name}.json"
        if cached.exists():
            saved = json.loads(cached.read_text(encoding="utf-8"))
            if saved.get("request_sha256") != request_sha256 or saved.get("configuration_sha256") != self.configuration_sha256:
                raise ResponderBridgeError("Responder request or configuration changed during recovery")
            jsonschema.validate(saved["result"], schema)
            self._cached_calls += 1
            return saved["result"]
        if self._model is None:
            self._model = self.model_factory(self.config, self.private_root / "model")
        response = self._model.complete([
            {"role": "system", "content": system + "\nReturn only one valid JSON object matching the supplied JSON schema."},
            {"role": "user", "content": json.dumps({"task": payload, "json_schema": schema}, ensure_ascii=False)},
        ], task=f"responder:{name}",response_format={"type":"json_schema","json_schema":{
            "name":name,"strict":True,"schema":schema}})
        try:
            choice = response["choices"][0]
            message = choice["message"]
            if message.get("refusal") or choice.get("finish_reason") in {"content_filter", "length"}:
                raise ValueError("Incomplete or refused structured output")
            content = message.get("content")
            if not isinstance(content, str):
                raise ValueError("Missing JSON content")
            content = content.strip()
            if content.startswith("```json\n") and content.endswith("\n```"):
                content = content[8:-4]
            result = json.loads(content)
            jsonschema.validate(result, schema)
        except (ValueError, KeyError, IndexError, TypeError, jsonschema.ValidationError):
            _private_json(audit_dir / f"{name}-failure.json", {
                "request_sha256": request_sha256, "configuration_sha256": self.configuration_sha256,
                "category": "invalid_structured_response",
            })
            raise ResponderBridgeError("Responder returned invalid structured output") from None
        _private_json(cached, {
            "request_sha256": request_sha256, "configuration_sha256": self.configuration_sha256,
            "configuration": self.configuration, "usage": response.get("usage"), "result": result,
        })
        return result

    def __call__(self, workspace: Path, stage_root: Path) -> Path:
        workspace = Path(workspace).resolve()
        stage_root = Path(stage_root).resolve()
        if stage_root != self.task_root / "steps" / "T11":
            raise ResponderBridgeError("The Responder bridge only accepts the T11 event")
        if self.private_root.is_relative_to(workspace) or workspace.is_relative_to(self.private_root):
            raise ResponderBridgeError("Responder private storage must be outside Candidate workspace")
        if os.environ.get("DELIVERBENCH_RESPONSE_FIXTURES"):
            raise ResponderBridgeError("Synthetic Responder fixture mode is forbidden")
        event = self._event()
        release_root = _inside(stage_root, "workdir/release_materials")
        visible = {p.relative_to(workspace).as_posix(): _sha(p) for p in _files(workspace)}
        released = {p.relative_to(release_root).as_posix(): _sha(p) for p in _files(release_root)}
        fingerprint = _digest({
            "workspace_files": visible, "T11_static_release": released,
            "event_sha256": _digest(event), "configuration_sha256": self.configuration_sha256,
        })
        binding = self.private_root / f"{event['id']}.binding.json"
        if binding.exists():
            prior_binding=json.loads(binding.read_text(encoding="utf-8"))
            if prior_binding.get("fingerprint") != fingerprint:
                # Rebind a protocol correction only while the business event is
                # uncommitted, and only after proving all actual inputs match
                # the old binding under its original model configuration.
                prior_fingerprint=_digest({"workspace_files":visible,"T11_static_release":released,
                    "event_sha256":_digest(event),"configuration_sha256":prior_binding.get("configuration_sha256")})
                prior_session=self.private_root/"sessions"/str(prior_binding.get("fingerprint"))
                committed=any(prior_session.glob("**/publication.json")) or any(prior_session.glob("**/P02_LENDER/receipt.json"))
                old_requests=sorted((self.private_root/"model/model_calls").glob("*.request.json"))
                prior_request=json.loads(old_requests[-1].read_text()) if old_requests else {}
                format_upgrade=bool(prior_request and "response_format" not in prior_request
                    and prior_request.get("model")==self.configuration["model"]
                    and prior_request.get("temperature")==self.configuration["temperature"]
                    and prior_request.get("max_tokens",prior_request.get("max_completion_tokens"))==self.configuration["max_output_tokens"])
                if prior_fingerprint!=prior_binding.get("fingerprint") or committed or not format_upgrade:
                    raise ResponderBridgeError("Committed event inputs or model configuration changed during recovery")
                _private_json(self.private_root/"binding_history"/(prior_binding["fingerprint"]+".json"),{
                    **prior_binding,"rebound_to":fingerprint,"reason":"Uncommitted protocol recovery with byte-identical business inputs"})
                _private_json(binding,{"fingerprint":fingerprint,"configuration_sha256":self.configuration_sha256})
        else:
            _private_json(binding, {"fingerprint": fingerprint, "configuration_sha256": self.configuration_sha256})
        session = self.private_root / "sessions" / fingerprint
        snapshot = session / "workspace"
        snapshot_manifest = session / "snapshot.json"
        expected_snapshot = dict(visible)
        for relative, digest in released.items():
            target = "source/" + relative
            if target in expected_snapshot and expected_snapshot[target] != digest:
                raise ResponderBridgeError("T11 static release conflicts with published input")
            expected_snapshot[target] = digest
        if snapshot.exists():
            actual = {p.relative_to(snapshot).as_posix(): _sha(p) for p in _files(snapshot)}
            saved = json.loads(snapshot_manifest.read_text(encoding="utf-8"))
            if actual != expected_snapshot or saved.get("files") != expected_snapshot:
                raise ResponderBridgeError("Private Responder snapshot changed during recovery")
        else:
            session.mkdir(parents=True, exist_ok=True)
            session.chmod(0o700)
            snapshot.mkdir()
            for path in _files(workspace):
                destination = _inside(snapshot, path.relative_to(workspace).as_posix())
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
            for path in _files(release_root):
                destination = _inside(snapshot, "source/" + path.relative_to(release_root).as_posix())
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    shutil.copyfile(path, destination)
            _private_json(snapshot_manifest, {"files": expected_snapshot, "configuration_sha256": self.configuration_sha256})
        try:
            with _backend_environment(self.configuration):
                root, receipt = self._official_commit()(event, snapshot, self.task_root, session / "trial", caller=self._caller)
            if receipt.get("status") != "committed" or receipt.get("synthetic_fixture") is not False:
                raise ResponderBridgeError("Responder did not commit a real response")
            if receipt.get("id") != event["id"] or receipt.get("stage") != event["stage"]:
                raise ResponderBridgeError("Responder receipt does not match the event")
            files = receipt.get("files", [])
            if not isinstance(files, list) or not files or {r.get("path") for r in files} != set(event["outputs"]):
                raise ResponderBridgeError("Responder receipt omits or invents a public output")
            if len(files) != len(event["outputs"]):
                raise ResponderBridgeError("Responder receipt duplicates public outputs")
            public = Path(root) / "public"
            for row in files:
                original = _inside(public, row["path"])
                if not original.is_file() or _sha(original) != row["sha256"]:
                    raise ResponderBridgeError("Responder public bytes do not match the receipt")
            public_only = session / "public_only"
            if public_only.exists():
                actual = {p.relative_to(public_only).as_posix(): _sha(p) for p in _files(public_only)}
                expected = {r["path"]: r["sha256"] for r in files}
                if actual != expected:
                    raise ResponderBridgeError("Previously exported public response changed")
            else:
                public_only.mkdir()
                for row in files:
                    destination = _inside(public_only, row["path"])
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(_inside(public, row["path"]), destination)
            _private_json(session / "publication.json", {
                "mode": "local_research_real_responder", "official_evaluation": False,
                "event": event["id"], "files": files, "configuration_sha256": self.configuration_sha256,
            })
            _private_json(self.private_root / "metrics.json", self.metrics())
            return public_only
        except Exception as exc:
            _private_json(session / "failure.json", {"error_type": type(exc).__name__, "status": "not_published"})
            _private_json(self.private_root / "metrics.json", self.metrics())
            raise ResponderBridgeError("T11 Responder failed; no public response was published") from None

    def metrics(self) -> dict[str, Any]:
        usage = self._model.metrics() if self._model is not None else self._prior_metrics or {
            "model": self.configuration["model"], "calls": 0, "successful_calls": 0,
            "failed_calls": 0, "input_tokens": 0, "output_tokens": 0,
            "known_input_tokens": 0, "known_output_tokens": 0,
            "usage_complete": True, "estimated_cost_rmb": 0.0,
            "known_cost_rmb": 0.0, "budget_accounted_rmb": 0.0,
            "unpriced_attempts": 0, "elapsed_ms": 0,
            "pricing_unit": "RMB per 1,000,000 tokens",
        }
        return {**usage, "role": "responder", "official_evaluation": False,
                "cached_structured_calls": self._prior_cached_calls + self._cached_calls}

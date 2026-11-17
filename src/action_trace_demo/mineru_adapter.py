"""MinerU hosted v4 parsing and content-addressed local result reuse.

The API protocol is intentionally separate from the actual returned-content
adapter in evidence_windows. No provider credentials or signed URLs are saved.
"""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import time
import zipfile

import httpx

ADAPTER_VERSION = "m01-hosted-content-list-v1"
DIRECT_EXTENSIONS = {".txt", ".md", ".csv", ".tsv", ".json"}
MINERU_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".jp2", ".webp", ".gif", ".bmp", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".html"}


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@contextmanager
def _lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _safe_error(message: Any) -> str:
    value = re.sub(r"https?://\S+", "[URL]", str(message))
    return re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", value)[:500]


def parse_profile(path: str | Path, config: dict) -> dict:
    suffix = Path(urlparse(str(path)).path).suffix.lower()
    if suffix in DIRECT_EXTENSIONS:
        return {"route": "direct", "extension": suffix, "adapter_version": ADAPTER_VERSION}
    if suffix not in MINERU_EXTENSIONS:
        raise ValueError(f"Unsupported source extension: {suffix}")
    model = config.get("html_model_version", "MinerU-HTML") if suffix == ".html" else config.get("model_version", "vlm")
    result = {"route": "mineru", "extension": suffix, "model_version": model,
              "adapter_version": ADAPTER_VERSION, "api_base": config.get("api_base", "https://mineru.net/api/v4")}
    for key in ("language", "enable_formula", "enable_table", "is_ocr", "page_ranges"):
        if key in config:
            result[key] = config[key]
    return result


def _reserve_task(config: dict, cache_key: str, name: str) -> int:
    path = Path(config["task_budget_file"])
    with _lock(path.with_suffix(".lock")):
        state = json.loads(path.read_text()) if path.exists() else {"new_tasks": 0, "tasks": []}
        if state["new_tasks"] >= config.get("max_new_tasks", 3):
            raise RuntimeError("MinerU new-task budget exhausted")
        slot = state["new_tasks"]
        state["new_tasks"] += 1
        state["tasks"].append({"slot": slot, "cache_key": cache_key, "name": name, "state": "reserved", "reserved_at": time.time()})
        _write(path, state)
        return slot


def _update_task(config: dict, slot: int, **fields) -> None:
    path = Path(config["task_budget_file"])
    with _lock(path.with_suffix(".lock")):
        state = json.loads(path.read_text())
        state["tasks"][slot].update(fields)
        _write(path, state)


def _request(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    try:
        response = client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"MinerU transport failed: {type(exc).__name__}") from None
    if not response.is_success:
        raise RuntimeError(f"MinerU HTTP {response.status_code}")
    return response


def _api(client: httpx.Client, method: str, url: str, key: str, **kwargs) -> dict:
    response = _request(client, method, url, headers={"Authorization": f"Bearer {key}"}, **kwargs)
    data = response.json()
    if data["code"] != 0:
        raise RuntimeError(f"MinerU API {data['code']}: {_safe_error(data.get('msg'))}")
    return data["data"]


def _extract_archive(archive: Path, output: Path) -> None:
    with zipfile.ZipFile(archive) as zipped:
        for item in zipped.infolist():
            target = (output / item.filename).resolve()
            if not target.is_relative_to(output.resolve()):
                raise ValueError("MinerU result contains an unsafe archive member")
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("MinerU result contains a symlink")
        zipped.extractall(output)


def _download(client: httpx.Client, url: str, target: Path, config: dict) -> None:
    host = urlparse(url).hostname
    resolved_ip = config.get("download_resolve", {}).get(host)
    if resolved_ip:
        # An explicit network override retains the URL host for TLS/SNI. It is
        # useful on hosts whose DNS proxy cannot reach this particular CDN.
        completed = subprocess.run(["curl", "--silent", "--show-error", "--fail", "--location",
                                    "--max-time", str(config.get("request_timeout_seconds", 60)),
                                    "--resolve", f"{host}:443:{resolved_ip}", "--output", str(target), "--config", "-"],
                                   input="url = " + json.dumps(url) + "\n", capture_output=True, text=True)
        if completed.returncode:
            raise RuntimeError(f"MinerU download failed: {_safe_error(completed.stderr)}")
    else:
        target.write_bytes(_request(client, "GET", url).content)


def resume_parse(result_dir: str | Path, config: dict) -> dict:
    """Explicitly recover an already-created task; never create a new one."""
    output = Path(result_dir)
    receipt_path = output / "parse_receipt.json"
    with _lock(output / "parse.lock"):
        receipt = json.loads(receipt_path.read_text())
        if receipt["status"] == "completed":
            return receipt
        budget = json.loads(Path(config["task_budget_file"]).read_text())
        task = next(t for t in budget["tasks"] if t.get("batch_id") == receipt["batch_id"])
        keys = json.loads(Path(config["api_keys_file"]).read_text())["api_keys"]
        base = receipt["profile"]["api_base"]
        if urlparse(base).scheme != "https" or urlparse(base).hostname != "mineru.net":
            raise ValueError("MinerU credentials may only be sent to https://mineru.net")
        with httpx.Client(timeout=config.get("request_timeout_seconds", 60)) as client:
            data = _api(client, "GET", base + "/extract-results/batch/" + task["batch_id"], keys[task["key_slot"]])
            item = data["extract_result"][0]
            if item["state"] != "done":
                raise RuntimeError(f"Existing MinerU task is {item['state']}; no new task submitted")
            archive = output / "result.zip"
            _download(client, item["full_zip_url"], archive, config)
            _extract_archive(archive, output / "result")
        receipt.update(status="completed", recovered_existing_task=True)
        receipt.pop("error", None)
        receipt["usage"].update(result_bytes=archive.stat().st_size)
        _write(receipt_path, receipt)
        _update_task(config, task["slot"], state="completed", recovered_existing_task=True)
        return receipt


def parse_source(source_version: dict, parse_config: dict) -> dict:
    """Parse one registered local version; failures remain visible and are not retried.

    Required configuration: cache_dir; MinerU additionally requires api_keys_file
    and task_budget_file. A completed cache is reusable without API credentials.
    An interrupted task is reported rather than silently submitted a second time.
    """
    source_path = Path(source_version["original_location"])
    profile = parse_profile(source_path, parse_config)
    content_hash = sha256(source_path.read_bytes()).hexdigest()
    if source_version.get("content_hash", content_hash) != content_hash:
        raise ValueError("Source bytes changed after arrival registration")
    cache_key = sha256((content_hash + json.dumps(profile, sort_keys=True)).encode()).hexdigest()
    output = Path(parse_config["cache_dir"]) / cache_key
    output.mkdir(parents=True, exist_ok=True)
    receipt_path = output / "parse_receipt.json"
    with _lock(output / "parse.lock"):
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if receipt["status"] != "completed":
                raise RuntimeError(f"Previous parse is {receipt['status']}; see {receipt_path}")
            return {**receipt, "source_version_id": source_version["source_version_id"], "cache_hit": True,
                    "usage": {"new_tasks": 0, "cache_hits": 1, "elapsed_seconds": 0}}
        start = time.monotonic()
        original = output / ("original" + source_path.suffix.lower())
        shutil.copyfile(source_path, original)
        receipt = {"status": "parsing", "source_version_id": source_version["source_version_id"],
                   "content_hash": content_hash, "profile": profile, "cache_key": cache_key,
                   "cache_hit": False, "result_dir": str(output), "original_location": str(original),
                   "parse_result_location": str(output), "usage": {"new_tasks": 0, "cache_hits": 0}}
        _write(receipt_path, receipt)
        slot = None
        try:
            if profile["route"] == "mineru":
                base = profile["api_base"].rstrip("/")
                if urlparse(base).scheme != "https" or urlparse(base).hostname != "mineru.net":
                    raise ValueError("MinerU credentials may only be sent to https://mineru.net")
                keys = json.loads(Path(parse_config["api_keys_file"]).read_text())["api_keys"]
                if not keys:
                    raise ValueError("MinerU API key pool is empty")
                slot = _reserve_task(parse_config, cache_key, source_path.name)
                receipt["usage"]["new_tasks"] = 1
                key = keys[slot % len(keys)]
                payload = {k: v for k, v in profile.items() if k in {"model_version", "language", "enable_formula", "enable_table"}}
                file_data = {"name": source_path.name, "data_id": cache_key}
                for field in ("is_ocr", "page_ranges"):
                    if field in profile:
                        file_data[field] = profile[field]
                payload["files"] = [file_data]
                with httpx.Client(timeout=parse_config.get("request_timeout_seconds", 60), follow_redirects=False) as client:
                    data = _api(client, "POST", base + "/file-urls/batch", key, json=payload)
                    receipt["batch_id"] = data["batch_id"]
                    _write(receipt_path, receipt)
                    _update_task(parse_config, slot, state="created", batch_id=data["batch_id"], key_slot=slot % len(keys))
                    with original.open("rb") as stream:
                        _request(client, "PUT", data["file_urls"][0], content=stream)
                    deadline = time.monotonic() + parse_config.get("timeout_seconds", 600)
                    polls = 0
                    while True:
                        task = _api(client, "GET", base + "/extract-results/batch/" + data["batch_id"], key)
                        polls += 1
                        item = task["extract_result"][0]
                        state = item["state"]
                        if state == "done":
                            break
                        if state == "failed":
                            raise RuntimeError(f"MinerU parse failed: {_safe_error(item.get('err_msg'))}")
                        if time.monotonic() >= deadline:
                            raise TimeoutError("MinerU parsing deadline exceeded")
                        time.sleep(parse_config.get("poll_interval", 3))
                    archive = output / "result.zip"
                    _download(client, item["full_zip_url"], archive, parse_config)
                    _extract_archive(archive, output / "result")
                    receipt["usage"].update(polls=polls, result_bytes=archive.stat().st_size)
                    receipt["remote_state"] = {k: item[k] for k in ("state", "file_name", "data_id", "extract_progress") if k in item}
            receipt["status"] = "completed"
            receipt["usage"]["elapsed_seconds"] = round(time.monotonic() - start, 3)
            if slot is not None:
                _update_task(parse_config, slot, state="completed", elapsed_seconds=receipt["usage"]["elapsed_seconds"])
            _write(receipt_path, receipt)
            return receipt
        except Exception as exc:
            receipt.update(status="failed", error=_safe_error(exc))
            receipt["usage"]["elapsed_seconds"] = round(time.monotonic() - start, 3)
            _write(receipt_path, receipt)
            if slot is not None:
                _update_task(parse_config, slot, state="failed", error=_safe_error(exc))
            raise

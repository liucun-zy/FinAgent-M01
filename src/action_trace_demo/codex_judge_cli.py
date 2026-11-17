"""Invoke the actual OpenAI Codex CLI against the configured Efunds Responses endpoint."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import tempfile
import time

from .evaluation import _dump, _hash
from .model import ModelClient

CLI = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
VENV = Path(__file__).resolve().parents[2] / ".venv"
RUNTIME = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies"
EFFORT = "high"
TIMEOUT = 1200


def cli_identity():
    version = subprocess.check_output([str(CLI), "--version"], text=True).strip()
    if not version.startswith("codex-cli "):
        raise ValueError("The configured binary is not the OpenAI Codex CLI")
    return {"binary": str(CLI), "version": version, "sha256": _hash(CLI)}


def _table(mapping):
    return "{" + ",".join(json.dumps(k) + "=" + json.dumps(v) for k,v in mapping.items()) + "}"


def launch(config, work, schema, prompt, audit):
    import batched_judge as official_batch
    model = config["model"]
    if model != "gpt-6-luna":
        raise ValueError("Expected gpt-6-luna")
    key = ModelClient._load_api_key(config, required=True)
    command=[str(CLI),"-a","never","-m",model]
    permission = {":minimal":"read", str(work):"read",str(work/"scratch"):"write",
                  str(VENV):"read", str(RUNTIME):"read","/opt/homebrew":"read",
                  "/Library/Frameworks/Python.framework":"read"}
    cfg = {"model_reasoning_effort": json.dumps(EFFORT), "model_provider": '"efunds_judge"',
           "model_providers.efunds_judge.name": '"Efunds"',
           "model_providers.efunds_judge.wire_api": '"responses"',
           "model_providers.efunds_judge.base_url": json.dumps(config["base_url"]),
           "model_providers.efunds_judge.env_key": '"EFUNDS_JUDGE_KEY"',
           "model_providers.efunds_judge.http_headers": _table(config["extra_headers"]),
           "model_providers.efunds_judge.supports_websockets": "false",
           "model_providers.efunds_judge.request_max_retries":"0",
           "model_providers.efunds_judge.stream_max_retries":"0",
           "model_providers.efunds_judge.stream_idle_timeout_ms": str(TIMEOUT*1000),
           "default_permissions": '"deliverjudge"',
           "permissions.deliverjudge.filesystem": _table(permission),
           "permissions.deliverjudge.network.enabled":"false",
           "shell_environment_policy.inherit":'"none"',
           "shell_environment_policy.set": _table({"PATH":":".join([str(VENV/"bin"),"/opt/homebrew/bin",str(RUNTIME/"bin/override"),"/usr/bin","/bin","/usr/sbin","/sbin"]), "TMPDIR":str(work/"scratch"), "PYTHONDONTWRITEBYTECODE":"1"}),
           "disable_response_storage":"true","web_search":'"disabled"'}
    for k,v in cfg.items():
        command+=["-c",k+"="+v]
    for feature in ("plugins","apps","memories","multi_agent","hooks"):
        command+=["--disable",feature]
    result=work/"verdict.json"
    command+=["exec","-C",str(work),"--skip-git-repo-check","--ephemeral","--ignore-user-config","--ignore-rules","--json","--output-schema",str(schema),"--output-last-message",str(result),"-"]
    env={k:v for k,v in os.environ.items() if k in ("PATH","LANG","LC_ALL","HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","NO_PROXY","SSL_CERT_FILE","SSL_CERT_DIR")}
    env["EFUNDS_JUDGE_KEY"]=key
    # Ephemeral disables persistence of threads; the CLI still accesses config/auth
    # and a state DB in its home. Isolate only this child per native DeliverBench.
    env["CODEX_HOME"]=str(work/"cli_state")
    env["TMPDIR"]=str(work/"scratch")
    for name in ("cli_state","scratch"):
        (work/name).mkdir(exist_ok=True)
    audit.mkdir(parents=True,exist_ok=False)
    _dump(audit/"request.json",{"model":model,"model_reasoning_effort":EFFORT,
        "base_url":config["base_url"],"wire_api":"responses","header_names":list(config["extra_headers"]),
        "timeout_seconds":TIMEOUT,"cli":str(CLI),"schema_sha256":_hash(schema),
        "cli_flags":["--ephemeral","--ignore-user-config","--ignore-rules","--json","--output-schema"],
        "disabled_features":["plugins","apps","memories","multi_agent","hooks"],
        "permission_profile":"deliverjudge","source_files_read_only":True,"shell_network":False,
        "provider_sampling":"Codex Responses defaults; temperature not overridden",
        "cli_user_state":"fresh child-only temp directory","candidate_workspace_excluded":True})
    started=time.monotonic()
    p=subprocess.Popen(command,cwd=work,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                       text=True,start_new_session=True)
    timed_out=False
    try:
        stdout,stderr=p.communicate(prompt,timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        timed_out=True
        os.killpg(p.pid,signal.SIGTERM)
        try: stdout,stderr=p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid,signal.SIGKILL)
            stdout,stderr=p.communicate()
    stdout=stdout.replace(key,"[REDACTED]"); stderr=stderr.replace(key,"[REDACTED]")
    (audit/"events.jsonl").write_text(stdout)
    (audit/"stderr.log").write_text(stderr)
    terminal,usage=official_batch.usage_from_trace(stdout)
    good=p.returncode==0 and not timed_out and terminal and result.is_file()
    meta={"ok":good,"exit_code":p.returncode,"timeout":timed_out,"success_terminal":terminal,
          "seconds":round(time.monotonic()-started,3),"usage":usage}
    _dump(audit/"session.json",meta)
    payload=None
    if result.is_file():
        contents=result.read_text().replace(key,"[REDACTED]")
        (audit/"raw_response.json").write_text(contents)
        if good:
            try: payload=json.loads(contents)
            except ValueError: pass
    return payload, meta


def probe(config, case, destination):
    from .evaluation import _official
    _official(case)
    if destination.exists():
        raise FileExistsError(destination)
    destination.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="deliverbench-cli-probe-") as tmp:
        work=Path(tmp).resolve()
        nonce=secrets.token_hex(12)
        (work/"receipt.txt").write_text(nonce)
        schema={"type":"object","additionalProperties":False,"required":["receipt"],"properties":{"receipt":{"type":"string"}}}
        _dump(work/"schema.json",schema)
        result,meta=launch(config, work, work/"schema.json",
            "Read receipt.txt in the current directory using the shell. Return its exact contents in the requested JSON.",
            destination/"cli")
        ok=meta["ok"] and result=={"receipt":nonce}
    summary={"verified":ok,"native_file_read":ok,"cli":cli_identity(),"model":config["model"],"base_url":config["base_url"],"high_requested":True,"usage":meta["usage"]}
    _dump(destination/"probe.json",summary)
    return summary

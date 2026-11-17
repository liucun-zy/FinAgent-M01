"""Read-only summaries of one selected local evaluation sequence.

Reports never invoke Candidate, Judge, or Responder. Missing judgments and
unpriced model attempts stay unknown; diagnostic sequences are not main scores.
Only allowlisted metadata is exported, never model requests, source text, or
credential-bearing configuration files.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any


UNITS = [f"T{stage:02d}" for stage in range(1, 14)]


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _read(path: Path, issues: list[str]) -> dict:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("expected object")
        return value
    except (OSError, UnicodeError, ValueError):
        issues.append(f"元数据无法解析：{path}")
        return {}


def _rows(path: Path, issues: list[str]) -> tuple[list[dict], bool]:
    if not path.is_file():
        return [], False
    rows, complete = [], True
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("expected object")
                rows.append(row)
            except ValueError:
                complete = False
        if not complete:
            issues.append(f"日志含未完成或无效记录，费用总额保持未知：{path}")
    except (OSError, UnicodeError):
        issues.append(f"日志不可读：{path}")
        complete = False
    return rows, complete


def _pick(value: dict, keys: tuple[str, ...]) -> dict:
    return {key: value[key] for key in keys if key in value}


def _audit_metrics(path: Path, issues: list[str], *, candidate: bool = False) -> dict | None:
    if not path.is_file():
        return None
    rows, complete = _rows(path, issues)
    excluded = 0
    if candidate:
        keep = [row for row in rows if row.get("task") in {"opening", "candidate"}
                or re.fullmatch(r"T\d{2}", str(row.get("task", "")))]
        excluded = len(rows) - len(keep)
        rows = keep
    def known(key):
        return sum(row[key] for row in rows if _number(row.get(key)))
    def total(key):
        return known(key) if complete and all(_number(row.get(key)) for row in rows) else None
    return {
        "models": sorted({row["model"] for row in rows if isinstance(row.get("model"), str)}),
        "calls": len(rows) if complete else None,
        "known_calls": len(rows),
        "successful_calls": sum(row.get("status") == "succeeded" for row in rows) if complete else None,
        "failed_calls": sum(row.get("status") == "failed" for row in rows) if complete else None,
        "input_tokens": total("input_tokens"), "output_tokens": total("output_tokens"),
        "known_input_tokens": known("input_tokens"), "known_output_tokens": known("output_tokens"),
        "estimated_cost_rmb": total("estimated_cost_rmb"), "known_cost_rmb": known("estimated_cost_rmb"),
        "budget_accounted_rmb": total("budget_accounted_cost_rmb"),
        "unpriced_attempts": sum(not _number(row.get("estimated_cost_rmb")) for row in rows) if complete else None,
        "elapsed_ms": total("elapsed_ms"), "known_elapsed_ms": known("elapsed_ms"),
        "excluded_non_candidate_calls": excluded,
        "sources": [str(path.resolve())], "audit_complete": complete,
    }


def _stored_metrics(value: dict, source: Path) -> dict | None:
    if not value or not _number(value.get("calls")):
        return None
    result = _pick(value, ("calls", "successful_calls", "failed_calls", "input_tokens", "output_tokens",
                           "known_input_tokens", "known_output_tokens", "estimated_cost_rmb", "known_cost_rmb",
                           "budget_accounted_rmb", "unpriced_attempts", "elapsed_ms"))
    result.update(models=[value["model"]] if isinstance(value.get("model"), str) else [],
                  known_calls=value["calls"], known_elapsed_ms=value.get("elapsed_ms"),
                  sources=[str(source.resolve())], audit_complete=True)
    # Incomplete transport usage must never be represented as free attempts.
    if value.get("usage_complete") is False:
        result.update(input_tokens=None, output_tokens=None, estimated_cost_rmb=None)
    return result


def _combine(groups: list[dict | None]) -> dict:
    known_groups = [group for group in groups if group is not None]
    complete = bool(groups) and len(known_groups) == len(groups)
    def total(key):
        return sum(group[key] for group in known_groups) if complete and all(_number(group.get(key)) for group in known_groups) else None
    def known(key):
        values = [group[key] for group in known_groups if _number(group.get(key))]
        return sum(values) if values else None
    result = {key: total(key) for key in ("calls", "successful_calls", "failed_calls", "input_tokens",
              "output_tokens", "estimated_cost_rmb", "budget_accounted_rmb", "unpriced_attempts", "elapsed_ms")}
    result.update({key: known(key) for key in ("known_calls", "known_input_tokens", "known_output_tokens",
                                              "known_cost_rmb", "known_elapsed_ms")})
    result.update(models=sorted({model for group in known_groups for model in group.get("models", [])}),
                  sources=sorted({source for group in known_groups for source in group.get("sources", [])}),
                  missing_components=len(groups) - len(known_groups),
                  usage_complete=result["input_tokens"] is not None and result["output_tokens"] is not None,
                  cost_complete=result["estimated_cost_rmb"] is not None,
                  excluded_non_candidate_calls=known("excluded_non_candidate_calls"))
    return result


def _short(value: Any, limit: int = 150) -> str:
    return " ".join(str(value or "").split())[:limit]


def _unmet(unit_dir: Path, issues: list[str]) -> list[dict]:
    # Titles only: do not reproduce criterion bodies or raw source quotations.
    rubric = _read(unit_dir / "rubric.json", issues)
    by_id = {row.get("id"): row for row in rubric.get("rubrics", []) if isinstance(row, dict)}
    path = unit_dir / "verdicts.json"
    if not path.is_file():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        issues.append(f"判定摘要不可读：{path}")
        return []
    if not isinstance(rows, list):
        return []
    result = []
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "graded":
            continue
        if row.get("finding") not in {"criterion_not_satisfied", "violation_present"}:
            continue
        rid = row.get("rubric_id")
        criterion = by_id.get(rid, {})
        result.append({"id": rid, "title": _short(criterion.get("title") or rid), "finding": row["finding"]})
        if len(result) == 5:
            break
    return result


def _unit(eval_dir: Path, unit: str, issues: list[str]) -> tuple[dict, dict | None]:
    directory = eval_dir / unit
    path = directory / "evaluation_result.json"
    data = _read(path, issues)
    status = data.get("status", "missing")
    if data and data.get("evaluation_unit") != unit:
        issues.append(f"评测单元标识不匹配：{path}")
        status = "invalid_unit_identity"
    value = data.get("local_score")
    score = value if status == "graded" and _number(value) else None
    if status == "graded" and score is None:
        status = "invalid_numeric_score"
    protocol_path = directory / "adapter_protocol.json"
    protocol = _read(protocol_path, issues)
    output = {"unit": unit, "status": status, "score": score,
              "evaluation_result_path": str(path.resolve()),
              "adapter_protocol_path": str(protocol_path.resolve()) if protocol else None,
              "protocol": _pick(protocol, ("adapter_version", "protocol_version", "adapter_sha256", "evidence_access",
                                            "judge_transport", "row_validation", "score_arithmetic")),
              **_pick(data, ("rubric_count", "verdict_count", "error_type")),
              "hurdles": data.get("hurdles", []), "score_raw": data.get("score_raw"),
              "missing_verdict_count": len(data.get("missing_verdicts", [])),
              "unmet_criteria_sample": _unmet(directory, issues)}
    metrics = _audit_metrics(directory / "model_calls.jsonl", issues)
    if metrics is None:
        metrics = _stored_metrics(data.get("judge_metrics", {}), path)
    # A missing unit directory means this unit has not started, not a free
    # completed evaluation. It contributes no *observed* Judge attempts yet.
    return output, metrics


def _delivery_counts(run_dir: Path, issues: list[str]) -> dict:
    units, artifact_paths, versions = [], set(), 0
    final_paths: list[str] = []
    for unit in UNITS:
        path = run_dir / "freezes/workspace" / unit / "freeze.json"
        data = _read(path, issues)
        if not data:
            continue
        files = data.get("evidence_files", {})
        files = files if isinstance(files, dict) else {}
        checkpoints = data.get("checkpoint_files", {})
        checkpoints = checkpoints if isinstance(checkpoints, dict) else {}
        artifact_paths.update(files)
        versions += len(files)
        final_paths = sorted(name for name in files if name.startswith("deliverables/final/"))
        units.append({"unit": unit, "submission_complete": data.get("submission_complete"),
                      "checkpoint_file_count": len(checkpoints), "frozen_artifact_file_count": len(files),
                      "final_file_count": len(final_paths), "freeze_path": str(path.resolve()),
                      "artifact_directory": str((path.parent / "evidence").resolve())})
    return {"frozen_stage_count": len(units), "unique_frozen_artifact_path_count": len(artifact_paths),
            "frozen_artifact_version_count": versions, "latest_frozen_final_file_count": len(final_paths) if units else None,
            "latest_frozen_final_paths": final_paths, "stages": units}


def _trace_counts(run_dir: Path, issues: list[str]) -> dict:
    path = run_dir / "trace_events.jsonl"
    rows, complete = _rows(path, issues)
    claims, actions, comparisons, reasons = set(), {}, set(), {}
    for row in rows:
        payload = row.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if row.get("kind") == "claim_created" and payload.get("claim_id"):
            claims.add(payload["claim_id"])
        elif row.get("kind") in {"action_proposed", "action_decided"} and payload.get("action_id"):
            actions[payload["action_id"]] = payload.get("status", "unknown")
            if row.get("kind") == "action_decided":
                reasons[payload["action_id"]] = payload.get("reason", "unspecified")
        elif row.get("kind") == "comparison_recorded" and payload.get("comparison_id"):
            comparisons.add(payload["comparison_id"])
    ledger = _read(run_dir / "ledger.json", issues)
    artifact_path = run_dir / "artifact_events.jsonl"
    artifact_rows, artifact_complete = _rows(artifact_path, issues)
    return {"claim_version_count": len(claims) if complete else None,
            "ledger_export_claim_count": len(ledger["claims"]) if isinstance(ledger.get("claims"), list) else None,
            "ledger_export_stage": ledger.get("stage"), "ledger_export_may_lag_live_run": True,
            "claim_action_count": len(actions) if complete else None,
            "claim_action_status_counts": dict(Counter(actions.values())) if complete else None,
            "rejected_or_held_reason_counts": dict(Counter(reasons.get(a, "unspecified") for a, status in actions.items()
                                                          if status in {"rejected", "held"})) if complete else None,
            "comparison_count": len(comparisons) if complete else None,
            "artifact_action_count": len({row.get("id") for row in artifact_rows}) if artifact_complete else None,
            "semantic_correctness_verified": False, "trace_path": str(path.resolve())}


def _tool_counts(run_dir: Path, eval_dir: Path, issues: list[str]) -> dict:
    rows, complete = _rows(run_dir / "agent_trajectory.jsonl", issues)
    requests = [row for row in rows if row.get("tool") and str(row.get("task", "")).startswith("T")]
    judge_attempts = 0
    for unit in UNITS + ["FINAL"]:
        for path in sorted((eval_dir / unit / "tool_audits").glob("*.json")):
            audit = _read(path, issues)
            count = audit.get("tool_calls")
            if isinstance(count, int) and not isinstance(count, bool):
                judge_attempts += count
    return {"candidate_tool_requests": len(requests) if complete else None,
            "candidate_requests_by_tool": dict(Counter(row["tool"] for row in requests)),
            "candidate_tool_error_results": sum(bool((row.get("result") or {}).get("error")) for row in requests),
            "judge_observed_budgeted_tool_attempts": judge_attempts,
            "scope": "Candidate stage tool requests, including rejected/error results; Judge attempts recorded within evidence budgets; excludes queries and internal file extraction"}


def _provenance(run_dir: Path, eval_dir: Path, issues: list[str]) -> dict:
    path = run_dir / "run_manifest.json"
    manifest = _read(path, issues)
    sequence_path = eval_dir / "sequence_manifest.json"
    sequence = _read(sequence_path, issues)
    binding = []
    if sequence.get("run_dir") and Path(sequence["run_dir"]).resolve() != run_dir:
        binding.append("评测绑定到不同Candidate运行")
    if path.is_file() and sequence.get("run_manifest_sha256"):
        if hashlib.sha256(path.read_bytes()).hexdigest() != sequence["run_manifest_sha256"]:
            binding.append("Candidate manifest哈希与评测绑定不一致")
    recoveries = []
    for audit_path in sorted((run_dir / "recoveries").glob("*/recovery_manifest.json")):
        audit = _read(audit_path, issues)
        recoveries.append({**_pick(audit, ("kind", "task", "call_id", "current_stage", "completed_turns",
                                          "candidate_outputs_regenerated", "policy_change", "raw_result_hashes")),
                           "audit_path": str(audit_path.resolve())})
    environment = _read(run_dir / "environment_notes.json", issues)
    return {"run_manifest_path": str(path.resolve()), "sequence_manifest_path": str(sequence_path.resolve()),
            "candidate_configuration": _pick(manifest, ("schema_version", "mode", "model", "requested_through_stage",
                 "input_rmb_per_million", "output_rmb_per_million", "tool_capability", "context_policy", "context_limit_chars")),
            "evaluation_binding": _pick(sequence, ("run_manifest_sha256", "judge_configuration_sha256", "adapter_sha256",
                                                     "adapter_dependency_sha256", "adapter_version", "protocol_version", "evaluation_purpose")),
            "binding_errors": binding, "technical_recoveries": recoveries,
            "uninterrupted_original_configuration": not bool(recoveries or environment.get("technical_changes")),
            "environment_notes_path": str((run_dir / "environment_notes.json").resolve()) if environment else None,
            "environment": _pick(environment, ("mode", "platform", "docker_available", "python", "recorded_at")),
            "technical_changes": [_pick(change, ("stage", "kind", "description"))
                                  for change in environment.get("technical_changes", []) if isinstance(change, dict)]}


def _format(value: Any, digits: int = 4) -> str:
    return f"{value:.{digits}f}" if _number(value) else "未知／未完成"


def _score(value: Any) -> str:
    return f"{value * 100:.1f}/100" if _number(value) else "未知／未完成"


def _link(label: str, path: str | Path) -> str:
    return f"[{label}](<{path}>)"


def _markdown(report: dict) -> str:
    rows = report["process"]["units"]
    process = report["process"]
    scope = "主评测" if report["score_scope"] == "main" else "仅诊断，不作为主评测"
    lines = ["# 金融案例本地评测进展", "", f"生成时间：{report['generated_at_utc']}。{scope}；本报告读取一个指定评测目录，未重新运行模型或评分。", "",
             f"Candidate状态：`{report['candidate_status'].get('state', 'unknown')}`；冻结交付 {report['deliveries']['frozen_stage_count']}/13 阶段。",
             f"过程有效数值分 {process['valid_numeric_count']}/13；过程算术均分：**{_score(process['mean'])}**。FINAL独立验收：**{_score(report['FINAL']['score'])}**（`{report['FINAL']['status']}`）。",
             "", "以下分数来自本地独立Judge和官方评分算术，未运行官方Harbor/Docker完整评测。过程算术均分仅作本地描述，不代表官方总分；FINAL独立报告。展示采用百分制，机器JSON保留0—1原值。缺失、不可评及失败保留null。",
             "T13过程评审按官方默认映射读取 working/checkpoints/T13；FINAL读取 deliverables/final，两份评审分别保留。", "",
             "| 过程阶段 | 状态 | 分数 | 冻结交付文件 | 结果 |", "| --- | --- | ---: | ---: | --- |"]
    deliveries = {row["unit"]: row for row in report["deliveries"]["stages"]}
    for row in rows:
        delivery = deliveries.get(row["unit"], {})
        display_status = row['status'] + (f"; hurdle={len(row['hurdles'])}" if row.get('hurdles') else "")
        lines.append(f"| {row['unit']} | {display_status} | {_score(row['score'])} | {delivery.get('frozen_artifact_file_count', '—')} | {_link('评分记录', row['evaluation_result_path']) if row['status'] != 'missing' else '—'} |")
    final = report["FINAL"]
    lines += ["", f"FINAL：{_link('独立评分记录', final['evaluation_result_path']) if final['status'] != 'missing' else '尚无记录'}。", "",
              "| 模型角色 | 模型 | 模型调用数 | 输入Token | 输出Token | 估算费用（元） | 已知费用小计（元） |", "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for role in ("Candidate", "Responder", "Judge", "Total"):
        metric = report["model_usage"][role]
        display = "总计" if role == "Total" else role
        value = lambda key: str(metric[key]) if metric.get(key) is not None else "未知"
        lines.append(f"| {display} | {', '.join(metric['models']) or '未知'} | {value('calls')} | {value('input_tokens')} | {value('output_tokens')} | {_format(metric['estimated_cost_rmb'], 6)} | {_format(metric['known_cost_rmb'], 6)} |")
    lines += ["", "费用按所配置单价和服务商返回Token估算；已知小计仅覆盖有计量的调用，未知用量不按零计费。总计范围为Candidate、Responder及本次选定目录的Judge，旧v2、smoke及查询诊断成本不混入。调用耗时是各请求耗时之和，不等于端到端墙钟时间。", ""]
    tools = report["tool_usage"]
    lines.append(f"工具记录：Candidate阶段工具请求 {tools['candidate_tool_requests']} 次（含 {tools['candidate_tool_error_results']} 个异常结果），Judge预算内工具尝试已观察到 {tools['judge_observed_budgeted_tool_attempts']} 次。此处不含查询分支与内部文件提取；运行中数值会继续增加。")
    counts = report["trace_counts"]
    lines.append(f"交付统计：冻结文件路径 {report['deliveries']['unique_frozen_artifact_path_count']} 个，含版本的冻结文件 {report['deliveries']['frozen_artifact_version_count']} 份；最新冻结最终交付 {report['deliveries']['latest_frozen_final_file_count']} 个文件。")
    lines.append(f"行动轨迹：claim版本 {counts['claim_version_count']}，claim维护动作 {counts['claim_action_count']}，冲突比较 {counts['comparison_count']}，文件动作 {counts['artifact_action_count']}。数量不代表语义正确性；ledger导出可能落后于正在运行的状态。")
    states = counts.get("claim_action_status_counts") or {}
    lines.append(f"维护决策：接受 {states.get('accepted', 0)}、拒绝 {states.get('rejected', 0)}、暂缓 {states.get('held', 0)}。这些是结构及引文定位检查结果，不能据此推断修复了同等数量的真实业务错误。")
    provenance = report["provenance"]
    lines += ["", f"配置与适配器：{_link('Candidate manifest', provenance['run_manifest_path'])}；{_link('Judge配置与Adapter哈希', provenance['sequence_manifest_path'])}。",
              f"选定评测：{_link(report['evaluation_directory_name'], report['eval_dir'])}；适配器哈希：`{provenance['evaluation_binding'].get('adapter_sha256', '未记录')}`。"]
    protocols = report["adapter_protocols"]
    for protocol in protocols:
        lines.append(f"证据访问协议：`{protocol.get('evidence_access', '未记录')}`；版本：`{protocol.get('adapter_version', protocol.get('protocol_version', '未标注'))}`；哈希：`{protocol.get('adapter_sha256', '未记录')}`。")
    spot_check = Path(report["eval_dir"]) / "reports/judge_spot_check.md"
    if spot_check.is_file():
        lines += ["", f"已做3条判据的只读抽检：{_link('抽检记录', spot_check)}。抽检发现一处理由措辞不精确，未据此重判；少量抽检不能证明全部Judge判决准确。"]
    scheduling = Path(report["eval_dir"]) / "scheduling_transition.json"
    if scheduling.is_file():
        lines.append(f"评分调度从串行切换到并行，已完成判决保留，未开始判决的准备文件另存：{_link('调度切换记录', scheduling)}。")
    if not provenance["uninterrupted_original_configuration"]:
        lines += ["", "本次包含技术续接或运行环境调整，不是一次无中断的原配置运行。已冻结阶段未因此重新生成；具体以审计记录为准。", ""]
        for recovery in provenance["technical_recoveries"]:
            lines.append(f"- {_link('技术续接审计', recovery['audit_path'])}：{recovery.get('task', '未知阶段')}，精确续接调用 `{recovery.get('call_id', '未知')}`，已完成轮数 {recovery.get('completed_turns', '未知')}。")
        if provenance["environment_notes_path"]:
            lines.append(f"- {_link('运行环境变更记录', provenance['environment_notes_path'])}。")
        for change in provenance["technical_changes"]:
            lines.append(f"- {_short(change.get('description') or change.get('kind'), 260)}")
    samples = [(row["unit"], criterion) for row in rows + [final] for criterion in row["unmet_criteria_sample"]]
    if samples:
        lines += ["", "部分未满足／违规判据（仅列标题或ID，详情见各评分记录）：", ""]
        for unit, criterion in samples[:8]:
            lines.append(f"- {unit}：{criterion['title']}（{criterion['finding']}）")
    if report["issues"]:
        lines += ["", "尚待核查：", ""] + [f"- {issue}" for issue in report["issues"]]
    lines += ["", f"完整轨迹：{_link('可视化报告', Path(report['run_dir']) / 'report.html')}；{_link('行动图', Path(report['run_dir']) / 'action_graph.json')}；{_link('机器汇总JSON', report['output_paths']['json'])}。", ""]
    return "\n".join(lines)


def build_evaluation_report(run_dir: Path, eval_dir: Path, output_dir: Path | None = None) -> dict:
    """Regenerate a partial or complete report without rerunning evaluation."""
    run_dir, eval_dir = Path(run_dir).resolve(), Path(eval_dir).resolve()
    output_dir = Path(output_dir).resolve() if output_dir else eval_dir / "reports"
    if eval_dir.is_relative_to(run_dir / "workspace") or output_dir.is_relative_to(run_dir / "workspace"):
        raise ValueError("Evaluation reports must stay outside the Candidate workspace")
    issues: list[str] = []
    status = _read(run_dir / "run_status.json", issues)
    provenance = _provenance(run_dir, eval_dir, issues)
    units, judge_groups, protocols = [], [], []
    for unit in UNITS + ["FINAL"]:
        row, metrics = _unit(eval_dir, unit, issues)
        units.append(row)
        if (eval_dir / unit).exists():
            judge_groups.append(metrics)
        if row["protocol"] and row["protocol"] not in protocols:
            protocols.append(row["protocol"])
    hashes = {p["adapter_sha256"] for p in protocols if p.get("adapter_sha256")}
    bound_hash = provenance["evaluation_binding"].get("adapter_sha256")
    if len(hashes) > 1 or bound_hash and hashes and hashes != {bound_hash}:
        provenance["binding_errors"].append("选定评测中Adapter哈希不一致，禁止汇总主均分")
    issues.extend(provenance["binding_errors"])
    diagnostic = bool(re.search(r"(?:^|[_-])v[12](?:$|[_-])", eval_dir.name, re.IGNORECASE)
                      or "smoke" in eval_dir.name.lower()
                      or provenance["evaluation_binding"].get("evaluation_purpose") == "diagnostic")
    score_scope = "diagnostic" if diagnostic else "main"
    numeric = [row["score"] for row in units[:-1] if _number(row["score"])]
    selected_mean = sum(numeric) / 13 if len(numeric) == 13 else None
    main_eligible = not diagnostic and not provenance["binding_errors"]
    fallback = _read(run_dir / "metrics.json", issues)
    candidate = _audit_metrics(run_dir / "model_calls.jsonl", issues, candidate=True)
    candidate = candidate if candidate is not None else _stored_metrics(fallback, run_dir / "metrics.json")
    responder_dir = run_dir / "private_responder"
    responder = _audit_metrics(responder_dir / "model/model_calls.jsonl", issues)
    if responder is None:
        stored = _read(responder_dir / "metrics.json", issues)
        responder = _stored_metrics(stored, responder_dir / "metrics.json")
    if responder is None:
        responder = _stored_metrics(fallback.get("responder", {}), run_dir / "metrics.json")
    usage = {"Candidate": _combine([candidate]), "Responder": _combine([responder]), "Judge": _combine(judge_groups or [None])}
    usage["Total"] = _combine(list(usage.values()))
    output_paths = {"json": str(output_dir / "evaluation_summary.json"), "markdown": str(output_dir / "evaluation_summary.md")}
    report = {"schema_version": "1.0", "generated_at_utc": datetime.now(timezone.utc).isoformat(),
              "mode": "local_rubric_scoring_report", "official_harbor_run": False,
              "run_dir": str(run_dir), "eval_dir": str(eval_dir), "evaluation_directory_name": eval_dir.name,
              "score_scope": score_scope, "main_score_eligible": main_eligible,
              "candidate_status": _pick(status, ("state", "current_stage", "completed_stages", "recovery_count", "error_type")),
              "process": {"units": units[:-1], "valid_numeric_count": len(numeric),
                          "mean": selected_mean if main_eligible else None, "selected_sequence_mean": selected_mean,
                          "mean_requires": "13 finite numeric graded stage scores; FINAL excluded; main configuration binding valid"},
              "FINAL": units[-1], "deliveries": _delivery_counts(run_dir, issues), "trace_counts": _trace_counts(run_dir, issues),
              "model_usage": usage, "cost_scope": "Candidate + Responder + selected evaluation Judge; excludes diagnostic/query runs",
              "tool_usage": _tool_counts(run_dir, eval_dir, issues),
              "provenance": provenance, "adapter_protocols": protocols, "issues": issues, "output_paths": output_paths}
    output_dir.mkdir(parents=True, exist_ok=True)
    Path(output_paths["json"]).write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    Path(output_paths["markdown"]).write_text(_markdown(report), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize one selected local evaluation without running models")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--eval-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    report = build_evaluation_report(args.run_dir, args.eval_dir, args.output_dir)
    print(json.dumps({"score_scope": report["score_scope"], "process_valid_numeric_count": report["process"]["valid_numeric_count"],
                      "process_mean": report["process"]["mean"], "FINAL": report["FINAL"]["score"],
                      "output_paths": report["output_paths"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Consolidate fixed V4 and separately audited supplemental evidence scores.

No model calls or scoring are performed. Original numeric scores, including
zero, always win. Supplemental scores can fill only unresolved units whose
base file hashes and unchanged original graded-row bytes independently verify.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .eval_report import UNITS, _audit_metrics, _combine, _number, _read, _stored_metrics


PROTOCOL = "v4_supplemental_evidence"
MECHANICAL = "deterministic_local_package_parse"
MECHANICAL_RID = "P02v3-T10-R064"
MECHANICAL_FILES = ("IC_prereading_pack_vF.pptx", "invitation letter.docx", "weekly_status_note_2026-07-17.docx")


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _score(result: dict, unit: str) -> int | float | None:
    value = result.get("local_score")
    return value if result.get("evaluation_unit") == unit and result.get("status") == "graded" and _number(value) else None


def _verify_zero_api(directory: Path, result: dict) -> None:
    metrics = result.get("judge_metrics")
    if (result.get("judge_backend") != MECHANICAL or type(result.get("model_calls")) is not int or result["model_calls"] != 0
            or not isinstance(metrics, dict) or metrics.get("backend") != MECHANICAL
            or type(metrics.get("model_calls")) is not int or metrics["model_calls"] != 0
            or not _number(metrics.get("estimated_cost_rmb")) or metrics["estimated_cost_rmb"] != 0):
        raise ValueError("机械补证必须明确且一致声明0 API调用／0费用")
    log = directory / "model_calls.jsonl"
    if (log.exists() and log.read_text(encoding="utf-8").strip()) or any((directory / "model_calls").glob("*")):
        raise ValueError("机械补证0 API声明与调用审计矛盾")


def _verify_mechanical(base_dir: Path, directory: Path, result: dict, resolved: dict, unit: str) -> dict:
    _verify_zero_api(directory, result)
    if unit != "T10" or result.get("eligible_ids") != [MECHANICAL_RID]:
        raise ValueError("机械补证仅支持明确的T10-R064")
    path = directory / "mechanical_observations.json"
    observations = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(observations, dict) or observations.get("backend") != MECHANICAL or observations.get("criterion") != MECHANICAL_RID:
        raise ValueError("机械观察的backend／判据绑定不匹配")
    rows = observations.get("observations")
    if not isinstance(rows, list) or len(rows) != 3 or any(not isinstance(row, dict) for row in rows) or [row.get("artifact") for row in rows] != list(MECHANICAL_FILES):
        raise ValueError("机械观察必须完整包含三个指定文件")
    manifest = json.loads((base_dir / "evidence/evaluation_manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("机械观察base证据清单不是对象")
    files = {row["path"]: row["sha256"] for row in manifest.get("files", []) if isinstance(row, dict) and "path" in row and "sha256" in row}
    for row in rows:
        name = row["artifact"]
        library = "python-pptx" if name.endswith(".pptx") else "python-docx"
        if row.get("library") != library or not isinstance(row.get("library_version"), str) or not row["library_version"]:
            raise ValueError("机械观察缺少指定库及版本")
        if any(type(row.get(key)) is not bool for key in ("exists", "parse_succeeded", "criterion_violation")):
            raise ValueError("机械观察存在／解析状态不是显式布尔值")
        original = base_dir / "evidence/submission" / name
        if row["exists"]:
            if row.get("original_hash_unchanged") is not True or row.get("original_sha256") != files.get("submission/" + name) or _hash(original) != row.get("original_sha256"):
                raise ValueError("机械观察原文件hash不匹配")
            if row["parse_succeeded"]:
                keys = ("slide_count",) if library == "python-pptx" else ("paragraph_count", "table_count")
                if any(type(row.get(key)) is not int or row[key] < 0 for key in keys):
                    raise ValueError("机械观察页／段落／表格计数无效")
                violation = row[keys[0]] == 0
            else:
                if not isinstance(row.get("parse_error_type"), str) or not row["parse_error_type"]:
                    raise ValueError("解析失败的机械观察缺少错误类别")
                violation = True
        else:
            if original.exists() or "submission/" + name in files or row.get("original_sha256") is not None or row["parse_succeeded"]:
                raise ValueError("机械缺失文件声明与base视图矛盾")
            violation = True
        if row["criterion_violation"] is not violation:
            raise ValueError("机械观察与明确的存在／可解析／非零计数判据矛盾")
    violation = any(row["criterion_violation"] for row in rows)
    verdict = resolved[MECHANICAL_RID]
    if verdict.get("hit") is not violation or verdict.get("finding") != ("violation_present" if violation else "violation_absent"):
        raise ValueError("机械观察与新增判决不一致")
    return {"backend": MECHANICAL, "criterion": MECHANICAL_RID, "model_calls": 0,
            "mechanical_observations_sha256": _hash(path), "observations": rows}


def _verdicts(path: Path) -> tuple[dict[str, dict], dict[str, str]]:
    """Read JSON-array objects and hash their exact object byte substrings."""
    text = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    def skip(offset):
        while offset < len(text) and text[offset].isspace():
            offset += 1
        return offset
    offset = skip(0)
    if text[offset:offset + 1] != "[":
        raise ValueError("verdicts must be an array")
    offset = skip(offset + 1)
    rows, hashes = {}, {}
    if text[offset:offset + 1] == "]":
        if text[skip(offset + 1):]:
            raise ValueError("trailing verdict data")
        return rows, hashes
    while True:
        start = offset
        row, offset = decoder.raw_decode(text, offset)
        if not isinstance(row, dict) or not isinstance(row.get("rubric_id"), str) or not row["rubric_id"]:
            raise ValueError("invalid verdict identity")
        rid = row["rubric_id"]
        if rid in rows:
            raise ValueError("duplicate verdict identity")
        rows[rid] = row
        hashes[rid] = hashlib.sha256(text[start:offset].encode("utf-8")).hexdigest()
        offset = skip(offset)
        if text[offset:offset + 1] == "]":
            if text[skip(offset + 1):]:
                raise ValueError("trailing verdict data")
            return rows, hashes
        if text[offset:offset + 1] != ",":
            raise ValueError("invalid verdict separator")
        offset = skip(offset + 1)


def _validate_supplement(base_dir: Path, supplement_dir: Path, base: dict, supplement: dict, unit: str) -> dict:
    audit = {"valid": False, "errors": [], "verified_hashes": {}, "original_graded_rows_verified": 0}
    errors = audit["errors"]
    try:
        if base.get("protocol_version") != "v4":
            raise ValueError("base不是固定v4评测")
        if base.get("evaluation_unit") != unit:
            raise ValueError("base单元ID与所在目录不匹配")
        if supplement.get("evaluation_unit") != unit:
            raise ValueError("补证单元ID与base不匹配")
        if supplement.get("mode") not in {PROTOCOL, MECHANICAL} or supplement.get("protocol_version") != PROTOCOL:
            raise ValueError("补证协议标识不匹配")
        declared_base = supplement.get("base_evaluation_dir")
        if not isinstance(declared_base, str) or Path(declared_base).resolve() != base_dir.resolve():
            raise ValueError("补证绑定到不同base目录")
        expected_files = {"base_evaluation_result_sha256": base_dir / "evaluation_result.json",
                          "base_verdicts_sha256": base_dir / "verdicts.json",
                          "resolved_verdicts_sha256": supplement_dir / "verdicts.json"}
        for key, path in expected_files.items():
            digest = _hash(path)
            if supplement.get(key) != digest:
                raise ValueError(f"{key}不匹配")
            audit["verified_hashes"][key] = digest
        if supplement.get("base_graded_rows_unchanged") is not True:
            raise ValueError("缺少原graded行保持不变声明")
        original, original_hashes = _verdicts(base_dir / "verdicts.json")
        resolved, resolved_hashes = _verdicts(supplement_dir / "verdicts.json")
        rubric = json.loads((base_dir / "rubric.json").read_text(encoding="utf-8"))
        if not isinstance(rubric, dict):
            raise ValueError("base判据文件不是对象")
        items = rubric.get("rubrics")
        if not isinstance(items, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in items):
            raise ValueError("base判据ID不可核验")
        rubric_ids = [item["id"] for item in items]
        if len(set(rubric_ids)) != len(rubric_ids) or not set(original).issubset(rubric_ids):
            raise ValueError("base判据或verdict ID重复／越界")
        if set(resolved) != set(rubric_ids):
            raise ValueError("补证合并verdict并非原判据ID完整集合")
        fixed = {rid for rid, row in original.items() if row.get("status") == "graded"}
        for rid in fixed:
            if original_hashes[rid] != resolved_hashes[rid]:
                raise ValueError(f"原graded行内容或字节已改变：{rid}")
        audit["original_graded_rows_verified"] = len(fixed)
        # A scored resolved unit requires a full set of explicit judgments.
        if any(row.get("status") != "graded" for row in resolved.values()):
            raise ValueError("补证仍有非graded判决")
        eligible_ids = supplement.get("eligible_ids")
        if not isinstance(eligible_ids, list) or len(set(eligible_ids)) != len(eligible_ids) or set(eligible_ids) != set(rubric_ids) - fixed:
            raise ValueError("补证候选ID不是原ungraded／missing ID全集")
        provenance = json.loads((supplement_dir / "base_provenance.json").read_text(encoding="utf-8"))
        if not isinstance(provenance, dict):
            raise ValueError("补证base审计不是对象")
        if provenance.get("fixed_graded_row_sha256") != {rid: original_hashes[rid] for rid in fixed}:
            raise ValueError("补证逐行hash审计不匹配")
        allowed_files = {"evaluation_result.json", "verdicts.json", "rubric.json", "adapter_protocol.json", "evidence/evaluation_manifest.json"}
        if not isinstance(provenance.get("base_files"), dict) or set(provenance["base_files"]) != allowed_files:
            raise ValueError("补证base文件审计不完整或含未授权路径")
        for name, expected in provenance["base_files"].items():
            # Only this fixed metadata allowlist is read, never arbitrary refs.
            if expected != _hash(base_dir / name):
                raise ValueError(f"补证base文件审计不匹配：{name}")
        if supplement.get("mode") == MECHANICAL:
            audit["mechanical_evidence"] = _verify_mechanical(base_dir, supplement_dir, supplement, resolved, unit)
        audit["valid"] = True
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        errors.append(str(exc))
    return audit


def _zero_usage() -> dict:
    return {"models": [], "sources": [], "audit_complete": True, **{key: 0 for key in (
        "calls", "known_calls", "successful_calls", "failed_calls", "input_tokens", "output_tokens", "known_input_tokens",
        "known_output_tokens", "estimated_cost_rmb", "known_cost_rmb", "budget_accounted_rmb", "unpriced_attempts", "elapsed_ms", "known_elapsed_ms")}}


def _usage(run_dir: Path, base_dir: Path, supplement_dir: Path, issues: list[str]) -> dict:
    stored = _read(run_dir / "metrics.json", issues)
    candidate = _audit_metrics(run_dir / "model_calls.jsonl", issues, candidate=True)
    if candidate is None:
        candidate = _stored_metrics(stored, run_dir / "metrics.json")
    responder = _audit_metrics(run_dir / "private_responder/model/model_calls.jsonl", issues)
    if responder is None:
        responder_path = run_dir / "private_responder/metrics.json"
        responder = _stored_metrics(_read(responder_path, issues), responder_path)
    if responder is None:
        responder = _stored_metrics(stored.get("responder", {}), run_dir / "metrics.json")
    def judges(root, *, kind="all"):
        groups = []
        for unit in UNITS + ["FINAL"]:
            directory = root / unit
            if not directory.is_dir():
                continue
            path = directory / "evaluation_result.json"
            data = _read(path, issues)
            mechanical = data.get("mode") == MECHANICAL
            if kind == "model" and mechanical or kind == "mechanical" and not mechanical:
                continue
            metric = _audit_metrics(directory / "model_calls.jsonl", issues)
            if metric is None:
                metric = _stored_metrics(data.get("judge_metrics", {}), path)
                if mechanical:
                    try:
                        _verify_zero_api(directory, data)
                        metric = _zero_usage()
                    except (OSError, UnicodeError, ValueError) as exc:
                        issues.append(f"{directory.name}：{exc}")
                if metric is None and data.get("status") == "prepared_without_model_calls":
                    metric = _zero_usage()
            groups.append(metric)
        return _combine(groups or [_zero_usage()])
    roles = {"Candidate": _combine([candidate]), "Responder": _combine([responder]),
             "BaseJudge": judges(base_dir), "SupplementalJudge": judges(supplement_dir, kind="model"),
             "MechanicalEvidence": judges(supplement_dir, kind="mechanical")}
    roles["Total"] = _combine(list(roles.values()))
    return roles


def _format(value: Any, digits: int = 2, percent: bool = True) -> str:
    if not _number(value):
        return "未知／未完成"
    return f"{value * 100:.{digits}f}%" if percent else f"{value:.{digits}f}"


def _link(label: str, path: str | Path) -> str:
    return f"[{label}](<{path}>)"


def _markdown(report: dict) -> str:
    process = report["process"]
    lines = ["# 固定v4与补充取证后的本地评测", "",
        "本报告保留固定预算v4初始结果，并单列补齐缺失取证后的结果。只补判原不可评／缺失ID；原graded判决逐行保持，原有效数值分（含0）优先采用。报告生成不重跑Candidate、不修改冻结交付，也不触发模型调用；补证Judge已有调用另计。", "",
        "这是本地独立Judge与官方评分算术结果，并非官方Harbor完整评测。初始分与补证后分使用不同预算，分别存档，不应视为同一次固定预算结果。", "",
        f"固定v4：有效过程分 {process['base_valid_numeric_count']}/13，均分 **{_format(process['base_mean'])}**；补证后：{process['effective_valid_numeric_count']}/13，均分 **{_format(process['effective_mean'])}**。FINAL独立验收，不参与过程均分。", "",
        "| 单元 | 固定v4状态 | 初始分 | 补证状态 | 补证分 | 最终采用来源 | 最终分 |", "| --- | --- | ---: | --- | ---: | --- | ---: |"]
    for row in process["units"] + [report["FINAL"]]:
        source = {"base": "固定v4", "supplemental_evidence": "机械补证" if row.get("supplemental_mode") == MECHANICAL else "补充取证", "unresolved": "尚未解决"}[row["effective_source"]]
        lines.append(f"| {row['unit']} | {row['base_status']} | {_format(row['base_score'])} | {row['supplemental_status']} | {_format(row['supplemental_reported_score'])} | {source} | {_format(row['effective_score'])} |")
    lines += ["", "仅13项均为有效有限数值时才给过程均分；缺失、不可评、失败与绑定不一致保留null，不补零。补证未通过hash／原行校验时，即使报告了数值也不采用。", "",
              "| 费用角色 | 调用数 | 估算费用（元） | 已知费用小计（元） |", "| --- | ---: | ---: | ---: |"]
    for role, label in (("Candidate", "Candidate"), ("Responder", "Responder"), ("BaseJudge", "固定v4 Judge"),
                        ("SupplementalJudge", "补充取证 Judge"), ("MechanicalEvidence", "机械补证（非LLM）"), ("Total", "合计")):
        usage = report["model_usage"][role]
        calls = usage["calls"] if usage["calls"] is not None else "未知"
        lines.append(f"| {label} | {calls} | {_format(usage['estimated_cost_rmb'], 6, False)} | {_format(usage['known_cost_rmb'], 6, False)} |")
    lines += ["", "补证Judge费用为额外增量，未重复计入base Judge。费用覆盖截至生成时已落盘审计的调用，执行中的调用与后续评分会更新。未知用量不按零计费；已知小计不代表完整费用。仅统计所选运行和评测目录，未混入trial01或smoke。", "",
              f"基准评测：{_link('固定v4目录', report['base_eval_dir'])}；补证：{_link('独立补证目录', report['supplemental_dir'])}。",
              f"固定v4 manifest：{_link('配置及adapter绑定', Path(report['base_eval_dir']) / 'sequence_manifest.json')}；补证协议：`{PROTOCOL}`。"]
    adopted = [row for row in process["units"] + [report["FINAL"]] if row["effective_source"] == "supplemental_evidence"]
    if adopted:
        lines += ["", "已采用的补证审计：", ""]
        for row in adopted:
            lines.append(f"- {row['unit']}：{_link('补证记录', row['supplemental_result_path'])}；已核验 {row['integrity']['original_graded_rows_verified']} 条原graded行不变。")
    if report["issues"]:
        lines += ["", "尚待核查：", ""] + [f"- {issue}" for issue in report["issues"]]
    lines += ["", f"机器汇总：{_link('resolved JSON', report['output_paths']['json'])}。生成时间：{report['generated_at_utc']}。", ""]
    return "\n".join(lines)


def build_resolved_report(base_eval_dir: Path, *, run_dir: Path | None = None, supplemental_dir: Path | None = None,
                          output_dir: Path | None = None) -> dict:
    """Write a partial or complete consolidated report, without changing inputs."""
    base_eval_dir = Path(base_eval_dir).resolve()
    issues: list[str] = []
    sequence = _read(base_eval_dir / "sequence_manifest.json", issues)
    if run_dir is None:
        if not isinstance(sequence.get("run_dir"), str):
            raise ValueError("Pass run_dir or provide sequence_manifest.run_dir")
        run_dir = Path(sequence["run_dir"])
    run_dir = Path(run_dir).resolve()
    supplemental_dir = Path(supplemental_dir).resolve() if supplemental_dir else base_eval_dir / "supplemental"
    output_dir = Path(output_dir).resolve() if output_dir else base_eval_dir / "reports"
    if any(path.is_relative_to(run_dir / "workspace") for path in (base_eval_dir, supplemental_dir, output_dir)):
        raise ValueError("Reports and evaluation must remain outside Candidate workspace")
    if supplemental_dir == base_eval_dir:
        raise ValueError("Supplemental directory must differ from base evaluation")
    binding_errors = []
    if sequence.get("run_dir") and Path(sequence["run_dir"]).resolve() != run_dir:
        binding_errors.append("base评测与指定Candidate运行绑定不一致")
    manifest_path = run_dir / "run_manifest.json"
    if sequence.get("run_manifest_sha256") and (not manifest_path.is_file() or _hash(manifest_path) != sequence["run_manifest_sha256"]):
        binding_errors.append("base评测绑定的Candidate manifest哈希不一致")
    rows = []
    for unit in UNITS + ["FINAL"]:
        base_dir, supplement_dir = base_eval_dir / unit, supplemental_dir / unit
        base_path, supplement_path = base_dir / "evaluation_result.json", supplement_dir / "evaluation_result.json"
        base, supplement = _read(base_path, issues), _read(supplement_path, issues)
        base_score, supplement_score = _score(base, unit), _score(supplement, unit)
        row = {"unit": unit, "base_status": base.get("status", "missing"), "base_score": base_score,
               "supplemental_status": supplement.get("status", "missing"), "supplemental_reported_score": supplement_score,
               "supplemental_mode": supplement.get("mode"),
               "effective_source": "base" if base_score is not None else "unresolved", "effective_score": base_score,
               "base_result_path": str(base_path), "supplemental_result_path": str(supplement_path),
               "integrity": {"valid": None, "errors": [], "original_graded_rows_verified": 0}}
        if base_score is not None:
            row["supplemental_adoption"] = "ignored_valid_base" if supplement else "not_needed"
        elif supplement_score is not None:
            row["integrity"] = _validate_supplement(base_dir, supplement_dir, base, supplement, unit)
            if row["integrity"]["valid"] and not binding_errors:
                row.update(effective_source="supplemental_evidence", effective_score=supplement_score, supplemental_adoption="adopted")
            else:
                row["supplemental_adoption"] = "rejected_integrity"
                row["supplemental_status"] = "rejected_binding_or_changed_rows"
                issues.extend(f"{unit}：{error}" for error in row["integrity"]["errors"])
        else:
            row["supplemental_adoption"] = "pending_or_ungradable"
        rows.append(row)
    issues.extend(binding_errors)
    process = rows[:-1]
    base_numbers = [row["base_score"] for row in process if _number(row["base_score"])]
    effective_numbers = [row["effective_score"] for row in process if _number(row["effective_score"])]
    output_paths = {"json": str(output_dir / "evaluation_resolved_summary.json"),
                    "markdown": str(output_dir / "evaluation_resolved_summary.md")}
    report = {"schema_version": "1.0", "generated_at_utc": datetime.now(timezone.utc).isoformat(),
              "mode": "local_v4_supplemental_evidence_consolidation", "official_harbor_run": False,
              "run_dir": str(run_dir), "base_eval_dir": str(base_eval_dir), "supplemental_dir": str(supplemental_dir),
              "process": {"units": process, "base_valid_numeric_count": len(base_numbers),
                          "base_mean": sum(base_numbers) / 13 if len(base_numbers) == 13 and not binding_errors else None,
                          "effective_valid_numeric_count": len(effective_numbers),
                          "effective_mean": sum(effective_numbers) / 13 if len(effective_numbers) == 13 and not binding_errors else None},
              "FINAL": rows[-1], "binding_errors": binding_errors,
              "provenance": {"base_sequence_manifest_sha256": _hash(base_eval_dir / "sequence_manifest.json") if sequence else None,
                             "base_adapter_sha256": sequence.get("adapter_sha256"), "supplemental_protocol": PROTOCOL,
                             "fixed_numeric_scores_are_never_replaced": True,
                             "same_budget_as_initial_v4": False},
              "model_usage": _usage(run_dir, base_eval_dir, supplemental_dir, issues), "issues": issues, "output_paths": output_paths}
    output_dir.mkdir(parents=True, exist_ok=True)
    Path(output_paths["json"]).write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    Path(output_paths["markdown"]).write_text(_markdown(report), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only V4 + supplemental-evidence score report")
    parser.add_argument("--base-eval-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--supplemental-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    report = build_resolved_report(args.base_eval_dir, run_dir=args.run_dir, supplemental_dir=args.supplemental_dir,
                                   output_dir=args.output_dir)
    print(json.dumps({"base_process_mean": report["process"]["base_mean"], "effective_process_mean": report["process"]["effective_mean"],
                      "effective_valid_numeric_count": report["process"]["effective_valid_numeric_count"],
                      "FINAL": report["FINAL"]["effective_score"], "output_paths": report["output_paths"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

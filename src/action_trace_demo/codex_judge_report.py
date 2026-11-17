"""Write the Chinese end-of-run report from hash-bound native Codex verdicts."""
from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from statistics import mean, median
import json

from .evaluation import _hash, _dump
from .eval_parallel import UNITS


def read(path):
    return json.loads(path.read_text())


def write_report(folder, config_path, probe_path):
    folder = folder.resolve()
    summary = read(folder / "summary.json")
    if summary["state"] != "finished" or not (folder / "FINAL/evaluation_result.json").is_file():
        raise ValueError("Run has not reached all evaluation units")
    protocol = read(folder / "protocol.json")
    if protocol["config_sha256"] != _hash(config_path):
        raise ValueError("Price configuration does not match the bound original API configuration")
    config = read(config_path)
    audit = read(probe_path / "endpoint_response.json")
    probe = read(probe_path / "probe.json")
    if not probe["verified"] or audit.get("status") != 200:
        raise ValueError("Missing native CLI capability test or server route confirmation")

    rows, session_records = [], []
    for unit in UNITS:
        directory = folder / unit
        item = read(directory / "evaluation_result.json")
        for key, file in [("verdicts_sha256", "verdicts.json"), ("unit_binding_sha256", "unit_binding.json"),
                          ("score_details_sha256", "score_details.json")]:
            if item[key] != _hash(directory / file):
                raise ValueError(f"Changed {unit}/{file}")
        observed = read(directory / "verdicts.json")
        rubric = read(directory / "rubric.json")["rubrics"]
        if len(set(r["rubric_id"] for r in observed)) != len(observed):
            raise ValueError("Duplicate rubric verdict")
        if any(r["status"] != "graded" or type(r["hit"]) is not bool for r in observed):
            raise ValueError("Non-final verdict counted as graded")
        if item["status"] == "graded" and {r["id"] for r in rubric} != {r["rubric_id"] for r in observed}:
            raise ValueError("Graded unit has missing or unknown verdicts")
        sessions = list(directory.glob("batches/*/attempt-*/session.json"))
        if len(sessions) != item["cli_sessions"]:
            raise ValueError("CLI audit count does not match the unit result")
        for path in sessions:
            request = read(path.with_name("request.json"))
            if (request["model"], request["model_reasoning_effort"], request["timeout_seconds"]) != (
                    protocol["model"], protocol["reasoning_effort"], protocol["timeout_seconds"]):
                raise ValueError("CLI request differs from registered settings")
            session_records.append((path, read(path)))
        score = read(directory / "score_details.json")
        rows.append((unit, len(observed), len(rubric), item["local_score"], item["status"],
                     len(score.get("hurdles", [])), len(sessions)))
    process = [row[3] for row in rows[:-1]]
    process_mean = mean(process) if all(x is not None for x in process) else None
    if process_mean is not None and abs(process_mean - summary["process_mean"]) > 1e-10:
        raise ValueError("Arithmetic mean differs from scheduler totals")

    pct = lambda score: "未形成分数" if score is None else f"{score*100:.2f}"
    sessions = [s for p, s in session_records]
    primaries = sum(p.parent.name == "attempt-1" and p.parent.parent.name.startswith("batch_") for p,s in session_records)
    second = sum(p.parent.name == "attempt-2" for p,s in session_records)
    singleton = sum(p.parent.parent.name.startswith("single_") for p,s in session_records)
    events = list(folder.glob("*/batches/*/attempt-*/request.json"))
    rubric_count = sum(row[2] for row in rows)
    valid_count = sum(row[1] for row in rows)
    tokens = {k:sum(s["usage"].get(k,0) for s in sessions) for k in ("input_tokens","output_tokens","cached_input_tokens","reasoning_output_tokens")}
    p_in, p_out = config["input_rmb_per_million_tokens"], config["output_rmb_per_million_tokens"]
    estimate = (tokens["input_tokens"]*p_in + tokens["output_tokens"]*p_out) / 1_000_000
    probe_sessions = []
    for label, path in [
        ("native_cli_read_schema", probe_path / "cli/session.json"),
        ("native_cli_formula_sandbox", probe_path.parent / "judge_capability_20261113_c02_io/cli/session.json"),
    ]:
        if not path.is_file():
            raise ValueError("Missing capability probe usage record: "+label)
        usage = read(path)["usage"]
        probe_sessions.append({"purpose":label,"source":str(path.resolve()),"usage":usage,
                               "estimated_rmb":(usage["input_tokens"]*p_in + usage["output_tokens"]*p_out)/1e6})
    direct = audit["usage"]
    probe_sessions.append({"purpose":"endpoint_model_and_reasoning","source":str((probe_path/"endpoint_response.json").resolve()),
                           "usage":direct,"estimated_rmb":(direct["input_tokens"]*p_in + direct["output_tokens"]*p_out)/1e6})
    probe_cost = sum(probe["estimated_rmb"] for probe in probe_sessions)
    unreported = ("Earlier connectivity-only HTTP probes have no retained usage. "
                  "This is the cost of recorded probe usage; no total API bill was returned.")
    # Distinguish observed runtime parallelism from the requested worker limit.
    timeline = []
    for path, session in session_records:
        end = path.stat().st_mtime_ns / 1e9
        timeline.extend([(end-session["seconds"], 1), (end, -1)])
    active = maximum = 0
    for _, change in sorted(timeline):
        active += change
        maximum = max(active,maximum)
    metrics = {"mode":summary["mode"], "official_harbor_run":False,
               "complete": all(row[4]=="graded" and row[1]==row[2] for row in rows),
               "process_mean":process_mean,"process_score_100":None if process_mean is None else process_mean*100,
               "process_graded":sum(row[1] for row in rows[:-1]),"process_rules":sum(row[2] for row in rows[:-1]),
               "final_score":rows[-1][3], "final_score_100":None if rows[-1][3] is None else rows[-1][3]*100,
               "final_graded":rows[-1][1], "final_rules":rows[-1][2],"verdict_count":valid_count,"rubric_count":rubric_count,
               "units":[{"unit":u,"graded":n,"rules":total,"score":score,"status":status,"hurdles":h,"cli_sessions":ncli}
                        for u,n,total,score,status,h,ncli in rows],
               "cli_sessions":len(sessions),"requests":len(events),"primary_batches":primaries,
               "second_batch_attempts":second,"singleton_supplements":singleton,
               "timeouts":sum(s["timeout"] for s in sessions),"unsuccessful_sessions":sum(not s["ok"] for s in sessions),
               "interrupted_request_without_session":len(events)-len(sessions),"concurrency_cap":protocol["global_concurrency"],
               "observed_peak_parallel_sessions":maximum,"overlap_precision":"session receipt mtime and monotonic elapsed seconds",
               "usage":tokens, "estimated_rmb":estimate, "input_rmb_per_million_tokens":p_in,"output_rmb_per_million_tokens":p_out,
               "billed_cost":None,"probe_estimated_rmb_recorded_usage":probe_cost,"probe_sessions":probe_sessions,
               "probe_coverage":unreported,"protocol":str((folder/"protocol.json").resolve()),
               "summary":str((folder/"summary.json").resolve()),"endpoint_probe":str((probe_path/"endpoint_response.json").resolve())}
    if maximum>protocol["global_concurrency"]:
        raise ValueError("Observed more overlapping CLI sessions than the bound worker cap")
    _dump(folder/"judge_metrics.json",metrics)
    lines = [
        "# 台账与行动图自审 Agent 的 DeliverBench 金融案例评分",
        "",
        f"本次运行完成 13 个过程阶段及独立最终验收。过程等权均分为 **{pct(process_mean)}/100**，独立最终验收为 **{pct(rows[-1][3])}/100**。共有 **{valid_count}/{rubric_count}** 条规则取得通过官方校验的裁决。金额估算和运行次数仅覆盖评分端。",
        "",
        "| 评测单元 | 分数（满分 100） | 有效裁决／规则 | 触发硬门槛数 | CLI 会话 |",
        "|---|---:|---:|---:|---:|",
    ]
    for u, count, total, score, status, hurdles, n in rows:
        lines.append(f"| [{u}]({u}/score_details.json) | {pct(score)} | {count}/{total} | {hurdles} | {n} |")
    lines.extend([
        "",
        "每个阶段的链接包含逐条原文依据、判定理由、依赖门槛、实际得分与扣分。有效裁决数包含满足与不满足的判定。阶段内分数采用 DeliverBench 官方规则和计分程序，13 个阶段等权平均，FINAL 单独报告。[汇总](summary.json)",
        "",
        "## 评分执行与费用",
        "",
        f"- 原 Efunds 网关上的 Codex CLI 请求模型为 `{protocol['model']}`，推理强度 `high`。同 endpoint 独立探针返回服务版本 `{audit['observed_model']}`、强度 `{audit['observed_reasoning']['effort']}`。评分使用单独的 CLI 子进程和会话目录，并直接向原网关发送 Responses 请求。[实际配置与 CLI 可执行文件哈希](protocol.json) [独立探针]({probe_path.resolve()}/endpoint_response.json)",
        f"- 每批最多 8 条，全局最多 4 批并行，观察到的并发峰值为 {maximum} 批。CLI 会话限时 1,200 秒。初判 {primaries} 批，批内第二次 {second} 次，逐条补判 {singleton} 次。共记录 {len(events)} 次请求、{len(sessions)} 个结束状态，其中 {sum(s['timeout'] for s in sessions)} 次超时。具有完整终态的会话持续时间中位数为 {median(s['seconds'] for s in sessions):.0f} 秒。已获有效判定的条目不重复评分。",
        f"- CLI 终态记录的输入为 {tokens['input_tokens']:,} tokens，其中缓存 {tokens['cached_input_tokens']:,}。输出为 {tokens['output_tokens']:,} tokens，其中推理 {tokens['reasoning_output_tokens']:,}。按输入 ¥{p_in:g}、输出 ¥{p_out:g}／百万 tokens，对全部已记录输入按统一单价估算约 **¥{estimate:.2f}**。缓存和推理子项已经包含在相应总数中。网关未提供账单。",
        "",
        f"能力验证与正式评分分开统计。两次沙箱 CLI 探针及一次保留完整用量的端点验证合计约 ¥{probe_cost:.3f}。早期连通性试探缺少完整用量。[机器可读计数与费用](judge_metrics.json)",
        "",
        "## 结果与原始证据",
        "",
        "- 本机读取冻结的阶段交付、当时可见的原件和历史记录。每阶段的 `unit_binding.json` 绑定冻结证据、任务描述和规则。`evidence/evaluation_manifest.json` 列出交付与原件。每次真实取证命令和完整 CLI 裁决位于该阶段的 `batches/*/attempt-*/events.jsonl`、`raw_response.json`、`validation.json`。每阶段的 `evaluation_result.json` 保存裁决及计分明细的 SHA256。[T01 冻结证据](T01/unit_binding.json) [FINAL 冻结证据](FINAL/unit_binding.json)",
        "- 本次采用 macOS 上的真实 Codex CLI 和项目内的官方算分代码，未运行 Docker/Harbor。旧 v4 的评审器、证据读取方式和评分协议不同，本表提供本次运行的独立结果。",
    ])
    path = folder / "评分报告.md"
    path.write_text("\n".join(lines)+"\n",encoding="utf-8")
    return path


if __name__=="__main__":
    p = ArgumentParser()
    p.add_argument("--eval-dir", type=Path, required=True)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--probe-dir", type=Path, required=True)
    args=p.parse_args()
    print(write_report(args.eval_dir,args.config,args.probe_dir))

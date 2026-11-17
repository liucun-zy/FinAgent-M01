"""Paired scoring from committed post-action state, with single-window crossover checks.

Only the reporting process receives gold rows. It has no model client or retriever.
"""
from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path
from statistics import mean


POLICIES = ("relevance", "decision_value")
_MISSING = object()


def _unique(rows, key):
    result = {}
    for row in rows:
        value = key(row)
        if value in result:
            raise ValueError(f"Duplicate observation: {value}")
        result[value] = row
    return result


def _same_value(left, right):
    if left is _MISSING or right is _MISSING:
        return left is right
    # The strings "1", the Boolean true and the number 1 are distinct state values.
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same_value(v, right[k]) for k, v in left.items())
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same_value(a, b) for a, b in zip(left, right))
    return left == right


def _read_ids(run):
    return [r["window_id"] for r in run["reads"]]


def _compare_outcome(a, b):
    return (a["committed"] is True and b["committed"] is True and a["action"] == b["action"]
            and _same_value(a["final_state"], b["final_state"]))


def _check_transaction(episode, arm):
    prior = episode["initial_state"]
    after = arm["final_state"]
    if not isinstance(after, dict) or not isinstance(arm["committed"], bool):
        raise ValueError("Every arm needs its materialized state and actual commit flag")
    if not arm["committed"] and not _same_value(after, prior):
        raise ValueError("A failed submission changed persisted state")
    if arm["committed"] and episode.get("actions"):
        options = _unique(episode["actions"], lambda a: a["id"])
        if arm["action"] not in options or not _same_value(after, {**prior, **options[arm["action"]]["patch"]}):
            raise ValueError("Recorded final state differs from the committed transaction")


def _score(episode, gold, arm, max_reads, max_chars):
    _check_transaction(episode, arm)
    prior, after = episode["initial_state"], arm["final_state"]
    windows = _unique(episode["windows"], lambda w: w["id"])
    reads = arm["reads"]
    if len(reads) > max_reads:
        raise ValueError("Read budget exceeded")
    protected = [*gold["protected_slots"], *gold.get("other_unchanged_slots", [])]
    if len(set(protected)) != len(protected) or any(k not in prior for k in protected):
        raise ValueError("Protected slots must be unique and present in the initial state")
    if not gold["actions"]:
        raise ValueError("Gold needs at least one accepted action, including explicit deferral if appropriate")
    target = gold["target_state"]
    if any(k in target and not _same_value(prior[k], target[k]) for k in protected):
        raise ValueError("Gold cannot require a change to a protected slot")
    actual_source = {}
    for w in episode["windows"]:
        actual_source[w["id"]] = w.get("locator", "").split("#")[0] or w.get("source_sha256") or w.get("source_id")
    grounded_reads = []
    chars = 0
    cost = 0.0
    for read in reads:
        w = windows[read["window_id"]]
        sid = read.get("source_id", w.get("source_id"))
        if w.get("source_id") and sid != w["source_id"]:
            raise ValueError("Read ledger source does not match the frozen catalogue")
        n = read["chars"]
        charge = read.get("cost", n)
        if type(n) is not int or n <= 0 or not isinstance(charge, (int, float)) or not math.isfinite(charge) or charge < 0:
            raise ValueError("Missing or invalid evidence-delivery meter")
        chars += n
        cost += charge
        grounded_reads.append({**read, "source_id": sid, "source_document": actual_source[w["id"]], "locator": read.get("locator") or w.get("locator")})
    if max_chars is not None and chars > max_chars:
        raise ValueError("Character budget exceeded")
    changed = sorted(k for k in prior.keys() | after.keys()
                     if not _same_value(prior.get(k, _MISSING), after.get(k, _MISSING)))
    target_misses = sorted(k for k, v in target.items() if not _same_value(after.get(k, _MISSING), v))
    damaged = sorted(set(changed) & set(protected))
    exposed = sorted(k for k in protected if any(
        k in a["patch"] and not _same_value(a["patch"][k], prior[k]) for a in episode.get("actions", [])))
    matches_action = arm["action"] in gold["actions"]
    return {**arm, "reads": grounded_reads, "read_count": len(reads), "chars": chars, "meter_cost": cost,
            "changed_slots": changed, "damaged_slots": damaged, "protected_count": len(protected),
            "at_risk_protected_slots": exposed, "at_risk_damaged_slots": sorted(set(damaged) & set(exposed)),
            "target_misses": target_misses,
            "correct": arm["committed"] and matches_action and not target_misses and not damaged}


def _validate_control(side, control, factual, opposite):
    base, swapped = _read_ids(factual), _read_ids(opposite)
    idx = control["replace_index"]
    if control.get("side") != side or idx != 0 or len(base) != 1 or len(swapped) != 1:
        raise ValueError("Crossover requires a one-window, one-decision intervention")
    if control["baseline_reads"] != base or control["swapped_reads"] != swapped or base == swapped:
        raise ValueError("Crossover must replace exactly the differing read")
    # When receipts expose hashes, a cross-replay must give byte-identical model input to the other arm.
    if opposite.get("input_sha256") and control.get("input_sha256") != opposite["input_sha256"]:
        raise ValueError("Crossover input differs from the opposite factual input")
    if factual.get("prefix_sha256") and (control.get("prefix_sha256") != factual["prefix_sha256"]
                                       or opposite.get("prefix_sha256") != factual["prefix_sha256"]):
        raise ValueError("Crossover must start from the same frozen prefix")
    if not all([factual.get("prefix_sha256"), control.get("prefix_sha256"), opposite.get("input_sha256"), control.get("input_sha256")]):
        return None
    return _compare_outcome(control, opposite)


def _attribution(arms, controls):
    left, right = (arms[k] for k in POLICIES)
    same_window = _read_ids(left) == _read_ids(right)
    same_source = [r["source_document"] for r in left["reads"]] == [r["source_document"] for r in right["reads"]]
    observed_change = (left["committed"] and right["committed"] and
                       (left["action"] != right["action"] or not _same_value(left["final_state"], right["final_state"])))
    by_side = _unique(controls, lambda c: c["side"])
    if set(by_side) - set(POLICIES):
        raise ValueError("Unknown crossover arm")
    twins = {}
    for side, opposite in zip(POLICIES, reversed(POLICIES)):
        if side in by_side:
            twins[side] = _validate_control(side, by_side[side], arms[side], arms[opposite])
    if not left["committed"] or not right["committed"]:
        verdict = "unsubmitted"
    elif same_window:
        verdict = "same_window_different_outcome" if observed_change else "same_window_same_outcome"
    elif not observed_change:
        verdict = "different_window_same_outcome"
    elif set(twins) != set(POLICIES):
        verdict = "different_outcome_no_complete_crossover"
    elif any(match is None for match in twins.values()):
        verdict = "different_outcome_unverified_inputs"
    elif any(not c["committed"] for c in by_side.values()):
        verdict = "different_outcome_unsubmitted_crossover"
    elif all(twins.values()):
        verdict = "different_outcome_bidirectionally_reproduced"
    elif any(twins.values()):
        verdict = "different_outcome_one_side_reproduced"
    else:
        verdict = "different_outcome_unstable"
    return {"different_window": not same_window, "different_source": not same_source,
            "both_committed": left["committed"] and right["committed"],
            "different_committed_action": left["committed"] and right["committed"] and left["action"] != right["action"],
            "different_committed_state": left["committed"] and right["committed"] and not _same_value(left["final_state"], right["final_state"]),
            "crossover_matches_opposite": twins, "result": verdict}


def _rate(n, d):
    return {"numerator": n, "denominator": d, "rate": n/d if d else None}


def _aggregate(rows):
    result = {}
    for side in POLICIES:
        arms = [row["arms"][side] for row in rows]
        correct_by_cluster = {}
        for row in rows:
            correct_by_cluster.setdefault(row["cluster"], []).append(row["arms"][side]["correct"])
        is_repair_needed = [r["repair_needed"] for r in rows]
        required = [arm for arm, needs in zip(arms, is_repair_needed) if needs]
        keep = [arm for arm, needs in zip(arms, is_repair_needed) if not needs]
        result[side] = {
            "correct": _rate(sum(a["correct"] for a in arms), len(arms)),
            "correct_when_change_required": _rate(sum(a["correct"] for a in required), len(required)),
            "correct_when_existing_state_valid": _rate(sum(a["correct"] for a in keep), len(keep)),
            "cluster_macro_accuracy": mean(mean(x) for x in correct_by_cluster.values()),
            "damaged_valid_slots": _rate(sum(len(a["damaged_slots"]) for a in arms), sum(a["protected_count"] for a in arms)),
            "damaged_at_risk_slots": _rate(sum(len(a["at_risk_damaged_slots"]) for a in arms),
                                           sum(len(a["at_risk_protected_slots"]) for a in arms)),
            "episodes_with_collateral_edits": _rate(sum(bool(a["damaged_slots"]) for a in arms), len(arms)),
            "unsubmitted": sum(not a["committed"] for a in arms), "reads": sum(a["read_count"] for a in arms),
            "chars": sum(a["chars"] for a in arms), "meter_cost": sum(a["meter_cost"] for a in arms),
        }
    discordance = Counter()
    for row in rows:
        a, b = (row["arms"][k]["correct"] for k in POLICIES)
        discordance["both" if a and b else "neither" if not a and not b else POLICIES[0] if a else POLICIES[1]] += 1
    left_only, right_only = (discordance[k] for k in POLICIES)
    discordant = left_only + right_only
    p_value = min(1.0, 2 * sum(math.comb(discordant, k) for k in range(min(left_only, right_only)+1)) / 2**discordant)
    differences = {metric: result["decision_value"][metric]["rate"] - result["relevance"][metric]["rate"]
                   if result["relevance"][metric]["rate"] is not None else None
                   for metric in ["correct", "correct_when_change_required", "correct_when_existing_state_valid",
                                  "damaged_valid_slots", "damaged_at_risk_slots", "episodes_with_collateral_edits"]}
    return {"policies": result, "paired_correct_counts": dict(discordance), "decision_minus_relevance": differences,
            "exact_mcnemar_p": p_value, "discordant_correct_count": discordant,
            "attribution_counts": dict(Counter(r["attribution"]["result"] for r in rows)),
            "different_windows": sum(r["attribution"]["different_window"] for r in rows),
            "different_sources": sum(r["attribution"]["different_source"] for r in rows),
            "different_committed_actions": sum(r["attribution"]["different_committed_action"] for r in rows),
            "different_committed_states": sum(r["attribution"]["different_committed_state"] for r in rows),
            "episodes": len(rows), "clusters": len({r["cluster"] for r in rows})}


def score_pairs(episodes, gold_rows, run_rows, control_rows=(), metadata=None):
    metadata = dict(metadata or {})
    read_budget = metadata.get("read_budget", {})
    metadata.setdefault("max_reads", read_budget.get("windows", 1))
    metadata.setdefault("max_chars", read_budget.get("max_chars"))
    fixtures = _unique(episodes, lambda r: r["id"])
    gold_rows = _unique(gold_rows, lambda r: r["id"])
    observed = _unique(run_rows, lambda r: (r["id"], r["policy"]))
    controls = _unique(control_rows, lambda r: (r["id"], r["side"]))
    pairs = {(i, k) for i in fixtures for k in POLICIES}
    if not fixtures or fixtures.keys() != gold_rows.keys() or set(observed) != pairs or not set(controls) <= pairs:
        raise ValueError("Every registered episode needs one sealed reference and both factual outcomes")
    rows = []
    for i, ep in fixtures.items():
        gold = gold_rows[i]
        arms = {k: _score(ep, gold, observed[i, k], metadata.get("max_reads", 1), metadata.get("max_chars")) for k in POLICIES}
        ancillary = [controls[i, k] for k in POLICIES if (i, k) in controls]
        for control in ancillary:
            _check_transaction(ep, control)
            if "reads" in control and _read_ids(control) != control["swapped_reads"]:
                raise ValueError("Model read receipt differs from crossover window assignment")
        if all(a.get("prefix_sha256") for a in arms.values()) and len({a["prefix_sha256"] for a in arms.values()}) != 1:
            raise ValueError("Factual arms do not share the same prefix")
        target_change = any(not _same_value(ep["initial_state"].get(k, _MISSING), v) for k, v in gold["target_state"].items())
        rows.append({"id": i, "label": ep.get("label", i), "cluster": ep.get("cluster", i),
                     "initial_state": ep["initial_state"], "repair_needed": target_change, "gold": gold, "arms": arms,
                     "origin": ep.get("origin", "unspecified"), "attribution": _attribution(arms, ancillary), "controls": ancillary})
    by_origin = {origin: _aggregate([r for r in rows if r["origin"] == origin])
                 for origin in sorted({r["origin"] for r in rows})}
    return {"metadata": metadata, "summary": _aggregate(rows), "by_origin": by_origin, "episodes": rows}


def _usage(records):
    """Aggregate only observed charges; a missing input is unknown, not zero."""
    usage = [r.get("decision_tokens", r.get("usage")) for r in records]
    result = {}
    for key in ("input_tokens", "output_tokens", "estimated_cost_rmb"):
        if all(isinstance(u, dict) and isinstance(u.get(key), (int, float)) and not isinstance(u[key], bool)
               for u in usage):
            result[key] = sum(u[key] for u in usage)
        else:
            result[key] = None
    return result


def _comparison_label(row):
    a = row["attribution"]
    if not a["both_committed"]:
        return "至少一臂未提交"
    if a["different_committed_action"] and a["different_committed_state"]:
        return "动作与状态均变"
    if a["different_committed_action"]:
        return "动作标签变，状态相同"
    return "动作相同，状态变" if a["different_committed_state"] else "动作、状态相同"


def _result_text(arm):
    if not arm["committed"]:
        return f"未提交（提案 {arm.get('proposed_action') or '无'}）"
    return f"{arm['action']}（{'正确' if arm['correct'] else '错误'}）"


def _escaped(cell):
    return str(cell).replace("|", "\\|").replace("\n", " ")


def _table_row(*cells):
    return "| " + " | ".join(_escaped(v) for v in cells) + " |"


def _short(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _read_text(arm):
    if not arm["reads"]:
        return "无读取"
    return "、".join(
        f"{r['window_id']} · {Path(r.get('locator') or r['source_id']).name} · {r.get('description', '')} [{r.get('start', '?')}:{r.get('end', '?')}]"
        for r in arm["reads"])


def _rate_text(rate):
    return f"{rate['numerator']}/{rate['denominator']}（{rate['rate']:.1%}）" if rate['rate'] is not None else "无分母"


_ATTRIBUTIONS = {
    "unsubmitted": "有未提交",
    "same_window_different_outcome": "同窗解码分歧",
    "same_window_same_outcome": "同窗复现",
    "different_window_same_outcome": "异窗，原运行同结果",
    "different_outcome_no_complete_crossover": "异窗切换，未做双向交叉",
    "different_outcome_unverified_inputs": "交叉输入未核实",
    "different_outcome_unsubmitted_crossover": "有交叉未提交",
    "different_outcome_bidirectionally_reproduced": "双向证据交换复现",
    "different_outcome_one_side_reproduced": "单向复现",
    "different_outcome_unstable": "双向均未复现",
}


def _costs(rows, controls=False):
    if controls:
        return _usage([c for r in rows for c in r["controls"]])
    return {p: _usage([r["arms"][p] for r in rows]) for p in POLICIES}


def _witness_text(row):
    gold = row["gold"]
    items = []
    for window_id in gold.get("witness_ids", []):
        w = row["catalogue"].get(window_id)
        if not w:
            raise ValueError(f"Unknown gold witness {window_id}")
        locator = w.get("locator", w.get("source_id", ""))
        quote = w.get("quote") or w.get("text")
        text = f"{window_id} · {locator} · {w.get('description', '')} [{w.get('start','?')}:{w.get('end','?')}]"
        if quote:
            text += " · " + str(quote).strip()
        items.append(text)
    return items


def _markdown(report):
    meta, rows, totals = report["metadata"], report["episodes"], report["summary"]
    by = totals["policies"]
    lines = ["# 同预算取证策略消融", "",
             f"共 {len(rows)} 个冻结冲突，归为 {totals['clusters']} 组。每题每臂上限 {meta.get('max_reads', 1)} 个原文窗口"
             + (f"、{meta['max_chars']} 个字符。" if meta.get("max_chars") is not None else "。"),
             "", "| 观察指标 | 文本相关性 | 修复决策价值 |", "|---|---:|---:|"]
    for field, label in (("correct", "正确提交与状态"), ("correct_when_change_required", "需要实际修改时正确修复"),
                         ("correct_when_existing_state_valid", "原状态有效时正确处理"),
                         ("damaged_valid_slots", "有效槽位误改率"), ("damaged_at_risk_slots", "可被备选动作改动的有效槽位误改率"), ("episodes_with_collateral_edits", "出现有效状态误改的题数")):
        lines.append(_table_row(label, *(_rate_text(by[k][field]) for k in POLICIES)))
    for field, label in (("unsubmitted", "未提交题数"), ("reads", "实际原文窗口"), ("chars", "实际原文字符")):
        lines.append(_table_row(label, *(by[k][field] for k in POLICIES)))
    lines.append(_table_row("每组等权正确率", *(f"{by[k]['cluster_macro_accuracy']:.1%}" for k in POLICIES)))
    for field, label in (("input_tokens", "完整模型请求输入 token"), ("output_tokens", "模型输出 token"),
                         ("estimated_cost_rmb", "模型调用估算费用（元）")):
        values = [totals["primary_usage"][p][field] for p in POLICIES]
        if any(v is not None for v in values):
            lines.append(_table_row(label, *(f"{v:.5f}" if isinstance(v, float) else v if v is not None else "未计量" for v in values)))
    lines += ["", "正确提交同时要求动作可接受、目标状态到位、受保护槽位完好。未提交列入错误，可提交的明确暂缓按该题的预期动作和实际状态判决。原文字符只计模型实际收到的正文，完整请求费用还包含共同指令、台账和回答。每次交叉复跑的成本另列。可误改分母计入至少有一个可选事务会改变其值的有效槽位。",
              "", "## 哪次读取改变了动作", "",
              f"两策略在 {totals['different_windows']} 题读取不同窗口，其中 {totals['different_sources']} 题来自不同来源。"
              f"已提交的动作标签有 {totals['different_committed_actions']} 题不同，最终状态有 {totals['different_committed_states']} 题不同。"
              f"两侧交换单个证据窗口、共享前缀且复制对侧完整输入后，有 {totals['attribution_counts'].get('different_outcome_bidirectionally_reproduced', 0)} 题再次得到对侧动作和状态。",
              "", "| 冲突 | 相关性所读原文 | 相关性提交 | 决策价值所读原文 | 决策价值提交 | 实际差异 | 同证据复现 |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        l, v = (r["arms"][p] for p in POLICIES)
        at = r["attribution"]
        matches = at["crossover_matches_opposite"]
        direction = "；".join(f"{k}→对侧{'一致' if ok is True else '分歧' if ok is False else '未核实'}"
                               for k, ok in matches.items())
        lines.append(_table_row(f"{r['id']} {r['label']}", _read_text(l), _result_text(l), _read_text(v), _result_text(v),
                                _comparison_label(r), _ATTRIBUTIONS[at["result"]] + (" · " + direction if direction else "")))
    lines += ["", "## 按冲突来源拆分", "",
              "| 冲突来源 | 题数/组数 | 文本相关性正确 | 决策价值正确 | 文本相关性误改槽 | 决策价值误改槽 |",
              "|---|---:|---:|---:|---:|---:|"]
    for origin, summary in report["by_origin"].items():
        rates = summary["policies"]
        lines.append(_table_row(origin, f"{summary['episodes']}/{summary['clusters']}",
                                *(_rate_text(rates[k]["correct"]) for k in POLICIES),
                                *(_rate_text(rates[k]["damaged_valid_slots"]) for k in POLICIES)))
    lines += ["", "## 逐题证据与双向交换", ""]
    for r in rows:
        lines.extend([f"### {r['id']} · {r['label']}", "",
                      f"来源类型 {_short(r['origin'])}，组别 {_short(r['cluster'])}。接受动作 {_short(r['gold']['actions'])}。"])
        rationale = r["gold"].get("rationale") or r["gold"].get("reason")
        if rationale:
            lines.append(str(rationale))
        witnesses = _witness_text(r)
        if witnesses:
            lines.extend(["", *[f"- 金标见证 {w}" for w in witnesses]])
        for side, name in zip(POLICIES, ("文本相关性", "修复决策价值")):
            arm = r["arms"][side]
            changes = ", ".join(f"{k}→{_short(arm['final_state'][k]) if k in arm['final_state'] else '删除'}" for k in arm["changed_slots"]) or "无"
            read_text = " · ".join((x.get("text") or x.get("quote") or r["catalogue"].get(x["window_id"], {}).get("text") or "") for x in arm["reads"])
            lines.extend(["", f"**{name}** 读取 {_read_text(arm)}（{arm['chars']} 字符）。{_result_text(arm)}。实际改变 {changes}。"])
            if read_text.strip():
                lines.append("> " + read_text.replace("\n", "\n> "))
        if r["controls"]:
            lines += ["", "| 被干预一侧 | 被换出的窗 → 换入的窗 | 新的提交动作 | 复现对侧动作和状态 |", "|---|---|---|---|"]
            for c in r["controls"]:
                match = r["attribution"]["crossover_matches_opposite"].get(c["side"])
                lines.append(_table_row(c["side"], f"{','.join(c['baseline_reads'])} → {','.join(c['swapped_reads'])}",
                                        c["action"] if c["committed"] else "未提交",
                                        "是" if match is True else "否" if match is False else "输入哈希未确认"))
    comparison = totals["paired_correct_counts"]
    control_usage = totals["crossover_usage"]
    accuracy_difference = totals["decision_minus_relevance"]["correct"]
    lines += ["", "## 计量口径", "",
              f"决策价值与相关性的正确率配对差为 {accuracy_difference:+.1%}。不一致对 {totals['discordant_correct_count']} 个，双侧精确 McNemar p={totals['exact_mcnemar_p']:.4g}。检验以冲突题为配对单位，题目来自同一项目；该 p 值只作描述。",
              f"对题配对结果为两臂均正确 {comparison.get('both', 0)}，均未正确 {comparison.get('neither', 0)}，相关性单独正确 {comparison.get('relevance', 0)}，决策价值单独正确 {comparison.get('decision_value', 0)}。",
              f"交叉控制共 {sum(len(r['controls']) for r in rows)} 次。复跑完整请求输入 {control_usage['input_tokens']} token、输出 {control_usage['output_tokens']} token，估算费用 {control_usage['estimated_cost_rmb']} 元。"]
    lines.extend(["", "## 实验范围", "",
                  "一次双向交换提供单题的条件性证据。两次独立解码足以发现不稳定，样本量和同一项目内的关联会影响外推。各题留存提交状态、原文标识、受保护项变化、请求哈希与交叉结果，便于复核。",
                  "", "[逐题原始判决和运行信息](paired_results.json)"])
    return "\n".join(lines) + "\n"


def write_report(output_dir, episodes, gold_rows, run_rows, control_rows=(), metadata=None):
    episodes = list(episodes)
    report = score_pairs(episodes, gold_rows, run_rows, control_rows, metadata)
    paths = Path(output_dir)
    # These source bytes are loaded only after all blind model decisions have finished.
    text_index_path = paths / "fixtures_texts.json"
    texts = json.loads(text_index_path.read_text(encoding="utf-8")) if text_index_path.is_file() else {}
    catalogues = {e["id"]: {w["id"]: ({**w, "text": texts[f"{e['id']}/{w['id']}"]}
                            if f"{e['id']}/{w['id']}" in texts else w) for w in e["windows"]} for e in episodes}
    for row in report["episodes"]:
        row["catalogue"] = catalogues[row["id"]]
    report["summary"]["primary_usage"] = _costs(report["episodes"])
    report["summary"]["crossover_usage"] = _costs(report["episodes"], controls=True)
    paths.mkdir(parents=True, exist_ok=True)
    markdown = _markdown(report)
    (paths / "paired_results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (paths / "report.md").write_text(markdown, encoding="utf-8")
    return report

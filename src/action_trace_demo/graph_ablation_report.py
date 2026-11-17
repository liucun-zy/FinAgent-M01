"""Post-run audit for BM25 and real historical-action-graph reranking.

Evidence receipts, ledger mutations, window-to-claim spans and original DAG edges
are audited separately. The ranker and the task model do not import this module.
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re

from .ablation_report import (_rate,
                              _rate_text, _read_text, _same_value, _score, _table_row,
                              _usage, _validate_control)

POLICIES = ("relevance", "graph")


def _load(path):
    return json.loads(path.read_text())


def _digest(path):
    return sha256(path.read_bytes()).hexdigest()


def _edge_tuple(edge):
    return (edge["from"], edge["to"], edge["kind"])


def _receipt(folder, episode, row):
    receipt = _load(folder / row["receipt"])
    if receipt.get("host") != "deepseek-harness" or receipt["committed"] != row["committed"]:
        raise ValueError("Action is not backed by this DSH receipt")
    if receipt["committed"]:
        event = receipt["result"]
        if (receipt["tool"] != "commit_repair" or event["action"] != row["action"]
            or not _same_value(event["initial_state"], episode["initial_state"])
            or not _same_value(event["final_state"], row["final_state"])):
            raise ValueError("Action or actual ledger differs from terminal receipt")
    if len(row["reads"]) != 1:
        raise ValueError("Expected exactly one delivered original per trial")
    w = next(w for w in episode["windows"] if w["id"] == row["reads"][0]["window_id"])
    read = row["reads"][0]
    if (w["window_sha256"] != sha256(read["text"].encode()).hexdigest()
        or read["start"] != w["start"] or read["end"] != w["end"]
        or read["chars"] != w["end"] - w["start"]):
        raise ValueError("Read does not match the frozen source window")
    return receipt


def _audit_paths(audit, prefix, episode, raw):
    catalogue = {w["id"]: w for w in episode["windows"]}
    if set(audit["order"]) != set(catalogue) or set(audit["bm25_order"]) != set(catalogue):
        raise ValueError("Reranker changed candidate membership")
    nodes, edges = raw
    anchors = {a["id"] if isinstance(a, dict) else a for a in audit["anchors"]}
    cutoff = prefix["cutoff"]
    forbidden = {"revision", "artifact_contract", "native_projection", "artifact_repair_proposal",
                 "artifact_check", "artifact_observation", "comparison", "probe", "diagnosis_step", "stage_submission"}
    for wid, paths in audit["paths"].items():
        win = catalogue[wid]
        for path in paths:
            sequence = path["edges"]
            if not sequence or any(_edge_tuple(edge) not in edges for edge in sequence):
                raise ValueError("Claimed route lacks an explicit exported edge")
            ids = {node for edge in sequence for node in (edge["from"], edge["to"])}
            if (not anchors & ids or any(nodes[n]["seq"] > cutoff or nodes[n]["kind"] in forbidden
                    or nodes[n]["kind"].startswith("qa_") or nodes[n]["payload"].get("stage", 0) > episode["stage"]
                    for n in ids)):
                raise ValueError("Path outside the historical cutoff or current proposal anchors")
            sid, cid = path["source_id"], path["claim_id"]
            claim, source = nodes[cid], nodes[sid]
            if (claim["kind"] != "claim" or source["kind"] != "source" or
                claim["payload"]["source_id"] != sid or sid not in ids or cid not in ids):
                raise ValueError("Path has no citation to its actual observed source")
            p = source["payload"]
            address = re.fullmatch(r"(.*?)(?:#chars=(\d+):(\d+))?", p["locator"])
            base = int(address[2] or 0)
            cp = claim["payload"]
            start, end = base + cp["start"], base + cp["end"]
            if (p["text"][cp["start"]:cp["end"]] != cp["quote"] or address[1] != win["locator"]
                or not win["start"] <= start < end <= win["end"]):
                raise ValueError("Candidate window does not contain the complete source-anchored claim quote")
    cf = audit["counterfactual"]
    if cf["empty_graph_winner"] != audit["baseline"] or (audit["changed"] and not audit["paths"][audit["selected"]]):
        raise ValueError("Failed no-edge control or promoted a window without a real route")
    return sum(len(paths) for paths in audit["paths"].values())


def _scored_run(folder, ep, gold, row, metadata):
    _receipt(folder, ep, row)
    budget = metadata.get("read_budget", {})
    return _score(ep, gold, row, budget.get("windows", 1), budget.get("max_chars", 960))


def _arm_summary(rows, side):
    arms = [r["arms"][side] for r in rows]
    required = [r["arms"][side] for r in rows if r["repair_needed"]]
    return {"correct": _rate(sum(a["correct"] for a in arms), len(arms)),
            "correct_when_change_required": _rate(sum(a["correct"] for a in required), len(required)),
            "protected_damage": _rate(sum(len(a["damaged_slots"]) for a in arms), sum(a["protected_count"] for a in arms)),
            "at_risk_damage": _rate(sum(len(a["at_risk_damaged_slots"]) for a in arms),
                                    sum(len(a["at_risk_protected_slots"]) for a in arms)),
            "deferred": sum(a["committed"] and a["action"] == "defer" for a in arms),
            "unsubmitted": sum(not a["committed"] for a in arms),
            "reads": sum(a["read_count"] for a in arms), "chars": sum(a["chars"] for a in arms),
            "usage": _usage(arms)}


def _matrix(rows):
    fresh = [r for r in rows if r["arms"]["relevance"]["correct"]]
    prior = [r for r in rows if r["previous_bm25_correct"]]
    gain = [r["id"] for r in rows if r["arms"]["graph"]["correct"] and not r["arms"]["relevance"]["correct"]]
    loss = [r["id"] for r in fresh if not r["arms"]["graph"]["correct"]]
    prior_graph_loss = [r["id"] for r in prior if not r["arms"]["graph"]["correct"]]
    return {"raw_gain_ids":gain, "raw_loss_ids":loss, "net_gain":len(gain)-len(loss),
            "new_bm25_correct_ids":[r["id"] for r in fresh], "new_bm25_regression_rate":_rate(len(loss),len(fresh)),
            "previous_bm25_correct_ids":[r["id"] for r in prior], "previous_correct_graph_incorrect_ids":prior_graph_loss,
            "previous_correct_new_comparison": {r["id"]: {s:r["arms"][s]["correct"] for s in POLICIES} for r in prior},
            "old_bm25_regression_rate":_rate(len(prior_graph_loss),len(prior)),
            "previous_correct_window_change_ids":[r["id"] for r in prior if r["window_changed"]]}


def _model_delta(arms, controls):
    b, g = (arms[k] for k in POLICIES)
    both = b["committed"] and g["committed"]
    return {"both_committed":both, "action_changed": both and b["action"] != g["action"],
            "state_changed": both and not _same_value(b["final_state"], g["final_state"]),
            "crossover_states_match":{k: c["committed"] and arms[o]["committed"] and _same_value(c["final_state"],arms[o]["final_state"])
                       for k,o in zip(POLICIES,reversed(POLICIES)) for c in controls if c["side"] == k},
            "crossovers_match":{k:_validate_control(k,c,arms[k],arms[o])
                               for k,o in zip(POLICIES,reversed(POLICIES)) for c in controls if c["side"] == k}}


def _history_correct(rows):
    return {r["id"] for r in rows["episodes"] if r["arms"]["relevance"]["correct"]}


def _rank_after_cut(audit, removed):
    distances = {}
    for window, paths in audit["paths"].items():
        costs = [len(path["edges"]) for path in paths
                 if not any(edge["edge_id"] in removed for edge in path["edges"])]
        if costs:
            distances[window] = min(costs)
    top = [w for w, d in distances.items() if d == min(distances.values())]
    return top[0] if len(top) == 1 else audit["baseline"]


def _check_edges(audit, episode, raw):
    count = _audit_paths(audit, audit, episode, raw)
    if _rank_after_cut(audit, set()) != audit["selected"]:
        raise ValueError("Chosen window is not independently reproducible from the paths")
    cf = audit["counterfactual"]
    for check in cf["individual"]:
        if _rank_after_cut(audit, {check["removed_edge"]}) != check["winner"]:
            raise ValueError("Single-edge ablation failed independent verification")
    joint = cf["joint"]
    if (_rank_after_cut(audit, set(joint["removed_edges"])) != joint["winner"]
            or not joint["restores_baseline"] or joint["winner"] != audit["baseline"]):
        raise ValueError("Joint edge ablation failed independent verification")
    return count


def _summary_lines(report):
    s=report["summary"]
    regress=s["regression"]
    names=lambda ids: "、".join(ids) or "无"
    lines = ["# BM25 与时序行动图的单窗对照", "",
             f"同一项目的 {len(report['episodes'])} 道题，各臂每题读取一个不超过 960 字符的原文窗。两臂共用开账状态、当时可见的历史台账、候选事务与候选原文目录；排序器在写入前选择本次要核对的出处。", "",
             "| 观察指标 | BM25 第一名 | 行动图重排 |", "|---|---:|---:|"]
    for field,label in (("correct","动作正确且落账状态正确"),("correct_when_change_required","需要修改时正确修复"),
                        ("protected_damage","有效槽位误改"),("at_risk_damage","其中有错误修改选项的有效槽位误改")):
        lines.append(_table_row(label,*(_rate_text(s["policies"][k][field]) for k in POLICIES)))
    for field, label in (("reads","实际窗口"),("chars","实际字符"),("deferred","已提交暂缓"),("unsubmitted","未提交")):
        lines.append(_table_row(label, *(s["policies"][k][field] for k in POLICIES)))
    for field,label in (("input_tokens","全请求输入 token"),("output_tokens","输出 token"),("estimated_cost_rmb","模型费用（元）")):
        lines.append(_table_row(label,*(s["policies"][k]["usage"][field] for k in POLICIES)))
    lines += ["",f"本轮 BM25 原本正确 {names(regress['new_bm25_correct_ids'])}，图重排后变错 {_rate_text(regress['new_bm25_regression_rate'])}。新增正确 {names(regress['raw_gain_ids'])}，失去正确 {names(regress['raw_loss_ids'])}，净增 {regress['net_gain']:+d} 题。",
              f"上一轮 BM25 做对 {names(regress['previous_bm25_correct_ids'])}。这些题本轮因图重排换窗 {names(regress['previous_correct_window_change_ids'])}，本轮图臂未答对 {names(regress['previous_correct_graph_incorrect_ids'])}。该跨轮对照额外受历史台账上下文和独立解码影响。", "",
              f"首窗改变 {s['changed_windows']} 题，提交动作改变 {s['action_changes']} 题，最终状态改变 {s['state_changes']} 题。换窗后双向复现动作与状态切换 {s['reproduced_action_changes']} 题，单独核对实际账面状态双向复现 {s['reproduced_state_changes']} 题。两臂读相同窗口却给出不同动作或状态 {names(s['same_input_disagreements'])}。", "",
              "| 冲突 | BM25 原文 → 图重排原文 | 胜出路径（按依赖方向） | 提交动作与台账 |", "|---|---|---|---|"]
    for r in report["episodes"]:
        a = r["rank"]
        pair = [r["arms"][k] for k in POLICIES]
        path_str = " / ".join(e["edge_id"] for e in a["paths"].get(a["selected"],[{}])[0].get("edges",[])) if a["paths"].get(a["selected"]) else "无完整路径"
        actions = [f"{_semantic(r, p)}（{'对' if p['correct'] else '错'}）" for p in pair]
        if not r["action_changed"] and not r["state_changed"]:
            delta="两臂动作和账值相同" if r["both_committed"] else "提交不完整"
        else:
            delta="账值实际改变" if r["state_changed"] else "动作标签变化，账值相同"
        lines.append(_table_row(f"{r['id']} {r['label']}", " → ".join(_read_text(p) for p in pair),
                                path_str, " → ".join(actions) + "；" + delta))
    return lines


def _semantic(row, arm):
    if not arm["committed"]:
        return "未提交"
    if arm["action"] == "defer":
        return "暂缓"
    return row["actions"][arm["action"]]["description"]


def _details(report):
    lines = ["", "## 逐题边删除与证据交换", ""]
    for r in report["episodes"]:
        a = r["rank"]
        lines += [f"### {r['id']} · {r['label']}", "",
                  f"严格切点 {a['cutoff']}。原排名 {'、'.join(a['bm25_order'])}，图排名 {'、'.join(a['order'])}。路由理由 `{a['reason']}`，找到 {r['path_count']} 条由实际边连接且引文完整落窗的路径。"]
        routes=a["paths"].get(a["selected"],[])
        if routes:
            lines+=[""] + [f"- 路径 {' → '.join(e['edge_id'] for e in p['edges'])}，引用范围 {p['quote_location']['start']}–{p['quote_location']['end']}。"
                           for p in routes]
        cf=a["counterfactual"]
        lines.extend(["",f"清空允许的图边，首窗为 {cf['empty_graph_winner']}。"])
        if a["changed"]:
            lines.extend(f"- 删除 {d['removed_edge']} 后首窗为 {d['winner']}。"
                         for d in cf["individual"])
            lines.append(f"- 同时删除 {'、'.join(cf['joint']['removed_edges'])} 后恢复 BM25 首窗 {cf['joint']['winner']}。")
        for k,name in zip(POLICIES,("BM25","图重排")):
            arm = r["arms"][k]
            fields = {i:arm["final_state"][i] for i in arm['changed_slots']}
            lines.extend(["",f"**{name}** · {_read_text(arm)}。{_semantic(r, arm)}。本题改写字段 `{json.dumps(fields,ensure_ascii=False)}`。"])
            if arm.get("quote"):
                lines.append("> "+arm['quote'].replace("\n","\n> "))
        if r["window_changed"]:
            matches = r["crossovers_match"]
            lines.extend(["", "两次交叉解码的输入均核对对侧完整请求哈希。"+ "，".join(
                f"{k} 换证后{'复现' if matches.get(k) else '未复现'}对侧动作和状态" for k in POLICIES)+"。"])
        if r["window_changed"]:
            lines.append("单独比对落账取值，两个交换方向" + ("均复现对侧状态。" if all(r["crossover_states_match"].values()) else "存在未复现的状态。"))
        lines.append("")
    lines.extend(["## 复核口径","",
                  "正确修复要求正式提交、可接受动作、目标状态到位且受保护槽位完好。维持旧值与明确确认旧值可能产生不同动作。结果记录每臂命中金标见证窗口的情况；引用字面存在不自动证明所有金融结论。源窗口的开销、交叉解码开销分别计算。",
                  "每个路径端点核对真实导出节点、源到 claim 的原句偏移及冻结时间。边删除只是对选窗器的干预；动作层归因还要求两个模型分支受控交换窗口，并在可复现时观察到输出改变。",
                  f"共 {report['summary']['control_calls']} 次交叉解码，输入 {report['summary']['control_usage']['input_tokens']} token，输出 {report['summary']['control_usage']['output_tokens']} token，费用 {report['summary']['control_usage']['estimated_cost_rmb']} 元。","",
                  "[冻结排序与逐题路径、验证收据和交叉输入哈希](paired_results.json) · [从物理图逐条删除并重遍历](edge_deletion_recheck.json)"])
    return lines


def write_report(output_dir, episodes, gold_rows, run_rows, control_rows, metadata, graph_audits, previous_correct=None):
    out = Path(output_dir)
    previous = Path(metadata["baseline_dir"])
    if previous_correct is None:
        previous_correct = _history_correct(_load(previous/"paired_results.json"))
    baseline_rankings = _load(previous/"rankings.json")
    path = Path(metadata["graph_path"])
    if _digest(path) != metadata["graph_sha256"]:
        raise ValueError("Original DAG hash changed")
    graph = _load(path)
    raw = ({n["id"]:n for n in graph["nodes"]}, {_edge_tuple(e) for e in graph["edges"]})
    golds = {g["id"]:g for g in gold_rows}
    lookup = {(a["id"],a["policy"]):a for a in run_rows}
    crossed = {(a["id"],a["side"]):a for a in control_rows}
    keys={(e["id"],side) for e in episodes for side in POLICIES}
    if set(lookup)!=keys or len(lookup)!=len(run_rows) or not crossed.keys() <= keys or len(crossed)!=len(control_rows):
        raise ValueError("Incomplete or duplicated study arms")
    results=[]
    for ep in episodes:
        i=ep["id"]
        audit=graph_audits[i]
        if audit["bm25_order"] != [r["id"] for r in baseline_rankings[i]["relevance"]]:
            raise ValueError("Ranker did not use the stored previous BM25 list")
        path_count = _check_edges(audit,ep,raw)
        arms = {s:_scored_run(out,ep,golds[i],lookup[i,s],metadata) for s in POLICIES}
        if ({s:a["reads"][0]["window_id"] for s,a in arms.items()} != {"relevance":audit["baseline"],"graph":audit["selected"]}
                or len({a["prefix_sha256"] for a in arms.values()})!=1):
            raise ValueError("Actual model input does not match registered selections and prefix")
        changes = audit["baseline"] != audit["selected"]
        if not changes and arms["relevance"]["input_sha256"] != arms["graph"]["input_sha256"]:
            raise ValueError("Same-window controls must receive exactly the same model request")
        controls={k:crossed[i,k] for k in POLICIES if (i,k) in crossed}
        if set(controls)!=(set(POLICIES) if changes else set()):
            raise ValueError("Exchange both directions for every divergent selection")
        for row in controls.values():
            _scored_run(out,ep,golds[i],row,metadata)
            if [r["window_id"] for r in row["reads"]] != row["swapped_reads"]:
                raise ValueError("Control receipt differs from assigned window")
        for a in arms.values():
            a["gold_witness_selected"] = a["reads"][0]["window_id"] in golds[i]["witness_ids"]
        state=ep["initial_state"]
        results.append({"id":i,"label":ep["label"],"origin":ep["origin"],"stage":ep["stage"],
                        "actions":{a["id"]:a for a in ep["actions"]},"gold":golds[i],"arms":arms,
                        "controls":list(controls.values()),"previous_bm25_correct":i in previous_correct,
                        "window_changed":changes,"repair_needed":any(not _same_value(state.get(k),v) for k,v in golds[i]["target_state"].items()),
                        "path_count":path_count,"rank":audit, **_model_delta(arms,controls.values())})
    totals = {"policies":{s:_arm_summary(results,s) for s in POLICIES}, "regression":_matrix(results),
              "changed_windows":sum(r["window_changed"] for r in results),
              "action_changes":sum(r["action_changed"] for r in results),
              "state_changes":sum(r["state_changed"] for r in results),
              "reproduced_action_changes":sum(r["action_changed"] and r["window_changed"] and all(r["crossovers_match"].values()) for r in results),
              "reproduced_state_changes":sum(r["state_changed"] and r["window_changed"] and all(r["crossover_states_match"].values()) for r in results),
              "same_input_disagreements":[r["id"] for r in results if not r["window_changed"] and (r["action_changed"] or r["state_changed"])],
              "control_calls":len(crossed),"control_usage":_usage(control_rows)}
    report = {"metadata":metadata,"summary":totals,"episodes":results}
    (out/"paired_results.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+"\n")
    (out/"report.md").write_text("\n".join(_summary_lines(report)+_details(report))+"\n")
    return report

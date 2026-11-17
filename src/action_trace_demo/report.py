"""Offline viewer for public run exports; never reads private evaluator inputs."""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


def _read_public_json(root: Path, relative: Path, default: Any, notices: list[str]) -> Any:
    """Read only the named public export, without following symlink components."""
    if relative.is_absolute() or ".." in relative.parts or "private_responder" in relative.parts:
        notices.append(f"Skipped undeclared report input: {relative.name}")
        return default
    target = root
    for part in relative.parts:
        target = target / part
        if target.is_symlink():
            notices.append(f"Skipped symlink report input: {relative.as_posix()}")
            return default
    if not target.is_file():
        notices.append(f"Missing report input: {relative.as_posix()}")
        return default
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        notices.append(f"Unreadable report input: {relative.as_posix()} ({type(exc).__name__})")
        return default


def render_report(run_dir: Path) -> Path:
    """Render an offline, single-file report and return its absolute path.

    Inputs are action_graph.json, ledger.json, metrics.json, run_status.json,
    and queries/*/answer.json only. Graph text is treated as untrusted data.
    Missing inputs remain explicit so failed runs are still inspectable.
    """
    root = Path(run_dir).resolve()
    if not root.is_dir():
        raise FileNotFoundError("Report run directory does not exist")
    notices: list[str] = []
    graph = _read_public_json(root, Path("action_graph.json"), {"nodes": [], "edges": []}, notices)
    ledger = _read_public_json(root, Path("ledger.json"), {}, notices)
    metrics = _read_public_json(root, Path("metrics.json"), {}, notices)
    status = _read_public_json(root, Path("run_status.json"), {}, notices)
    if not isinstance(graph, dict):
        notices.append("action_graph.json is not an object")
        graph = {"nodes": [], "edges": []}
    if not isinstance(ledger, (dict, list)):
        notices.append("ledger.json is not an object or array")
        ledger = {}
    nodes = [n for n in graph.get("nodes", []) if isinstance(n, dict)] if isinstance(graph.get("nodes", []), list) else []
    edges = [e for e in graph.get("edges", []) if isinstance(e, dict)] if isinstance(graph.get("edges", []), list) else []
    queries = []
    query_root = root / "queries"
    if query_root.is_dir() and not query_root.is_symlink():
        for folder in sorted(query_root.iterdir()):
            if folder.is_dir() and not folder.is_symlink() and (folder / "answer.json").exists():
                relative = folder.relative_to(root) / "answer.json"
                answer = _read_public_json(root, relative, None, notices)
                if isinstance(answer, dict):
                    queries.append(answer)
    if graph.get("order","").startswith("knowledge sequence"):
        nodes = [{**n.get("payload",{}),**n} for n in nodes]
        ledger = {**ledger,"claims":[{"claim_id":n["id"],**n["payload"]} for n in ledger.get("claims",[])]}
        # Only already-exported knowledge and terminal receipts enter this view.
        questions={n["id"]:n for n in nodes if n["kind"]=="question"}
        for n in nodes:
            if n["kind"]=="qa_answer":
                q=questions.get(n.get("question_id"),{})
                queries.append({"question":q.get("question"),"status":n["status"],"answer":n["answer"],
                    "stage_cutoff":"事件 "+str(n["as_of"]),"routing_mode":"chronological DAG","evidence":n.get("citations")})
        status={"state":"DSH graph export","completed_stages":[n["stage"] for n in nodes if n["kind"]=="stage_submission"]}
        receipts=[_read_public_json(root,p.relative_to(root),{},notices) for p in (root/".dsh").glob("*/receipt.json")]
        usage=[r["usage"] for r in receipts if isinstance(r,dict) and isinstance(r.get("usage"),dict)]
        metrics={k:sum((u.get(k) or 0) for u in usage) for k in ("calls","known_cost_rmb","input_tokens","output_tokens")}
        metrics["usage_complete"]=all(u.get("usage_complete") for u in usage)
        metrics["estimated_cost_rmb"]=metrics["known_cost_rmb"] if metrics["usage_complete"] else None
        notices=[msg for msg in notices if msg not in {"Missing report input: metrics.json","Missing report input: run_status.json"}]
    data = {
        "schema_version": "0.1", "run_label": root.name,
        "local_research_only": True, "official_score": None,
        "graph": {**graph, "nodes": nodes, "edges": edges},
        "ledger": ledger, "metrics": metrics if isinstance(metrics, dict) else {},
        "status": status if isinstance(status, dict) else {},
        "queries": queries, "notices": notices,
    }
    # application/json is still parsed by the HTML parser: escaping '<' is
    # essential, even though JSON strings themselves may legally contain it.
    serialized = json.dumps(data, ensure_ascii=False, allow_nan=False)
    serialized = serialized.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    before_data, _, after_data = _HTML.partition("__REPORT_DATA__")
    before_data = before_data.replace("__RUN_TITLE__", html.escape(root.name, quote=True), 1)
    document = before_data + serialized + after_data
    output = root / "report.html"
    if output.is_symlink():
        raise ValueError("Refuse to overwrite a symlink report output")
    output.write_text(document, encoding="utf-8")
    return output


_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; img-src 'none'; base-uri 'none'; form-action 'none'">
<title>行动轨迹运行报告 · __RUN_TITLE__</title>
<style>
:root{color-scheme:light;--ink:#18222c;--muted:#66717b;--line:#dce2e7;--paper:#fff;--bg:#f4f6f8;--blue:#345b85;--red:#9b3d3d;--amber:#8a641a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:1480px;margin:auto;padding:26px 28px 70px}h1{font-size:26px;line-height:1.3;margin:8px 0}h2{font-size:18px;margin:0 0 12px}h3{font-size:15px;margin:0 0 8px}.muted{color:var(--muted)}.small{font-size:12px}.label{font-size:12px;letter-spacing:.04em;color:var(--muted)}.banner{border-left:4px solid var(--blue);background:#eaf0f6;padding:12px 16px;margin:18px 0}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:18px 0}.card,.panel{background:var(--paper);border:1px solid var(--line);border-radius:8px}.card{padding:13px 15px}.value{font-size:23px;line-height:1.4;overflow-wrap:anywhere}.panel{padding:18px;margin-top:16px}.filters{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}.filters input[type=search]{flex:1;min-width:230px}.filters input,.filters select,button{font:inherit;border:1px solid #c9d1d9;background:#fff;border-radius:5px;padding:7px 9px;color:var(--ink)}button{cursor:pointer}button:hover{background:#eef3f7}.layout{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(300px,1fr);gap:16px}.graph{background:#fafbfd;border:1px solid var(--line);border-radius:6px;overflow:auto;min-height:210px}.graph svg{display:block;max-width:100%;height:auto}.edge{stroke:#b7c3cf;stroke-width:1;fill:none}.node{cursor:pointer}.node circle{stroke:#fff;stroke-width:2}.node.selected circle{stroke:#111;stroke-width:3}.node text{font-size:10px;fill:#253544}.table-wrap{overflow:auto;max-height:440px}table{width:100%;border-collapse:collapse;font-size:12px}th,td{text-align:left;vertical-align:top;padding:8px;border-bottom:1px solid var(--line)}th{position:sticky;top:0;background:#f3f6f8;z-index:1}tr.clickable{cursor:pointer}tr.clickable:hover{background:#f2f6fa}.status{display:inline-block;border-radius:4px;background:#e9eef3;padding:1px 6px;font-size:12px;overflow-wrap:anywhere}.status.bad{background:#f9eaea;color:var(--red)}.status.warn{background:#fbf2db;color:var(--amber)}pre{margin:8px 0;white-space:pre-wrap;word-break:break-word;font:12px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;background:#f5f7f9;border:1px solid var(--line);padding:10px;border-radius:5px;max-height:480px;overflow:auto}.quote{border-left:3px solid #a4b6c9;background:#f5f8fb;padding:9px 12px;white-space:pre-wrap;overflow-wrap:anywhere}.details-line{margin:6px 0;overflow-wrap:anywhere}.links{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}.query{border-top:1px solid var(--line);padding:16px 0}.query:first-child{border-top:0;padding-top:0}.answer{white-space:pre-wrap;overflow-wrap:anywhere;margin:10px 0}.query-meta{display:flex;gap:8px;flex-wrap:wrap}.empty{padding:20px;color:var(--muted)}details summary{cursor:pointer;color:var(--blue)}.legend{display:flex;gap:12px;flex-wrap:wrap}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}noscript{display:block;padding:18px;background:#fff0dc}footer{margin-top:22px;color:var(--muted);font-size:12px}
@media(max-width:850px){main{padding:18px 14px}.layout{grid-template-columns:1fr}.value{font-size:20px}}
</style>
</head>
<body><main>
<div class="label">ACTION TRACE DEMO · LOCAL RESEARCH</div>
<h1>行动轨迹运行报告</h1>
<div id="run-title" class="muted"></div>
<div class="banner"><strong>本地研究运行，没有官方 DeliverBench 验收分数。</strong><br>动作被运行器接受、模型返回答复或引用位置通过检查，均不等于金融内容已核验。该页只展示公开运行导出，不执行模型调用或联网回查。</div>
<div class="cards" id="stats"></div>
<div id="notices"></div>
<section class="panel">
<h2>行动与 Claim 图</h2>
<div class="filters"><input id="search" type="search" placeholder="搜索 ID、实体、字段、描述或引文" aria-label="搜索节点"><select id="kind" aria-label="节点类别"></select><select id="stage" aria-label="阶段"></select><label><input id="rejected" type="checkbox"> 仅拒绝或暂缓</label></div>
<div id="filter-count" class="muted small"></div>
<div class="legend small" id="legend"></div>
<div class="layout">
<div><div class="graph" id="graph"></div><p class="small muted">图显示当前筛选结果中的最多80个节点；完整筛选结果在下方列表。点击节点查看详情及关联边。</p></div>
<div><h3>节点详情</h3><div id="detail" class="empty">选择图节点或列表记录。</div></div>
</div>
<div class="table-wrap"><table><thead><tr><th>节点 ID</th><th>类别／状态</th><th>阶段</th><th>内容</th></tr></thead><tbody id="node-table"></tbody></table></div>
</section>
<section class="panel"><h2>当前台账</h2><p class="small muted">这是本次运行的导出台账，不是参考答案；历史问答应使用对应的阶段视图。</p><div class="table-wrap"><table><thead><tr><th>Claim</th><th>实体／字段</th><th>值</th><th>范围与有效时间</th></tr></thead><tbody id="ledger-table"></tbody></table></div></section>
<section class="panel"><h2>问答与核查结果</h2><div id="queries"></div></section>
<footer>该文件可直接本地打开。所有描述和引文按纯文本展示；图边与模型解释是可检查的记录，不是正确性的自动证明。</footer>
</main><noscript>请启用浏览器JavaScript以查看本文件内嵌的运行数据；报告不需要网络。</noscript>
<script type="application/json" id="report-data">__REPORT_DATA__</script>
<script>
"use strict";
const data=JSON.parse(document.getElementById("report-data").textContent);
const nodes=data.graph.nodes||[],edges=data.graph.edges||[];
const byId=new Map(nodes.map(n=>[String(n.id),n]));
const colors={source:"#7890a7",claim:"#3f7690",action:"#6b78a8",comparison:"#9b8e69",artifact_action:"#477e68",artifact_version:"#9a775c"};
const kindNames={source:"原文来源",claim:"Claim",action:"维护行动",comparison:"Claim比较",artifact_action:"文件行动",artifact_version:"文件版本"};
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);if(cls)n.className=cls;return n;}
function plain(v){if(v===undefined||v===null)return "—";return typeof v==="object"?JSON.stringify(v):String(v);}
function pretty(v){return JSON.stringify(v,null,2);}
function statusOf(n){return String(n.status||n.structural_status||"");}
function statusClass(s){return ["rejected","failed","turn_limit","candidate_turn_limit","budget_exhausted"].includes(s)?"bad":["held","proposed","incomplete_submission"].includes(s)?"warn":"";}
function pill(s){return el("span",s,"status "+statusClass(s));}
function label(n){return plain(n.field||n.path||n.entity||n.description||n.id);}
function addCard(label,value,note){const c=el("div",undefined,"card");c.append(el("div",label,"label"),el("div",value,"value"));if(note)c.append(el("div",note,"small muted"));document.getElementById("stats").append(c);}
const m=data.metrics||{},s=data.status||{};
document.getElementById("run-title").textContent=data.run_label;
addCard("运行状态",plain(s.state),"官方分数：未运行");
addCard("完成阶段",Array.isArray(s.completed_stages)?s.completed_stages.length:"—",Array.isArray(s.completed_stages)?s.completed_stages.join(", "):"");
addCard("图节点／边",nodes.length+" / "+edges.length,"包含文件行动与版本");
addCard("拒绝／暂缓",nodes.filter(n=>statusOf(n)==="rejected").length+" / "+nodes.filter(n=>statusOf(n)==="held").length,"保留原始维护决定");
addCard("模型调用",plain(m.calls),"含记录在案的重试");
const knownCost=m.known_cost_rmb;
addCard("估算成本 RMB",typeof m.estimated_cost_rmb==="number"?m.estimated_cost_rmb.toFixed(4):"未完整计价",typeof knownCost==="number"?"已知用量成本小计 "+knownCost.toFixed(4):"费用按配置单价估算");
addCard("输入／输出 Token",plain(m.input_tokens)+" / "+plain(m.output_tokens),m.usage_complete===false?"存在未知用量，未按零计":"以导出的供应商用量为准");
if(m.responder)addCard("Responder",plain(m.responder.calls)+" 次调用","仅显示metrics内已汇总数据");
if(s.error){const box=el("div",undefined,"banner");box.append(el("strong","运行错误"),el("div",plain(s.error)));document.getElementById("notices").append(box);}
if(data.notices.length){const d=el("details");d.append(el("summary","导出文件提示（"+data.notices.length+"）"));const ul=el("ul");for(const n of data.notices)ul.append(el("li",n));d.append(ul);document.getElementById("notices").append(d);}
const kindSelect=document.getElementById("kind"),stageSelect=document.getElementById("stage");
function option(select,value,text){const o=el("option",text);o.value=value;select.append(o);}
option(kindSelect,"all","全部节点类别");
for(const kind of [...new Set(nodes.map(n=>String(n.kind||"unknown")))].sort())option(kindSelect,kind,(kindNames[kind]||kind)+"（"+nodes.filter(n=>String(n.kind||"unknown")===kind).length+"）");
option(stageSelect,"all","全部阶段");
for(const stage of [...new Set(nodes.map(n=>plain(n.stage)))].sort((a,b)=>Number(a)-Number(b)))option(stageSelect,stage,"阶段 "+stage);
for(const [kind,color] of Object.entries(colors)){const item=el("span");const dot=el("i",undefined,"dot");dot.style.backgroundColor=color;item.append(dot,el("span",kindNames[kind]));document.getElementById("legend").append(item);}
let selectedId=null;
function selectNode(id){selectedId=String(id);const n=byId.get(selectedId);if(!n)return;const detail=document.getElementById("detail");detail.className="";detail.replaceChildren();detail.append(el("h3",plain(n.id)),el("div",kindNames[n.kind]||plain(n.kind),"muted"));if(statusOf(n))detail.append(pill(statusOf(n)));if(n.description)detail.append(el("p",n.description));if(n.quote)detail.append(el("div",n.quote,"quote"));const links=el("div",undefined,"links");for(const edge of edges.filter(e=>String(e.from)===selectedId||String(e.to)===selectedId).slice(0,30)){const target=String(edge.from)===selectedId?String(edge.to):String(edge.from);if(!byId.has(target))continue;const b=el("button",plain(edge.kind)+" → "+target);b.type="button";b.addEventListener("click",()=>selectNode(target));links.append(b);}if(links.childNodes.length)detail.append(el("div","关联节点","small muted"),links);const raw=el("details");raw.append(el("summary","完整记录"),el("pre",pretty(n)));detail.append(raw);document.querySelectorAll(".node").forEach(g=>g.classList.toggle("selected",g.dataset.id===selectedId));}
function svgEl(tag,attrs){const n=document.createElementNS("http://www.w3.org/2000/svg",tag);for(const [k,v] of Object.entries(attrs||{}))n.setAttribute(k,String(v));return n;}
function drawGraph(visible){const host=document.getElementById("graph");host.replaceChildren();const shown=visible.slice(0,80);if(!shown.length){host.append(el("div","没有匹配节点。","empty"));return;}const columns=Math.min(7,Math.max(2,Math.ceil(Math.sqrt(shown.length)))),rows=Math.ceil(shown.length/columns);const width=columns*145+20,height=rows*84+35;const svg=svgEl("svg",{viewBox:"0 0 "+width+" "+height,role:"img","aria-label":"行动与Claim节点关系"});const positions=new Map();shown.forEach((n,i)=>positions.set(String(n.id),{x:80+(i%columns)*145,y:32+Math.floor(i/columns)*84}));for(const e of edges){const a=positions.get(String(e.from)),b=positions.get(String(e.to));if(a&&b){const path=svgEl("path",{d:"M"+a.x+","+a.y+" L"+b.x+","+b.y,class:"edge"});const title=svgEl("title");title.textContent=plain(e.kind);path.append(title);svg.append(path);}}for(const n of shown){const id=String(n.id),p=positions.get(id),g=svgEl("g",{class:"node"+(selectedId===id?" selected":""),tabindex:0,role:"button","aria-label":id+" "+label(n)});g.dataset.id=id;const fill=statusOf(n)==="rejected"?"#b96a6a":colors[n.kind]||"#7c8790";g.append(svgEl("circle",{cx:p.x,cy:p.y,r:11,fill}));const text=svgEl("text",{x:p.x,y:p.y+27,"text-anchor":"middle"});text.textContent=id.slice(0,22);g.append(text);const subtitle=svgEl("text",{x:p.x,y:p.y+41,"text-anchor":"middle"});subtitle.textContent=label(n).slice(0,19);g.append(subtitle);const title=svgEl("title");title.textContent=id+" · "+label(n);g.append(title);g.addEventListener("click",()=>selectNode(id));g.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();selectNode(id);}});svg.append(g);}host.append(svg);}
function renderNodes(){const term=document.getElementById("search").value.trim().toLowerCase(),kind=kindSelect.value,stage=stageSelect.value,rejected=document.getElementById("rejected").checked;const visible=nodes.filter(n=>(kind==="all"||String(n.kind||"unknown")===kind)&&(stage==="all"||plain(n.stage)===stage)&&(!rejected||["rejected","held"].includes(statusOf(n)))&&(!term||JSON.stringify(n).toLowerCase().includes(term)));document.getElementById("filter-count").textContent="匹配 "+visible.length+" / "+nodes.length+" 个节点；列表最多显示500条。";const table=document.getElementById("node-table");table.replaceChildren();for(const n of visible.slice(0,500)){const tr=el("tr",undefined,"clickable");tr.tabIndex=0;const id=el("td",plain(n.id)),kindCell=el("td",kindNames[n.kind]||plain(n.kind));if(statusOf(n)){kindCell.append(el("br"),pill(statusOf(n)));}tr.append(id,kindCell,el("td",plain(n.stage)),el("td",label(n)));tr.addEventListener("click",()=>selectNode(n.id));tr.addEventListener("keydown",e=>{if(e.key==="Enter")selectNode(n.id);});table.append(tr);}drawGraph(visible);}
for(const id of ["search","kind","stage","rejected"])document.getElementById(id).addEventListener(id==="search"?"input":"change",renderNodes);
const ledgerRows=Array.isArray(data.ledger)?data.ledger:(Array.isArray(data.ledger.claims)?data.ledger.claims:[]),ledgerTable=document.getElementById("ledger-table");
for(const c of ledgerRows){const tr=el("tr");tr.append(el("td",plain(c.claim_id||c.id)),el("td",plain(c.entity)+" / "+plain(c.field)),el("td",plain(c.value)),el("td",[plain(c.scope),plain(c.valid_from),plain(c.valid_to)].join(" · ")));ledgerTable.append(tr);}if(!ledgerRows.length){const tr=el("tr"),td=el("td","没有导出的当前Claim。");td.colSpan=4;tr.append(td);ledgerTable.append(tr);}
const queryHost=document.getElementById("queries");
for(const q of data.queries){const item=el("article",undefined,"query");item.append(el("h3",plain(q.question)));const meta=el("div",undefined,"query-meta");meta.append(pill(String(q.status||"unknown")),el("span","截止阶段 "+plain(q.stage_cutoff),"small muted"),el("span","路由 "+plain(q.routing_mode),"small muted"),el("span","轮数 "+plain(q.turns),"small muted"));if(q.budget_forced_final)meta.append(el("span","预算末轮强制收束 · 非完整核查证明","status warn"));item.append(meta);if(q.status==="turn_limit")item.append(el("p","达到轮数上限；该次查询没有形成最终答复。","muted"));if(q.answer)item.append(el("div",q.answer,"answer"));else item.append(el("div","未形成最终答复文本。","answer muted"));const raw=el("details");raw.append(el("summary","完整查询结果"),el("pre",pretty(q)));item.append(raw);queryHost.append(item);}if(!data.queries.length)queryHost.append(el("div","尚无导出的问答结果。","empty"));
renderNodes();
</script></body></html>
'''

# M01 表格语义与上下文合同

本次修复将表格语法识别集中到 `table_structure.parse_table`；现有正式 normalize、source unit、DSH保存及SQLite接口继续使用。HTML由lxml解析，GFM由markdown-it-py解析；不依赖任务名、基金代码或业务属性词典。

## 研究结论与覆盖

依据：[HTML表格及表头关联规范](https://html.spec.whatwg.org/multipage/tables.html#header-and-data-cell-semantics)、[GFM tables](https://github.github.com/gfm/#tables-extension-)、[MinerU固定版本的内容渲染](https://github.com/opendatalab/MinerU/blob/f0497e0c6e9575fa553ce57cc9aa867906a00572/mineru/render/_internal/content_list/v1.py)、[lxml HTML解析](https://lxml.de/lxmlhtml.html)、[markdown-it-py使用文档](https://markdown-it-py.readthedocs.io/en/latest/using.html)。HTML的标签、表头关系和网格坐标是不同层次，不能只靠整行是否全部为th来判定。

| 表示 | 本地处理 |
|---|---|
| HTML `th` / `thead`中的`td` / ARIA rowheader、columnheader | 保留明确表头身份与来源依据 |
| `scope=col/row/colgroup/rowgroup`、`headers/id` | 单元格绑定实际表头；显式headers优先，行表头不传播到不相关行 |
| 多级列头、rowspan/colspan、rowspan=0 | 构建占位网格、列覆盖和行组范围，保留父子表头；重复表头带替换上一带 |
| 全td、无显式语义表头 | 第一行保留为未确认候选上下文，并继续作为普通主单元处理；不强行认定它是表头或删除其事实 |
| caption、外部脚注、表前说明、表内注释 | 保留可定位上下文及表／章节／行组作用范围；不将其内容自动变成每条事实的已确认限定 |
| 嵌套HTML表格 | 仅外表参与外层行网格，内表文字保留在父单元格中；原始HTML留存，不把内表行混进外表；不宣称内表独立层次已完全建模 |
| 省略结束标签等可恢复HTML | 由lxml恢复DOM；不自己拼接不完整标签 |
| GFM pipe表 | 识别分隔行、对齐、可选外侧竖线、转义竖线和行内格式；源行与表格定位保留 |
| Markdown嵌入HTML | 进入同一HTML表格合同，合并单元格等不会因转成pipe表而丢失 |

[Pandoc](https://pandoc.org/MANUAL.html#tables)另有simple、multiline、grid等Markdown扩展。当前不将这些方言冒充GFM结构表，也不凭纯文本猜列；原文继续作为文本保留。未知多层全td表的首行仅作候选，不承诺推断全部表头层级。跨页逻辑关联见下节，物理片段和原文继续独立保存。

## 数据合同

每个单元格保存明确的 `header_ref_ids` 和 `candidate_header_ref_ids`。行scope、显式id和多级跨度在规范化阶段处理，source unit无需再猜角色。必要关联随数据行进入独立请求；分组与截断恢复重新渲染同一source unit时也保留关系。

候选前导行的模型上下文带 `role=header_candidate`、`status=unconfirmed`、`basis=untyped_leading_row`，与确定headers分开。它保持普通cell身份，可继续提取本身事实。正式单位校验只消费确定header关系，候选不会自动授权数值换算。单位校验仅增加对已交付精确header_ref_ids的选择，没有修改数值换算或工作日识别规则。

M01 v1 冻结版本的本地规范化为 `mineru-content-contract-v5-table-title-scope`，source unit为 `source-elements-v5-table-title-scope`。它们进入既有提取profile；远端MinerU profile和已下载结果不改，不会因为本地修复重新提交PDF。SQLite schema、DSH保存、单位校验与身份模型框架均不变。

### M01 v1：文本表题与引用条件接入

明确表题也可能被解析成普通 text 或单单元格表格。正式上下文组织现在优先使用解析器明确 caption 或紧邻数据表的独立引号表题，再回退到真实父标题；不跨过普通正文／其他表格寻找方便匹配的标题。附件／附录表题开始新的逻辑说明范围，前一章节的普通说明不会跟随进入。物理 kind、parent、原文、页码与 bbox 不回写。

程序仍要求前文真实引用完整表题，才关联引用段落及其所在章节的明确条件。最近明确表题优先于解析器遗留的父标题，链接续表继承同一关系；状态仍为 context_only，不自动补写 claim 条件。没有明确表题或无法确认治理范围时保持未知。

该规则修复 F023 附件四表题被标成 text 后漏关联审批／生效条件的问题，不依赖基金代码、特定标题或属性。正式版本与封装见 [M01 v1 基线](m01_v1_baseline.md)。

## 续表链接与说明范围（2026-11-16）

`table_context.organize_table_context` 在现有规范化完成后组织关系，HTML／Office／PDF／直接Markdown共用同一入口。无需额外依赖、OCR或LLM。

参考实际实现：[PaddleX固定版本续表处理](https://github.com/PaddlePaddle/PaddleX/blob/4c968d30b9d0d4a693d9eaaef0881c20f8d7d0e0/paddlex/inference/pipelines/layout_parsing/merge_table.py)、[AWS TRP固定版本表格检查](https://github.com/aws-samples/amazon-textract-response-parser/blob/a9b44fee2aa9aecdc8fd59e1201a13d1164e8104/src-python/trp/t_tables.py)、[AWS LINK与MERGE说明](https://aws.amazon.com/blogs/machine-learning/postprocessing-with-amazon-textract-multi-page-table-handling/)。本地独立实现其布局／列结构判断与逻辑链接方式，没有引入PaddleOCR或Textract服务。

- 基于已有网格（包含rowspan/colspan）、表宽、左右边界、页间正文和显式表头判断相邻续表。表宽相对差小于10%，两侧边界差小于较窄表宽的5%；列数相同本身不足以连表。不同显式表头或夹入正文会阻断普通续表。
- 已有MinerU合并片段的结束页参与邻接判断。`table_context` 保留 `logical_table_id`、前后片段、依据、状态及链接路径；各片段ID、原HTML、行／单元格、页码和bbox均不移动、不清空，不拼接跨页单元格。
- 沿用最后一组明确列头；全td首行保留未确认候选身份。续表中首个业务行继续可提取，不再作为与根表竞争的伪表头。
- 只有两列的残缺续表若有明确续接标记，可进入候选链，但列映射为 `unresolved`，不按三列位置猜配。
- 对单页退化成正文的间隔，必须同时满足两端表格对齐、中间文字位于相容的多个版面栏、没有横跨栏位的正文／标题、边界有足够长的重复原文，才保存 `candidate` 关系。这是本地对上游相邻规则的扩展；不声称已确认语义连续，也不补造缺失单元格。多页正文间隔、旋转或重排列、无布局线索时保持断开。
- 表前独立单列表及连续段落成为紧随数据表的说明；明确的全局金额说明可在同章节／工作表内复用，新的一般说明替换旧说明。新标题和Office工作表边界重置范围，普通说明默认不扩散到下一张表。
- 表后明确注释绑定前表；表内注释按列头带／注释边界确定行组范围。撤掉旧版“所有稀疏业务行广播到整表”的逻辑；普通稀疏业务行仍独立提取。
- 对附件表，可依据真实的完整表题引用连接引用段落，并保留该引用所在章节的审批／生效条件。只做原文关联；不把“修订对照表”一律推断为未生效。
- `table_note_bindings` 和模型侧 `table_notes` 分开表达说明来源、范围和依据，状态始终为 `context_only`。例外（如每股数据）完整保留，由模型解释哪些事实适用。正常提取、重组请求和截断恢复均保留这些关联。

本轮只诊断 MARS-007／011／033 和 F031 各一行。输入关系4/4恢复；三个数值目标的货币尺度及期间恢复，F031新旧列身份恢复。按冻结完整业务预期严格通过2/4：MARS-033租金未保留Beta限定，F031仍漏拟议／待通过状态。不得用已提供上下文代替正式claim中的缺失语义。实际调用、保存及逐字段证据见 `analysis/m01_table_scope_20261116_01/report.md`。

现有正式入口自动使用新版组织逻辑：

```bash
.venv/bin/action-trace ingest --workspace <新工作区> --manifest <来源清单.json> --config <工程配置.json>
```

复用已有parse cache需在清单／配置中使用已有缓存路径；本轮诊断将 `mineru.max_new_tasks=0`，仅选择四个目标行，未通过整源ingest扩大提取范围。诊断脚本调用同一规划、DSH执行、stage和merge内核，不是新建模型调用通道。

## 前轮表头限定验证（保留历史口径）

先将上一轮全部45个非严格通过锚点封装，再固定15个表格失败案例、原业务预期和旧输出。实际调用只用这15个案例原提取包对应的主单元，加新版必要表头；不把整份长文重提一遍。19组、20并发、最多57次传输，仍为EFundGPT-air/4096输出；实际成本见新报告。调用经过现有DSH save_extraction、stage_extraction与merge_arrival；未运行身份匹配模型。这是受控表格提取/保存诊断，不是新一轮完整M01准确率。

必要检查命令：

```bash
.venv/bin/python -m pytest -q tests/test_m01_table_context.py tests/test_m01_source_units.py tests/test_m01_evidence.py tests/test_mineru_content_contract.py tests/test_m01_source_organization.py tests/test_m01_normalization_state.py tests/test_m01_product_reuse.py
# 34 passed
.venv/bin/python regression_sets/m01_failures_20261116_01/dataset.py verify
```

新合同测试先复现失败，再覆盖明确/候选表头、合并层级、行头隔离、嵌套行网格、Markdown及两个真实丢失案例。未扩大全量评测、随机种子或并发压测。详细新旧业务结果见 `analysis/m01_table_headers_20261116_01/report.md`。

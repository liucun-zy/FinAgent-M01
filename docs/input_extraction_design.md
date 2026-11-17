# M01：证据接入、来源提取与初始台账

## M01 v1 开发基线冻结

本轮收尾只将表题／附件引用的上下文关联修复接入正式来源管线，并完成版本化封装。规范化版本为 `mineru-content-contract-v5-table-title-scope`，来源单元版本为 `source-elements-v5-table-title-scope`；远端解析缓存、模型、并发、提示词、单位校验合同与 SQLite schema 保持现状。基线范围、验证及已知边界见 [M01 v1 基线](m01_v1_baseline.md)。此前 CPU 单位／日期实验保留为研究结果，不作为本次正式基线已集成的收益。

## 2026-11-16 解析合同与身份规模修复

本地解析规范化使用 `mineru-content-contract-v2`，与 MinerU 远端任务/cache profile 分离；升级本地适配不会重新提交远端任务。`save_parse` 按本地版本追加记录，提取 profile 同时绑定规范化版本，旧解析事件和来源 claim 不回写。适配字段、上游版本及未知内容处置见 [MinerU 输出合同](mineru_output_contract.md)。

页码、目录和已证实的跨页合并占位保留来源定位；脚注、页眉、图表文本和表格内容按实际结构读取。未知、不可读或损坏的独立元素保留原始条目与诊断，其可读同级内容继续提取。需审阅诊断进入 Arrival 和来源状态的未完成列表，不能将跳过元素标为完整完成或完整缓存复用。

身份投影使用 `source-identity-v5`：只有非空 ID 可以成为 ID 命中依据，候选由索引召回并排序，默认每个 mention 最多 12 个；扩大检索会实际扩大探测范围。确定性作用域标识与已确认别名单独处理，候选上限不能把身份歧义变成唯一绑定。身份装包仅展开本批焦点的一跳候选，并保留预算外身份与召回延续记录，详见 [身份规模说明](m01_identity_scaling.md)。本轮不改事实提取提示、单位校验、模型或身份请求预算。

## 2026-11-15 当前重构

当前生产入口采用来源单元事实合同、`source-elements-v1` 来源单元调度和 `source-identity-v4` 保存后投影。先按来源单元独立提取与保存，再解析实体身份；概念仅按角色和原字段名称确定性登记，提取前不再生成共享 schema。固定单元、任务及合同 profile、部分保存与预算边界见 [来源优先的产品管线](m01_product_pipeline.md)，配置见 `configs/m01.example.json`。

前一版 `shared-schema-source-observations-v4`、`structure-input-output-wire-v2` 与 `task-schema-identity-v2` 的实现及历史结果保留在 [开源基础重构](m01_open_source_refactor.md)。以下原设计段落作为背景；与当前入口不同的调度和合同细节以产品管线说明为准。

## 2026-11-14 原语义绑定设计背景

原 `source-scope-capacity-v3`（历史接入见 [容量接入](m01_m03_capacity.md)）在一次窗口提取中表达字段作用关系，沿用实体提及、来源 claim 和事件存储。以下保留其设计背景；当前原文、类型和语义组织职责以上节为准。

- `subject_local_id` 与 `value.local_entity_id` 保留主体、对象身份；`aliases` 只表达来源已确认的同一指代。新增 `qualifier_entity_refs=[{path,local_entity_id}]` 表达限定中的实体角色，保存时转换为 `qualifier_entity_links`。原 qualifier 文字保留，明确实体绑定优先于仅靠名称匹配的归一视图。
- `effective_time` 只表示断言的业务适用时间，未知为 null。`temporal.events=[{event,time}]` 单独记录报价、通知等业务事件的时间。日期型 `value` 是断言对象；来源 `document_date` 与 Arrival 的 `received_at` 仍分别保存在元数据中。读取代码不会在这几种角色之间回填。
- `field_evidence` 是可选辅助绑定，可关联当前窗口已提供的同来源证据，无需在 claim 主引用列表重复填写。合法引用纳入保存的证据列表，原 proposal 不变；无效绑定仅记录 warning 并忽略，不阻塞核心事实。`qualifier_entity_refs` 的无效相对路径或实体同样只影响辅助链接，原 qualifiers 保留。存在引用不证明语义正确。
- 来源 claim 保存 `semantic_contract_version`。历史记录按 legacy 读取，原来的 effective_time 不改写、不批量清空；新视图也不宣称旧时间解释已纠正。原值字段的缺省仍可见，完整原文可读不等于原值字段已提取。


### 独立 claim 保存与部分修正

`save_extraction` 沿用单一正式工具。逐实体和逐 claim 检查必需结构、主体、已提供的单来源引用和原有单位约束；某个实体或 claim 错误不拒绝其他独立事实。坏主体实体只使依赖它的 claim 失败。`raw_value.unit=null` 与 `temporal=null` 表达可选信息缺省，不再将其他事实挡在保存之外；业务值、单位和作用时间不做猜测修复。

每次保存的不可变 extraction 事件分别保留 `proposal`（原输入）、`result`（可用实体和 claim）、`save_receipt`。回执 `accepted_claim_indices` 为累计已保存原索引，`newly_accepted_claim_indices` 为本次新成功项，`rejected_claims` 含原索引与具体错误，`warnings` 只说明辅助信息问题。`committed=true` 表示本次审计及可用事实已经保存，不保证全部 claim 通过；`session_complete=false` 留给同会话失败项修正。无需为 warnings 增加一次调用。

后续可以只按失败索引顺序重交失败 claim（已接受的局部实体可以复用），也可按原顺序重发完整列表；成功索引不会重复保存。仅修部分失败项时，使用可选 `retry_claim_indices` 与提交的 claim 一一对应。相同 proposal 重复调用复用其原回执，不改写历史事件。

部分保存不把窗口标为全部 processed；只有该组没有剩余失败 claim 时才完成窗口。现有 Arrival 补处理会再次发现未完成窗口，已记录的父/子范围完成信息参与调度，不重新提取成功子范围；来源 claim 的原去重机制继续保留。旧失败 proposal 保留，最终 Arrival 状态只以各组最后一次失败范围报告未完成，不把已经修正的旧失败永远累加。

M02 的比较材料按 ID 读取真实绑定和时间角色。事项字段依据引用的独立来源也进入去重材料，但不自动成为当前 adopted 来源。主体、关系、对象、限定与原文仍分层，不将汇总事项的条件广播给各来源。

提取复用与解析缓存分开：正式 ingest 为静态生产提示、工具 schema、语义/规范版本及模型配置建立 `extraction_profile`；每组提取回执保存 profile，完成提取复用及已处理窗口筛选均要求 profile 一致。旧记录没有该 profile 时不能冒充新合同结果。新 profile 的相同来源重新提取时追加新的来源 claim，不改写旧记录。解析缓存仍使用原内容与解析配置身份；未改变解析时可以继续命中。模型密钥不参与可读 profile；相关模型参数只保存哈希和模型名称。

## 2026-11-14 系统重构补充

当前代码增加 `claim_view.py` 读取视图，直接映射既有字段为 subject/relation/object/qualifiers/valid_time/observed_at/evidence，未迁移或重写历史 claim。业务关系仍为开放字段，多方关系可以用事件主体及关联角色表达。

原始观察从 evidence 读取；可精确匹配的值文本仅保存其原文区间视图。历史 `raw_value` 明确标为模型对原写法的结构化解释，不能当作原字节；比较视图从既有提取回执取得 `extracted_value`，与机械规范转换后的 `value` 分开，不重复落库。旧记录没有保存转换前字段时保持未知，不从规范值倒造原值。

提取提示明确要求主体标识与对象/限定角色分开、显式等同名称作为别名、隐含单位与条件不冒充原文。未改原单位一致性校验、EFundGPT-air、M01 并发上限 20 或 DSH 预算实现。接口与模型语义效果分别验证，详见 [系统重构说明](m01_m03_refactor.md)。

实现日期：2026-11-14。按本轮最新选择复用 Efunds，提取模型为 `EFundGPT-air`，提取并发上限为 20。冻结归档、旧运行、旧评分均未修改。

## 入口与边界

```sh
cd <LOCAL_PROJECT_OR_FREEZE_PATH>

# 新批次：解析并保存来源 claim；不会自动建立或修改初始台账。
.venv/bin/python -m action_trace_demo.cli ingest \
  --workspace runs/m01_new_run \
  --manifest examples/m01/arrival.json \
  --config configs/m01.example.json

# 用首批 arrival_id 首次建账；已有台账则读取其初始视图。
.venv/bin/python -m action_trace_demo.cli build-ledger \
  --workspace runs/m01_new_run --arrival <arrival_id>

# 后到材料只保存增量，不替换已有台账。
.venv/bin/python -m action_trace_demo.cli ingest \
  --workspace runs/m01_new_run \
  --manifest examples/m01/later_arrival.json \
  --config configs/m01.example.json

# 显式补处理未保存的窗口，沿用原抵达事件、追加处理回执。
.venv/bin/python -m action_trace_demo.cli resume-ingestion \
  --workspace runs/m01_new_run --arrival <arrival_id> \
  --config configs/m01.example.json

.venv/bin/python -m action_trace_demo.cli ingestion-result \
  --workspace runs/m01_new_run --arrival <arrival_id>
```

新目录冷启动会消耗配置允许的解析与模型预算。已有验证的只读查看命令、真实输出和用量见 [验证报告](../analysis/m01_20261114/report.md)。`python -m action_trace_demo.ingestion` 也提供相同子命令。

`build-ledger` 已接入 [M02_初始台账](m02_initial_ledger.md)：可用 `--task-file` 提供显式任务说明，已有初始台账可省略 `--arrival` 读取，并导出任务卡、实体目录、事项表与来源展开。该组织步骤不增加模型或解析调用。

本模块不调用 `finish_stage`、工作项验收、文书签署、Judge 或冲突裁决。保存成功即结束 ingest 会话。后续批次产生 M02 增量，不按同名字段覆盖旧值。

## 配置与抵达批次

`configs/m01.example.json` 不含密钥。它引用已有 Efunds 私有配置，另以 `extraction_model` 选择 `EFundGPT-air`。运行目录的 `.private/model.local.json` 权限为 0600，仅供已有 ModelClient 使用。

MinerU 密钥池保存在工程外私密 JSON，结构为 `{"api_keys":["..."]}`，配置只记录文件路径。任务按预算槽轮转选择 key；创建失败不会自动换 key 重复提交。API 凭据只发送到官方 `https://mineru.net`。

批次 manifest 路径相对于 manifest 文件所在目录：

```json
{
  "task_context": "当前维护主题，可省略",
  "sources": [
    {"path": "report.pdf"},
    {"path": "records.csv", "source_id": "explicit-logical-source"}
  ]
}
```

可重复传入 `--manifest`，也可用 `{"arrivals":[批次1,批次2]}`。全部抵达先登记，再并发解析；提取、语义归并按抵达次序推进。来源可用 `url` 替代 `path`，后端先保留原始字节；无文件名的 URL 需提供带扩展名的 `display_name`。`source_id`、`document_date` 仅在已知时提供，不从文件名推断。

默认解析并发为 2，提取并发为 20；每组最多两次模型请求，批量补读最多一次。配置示例将整个工作区限制为 8 次实际传输尝试（包括重试）、3 个新 MinerU 任务。并发锁只覆盖预算预留与审计结算，网络请求可以重叠；崩溃后的未结算预留继续占用预算。原维护模式的预算路径保持不变。

## 解析与证据

| 格式 | 路由 | 本轮验证 |
|---|---|---|
| PDF | MinerU `vlm` | 真实托管返回与端到端提取 |
| XLSX | MinerU `vlm` | 真实托管返回与端到端提取 |
| HTML | MinerU `MinerU-HTML` | 真实 `main.html` 返回与端到端提取 |
| TXT、Markdown | 直接读原文 | TXT 端到端；Markdown 结构边界检查 |
| CSV、TSV、JSON | 直接读记录、字段 | 离线读取与引用定位检查 |
| PNG/JPG/JPEG/JP2/WEBP/GIF/BMP、DOC/DOCX/PPT/PPTX/XLS | MinerU 格式路由 | 未新增真实任务验证这些格式 |

原文件、结果 ZIP、原始展开内容、解析回执分别保留。解析缓存依据是内容 SHA256、实际解析配置和适配器版本。重复字节可以复用解析；已完成提取还须匹配本轮 extraction_profile，抵达事件仍独立登记。

本轮 XLSX 返回的是解析表格，没有原生工作表名、A1 坐标和公式。位置标为 `parsed_table`；不会把解析行列伪装成工作簿地址。PDF 使用实际页索引与可取得的区域，文本保留行/字符范围，HTML/JSON 保留元素或字段路径。表格单元保留独立引用；整行引用保留原解析 `<tr>`，可读拼接行不冒充原文。

块内容不随窗口变化。窗口携带标题、表头、说明及可观察的相邻关系；长文本按句子与精确区间拆分。上下文按实际证据 ID 去重，支持 neighbors、section、table_context、referenced。读取视图不证明任何业务结论已获完整支持。

首次下载在当前机器遇到 CDN DNS/TLS 路径问题，已按原任务 ID 恢复，未关闭证书校验。验证专用配置记录 `download_resolve`；通用示例没有固定 IP。失败任务可通过 `mineru_adapter.resume_parse(result_dir, parse_config)` 取回已有结果，不能通过重新创建付费任务掩盖失败。

## DSH 与数据合同

每个 WindowGroup 对应短 DSH 会话，只开放 `read_context`、`lookup_entities`、`save_extraction`。初始提示包含实际已提供的证据和实体候选，不发送全部历史 claim。模型只选择证据 ID；来源版本由程序从 claim 的引用推导。不同来源的相同事实分别保存，不把一个多来源引文自动拆成多个断言。

来源 claim 保留 statement、原值、规范值、限定条件和精确证据。明确货币量级由 Decimal 换算到基础货币单位，原值不变；未识别单位保持原样。实体值可引用局部实体 ID，由程序换成持久 ID。限定条件只承载业务含义，位置属于 evidence。

新对象复用 `temporal_dag.sqlite3` 中的追加事件和程序分配的 D 序号，使用 `m01_*` 事件类型。提取回执可以并发落库，实体与 claim 按窗口顺序归并。相同名称不直接合并实体；优先使用明确业务标识、已确认别名和名称/描述/上下文。

初始台账对等价的规范值、期间和限定条件合并展示，仍保留独立来源 claim；不同值并列。补处理产生新回执和创建记录，旧结果不回写。导出目录带 `at_<event_seq>`，后续导出不会覆盖早期快照。

### 别名、单位与提示约束修复

同一抵达先登记全部窗口组中的实体提及和别名，再生成来源 claim 的合并键。只有在实体目录中唯一对应同一对象的名称/别名才用于限定字段归一；重名、未知名称及不同的税口径、期间、范围仍保留区别。来源 `qualifiers` 保留原样，另外记录 `canonical_qualifiers` 和 `qualifier_entity_links`，用于说明哪些字段因何实体证据参与合并。不会回写已保存的来源记录或已建立的初始台账。

数值单位一致性检查在 `save_extraction` 落库前执行。有明确原始单位或可关联的证据单位时，检查币种、分母和量级转换是否相容；明确的单价不能只保留货币、丢掉“每台”等分母。发现矛盾时仅将相关 claim 记为 rejected，由模型在现有会话预算内修正；同组独立事实继续保存，程序不默补未知单位。该检查是有边界的确定性约束，不是对所有正文、单位和业务条件的语义证明。

生产提取提示已经采用对照实验中的补充约束：量级换算保留量纲、已确认别名一致表达、相同事项使用一致字段、限定条件仅附于适用断言，并保留不同来源的独立证据。不能通过删除条件换取更少的台账行。`completed` 仍表示流程提交完成；是否漏提、金额税口径和预计性质是否保留，需要与来源逐项检查。

修复的离线回归、真实模型重跑与保护目录核验记录见 [修复验证报告](../analysis/m01_fix_validation_20261114_01/report.md)。历史对照见 [根因分析](../analysis/m01_root_cause_compare_20261114_01/report.md)，旧实验保持原样。

输出包括：

- `entities.json`：实体目录与提及关联。
- `source_claims.json`：独立来源陈述、原值、规范值及创建记录。
- `initial_ledger.json`：初始台账与 assertion IDs。
- `evidence_refs.json`、`evidence_store.json`：原始内容、定位和完整处理记录。
- `m02_delta.json`：所选抵达的来源 claim 和实体关联，供 M02 使用。
- `result.json`：completed/partial/failed、未处理内容和用量。completed 只表示处理完成。

配置未核实 `EFundGPT-air` 的计费价格，本轮交付只报告 token 与请求数。ModelClient 原始审计保留继承配置下的预算估值，不作为新模型的实际费用。MinerU 报告任务数，不猜金额。

## 实现文件

- `mineru_adapter.py`：托管任务、密钥池、结果缓存、显式恢复。
- `evidence_windows.py`：实际返回适配、直接读取、证据块/引用、窗口和补读。
- `extraction.py`：提示、工具合同、单次落库结构处理、规范值。
- `unit_consistency.py`：保存前检查明确单位、量级和数值一致性；不确定关联不擅自推断。
- `ingestion_store.py`：原有 SQLite 中的 M01 事件、实体对齐、台账与增量。
- `ingestion.py`、`cli.py`：抵达调度、DSH 启动、预算配置和导出入口。
- `ingestion_tools.py`、`dsh_bridge.py` 与 `dsh_host/{plugin,runner,adapters}.mjs`：正式 ingest 工具链。
- `dsh_host/model_call.py`：仅 ingest 启用的并发预算预留。

本轮没有增加独立 Reviewer、小模型或新的模型调用框架。大量同构表的字段映射执行器未实现；当前小表走窗口联合提取，尚未取得需要该执行器的大表用例。

接口参考：[MinerU 托管 API](https://mineru.net/apiManage/docs)、[MinerU 输出说明](https://opendatalab.github.io/MinerU/reference/output_files/)。宿主继续使用本工程已锁定的 DSH 0.2.2-rc.1。

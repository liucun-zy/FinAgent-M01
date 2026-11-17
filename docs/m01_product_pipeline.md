# M01 来源优先的产品管线

M01 v1 已完成上下文关联接入并版本化封装；当前规范化／来源单元版本、验证与封装边界见 [M01 v1 基线](m01_v1_baseline.md)。以下保留来源优先管线的职责说明。

2026-11-16：当前身份投影已升级为 `source-identity-v5`，采用非空身份依据、索引候选和有界装包，见 [身份规模说明](m01_identity_scaling.md)。本地解析采用独立版本的 `mineru-content-contract-v2`，见 [MinerU 输出合同](mineru_output_contract.md)；不改变远端任务缓存键，未知或不可读元素的诊断随正式来源状态保留。提取提示与单元事实合同在本轮保持原样。

本版把“读原文、保存事实”和“共享身份、组织概念”拆成两个稳定接口。来源事实不再等待一次模型生成的任务 schema，也不读取台账赢家或历史摘要来补写原文。已有 SQLite、DSH、模型预算、原文定位和 claim 保存内核继续使用。

```text
解析缓存 → 来源单元 → 单来源紧凑包 → 来源事实提取
                                      ↓
                          定位、单位编译、独立部分保存
                                      ↓
                     已确认身份复用 + 有限实体身份解析
                                      ↓
                       版本化概念映射、来源投影 → M02
```

| 阶段 | 当前源码 | 责任与失败边界 |
| --- | --- | --- |
| 来源单元 | `source_units.py` | 正文按句/段，表格按行、记录按字段集合组织；保留标题、表头和必要邻文。一个包仅含一个 source version。模型看 U/C 等短地址；程序保留真实 ID、locator 和原文。超大原子行明确留待处理。 |
| 调度与复用 | `source_pipeline.py`、`ingestion.py` | 依据实际工具合同、输入包和输出容量组织任务。固定来源单元是完成与恢复边界；截断恢复在剩余总预算内执行。成功单元不随失败兄弟项重新提取。 |
| 来源合同 | `source_extraction.py`、`ingestion_tools.py` | 模型给来源主体、关系、逐字值短语、规范解释、单位和限定。程序分配局部 ID、查回短地址、定位原文并编译到现有保存接口；逐事实返回成功/失败，独立可用内容先保存。 |
| 保存后组织 | `semantic_schema.py`、`semantic_ingestion.py` | 先复用明确作用范围的标识和已确认别名；其余仅提交 E/H 实体身份组和逐字别名原句。字段概念按角色与来源名称确定性登记，模型不能重命名。有正式 schema 时显式导入；提取前模型调用为零。 |
| 版本投影 | `ingestion_store.py`、`semantic_schema.project_claim` | 原来源字段不回写；映射只作用到具体提及和字段出现。新概念同正式键可覆盖本批尚未映射的出现，不追溯重解历史原字段。批内实体归并不允许把两个已有历史实体整体合并。 |

当前读取投影为 `source-identity-v5`。`entity_type`、`identifier`、`predicate`、`qualifier` 分角色登记相同来源名称，不做自动语义重分类。不同写法诚实保留，由 M02 的结构与文本召回寻找可比较事项；来源的历史报价、执行值、性质、设备名和时间关系不会因目录只有少数概念被强制合并。旧分组合同仅保留历史读取/离线兼容，不是正常模型工具的可选分支。

身份工具只返回 `entity_groups` 和 `aliases`。别名 `source_text` 填完整逐字原句，由程序在可见原文中定位实际引用及区间。实体句柄、已有身份候选、冲突自身标识和别名引用在工具终结前检查；不合法提交得到现有 DSH 回执以便在两次请求内修正。合法绑定写入独立产物后再装载，避免“工具成功、后台静默丢弃引用”。身份是否相同仍是语义边界；相关、拥有、同产品和范围包含不构成身份等价。

组织请求按实际模型可见输入分包，超限组件和未返回内容显式保留为未定，不阻止已保存事实。`config.semantic` 的默认值为每包 `max_requests_per_batch=2`（初次提交及一次合同修正）、每 Arrival `max_transport_attempts_per_arrival=4`、`max_batches_per_arrival=4`。各次传输继续消耗现有 DSH 总预算和 M01 阶段预算；未引入新的模型客户端或 Reviewer。短地址译回长期 ID 后仍使用原有 semantic_resolution、task_schema 和 mapping_version 事件。

缓存边界由 source version、来源单元版本及 extraction profile 联合约束。profile 包含任务说明、模型设置、提示词、工具合同和单元/上下文配置的指纹。任务或合同改变后不能凭“之前处理过同一来源”复用旧提取；候选组织结果也不冒充新的来源提取。固定快照仍按现有事件截止点读取。

采用的成熟处理方式及范围如下，官方资料核查于 2026-11-15：

| 上游 | 借鉴与本地落点 |
| --- | --- |
| [Docling chunking](https://docling-project.github.io/docling/concepts/chunking/) | 区分文档单元和附带上下文，保留标题/表头，再按容量组织输入；用于 `source_units.py` 的元素与上下文边界。未引入 Docling 运行服务。 |
| [Unstructured chunking](https://docs.unstructured.io/open-source/core-functionality/chunking) | 优先保留文档元素完整性，超大元素再明确处理；用于句段、表格行、记录边界。未更换已有解析器。 |
| [LangExtract](https://github.com/google/langextract) | 原文短语与语义属性分开、结果定位回来源；用于精简的 `value_text` 合同和既有 `source_alignment.py`。当前仅依赖已锁定的定位组件，不调用上游提取/重试循环。 |
| [Graphiti resolve_extracted_nodes](https://github.com/getzep/graphiti/blob/3a8a78c997a07cceeb3411ede533b0f342d71c64/graphiti_core/utils/maintenance/node_operations.py) | 提取后收集候选，区分确定性复用和语义解析，返回 ID 映射；用于 `prepare_resolution`、`organization_plan` 和 `compile_organization`。不引入图数据库、向量服务或全局实体合并。 |

`session_complete`/单元完成只表示本次合同已提交，不能证明事实召回完整。逐字定位能检查输出来自哪里，不能证明主体、时间、否定范围和同角色概念等价判断一定正确。模糊来源、超大原子单元、预算耗尽和未定映射均可继续存在；这些边界通过回执和组织事件暴露。新管线没有自动证明跨领域稳定性；模型效果必须以冻结来源槽与维护意图的实际运行另行报告。

本地组织合同检查命令：

```bash
cd <LOCAL_PROJECT_OR_FREEZE_PATH>
.venv/bin/python -m pytest tests/test_m01_source_organization.py tests/test_m01_semantic_schema.py tests/test_m01_semantic_ingestion.py -q
```

该组 12 项检查通过，包含正式 DSH 的离线动作入口、来源投影、角色边界、新概念局部作用、历史身份、预算分包、固定快照、终结前错误反馈和原句别名定位。新增真实模型调用为 0；离线指定分组验证的是接口执行，不是模型识别准确率。

原冻结 `source-organization-v3` 的实际缺陷保留在 [独立设施/仓储核查](../analysis/m01_product_20261115_01/lab_heldout_review.md)：同角色概念的错误合并会损害原本正确事实，alias 的 R 引用错误在终结后才暴露。因此当前正常接口移除了模型概念重分类权限，并将实体引用编译前移到工具提交。原结果不回写；之后对相同来源的运行只能称为缺陷修复回归。

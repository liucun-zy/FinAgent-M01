# M01 v1 开发基线

基线标识：`m01-v1-context-20261116-01`。本轮开发完成并冻结，后续开发使用该版本化封装；本次不继续扩大 M01 优化范围。

正式封装目录：`<LOCAL_PROJECT_OR_FREEZE_PATH>`。

## 范围与入口

M01 负责抵达登记 → 格式路由／缓存解析 → 结构规范化与证据定位 → 表头、说明、续表及引用上下文关联 → 来源单元装包 → EFundGPT-air 提取 → 原文定位、校验与独立保存 → 有界实体身份整理 → 来源 claim 与 M02 增量导出。新旧事项候选配对、维护裁决和台账更新不属于 M01。

```bash
.venv/bin/action-trace ingest --workspace <新工作区> --manifest <来源清单.json> --config <私有工程配置.json>
.venv/bin/action-trace resume-ingestion --workspace <工作区> --arrival <Arrival_ID> --config <私有工程配置.json>
.venv/bin/action-trace ingestion-result --workspace <工作区> --arrival <Arrival_ID>
```

输出保持 `entities`、独立 `source_claims`、`evidence_refs`、`evidence_store`、`m02_delta` 及处理回执；原文、提取字段、规范值分别保留。已有初始台账的读取兼容接口保留，但 ingest 不决定维护动作。SQLite 继续追加事件，旧来源与旧快照不回写。

## 本轮正式接入

此前 HTML／Markdown 网格、多级表头、合并单元格、明确／候选表头、PaddleX 风格续表判定、AWS LINK 风格物理片段保留、说明作用域等实现继续使用。

本轮增加：解析器明确 caption 或紧邻的独立引号表题可作为表格的逻辑标题，即使其物理类型是普通 text 或单单元格表格。依据真实的完整表题引用，携带对应章节的审批／生效条件；不跨越正文找表题，不将无关父章节条件广播到表格。关联随逻辑续表传递，但不改写物理来源或自动填充事实语义。

- 本地规范化：`mineru-content-contract-v5-table-title-scope`。
- 来源单元：`source-elements-v5-table-title-scope`。
- 新版本进入 extraction profile；旧结果不冒充新合同结果，远端 MinerU profile／缓存不变。
- EFundGPT-air、提取并发 20、DSH、预算、提取提示词、身份框架及逐 claim 部分保存保持原样。

## 本次验证

采用上一轮运行前冻结的 16 个上下文用例与原预期：真实目标行由 5/6 恢复为 6/6，构造例 10/10。这是上下文可用性，不是模型提取准确率。

使用人工指定、原文可定位的一条 F023 事实，经过正式 save_extraction、merge、ingestion-result CLI，验证审批和生效字段引用确实指向新提供的条件原文；重复 merge 不重复保存。该诊断仅覆盖一个目标行，Arrival 保留 partial，未处理全文不冒充完成。

受影响回归 69 项通过，覆盖表格、来源单元、单位校验、来源合同、规范化版本和缓存。新增模型请求、token、MinerU 任务及 Judge 均为 0。

实际比较材料、提议、回执和导出见 `analysis/m01_v1_context_freeze_20261116_01/validation_02`。旧评测和 CPU 消融结果不修改；本轮结论写入新的验证目录。

## 已知能力边界与冻结口径

上下文进入请求不保证模型必然保留 Beta、拟议性质等限定；实体身份、任务过滤、输出截断及语义遗漏仍有已知限制。候选表头不作为已确认单位换算依据，审批说明不自动成为每份来源事实明确陈述的条件。

上一轮的通用单位／日期 CPU 优化仍是实验方案，不在本次“上下文关联接入”收尾范围内，不能将其组件成绩写成正式 M01 的完整提取成绩。局部字段 patch 同样不新增为正式模型合同。

封装包含源码快照、DSH 主机源码及锁文件、Python 构建产物、实际环境版本、必要回归及缓存样例、验证记录和 SHA-256 校验清单。工程共享代码随包保留以恢复运行依赖，不代表本次修改或发布 M02/M03。使用源码快照按 editable 方式安装，确保既有 DSH 项目相对路径有效；wheel 是 Python 构建产物，不是含 DSH 外部运行时的独立 SDK。

私有模型配置和 MinerU API 池继续使用本地已有配置，不写入封装。冻结包只读保存，任何后续 M01 变更应另建版本，不覆盖本基线。

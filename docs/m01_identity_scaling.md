# M01 身份候选与有界装包（source-identity-v5）

本次修复以失败 Mars 开场材料的真实已暂存提议为回放输入，同时约束一般缺失身份、大量同名实体、宽候选集合和预算不足的处理。不改来源事实、原值、条件和原文，不把候选关系当成身份确认，也不新增模型或状态机。

## 接口与身份依据

正式入口仍为 `semantic_ingestion.resolve_arrival`，内部使用 `prepare_resolution`、`identity_plan`、原有提交/映射/merge 内核。默认 `config.semantic.candidate_limit=12`；原身份预算仍为每 Arrival 最多 4 个批次及 4 次实际传输。

- 缺失、空白的内部 ID 和业务标识不产生命中；有效数值 `0` 仍是值。内部 ID 需非空相等，普通名字/别名/同句仅是候选依据。
- 类型与正式标识角色、明确唯一范围仍决定确定性复用。没有唯一范围的业务标识相等不自动确认身份；已确认别名可复用。确定性查找与有限候选列表分开，不能因为 top-k 只看见一个对象就忽略别处的身份歧义。
- `_CandidateIndex` 一次索引名称/别名、非空标识、原句引用和字符片段。弱词面召回先探测稀有片段，随后用真实匹配理由验证；排序依据是命中类别与稳定输入顺序，不输出匹配概率。
- 每个提及最多保留 `candidate_limit` 个候选。`candidate_search` 保存候选池大小、已验证探测数、`has_more`、`next_limit` 和是否查全。扩大 `candidate_limit` 会扩大探测范围并返回重新排序的更大列表；这是扩大检索，不是稳定分页。达到全候选池大小时查全，`has_more` 终止。
- 可通过 `prepare_resolution(..., candidate_limit=...)` 对相同可见输入扩大检索，或在后续运行设置相应配置。已记录的 semantic_resolution 保持原有幂等语义；本次没有增加自动后台重试/重处理状态机。

## 装包和未完成工作的表达

`organization_plan` 只展开当前 focus 的一跳候选；作为上下文出现的候选不再携带自身邻居列表。实体、证据按句柄去重。

每个独立 atom 的预算判断只做必要次数。单个 focus 加候选超预算时按排名缩小候选数量，不截断 focus 的原始证据，不删来源事实；当前 focus 证据本身仍超预算时明确留作 `entity` pending。保留候选未进入请求或检索未穷尽时记录 `candidate_page` continuation，含剩余已召回 target ID 和扩大检索所需 limit。它不是新事实，也不是“确定没有同一实体”。

运行入口把 `max_batches_per_arrival` 传给规划器。达到 4 个计划批次后，不再重复渲染后续实体，记录剩余 atom ID/offset 和原因。`pending_batches` 名称沿用旧存储字段，但其项可能为 `entity`、`field`、`batch` 或 `candidate_page`；应按 kind 统计，不能全部称为模型批次数。有 pending 的组织结果记 `partial`，即使没有额外未绑定计数。

身份工具仍只处理具体身份/别名，不修改字段含义。未定身份沿原有 `validate_resolution` 与 `merge_arrival` 保存为独立实体和来源事实，不能等待所有别名解决才让事实进入下游。

## 有限验证

```bash
cd <LOCAL_PROJECT_OR_FREEZE_PATH>
.venv/bin/python -m pytest tests/test_m01_identity_scaling.py tests/test_m01_semantic_schema.py tests/test_m01_source_organization.py tests/test_m01_semantic_ingestion.py -q
.venv/bin/python analysis/m01_repaired_20261116_01/identity_diagnostics/replay_opening.py
```

新回归先在旧实现下出现 2 个失败（空/空白身份伪命中、同名候选不受限），修复后包括既有正式离线 DSH 在内 16 项通过。有限覆盖身份 scope、已确认别名、未确认 existing ID、候选不确认归并、预算、非递归邻居装载、检索扩大与终止、固定快照和投影。

真实 OPENING 零模型回放含 1,102 条暂存事件、1,304 个提及：候选边从原错误的 1,699,112 条降到 9,529 条，最多 12 个/提及；候选准备、字段登记和 4 批装包共 0.287 秒。4 个请求估算分别为 7,686、7,752、7,927、7,774 token，均不超过 8,000。后续 1,241 个身份 atom 及 51 条候选延续记录诚实保留，不能将有限预算写成全身份已解决。具体运行环境和完整数值见 `analysis/m01_repaired_20261116_01/identity_diagnostics/opening_replay.json`。

另一份回放在历史 SQLite 的只读备份上让 4 个身份请求均返回 unavailable，实际模型调用为 0。`resolve_arrival → merge_arrival` 仍发布了 1,118 条正式来源 claim，身份状态为 partial（1,291 个未定）；resolve 0.429 秒、merge 0.281 秒。历史 DB hash 未改变。脚本及结果为 `identity_diagnostics/replay_merge.py`、`merge_replay.json`，脚本故意拒绝覆盖既有副本。

这两次是原提议的结构/执行回放，不是新模型准确率。候选排名不保证穷尽全部同义表达；高频同名、跨来源代词和别名语义仍需要模型或后续更大范围检索判断。真正的来源质量和跨 Arrival 身份覆盖由本轮重新冻结后的完整评测单独报告。

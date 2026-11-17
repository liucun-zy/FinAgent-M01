# FinAgent M01

M01 v1 开发基线：证据接入、文档结构与上下文组织、来源事实提取及证据保存。

基线标识：`m01-v1-context-20261116-01`。本仓库来自已冻结源码，M01 生产代码保持逐文件一致。

## 处理链路

材料抵达登记 → 格式路由与缓存解析 → 文档／表格规范化 → 表头、说明、续表及附件引用关联 → 来源单元装包 → EFundGPT-air 提取 → 原文定位与逐 claim 校验／部分保存 → 有界实体身份整理 → 来源 claim 和 M02 增量导出。

- 兼容 HTML／Markdown 表格、多级表头、合并单元格和明确／候选表头。
- 跨页表格采用逻辑 LINK，保留真实片段、原文、页码和坐标。
- 表前／表后说明、行组说明与附件治理条件按作用范围关联；仅提供上下文，不自动填充 claim 语义。
- 普通文本或单单元格表格中的明确表题，也可关联被真实引用的条件章节。
- 原文、模型提取字段、程序规范值和业务时间分开保存，已有 SQLite 事件及快照不回写。
- 模型采用 EFundGPT-air，提取并发上限 20，沿用既有 DSH 和请求预算。

M01 不执行台账维护裁决。包内保留现有工程共享代码以满足运行依赖，不代表本次发布 M02/M03。

## 安装

需要 Python 3.12+、Node.js 和 pnpm。在线调用还需要自己的 Efunds 与 MinerU 访问凭据。

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.lock.txt
uv pip install --python .venv/bin/python --no-deps -e .
pnpm --dir dsh_host install --frozen-lockfile
```

须使用源码目录的 editable 安装，以保留既有 DSH 的项目相对路径。

## 配置与运行

```bash
cp configs/model.example.json configs/model.local.json
cp configs/mineru.example.json configs/mineru.local.json
export FINAGENT_API_KEY="your-own-efunds-key"
```

在本地的 `configs/mineru.local.json` 填入自己的 MinerU API 池；必要时调整模型端点。示例配置限制模型调用与 MinerU 新任务数，按自己的任务预算调整。

```bash
.venv/bin/action-trace ingest --workspace runs/demo --manifest examples/m01/arrival.json --config configs/m01.example.json
.venv/bin/action-trace ingestion-result --workspace runs/demo
.venv/bin/action-trace resume-ingestion --workspace runs/demo --arrival <ARRIVAL_ID> --config configs/m01.example.json
```

`examples/m01` 是人工构造的采购与边界演示材料，其中 parse_samples 是这些合成材料的缓存解析输出。

## 验证与准确率边界

```bash
.venv/bin/python tools/verify_baseline.py
.venv/bin/python -m pytest -q
```

本仓库 **60 项合成／离线测试已通过**，不调用在线模型。这里只发布可以随代码共享的合成／离线测试。真实文档缓存检查及原始业务材料留在本地；本地冻结版的 69 项回归与此处测试的分母不同，详见 [验证范围](docs/validation_scope.md)。

本地上下文检查为真实目标行 6/6、构造上下文 10/10；另验证一条人工指定事实经过正式保存、引用绑定、重复归并和 CLI 读取。这些不是模型提取准确率。

**尚未进行新版对完整 45 例失败集的全量严格模型重测，不能宣称“失败集全部通过”。** 上下文可用不保证模型完整保留所有实体、时间和限定。

新 CPU 单位／日期适配方案和稀疏字段 patch 仍是独立实验，未纳入本次冻结的生产合同。

## 发布范围

源码、文档、合成示例、依赖锁文件和摘要验证记录已纳入仓库。私有密钥、真实失败集、原始业务文档、SQLite 和历史会话未上传。原始本地冻结包不变。具体生产文件指纹见 `M01_BASELINE.json`。

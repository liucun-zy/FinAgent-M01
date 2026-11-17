# MinerU 输出合同与本地适配（2026-11-16）

本地版本 `mineru-content-contract-v2` 与 MinerU 远端下载缓存版本分开。正式接口仍是 `normalize_parse_result(artifact) -> blocks`；读取已经保存的文件，不联网，不重新解析原 PDF，不从 gold 补写内容。原始文件和原始 JSON 保留，来源读取继续用 block/cell ID。

**研究范围。** 官方托管 API 文档把 `layout.json` 定义为 middle 输出、`*_content_list.json` 为内容列表，并单列 MinerU-HTML 的 `main.html`。对照了 3.1.0 的发布文档以及当前官方仓库的内容渲染实现。3.x V2 文档明确为可能变化的结构；4.x 已采用 DocVortex 中间协议。不能用模型名 `vlm` 或生产版本号判断 JSON schema，也不能承诺未来类型永久穷尽。

固定证据：

- [官方托管 API 输出说明](https://mineru.net/doc/docs/index_en/)。
- [3.1.0 输出文档，commit d9cd58a](https://github.com/opendatalab/MinerU/blob/d9cd58add047c2364c1198eefcb1ee9cd63a971a/docs/en/reference/output_files.md)。
- [当前官方输出合同，commit f0497e0](https://github.com/opendatalab/MinerU/blob/f0497e0c6e9575fa553ce57cc9aa867906a00572/docs/en/reference/output_files.md)。
- [当前 V1 渲染源码](https://github.com/opendatalab/MinerU/blob/f0497e0c6e9575fa553ce57cc9aa867906a00572/mineru/render/_internal/content_list/v1.py)、[V2 渲染源码](https://github.com/opendatalab/MinerU/blob/f0497e0c6e9575fa553ce57cc9aa867906a00572/mineru/render/_internal/content_list/v2.py)、[共用行内和视觉内容处理](https://github.com/opendatalab/MinerU/blob/f0497e0c6e9575fa553ce57cc9aa867906a00572/mineru/render/_internal/content_list/common.py)。

检索副本、commit、下载状态保存在 `analysis/m01_repaired_20261116_01/research/mineru/`。下面是本项目独立实现的消费合同摘要，具体可选字段以固定的官方实现及来源 JSON 为准。

| 表示层 | 实际结构 | 本地选择及边界 |
|---|---|---|
| V1 内容列表 | 平铺 `list[dict]`，`type`、可选 `page_idx/bbox/anchor/sub_type`，各类型专用字段 | 优先使用；支持带原文件前缀或 `content_list.json` |
| V2 内容列表 | 按页 `list[list[dict]]`，每项 `type + content`，bbox 可选，行内内容为 span 数组 | V1 不存在时使用；外层页索引保留，不将每页当单个事实 |
| legacy middle/layout | `pdf_info` → `para_blocks`、`discarded_blocks` → `blocks/lines/spans` | 没有内容列表时只用后处理块；`preproc_blocks`仅用于确认跨页表格延续，不能混入重复正文 |
| current middle | `middle_json.json`，`schema=docvortex.middle`、`schema_version=2.0`、`pages[].blocks`，视觉主体在 `children[].content` | 按 schema 身份读取；保留现有页索引、正文、表格主体和注释 |
| structured content | `structured_content.json`，`pages[].blocks`，自然语言 content 为字符串，**无 schema 标识** | 独立消费格式；不冒充 middle2.0，不凭 producer.version 选择分支 |
| raw model | 历史 `*_model.json` 为推理结果，坐标和层次不同；4.x 不作为消费者结果提供 | 不将其自动混作后处理正文或缺失事实补丁 |
| MinerU-HTML | `main.html`，DOM 文本、标题、列表、表格、图片等 | 保留元素路径、表格行/单元；图片待视觉读取，坏表只影响本元素 |

**类型与字段适配。** 下表涵盖官方 V1/V2 文档和固定渲染源码中的消费类型；middle 的 `*_body/*_caption/*_footnote` 是父视觉块的子结构，不能与平铺 V1 类型混为一层。

| 类型族 | 字段/变体 | 处理 |
|---|---|---|
| 正文 | V1 `text`；V2 `paragraph.content.paragraph_content` | 按精确文本拆块，保留原 JSON 指针；不读取 bbox/路径/布尔元数据作为正文 |
| 标题 | V1 `text_level`；V2 `title_content/level`；middle `doc_title/paragraph_title` | 建立有依据的标题上下文；无效 level 降为正文并诊断 |
| 页辅助 | `header/footer/page_header/page_footer/aside_text/page_aside_text/page_footnote/ref_text/phonetic` | 保留文本与角色。页眉也可能是联系人，脚注也可能是实质条件，不能统一删除 |
| 页码、目录 | `page_number`；`index.list_items` | 作为可追溯导航元数据保留，`extraction_eligible=false`，不变成业务事实或邻接事实上下文 |
| 列表与参考文献 | V1 `list_items/sub_type`；V2 `list_type/list_items[].item_content` | 逐项保留；参考文献角色不等于无用内容 |
| 公式 | `equation.text/text_format`；`equation_interline.math_content/math_type`；middle `interline_equation` | 识别文字/LaTeX 为公式证据；无可读内容则保留待处理，不能从图片路径猜公式 |
| 表格 | V1 `table_body/table_caption/table_footnote/img_path`；V2 `html/table_type/table_nest_level/image_source` | HTML 行与独立单元引用、标题/脚注上下文；结构坏时保留可读单元文本与原始条目并标待处理 |
| 图像、图表、印章 | `image/chart/seal`，`content`、`*_caption/*_footnote`、`img_path/image_source`、可选 `sub_type` | 图表中的 HTML 走表格接口；已有可读内容保留；纯图或内嵌图不伪装为读完，保留资产路径和待视觉读取诊断 |
| 代码、算法 | V1 `code.code_body/code_caption/sub_type/language`；V2 `code_content/code_language/code_footnote` 或 `algorithm_content/*_caption/*_footnote` | 保留文本与角色，不执行代码，注释仍独立保留 |
| future/unknown | 尚未映射的 type、可读 `text/content`、额外字段 | 可读文本仍可进入提取，完整原条目保留，强制 review；无可读内容只保留未处理项，不输出 JSON 数字当事实 |

V2 span 的 text/content、行内公式、代码和链接显示文字均按已提供文字读取。布局坐标、anchor、sub_type、语言、表格嵌套字段留在 locator 或原条目中；不要求把所有技术字段塞入模型业务提示。

**跨页空表不是自动失败。** 本批真实的空 `table_body` 多数是合并后的延续占位：同页同区域的 middle `para_blocks` 含 `lines_deleted=true`，相应 preproc 单元内容已经位于前面的合并表格。只有同时满足这些证据才建立 `merged_into_content_index`，不再次提取该页旧片段。主表记录全部 `page_indices/merged_segments`，`page_index_role=merged_table_anchor`，不把合并表的锚点页伪装成每一行的确切原始物理页。无法确认时保留原条目并报 `unreadable_table`。原 model/preproc 内容不用于不加区分地复制补表。

**失败隔离与状态。** 单元素结构错误留下 `malformed_element`，相邻正文继续可读。只有无法确定文档级结果文件或整个 JSON 根结构不可读才失败整个来源。多个同类内容文件有歧义时显式失败，不能任意拼接。未知字段通过 `raw_entry_ref` 指向原文件；未知或失读条目另外完整保留 `raw_entry`。

`normalization_diagnostics(blocks)` 返回 `code/message/severity/requires_review/block_id/locator/raw_entry_ref`。正常空白文本和已证实的表格延续是 info；未知类型、不可读表/公式、纯视觉内容、坏元素是 review。保存层将 review 传播为来源未完成，不能因其他文本已提取就标记整份文档完整。解析接入成功、模型输出、正式事实保存和语义正确是四个不同维度。

**验证边界。** 通过正式 normalize 接口对 87 份已有缓存做零模型回放，并保留少量合同回归：辅助文本/导航、V2 span/图表、未知及坏元素隔离、跨页去重、current middle/structured 子表体。新增 MinerU 和业务模型调用均为 0。可用块恢复不保证 OCR/表格识别本身正确，纯视觉区仍需要后续读取；全量正确率由本轮冻结来源 gold 的真实 M01 运行另行统计。

# Stage 1 选择性视觉证据流程

Stage 1 是 full-text-first 的学术论文阅读器。MinerU normalized full text、结构化页
元数据、图注、blocks、page index 和 OCR 是主要证据；Vision 只作为布局、对象边界或
视觉符号无法由纯文本可靠恢复时的确定性、受限补充。

正常路径：

```text
PDF / Zotero attachment
  -> canonical attachment resolution
  -> MinerU / preprocess
  -> normalized full text + structured page metadata + OCR/captions/blocks
  -> deterministic selective visual gate
  -> 只选择真正需要的 figure/table/formula/page evidence units
  -> ONE paper-level Stage 1 synthesis
  -> summary_v2_lite
```

## Normal、escalation 与 exceptional

对文字层可靠的现代数字版 PDF，Stage 1 只做一次 synthesis provider call：可以不带
图片，也可以带少量确定性选出的视觉对象。30 页论文不会因为有 30 页就产生 30 个
visual calls。

selector 在任何视觉 provider call 之前执行，只使用已持久化的 preprocess evidence：
`image_count`、figure/table/formula caption、framework/model/mechanism/workflow 词、
nearby text、block 几何、OCR 使用/冲突、text-layer 质量和 scanned-page 指标。selector
本身不调用 Vision 来决定页面是否值得发送。

如果已选对象装不进一次请求，才产生
`stage1_visual_extract:<paper>:<batch>`，随后仍只有一次 synthesis。这是
selected-visual batching，不是 all-page batching。

只有 scan-heavy 或 OCR-poor 输入才能走 exceptional adaptive page path。确定性理由可以
是 `scanned_pdf`、`scanned_page_ratio`、`low_text_coverage`、
`low_text_layer_coverage` 或明确的 OCR 严重退化。显式设置
`Stage1_Visual.selection_mode=adaptive_page_scan` 可进行 operator escalation 或测试；
`render_all_nonblank_pages=true` 只作为该 exception 的兼容开关，不能作为生产默认。
adaptive page 调用会在 coverage 和 receipt 中记录升级理由。

## Selective Visual Gate 与 soft budget

当前默认：

```ini
[Stage1_Visual]
selection_mode = selective
render_all_nonblank_pages = false
page_snapshot_soft_max = 4
figure_crop_soft_max = 6
table_crop_soft_max = 6
formula_crop_soft_max = 4
selected_visual_soft_total = 10
selected_visual_hard_total = 16
```

page snapshot、figure crop、table crop、formula crop 是不同 evidence unit。同一个对象
默认不同时发送整页和 crop；除非页面整体布局、扫描质量、归因不确定或 crop 不完整，
否则优先 crop。

soft budget 只指导 optional selection；达到 10 个不能静默丢弃 required unit。required
视觉超过一次请求的 transport limit 时，进入 selected-object extraction batches。现有
image byte、base64 膨胀估算、single-image/request byte、frozen local bytes、atomic
group、Registry hash 等 transport hardening 全部保留。

## Completeness 与 provenance

当前选择性 coverage artifact 是 `stage1_visual_coverage/v2`，完整性绑定 required
visual units，而不是所有非空页面：

- `visual_selection_status`
- `required_visual_unit_count`
- `required_visual_unit_ids`
- `optional_visual_unit_ids`
- `selected_visual_unit_ids`
- `inspected_visual_unit_ids`
- `unresolved_visual_unit_ids`
- `visual_extraction_strategy`
- `evidence_coverage_status`

没有 required visual unit 的纯文本论文使用 `evidence_coverage_status=not_required`。
selected unit 只有在 synthesis 中实际发送，或被 selected visual extraction observation
成功表示时才算 inspected。required unit 缺失就是 `incomplete`，不能静默变成 complete。

selected-object observation 使用 `stage1_visual_evidence/v3` 和 active prompt
`stage1.visual_extract.system.v1`。旧的 page-attribution
`stage1_visual_observations/v2` 及其 prompt 保留给 adaptive page exception 和历史审计；
旧 all-page artifact 不能 masquerade 成新的 selective authority。

provider 生成的 summary 仍绑定 source PDF bytes、preprocess artifacts、Prompt/schema
identity、selected visual ID 的 page/bbox/image hash、Registry dependencies、expected-call
graph、provider receipts、receipt closure 和 typed Stage 1 reuse authority。selection
identity 变化会使 exact reuse 失效；PDF、MinerU、OCR、page index、structured JSON 和
preprocess 只有在 hash/Registry dependency closure 通过后才能复用。

## Output budget 与 retry 分类

视觉提取与论文 synthesis 使用独立的
`stage1_visual_scan_max_output_tokens` 和 `stage1_synthesis_max_output_tokens`。synthesis
默认给 `summary_v2_lite` 留出足够 headroom；最终有效预算会按已配置或已知 provider
上限 clamp，并由 `reviewctl doctor` 展示。

不同错误不共享“再调用一次”：

- 网络 transient、429、502、503、504 使用有限 transport retry；
- `finish_reason=length` 只在同一 primary route 上提升一次预算，仍失败则
  `STAGE1_SYNTHESIS_OUTPUT_BUDGET_EXHAUSTED` 并 fail closed，不用同一参数重试，也不在
  ceiling 后继续烧 backup；
- 简单 JSON 外壳问题可以本地恢复；
- schema semantic failure 只允许有界的 schema-aware recovery；
- Prompt/schema drift、非法 enum、确定性的参数/认证错误和其他不可重试 4xx 直接停止。

`evidence_kinds` 只在 `services/stage1_visual_schema.py` 定义一次；page prompt/validator
和 selected-object prompt/validator 都从该 authority 读取，visual ID 不能被误写为
evidence kind。

## MinerU result URL 安全边界

MinerU result 下载要求 HTTPS 和精确 hostname。当前安全默认包含已观察到的官方 host：
`mineru.oss-cn-shanghai.aliyuncs.com` 与 `cdn-mineru.openxlab.org.cn`。
`MINERU_ALLOWED_URL_HOSTS` 只允许增加精确 host；协议、路径、arbitrary URL 和 wildcard
都会被拒绝。`reviewctl doctor` 会在不联网的情况下显示 effective allowlist 并提示非法值。

## Transport、validation 与架构对比

Vision transport 继续使用 capability gate、base64/image byte hardening；synthesis 前冻结
本地图片字节，receipt 记录 request hash 与实际 image membership。Validation 仍然只使用
text/evidence 和结构化 visual observation/provenance，不重新调用 Vision。

历史默认：

```text
all nonblank pages -> page visual scan -> page observations
                   -> selected raw reinspection -> paper synthesis
```

当前默认：

```text
MinerU full text -> deterministic selective visual gate
                 -> optional selected-visual extraction batches
                 -> one paper synthesis
```

all-page inspection 仍是有证据理由的 exceptional fallback。这样恢复了项目原始目标——
以 normalized full text 为主体的学术论文阅读器——同时保留 provenance、Registry、receipt、
fail-closed validation、transport safety 和 resume/reuse 能力。

# Stage 1 Vision-First 流程

Stage 1 始终把 MinerU 或其他预处理器生成的 normalized full text 作为主证据，
再加入页级视觉证据。流程不会在模型看图之前拍脑袋判断哪张图重要。

## 全页覆盖

每个非空 PDF 页面都会生成可追踪的 `page_snapshot`。空白页只能以
`skipped_blank` 跳过；渲染失败必须记录为 `render_failed`。持久化的
`stage1_visual_coverage/v1` 会记录 PDF 总页数、非空页、渲染/扫描/跳过/失败数量、
页面状态、crop、batch、coverage status 和 omissions。

## 请求路径

- 短论文（不超过 `single_call_max_pages`）使用一次 synthesis 请求，包含 MinerU
  全文、页面元数据、每页标签/OCR、页面图片以及受限的 figure/table/formula crop。
- 长论文先按不超过 `visual_scan_batch_size` 个 page object 做视觉扫描。每张图片
  前紧邻一个文本标签，标签包含 `visual_id`、页码、bbox、artifact type、图注和
  附近 OCR/文本。扫描结果持久化为 `stage1_visual_observations/v1`。
- 最终 synthesis 接收完整 normalized text、全部扫描 observations、coverage report
  以及不超过 `final_image_refs_max` 张高价值 crop。二阶段选 crop 只决定最终复核，
  不决定哪些页面曾经被视觉模型看见。

## 页到 crop 的归因与 reuse 资格

第一遍扫描严格以页面为单位：长文覆盖只发送并观察 `page_snapshot`。观察结果
通过严格校验后，才允许从同页选择 `table_crop`、`figure_crop` 或
`formula_crop`。子 crop 必须携带 `source_page_visual_id`、
`source_observation_visual_id`、`post_scan_score` 和评分分量；没有已验证的
来源页 observation 的 crop 不得晋级到最终 synthesis。因此最终多模态请求中
会出现带页面标签的确切本地 crop 路径，但扫描预算仍按页面计算。

最终 reducer 会在 typed `visual_evidence_qualification` binding 中记录四个
相互独立的事实：

- `scan_coverage_status`：`complete`、`partial`、`failed`、`not_required`；
- `final_synthesis_modality`：`multimodal`、`text_only`、`pdf_plus_text`；
- `final_raw_visual_recheck_status`：`complete`、`partial`、
  `not_run_fallback`、`not_required`；
- `evidence_coverage_status`：`complete`、`degraded`、`incomplete`。

旧的多义 `coverage_status` 只保留为兼容别名。完整页扫描后如果最终走 backup，
仍保持 `scan_coverage_status=complete`，但记录
`final_synthesis_modality=text_only`、
`final_raw_visual_recheck_status=not_run_fallback`、
`evidence_coverage_status=degraded`，并要求人工复核。

当 `require_complete_visual_coverage=true` 时，exact reuse 必须重新验证
Registry record、内容 hash、JSON type/version 以及 coverage/observation 文件本身。
部分/失败扫描、必需页遗漏，以及 coverage 或 observation 被删除、篡改、失效，
都会阻断 reuse 并要求新的 provider 工作。设为 `false` 是显式的降级复用策略：
不会抹掉状态或人工复核标记，引用的产物仍必须通过完整性验证。

## Provider 与 fallback

DeepSeek Vision 遵循官方 OpenAI-compatible Chat Completions 格式：`text` block 和
含 base64 data URL 的 `image_url` block。Responses 使用 `input_text` 和
`input_image`。实验模型通过 capability 显式识别；普通 `deepseek-v4-flash` 是纯文本。

视觉调用失败时回退普通 Flash，并携带 MinerU 全文和已经成功的视觉 observation 文本。
fallback 会写入证据，不能标记为 multimodal success。

Validation 仍使用普通 `deepseek-v4-flash`，只接收 source chunk、OCR、图注和 observation
文本，不接收图片或 PDF 文件。原始 PDF 默认不作为文件附件发送。

## 官方限制

实现遵循[DeepSeek Vision 官方文档](https://api-docs.deepseek.com/zh-cn/guides/vision)：
inline 请求体 48 MiB、base64/URL 单图 32 MiB、图片 token 上限 384；请求包含 15 张及以上
图片时单边最长限制为 4096 px。

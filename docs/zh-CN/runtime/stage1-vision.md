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

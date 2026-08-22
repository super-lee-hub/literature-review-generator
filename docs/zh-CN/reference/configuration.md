# 配置参考

受版本控制的配置模板是[`config.ini.example`](../../../config.ini.example)。
`config.ini` 和 `.env` 属于本地配置或密钥，不得提交。当前配置 schema 版本为 4，
由 `config_loader.py` 读取并由 `services.settings` 校验。

## Reader 与 Validator 默认值

- `Primary_Reader_API.model = deepseek-v4-flash-vision-exp`：实验性、vision-first
  的 Stage 1 reader。
- `Backup_Reader_API.model = deepseek-v4-flash`：纯文本 Stage 1 fallback。
- `Validator_API.model = deepseek-v4-flash`：验证保持纯文本。
- Primary reader 可以携带图片；Validator 请求永远不能携带 `local_image_path`、
  `image_url`、`input_image` 或 PDF 文件内容。
- 用户已有自定义 reader model 时保留；迁移只改变旧版 DeepSeek 默认，或显式启用
  `vision_first` 的配置。
- `[Multimodal]` 只为一次迁移兼容读取并发出 warning，不再写入，也不再作为第二个
  API key authority。

## Stage 1 输入

`[Stage1_Input]` 关键默认值：

- `mode = vision_first`
- `send_extracted_text = true`
- `send_selected_visuals = true`
- `send_original_pdf = never`
- `image_transport = base64`
- `single_call_max_pages = 12`
- `visual_scan_batch_size = 10`
- `final_image_refs_max = 8`
- `require_complete_visual_coverage = true`
- `max_request_image_bytes = 36000000` 原始图片字节预算，为 base64 膨胀预留空间，
  低于 DeepSeek 官方 inline 请求体 48 MiB 限制。
- `max_single_image_bytes = 24000000` 原始单图字节预算，为 base64 膨胀预留空间，
  低于官方 base64/URL 单图 32 MiB 限制。

运行时会按 base64 膨胀后的估算值同时执行单图和单请求预算。视觉扫描会分别记录
planned、sent、omitted、observed visual ID；只有对每个实际发送图片恰好返回一个严格
schema observation 的批次才算有效。长文先扫描全部可发送的非空页，再依据 observations
选择最终 raw image；备用 reader 强制纯文本，并在 provider 证据中如实记录。

## Stage 1 视觉渲染

`[Stage1_Visual]` 默认把每个非空页面渲染到约 2200 px 长边。全页图使用 JPEG
质量 92；figure、table、formula crop 使用 PNG，bbox 默认外扩约 4%。发布前执行
像素和字节安全上限。每个 visual manifest 记录宽高、scale、estimated DPI、格式、
字节数和 SHA-256。

## 迁移与证据

model、capability、Prompt identity/hash、schema、预处理证据、visual manifest 或
visual coverage 任一变化都会使 Stage 1 reuse 失效。页面渲染缺失或失败会写入
`stage1_visual_coverage/v1`；当 `require_complete_visual_coverage=true` 时，摘要
必须带 `quality_audit.needs_manual_review=true`，不能静默声称视觉完整。Prompt 文件由
Registry 的 SHA-256 authority 绑定；JSON policy 损坏、hash 漂移或缺少 placeholder 都会
fail closed。

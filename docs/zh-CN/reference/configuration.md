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

当前 Stage 1 的布尔值和枚举值采用严格解析。布尔值允许
`true/false`、`1/0`、`yes/no`、`on/off`；枚举值为 `mode=vision_first`、
`image_transport=base64` 以及 `send_original_pdf=never|auto|always`。
`crop_padding_ratio` 必须是 `0` 到 `0.25`（含边界）的有限数值。未知拼写、
不支持的枚举、非有限浮点数和越界 padding 都会使配置校验失败，不会静默回退到默认值。

运行时会按 base64 膨胀后的估算值同时执行单图和单请求预算。视觉扫描会分别记录
planned、sent、omitted、observed visual ID；只有对每个实际发送图片恰好返回一个严格
schema observation 的批次才算有效。长文先扫描全部可发送的非空页，再依据 observations
选择最终 raw image；备用 reader 强制纯文本，并在 provider 证据中如实记录。

## Stage 1 配置 ownership 与迁移键

`Stage1_Input` 负责 provider-facing 的文本/PDF/图片传输、页面扫描 batch 与最终
引用预算，以及 `require_complete_visual_coverage` reuse 策略。
`mode=vision_first`、`image_transport=base64` 和
`Stage1_Visual.render_all_nonblank_pages=true` 是 invariant。历史键
`pdf_required_for_formal_precision`、`formal_precision_text_only_policy`、
`pdf_verifier_api` 仅用于迁移归一化，进入 typed current settings 前会被移除。

`Stage1_Visual` 负责渲染和 crop 形状：页面/crop 尺寸、像素和产物字节上限、格式、
JPEG 质量、padding，以及 table/formula crop 开关。当前传输预算统一留在
`Stage1_Input`。旧的 `max_visual_refs_per_paper`、`visual_artifact_dir` 和重复的
`Stage1_Visual.max_request_image_bytes` / `max_single_image_bytes` 已移除并拒绝，
不会被静默当作当前控制项。

`config.ini.example` 的默认值会与 `default_config_sections()` 做一致性检查（秘密
占位符除外），避免 GUI 默认值、示例文件和 runtime owner map 各自漂移。

## Stage 1 视觉渲染

`[Stage1_Visual]` 默认把每个非空页面渲染到约 2200 px 长边。全页图使用 JPEG
质量 92；figure、table、formula crop 使用 PNG，bbox 默认外扩约 4%。发布前执行
像素和字节安全上限。每个 visual manifest 记录宽高、scale、estimated DPI、格式、
字节数和 SHA-256。

## 迁移与证据

model、capability、Prompt identity/hash、schema、预处理证据、visual manifest 或
visual coverage 任一变化都会使 Stage 1 reuse 失效。页面渲染缺失或失败会写入
`stage1_visual_coverage/v1`；当 `require_complete_visual_coverage=true` 时，摘要
必须由 typed `visual_evidence_qualification` 验证为完整证据后才能 exact reuse。
`quality_audit.needs_manual_review=true` 只记录降级或不完整证据，不能授权 reuse。
显式设置 `require_complete_visual_coverage=false` 时，可以在验证 binding 完整性后复用
降级结果，但必须保留该状态。该开关只放宽最终 raw reinspection 完整性，不放宽页面渲染、
页面扫描、observation 完整性或 final transport omission 的 fail-closed 语义门槛。存在未解决
raw unit 时，authority 必须保留 degraded evidence，并将最终 raw recheck 保持为 partial
或 fallback，不能改写为 complete。当前持久化的 qualification JSON 使用严格类型解析；
布尔值、整数、数组或 omission 字段格式异常时，必须在任何宽松投影前阻断 reuse。Prompt
文件由 Registry 的 SHA-256 authority 绑定；JSON policy 损坏、hash 漂移或缺少 placeholder
都会 fail closed。仅当 binding 确实早于当前视觉标记、且不含任何当前视觉标记时，才允许因缺少
`visual_evidence_qualification` 走 legacy 兼容路径；从当前 authority 中删除或置空该
qualification 属于校验失败，绝不能降级为 legacy。

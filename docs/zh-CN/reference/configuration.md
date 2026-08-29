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

## Outline 角色化路由

`[OutlineModels]` 的每个语义角色都会解析到自己的 API section，由
`outline/provider_router.py` 中的 `OutlineProviderRouter` 执行：

| 角色     | 配置键                          | 说明         |
| ------ | ---------------------------- | ---------- |
| 关系裁决   | `relation_adjudicator_model` | 建议与候选生成不同  |
| 候选大纲生成 | `outline_model`              | 强推理模型      |
| 结构审查   | `structure_critic_model`     | 应与生成模型不同   |
| 覆盖度审查  | `coverage_critic_model`      | 应与生成模型不同   |
| 证据审查   | `evidence_critic_model`      | 应与生成模型不同   |
| 最终仲裁   | `arbitrator_model`           | 通常与生成模型相同  |

生成与仲裁共用同一模型是**刻意设计**：仲裁必须用产出候选的同一个推理模型去吸收 peer
critiques。因此只有「某个 critique 与候选生成撞成同一 provider」才算 self-review；
系统会明确报出该诊断，不会静默降级为单模型自审。

无法解析的角色不会被悄悄改指到 `Outline_API`——它们会被记录为诊断，节点取路由时
fail-closed 抛错。

当前发布的角色映射如下：

| Outline 角色 | API section | 模型 | 传输协议 / 网络归属 |
| --- | --- | --- | --- |
| 关系裁决 | `Free_Mode_API` | DeepSeek V4 Pro | DeepSeek Chat Completions，DeepSeek 官方 API |
| 候选大纲生成 | `Outline_API` | Claude Opus 5 | 原生 Anthropic Messages，经 `chat.178266.xyz`，第三方 gateway |
| 结构审查 | `Writer_API` | GPT-5.6-sol | OpenAI Responses 兼容协议，经 `ai.saigou.work`，第三方 gateway |
| 覆盖度审查 | `Free_Mode_API` | DeepSeek V4 Pro | DeepSeek Chat Completions，DeepSeek 官方 API |
| 证据审查 | `Writer_API` | GPT-5.6-sol | OpenAI Responses 兼容协议，经 `ai.saigou.work`，第三方 gateway |
| 最终仲裁 | `Outline_API` | Claude Opus 5 | 原生 Anthropic Messages，经 `chat.178266.xyz`，第三方 gateway |

端点/协议和模型品牌是两件事。请求发往 `chat.178266.xyz` 或 `ai.saigou.work`，
只能证明程序请求发给了第三方 gateway；项目不会因为返回的是 Claude 或 GPT 模型名，
就声称这是 Anthropic 或 OpenAI 官方直连。运行时只把 gateway host 和不含 secret 的
route fingerprint 写入 binding/receipt；凭据留在 `.env` 或本地安全凭据存储中。

## Anthropic Messages 传输

`endpoint_type` 当前支持 `chat_completions`、`responses` 与 `anthropic`。配置为
`anthropic` 时使用原生 Anthropic Messages 协议：

* 请求发往 `<api_base>/<anthropic_path>`，默认 `v1/messages`，可用 `anthropic_path` 覆盖；
* 鉴权使用 `x-api-key` 与 `anthropic-version` 头，而非 Bearer token；默认版本
  `2023-06-01`，可用 `anthropic_version` 覆盖；
* system prompt 位于顶层 `system` 字段，而不是 messages 中的一条 system 消息；
* 令牌上限是 `max_tokens`；开启 extended thinking 时 `max_tokens` 必须大于
  `thinking_budget_tokens`，构造请求时会自动抬高；
* 该协议没有 `response_format` 参数，请求 JSON 时改为向 system prompt 追加指令；
* 响应的 `content` 是 block 列表，只有 `type == "text"` 的块才作为回答内容。

对 Claude Opus 5，当前请求策略是 adaptive thinking，并使用
`output_config.effort` 控制深度；不会发送旧式 `enabled + budget_tokens` 组合。
`thinking_budget_tokens` 仅为仍要求手工 extended thinking 的旧版 Claude 保留。

Claude 模型名本身**不会**触发协议推断：同名模型既可能挂在 Anthropic 端点，也可能挂在
OpenAI 兼容的第三方网关，仅凭名字猜测会选错线格式。

## Stage 3 Review 与 Writer

“Stage 3 Review”和“Writer”是同一阶段里的两个层次，不是两个独立阶段。Stage 3
是综述生成工作流；`Writer_API` 是它调用的 provider/model，按已 adoption 的
Outline v3 section 各调用一次。Writer 产出结构化 section block 和 citation token，
随后 runtime 发布 canonical 的 `review_draft/v3`、`citation_manifest/v3` 和 DOCX，
并完成 Stage 3 provider receipt closure。`Validator_API` 只在显式请求 validation 阶段时
使用。

## 显式配置迁移

loader 采用 fail-closed，不会偷偷改写旧配置。对旧配置执行显式迁移时，会在同目录原子
替换并先写备份：

```bash
python -m reviewctl config-migrate --config config.ini
```

可先用 `--dry-run` 查看报告。无法明确归属的旧 `[API_Parameters]` 键默认保存在标记过
的注释块中；只有显式加 `--drop-unknown-legacy` 才会丢弃。

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

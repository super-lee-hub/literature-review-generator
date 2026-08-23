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
  附近 OCR/文本。扫描结果持久化为 `stage1_visual_observations/v2`。
- 最终 synthesis 接收完整 normalized text、全部扫描 observations、coverage report
  以及不超过 `final_image_refs_max` 张高价值 crop。二阶段选 crop 只决定最终复核，
  不决定哪些页面曾经被视觉模型看见。

## 页到 crop 的归因与 reuse 资格

第一遍扫描严格以页面为单位：长文覆盖只发送并观察 `page_snapshot`。v2 Prompt
会接收同页 `figure_crop`、`table_crop`、`formula_crop` 的有界候选元数据，但
元数据只是候选，不是已经确认的观察。每个页面 observation 必须声明
`resolved`、`ambiguous` 或 `no_matching_candidate`。只有通过严格校验的
`raw_reinspection_candidates` 显式归因才能选择 child；child 不会仅因同页就继承
定量或关系证据。被选择的 child 带有 `source_page_visual_id`、
`source_observation_visual_id`、`object_attribution_*`、`post_scan_score` 和评分
分量。如果 ambiguous 集合超出 raw-image 预算，reducer 会保留整页 snapshot
作为安全回退。ambiguous 归因是原子的：要么保留完整候选 child 集合，要么保留
一张整页 snapshot，绝不保留部分 child。原子决策同时覆盖引用数量、编码后的请求
字节数、单图字节数、child 缺失/不可读、重叠、dedupe group 以及 transport 约束；
最终 coverage 和 provider transport metadata 会保存 group id、候选 id、resolution、
selected id、回退原因、transport status 和 child 完成标志。

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
Registry record、内容 hash、JSON type/version、v2 observation 的 Prompt/schema
identity 以及 coverage/observation 文件本身。旧 v1 observation contract 会被判为
失效，不会被静默重新解释。`validate_legacy_visual_observations_v1` 只允许作为旧
reader；当前 Stage 1 和 Registry 路径使用 `validate_current_visual_observations_v2`，
会拒绝 provider 返回的 v1。
部分/失败扫描、必需页遗漏，以及 coverage 或 observation 被删除、篡改、失效，
都会阻断 reuse 并要求新的 provider 工作。设为 `false` 是显式的降级复用策略：
不会抹掉状态或人工复核标记，引用的产物仍必须通过完整性验证。

## Provider 与 fallback

DeepSeek Vision 遵循官方 OpenAI-compatible Chat Completions 格式：`text` block 和
含 base64 data URL 的 `image_url` block。Responses 使用 `input_text` 和
`input_image`。实验模型通过 capability 显式识别；普通 `deepseek-v4-flash` 是纯文本。
配置中的 `deepseek-v4-flash-vision-exp` 已有公开文档/公开运行报告；普通 model-list
接口可能滞后。因此没有 key 只表示本环境没有独立验证 live availability，不表示把该
模型判定为不可用。

视觉调用失败时回退普通 Flash，并携带 MinerU 全文和已经成功的视觉 observation 文本。
fallback 会写入证据，不能标记为 multimodal success。

Validation 仍使用普通 `deepseek-v4-flash`，只接收 source chunk、OCR、图注和 observation
文本，不接收图片或 PDF 文件。原始 PDF 默认不作为文件附件发送。

## 严格配置值

当前 Stage 1 的布尔值、枚举值和 `crop_padding_ratio` 在配置校验、settings 归一化和
运行时 input 构建中共用同一个严格 parser。未知的布尔拼写、不支持的枚举值、非有限或
超出范围的 padding 都会 fail closed，不会静默变成默认值。布尔值允许
`true/false`、`1/0`、`yes/no`、`on/off`；当前枚举为 `mode=vision_first`、
`image_transport=base64` 以及 `send_original_pdf=never|auto|always`。

## 官方限制

实现遵循[DeepSeek Vision 官方文档](https://api-docs.deepseek.com/zh-cn/guides/vision)：
inline 请求体 48 MiB、base64/URL 单图 32 MiB、图片 token 上限 384；请求包含 15 张及以上
图片时单边最长限制为 4096 px。

## 显式 live smoke

可选 live smoke 会在临时目录生成一页包含小表格和框架图的合成 PDF，
然后检查精确实验模型 ID、图片 payload、JSON 输出、usage 和 provider receipt。
只有在明确允许外部网络调用且配置凭据时才运行：

```powershell
$env:AUTO_GENERATE_RUN_LIVE_API = "1"
python -m pytest -q tests/live/test_deepseek_vision_smoke.py -m live_api
```

未显式 opt-in 时，结果必须是
`LIVE_DEEPSEEK_VISION_SMOKE=NOT_RUN_LIVE_API_NOT_ENABLED`；没有 DeepSeek 专用 key
时是 `LIVE_DEEPSEEK_VISION_SMOKE=NOT_RUN_NO_DEEPSEEK_KEY`。smoke 只接受
`DEEPSEEK_API_KEY` 或 `AUTO_GENERATE_DEEPSEEK_API_KEY`，不会把通用 live key 或
`OPENAI_API_KEY` 发往 DeepSeek endpoint。离线和 mocked 测试不能替代 live 证据。
测试不会把 key、响应正文或合成 PDF 写入仓库。

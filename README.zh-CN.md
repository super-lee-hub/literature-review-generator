# auto-generate 中文用户指南

> 一个本地运行、语料可控、全文优先、可追踪产物链的 AI 文献分析与综述写作工作台。

这份文档面向普通用户和研究者，重点回答三个问题：

1. 这个项目为什么值得用？
2. 第一次如何用 GUI 或 CLI 跑起来？
3. 常用命令、接口、输入模式、输出产物分别是什么？

如果你是 AI agent、开发者或维护者，请从 [AGENTS.md](./AGENTS.md) 开始；如果你要核对底层产物真相，请看 [TRUTH_SOURCES.md](./TRUTH_SOURCES.md) 和 [FEATURE_MATRIX.md](./FEATURE_MATRIX.md)。

## 1. 为什么不是又一个“AI 生成综述”工具？

很多 AI 文献综述工具的默认逻辑是：你给它一个主题，它自己去搜索、筛选、阅读，然后生成一篇看起来完整的综述。这个流程很省事，但对真正写论文的人来说有几个硬伤：

- **文献池是黑盒**：你不知道它为什么选这些论文，也不知道遗漏了哪些关键文献。
- **全文不可控**：人文社科论文、大量订阅数据库论文、你自己 Zotero 里的 PDF，很多时候并不在开放网络里。
- **证据链断裂**：从原始 PDF 到摘要、提纲、正文、引用，很多工具不给你可检查的中间产物。
- **模型和成本不可控**：订阅制工具通常限制上传篇数、批量规模、模型选择和调用方式；一次综述常常需要几十篇甚至上百篇文献，不适合被很小的上传额度卡住。
- **质量控制不足**：生成后的内容是否真的被原文支持，引用是否错配，通常需要人工重查。

`auto-generate` 的定位正好相反：它不替你黑盒决定“该读哪些论文”，而是让你把已经掌控的文献集交给 AI 做完整、可追踪的综述生产。

核心贡献可以概括为：

- **你控制文献范围**：用 PDF 文件夹或 Zotero report + library 明确指定本次综述的论文池。
- **尽量使用全文**：先做 PDF 预处理、OCR / MinerU / 本地解析，再把适合阶段一的全文材料交给 AI。
- **你控制模型和接口**：阅读、提纲、写作、自由规划、验证可以接不同 API 和不同模型。
- **本地工作区保存证据链**：每次任务都有 job workspace，保留 summaries、outline、review draft、citation manifest、DOCX、日志、验证和修复产物。
- **大批量更自然**：主要受你自己的本地资源、API 额度和模型上下文限制，而不是被平台上传文件数卡住。
- **用户和 AI 都能接入**：GUI 给小白用户，CLI 给重度用户，repo-local Codex / OMX skill 给 AI-native 自动执行。

## 2. 适合谁？

适合：

- 正在写学位论文、论文引言、理论综述、系统综述或研究背景的人
- 已经在 Zotero 中整理了文献库，并希望直接复用附件 PDF 的人
- 手头有一批 PDF，希望让 AI 逐篇分析再生成综述的人
- 想自己选择 API、模型和成本结构，而不是订阅黑盒平台的人
- 需要检查中间产物、复用历史摘要、局部重跑、验证引用的人

不适合：

- 只想输入一个主题，让平台全自动替你找文献、筛文献、写综述的人
- 完全不关心文献来源、全文质量和引用证据链的人
- 想要一个云端账号登录即可使用的在线 SaaS，而不是本地工具的人

## 3. 一张图理解工作流

```text
你的论文来源
├─ PDF 文件夹
└─ Zotero report + Zotero library

        ↓

PDF 预处理
├─ 本地解析 / OCR
├─ MinerU 远程解析（可选）
└─ normalized.md / page_index / diagnostics / cache

        ↓

阶段一：论文分析
└─ *_summaries.json + paper_artifacts

        ↓

阶段二：生成综述大纲
└─ *_literature_review_outline.md

        ↓

阶段三：生成综述正文
├─ review_draft.json（artifact_version=v3）
├─ citation_manifest_v3.json
└─ *_literature_review.docx

        ↓

可选：验证 / 修复
└─ validation_report / repair_plan / repair_apply_result
```

## 4. 三种入口：GUI、CLI、Codex skill

| 入口 | 适合谁 | 特点 |
| --- | --- | --- |
| GUI：`python launch_gui.py` | 第一次使用、希望界面化配置的人 | Setup / Workflow / Logs / Guide，Workflow 页面内置串行后台队列 |
| CLI：`python main.py ...` | 熟悉命令行、需要重复批处理的人 | 直接运行，不进入 GUI 队列，适合脚本化和精确控制 |
| Codex / OMX skill：`auto-generate-orchestrator` | 在 Codex 中让仓库自主执行的人 | AI-native 加法入口，复用同一套 workspace / artifact / validation 基座 |

这三条入口不是三套引擎。GUI 和 CLI 共享底层 job runner / workspace / artifact 逻辑；Codex skill 是第三条加法入口，不替代 GUI / CLI。

## 5. 安装与初始化

### 5.1 安装依赖

```bash
pip install -r requirements.txt
```

### 5.2 运行设置向导

```bash
python main.py --setup
```

### 5.3 启动 GUI

```bash
python launch_gui.py
```

开发调试时可用：

```bash
python launch_gui.py --reload --no-show
```

## 6. GUI 快速开始

推荐第一次使用走 GUI：

1. 运行 `python launch_gui.py`。
2. 在 Setup 页面配置输出目录、Zotero 路径、API、模型和预处理选项。
3. 在 Workflow 页面选择输入模式：
   - PDF folder：选择一个 PDF 文件夹。
   - Zotero：使用 `zotero_report` 和 `library_path`。
4. 点击主流程按钮：
   - Analyze only：只做阶段一论文分析。
   - Generate outline：生成大纲。
   - Generate review：生成综述正文。
   - Run all：一键跑完分析、大纲、正文。
5. 到 Logs 页面观察日志；输出通常在 `output/<project_name>__<job_id>/`。

GUI 的任务会进入 GUI 内部的持久化串行后台队列。提交一个任务后，表单仍可继续编辑，你可以准备下一项；CLI 和 Codex skill 不进入这个 GUI 队列。

## 7. CLI 快速开始

### 7.1 PDF 文件夹一键跑完

```bash
python main.py --pdf-folder "D:\papers" --project-name "my_review" --run-all
```

建议始终显式指定 `--project-name`，这样输出目录更稳定、后续重跑更好找。

### 7.2 分阶段运行

```bash
python main.py --pdf-folder "D:\papers" --project-name "my_review" --analyze-only
python main.py --project-name "my_review" --generate-outline
python main.py --project-name "my_review" --generate-review
python main.py --project-name "my_review" --validate-review
```

第一次调试建议先跑 `--analyze-only`，确认 PDF 能被正确解析、summary 能生成，再跑后续阶段。

## 8. 输入模式

### 8.1 PDF 文件夹模式

最直接的方式：把要纳入综述的 PDF 放在一个文件夹里。

```bash
python main.py --pdf-folder "D:\papers" --project-name "my_review" --analyze-only
python main.py --project-name "my_review" --generate-outline
python main.py --project-name "my_review" --generate-review
```

### 8.2 Zotero 模式

Zotero 模式适合已经用 Zotero 管理文献的人。你需要：

- `zotero_report`：Zotero 导出的报告文件
- `library_path`：Zotero storage / library 路径

可以写在 `config.ini`：

```ini
[Paths]
zotero_report = D:\zotero_report\Zotero 报告.txt
library_path = D:\zotero_library\Zotero\storage
```

也可以命令行直接传入：

```bash
python main.py --project-name "my_review" --zotero-report "D:\zotero_report.txt" --library-path "D:\ZoteroLibrary" --analyze-only
python main.py --project-name "my_review" --generate-outline
python main.py --project-name "my_review" --generate-review
```

## 9. 常用命令速查

| 命令 | 用途 |
| --- | --- |
| `python main.py --setup` | 运行交互式设置向导 |
| `python launch_gui.py` | 启动本地 GUI |
| `--pdf-folder "D:\papers"` | 指定 PDF 文件夹 |
| `--project-name "my_review"` | 指定项目名和输出工作区标识 |
| `--run-all` / `-a` | 一键运行：分析 -> 大纲 -> 综述 |
| `--analyze-only` / `-A` | 只运行阶段一：论文分析 |
| `--generate-outline` / `-o` | 只运行阶段二：生成大纲 |
| `--generate-review` / `-r` | 只运行阶段三：生成综述 |
| `--validate-review` / `-v` | 验证已生成综述 |
| `--retry-failed` | 重试阶段一失败论文 |
| `--generate-section 3` | 只重做第 3 节综述 |
| `--retry-review-failed` | 重试失败或缺失的综述章节 |
| `--summary-file <path>` | 为大纲/正文/验证显式指定 summary 文件 |
| `--summary-source <path>` | 追加一个下游 summary 来源，可重复 |
| `--reuse-stage1` | 兼容保留；阶段一/一键执行默认已自动复用历史 summary |
| `--no-reuse-stage1` | 强制关闭阶段一历史 summary 自动复用 |
| `--reuse-summary-file <path>` | 追加一个阶段一复用池来源，可重复 |
| `--merge <path>` | 把另一个 summaries.json 合并进当前 summary |
| `--prime-with-folder <path>` + `--concept <name>` | 概念预热 / concept priming |
| `--free-mode-profile <json>` | 加载 free mode profile |
| `--free-mode-idea <text>` | 直接传入 free mode idea |
| `--outline-adopt` | 显式采纳大纲仲裁结果；兼容/手动路径，不是默认主链 |
| `--cleanup` | 清理旧工作空间，只保留最新的 |

完整参数可运行：

```bash
python main.py --help
```

## 10. 摘要复用、合并与局部重跑

### 10.1 复用历史阶段一摘要

当你新增少量论文、重跑类似主题，或不想重复花钱分析已经处理过的 PDF，阶段一和一键执行会默认扫描历史输出并自动复用：

```bash
python main.py --pdf-folder "D:\new_papers" --project-name "my_review_v2" --analyze-only
```

也可以显式指定复用池：

```bash
python main.py --pdf-folder "D:\new_papers" --project-name "my_review_v2" --analyze-only --reuse-summary-file "D:\cache\curated_summaries.json"
```

如需强制全新重跑阶段一：

```bash
python main.py --pdf-folder "D:\new_papers" --project-name "my_review_v2" --analyze-only --no-reuse-stage1
```

当前复用逻辑会优先匹配：

1. DOI
2. canonical paper key
3. `title + first author + year` 的唯一高置信命中

### 10.2 下游显式加载 summary

如果你已经有一个 `summaries.json`，可以跳过阶段一，直接生成大纲或正文：

```bash
python main.py --project-name "subset_outline" --summary-file "D:\subset\subset_summaries.json" --generate-outline

python main.py --project-name "subset_review" --summary-file "D:\subset\subset_a_summaries.json" --summary-source "D:\subset\subset_b_summaries.json" --generate-review
```

### 10.3 局部重跑

```bash
python main.py --project-name "my_review" --generate-section 3
python main.py --project-name "my_review" --retry-review-failed
python main.py --project-name "my_review" --retry-failed
```

## 11. PDF 预处理、MinerU 与 OCR

AI 直接读扫描件或复杂 PDF 往往会失败，因为它拿不到真正可用的文本。这个项目把 PDF 预处理放在阶段一前面：

- 本地解析 PDF 文本
- 必要时 OCR
- 可选 MinerU 远程解析
- 生成 `normalized.md`、`plain_text.txt`、`page_index.json`、`diagnostics.json`、`structured.json`
- 将更适合 AI 阅读的阶段一输入送入 Reader API

相关配置在 `config.ini` 的 `[Preprocess]`：

```ini
[Preprocess]
enabled = true
cache_dir = ./output/_preprocess_cache
parser_mode = hybrid
primary_parser = mineru_remote
fallback_parser = local
ocr_mode = auto
use_markdown_as_stage1_input = true
retain_page_index = true
retain_diagnostics = true
enable_local_rag = false
```

如果没有 MinerU token 或远程解析失败，系统可以按配置回退到本地解析。不同论文是否真的调用了 MinerU，请看对应 workspace 里的 preprocess diagnostics / manifest，而不是只看全局配置。

`MINERU_ALLOWED_URL_HOSTS` 仅用于信任 MinerU 配置源站之外的 HTTPS 上传、下载或存储地址。值必须是逗号分隔的精确主机名，不能包含协议、路径或通配符；除非服务商确实返回受信任的外部存储链接，否则保持为空。

## 12. API 与模型配置

建议把敏感 API key 放在 `.env`，把非敏感运行参数放在 `config.ini`。

`.env` 示例：

```text
LLM_PRIMARY_READER_API=your_primary_reader_key
LLM_BACKUP_READER_API=your_backup_reader_key
LLM_WRITER_API=your_writer_key
LLM_OUTLINE_API=your_outline_key
LLM_FREE_MODE_API=your_free_mode_key
LLM_VALIDATOR_API=your_validator_key
MINERU_API_TOKEN=your_mineru_token
MINERU_ALLOWED_URL_HOSTS=
```

主要 API 角色：

| 配置段 | 角色 |
| --- | --- |
| `[Primary_Reader_API]` | 阶段一主阅读模型 |
| `[Backup_Reader_API]` | 阶段一备用阅读模型，适合长文/失败回退 |
| `[Outline_API]` | 阶段二大纲生成 |
| `[Writer_API]` | 阶段三正文写作 |
| `[Free_Mode_API]` | free mode / idea planning |
| `[Validator_API]` | 综述验证 |

每个 API 段可设置 `model`、`api_base`、`proxy_mode` 等。`proxy_mode = environment` 表示跟随系统代理环境变量；`proxy_mode = direct` 表示该 provider 绕过本地代理。

## 13. 输出目录与关键产物

当前真实输出通常在：

```text
output/<project_name>__<job_id>/
```

典型结构：

```text
output/<project_name>__<job_id>/
├─ artifacts/
│  ├─ <project>_summaries.json
│  ├─ <project>_summary_source_manifest.json
│  ├─ <project>_summary_reuse_report.json
│  ├─ <project>_literature_review_outline.md
│  ├─ paper_artifacts/
│  ├─ review_drafts/
│  ├─ citation_manifests/
│  └─ validation / repair 相关 JSON
├─ checkpoints/
├─ logs/
├─ reports/
└─ artifact_registry.json
```

常见导出物：

- `reports/*_analyzed_papers.xlsx`
- `reports/*_literature_review.docx`
- `reports/*_failed_papers_report.txt`
- `artifacts/review_drafts/*_review_draft.json`（artifact_version=v3）
- `artifacts/citation_manifests/*_citation_manifest_v3.json`

兼容目录：

```text
output/<project_name>/
```

这个目录现在通常只保留 `_latest_job.json` 之类的指针，不要优先把它当成真实产物主目录。

## 14. 验证与修复

生成综述后，可以运行：

```bash
python main.py --project-name "my_review" --validate-review
```

验证/修复管线会围绕 review draft、citation manifest、preprocess evidence 和 paper metadata 检查引用准确性、证据支撑和潜在漂移。启用时可能产生：

- `validation_report.json`
- `repair_plan.json`
- `repair_apply_result.json`
- `applied_patch_*.json`

这不是“魔法保证 100% 正确”，但它比只拿一个 DOCX 结果要更适合追查问题。

## 15. Codex / OMX skill 入口

如果你在 Codex 或 OMX 中直接操作这个仓库，可以使用 repo-local skill：

```text
auto-generate-orchestrator
```

它会把 AI-native 请求归一化成项目自己的 runtime job spec，并复用同一套 workspace / artifact / validation 基座。使用该入口时，工作区里还可能出现：

- `artifacts/source_bundle.json`
- `artifacts/runtime_stage_trace.json`

普通用户优先掌握 GUI 和 CLI；只有当你在 Codex 里让仓库自主执行时，才需要关心这个入口。

## 16. 排障快捷表

| 问题 | 优先检查 |
| --- | --- |
| 第一次不知道怎么跑 | 用 GUI，或 CLI 先跑 `--analyze-only` |
| 找不到输出 | 看 `output/<project_name>__<job_id>/` |
| PDF 似乎没读出来 | 看 preprocess cache / diagnostics |
| 想确认 MinerU 是否实际调用 | 看单篇 preprocess manifest / diagnostics，不只看全局配置 |
| 阶段一太慢或太贵 | 默认会自动复用历史 summary；额外复用池用 `--reuse-summary-file` |
| 只想修某一节 | 用 `--generate-section <n>` |
| 只想补失败章节 | 用 `--retry-review-failed` |
| 想验证生成结果 | 用 `--validate-review` |
| 想了解底层产物真相 | 看 [docs/zh-CN/runtime/](./docs/zh-CN/runtime/) |
| 想接手开发 | 看 [docs/zh-CN/developer/](./docs/zh-CN/developer/) 和 [docs/zh-CN/reference/](./docs/zh-CN/reference/) |

## 17. 文档分工

- [README.md](./README.md)：中英双语项目入口和导航
- [README.zh-CN.md](./README.zh-CN.md)：中文用户指南，也就是本文
- [README.en.md](./README.en.md)：英文用户指南
- [AGENTS.md](./AGENTS.md)：AI / 开发者接手入口（fat stub → docs/）
- [docs/zh-CN/](./docs/zh-CN/)：完整中文技术文档站（开发者、AI、运行时、参考）
- [docs/en/](./docs/en/)：完整英文技术文档站

## 18. 一句话总结

把 `auto-generate` 理解成一个“研究者自己掌控语料和模型的本地 AI 文献综述工作台”：它不是替你黑盒决定读什么，而是把你已经确定的 PDF / Zotero 全文集合，变成可检查、可复用、可验证的综述生产链。

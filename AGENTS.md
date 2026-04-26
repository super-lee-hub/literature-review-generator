# AGENTS.md

本文件用于让新的 AI 对话或新加入的开发者，在 3 到 5 分钟内快速理解 `D:/auto-generate` 这个仓库的当前现实。

- 推荐项目名：`auto-generate`
- 历史名称：`llm_reviewer_generator`
- 文档定位：AI 接手文档 / 维护者速览 / 架构与运行时真相入口
- 最近更新：`2026-04-22`

## 1. 先知道文档怎么分工

当前文档分工已经调整为：

- `README.md`
  - 项目首页 / 路由页
  - 不再承载全部细节
- `README.zh-CN.md`
  - 中文用户完整指南
- `README.en.md`
  - 英文用户完整指南
- `AGENTS.md`
  - 给 AI / 新维护者的仓库接手文档（就是这份）
- `TRUTH_SOURCES.md`
  - 运行时真相、durable artifacts、兼容路径说明
- `FEATURE_MATRIX.md`
  - 功能状态矩阵：implemented / partial / legacy / planned
- `ARCHITECTURE_BASELINE.md`
  - 迁移时期基线；有参考价值，但不是当前主真相

如果以后 README 再发生变化，优先保持上面这个分工，不要再让根 README 承担全部说明。

## 2. 一句话说明

这是一个本地运行的 AI 文献分析与综述写作工作台，支持：

- `PDF folder` 输入
- `Zotero report + Zotero library` 输入
- CLI、GUI 与 repo-local Codex skill 三入口
- 阶段一摘要 / 阶段二提纲 / 阶段三综述正文
- GUI 后台队列、断点恢复、历史摘要复用、预处理缓存、validation / repair、可选本地 RAG

它已经不应再被理解成“单个脚本的文献综述生成器”，而是一个带 workspace / artifact / GUI 后台队列的本地工作台。

## 3. 推荐阅读顺序

如果你是新的 AI 对话，建议按下面顺序建立上下文：

1. `AGENTS.md`
2. `TRUTH_SOURCES.md`
3. `FEATURE_MATRIX.md`
4. `summary_schema.py`
5. `services/job_runner.py`
6. `main.py`
7. `gui/app.py`
8. `.codex/skills/auto-generate-orchestrator/SKILL.md`
9. `runtime/orchestrator.py`
10. `preprocess/service.py`
11. `services/summary_reuse.py`
12. `validation/review_validator.py`
13. `services/repair_integration.py`
14. `tests/test_summary_reuse.py`
15. `tests/test_gui_playwright.py`
16. `tests/test_runtime_orchestrator.py` / `tests/test_runtime_subagent_contract.py`
17. `tests/test_job_runner.py`
18. `tests/test_week3_validation.py` / `tests/test_week4_repair_integration.py`

其中：

- `summary_schema.py` 是阶段一 canonical summary 的事实来源
- `services/job_runner.py` 是当前 job workspace / resume / artifact 协调层的重要入口
- `main.py` 仍然很大，但现在更多承担兼容入口与主流程编排角色
- `tests/*` 往往比旧注释和旧帮助文本更可信

## 4. 当前架构总览

```text
用户入口
├─ CLI: main.py -> dispatch_command() -> services/job_runner.py
└─ GUI: launch_gui.py -> gui/app.py -> services/workflow_facade.py -> services/job_runner.py

AI-native 入口
└─ Codex / OMX: .codex/skills/auto-generate-orchestrator/SKILL.md
   -> runtime/job_spec.py
   -> runtime/orchestrator.py
   -> services/job_runner.py / validator.py / workspace artifacts

运行时 / 工作区层
├─ services/job_workspace.py
├─ services/artifact_registry.py
├─ services/progress_state.py
├─ services/job_fingerprint.py
└─ services/queue_service.py

阶段一：输入、预处理、结构化摘要
├─ zotero_parser.py
├─ file_finder.py
├─ preprocess/service.py
├─ preprocess/visual_artifacts.py
├─ services/stage1_input_builder.py
├─ summary_schema.py
├─ services/paper_artifact.py
└─ services/summary_reuse.py

阶段二 / 三：大纲、综述、引用主链
├─ main.py / LiteratureReviewGenerator
├─ services/review_draft.py
├─ services/citation_manifest.py
├─ report_generator.py
└─ docx_writer.py

验证 / 修复层
├─ validator.py
├─ validation/review_validator.py
├─ validation/summary_recheck.py
├─ validation/repair_planner.py
├─ validation/repair_apply.py
└─ services/repair_integration.py

产品化 / 配置 / 扩展
├─ gui/app.py
├─ services/configuration_service.py
├─ services/environment_service.py
├─ free_mode/service.py
├─ free_mode/profile_manager.py
└─ rag/local_rag.py
```

## 5. 当前真实主链

### 5.1 输入模式

项目当前支持两条正式输入链：

- **PDF folder 模式**：直接扫描文件夹中的 PDF
- **Zotero 模式**：通过 `Paths.zotero_report` + `Paths.library_path` 定位文献与附件

### 5.2 阶段一：论文分析

阶段一现在不只是“从 PDF 提取文本然后让模型总结”。真实链路已经扩展为：

1. 收集源论文描述（PDF / Zotero）
2. 解析并定位 PDF
3. 进入预处理层（可能产生 `normalized.md`、`page_index.json`、`diagnostics.json` 等）
4. 构建 stage1 输入（包括文本、可选 visual refs、多模态信息）
5. 调用 Reader API 生成结构化摘要
6. 归一化到 canonical summary schema
7. 写入 `*_summaries.json`
8. 为每篇论文额外写入 `paper_artifact`
9. 输出 Excel 分析表

### 5.3 阶段二：大纲

当前默认下游真相来源是 markdown outline：

- 主输出：`*_literature_review_outline.md`
- 默认 API：`Outline_API`
- `--outline-adopt` 仍然存在，但它是显式 / 手动兼容路径，不应被视为默认主链

### 5.4 阶段三：综述正文

阶段三已经不再只依赖“summary + Word”这类松散结构，当前更接近：

- 先写出 `review_draft_v2`
- 再写出 `citation_manifest_v3`
- 再根据 draft + manifest 输出 DOCX

也就是说：

- `docx` 是最终导出物
- `review_draft_v2 + citation_manifest_v3` 才是阶段三更重要的结构化真相来源

### 5.5 验证 / 修复

项目现在已经有单独的 validation / repair 管线：

- `validation_report`
- `repair_plan`
- `repair_apply_result`

用户可见入口仍以 `--validate-review` 为主，但内部已经存在更细分的 evidence resolver、summary recheck、repair planner / apply 结构。

## 6. 当前数据契约与真相来源

### 6.1 阶段一

- 主真相来源：canonical `*_summaries.json`
- 伴随 durable artifact：`paper_artifacts/*.json`
- 结构事实来源：`summary_schema.py`

canonical summary 的核心块包括：

- `routing`
- `paper_metadata`
- `core_analysis`
- `specialized_details`
- `quality_audit`

### 6.2 阶段二

- 主真相来源：`*_literature_review_outline.md`
- reviewed outline JSON 仅保留为兼容 / 手动路径，不是默认主链

### 6.3 阶段三

- 主真相来源：`review_drafts/*_review_draft_v2.json`
- 引用主真相：`citation_manifests/*_citation_manifest_v3.json`
- DOCX / Excel 都是重要导出物，但不要把它们误判成唯一真相来源

### 6.4 阶段四（验证 / 修复）

启用时会出现：

- `validation_report*.json`
- `repair_plan_*.json`
- `repair_apply_result_*.json`
- 以及相关 patch 记录

更细节请看 `TRUTH_SOURCES.md`。

## 7. Job workspace、输出目录与缓存

### 7.1 当前真实输出目录

当前主输出通常位于：

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

### 7.2 兼容目录

```text
output/<project_name>/
```

现在通常只保留指针（例如 `_latest_job.json`），不要再默认认为它是主产物目录。

### 7.3 预处理缓存

预处理缓存通常位于：

```text
output/_preprocess_cache/
```

常见缓存文件：

- `normalized.md`
- `plain_text.txt`
- `page_index.json`
- `chunks.json`
- `diagnostics.json`
- `structured.json`
- `prepare_manifest.json`

## 8. GUI 与 CLI 的真实关系

这部分和旧版理解相比已经发生了明显变化：

### 8.1 稳定事实

- CLI 入口仍然是 `python main.py ...`
- GUI 入口仍然是 `python launch_gui.py`
- GUI 没有自建一套完全独立引擎；GUI 和 CLI 共享底层 job request / workspace / artifact 逻辑，但只有 GUI 拥有用户可见的后台队列交互
- 仓库现在还有 **repo-local Codex skill** 这一条 AI-native 入口；它是加法面，不替代 GUI / CLI

### 8.2 当前执行链

- GUI 通过 `services/workflow_facade.py` 构造参数
- 实际执行由 `services/job_runner.py` 协调
- `main.py` 的 `dispatch_command()` 仍然是兼容入口，但真实运行已经越来越依赖 job workspace / artifact / resume 这一层
- AI-native skill 通过 `runtime/job_spec.py` + `runtime/orchestrator.py` 归一化输入，并在本地复用同一套 workspace / artifact / validation 基座

### 8.3 不要再沿用的旧判断

以后不要简单说：

- “GUI 只是薄壳，核心只有 main.py”
- “所有输出都在 `output/<project>/`”
- “Word/Excel 就是唯一主产物”

这些判断都已经不够准确。

## 9. 配置系统

当前推荐约定仍然是：

- 敏感信息放 `.env`
- 非敏感运行参数放 `config.ini`

关键配置段包括：

- `Paths`
- `Primary_Reader_API`
- `Backup_Reader_API`
- `Writer_API`
- `Outline_API`
- `Free_Mode_API`
- `Validator_API`
- `Performance`
- `Preprocess`
- `Retry_Settings`
- `Stage2_Retry`
- `Validation`
- `Styling`
- `GUI`
- `API_Parameters`

关键环境变量包括：

- `LLM_PRIMARY_READER_API`
- `LLM_BACKUP_READER_API`
- `LLM_WRITER_API`
- `LLM_OUTLINE_API`
- `LLM_FREE_MODE_API`
- `LLM_VALIDATOR_API`
- `MINERU_*`

## 10. 当前重要能力（不要再漏掉）

如果以后更新 README、做项目介绍或回答“现在这个仓库支持什么”，至少要覆盖下面这些能力：

- PDF folder / Zotero 双输入
- GUI + CLI + repo-local Codex skill 三入口
- job workspace + artifact registry + latest pointer
- stage-1 summary reuse
- downstream `--summary-file` + `--summary-source`
- GUI workflow-page queue system
- partial rerun / failed retry
- preprocess cache + OCR fallback
- free mode profile / idea
- review_draft_v2 + citation_manifest_v3
- optional validation / repair
- optional local RAG
- AI-native runtime bridge + `source_bundle.json` / `runtime_stage_trace.json`

## 11. 技术债与注意事项

当前仓库最重要的技术现状如下：

- `main.py` 依然很大，历史逻辑和新逻辑共存
- 项目名称尚未完全统一，仍残留 `llm_reviewer_generator`
- 有些旧帮助文本 / 老文档 / 迁移期说明会落后于当前 reality
- 兼容路径仍然存在，尤其是 outline adopt、部分 legacy output / citation fallback
- GUI 虽然已经产品化很多，但仍然共享历史主流程，不要假设所有页面文案都天然等于底层真实能力
- Word / Excel 是导出层，不是所有问题都应该从这些导出层倒推
- 看到老式 `output/<project>/` 内容时，先确认它是不是兼容指针而不是真实 workspace

## 12. 改这个仓库时先看哪里

如果未来任务是：

- **改用户文档分工**：`README.md`、`README.zh-CN.md`、`README.en.md`、`AGENTS.md`
- **改阶段一摘要结构**：`summary_schema.py`、`services/paper_artifact.py`、`report_generator.py`、相关 tests
- **改阶段一复用**：`services/summary_reuse.py`、`tests/test_summary_reuse.py`
- **改 workspace / output / resume**：`services/job_runner.py`、`services/job_workspace.py`、`services/artifact_registry.py`、`services/progress_state.py`
- **改 GUI 工作台**：`gui/app.py`、`services/configuration_service.py`、`tests/test_gui_playwright.py`
- **改 PDF 预处理**：`preprocess/service.py`、`rag/local_rag.py`、`tests/test_preprocess_service.py`
- **改 review draft / citation 主链**：`services/review_draft.py`、`services/citation_manifest.py`、`docx_writer.py`
- **改 validation / repair**：`validator.py`、`validation/*.py`、`services/repair_integration.py`
- **改 queue**：`services/queue_service.py`、`gui/app.py`、`tests/test_queue_service.py`
- **改 AI-native skill / runtime bridge**：`.codex/skills/auto-generate-orchestrator/SKILL.md`、`runtime/*.py`、`tests/test_runtime_*.py`

## 13. 推荐启动方式

CLI：

```bash
python main.py --setup
python main.py --pdf-folder "D:\papers" --analyze-only
python main.py --pdf-folder "D:\papers" --generate-outline
python main.py --pdf-folder "D:\papers" --generate-review
python main.py --pdf-folder "D:\papers" --run-all
```

GUI：

```bash
python launch_gui.py
```

开发时可用：

```bash
python launch_gui.py --reload --no-show
```

## 14. 给未来对话的简短结论

如果你是新的 AI 对话，请默认把这个项目理解为：

- 一个以 job workspace / artifact / GUI 后台队列为底层支撑的本地 AI 文献分析 / 综述写作工作台
- 入口上既有 `main.py` CLI、`launch_gui.py` + `gui/app.py` GUI，也有 repo-local Codex skill
- 阶段一主真相是 canonical summaries，阶段三主真相已经前移到 `review_draft_v2 + citation_manifest_v3`
- `README.md` 现在只是路由页；用户细节看 `README.zh-CN.md` / `README.en.md`
- 如果需要更底层的 artifact / compatibility 说明，请继续看 `TRUTH_SOURCES.md`

先看本文件，再按任务切到对应模块，不要直接把旧版迁移文档或零散注释当作唯一事实来源。

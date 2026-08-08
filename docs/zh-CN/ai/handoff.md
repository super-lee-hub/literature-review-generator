# AI 交接文档

> 受众：AI Agent、新维护者。
> 来源：AGENTS.md §1-3, §9, §10, §14。

## 文档分工

当前文档分工：

- `README.md` — 项目首页 / 路由页，不再承载全部细节
- `README.zh-CN.md` — 中文用户完整指南
- `README.en.md` — 英文用户完整指南
- `AGENTS.md` — AI / 新维护者仓库接手入口（fat stub，含阅读顺序和链接）
- `TRUTH_SOURCES.md` — 运行时真相、durable artifacts、兼容路径说明（thin stub → docs/）
- `FEATURE_MATRIX.md` — 功能状态矩阵（thin stub → docs/）
- `ARCHITECTURE_BASELINE.md` — 迁移时期基线（thin stub → docs/）

## 项目概述

`auto-generate` 是一个本地运行的 AI 文献分析与综述写作工作台，支持：

- PDF folder 输入
- Zotero report + Zotero library 输入
- CLI、GUI 与 repo-local Codex skill 三入口
- 阶段一摘要 / 阶段二提纲 / 阶段三综述正文
- GUI 后台队列、断点恢复、历史摘要复用、预处理缓存、validation / repair、可选本地 RAG

它不应被理解成"单个脚本的文献综述生成器"，而是一个带 workspace / artifact / GUI 后台队列的本地工作台。

## 推荐阅读顺序

如果你是新的 AI 对话，建议按下面顺序建立上下文：

1. `AGENTS.md` (fat stub at root)
2. `TRUTH_SOURCES.md` → [../runtime/truth-sources.md](../runtime/truth-sources.md)
3. `FEATURE_MATRIX.md` → [../reference/feature-matrix.md](../reference/feature-matrix.md)
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

其中 `summary_schema.py` 是阶段一 canonical summary 的事实来源，`services/job_runner.py` 是 job workspace / resume / artifact 协调层入口，`tests/*` 往往比旧注释和旧帮助文本更可信。

## 配置系统

当前推荐约定：敏感信息放 `.env`，非敏感运行参数放 `config.ini`。

关键配置段：`Paths`, `Primary_Reader_API`, `Backup_Reader_API`, `Writer_API`, `Outline_API`, `Free_Mode_API`, `Validator_API`, `Performance`, `Preprocess`, `Runtime`, `Validation`, `Styling`, `GUI`。provider 自有的输出限制、上下文限制、超时和传输重试上限写在对应 provider 段中。

关键环境变量：`LLM_PRIMARY_READER_API`, `LLM_BACKUP_READER_API`, `LLM_WRITER_API`, `LLM_OUTLINE_API`, `LLM_FREE_MODE_API`, `LLM_VALIDATOR_API`, `MINERU_*`

## 当前重要能力

如果更新 README 或做项目介绍，至少要覆盖：

- PDF folder / Zotero 双输入
- GUI + CLI + repo-local Codex skill 三入口
- job workspace + artifact registry + latest pointer
- stage-1 summary reuse
- downstream `--summary-file` + `--summary-source`
- GUI workflow-page queue system
- partial rerun / failed retry
- preprocess cache + OCR fallback
- free mode profile / idea
- review_draft（artifact_version=v3）+ citation_manifest_v3
- optional validation / repair
- optional local RAG
- AI-native runtime bridge + `source_bundle.json` / `runtime_stage_trace.json`

## 给未来对话的简短结论

如果你是新的 AI 对话，请默认把这个项目理解为：

- 一个以 job workspace / artifact / GUI 后台队列为底层支撑的本地 AI 文献分析 / 综述写作工作台
- 入口上既有 `main.py` CLI、`launch_gui.py` + `gui/app.py` GUI，也有 repo-local Codex skill
- 阶段一主真相是 canonical summaries，阶段三主真相是当前 `review_draft（artifact_version=v3）+ citation_manifest_v3`
- `README.md` 现在只是路由页；用户细节看 `README.zh-CN.md` / `README.en.md`
- 如果需要更底层的 artifact / compatibility 说明，请继续看 `docs/zh-CN/runtime/`

先看本文件，再按任务切到对应模块，不要直接把旧版迁移文档或零散注释当作唯一事实来源。

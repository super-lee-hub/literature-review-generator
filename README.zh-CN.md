# auto-generate 中文指南

> 根 README (`README.md`) 现在只做路由页；这份文档负责中文用户说明。

## 1. 文档怎么分工

- `README.md`：项目首页 / 路由页 / 快速选入口
- `README.zh-CN.md`：中文完整使用说明（就是这份）
- `README.en.md`：英文完整使用说明
- `AGENTS.md`：给 AI 和新维护者的接手文档
- `TRUTH_SOURCES.md`：更底层的运行时事实来源、产物真相、兼容层说明
- `FEATURE_MATRIX.md`：功能实现状态矩阵

## 2. 项目定位

`auto-generate` 是一个本地运行的 AI 文献分析与综述写作工作台，而不再只是早期的“单脚本综述生成器”。

它支持两种主输入模式：

- **PDF folder 模式**：直接扫描一个 PDF 文件夹
- **Zotero 模式**：读取 `Zotero report + Zotero library`

它现在有三种主要入口面：

- **CLI**：`python main.py ...`
- **GUI**：`python launch_gui.py`
- **Codex / OMX skill**：仓库内置的 `auto-generate-orchestrator`，适合在 Codex 会话里直接走 AI-native 执行链

主流程仍然是经典三阶段：

1. **阶段一：论文分析** -> 生成结构化 `summaries.json`
2. **阶段二：综述大纲** -> 生成 `outline.md`
3. **阶段三：综述正文** -> 生成 `docx`

但现在项目外围已经扩展出：

- 本地 GUI 工作台
- Job workspace / artifact registry / resume state
- GUI 工作台内置串行后台队列与任务恢复
- PDF 预处理缓存、OCR fallback、`normalized.md` 中间产物
- 阶段一历史摘要复用
- free mode profile / idea
- review draft + citation manifest 持久化
- 可选验证 / repair 管线
- 可选本地 RAG

## 3. 当前能力一览

### 3.1 你可以做什么

- 扫描一个 PDF 文件夹批量分析论文
- 通过 Zotero report + library 解析文献和附件
- 生成阶段一摘要、阶段二提纲、阶段三综述正文
- 只重跑某一章 / 某一节
- 只重试失败论文或失败综述章节
- 复用历史 `summaries.json`
- 把多个历史 `summaries.json` 合并为当前下游输入
- 用 GUI 管理 setup、workflow（含后台队列）、logs、guide
- 用 CLI 直接批量执行（CLI 不再暴露公共队列命令）
- 在 Codex / OMX 中使用仓库内置 skill 走 AI-native 运行时
- 对已生成综述做额外验证

### 3.2 当前工作方式

- **GUI 与 CLI 共用同一条底层执行链**，不是两套完全独立引擎。
- **Codex skill 模式是第三条加法入口**：它不会替代 GUI / CLI，而是复用现有 workspace / artifact / validation 基座。
- 当前真实输出以 **job workspace** 为主，而不是老式 `output/<project>/` 混合目录。
- Word / Excel 是重要导出物，但不是唯一或最高优先级的真相来源；一些更底层的产物已经转移到结构化 JSON。

## 4. 安装与初始化

### 4.1 安装依赖

```bash
pip install -r requirements.txt
```

### 4.2 运行 setup

```bash
python main.py --setup
```

### 4.3 启动 GUI

```bash
python launch_gui.py
```

开发时可用：

```bash
python launch_gui.py --reload --no-show
```

## 5. 快速开始

### 5.1 最短 CLI 路径（PDF 文件夹）

```bash
python main.py --pdf-folder "D:\papers" --analyze-only
python main.py --pdf-folder "D:\papers" --generate-outline
python main.py --pdf-folder "D:\papers" --generate-review
```

或者一次跑完：

```bash
python main.py --pdf-folder "D:\papers" --run-all
```

如果你希望输出更容易区分，建议显式指定项目名：

```bash
python main.py --pdf-folder "D:\papers" --project-name "my_review" --run-all
```

### 5.2 最短 GUI 路径

1. `python launch_gui.py`
2. 先在 Setup 页面填路径、API 和模型
3. 到 Workflow 页面选择 PDF / Zotero 模式
4. 在 Workflow 页面点击主流程按钮提交任务；GUI 会自动加入串行后台队列，表单仍可继续配置下一项

## 6. 常用工作流

### 6.1 PDF 文件夹模式

```bash
python main.py --pdf-folder "D:\papers" --analyze-only
python main.py --pdf-folder "D:\papers" --generate-outline
python main.py --pdf-folder "D:\papers" --generate-review
python main.py --pdf-folder "D:\papers" --run-all
```

### 6.2 Zotero 模式

先在 `config.ini` 或 GUI 中设置：

- `Paths.zotero_report`
- `Paths.library_path`

也可以直接命令行传入：

```bash
python main.py --project-name "my_review" --zotero-report "D:\zotero_report.txt" --library-path "D:\ZoteroLibrary" --analyze-only
python main.py --project-name "my_review" --generate-outline
python main.py --project-name "my_review" --generate-review
```

### 6.3 复用已有阶段一摘要

当前有两种复用方式：

1. **下游步骤显式加载历史 summary 文件**
   - `--summary-file`
   - 可重复追加 `--summary-source <path>`
2. **阶段一自动扫描历史输出并增量复用**
   - `--reuse-stage1`
   - 可重复追加 `--reuse-summary-file <path>`

当前自动复用会优先尝试：

1. DOI 完全一致
2. canonical paper key 完全一致
3. `title + first author + year` 的唯一高置信命中

示例：

```bash
python main.py --project-name "subset_outline" --summary-file "D:\subset\subset_summaries.json" --generate-outline

python main.py --project-name "subset_review" --summary-file "D:\subset\subset_a_summaries.json" --summary-source "D:\subset\subset_b_summaries.json" --generate-review

python main.py --pdf-folder "D:\new_papers" --project-name "pdf_overlap" --analyze-only --reuse-stage1

python main.py --pdf-folder "D:\new_papers" --project-name "pdf_overlap" --analyze-only --reuse-stage1 --reuse-summary-file "D:\cache\curated_summaries.json"
```

### 6.4 局部重跑与失败恢复

```bash
python main.py --pdf-folder "D:\papers" --generate-section 3
python main.py --pdf-folder "D:\papers" --retry-failed
python main.py --pdf-folder "D:\papers" --retry-review-failed
python main.py --project-name "my_review" --validate-review
```

说明：

- `--generate-section <n>`：只重做指定章节
- `--retry-failed`：只重试阶段一失败论文
- `--retry-review-failed`：只重试失败或缺失的综述章节
- `--validate-review`：对当前综述做额外验证；更底层的 validation / repair 产物会写入当前 workspace

### 6.5 GUI 后台队列

队列现在是 **GUI-first** 的交互模型：在 Workflow 页面点击“仅分析文献 / 生成大纲 / 生成全文 / 一键运行”等主流程按钮时，任务会进入 GUI 内部的持久化串行队列，并在后台按顺序处理。提交后表单保持可编辑，你可以继续配置并提交下一项。

CLI 不再暴露公共队列命令；命令行入口保持直接运行模式，例如 `--analyze-only`、`--generate-outline`、`--generate-review`、`--run-all`。AI-native Codex / OMX skill 也保持直接运行，不进入 GUI 队列。

## 7. 进阶能力

这些功能通常不是第一次使用时的必需项，但已经是当前产品的一部分：

- `auto-generate-orchestrator`：在 Codex / OMX 中直接调用仓库内置 skill，走 AI-native 入口
- `--prime-with-folder` + `--concept`：概念预热 / concept priming
- `--free-mode-profile`：加载 free mode profile JSON
- `--free-mode-idea`：直接输入 free mode idea 文本
- `--merge`：把多个 `summaries.json` 合并成一个
- `--outline-adopt`：大纲采纳兼容路径（手动 / 显式流程，不是默认主链）
- PDF 预处理缓存：`normalized.md` / `page_index.json` / `diagnostics.json` / `chunks.json`
- 可选本地 RAG：在预处理阶段构建索引

### 7.1 Codex / skill AI-native 入口

如果你是在 Codex / OMX 里直接操作这个仓库，而不是手动点 GUI 或敲 CLI，那么可以使用仓库内置的 `auto-generate-orchestrator` skill。

它的定位是：

- **第三入口面**，不是对 GUI / CLI 的替换
- 仍然复用现有的 `job workspace`、`artifact registry`、`resume`、`validation / repair` 基座
- 更适合让 Codex 在仓库里直接做“输入归一化 -> 阶段执行 -> 持久化产物 -> 验证”的 AI-native 编排

当你走这条入口时，除了常规产物外，还可能看到：

- `artifacts/source_bundle.json`
- `artifacts/runtime_stage_trace.json`

如果你是普通用户，优先理解 GUI / CLI 即可；如果你是在 Codex 里让仓库自主执行，再关注这个入口。

## 8. 输出目录与关键产物

### 8.1 当前主输出目录

当前真实输出通常位于：

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
│  └─ validation / repair 相关 JSON（启用时）
├─ checkpoints/
├─ logs/
├─ reports/
└─ artifact_registry.json
```

### 8.2 兼容目录

```text
output/<project_name>/
```

这个目录现在通常只保留指针，例如：

- `_latest_job.json`

不要优先把它当成真实产物主目录。

### 8.3 常见导出物

- `reports/*_analyzed_papers.xlsx`
- `reports/*_literature_review.docx`
- `reports/*_failed_papers_report.txt`
- `checkpoints/*_review_checkpoint.json`
- `artifacts/review_drafts/*_review_draft_v2.json`
- `artifacts/citation_manifests/*_citation_manifest_v3.json`

### 8.4 预处理缓存

```text
output/_preprocess_cache/
```

常见缓存产物：

- `normalized.md`
- `plain_text.txt`
- `page_index.json`
- `chunks.json`
- `diagnostics.json`
- `structured.json`
- `prepare_manifest.json`

### 8.5 AI-native runtime 附加产物

当你使用仓库内置的 Codex skill 入口时，当前工作区里还可能出现：

- `artifacts/source_bundle.json`：这次 AI-native 运行归一化后的输入快照
- `artifacts/runtime_stage_trace.json`：阶段执行轨迹，区分本地步骤与 subagent 生成步骤

## 9. 配置建议

当前推荐约定：

- **敏感信息** 放 `.env`
- **非敏感运行参数** 放 `config.ini`

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

## 10. 排障建议

- **第一次跑**：从 `--analyze-only` 开始
- **想用界面**：`python launch_gui.py`
- **想在 Codex 里直接让仓库自主执行**：使用 repo-local skill `auto-generate-orchestrator`
- **找不到输出**：先看 `output/<project_name>__<job_id>/`
- **只修一部分**：`--generate-section` 或 `--retry-review-failed`
- **想增量复用历史阶段一结果**：`--reuse-stage1`
- **需要更深入的运行时真相**：看 `TRUTH_SOURCES.md`
- **需要 AI / 维护者接手文档**：看 `AGENTS.md`

## 11. 给开发者 / 维护者的入口

如果你不是普通用户，而是来接手仓库或排查实现，请优先看：

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
11. `validation/review_validator.py`

## 12. 一句话总结

把这个项目理解成：

- 一个以本地 GUI + CLI + repo-local Codex skill 为入口的 AI 文献分析 / 综述写作工作台
- 已经具备 job workspace、artifact、GUI 后台队列、reuse、validation 等产品化能力
- 用户文档以本文件和英文 README 为主
- 更底层的真相和兼容细节请看 `AGENTS.md` 与 `TRUTH_SOURCES.md`

# LLM 文献综述生成器

[英文指南](./README.en.md) | [项目首页](./README.md)

这是一个面向普通用户的本地 AI 文献工作台。你可以把 PDF 文件夹或 Zotero 文献库交给它，然后生成论文摘要、提纲和完整综述。

## 你可以做什么

- 扫描一个 PDF 文件夹，批量分析论文
- 使用 Zotero 报告 + 文献库路径来处理文献
- 生成论文摘要、综述提纲和完整综述
- 重试失败的论文或失败的综述章节
- 只重跑某一章或某一节
- 使用命令行或 GUI 操作
- 先排队多个任务，之后再统一运行

## 开始之前

1. 安装依赖：

```bash
pip install -r requirements.txt
```

2. 先执行一次初始化：

```bash
python main.py --setup
```

3. 如果你的项目需要 API key 或文件路径，请在 `config.ini` 或 GUI 设置页里填好。

## 快速开始

如果你已经有 PDF 文件夹，最简单的方式是：

```bash
python main.py --pdf-folder "D:\你的PDF文件夹路径" --analyze-only
```

然后继续执行：

```bash
python main.py --pdf-folder "D:\你的PDF文件夹路径" --generate-outline
python main.py --pdf-folder "D:\你的PDF文件夹路径" --generate-review
```

也可以一次跑完全部流程：

```bash
python main.py --pdf-folder "D:\你的PDF文件夹路径" --run-all
```

如果你想让输出文件带上自己的项目名，可以加 `--project-name`：

```bash
python main.py --pdf-folder "D:\你的PDF文件夹路径" --project-name "我的综述项目" --run-all
```

## PDF 文件夹模式

当你的论文 PDF 都放在同一个文件夹里时，用这个模式最方便。

常用命令：

```bash
python main.py --pdf-folder "D:\你的PDF文件夹路径" --analyze-only
python main.py --pdf-folder "D:\你的PDF文件夹路径" --generate-outline
python main.py --pdf-folder "D:\你的PDF文件夹路径" --generate-review
python main.py --pdf-folder "D:\你的PDF文件夹路径" --run-all
```

如果只想重做某一章：

```bash
python main.py --pdf-folder "D:\你的PDF文件夹路径" --generate-section <章节号>
```

这里要填提纲里的「实际章节号」。

如果第一阶段里有论文失败了：

```bash
python main.py --pdf-folder "D:\你的PDF文件夹路径" --retry-failed
```

## Zotero 模式

如果你使用 Zotero，请先在 `config.ini` 或 GUI 里设置：

- `Paths.zotero_report`
- `Paths.library_path`

你也可以在启动任务时直接在命令行里传入：

```bash
python main.py --project-name "我的综述项目" --zotero-report "D:\你的zotero_report.txt" --library-path "D:\你的Zotero文库路径" --analyze-only
```

然后继续跑同样的主流程：

```bash
python main.py --project-name "我的综述项目" --analyze-only
python main.py --project-name "我的综述项目" --generate-outline
python main.py --project-name "我的综述项目" --generate-review
```

## 复用已有的第一阶段摘要

现在可以用两种方式复用以前跑过的第一阶段结果：

1. **为后续阶段加载一个或多个历史 summary 文件**
   - 常见场景继续用 `--summary-file`。
   - 如果要把多个历史 `summaries.json` 一起作为当前任务输入，可以重复使用 `--summary-source <path>`。
   - 程序会先把这些 summary 合并并物化到当前 workspace，再继续生成 outline / review / section / validation，保证后续生成和复跑的一致性。
2. **在第一阶段自动扫描历史结果并增量复用**
   - 在第一阶段（`--analyze-only` 或 `--run-all`）加上 `--reuse-stage1`。
   - 程序会自动扫描 `Paths.output_path` 下的历史结果，包括当前 workspace 结构和兼容的旧版 `output/<project>/<project>_summaries.json`。
   - 自动匹配优先级是：`DOI 完全一致` -> `canonical paper key 完全一致` -> `title + first author + year` 的唯一高置信命中。
   - 只有唯一高置信候选才会自动复用；如果候选有歧义，会写进 reuse report，然后该论文继续正常分析。
   - 如果你还有位于 `output_path` 之外的额外摘要缓存，也可以重复使用 `--reuse-summary-file <path>` 把它们补充进复用池。

示例：

```bash
# 用单个显式 summary 文件生成子集 outline
python main.py --project-name "subset_outline" --summary-file "D:\subset\subset_summaries.json" --generate-outline

# 用多个历史 summary 文件共同生成综述正文
python main.py --project-name "subset_review" --summary-file "D:\subset\subset_a_summaries.json" --summary-source "D:\subset\subset_b_summaries.json" --generate-review

# PDF 模式：第一阶段自动扫描历史输出，唯一命中的论文会直接复用
python main.py --pdf-folder "D:\new_papers" --project-name "pdf_overlap" --analyze-only --reuse-stage1

# Zotero 模式：同样支持自动扫描历史输出并增量复用
python main.py --project-name "zotero_overlap" --zotero-report "D:\zotero_report.txt" --library-path "D:\ZoteroLibrary" --analyze-only --reuse-stage1

# 额外追加一个位于 output_path 之外的手工复用摘要池
python main.py --pdf-folder "D:\new_papers" --project-name "pdf_overlap" --analyze-only --reuse-stage1 --reuse-summary-file "D:\cache\curated_summaries.json"
```

规则：
- 自动跨运行复用不再只看 DOI。现在会先复用 `DOI 完全一致`，再尝试 `canonical paper key 完全一致`，最后才是唯一的 `title + first author + year` 命中。
- 如果同一优先级下匹配到多个历史候选，程序不会自动复用，而是把它标记为 `ambiguous`，然后继续正常分析，避免误复用。
- 如果当前项目只是部分论文重叠，程序会直接复用已命中的论文，只分析剩余没命中的论文，不需要重新全量跑第一阶段。
- 如果你想同时保留“大组版本”和“子集版本”，请使用不同的 `--project-name`，避免它们共享同一个 workspace 状态。

## GUI

用这个命令启动 GUI：

```bash
python launch_gui.py
```

如果你更喜欢点按钮、看进度条、看队列，就用 GUI。

## 队列任务

当你想先准备多个任务，之后统一执行时，队列模式很有用。

常用命令：

```bash
python main.py --queue-add --pdf-folder "D:\你的PDF文件夹路径" --project-name "我的综述项目" --analyze-only
python main.py --queue-run
python main.py --queue-list
python main.py --queue-cancel <job_id>
python main.py --queue-retry <job_id>
python main.py --queue-clear
```

如果你想指定某个队列文件：

```bash
python main.py --queue-file "custom_queue_file.json" --queue-list
```

如果你想一次加载多个队列文件：

```bash
python main.py --queue-run --queue-files "queue1.json" "queue2.json"
```

## 可选功能

这些功能有用，但不是最基础的必需项：

- `--prime-with-folder` + `--concept`：用概念文件夹做预热
- `--free-mode-profile`：加载 free mode 的 profile JSON
- `--free-mode-idea`：直接输入 free mode 想法文本
- `--summary-source`：在 `--summary-file` 之外继续追加多个下游 `summaries.json` 输入
- `--merge`：把多个 `summaries.json` 合并成一个文件的兼容工具
- `--validate-review`：在需要时额外检查已生成的综述
- `--outline-adopt`：在需要时使用手动采纳的提纲
- `--cleanup`：清理旧工作区文件，只保留最新任务文件
- `--retry-review-failed`：只重试失败或缺失的综述章节

## 输出文件

当前结果通常写到活动任务工作区：

```text
output/<project_name>__<job_id>/
```

常见文件：

- `artifacts/*_summaries.json`
- `artifacts/*_summary_source_manifest.json`
- `artifacts/*_summary_reuse_report.json`
- `artifacts/*_literature_review_outline.md`
- `reports/*_analyzed_papers.xlsx`
- `reports/*_literature_review.docx`
- `reports/*_failed_papers_report.txt`
- `checkpoints/*_review_checkpoint.json`
- `logs/`

如果你看到 `output/<project_name>/`，它通常只是兼容路径或指针，先优先看活动任务工作区。

## 排障提示

- 不知道从哪里开始时，先跑 `--analyze-only`。
- 想打开 GUI？运行 `python launch_gui.py`。
- 只想重做综述的一部分？用 `--generate-section <section_number>`。
- 只想重试失败项？用 `--retry-failed` 或 `--retry-review-failed`。
- 想给这次运行起个名字？加 `--project-name`。
- 用 Zotero 时，先确认报告路径和文献库路径已经填好。

如果你需要技术详细或开发说明，请查看单独的内部文档，不要把这些内容放到 README 里。

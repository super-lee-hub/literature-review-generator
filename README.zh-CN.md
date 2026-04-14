# LLM 文献综述生成器

[中文说明](./README.zh-CN.md) | [English](./README.en.md)

这是一个本地运行的 AI 文献分析与综述生成工作台，支持两种入口：

- `PDF 文件夹模式`：直接分析一个文件夹里的 PDF
- `Zotero 模式`：读取 Zotero 报告和文库路径

它会按阶段完成：

1. 文献预处理与阶段一分析
2. 综述大纲生成
3. 综述正文生成

现在 GUI 和命令行已经做了对齐：

- CLI 有的核心功能，GUI 都有入口
- GUI 和 CLI 都能看到清晰进度
- 阶段二支持自动重试和手动补跑失败章节

## 1. 安装

### 仅运行项目

```bash
pip install -r requirements.txt
```

开发环境、测试、类型检查和 Playwright GUI 端到端测试说明，请看 [DEVELOPMENT.md](./DEVELOPMENT.md)。

推荐安装完成后先执行初始化：

```bash
python main.py --setup
```

## 2. 最常用命令

### 2.1 只做阶段一分析

```bash
python main.py --pdf-folder "D:\你的PDF文件夹路径"
```

### 2.2 生成大纲

```bash
python main.py --pdf-folder "D:\你的PDF文件夹路径" --generate-outline
```

### 2.3 生成全文

```bash
python main.py --pdf-folder "D:\你的PDF文件夹路径" --generate-review
```

### 2.4 一键全流程

```bash
python main.py --pdf-folder "D:\你的PDF文件夹路径" --run-all
```

### 2.5 单独补写某一章

```bash
python main.py --pdf-folder "D:\你的PDF文件夹路径" --generate-section <章节序号>
```

请把 `<章节序号>` 替换成你实际要补写的章节编号，它不是固定默认值。

### 2.6 只重试失败章节

```bash
python main.py --pdf-folder "D:\你的PDF文件夹路径" --retry-review-failed
```

### 2.7 重试阶段一失败论文

```bash
python main.py --pdf-folder "D:\你的PDF文件夹路径" --retry-failed
```

### 2.8 启动 GUI

```bash
python main.py --gui
```

或直接双击：

```bash
start_gui.bat
```

## 2.9 排队功能命令

### 2.9.1 添加任务到队列

```bash
python main.py --queue-add --pdf-folder "D:\你的PDF文件夹路径" --project-name "你的项目名" --analyze-only
```

### 2.9.2 运行队列任务

```bash
python main.py --queue-run
```

### 2.9.3 查看队列任务

```bash
python main.py --queue-list
```

### 2.9.4 取消任务

```bash
python main.py --queue-cancel <job_id>
```

### 2.9.5 重试任务

```bash
python main.py --queue-retry <job_id>
```

### 2.9.6 清空已完成任务

```bash
python main.py --queue-clear
```

### 2.9.7 指定队列文件

```bash
python main.py --queue-file "自定义队列文件路径" --queue-list
```

### 2.9.8 批量加载队列文件

```bash
python main.py --queue-run --queue-files "队列文件1.json" "队列文件2.json"
```

## 3. Zotero 模式

如果你平时用 Zotero，可以先导出 Zotero Report，然后在 `config.ini` 或 GUI 里配置：

- `Paths.zotero_report`
- `Paths.library_path`

然后运行：

```bash
python main.py --project-name "你的项目名"
python main.py --project-name "你的项目名" --generate-outline
python main.py --project-name "你的项目名" --generate-review
```

## 3.1 Week 1 输出目录与恢复语义

Week 1 起，真实产物统一写入 job workspace：

```text
output/<project_name>__<job_id>/
  artifacts/
  checkpoints/
  logs/
  reports/
  artifact_registry.json
```

`output/<project_name>/` 只保留兼容 pointer，例如 `_latest_job.json`。除 pointer 外，代码路径不应再把摘要、checkpoint、大纲、综述正文或报告直接写回 `output/<project_name>/`。

恢复语义也同步调整为：

- 每次成功写入 `*_summaries.json` 时，会同步写入 `stage1_progress_snapshot.json`
- 只有 summaries、没有 progress snapshot 的旧状态会被视为 `weak_resumable`
- fingerprint 不一致的状态会被视为 `non_resumable`
- GUI 和 CLI 现在都通过同一套 facade + compat 底层语义进入工作流

## 4. 当前架构状态（Week 0-5 已完成）

本项目已从简单的三阶段脚本演进为结构化工作台。Week 0-5 主骨架现已完成：

### 核心基础设施（Week 1）
- **Job Workspace**：带 artifact registry 的隔离执行环境
- **Artifact Registry**：所有生成产物的持久化追踪
- **Workflow Facade**：CLI 和 GUI 的统一入口

### 核心契约（Week 2-3）
- **Review Draft V2**：块/段结构的综述内容
- **Citation Manifest V2**：带有 occurrence/cluster 语义的引用真值层
- **Paper Artifact**：持久化的论文分析记录
- **Validation Pipeline**：带证据解析器的基于证据的验证

### 修复链路（Week 4）
- **Repair Planner**：识别问题但不立即应用
- **Repair Apply**：仅在明确批准时应用修复
- **Integration**：完整流水线集成，包括修复后自动生成新的review_draft、citation_manifest和review docx，并自动跑一次review recheck

### 大纲批评与仲裁（Week 5）
- **JSON-first Outline**：结构化大纲表示
- **Critique**：自动化大纲质量批评
- **Arbitration**：多个批评之间的冲突解决
- **Adopt**：安全采用仲裁结果，需要显式用户操作，不再自动adopt

### 队列系统（Week 5）
- **Persistent Queue Service**：基于JSON的持久化队列存储
- **Complete Task Snapshot**：包含source_snapshot、input_fingerprint、config_fingerprint、workspace_path、log_path的完整任务快照
- **Dependency Tracking**：支持串行依赖阻塞和上游失败时的状态传播

### 预处理与证据加载（Week 5）
- **Preprocess Evidence Loader**：统一加载plain_text_path/page_index_path/chunks_path/structured_json_path/manifest_path为validator可直接消费的evidence context
- **Visual Bundle Support**：支持"文本没问题但图表/框架图理解不足"的recheck分流

## 5. 持久化队列（MVP）

项目现在包含持久化队列系统（参见 `services/queue_service.py`）：

- `QueueJobSpec`：带参数和依赖关系的作业规范
- `QueueJobRuntime`：带重试计数的运行时状态追踪
- `PersistentQueueService`：基于 JSON 的持久化队列存储
- 支持：add_job、update_job_state、retry_failed_jobs 等

## 6. 引用真值层

引用系统已升级到 V2（参见 `services/citation_manifest.py`）：

- `CitationOccurrence`：块/段级别的引用出现
- `CitationCluster`：论文级别的引用簇
- `BibliographyEntry`：带 `is_cited` 标志的参考文献条目
- `get_cited_bibliography()`：仅从实际被引论文生成参考文献
- 向后兼容：V1 manifest 可迁移到 V2

## 4. GUI 和 CLI 对应关系

GUI 工作台里现在有这些动作：

- `仅分析文献` -> `python main.py --pdf-folder "..."`
- `生成大纲` -> `python main.py --pdf-folder "..." --generate-outline`
- `生成全文` -> `python main.py --pdf-folder "..." --generate-review`
- `一键运行` -> `python main.py --pdf-folder "..." --run-all`
- `重试失败论文` -> `python main.py --pdf-folder "..." --retry-failed`
- `补写指定章节` -> `python main.py --pdf-folder "..." --generate-section N`
- `重试失败章节` -> `python main.py --pdf-folder "..." --retry-review-failed`

`validate` 还保留，但现在默认关闭，并放在 GUI 的“高级 / 实验功能”里。

## 5. 进度条与可视化

现在所有关键流程都有进度显示：

- PDF 预处理
- 阶段一文献分析
- 阶段一失败论文自动重试
- 大纲生成
- 阶段二章节生成
- 阶段二失败章节自动重试
- 单章补写

显示方式：

- 命令行：继续使用 `tqdm` 和日志
- GUI：显示任务进度卡，包括当前任务、当前阶段、当前论文/章节、成功/失败/剩余数、重试轮次、已耗时

说明：

- 能精确计数的阶段会显示确定型进度条
- 单次长 API 调用会显示不确定型进度条

## 6. 阶段二自动重试与手动重试

### 自动重试

`--generate-review` 在首轮章节生成结束后，会自动补跑失败章节。

对应配置在：

```ini
[Stage2_Retry]
enabled = true
max_retry_rounds = 2
base_retry_delay = 30
max_retry_delay = 120
```

### 手动重试

如果某次运行结束后仍然有缺章，可以执行：

```bash
python main.py --pdf-folder "D:\你的PDF文件夹路径" --retry-review-failed
```

它会只补跑失败章节或缺失章节，不会重跑已经成功的章节。

### 单章补写

如果你只想补一章：

```bash
python main.py --pdf-folder "D:\你的PDF文件夹路径" --generate-section <章节序号>
```

这里的编号应当与大纲中的章节序号一致，例如你要补写第 3 章就传入对应的章节编号。

## 7. 输出文件

运行后一般会在：

```text
output/项目名/
```

看到这些文件：

- `*_summaries.json`：阶段一结构化摘要
- `*_analyzed_papers.xlsx`：Excel 文献分析表
- `*_literature_review_outline.md`：综述大纲
- `*_literature_review.docx`：综述正文
- `*_failed_papers_report.txt`：阶段一失败论文报告
- `*_review_checkpoint.json`：阶段二章节断点
- `*_sections/`：阶段二单章 artifact

## 8. 论文类型分流

阶段一现在是一篇论文一次主调用，模型会在同一次输出里完成：

1. 判断 `paper_type`
2. 生成 `common_core`
3. 生成对应的 `type_specific_details`

当前主类型为：

- `empirical`
- `review`
- `conceptual`
- `uncertain`

同时保留旧字段兼容，所以老的 `summaries.json`、Excel 导出和阶段二流程还能继续用。

## 9. Validate 说明

`validate` 功能目前还保留，但本轮没有继续扩展，也不建议作为默认流程使用。

默认状态：

- `enable_stage1_validation = false`
- `enable_stage2_validation = false`

GUI 中也被移动到了“高级 / 实验功能”。

## 10. 推荐使用顺序

第一次使用建议直接照这个顺序：

1. `python main.py --setup`
2. 先跑阶段一：

```bash
python main.py --pdf-folder "D:\你的PDF文件夹路径"
```

3. 看摘要和 Excel 是否正常
4. 再生成大纲：

```bash
python main.py --pdf-folder "D:\你的PDF文件夹路径" --generate-outline
```

5. 最后生成正文：

```bash
python main.py --pdf-folder "D:\你的PDF文件夹路径" --generate-review
```

如果中途缺章：

- 补单章：`--generate-section N`
- 补所有失败章节：`--retry-review-failed`

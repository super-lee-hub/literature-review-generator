# AGENTS.md

本文件用于让新的 AI 对话或新加入的开发者，在 3 到 5 分钟内快速理解 `d:/auto-generate` 这个仓库的真实现状。

- 推荐项目名：`auto-generate`
- 历史名称：`llm_reviewer_generator`
- 文档定位：AI 接手文档 / 项目上下文速览 / 架构与现状说明
- 最近更新：`2026-04-01`

## 1. 一句话说明

这是一个本地运行的 AI 文献分析与综述写作工作台，支持两种输入模式：

- 直接扫描 `PDF` 文件夹
- 读取 `Zotero report + Zotero library`

它的主流程仍然是经典三阶段：

1. 阶段一：逐篇论文分析，产出结构化摘要 `summaries.json`
2. 阶段二：基于摘要生成综述大纲 `outline.md`
3. 阶段三：基于大纲和摘要生成 `docx` 综述正文

但仓库已经在主流程外围扩展出：

- 本地 GUI 工作台
- 配置与环境检测服务层
- PDF 预处理与缓存层
- 可选本地 RAG
- 自由模式规划器（free mode）
- 新版结构化摘要 schema

所以当前项目不应再简单理解成“单个脚本的文献综述生成器”，更接近“文献分析/综述写作工作台”。

## 2. 先读哪些文件

如果以后新开对话需要快速建立上下文，建议阅读顺序如下：

1. `AGENTS.md`
2. `summary_schema.py`
3. `main.py`
4. `gui/app.py`
5. `services/configuration_service.py`
6. `preprocess/service.py`
7. `report_generator.py`
8. `tests/test_preprocess_service.py`
9. `tests/test_configuration_service.py`
10. `tests/test_gui_playwright.py`

其中：

- `summary_schema.py` 是“阶段一输出长什么样”的当前事实来源
- `main.py` 仍然是核心业务编排入口
- `gui/app.py` 体现了当前产品化方向
- `tests/*` 比很多老注释和旧文档更可信

## 3. 当前架构总览

```text
用户入口
├─ CLI: main.py
└─ GUI: launch_gui.py -> gui/app.py

共享服务层
├─ services/workflow_facade.py
├─ services/configuration_service.py
├─ services/environment_service.py
├─ services/progress_service.py
└─ services/model_selection.py

核心工作流
├─ main.py / LiteratureReviewGenerator
├─ zotero_parser.py
├─ file_finder.py
├─ pdf_extractor.py
├─ preprocess/service.py
├─ ai_interface.py
├─ summary_schema.py
├─ report_generator.py
├─ docx_writer.py
└─ validator.py

扩展能力
├─ free_mode/service.py
├─ free_mode/profile_manager.py
├─ rag/local_rag.py
└─ generate_policy_analysis_excel.py
```

## 4. 核心流程

### 4.1 输入模式

项目有两种主输入模式：

- `PDF folder` 模式：直接给一个 PDF 目录
- `Zotero` 模式：通过 `Paths.zotero_report` 和 `Paths.library_path` 读取文献与附件

### 4.2 阶段一：论文分析

阶段一目标是对每篇论文生成结构化摘要，关键路径如下：

1. 收集论文来源信息
2. 定位 PDF
3. 预处理 PDF
4. 调用 Reader API 进行结构化分析
5. 标准化到统一 schema
6. 写入 `*_summaries.json`
7. 输出 Excel 分析表

阶段一的重要特点：

- 支持 `Primary_Reader_API` + `Backup_Reader_API`
- 支持 PDF 预处理缓存
- 支持将预处理后的 `normalized.md` 作为阶段一输入
- 支持论文类型路由：`empirical` / `review` / `conceptual`
- 支持质量审计字段：完整度、冲突标记、人工复核建议

### 4.3 阶段二：综述大纲

阶段二基于阶段一摘要生成综述大纲：

- 主要输入：`*_summaries.json`
- 主要输出：`*_literature_review_outline.md`
- 模型选择：优先 `Outline_API`，未配置时可回退到 `Writer_API`

### 4.4 阶段三：综述正文

阶段三基于大纲和摘要生成 Word 文档：

- 主要输出：`*_literature_review.docx`
- 断点文件：`*_review_checkpoint.json`
- 样式输出：`docx_writer.py`

### 4.5 可选能力

- 概念预热 / concept priming：`--prime-with-folder` + `--concept`
- 综述验证：`validator.py`，默认关闭
- 本地 RAG：预处理阶段可选构建 Chroma 索引
- 自由模式：通过自然语言先规划 prompt/profile，再应用到任务

## 5. 模块分工

### 5.1 主入口

- `main.py`
  - 仓库的核心调度器仍在这里
  - `LiteratureReviewGenerator` 是主业务类
  - `dispatch_command()` 是 CLI 实际分发入口

- `launch_gui.py`
  - GUI 启动器
  - 负责端口选择、环境提示、NiceGUI 启动

- `gui/app.py`
  - 本地 GUI 工作台
  - 包含 workflow、setup、processing、logs、guide 等页面

### 5.2 配置与环境

- `services/configuration_service.py`
  - 当前默认配置结构的事实来源
  - 定义了新段落：`Outline_API`、`Free_Mode_API`、`Preprocess`、`Stage2_Retry`

- `config.ini.example`
  - 用户配置模板

- `.env.example`
  - API key 与 MinerU 相关环境变量模板

- `services/environment_service.py`
  - 检测当前 Python 运行环境
  - 给出 conda 隔离环境建议

### 5.3 输入与预处理

- `zotero_parser.py`
  - 解析 Zotero report
  - 兼容标准格式、简化 key-value 格式、正则增强解析

- `file_finder.py`
  - 在 Zotero 库里建立 PDF 索引并匹配附件

- `pdf_extractor.py`
  - 老的本地 PDF 文本提取链路
  - 使用 `pdfplumber` 和 `PyMuPDF`

- `preprocess/service.py`
  - 当前较新的预处理层
  - 支持 `MinerU remote -> local fallback`
  - 生成稳定的中间产物和缓存

### 5.4 AI 调用与摘要 schema

- `ai_interface.py`
  - 统一调用 OpenAI-compatible API
  - 负责 JSON 响应解析与自动纠错

- `summary_schema.py`
  - 当前最重要的数据契约文件
  - schema 版本：`summary_v2_lite`
  - 负责 canonical schema 归一化、兼容旧字段、导出报表所需映射

- `models.py`
  - TypedDict 数据模型定义

### 5.5 输出层

- `report_generator.py`
  - 生成多工作表 Excel
  - 按论文类型拆 sheet

- `docx_writer.py`
  - 生成和追加 Word 综述内容

- `validator.py`
  - 阶段一交叉验证
  - 阶段二引用/观点验证
  - 默认不是主流程

### 5.6 扩展层

- `free_mode/service.py`
  - 自由模式规划与 profile 生成

- `free_mode/profile_manager.py`
  - 负责 `*_free_mode_profile.json`

- `rag/local_rag.py`
  - 可选本地 Chroma 索引
  - 依赖 `chromadb` + `sentence-transformers`

- `generate_policy_analysis_excel.py`
  - 项目特化脚本
  - 不是通用主流程的一部分
  - 目前更像示例/专项导出工具

## 6. 当前数据契约

阶段一输出的核心结构不再只是早期的松散摘要，而是 canonical summary：

- `routing`
  - 论文类型、子类型、路由置信度、分类状态

- `core_analysis`
  - 摘要、方法、发现、结论、相关性、局限、理论框架、研究空白等

- `specialized_details`
  - 按 `empirical / review / conceptual` 分类存放类型专属字段

- `quality_audit`
  - 抽取置信度、完整度、是否建议人工复核、缺失关键字段、冲突标记

旧结构仍被兼容，但以后讨论阶段一结果时，应该默认以 `summary_schema.py` 的 canonical 结构为准。

## 7. 预处理缓存与产物

`preprocess/service.py` 会为每个 PDF 生成稳定缓存目录，典型产物包括：

- `normalized.md`
- `plain_text.txt`
- `page_index.json`
- `chunks.json`
- `diagnostics.json`
- `structured.json`
- `prepare_manifest.json`

这些文件的意义：

- `normalized.md`：可作为阶段一输入
- `page_index.json`：按页组织内容
- `chunks.json`：为本地 RAG 准备
- `diagnostics.json`：记录提取质量、OCR、MinerU 状态
- `prepare_manifest.json`：记录缓存是否可复用

## 8. 输出目录约定

项目输出通常位于：

```text
output/<project_name>/
```

常见文件：

- `<project>_summaries.json`
- `<project>_analyzed_papers.xlsx`
- `<project>_failed_papers_report.txt`
- `<project>_checkpoint.json`
- `<project>_literature_review_outline.md`
- `<project>_review_checkpoint.json`
- `<project>_literature_review.docx`
- `<project>_free_mode_profile.json`

预处理缓存通常位于：

```text
output/_preprocess_cache/
```

## 9. 配置系统

当前推荐约定是：

- 敏感信息放 `.env`
- 非敏感运行参数放 `config.ini`

关键配置段：

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
- `Styling`
- `GUI`
- `API_Parameters`

关键环境变量：

- `LLM_PRIMARY_READER_API`
- `LLM_BACKUP_READER_API`
- `LLM_WRITER_API`
- `LLM_OUTLINE_API`
- `LLM_FREE_MODE_API`
- `LLM_VALIDATOR_API`
- `MINERU_*`

## 10. GUI 与 CLI 的真实关系

这部分非常重要。

稳定事实：

- CLI 核心入口是 `python main.py ...`
- GUI 并没有自己重写核心引擎，而是通过 `services/workflow_facade.py -> main.dispatch_command()` 复用旧主流程
- GUI 已经覆盖 setup、workflow、processing、logs、guide 等产品层页面

当前分叉状态：

- `workflow_facade.py` 已经为 GUI 预留了 `generate_section`、`retry_review_failed`、`free_mode_profile`、`free_mode_idea`、`progress_tracker` 等参数
- 但 `main.py` 的 CLI parser 和 `dispatch_command()` 目前仍主要暴露旧版参数集
- 也就是说：GUI 层和服务层已经朝更完整工作台演进，但核心引擎对这些新能力的整合还没有完全闭环

因此以后排查问题时要区分三件事：

- 某能力是否出现在 GUI 上
- 某能力是否在 `workflow_facade.py` 里留了接口
- 某能力是否真的被 `main.py` 主流程完整支持

不要仅凭 GUI 按钮或测试名称就假设主引擎已经完全支持。

## 11. 当前技术债与注意事项

这个仓库最重要的技术现状如下：

- `main.py` 仍然很大，核心逻辑高度集中
- 新能力在持续外移到 `services/`、`gui/`、`preprocess/`、`free_mode/`
- 项目名称尚未完全统一，很多字符串仍保留历史名 `llm_reviewer_generator`
- 旧文档、旧注释、旧帮助文本与当前实现之间存在偏差
- 一些中文注释/字符串存在历史编码遗留，遇到可读性差的地方，优先以代码行为和测试为准
- `ProgressTracker` 已存在，但目前主要是 GUI 外层包装级别的开始/结束状态，并未深度接入主引擎内部逐篇进度事件
- `validator.py` 仍在，但默认配置是关闭的
- `generate_policy_analysis_excel.py` 是专项导出脚本，不要误认成主流程必经步骤

## 12. 以后改这个仓库时的建议

如果未来任务是：

- 改阶段一摘要结构：先看 `summary_schema.py`、`models.py`、`report_generator.py`、相关 tests
- 改 GUI 配置页：先看 `gui/app.py`、`services/configuration_service.py`、`tests/test_gui_playwright.py`
- 改 PDF 预处理：先看 `preprocess/service.py`、`rag/local_rag.py`、`tests/test_preprocess_service.py`
- 改 CLI/GUI 对齐：先看 `services/workflow_facade.py`、`main.py`、`gui/app.py`
- 改 Word/Excel 输出：先看 `docx_writer.py`、`report_generator.py`

## 13. 推荐启动方式

CLI：

```bash
python main.py --setup
python main.py --pdf-folder "D:\\papers" --analyze-only
python main.py --pdf-folder "D:\\papers" --generate-outline
python main.py --pdf-folder "D:\\papers" --generate-review
python main.py --pdf-folder "D:\\papers" --run-all
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

- 一个以 `main.py` 为核心引擎的本地 AI 文献分析/综述写作工作台
- 当前正在从“单文件脚本式工具”演进为“GUI + 服务层 + 预处理层 + schema 驱动”的结构
- `summary_schema.py`、`preprocess/service.py`、`gui/app.py` 代表了较新的方向
- `main.py`、`validator.py`、部分帮助文本和旧命名仍带有历史包袱

先看本文件，再按任务切到对应模块，不要直接把旧版 `IFLOW.md` 或零散注释当作唯一事实来源。

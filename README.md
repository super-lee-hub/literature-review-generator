# 📚 LLM Reviewer Generator: 你的 AI 科研文献综述助手

[![Python](https://img.shields.io/badge/Python-3.7%2B-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/Version-1.2-orange.svg)](VERSION)

**从此告别繁琐的文献阅读与整理工作。**

这是一个基于大语言模型（LLM）的智能工具，能够自动阅读数百篇 PDF 文献，提取关键信息，并为你生成一份结构清晰、引用真实的学术文献综述初稿。

无论你是使用 **Zotero** 管理文献，还是手头只有一个装满 **PDF** 的文件夹，本工具都能帮你从繁重的阅读中解放出来。

## 🚀 3分钟快速上手

### 1. 准备工作
确保你已安装 Python 环境，并拥有大模型 API Key（如 OpenAI, Moonshot/Kimi, Gemini 等）。

```bash
# 安装依赖
pip install -r requirements.txt
```

### 2. 交互式配置（强烈推荐）
第一次使用？直接运行下面的命令，根据提示一步步填入 API Key 即可：
```bash
python main.py --setup
```

### 3. 开始你的第一次综述
根据你的使用习惯，选择一种模式运行：

#### 场景 A：我是 Zotero 用户 (推荐 🌟)
如果你平时用 Zotero 管理文献，请先导出文献报告（右键选中文献 -> 导出条目 -> 格式选 Zotero Reports/报告 -> 保存为 txt）。

**重要配置**：使用Zotero模式前，请确保在 `config.ini` 文件中配置以下路径：
1. `zotero_report`：你导出的Zotero报告文件路径（如 `D:\zotero_report\Zotero 报告.txt`）
2. `library_path`：Zotero文献库存放PDF的路径（如 `D:\zotero_library\Zotero\storage`）

*提示：运行 `python main.py --setup` 可使用交互式向导完成配置*

**一键执行（推荐新手）：**
```bash
# 格式：python main.py --project-name "你的项目名" --run-all
python main.py --project-name "消费者行为研究" --run-all
```

**分步骤执行（适合高级用户）：**
```bash
# 步骤 1：仅分析文献并生成摘要（--analyze-only 可选，默认行为）
python main.py --project-name "消费者行为研究"

# 步骤 2：根据摘要生成大纲
python main.py --project-name "消费者行为研究" --generate-outline

# 步骤 3：根据大纲生成完整综述
python main.py --project-name "消费者行为研究" --generate-review
```

#### 场景 B：我只有一堆 PDF 文件
如果你有一个文件夹，里面放满了要读的 PDF：

**一键执行（推荐新手）：**
```bash
# 格式：python main.py --pdf-folder "PDF文件夹的完整路径" --run-all
python main.py --pdf-folder "D:\我的文档\毕业论文参考文献" --run-all
```

**分步骤执行（适合高级用户）：**
```bash
# 步骤 1：仅分析文献并生成摘要（--analyze-only 可选，默认行为）
python main.py --pdf-folder "D:\我的文档\毕业论文参考文献"

# 步骤 2：根据摘要生成大纲
python main.py --pdf-folder "D:\我的文档\毕业论文参考文献" --generate-outline

# 步骤 3：根据大纲生成完整综述
python main.py --pdf-folder "D:\我的文档\毕业论文参考文献" --generate-review
```
> **提示**：系统会自动用文件夹的名字作为项目名称。

---

## ✨ 核心亮点：它能为你做什么？

*   **📖 自动化阅读**：直接解析 PDF 文件，提取摘要、方法、结论等核心内容。
*   **✍️ 智能综述写作**：基于 AI 分析，生成带有大纲和逻辑的 Word 综述文档。
*   **🛡️ 拒绝 AI 幻觉**：内置双重验证机制，自动核查引用真实性和观点准确性，拒绝"胡编乱造"。
*   **📊 数据化报表**：除了文章，还生成 Excel 分析报表，文献概况一目了然。
*   **⏸️ 随时中断/继续**：处理几百篇文献也不怕，支持断点续传，随时暂停，下次接着跑。
*   **🧠 概念增强模式**：(高级功能) 能够基于特定的学术概念（如"消费者成熟度"）进行深度挖掘。

---

<details>
<summary><b>点击展开：技术工作流程详解</b></summary>

### 🔄 完整处理流程图

```
开始
  ↓
[配置检查] → 配置文件存在且完整？
  ↓ 否
运行交互式配置向导
  ↓
[选择模式]
  ├─ Zotero模式 → 解析Zotero报告
  └─ PDF模式 → 扫描PDF文件夹
  ↓
[阶段一：文献分析]
  ├─ PDF文本提取
  ├─ AI内容分析
  └─ (可选)第一阶段验证
  ↓
[阶段二：综述生成]
  ├─ 生成大纲
  └─ 填充内容
  ↓
[阶段三：验证] (可选)
  ├─ 引用检查
  └─ 观点核查
  ↓
完成
```

### 📝 各阶段详细说明

#### 阶段一：文献分析
- **输入**：Zotero报告文件 或 PDF文件夹
- **处理**：提取PDF文本 → AI分析 → 生成结构化摘要
- **输出**：`summaries.json`, `analyzed_papers.xlsx`, `failed_papers_report.txt`

#### 阶段二：综述生成
- **第一步**：基于摘要生成大纲 (`literature_review_outline.md`)
- **第二步**：填充大纲内容，生成Word文档 (`literature_review.docx`)
- **特性**：支持断点续传，自动处理超长内容

#### 阶段三：验证（可选）
- **触发方式**：
  1. **自动验证**：配置文件中设置 `enable_stage2_validation = true` 后，运行 `--run-all` 或 `--generate-review` 会自动执行验证
  2. **手动验证**：任何时候都可以运行 `--validate-review` 命令手动验证
- **输出**：`validation_report.txt`

</details>

---

## 📂 你将获得什么？

程序运行完成后，请打开 `output/项目名称/` 文件夹，你将看到：

1.  **📄 文献综述初稿 (`.docx`)**
    *   一份包含目录、章节、引用的完整 Word 文档。
    *   *用途：作为你写作的基础底稿，在此基础上修改润色。*
2.  **📊 文献分析报表 (`.xlsx`)**
    *   包含每篇文献的标题、作者、核心观点、AI 总结。
    *   *用途：快速筛选高价值文献。*
3.  **📝 验证报告 (`validation_report.txt`)**
    *   (如果开启第二阶段验证) AI 对综述中引用的核查结果。
    *   *用途：检查是否有 AI 捏造的引用。*
    *   *开启方法：在 `config.ini` 中设置 `enable_stage2_validation = true`*

---

## ⚙️ 高级配置与 API 设置

为了保护你的 Key，建议使用 `.env` 文件，而不是直接修改代码。

<details>
<summary><b>点击展开：如何配置 API Key (.env)</b></summary>

1.  复制 `.env.example` 文件并重命名为 `.env`。
2.  用记事本打开 `.env`，填入你的 Key：

```bash
# 负责主要阅读和分析的 AI（建议使用处理长文本能力强的模型，如 Kimi, Claude-3-Opus）
LLM_PRIMARY_READER_API=sk-xxxxxx

# 备用 AI（当主要 AI 报错时使用）
LLM_BACKUP_READER_API=sk-xxxxxx

# 负责写综述的 AI
LLM_WRITER_API=sk-xxxxxx

# 负责验证真伪的 AI (建议用 GPT-4 等逻辑强的模型)
LLM_VALIDATOR_API=sk-xxxxxx
```
</details>

<details>
<summary><b>点击展开：手动修改详细配置 (config.ini)</b></summary>

如果你需要调整模型名称、并发线程数或字体大小，可以编辑 `config.ini` 文件。
*   `max_workers`: 并发处理数量，建议 2-3，太高容易被 API 限制。
*   `enable_stage2_validation`: 设置为 `true` 开启综述引用验证。
</details>

---

## 🛠️ 进阶功能

### 1. 仅分析文献（不生成综述）
如果你只想先分析文献，稍后再生成综述：
```bash
# Zotero模式仅分析文献（不生成综述）
python main.py --project-name "消费者行为研究"

# PDF文件夹模式仅分析文献（不生成综述）
python main.py --pdf-folder "D:\你的PDF文件夹路径"
```

### 2. 遇到报错怎么办？（断点重试）
网络波动导致几篇文献处理失败？不需要从头再来！系统提供了一键重试功能。

**详细使用方法请参阅下面的 [失败处理机制](#-失败处理机制) 部分。**

简单示例：
```bash
# Zotero模式重试
python main.py --project-name "你的项目名称" --retry-failed

# PDF模式重试（必须提供原始PDF文件夹路径）
python main.py --pdf-folder "D:\你的PDF文件夹路径" --retry-failed
```

### 3. 合并其他分析结果
当你有多批文献分析结果需要合并时，可以使用 `--merge` 命令将新的分析结果合并到现有项目中。

**合并规则说明：**
- **智能合并**：系统基于论文的DOI（或标题+作者）进行匹配
  - 匹配的记录：会用新的分析结果覆盖现有记录
  - 不匹配的记录：会作为新论文添加到项目中
- **自动备份**：合并前会自动创建备份文件 `*.summary.json.backup.*`
- **支持模式**：同时支持Zotero模式和PDF文件夹模式

**使用示例：**

#### Zotero模式合并
```bash
# 将 additional_summaries.json 中的分析结果合并到现有项目中
python main.py --project-name "消费者行为研究" --merge ./additional_summaries.json
```

#### PDF文件夹模式合并
```bash
# 将 additional_summaries.json 中的分析结果合并到现有PDF文件夹项目中
python main.py --pdf-folder "D:\你的PDF文件夹路径" --merge ./additional_summaries.json
```

**操作流程：**
1. 确保要合并的文件路径正确（相对路径或绝对路径）
2. 系统会自动识别主项目对应的 `*_summaries.json` 文件
3. 执行智能合并，更新匹配的记录，添加新的记录
4. 生成合并报告，显示更新和新增的论文数量

### 4. 概念增强模式 (Concept Priming)
如果你想研究某个特定概念在文献中的演变，分两步进行：

**第一步：概念学习**
```bash
# 让 AI 学习种子论文中的概念（建议精选1-5篇核心论文）
python main.py --prime-with-folder "D:\种子论文\消费者成熟度" --concept "消费者成熟度" --project-name "消费者研究"
```

**第二步：概念增强分析**
概念学习完成后，你可以使用已学到的概念分析所有文献。系统支持两种执行模式：

#### 分步骤执行模式（推荐）
```bash
# 1. 仅分析文献（阶段一）
python main.py --project-name "消费者研究" --concept "消费者成熟度"

# 2. 仅生成大纲
python main.py --project-name "消费者研究" --concept "消费者成熟度" --generate-outline

# 3. 仅生成综述
python main.py --project-name "消费者研究" --concept "消费者成熟度" --generate-review

# 4. 仅验证综述
python main.py --project-name "消费者研究" --concept "消费者成熟度" --validate-review
```

#### 一键执行模式
```bash
# 自动执行所有步骤（文献分析 → 大纲生成 → 综述生成 → 验证）
python main.py --project-name "消费者研究" --concept "消费者成熟度" --run-all
```

#### PDF文件夹模式（无需Zotero报告）
```bash
# 分步骤执行
python main.py --pdf-folder "D:\PDF文件夹路径" --concept "消费者成熟度"
python main.py --pdf-folder "D:\PDF文件夹路径" --concept "消费者成熟度" --generate-outline
python main.py --pdf-folder "D:\PDF文件夹路径" --concept "消费者成熟度" --generate-review
python main.py --pdf-folder "D:\PDF文件夹路径" --concept "消费者成熟度" --validate-review

# 一键执行
python main.py --pdf-folder "D:\PDF文件夹路径" --concept "消费者成熟度" --run-all
```

**说明**：
- **概念学习篇数**：系统没有硬性限制，但**建议精选1-5篇核心种子论文**以确保概念定义清晰。更多论文会增加API成本和处理时间，但系统仍可正常处理。
- **分步骤优势**：便于中途检查分析质量、调整概念定义或优化大纲结构。

### 5. 使用自定义配置文件
如果你有多个不同的配置（如不同API模型、不同性能参数），可以使用 `--config` 参数指定自定义配置文件。系统支持两种模式：

#### Zotero模式使用自定义配置
```bash
python main.py --project-name "消费者行为研究" --config custom_config.ini --run-all
```

#### PDF文件夹模式使用自定义配置
```bash
python main.py --pdf-folder "D:\你的PDF文件夹路径" --config custom_config.ini --run-all
```

> **提示**：自定义配置文件应包含完整的配置项。可以使用 `config.ini.example` 作为模板，修改后重命名使用。

### 6. 手动验证功能
系统提供双重验证机制，你可以根据需要手动启用：

#### 阶段一验证（单篇论文级）
- **自动模式**：在 `config.ini` 中设置 `enable_stage1_validation = true`，系统会在分析每篇论文后自动进行交叉验证
- **手动触发**：重新运行分析时启用验证配置即可

#### 阶段二验证（综述级）
- **自动模式**：在 `config.ini` 中设置 `enable_stage2_validation = true`，系统会在生成综述后自动验证引用准确性
- **手动模式**：任何时候都可以使用 `--validate-review` 命令手动验证已生成的综述：
```bash
# Zotero模式验证已生成的文献综述
python main.py --project-name "你的项目名称" --validate-review

# PDF文件夹模式验证已生成的文献综述
python main.py --pdf-folder "D:\你的PDF文件夹路径" --validate-review
```

> **提示**：验证功能需要配置独立的 `[Validator_API]`，建议使用与主分析引擎不同的模型以保证验证的独立性。

---

## ❓ 常见问题 (FAQ)

**Q: 输出目录在哪？**  
A: 所有的结果都会保存在 `output/` 目录下。

**Q: 为什么生成的综述有些地方看起来像是在瞎编？**  
A: 虽然我们有验证机制，但 LLM 仍可能产生幻觉。**请务必将生成的内容视为"初稿"或"辅助材料"，必须人工核对原始文献。**

**Q: `--project-name` 报错？**  
A: 项目名称请使用简洁的词（如"ProjectA"），**不要**填入文件路径（如 `C:\Users\...`）。如果需要指定路径，请使用 `--pdf-folder` 参数。

**Q: 支持中文 PDF 吗？**  
A: 支持。只要 PDF 是文字版（能复制文字）即可；扫描版图片 PDF 效果较差。

---

## 📂 输出文件说明

### 阶段一输出
- `[项目名称]_summaries.json`：AI生成的结构化摘要（包含ai_summary和concept_analysis两部分）
- `[项目名称]_analyzed_papers.xlsx`：Excel格式的详细分析报告（包含完整元数据、文本长度、状态信息等）
- `[项目名称]_failed_papers_report.txt`：失败论文报告
- `[项目名称]_zotero_report_for_retry.txt`：重跑报告（仅Zotero模式）
- `[项目名称]_checkpoint.json`：处理进度检查点
- `[项目名称]_concept_profile.json`：概念学习笔记（概念增强模式）

### 阶段二输出
- `[项目名称]_literature_review_outline.md`：结构化的文献综述大纲
- `[项目名称]_literature_review.docx`：文献综述Word文档
- `[项目名称]_review_checkpoint.json`：综述生成断点文件

### 验证功能输出
- `[项目名称]_validation_report.txt`：文献综述验证报告（包含幻觉引用检查和观点-引用匹配分析）

所有文件都保存在 `output/项目名称/` 目录下。

---

## 🔄 失败处理机制

### 阶段一自动重试
系统在阶段一结束后，会自动对网络或API临时故障导致的失败进行最多2轮的智能重试。

### 一键手动重试 (--retry-failed)
对于自动重试后仍然失败的文献，系统提供了一键重试功能：

**功能特点**：
- 🎯 **精准重试**：只处理上一轮失败的文献
- 📊 **轻量化流程**：在内存中完成所有处理
- 🔄 **状态同步**：自动更新失败报告和重跑报告
- 📈 **可重复执行**：可以重复运行直到所有文献处理成功

**使用方法**：
根据你的原始运行模式，选择对应的重试命令：

**Zotero模式重试**（需要 `--project-name`）：
```bash
python main.py --project-name "你的项目名称" --retry-failed
```

**PDF模式重试**（需要 `--pdf-folder`）：
```bash
# 必须提供与原始运行相同的 --pdf-folder 参数
python main.py --pdf-folder "D:\你的PDF文件夹路径" --retry-failed
```

> **重要提示**：PDF模式重试时必须提供 `--pdf-folder` 参数，否则系统会误认为是Zotero模式。

---

## 🛠 故障排除

**Q: 程序启动失败，提示"配置文件不存在"**  
A: 运行 `python main.py --setup` 启动交互式配置向导

**Q: PDF 文本提取失败**  
A: 确保 PDF 是文字版（能复制文字），检查文件是否加密

**Q: API 调用频繁失败**  
A: 检查网络连接，适当降低 `max_workers` 值（建议 2-3）

**Q: 大纲生成不完整**  
A: 系统会自动启用续写机制，无需手动干预

**Q: 重试功能提示找不到报告文件**  
A: 这通常是因为模式不匹配导致的。请检查：

1. **Zotero模式**：重试时需要提供 `--project-name` 参数，并且输出目录中必须有 `[项目名称]_zotero_report_for_retry.txt` 文件。

2. **PDF模式**：重试时必须提供 `--pdf-folder` 参数（与原始运行相同），否则系统会误认为是Zotero模式而查找Zotero重跑报告。

3. **参数格式**：确保 `--project-name` 是简洁的项目名称（如"案例分析"），而不是完整文件路径。

正确示例：
- Zotero模式：`python main.py --project-name "我的研究" --retry-failed`
- PDF模式：`python main.py --pdf-folder "D:\文献\PDF文件夹" --retry-failed`

---

<details>
<summary><b>点击展开：开发者指南</b></summary>

## 👩‍💻 开发者贡献

如果你想参与本项目开发或进行二次开发：

- **项目结构**：核心逻辑在 `ai_interface.py` (AI交互) 和 `docx_writer.py` (文档生成)
- **运行测试**：`pytest tests/ -v`
- **代码规范**：遵循 PEP 8

更多信息请查看源码和注释。

</details>

---

## 📄 许可证

MIT License

---

## 📁 项目结构

项目采用模块化设计，核心模块清晰分离，便于维护和扩展。清理后的项目结构如下：

```
llm_reviewer_generator/
├── main.py                      # 主程序入口，核心业务流程
├── ai_interface.py              # AI接口层，支持多API统一调用
├── validator.py                 # 验证模块，双阶段验证机制
├── config_loader.py             # 配置加载器，支持.env安全配置
├── file_finder.py               # 智能文件查找工具
├── pdf_extractor.py             # PDF文本提取器，支持多引擎
├── docx_writer.py               # Word文档生成器
├── report_generator.py          # Excel报告生成器
├── setup_wizard.py              # 交互式安装向导
├── utils.py                     # 通用工具函数
├── zotero_parser.py             # Zotero报告解析器
├── config_validator.py          # 配置验证器
├── models.py                    # 数据模型定义，TypedDict实现
├── context_manager.py           # 上下文管理器，支持智能压缩和token优化
├── placeholder_analyzer.py      # 占位符分析器（辅助模块）
├── generate_policy_analysis_excel.py  # 政策分析报告生成器（扩展功能）
├── config.ini                   # 运行时配置（用户创建，不提交）
├── config.ini.example           # 配置模板
├── .env                         # API密钥安全存储（用户创建，不提交）
├── .env.example                 # 环境变量模板
├── .gitignore                   # Git忽略规则
├── requirements.txt             # 依赖管理
├── prompts/                     # AI提示词模板目录
│   ├── backup/                  # 备用提示词文件
│   │   ├── prompt_default_outline.txt
│   │   ├── prompt_default_continue_outline.txt
│   │   └── prompt_default_synthesize.txt
│   ├── optimized_prompt_analyze.txt           # 优化版分析提示词（主）
│   ├── optimized_prompt_synthesize_section.txt # 优化版章节提示词
│   ├── prompt_analyze.txt                     # 备选分析提示词
│   ├── prompt_synthesize_outline.txt          # 大纲生成提示词
│   ├── prompt_synthesize_section.txt          # 章节生成提示词
│   ├── prompt_synthesize.txt                  # 完整综述提示词
│   ├── prompt_concept_analysis.txt            # 概念分析提示词
│   ├── prompt_prime_concept.txt               # 概念学习提示词
│   ├── prompt_continue_section.txt            # 章节续写提示词
│   ├── prompt_continue_outline.txt            # 大纲续写提示词
│   ├── prompt_system_*.txt (4个)              # 系统角色提示词
│   ├── prompt_validate_analysis_strict.txt    # 严格验证提示词
│   └── prompt_validate_claims_batch.txt       # 批量验证提示词
├── tests/                       # 测试套件
│   ├── __init__.py              # 包初始化
│   ├── conftest.py              # pytest配置和fixtures
│   ├── test_ai_interface.py     # AI接口和速率限制测试
│   ├── test_pdf_extractor.py    # PDF文本提取测试
│   ├── test_zotero_parser.py    # Zotero解析器测试
│   ├── test_docx_writer.py      # Word文档生成测试
│   ├── test_report_generator.py # Excel报告生成测试
│   ├── test_api_connection.py   # API连接验证测试
│   ├── test_main_flow.py        # 完整流程集成测试
│   ├── test_validator_diagnostics.py  # 验证模块诊断测试
│   └── test_writer_api.py       # Writer API连接测试
├── output/                      # 输出文件组织
│   └── [项目名称]/              # 按项目分组的输出文件
│       ├── [项目名称]_summaries.json          # 结构化分析数据
│       ├── [项目名称]_analyzed_papers.xlsx    # Excel分析报告
│       ├── [项目名称]_literature_review.docx  # Word综述文档
│       ├── [项目名称]_failed_papers_report.txt # 失败报告
│       ├── [项目名称]_validation_report.txt   # 验证报告（可选）
│       └── [项目名称]_checkpoint.json         # 处理进度
├── logs/                        # 运行日志目录（自动生成）
└── README.md                    # 项目文档
```

### 关键模块说明

- **核心处理流程**：`main.py` - 整合所有模块，管理完整工作流
- **AI交互层**：`ai_interface.py` - 统一的多API接口，支持主引擎和备用引擎切换
- **数据处理**：`context_manager.py` - 智能上下文压缩和token优化
- **验证系统**：`validator.py` - 双阶段验证（单篇论文级和综述级）
- **文件处理**：`pdf_extractor.py`, `zotero_parser.py`, `file_finder.py`
- **输出生成**：`docx_writer.py`, `report_generator.py`
- **配置管理**：`config_loader.py`, `config_validator.py`
- **数据模型**：`models.py` - 强类型数据定义

### 目录管理说明

**用户创建文件**（不应提交到版本控制）：
- `config.ini` - 运行时配置，通过 `--setup` 向导生成
- `.env` - API密钥安全存储，使用 `.env.example` 模板创建

**自动生成目录**：
- `output/` - 所有分析结果和生成文档
- `logs/` - 运行日志，便于调试和问题追踪

**开发资源**：
- `tests/` - 完整的测试套件，使用 pytest 运行
- `prompts/` - AI提示词模板，分为主用和备用版本

**提示词管理策略**：
- **主工作流程**：使用 `optimized_*` 和 `prompt_*` 文件（16个）
- **备用文件**：`backup/` 目录中的3个 `default_*` 文件
- **版本管理**：优化版提示词优先使用，默认版作为备用

---

---

## 👨‍💻 开发者贡献

欢迎为项目贡献代码！如果您想了解项目的技术架构、设计理念或贡献代码，请参考以下资源：

### 开发者文档
- **[DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md)** - 详细的技术架构说明、核心组件介绍、开发指南

### 开发环境搭建
```bash
# 1. 安装开发依赖
pip install -r requirements.txt

# 2. 运行测试
pytest tests/ -v

# 3. 运行特定测试
pytest tests/test_ai_interface.py -v
```

### 贡献指南
1. Fork 本仓库
2. 创建您的特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交您的更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 打开一个 Pull Request

### 代码规范
- 遵循 PEP 8 代码风格
- 添加适当的单元测试
- 更新相关文档
- 确保所有测试通过

### 测试覆盖
项目使用 pytest 进行测试，位于 `tests/` 目录下：
- `test_ai_interface.py` - AI接口和速率限制测试
- `test_pdf_extractor.py` - PDF文本提取测试
- `test_zotero_parser.py` - Zotero解析器测试
- `test_docx_writer.py` - Word文档生成测试
- `test_main_flow.py` - 主要流程集成测试
- `conftest.py` - 测试配置和Fixtures

运行测试：
```bash
pytest tests/ -v --cov=. --cov-report=html
```

---

## 📄 许可证

MIT License - 详见LICENSE文件
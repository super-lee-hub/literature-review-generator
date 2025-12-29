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
# --analyze-only 可选，默认行为就是执行阶段一
python main.py --project-name "消费者行为研究"
```

### 2. 遇到报错怎么办？（断点重试）
网络波动导致几篇文献处理失败？不需要从头再来！
```bash
# 自动重试上一次失败的文献
python main.py --project-name "消费者行为研究" --retry-failed
```

### 3. 合并其他分析结果
如果你有多批文献分析结果需要合并：
```bash
python main.py --project-name "消费者行为研究" --merge ./additional_summaries.json
```

### 4. 概念增强模式 (Concept Priming)
如果你想研究某个特定概念在文献中的演变，分两步进行：

**第一步：概念学习**
```bash
# 让 AI 学习 1-5 篇核心种子论文中的概念
python main.py --prime-with-folder "D:\种子论文\消费者成熟度" --concept "消费者成熟度" --project-name "消费者研究"
```

**第二步：概念增强分析**
```bash
# 让 AI 带着学到的概念去分析所有文献
python main.py --project-name "消费者研究" --concept "消费者成熟度" --run-all
```

### 5. 使用自定义配置文件
如果你有多个不同的配置：
```bash
python main.py --project-name "消费者行为研究" --config custom_config.ini --run-all
```

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
A: 检查是否使用了正确的参数格式，不要把完整路径当作 `--project-name`

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

清理后的项目结构如下：

```
llm_reviewer_generator/
├── main.py                      # 主程序入口
├── ai_interface.py              # AI接口层
├── validator.py                 # 验证模块
├── config_loader.py             # 配置加载器
├── file_finder.py               # 文件查找工具
├── pdf_extractor.py             # PDF文本提取器
├── docx_writer.py               # Word文档生成器
├── report_generator.py          # 报告生成器
├── setup_wizard.py              # 交互式安装向导
├── utils.py                     # 工具函数
├── zotero_parser.py             # Zotero报告解析器
├── config_validator.py          # 配置验证器
├── config.ini                   # 配置文件（由用户创建，不提交）
├── config.ini.example           # 配置文件模板
├── .env                         # 环境变量文件（由用户创建，不提交）
├── .env.example                 # 环境变量模板
├── .gitignore                   # Git忽略文件
├── requirements.txt             # 依赖列表
├── prompts/                     # 提示词模板目录
│   ├── prompt_*.txt
│   └── prompt_system_*.txt
├── tests/                       # 测试目录
│   ├── test_*.py                # 单元测试
│   ├── test_main_flow.py        # 集成测试
│   └── conftest.py              # pytest配置
├── output/                      # 输出目录（自动创建）
│   └── [项目名称]/              # 项目输出文件
└── README.md                    # 项目文档
```

### 关键文件说明

- **核心模块**：`main.py`, `ai_interface.py`, `pdf_extractor.py`, `docx_writer.py`
- **配置文件**：`config.ini`, `.env`（用户创建）
- **测试文件**：`tests/` 目录下的所有测试文件
- **提示词**：`prompts/` 目录下的所有模板文件
- **输出文件**：`output/` 目录（自动生成）

**注意**：
- `config.ini` 和 `.env` 不应提交到版本控制系统
- `output/` 目录下的所有文件都是自动生成的
- 测试文件在 `tests/` 目录下，使用 pytest 运行

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
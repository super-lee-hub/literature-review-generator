# llm_reviewer_generator 文献综述自动生成器

[![Python](https://img.shields.io/badge/Python-3.7%2B-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/Version-1.2-orange.svg)](VERSION)

基于AI的文献综述自动生成工具，支持身份基断点续传、双重工作模式、智能PDF处理、可视化进度条等特性。自动解析Zotero报告或直接处理PDF文件夹，提取PDF文献关键信息，生成结构化文献综述。

## 🚀 5分钟快速开始

### 第一步：安装和配置
```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动交互式配置向导（推荐）
python main.py --setup
```

### 第二步：选择您的使用模式

> **⚠️ 重要提醒**：
> - `--project-name` 参数应该使用**简洁的项目名称**（如"消费者行为研究"）
> - **不要**将完整文件路径用作项目名称，这会导致输出目录错误
> - 如果需要处理特定文件夹的PDF，请使用 `--pdf-folder` 参数

#### 🔥 Zotero模式（推荐用于学术研究）
如果您有Zotero文献库，请使用此模式：
```bash
# 一键完成所有步骤（请使用简洁的项目名称）
python main.py --project-name "消费者行为研究" --run-all
```

#### 📁 直接PDF文件夹模式（快速处理）
如果您只有PDF文件，请使用此模式：
```bash
# 一键完成所有步骤
python main.py --pdf-folder "D:\MyDocuments\Papers\ResearchProject" --run-all
```
**优势**：系统会自动使用文件夹名称作为项目名，无需额外设置。

### 第三步：查看结果
所有输出文件保存在 `output/项目名称/` 目录下，包括：
- Excel分析报告
- Word文献综述
- 结构化数据文件

---

## 🌟 核心特性

### 🔒 稳定性与可靠性
- **身份基断点续传**：基于论文唯一身份（DOI或标题+作者）的断点续传，解决传统索引方式的脆弱性问题
- **动态源数据适应**：支持论文顺序变化、新增/删除论文，系统智能识别已处理内容
- **线程安全设计**：完善的并发控制机制，确保多线程环境下的数据一致性

### ⚙️ 智能处理能力
- **双重工作模式**：支持Zotero报告模式和直接PDF文件夹模式
- **智能PDF处理**：三步决策文件查找，双引擎确保文本提取成功率
- **AI内容分析**：使用大语言模型分析文献内容，生成结构化摘要
- **自动续写机制**：自动检测并解决大纲生成时的上下文长度限制问题
- **概念增强模式**：基于核心概念的深度文献分析，提供历史发展视角

### 🚀 用户体验优化
- **可视化进度条**：实时显示处理进度，提供直观的进度反馈
- **多格式输出**：生成Excel分析报告、Word综述文档和JSON数据
- **自动生成目录**：Word文档自动生成结构化目录，提升阅读体验
- **并发处理**：多线程并行处理，提升处理效率
- **项目管理**：支持多项目并行，文件组织清晰

### 🔍 AI验证与修正
- **第一阶段验证**：对单篇论文的AI分析结果进行交叉验证和自动修正
- **第二阶段验证**：对生成的文献综述进行引用和观点核查，生成结构化验证报告
- **独立验证引擎**：使用独立的AI模型进行验证，确保交叉验证的客观性
- **高效批量处理**：采用"以文献为中心"的反向验证策略，提升验证效率

---

## 📋 详细工作流程

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
  1. 配置文件中启用验证 + `--run-all` 模式（自动执行）
  2. 手动运行 `--validate-review` 命令
- **输出**：`validation_report.txt`

---

## ⚙️ 配置说明

### 🔒 安全配置（重要！）

为了保护您的API密钥，**强烈建议使用 `.env` 文件来管理敏感信息**，而不是直接写在 `config.ini` 中。

#### 使用 .env 文件的步骤：

1. **创建 .env 文件**
   ```bash
   # 复制 .env.example 为 .env
   cp .env.example .env
   ```

2. **编辑 .env 文件，填入您的API密钥**
   ```bash
   # .env 文件格式（不要添加引号）
   LLM_PRIMARY_READER_API=your_primary_api_key_here
   LLM_BACKUP_READER_API=your_backup_api_key_here
   LLM_WRITER_API=your_writer_api_key_here
   LLM_VALIDATOR_API=your_validator_api_key_here  # 可选，仅在启用验证时需要
   ```

3. **确保 .env 不被提交到版本控制系统**
   - `.env` 文件已自动添加到 `.gitignore`
   - 不会意外提交到 Git 仓库

4. **优先级说明**
   - 如果同时在 `.env` 和 `config.ini` 中设置了相同密钥，`.env` 中的值将优先使用
   - 如果 `.env` 文件存在但缺少某些密钥，系统会使用 `config.ini` 中的值作为备用

### 交互式配置（推荐）

首次使用时，运行以下命令启动交互式安装向导：

```bash
python main.py --setup
```

安装向导将引导您逐步完成所有配置项的设置，并自动生成 `config.ini` 文件。

**注意**：安装向导只会设置 `config.ini` 中的非敏感配置（如路径、模型名称等）。API密钥请通过 `.env` 文件设置。

### 手动配置（高级选项）

如果您需要手动编辑配置，可以编辑 `config.ini` 文件：

**重要**：API密钥建议在 `.env` 文件中设置，而不是直接写在 `config.ini` 中。如果必须在配置文件中设置，请使用占位符：

```ini
[Paths]
zotero_report = D:\path\to\Zotero 报告.txt
library_path = D:\path\to\Zotero\storage
output_path = ./output

[Primary_Reader_API]
api_key = sk-your-api-key
model = Pro/moonshotai/Kimi-K2-Instruct-0905
api_base = https://api.siliconflow.cn/v1

[Backup_Reader_API]
api_key = sk-your-backup-key
model = gemini-2.5-pro
api_base = https://api.videocaptioner.cn/v1

[Writer_API]
api_key = sk-your-writer-key
model = gemini-2.5-pro
api_base = https://api.videocaptioner.cn/v1

[Validator_API]
# 验证者AI引擎 - 建议使用与Reader/Writer不同的强大模型
api_key = sk-your-validator-key
model = gpt-4-turbo-preview
api_base = https://api.openai.com/v1

[Performance]
max_workers = 3
api_retry_attempts = 5
# 适应性混合速率控制
# 如果您的API服务商未提供TPM/RPM信息，请设置为0或留空，系统将自动切换到被动模式
primary_tpm_limit = 900000  # 主引擎令牌限制（0=被动模式）
primary_rpm_limit = 9000    # 主引擎请求限制（0=被动模式）
backup_tpm_limit = 2000000  # 备用引擎令牌限制（0=被动模式）
backup_rpm_limit = 9000     # 备用引擎请求限制（0=被动模式）
# 验证模块配置（重要！）
enable_stage1_validation = false  # 第一阶段验证开关：true=启用，false=禁用
enable_stage2_validation = false  # 第二阶段验证开关：true=启用，false=禁用

[Styling]
# 文档样式配置
font_name = Times New Roman
font_size_body = 12
font_size_heading1 = 16
font_size_heading2 = 14
```

---

## 🎮 使用方式详解

### 📝 命令行参数详解

#### 🔑 核心参数说明

| 参数 | 用途 | 使用场景 | 重要说明 |
|------|------|----------|----------|
| `--project-name` | 指定项目名称 | 所有模式都可用 | 应该是简洁的项目名称（如"案例分析"），**不要使用完整文件路径** |
| `--pdf-folder` | 指定PDF文件夹路径 | 直接PDF模式 | 直接指定包含PDF文件的文件夹，支持中文路径 |

#### ⚠️ 常见错误用法

❌ **错误示例：把完整路径当作项目名称**
```bash
python main.py --project-name "C:\Users\Documents\My Papers\Research Project"
```
**错误原因**：系统会错误地将完整路径当作项目名称，导致输出目录嵌套错误。

✅ **正确用法示例**

#### Zotero模式（推荐用于学术研究）

```bash
# 一键执行所有阶段（推荐新手使用）
python main.py --project-name "消费者行为研究" --run-all

# 分阶段执行（适合高级用户）
python main.py --project-name "消费者行为研究"              # 阶段一：文献分析
python main.py --project-name "消费者行为研究" --generate-outline  # 阶段二第一步：生成大纲
python main.py --project-name "消费者行为研究" --generate-review   # 阶段二第二步：生成完整综述
```

#### 直接PDF文件夹模式（快速处理）

```bash
# 一键执行所有阶段（推荐新手使用）
python main.py --pdf-folder "D:\MyDocuments\Papers\ResearchProject" --run-all

# 分阶段执行（适合高级用户）
python main.py --pdf-folder "D:\MyDocuments\Papers\ResearchProject"              # 阶段一：文献分析
python main.py --pdf-folder "D:\MyDocuments\Papers\ResearchProject" --generate-outline  # 阶段二第一步：生成大纲
python main.py --pdf-folder "D:\MyDocuments\Papers\ResearchProject" --generate-review   # 阶段二第二步：生成完整综述
```

#### 💡 参数选择建议

- **如果您有Zotero库**：使用`--project-name`参数，先在配置文件中设置Zotero报告路径
- **如果您只有PDF文件夹**：直接使用`--pdf-folder`参数，系统会自动使用文件夹名作为项目名
- **项目名称命名规范**：使用简洁的中文名称，如"案例分析"、"文献综述"、"研究项目"等

### 🔍 AI验证功能详解

#### 验证配置与执行关系

| 配置项 | 作用 | 自动执行条件 | 手动执行命令 |
|--------|------|--------------|--------------|
| `enable_stage1_validation` | 控制第一阶段验证 | 阶段一执行时自动检查 | 无需手动执行 |
| `enable_stage2_validation` | 控制第二阶段验证 | `--run-all`模式下自动执行 | `--validate-review` |

#### 验证功能使用示例

```bash
# 启用第一阶段验证
# 1. 修改config.ini：enable_stage1_validation = true
# 2. 运行阶段一：验证会自动执行
python main.py --project-name "我的研究项目"

# 启用第二阶段验证（两种方式）

# 方式一：自动执行（推荐）
# 1. 修改config.ini：enable_stage2_validation = true
# 2. 运行一键模式：验证会在综述生成后自动执行
python main.py --project-name "我的研究项目" --run-all

# 方式二：手动执行
# 1. 先生成综述
python main.py --project-name "我的研究项目" --run-all
# 2. 再手动运行验证
python main.py --project-name "我的研究项目" --validate-review
```

#### 验证结果说明

- **第一阶段验证**：
  - 自动修正摘要数据中的错误
  - 结果直接更新到 `summaries.json` 文件
  - 不生成单独报告

- **第二阶段验证**：
  - 生成独立验证报告：`[项目名称]_validation_report.txt`
  - 包含引用检查和观点匹配分析
  - 不修改原始综述文档

### 🎯 概念增强模式使用指南

概念增强模式通过两阶段工作流为用户提供基于核心概念的深度文献分析：

1. **概念学习阶段（Priming）**：基于1-5篇核心种子论文生成概念学习笔记
2. **文献调查阶段（Investigation）**：对每篇论文进行标准分析和概念分析

#### 完整使用流程

```bash
# 第一步：概念学习
python main.py --prime-with-folder "D:\核心种子论文" --concept "消费者成熟度" --project-name "消费者研究"

# 第二步：概念增强分析
python main.py --project-name "消费者研究" --concept "消费者成熟度" --run-all
```

### 🛠 高级功能

#### 🔄 重试失败的文献

当文献分析过程中有论文处理失败时，系统提供了便捷的重试功能：

```bash
# Zotero模式：重试失败论文
python main.py --project-name "我的研究项目" --retry-failed

# 直接PDF模式：重试失败论文  
python main.py --pdf-folder "D:\我的PDF文献" --retry-failed
```

**重试功能工作原理**：
- **Zotero模式**：自动查找并重试在 `[项目名称]_zotero_report_for_retry.txt` 中记录的失败论文
- **直接PDF模式**：从 `summaries.json` 和失败报告中识别需要重试的论文
- **智能匹配**：自动匹配PDF文件，无需手动指定文件路径

**重试成功标志**：当所有失败论文都成功处理后，系统会自动删除重跑报告文件。

#### 📊 合并处理结果
```bash
# 合并额外的文献分析结果
# Zotero模式
python main.py --project-name "我的研究项目" --merge ./additional_summaries.json

# 直接PDF模式
python main.py --pdf-folder "D:\我的PDF文献" --merge ./additional_summaries.json
```

#### ⚙️ 指定自定义配置文件
```bash
# 使用自定义配置文件
python main.py --project-name "我的研究项目" --config custom_config.ini --run-all
```

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

## 📚 常见问题解答

**Q: 如何获取Zotero报告？**
A: 在Zotero中右键点击文献集合 → "导出文献" → 选择格式并保存为txt文件

**Q: 什么是身份基断点续传？**
A: 基于论文唯一身份（DOI或标题+作者）的断点续传机制，相比传统索引方式，它能适应论文顺序变化、支持动态增删论文，具有高稳定性。

**Q: 直接PDF文件夹模式有什么限制？**
A: 该模式会自动从PDF文件名提取标题，并尝试从PDF文件中提取元数据（如作者信息）。如果PDF包含文本型元数据，系统会自动识别并填充相关信息。部分信息（如年份、期刊）可能仍需要手动补充。

**Q: 支持哪些PDF格式？**
A: 支持文本型PDF，自动过滤扫描版和图片型PDF

**Q: 什么是适应性混合速率控制？**
A: 适应性混合速率控制是系统的速率控制机制，支持主动（proactive）和被动（reactive）两种模式。主动模式使用令牌桶进行主动速率控制，被动模式依赖API的429错误处理。

**Q: 验证功能如何工作？**
A: 
- **第一阶段验证**：在文献分析时自动运行，验证并修正每篇论文的AI分析结果
- **第二阶段验证**：在综述生成后运行，检查引用准确性和观点支持度
- **控制方式**：通过config.ini中的`enable_stage1_validation`和`enable_stage2_validation`控制

**Q: 如何在Zotero模式下使用概念增强模式？**
A: 分两步进行：1) 使用`--prime-with-folder`和`--concept`参数进行概念学习；2) 使用`--project-name`和`--concept`参数进行概念增强分析。

**Q: --project-name 和 --pdf-folder 参数有什么区别？应该如何使用？**
A: 
- `--project-name`：指定项目的名称，用于创建独立的输出文件夹。应该是简洁的名称，如"消费者行为研究"，**不要使用完整文件路径**
- `--pdf-folder`：直接指定包含PDF文件的文件夹路径，系统会自动使用文件夹名作为项目名
- **选择建议**：如果您的文献在Zotero中，使用`--project-name`；如果只有PDF文件夹，直接使用`--pdf-folder`

**Q: 为什么重试功能提示找不到重跑报告文件？**
A: 这通常是由于之前使用了错误的参数格式导致的。请确保：
1. 使用正确的参数格式，不要把完整路径当作`--project-name`
2. 确保输出目录路径正确
3. 清理错误的输出目录后重新运行

**Q: 可以在同一个项目中混合使用两种模式吗？**
A: 不建议混合使用。建议为不同类型的文献创建独立的项目，使用不同的项目名称进行区分。

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

## 🛠 故障排除

### 常见问题及解决方案

#### 1. 程序启动失败
- **问题**：运行程序时出现"配置文件不存在"错误
- **解决方案**：运行 `python main.py --setup` 启动交互式配置向导

#### 2. PDF处理问题
- **问题**：PDF文本提取失败
- **解决方案**：确保PDF是文本型，检查文件是否加密

#### 3. API调用问题
- **问题**：API调用频繁失败
- **解决方案**：检查网络连接，适当降低max_workers值

#### 4. 大纲生成问题
- **问题**：大纲生成被截断或不完整
- **解决方案**：系统会自动启用续写机制

#### 5. 概念增强模式问题
- **问题**：概念配置生成失败
- **解决方案**：检查种子论文文件夹是否包含1-5篇文本型PDF或TXT文件

#### 6. 验证功能问题
- **问题**：验证功能不工作
- **解决方案**：
  1. 检查config.ini中的`enable_stage1_validation`和`enable_stage2_validation`设置
  2. 确认[Validator_API]配置正确
  3. 第二阶段验证需要先运行完整的综述生成流程

#### 7. 重试功能问题
- **问题**：重试功能提示找不到重跑报告文件
- **解决方案**：
  1. 检查是否使用了正确的参数格式（不要把完整路径当作`--project-name`）
  2. 确认输出目录路径是否正确
  3. 清理错误的输出目录后重新运行

#### 8. 参数使用错误
- **问题**：程序报错"未找到重跑报告文件"或输出目录异常
- **常见原因**：错误地将完整文件路径用作`--project-name`参数
- **解决方案**：
  1. 使用简洁的项目名称，如"案例分析"而不是完整路径
  2. 如果需要处理特定文件夹的PDF，使用`--pdf-folder`参数
  3. 清理错误生成的输出目录

### 性能优化建议
- 设置 `max_workers = 2-3`，避免API限制
- 使用稳定的网络连接
- 分批处理大量文献（建议每次不超过50篇）

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
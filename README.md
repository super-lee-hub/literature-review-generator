# 📚 auto-generate：可追踪、可恢复的 AI 文献综述工作台

[![Windows tests](https://github.com/super-lee-hub/literature-review-generator/actions/workflows/windows-tests.yml/badge.svg)](https://github.com/super-lee-hub/literature-review-generator/actions/workflows/windows-tests.yml)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/super-lee-hub/literature-review-generator)

[中文指南](./README.zh-CN.md) · [English Guide](./README.en.md) · [DeepWiki](https://deepwiki.com/super-lee-hub/literature-review-generator)

**把"读几十/几百篇论文 → 整理证据 → 搭大纲 → 写综述 → 核对引用 → 修正错误"变成一条可恢复、可追踪、可审计的 AI 工作流。**

`auto-generate` 不是一个简单的"把一堆 PDF 丢给大模型，然后让它自由写综述"的脚本。

它更像一个本地文献综述工作台：语料由你控制，每一阶段都有结构化 artifact，失败可以恢复，引用可以追踪，验证能够回到论文证据，大纲还能由不同模型相互审查。

## ✨ 它解决什么问题？

传统的 AI 文献综述流程经常有几个痛点：

* PDF 很多，一个个上传和整理太慢；
* 只抽文本会漏掉图表、理论框架图和复杂表格；
* 模型写出的 `(作者, 年份)` 很容易引用错论文；
* 大纲由一个模型生成后，往往也是同一个模型自己判断自己；
* 中途失败后重新跑很浪费；
* 最终 Word 看起来完成了，却很难追溯一句话究竟依据哪篇论文、哪段证据。

auto-generate 的设计就是围绕这些问题展开。

## 🌟 核心优势

### 📖 1. 全文优先，而不是只看摘要

支持两种语料入口：

* PDF 文件夹
* Zotero Report + Zotero 文库

系统会先进行 PDF 预处理，再进入 Stage 1 结构化阅读。

### 👁️ 2. 文本 + 视觉证据

Stage 1 不只读取抽取文本。

当前 Vision-First pipeline 会保留全文文本，同时让视觉模型查看论文页面，并追踪图、表、公式、框架图等视觉证据。

长论文可以分批扫描视觉页面，最终再结合全文文本进行综合；视觉模型失败时会明确记录 fallback，而不会把纯文本结果冒充成多模态成功。

### 🧠 3. 多模型 Outline 交叉审校

大纲不是"一个模型写、同一个模型自己夸自己"。

当前推荐流程：

```text
Stage 1 summaries
       ↓
Claude Opus 5：生成主候选大纲
       ↓
GPT-5.6-sol：结构 / 证据 critique
DeepSeek V4 Pro：关系 / coverage critique
       ↓
Claude Opus 5：综合 peer critiques 做最终仲裁
       ↓
reviewed outline
```

不同模型承担不同判断职责，以减少单模型自洽但遗漏问题的风险。

角色由 `[OutlineModels]` 决定，每个语义角色解析到自己的 API section。若某个 critique 与候选生成模型撞成同一个 provider，系统会**明确报出 self-review 诊断**，而不是悄悄降级成单模型自审。

### 🔗 4. 引用是对象，不只是字符串

综述写作使用结构化 `review_draft` 与 `citation_manifest`。

引用关系可以追踪到具体 paper，而不是最后再依赖正则表达式猜 `(Author, Year)`。

最终 bibliography 由实际引用对象驱动。

### 🛡️ 5. 验证回到论文证据

Validation 不把 Stage 1 summary 当作绝对真相。

验证流程可以结合：

* review citation context
* 原论文 preprocess chunks
* page evidence
* OCR / caption
* visual observations

判断综述中的陈述是否真正被来源支持。

### 🩹 6. 修复不是整章重写

Repair 采用受约束的 block/span patch，并绑定 artifact hash / dependency。

mapping 错误优先修 citation mapping；
正文只有在 claim 本身错误时才修改。

修复后可以再次 targeted validation。

### ⏯️ 7. 可恢复、可审计的 Durable Runtime

每次运行拥有自己的 JobWorkspace 和 Artifact Registry。

系统保存阶段 artifact、依赖关系、hash、provider receipt 和状态，因此可以：

* 中断后恢复；
* 只重跑失败节点；
* 判断旧结果是否仍可安全复用；
* 区分 generated / reused / failed；
* 避免只凭文件名猜哪个结果是最新真值。

### 📚 8. Queue 批量处理

可以把多组 PDF / Zotero 任务加入队列，让一个任务完成后继续下一个。

支持查看状态、重试、取消和恢复。

## 🔄 当前处理流程

```text
PDF Folder / Zotero
        ↓
PDF preprocessing / MinerU
        ↓
Stage 1
DeepSeek V4 Flash Vision
文本 + 页面视觉证据
        ↓
Structured summaries
        ↓
Outline Intelligence v3
Claude + GPT + DeepSeek peer review
        ↓
review_draft v3
citation_manifest v3
        ↓
Writer
        ↓
DOCX
        ↓
optional Validation
        ↓
optional Repair + Recheck
```

## 🚀 快速开始

### 1. 安装

```bash
pip install -r requirements.txt
```

推荐使用独立 Python 环境。

### 2. 初始化配置

```bash
python setup_wizard.py
```

API Key 保存在本地 `.env`，不要提交到 Git。

### 3. GUI

```bash
python launch_gui.py
```

适合日常使用和任务管理。

### 4. CLI / durable runtime

```bash
python -m reviewctl --help
```

可以先规划、不调用模型：

```bash
python -m reviewctl plan --spec examples/runtime_specs/direct-run-all.json
```

再运行自己的 spec：

```bash
python -m reviewctl run --spec my-run.json
```

已有 job 可以：

```bash
python -m reviewctl status --job <job_id>
python -m reviewctl inspect --job <job_id>
python -m reviewctl next-action --job <job_id>
python -m reviewctl resume --job <job_id>
python -m reviewctl validate --job <job_id>
python -m reviewctl validation-status --job <job_id>
```

队列与修复另有 `queue-*`、`repair-*`、`retry-node`、`reconcile`、`export`、`attest` 等子命令，用 `python -m reviewctl --help` 查看全部。

## 🤖 当前推荐模型分工

| 环节                                  | 模型                       | 通道                            |
| ----------------------------------- | ------------------------ | ----------------------------- |
| PDF 预处理                             | MinerU `vlm`             | MinerU 官方                     |
| Stage 1 主阅读                         | DeepSeek V4 Flash Vision | DeepSeek 官方                   |
| Stage 1 fallback                    | DeepSeek V4 Flash        | DeepSeek 官方                   |
| Free Mode                           | DeepSeek V4 Pro          | DeepSeek 官方                   |
| Outline 主生成 / 最终仲裁                  | Claude Opus 5            | `chat.178266.xyz` 第三方 gateway |
| Outline structure/evidence critique | GPT-5.6-sol              | `ai.saigou.work` 第三方 gateway  |
| Outline relation/coverage critique  | DeepSeek V4 Pro          | DeepSeek 官方                   |
| Review Writer                       | GPT-5.6-sol              | `ai.saigou.work` 第三方 gateway  |
| Validation adjudication             | DeepSeek V4 Flash        | DeepSeek 官方                   |

> 第三方 gateway 只表示程序请求发送到该服务地址。项目不会据此声称其上游一定为 OpenAI / Anthropic 官方直连，也不会把 provider gateway 身份和模型品牌混为一谈。

## 📂 运行结果与可追溯性

真实运行结果写入 job workspace，而不是把所有状态混在一个项目目录里。

典型 artifact 包括：

* structured Stage 1 summaries
* paper artifacts
* visual coverage / observations
* outline artifacts
* review draft
* citation manifest
* validation result
* repair transaction
* DOCX
* provider receipts
* current artifact set

这些 artifact 共同构成可恢复和可审计的运行记录。

## 🔐 API Key 与隐私

* `.env`、本机 `config.ini`、output 和日志中的敏感内容不得提交到 Git；
* 公共模板只放 placeholder；
* 使用第三方 API gateway 时，请自行判断服务可信度；
* 本项目不会因为模型名写着 GPT / Claude 就自动把第三方接口认定为官方接口。

## ⚠️ 当前证据边界

普通 CI 主要验证：

* strict-offline tests
* Python compile
* Pyright
* CLI smoke
* doctor
* artifact/runtime invariants

Live provider、Playwright、heavy OCR、多主机并发属于独立验证范围。

因此：

**mock/offline 测试通过 ≠ 已证明外部模型接口实时可用。**

## 📚 更多文档

* [中文用户指南](./README.zh-CN.md)
* [English Guide](./README.en.md)
* [Runtime truth sources](./docs/zh-CN/runtime/truth-sources.md)
* [Architecture](./docs/zh-CN/developer/architecture.md)
* [Feature Matrix](./docs/zh-CN/reference/feature-matrix.md)
* [Stage 1 Vision](./docs/zh-CN/runtime/stage1-vision.md)
* [Configuration](./docs/zh-CN/reference/configuration.md)
* [Prompt Inventory](./docs/zh-CN/reference/prompt-inventory.md)
* [AI/developer handoff](./AGENTS.md)
* [DeepWiki](https://deepwiki.com/super-lee-hub/literature-review-generator)

## 🧪 项目定位

生成结果应该被当作**有证据链的学术写作底稿**，而不是无需人工判断的最终论文。

auto-generate 想解决的不是"让 AI 替你思考"，而是把最耗时间、最容易出错的文献整理、证据追踪、结构组织和初稿核查过程做得更系统。

---

如果这是你第一次接触项目：

```bash
pip install -r requirements.txt
python setup_wizard.py
python launch_gui.py
```

从这里开始即可。

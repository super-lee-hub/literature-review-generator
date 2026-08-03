# auto-generate

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/super-lee-hub/literature-review-generator)

> A local, corpus-controlled AI literature analysis and review-writing workbench.  
> 一个本地运行、语料可控、全文优先的 AI 文献分析与综述写作工作台。

## Why This Project Exists / 为什么做这个项目

Many AI literature-review tools start from a topic, search papers on your behalf, and return a polished-looking answer. That is convenient, but it leaves several hard problems unresolved:

- You may not know why those papers were selected, which papers were missed, or whether the review used the exact corpus you intended.
- Humanities and social-science papers are often behind paywalls or stored in personal libraries, so open-web search alone may not reach the full text you actually need.
- Subscription platforms often limit upload counts, hide model choices, and make large review projects expensive or hard to reproduce.
- A generated review is difficult to trust if there is no durable trail from PDF/Zotero input to summary, outline, citation manifest, DOCX, and validation report.

`auto-generate` was built for the opposite workflow:

1. **You control the paper set** through a PDF folder or Zotero export.
2. **The AI works from full text whenever possible**, after local/remote preprocessing and OCR fallback.
3. **You control the APIs and models**, with separate roles for reader, outline, writer, free-mode planning, and validation.
4. **The whole run stays inspectable**, with job workspaces, summaries, outlines, review drafts, citation manifests, logs, and optional validation/repair artifacts.
5. **Both beginners and power users get an entry point**: GUI for guided use, CLI for repeatable runs, and a repo-local Codex/OMX skill for AI-native execution.

## Pick Your Path / 选择入口

| If you are... | Start here | Purpose |
| --- | --- | --- |
| 中文普通用户 | [README.zh-CN.md](./README.zh-CN.md) | 项目定位、GUI 上手、CLI 命令、Zotero/PDF 工作流 |
| English user | [README.en.md](./README.en.md) | Positioning, GUI start, CLI commands, Zotero/PDF workflows |
| AI agent / maintainer | [AGENTS.md](./AGENTS.md) | Repository handoff, architecture map, runtime truth |
| Browsing full documentation | [docs/en/index.md](./docs/en/index.md) \| [docs/zh-CN/index.md](./docs/zh-CN/index.md) | Developer, AI, runtime, and reference docs (bilingual) |
| Debugging outputs or compatibility | [docs/en/runtime/truth-sources.md](./docs/en/runtime/truth-sources.md) | Durable artifacts, compatibility paths |
| Checking feature status | [docs/en/reference/feature-matrix.md](./docs/en/reference/feature-matrix.md) | Implemented / partial / legacy / planned surfaces |
| Want an external codebase overview | [DeepWiki analysis](https://deepwiki.com/super-lee-hub/literature-review-generator) | Generated repo map and architecture reading |

## What It Does / 核心能力

`auto-generate` turns a controlled local corpus into literature-review artifacts:

```text
PDF folder or Zotero report + library
  -> PDF preprocessing / OCR / MinerU or local parsing
  -> Stage 1: structured paper summaries
  -> Stage 2: literature-review outline
  -> Stage 3: review draft + citation manifest + DOCX
  -> Optional validation / repair
```

Main capabilities:

- PDF folder mode and Zotero report + library mode
- Local GUI workbench with setup, workflow, logs, guide, and serial background queue
- Direct CLI for batch/repeatable runs
- `reviewctl` queue operations with cross-process leases, heartbeat, expiry recovery, and read-only validation status
- Repo-local Codex/OMX skill: `auto-generate-orchestrator`
- Stage-1 summary reuse, summary merging, partial reruns, and failed-paper/failed-section retry
- PDF preprocessing cache with `normalized.md`, page index, diagnostics, structured artifacts, OCR fallback, and optional local RAG
- MinerU remote parsing support with local fallback
- Structured review draft and citation manifest artifacts
- Optional review validation and repair artifacts
- Per-module API configuration so reader/writer/outline/validator can use different providers or models

## Quick Start / 快速开始

Install dependencies and run setup:

```bash
pip install -r requirements.txt
python main.py --setup
```

Start the GUI:

```bash
python launch_gui.py
```

Run a PDF-folder review from CLI:

```bash
python main.py --pdf-folder "D:\papers" --project-name "my_review" --run-all
```

Run the stages separately:

```bash
python main.py --pdf-folder "D:\papers" --project-name "my_review" --analyze-only
python main.py --project-name "my_review" --generate-outline
python main.py --project-name "my_review" --generate-review
python main.py --project-name "my_review" --validate-review
```

## Documentation Boundaries / 文档分工

README 文件面向用户，解释项目为什么存在、怎么跑起来、有哪些命令。

开发者、AI Agent 和运行时的细节文档统一放在 `docs/` 中英双语镜像中：

- [docs/en/developer/](./docs/en/developer/) \| [docs/zh-CN/developer/](./docs/zh-CN/developer/) — 架构总览、模块地图、开发环境搭建
- [docs/en/ai/](./docs/en/ai/) \| [docs/zh-CN/ai/](./docs/zh-CN/ai/) — AI 交接文档、Codex/OMX Skill 说明
- [docs/en/runtime/](./docs/en/runtime/) \| [docs/zh-CN/runtime/](./docs/zh-CN/runtime/) — 真源体系、兼容性路径、工作区布局
- [docs/en/reference/](./docs/en/reference/) \| [docs/zh-CN/reference/](./docs/zh-CN/reference/) — 功能矩阵、迁移历史

根目录保留了 `AGENTS.md`、`TRUTH_SOURCES.md`、`FEATURE_MATRIX.md` 等薄存根作为入口，指向 `docs/` 中的权威正文。

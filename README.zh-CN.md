# 📚 auto-generate — 中文用户指南

[![Windows tests](https://github.com/super-lee-hub/literature-review-generator/actions/workflows/windows-tests.yml/badge.svg)](https://github.com/super-lee-hub/literature-review-generator/actions/workflows/windows-tests.yml)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/super-lee-hub/literature-review-generator)

[English Guide](./README.en.md) · [DeepWiki](https://deepwiki.com/super-lee-hub/literature-review-generator)

## 它是什么

`auto-generate` 是一个本地、语料可控、全文优先的 AI 文献综述工作台。

它把"读几十/几百篇论文 → 整理证据 → 搭大纲 → 写综述 → 核对引用 → 修正错误"变成一条**可恢复、可追踪、可审计**的工作流。

它不是"把一堆 PDF 丢给大模型让它自由发挥"的脚本。语料由你控制，每一阶段都有结构化 artifact，失败可以恢复，引用可以追踪，验证能回到论文证据，大纲还能由不同模型相互审查。

## 为什么有优势

### 📖 全文优先，而不是只看摘要

支持两种语料入口：PDF 文件夹，或 Zotero Report + Zotero 文库。系统会先做 PDF 预处理，再进入 Stage 1 结构化阅读。

### 👁️ 文本 + 视觉证据

Stage 1 不只读取抽取文本。Vision-First pipeline 保留全文文本，同时让视觉模型查看论文页面，并追踪图、表、公式、框架图等视觉证据。长论文分批扫描视觉页面后再综合；视觉失败会明确记录 fallback，不会把纯文本结果冒充成多模态成功。

### 🧠 多模型 Outline 交叉审校

大纲不是"一个模型写、同一个模型自己审自己"。当前推荐流程：

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

角色由 `[OutlineModels]` 决定，每个语义角色解析到自己的 API section。若某个 critique 与候选生成模型撞成同一个 provider，系统会**明确报出 self-review 诊断**，而不是悄悄降级。

### 🔗 引用是对象，不只是字符串

综述写作使用结构化 `review_draft` 与 `citation_manifest`。引用关系可追踪到具体 paper，最终 bibliography 由实际引用对象驱动，不依赖事后正则猜 `(Author, Year)`。

### 🛡️ 验证回到论文证据

Validation 不把 Stage 1 summary 当作绝对真相，而是结合 review citation context、原论文 preprocess chunks、page evidence、OCR / caption、visual observations 判断陈述是否真正被来源支持。

### 🩹 修复不是整章重写

Repair 采用受约束的 block/span patch 并绑定 artifact hash / dependency。mapping 错误优先修 citation mapping，正文只在 claim 本身错误时才改，修复后可再次 targeted validation。

### ⏯️ 可恢复、可审计的 Durable Runtime

每次运行拥有自己的 JobWorkspace 和 Artifact Registry，保存阶段 artifact、依赖关系、hash、provider receipt 和状态，因此可以中断后恢复、只重跑失败节点、判断旧结果是否仍可安全复用。

## 当前处理流程

```text
PDF Folder / Zotero
        ↓
PDF preprocessing / MinerU
        ↓
Stage 1：DeepSeek V4 Flash Vision（文本 + 页面视觉证据）
        ↓
Structured summaries
        ↓
Outline Intelligence v3：Claude + GPT + DeepSeek peer review
        ↓
review_draft v3 + citation_manifest v3
        ↓
Writer → DOCX
        ↓
optional Validation → optional Repair + Recheck
```

## 当前推荐模型分工

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

这里的“Stage 3 Review”和“Review Writer”不是两个独立阶段：Review 是阶段，
`Writer_API` 是该阶段按 adopted outline section 调用的写作 provider，最终生成
`review_draft/v3`、`citation_manifest/v3` 和 DOCX。

## 快速开始

```bash
pip install -r requirements.txt
python setup_wizard.py
python launch_gui.py
```

---

# 技术参考

以下内容是当前的机器控制面与运行时细节说明。

## 当前入口

| 需求 | 当前命令或文件 |
| --- | --- |
| 初始配置 | `python setup_wizard.py` |
| GUI 工作台 | `python launch_gui.py` |
| 机器可读 CLI 控制面 | `python -m reviewctl` |
| AI-native 运行 | `RuntimeJobSpec` -> `AgentRuntimeRunner` -> `AgentRuntimeBridge` |

`main.py` 是进入 `reviewctl` 的小型 compatibility-free shim，不是当前编排引擎，
也不是文档中的直接运行 CLI。

## CLI 运行时

请复制并编辑版本控制中的 `RuntimeJobSpec` 示例。示例只使用占位路径：

```bash
python -m reviewctl plan --spec examples/runtime_specs/direct-run-all.json
python -m reviewctl plan --spec examples/runtime_specs/zotero-run-all.json
python -m reviewctl plan --spec examples/runtime_specs/free-mode-idea.json
python -m reviewctl run --spec my-run.json
```

`plan` 会校验 source、action、路径、Free Mode 输入和阶段策略，不会执行 provider
调用；`run` 才会执行 spec 描述的 durable runtime。

## RuntimeJobSpec

直接运行使用 `source.mode = "direct"` 和 `pdf_folder`；Zotero 运行使用
`source.mode = "zotero"`、`zotero_report` 和 `library_path`。完整流程的当前 action
是 `run_all`。其他由 `RuntimeJobSpec` 校验的 typed action 包括 `analyze`、
`generate_outline`、`generate_review`、`generate_section` 和 `validate_review`。

Free Mode 在 spec 边界使用 typed 输入。`free_mode_idea` 与 `free_mode_profile`
只能二选一；idea 会投影为当前 `ReviewIntent`，并绑定到 Writer context。

Concept Mode is currently disabled（概念模式当前不可用）。过时的 Concept Mode
请求会被拒绝，不会静默降级，也不会因此发起 provider 调用。

## 已有 job 与验证

```bash
python -m reviewctl status --job <job_id>
python -m reviewctl inspect --job <job_id>
python -m reviewctl next-action --job <job_id>
python -m reviewctl resume --job <job_id>
python -m reviewctl validate --job <job_id>
python -m reviewctl validation-status --job <job_id>
```

当前验证服务是 `ValidationExecutionService`。其 adjudication reuse authority
绑定 provider ledger、receipt、source closure、attempt identity 和 Registry
dependency closure。

MinerU 的 presigned upload/result 链接仍经过 SSRF 防护。`.env` 中的
`MINERU_ALLOWED_URL_HOSTS` 必须填写精确的 HTTPS 主机名；官方默认返回的
上海 OSS host 是 `mineru.oss-cn-shanghai.aliyuncs.com`；本次真实 smoke 返回的
结果 CDN host 是 `cdn-mineru.openxlab.org.cn`，二者都必须作为精确 host 单独列出。
不要填写 `*`、协议、路径，也不要关闭 TLS 或 host 校验。

## Stage 1 与 Prompt authority

Stage 1 默认采用实验性的 `deepseek-v4-flash-vision-exp`：MinerU 文本仍是主
证据，所有非空 PDF 页面都会渲染并写入 visual coverage，长论文会先按批次做
可恢复的视觉扫描，再进行最终综合。视觉模型失败时回退到
`deepseek-v4-flash`；validation 仍固定使用纯文本的 `deepseek-v4-flash`。
生产 Prompt 统一通过带 hash 校验的 [Prompt 清单](./docs/zh-CN/reference/prompt-inventory.md)
加载。

## Queue 与维护命令

当前 parser 还提供 `doctor`、`queue-list`、`queue-add`、`queue-run`、`queue-retry`、
`queue-cancel`、`queue-remove`、`queue-export` 和 `queue-import`。每个命令都可以
使用 `--help` 查看实际参数。

```bash
python -m reviewctl doctor --config config.ini.example
python -m reviewctl queue-list --queue-file output/_queue/queue.json
```

## 配置迁移

运行时 loader 是 fail-closed 的：`validate_config_keys()` 会拒绝任何不认识的键或
section，因此按旧 schema 写的配置根本走不到 legacy 兼容逻辑。迁移必须在校验
**之前**完成，并且是显式操作，不会在每次运行时偷偷改写配置。

迁移器保证：

* 幂等——重复执行不产生变化；
* 逐行改写——保留注释、空行与顺序；
* 只映射语义确定的项，无法确定的项以告警报出，不凭名字猜测。

已确认 `[Retry_Settings]` 与 `[Stage2_Retry]` 在任何受支持版本中都没有读取方，
属于死配置，因此直接移除；当前 `[Runtime]` 的默认值与它们原本表达的行为一致。

## 证据边界

Windows CI 当前覆盖 compile、test collection、public CLI smoke、strict-offline
测试、Pyright、doctor 和 committed-range whitespace 检查。live API/provider、
Playwright、heavy OCR、多主机 publication/fencing、多主机 single-flight 和
cryptographic provenance verification 属于独立 opt-in 范围，不由离线证据推断。

**mock/offline 测试通过 ≠ 已证明外部模型接口实时可用。**

详见 [AGENTS.md](./AGENTS.md)、[运行时真源](./docs/zh-CN/runtime/truth-sources.md)、
[架构图](./docs/zh-CN/developer/architecture.md)、[功能矩阵](./docs/zh-CN/reference/feature-matrix.md)、
[Stage 1 Vision 流程](./docs/zh-CN/runtime/stage1-vision.md)、[配置参考](./docs/zh-CN/reference/configuration.md)、
[Prompt 清单](./docs/zh-CN/reference/prompt-inventory.md)
和 repo-local [Codex/OMX Skill](./.codex/skills/auto-generate-orchestrator/SKILL.md)。

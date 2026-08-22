# auto-generate — 中文用户指南

`auto-generate` 是一个本地、语料可控、全文优先的 AI 文献分析与综述写作
工作台。处理流程为：

```text
PDF 文件夹或 Zotero 报告 + 文献库
  -> 预处理与 Stage 1 结构化摘要
  -> Outline Intelligence v3
  -> review_draft v3 + citation_manifest v3 + DOCX
  -> 可选 validation / repair
```

输入语料、job workspace、Artifact Registry、阶段 closure 和 validation 证据
都可以检查并支持当前 durable runtime 的恢复。

## 当前入口

| 需求 | 当前命令或文件 |
| --- | --- |
| 初始配置 | `python setup_wizard.py` |
| GUI 工作台 | `python launch_gui.py` |
| 机器可读 CLI 控制面 | `python -m reviewctl` |
| AI-native 运行 | `RuntimeJobSpec` -> `AgentRuntimeRunner` -> `AgentRuntimeBridge` |

`main.py` 是进入 `reviewctl` 的小型 compatibility-free shim，不是当前编排引擎，
也不是文档中的直接运行 CLI。

## 快速开始

```bash
pip install -r requirements.txt
python setup_wizard.py
python launch_gui.py
```

CLI 运行时，请复制并编辑版本控制中的 `RuntimeJobSpec` 示例。示例只使用占位路径：

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

## 证据边界

Windows CI 当前覆盖 compile、test collection、public CLI smoke、strict-offline
测试、Pyright、doctor 和 committed-range whitespace 检查。live API/provider、
Playwright、heavy OCR、多主机 publication/fencing、多主机 single-flight 和
cryptographic provenance verification 属于独立 opt-in 范围，不由离线证据推断。

详见 [AGENTS.md](./AGENTS.md)、[运行时真源](./docs/zh-CN/runtime/truth-sources.md)、
[架构图](./docs/zh-CN/developer/architecture.md)、[功能矩阵](./docs/zh-CN/reference/feature-matrix.md)、
[Stage 1 Vision 流程](./docs/zh-CN/runtime/stage1-vision.md)、[配置参考](./docs/zh-CN/reference/configuration.md)、
[Prompt 清单](./docs/zh-CN/reference/prompt-inventory.md)
和 repo-local [Codex/OMX Skill](./.codex/skills/auto-generate-orchestrator/SKILL.md)。

# Codex/OMX Skill 契约

> 真源：`.codex/skills/auto-generate-orchestrator/SKILL.md`。

repo-local Skill 是与 GUI、CLI 共用 durable runtime 的 AI-native 适配层，不是
另一个 peer control plane。

## 当前 runtime

```text
AI request
  -> RuntimeJobSpec
  -> AgentRuntimeRunner
  -> AgentRuntimeBridge
  -> Registry-backed stages and workspace
```

人类使用的机器控制面是 `python -m reviewctl`，后端是 `ReviewControlPlane`。
`JobRunRequest` 只在当前代码需要时作为内部 adapter；公开的 durable run
specification 是 `RuntimeJobSpec`。

## 当前契约

- Outline Intelligence v3 是唯一生产 Outline 路径。
- Stage 3 输出 `review_draft` artifact version v3、`citation_manifest` v3 和 DOCX。
- Validation 使用 `ValidationExecutionService`、`current_validation`、
  `adjudication_reuse` 和 Registry-backed `closure` 证据。
- Free Mode 使用 `free_mode_intent_input/v1`、`ReviewIntent` projection 和
  Writer context binding。
- Concept Mode is currently disabled（概念模式当前不可用）。过时请求会被拒绝，
  不会静默降级。

source intake、workspace、Registry publication、stage closure、resume、validation
和 runtime trace 必须保持本地且 durable，不要用 shell 命令或 report projection
替代它们。

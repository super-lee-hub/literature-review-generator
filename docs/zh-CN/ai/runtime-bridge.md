# AI runtime bridge

当前 AI-native 链路为：

```text
Codex/OMX Skill
  -> RuntimeJobSpec
  -> AgentRuntimeRunner
  -> AgentRuntimeBridge
  -> current stage registry and durable workspace
```

人类机器控制面仍是 `python -m reviewctl`；GUI 从 `launch_gui.py` 和 `gui/app.py`
开始。三条入口最终共用 workspace、Artifact Registry、stage closure 和 resume
authority。

## Durable bridge artifacts

Bridge 会在 workspace 中记录规范化 source input 和执行模式，包括
`source_bundle.json` 与 `runtime_stage_trace.json`。这些是可观测 runtime artifact，
不是 canonical stage output，也不能替代 Registry authority。

## 当前阶段

- Stage 1 使用当前 preprocessing、source identity、summary 和 reuse contracts。
- Stage 2 仅使用 Outline Intelligence v3。
- Stage 3 通过 `services/review_generation_service.py` 生成
  `review_draft` artifact version v3、`citation_manifest` v3 和 DOCX。它是一个 Review
  阶段；`Writer_API` 是该阶段内部按 adopted outline section 调用一次的 provider。
- Validation 使用 `ValidationExecutionService`、`current_validation`、
  `adjudication_reuse` 和带 Registry dependency 的 `closure`。

Concept Mode is currently disabled。过时请求在当前边界失败，不会转化为 provider
调用。

Outline v3 将候选生成与最终仲裁路由到配置的 Claude Opus 5，将结构/证据审查路由到
配置的 GPT-5.6-sol Responses，将关系/覆盖度审查路由到配置的 DeepSeek V4 Pro。第三方
gateway host 只作为传输身份记录，不能证明其上游是官方服务。

## Resume 与 authority

`AgentRuntimeRunner` 将 status、resume、reconciliation 和 stage execution 交给
durable runtime。单独的 checkpoint、report projection 或 bridge trace 不能授权
completion、promotion、validation reuse 或 export；这些决策必须经过当前
Registry-backed contracts。

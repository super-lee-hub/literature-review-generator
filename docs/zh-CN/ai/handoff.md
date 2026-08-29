# AI 交接说明

本文面向 AI agent 和新维护者。编辑前先读取当前真源；历史 migration 和 baseline
文档保留原始 claim 与 evidence。

## 当前阅读顺序

1. `AGENTS.md`
2. `docs/zh-CN/runtime/truth-sources.md`
3. `docs/zh-CN/reference/feature-matrix.md`
4. `runtime/job_spec.py`
5. `reviewctl.py`
6. `runtime/control_plane.py`
7. `runtime/runner.py`
8. `runtime/orchestrator.py`
9. `services/job_runner.py`
10. `services/artifact_registry.py`
11. `services/job_workspace.py`
12. `outline/v3_executor.py`
13. `services/review_generation_service.py`
14. `validation/execution_service.py`
15. `validation/current_validation.py`
16. `validation/closure.py`
17. `runtime/provider_receipt_closure.py`

Stage 1 专题再读取 `summary_schema.py`、`preprocess/service.py` 和
`services/summary_reuse.py`。

## 当前真相

- 公共 CLI 是 `python -m reviewctl`。
- `RuntimeJobSpec` 是当前 durable run specification。
- AI-native 执行由 `AgentRuntimeRunner` 与 `AgentRuntimeBridge` 负责。
- Outline Intelligence v3 是唯一当前 Outline 路径。
- Outline 角色是节点级且 fail-closed：Claude Opus 5 生成并仲裁，GPT-5.6-sol 审查
  结构/证据，DeepSeek V4 Pro 裁决关系/覆盖度，具体由 `[OutlineModels]` 决定。
- Stage 3 真源是 `review_draft` v3、`citation_manifest` v3 和 DOCX。
  Review 是阶段，`Writer_API` 是阶段内部按 section 调用的 provider。
- Validation 真源由 `ValidationExecutionService` 及其 Registry-backed
  closure/adjudication authority 负责。
- `main.py` 是进入 `reviewctl` 的小型 compatibility-free shim，不是旧的编排 CLI。
- Concept Mode is currently disabled（概念模式当前不可用），过时请求会被拒绝。

## 安全边界

不要绕过 Registry、伪造 provider receipt、把 report projection 当作 canonical truth、
静默提升中间 Outline candidate、恢复 Outline v2，或在没有确定性回归证明真实缺陷时
修改冻结的 Stage 1、Free Mode、validation authority、queue、repair、promotion、
export 和 publication 契约。

# 当前架构总览

> 本文描述当前生产面。architecture baseline 和 migration report 保留为历史
> snapshot，不是当前运行说明。

## 入口与 runtime

```text
CLI:
reviewctl.py
  -> runtime/control_plane.py / ReviewControlPlane
  -> RuntimeJobSpec
  -> runtime/runner.py / AgentRuntimeRunner
  -> runtime/orchestrator.py / AgentRuntimeBridge

GUI:
launch_gui.py
  -> gui/app.py
  -> workflow facade / JobRunner
  -> RuntimeJobSpec
  -> AgentRuntimeRunner

AI-native:
Codex/OMX Skill
  -> RuntimeJobSpec
  -> AgentRuntimeRunner / AgentRuntimeBridge
```

`main.py` 是进入 `reviewctl` 的小型 compatibility-free shim，不是当前编排引擎，
也不是另一个公共控制面。

## 阶段与 authority 层

```text
Stage 1:
source intake -> preprocessing -> 当前 Stage 1 generation/reuse contracts

Stage 2:
仅使用 Outline Intelligence v3
  -> outline/v3_executor.py

Stage 3:
services/review_generation_service.py
  -> Writer_API：每个已 adoption 的 outline section 一次 provider call
  -> review_draft artifact_version=v3
  -> citation_manifest v3
  -> DOCX

Validation:
validation/execution_service.py
  -> validation/current_validation.py
  -> validation/adjudication_reuse.py
  -> validation/closure.py
  -> Registry-backed validation closure

Durability:
services/job_workspace.py + services/artifact_registry.py
  -> CurrentArtifactSet、JobOutcome、queue fencing/publication、
     repair transaction/promotion 和 export admission
```

Stage 1 identity、artifact dependency、provider receipt closure、queue fencing、
validation adjudication authority 和 publication boundary 都是 runtime 语义契约。
只修改文档时不得削弱这些契约。

Stage 3 Review 是阶段契约，Writer 是该阶段内部配置的生成 provider，并不是两个独立
pipeline stage。Writer 接收每个有 evidence binding 的已 adoption outline section，产出
带 citation token 的结构化 blocks，bridge 再把这些调用组装为 canonical review draft、
citation manifest 和 DOCX。

Outline v3 的角色由 `outline/provider_router.py` 解析：Claude Opus 5 负责候选生成与最终
仲裁，GPT-5.6-sol 负责结构/证据审查，DeepSeek V4 Pro 负责关系/覆盖度审查。配置中的
gateway host 只是传输身份，不是官方上游连接的证明。

## 查找入口

| 关注点 | 当前真源 |
| --- | --- |
| Run input 与路径解析 | `runtime/job_spec.py` |
| CLI 控制面 | `reviewctl.py`、`runtime/control_plane.py` |
| 执行与恢复 | `runtime/runner.py`、`runtime/orchestrator.py` |
| Workspace 与 Registry | `services/job_workspace.py`、`services/artifact_registry.py` |
| Outline | `outline/v3_executor.py` |
| Review artifacts | `services/review_generation_service.py` |
| Validation | `validation/execution_service.py`、`validation/current_validation.py`、`validation/closure.py` |
| GUI 与 queue | `gui/app.py`、`services/workflow_facade.py`、`services/queue_service.py` |

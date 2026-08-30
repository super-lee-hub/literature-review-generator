# Current architecture overview

> This document describes the current production surface. The separate
> architecture baseline and migration reports are historical snapshots.

## Entry points and runtime

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

`main.py` is a small compatibility-free shim into `reviewctl`; it is not the
current orchestration engine or a separate public control plane.

## Stage and authority layers

```text
Stage 1:
source intake -> preprocessing -> current Stage 1 generation/reuse contracts

Stage 2:
Outline Intelligence v3 only
  -> outline/v3_executor.py

Stage 3:
services/review_generation_service.py
  -> Writer_API, one provider call per adopted outline section
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
  -> current artifact set, JobOutcome, queue fencing/publication,
     repair transaction/promotion, and export admission
```

Stage 1 identity, artifact dependencies, provider receipt closure, queue
fencing, validation adjudication authority, and publication boundaries are
semantic runtime contracts. Documentation changes must not weaken them.

Stage 3 Review is the stage contract; Writer is the configured generation
provider inside that stage. They are not separate pipeline stages. The Writer
receives each evidence-bound adopted outline section, emits structured blocks
with citation tokens, and the bridge assembles those calls into the canonical
review draft, citation manifest, and DOCX.

Outline v3 role routing is resolved by `outline/provider_router.py`: Claude Opus
5 handles candidate generation and arbitration through the configured native
Anthropic-shaped route, GPT-5.6-sol handles structure/evidence critique through
the configured Responses route, and DeepSeek V4 Pro handles relation/coverage
critique through the DeepSeek route. The configured gateway host is recorded as
transport identity; it is not evidence of an official upstream connection.

## Where to look

| Concern | Current source |
| --- | --- |
| Run input and path resolution | `runtime/job_spec.py` |
| CLI control plane | `reviewctl.py`, `runtime/control_plane.py` |
| Execution and resume | `runtime/runner.py`, `runtime/orchestrator.py` |
| Workspace and Registry | `services/job_workspace.py`, `services/artifact_registry.py` |
| Outline | `outline/v3_executor.py` |
| Review artifacts | `services/review_generation_service.py` |
| Validation | `validation/execution_service.py`, `validation/current_validation.py`, `validation/closure.py` |
| GUI and queue | `gui/app.py`, `services/workflow_facade.py`, `services/queue_service.py` |

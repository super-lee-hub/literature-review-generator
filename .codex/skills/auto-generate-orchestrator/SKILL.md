# auto-generate orchestrator

This repo-local Skill is an AI-native adapter to the current runtime. It does
not define a second execution engine or a second public control plane.

## Current contract

- Public execution input: `RuntimeJobSpec` from `runtime/job_spec.py`.
- Public runtime: `AgentRuntimeRunner` from `runtime/runner.py`.
- Bridge: `AgentRuntimeBridge` from `runtime/orchestrator.py`.
- Human control plane: `python -m reviewctl`, backed by
  `ReviewControlPlane`.
- GUI: `launch_gui.py` -> `gui/app.py` -> workflow facade/JobRunner -> the
  same `RuntimeJobSpec` and `AgentRuntimeRunner` contracts.

`JobRunRequest` may remain an internal adapter/data boundary where the current
code uses it. It is not an alternate public peer to `RuntimeJobSpec`.

## Operating pattern

1. Normalize the request into a validated `RuntimeJobSpec`.
2. Resolve spec-owned paths relative to the spec file.
3. Let `AgentRuntimeRunner` and `AgentRuntimeBridge` create or resume the
   durable workspace, Registry state, source bundle, and stage trace.
4. Keep stage execution, artifact publication, validation, and resume decisions
   inside the current Registry-backed runtime.
5. Report the durable job status and current next action through `reviewctl`
   semantics rather than inventing a parallel result format.

## Current stage contracts

- Stage 1 uses the current source identity, preprocessing, summary, and reuse
  contracts.
- Stage 2 is Outline Intelligence v3 only. Do not use or describe Outline v2.
- Stage 3 produces `review_draft` with `artifact_version=v3`,
  `citation_manifest` v3, and DOCX through the current review service.
- Validation uses `ValidationExecutionService`, `current_validation`,
  `adjudication_reuse`, and Registry-backed `closure` evidence.
- Free Mode uses `free_mode_intent_input/v1`, projects to `ReviewIntent`, and
  binds the Writer context before generation/replay.
- Concept Mode is currently disabled. Stale Concept Mode requests must be
  rejected; do not silently downgrade or implement provider calls for them.

## Hard prohibitions

- Do not bypass the Registry, current stage closure, or durable resume state.
- Do not fake provider receipts or treat a raw checkpoint as reuse authority.
- Do not silently promote intermediate Outline candidates.
- Do not treat report projections as canonical truth.
- Do not shell into removed legacy direct-flag flows.
- Do not redesign Stage 1, Free Mode semantics, validation adjudication
  authority, queue fencing/publication, `JobOutcome`, `CurrentArtifactSet`,
  repair/promotion, export admission, candidate DAG, critics, arbitration,
  adoption, pricing, or trading contracts in a documentation task.

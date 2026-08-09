# AI runtime bridge

The current AI-native chain is:

```text
Codex/OMX Skill
  -> RuntimeJobSpec
  -> AgentRuntimeRunner
  -> AgentRuntimeBridge
  -> current stage registry and durable workspace
```

The human control plane remains `python -m reviewctl`; the GUI starts at
`launch_gui.py` and `gui/app.py`. All three surfaces converge on the same
workspace, artifact Registry, stage closure, and resume authority.

## Durable bridge artifacts

The bridge records the normalized source input and execution mode in the
workspace, including `source_bundle.json` and `runtime_stage_trace.json`.
These are observable runtime artifacts, not substitutes for canonical stage
outputs or Registry authority.

## Current stages

- Stage 1 uses the current preprocessing, source identity, summary, and reuse
  contracts.
- Stage 2 is Outline Intelligence v3 only.
- Stage 3 uses `services/review_generation_service.py` for
  `review_draft` artifact version v3, `citation_manifest` v3, and DOCX.
- Validation uses `ValidationExecutionService`, `current_validation`,
  `adjudication_reuse`, and `closure` with Registry dependencies.

Concept Mode is currently disabled. A stale request fails validation at the
current boundary and is not converted into a provider call.

## Resume and authority

`AgentRuntimeRunner` delegates status, resume, reconciliation, and stage
execution to the durable runtime. A checkpoint, report projection, or bridge
trace alone cannot authorize completion, promotion, validation reuse, or
export. Those decisions require the current Registry-backed contracts.

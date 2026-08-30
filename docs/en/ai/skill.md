# Codex/OMX Skill contract

> Source of truth: `.codex/skills/auto-generate-orchestrator/SKILL.md`.

The repo-local Skill is an AI-native adapter to the same durable runtime used
by the GUI and CLI. It is not a peer control plane.

## Public runtime

```text
AI request
  -> RuntimeJobSpec
  -> AgentRuntimeRunner
  -> AgentRuntimeBridge
  -> Registry-backed stages and workspace
```

The human machine control plane is `python -m reviewctl`, backed by
`ReviewControlPlane`. `JobRunRequest` is an internal adapter where current code
uses it; `RuntimeJobSpec` is the public durable run specification.

## Current contracts

- Outline Intelligence v3 is the only production Outline path.
- Outline v3 routes candidate generation/arbitration to Claude Opus 5,
  structure/evidence critique to GPT-5.6-sol, and relation/coverage critique to
  DeepSeek V4 Pro according to `[OutlineModels]`; each route has its own
  transport, budget, binding, receipt, and replay identity.
- Stage 3 emits `review_draft` artifact version v3 and `citation_manifest` v3.
  Review is the stage contract; `Writer_API` is the per-section provider inside
  that stage, not a separate stage.
- Validation uses `ValidationExecutionService`, `current_validation`,
  `adjudication_reuse`, and Registry-backed `closure` evidence.
- Free Mode uses `free_mode_intent_input/v1`, the `ReviewIntent` projection,
  and Writer context binding.
- Concept Mode is currently disabled. Requests using that stale mode are
  rejected rather than silently downgraded.

Keep source intake, workspace creation, Registry publication, stage closure,
resume, validation, and runtime trace local and durable. Do not replace them
with shell commands or report-only projections.

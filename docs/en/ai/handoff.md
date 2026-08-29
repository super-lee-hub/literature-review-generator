# AI handoff

This document is for AI agents and new maintainers. Read current sources before
editing; historical migration and baseline documents retain their original
claims and evidence.

## Current reading order

1. `AGENTS.md`
2. `docs/en/runtime/truth-sources.md`
3. `docs/en/reference/feature-matrix.md`
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

Stage 1-specific work should then use `summary_schema.py`,
`preprocess/service.py`, and `services/summary_reuse.py`.

## Current truth

- The public CLI is `python -m reviewctl`.
- `RuntimeJobSpec` is the current durable run specification.
- `AgentRuntimeRunner` and `AgentRuntimeBridge` own the AI-native execution
  path.
- Outline Intelligence v3 is the only current Outline path.
- Outline role routing is node-level and fail-closed: Claude Opus 5 generates
  and arbitrates, GPT-5.6-sol critiques structure/evidence, and DeepSeek V4 Pro
  adjudicates relations/coverage under `[OutlineModels]`.
- Stage 3 truth is `review_draft` v3 plus `citation_manifest` v3 and DOCX.
  Review is the stage; `Writer_API` is its per-section provider.
- Validation truth is owned by `ValidationExecutionService` and its
  Registry-backed closure/adjudication authority.
- `main.py` is a small compatibility-free shim into `reviewctl`, not the old
  orchestration CLI.
- Concept Mode is currently disabled and stale requests are rejected.

## Safety boundaries

Do not bypass the Registry, fake provider receipts, turn report projections into
canonical truth, silently promote intermediate Outline candidates, restore
Outline v2, or change frozen Stage 1, Free Mode, validation authority, queue,
repair, promotion, export, or publication contracts without a deterministic
regression proving a real defect.

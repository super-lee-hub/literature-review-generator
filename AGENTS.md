# AGENTS.md

This is the current AI/developer handoff for `auto-generate`. The repository
is a local, corpus-controlled, full-text-first literature analysis and
review-writing workbench with PDF-folder, Zotero, GUI, CLI, and repo-local
Codex/OMX surfaces.

## Current execution truth

The public machine control plane is `python -m reviewctl`:

```text
reviewctl.py
  -> runtime/control_plane.py / ReviewControlPlane
  -> RuntimeJobSpec
  -> runtime/runner.py / AgentRuntimeRunner
  -> runtime/orchestrator.py / AgentRuntimeBridge
  -> Registry-backed stage executors
```

The GUI starts at `launch_gui.py` and `gui/app.py`, then uses the workflow
facade/JobRunner and the same `RuntimeJobSpec` and `AgentRuntimeRunner`
contracts. The repo-local Skill is an AI-native adapter to the same runtime.

`main.py` is a small compatibility-free shim into `reviewctl`. It is not the
current orchestration engine.

## Recommended reading order

1. `AGENTS.md`
2. `TRUTH_SOURCES.md` -> `docs/en/runtime/truth-sources.md`
3. `FEATURE_MATRIX.md` -> `docs/en/reference/feature-matrix.md`
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
18. Stage 1 sources such as `summary_schema.py`, `preprocess/service.py`, and
    `services/summary_reuse.py`

Historical baselines and migration reports preserve their original snapshots;
they are not current execution instructions.

## Frozen production contracts

- Stage 1 summaries and reuse remain the canonical Stage 1 truth.
- Outline Intelligence v3 is the only current production Outline path.
- Stage 3 produces `review_draft` artifact version v3, `citation_manifest` v3,
  and DOCX through the current review service.
- Validation uses `ValidationExecutionService`, current validation, Registry
  dependency closure, and the provisional/durable adjudication authority.
- Queue fencing/publication, `JobOutcome`, `CurrentArtifactSet`, repair,
  promotion, and export remain fail-closed current contracts.
- Free Mode uses `free_mode_intent_input/v1`, `ReviewIntent`, and Writer
  context binding. Concept Mode is currently disabled; stale requests are
  rejected.

Do not bypass the Registry or stage closure, fake provider receipts, silently
promote intermediate Outline candidates, restore Outline v2, or reintroduce
the removed direct-flag CLI as a current surface.

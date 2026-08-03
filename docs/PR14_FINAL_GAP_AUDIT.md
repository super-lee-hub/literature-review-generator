# PR #14 Platform-Hardening Audit

Date: 2026-08-03
Repository: `super-lee-hub/literature-review-generator`
Branch: `codex/platform-hardening-outline-v3`
Base: `main`

This document records the current implementation and fresh local verification
for the PR #14 platform-hardening scope. It is evidence-bounded: no live
provider execution is claimed, and the two user-owned PPH ZIP files in the
workspace are outside the audit boundary.

Status vocabulary: `INTEGRATED`, `E2E_VERIFIED`, `LIVE_VERIFIED`, `BLOCKED`,
`NOT_RUN`.

## Current implementation matrix

| Contract | Current implementation | Fresh evidence | Status |
| --- | --- | --- | --- |
| Current-only configuration and one typed retry policy | `services/settings.py`, `services/configuration_service.py`, `config.ini.example`, setup/UI callers | configuration, setup-wizard, free-mode, and architecture tests | INTEGRATED |
| Strict Artifact Registry V2 and current artifact validators | `services/artifact_registry.py`, `runtime/artifact_validators.py`, `runtime/reconcile.py` | 26 current artifact types dispatched at registry, runner, and reconcile gates; placeholder negative tests fail closed | E2E_VERIFIED |
| Bound provider runtime and complete request estimation | `runtime/provider_runtime.py`, `runtime/provider_context.py`, `ai_interface.py` | provider-runtime and current Stage 1/Review/Validation tests | E2E_VERIFIED |
| Provider receipts and expected-call closure | `runtime/provider_receipt_closure.py`; Stage 1, Outline, Review, Validation services | request pre-binding, normalized/output/artifact/node closure, provider closure tests and full-chain readback | E2E_VERIFIED |
| Explicit versioned Outline adoption transaction | `outline/adoption_transaction.py`, `runtime/control_plane.py`, `runtime/export_bundle.py` | adoption identity/pointer, semantic outline, control-plane, and full-chain tests | E2E_VERIFIED |
| Exact execution binding, replay, and invalidation | `runtime/outline_v3_dag.py`, `outline/v3_executor.py`, `runtime/outline_v3_replay.py` | executor two-run, summary/candidate/review-intent invalidation, exact replay and closure tests | E2E_VERIFIED |
| Typed quality and full-decision stability gates | `outline/v3_models.py`, `outline/v3_executor.py` | quality fields, candidate/summary/shard/order/replay variants, failed-check diagnostics | E2E_VERIFIED |
| Direct and Zotero Stage 1 source boundary | `runtime/source_intake.py`, `services/stage1_analysis_service.py` | direct generation/reuse, source identity/intake, multimodal, Zotero, and current runtime E2E tests | E2E_VERIFIED |
| Durable per-section Review artifacts and citation spans | `services/review_generation_service.py`, citation catalog, DOCX projection | current review, citation, DOCX, and full-chain tests | E2E_VERIFIED |
| Current Validation execution service | `validation/execution_service.py`, `validation/review_validation_pipeline.py`, `reviewctl.py` | explicit constructor, pre/post transport bindings, validation execution/bridge, closure, and full-chain tests | E2E_VERIFIED |
| Validation receipts, claim-batch recovery, and status split | validation execution service, `reviewctl validate`, read-only `validation-status` | validation closure/run-result and control-plane tests | E2E_VERIFIED |
| Semantic repair revalidation and versioned promotion | `validation/semantic_revalidation.py`, `validation/repair_transaction.py`, repair integration | typed issue/action/patch models, structural closure, promotion, Week 4, and current repair tests | E2E_VERIFIED |
| Queue canonical output root, leases, and cancellation | `services/queue_service.py`, job runner, queue control plane | persistent queue, cross-process claim/heartbeat/expiry recovery, current queue, and cancellation tests | E2E_VERIFIED |
| GUI canonical lifecycle state | `gui/app.py` and current runtime control plane | GUI controller tests; Playwright surface is excluded when unavailable | INTEGRATED |
| Trust-bound export and forensic attestation | `runtime/export_bundle.py`, control plane | export-bundle and full-chain export readback | E2E_VERIFIED |
| True production-shaped full chain | `AgentRuntimeRunner`, explicit adoption, current validation, closure, export | three-PDF E2E with no manually registered validation artifact | E2E_VERIFIED |

## Verification readback

The following checks were run against the current working tree:

- `python -m pytest --collect-only -q`: 674 tests collected.
- `python -m compileall -q .`: passed.
- `python -m pyright`: 0 errors, 0 warnings, 0 informations.
- Targeted Ruff checks over changed production and test files: passed.
- `git diff --check`: passed; only normal Git LF-to-CRLF working-copy warnings
  were reported.
- `python -m pytest -q --strict-markers -m "not live_api and not playwright and not heavy_ocr"`:
  652 passed, 22 deselected.
- Focused Outline/Validation/Queue/Repair/Architecture/runtime group: 27
  passed, including the current three-PDF full-chain E2E.
- Placeholder `{}` and `{"hello":"world"}` negative checks fail closed for
  `final_outline`, `coverage_audit`, and `ValidationRunResultV1`.

No live-provider call was made. Playwright tests are not represented as
offline-pass evidence. Remote branch SHA, CI, and PR state are read back only
after the final allowlist push.

## Non-claims

- No `LIVE_VERIFIED` claim is made: no external provider call was required for
  this local acceptance run.
- The two workspace ZIPs named `PPH_五份综述_20260731.zip` and
  `PPH_完整资料包_20260731.zip` were not read, modified, staged, committed,
  ignored, or used as fixtures.

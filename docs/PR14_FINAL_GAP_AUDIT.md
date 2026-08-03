# PR #14 Platform-Hardening Audit

Date: 2026-08-03
Repository: `super-lee-hub/literature-review-generator`
Branch: `codex/platform-hardening-outline-v3`
Base: `main`

This document records the current implementation and fresh local verification
for the PR #14 platform-hardening scope. It is intentionally evidence-bounded:
external-provider execution is not claimed as live verification, and the two
user-owned PPH ZIP files in the workspace are outside the audit boundary.

Status vocabulary: `INTEGRATED`, `E2E_VERIFIED`, `LIVE_VERIFIED`, `BLOCKED`,
`NOT_RUN`.

## Current implementation matrix

| Contract | Current implementation | Fresh evidence | Status |
| --- | --- | --- | --- |
| Current-only configuration and one typed retry policy | `services/settings.py`, `services/configuration_service.py`, `config.ini.example`, setup/UI callers | configuration, setup-wizard, free-mode, and architecture tests | INTEGRATED |
| Strict Artifact Registry V2 and current artifact validators | `services/artifact_registry.py`, `runtime/reconcile.py` | registry transactions, current architecture, export, and full-chain tests | E2E_VERIFIED |
| Bound provider runtime and complete request estimation | `runtime/provider_runtime.py`, `runtime/provider_context.py`, `ai_interface.py` | provider-runtime and current Stage 1/Review/Validation tests | E2E_VERIFIED |
| Provider receipts and expected-call closure | `runtime/provider_receipt_closure.py`; Stage 1, Outline, Review, Validation services | provider closure tests and full-chain closure readback | E2E_VERIFIED |
| Explicit Outline adoption transaction | `outline/adoption_transaction.py`, `runtime/control_plane.py` | semantic outline, control-plane, and full-chain tests | E2E_VERIFIED |
| Exact replay and invalidation | `runtime/outline_v3_dag.py`, Outline V3 executor/replay store | replay and semantic execution tests | E2E_VERIFIED |
| Metamorphic Outline stability audit | Outline V3 executor stability node | semantic execution and architecture tests | E2E_VERIFIED |
| Direct and Zotero Stage 1 source boundary | `runtime/source_intake.py`, `services/stage1_analysis_service.py` | direct generation/reuse, source identity/intake, multimodal, `test_current_stage1_zotero_e2e.py`, and current runtime E2E tests | E2E_VERIFIED |
| Durable per-section Review artifacts and citation spans | `services/review_generation_service.py`, citation catalog, DOCX projection | current review, citation, DOCX, and full-chain tests | E2E_VERIFIED |
| Current Validation execution service | `validation/execution_service.py`, `validator.py`, `reviewctl.py` | validation execution/bridge, current repair, closure, and full-chain tests | E2E_VERIFIED |
| Validation receipts, claim-batch recovery, and status split | validation execution service, `reviewctl validate`, read-only `validation-status` | validation closure/run-result and control-plane tests | E2E_VERIFIED |
| Semantic repair revalidation and quarantine/promotion | `validation/semantic_revalidation.py`, repair transaction/integration | repair transaction, Week 4, and current validation repair tests | E2E_VERIFIED |
| Queue canonical output root and cancellation | `services/queue_service.py`, job runner, queue control plane | current queue/persistent queue and cancellation tests | E2E_VERIFIED |
| GUI canonical lifecycle state | `gui/app.py` and current runtime control plane | GUI controller tests; Playwright surface was collected but skipped when unavailable | INTEGRATED |
| Trust-bound export and forensic attestation | `runtime/export_bundle.py`, control plane | export-bundle and full-chain export readback | E2E_VERIFIED |
| True production-shaped full chain | `AgentRuntimeRunner`, explicit adoption, current validation, closure, export | `tests/test_current_runtime_full_e2e.py` passed with three generated PDFs and no manually registered validation artifact | E2E_VERIFIED |

## Verification readback

The following checks were run against the current working tree:

- `pytest --collect-only -q`: 660 tests collected after the final test-file
  rename and Zotero Stage 1 E2E addition.
- Broad `compileall`: passed.
- `git diff --check`: passed; Git only reported the repository's normal
  LF-to-CRLF working-copy warnings.
- Production architecture scan over the current source roots: zero findings.
- Targeted current architecture/runtime group: 99 passed.
- Remaining non-UI batches: 121 + 116 + 206 + 7 passed.
- Validation/free-mode/current runtime boundary group: 15 passed.
- Current full-chain E2E: passed, including explicit adoption, resume,
  clean validation, validation provider closure, and canonical export.
- Playwright GUI suite: 22 skipped because the optional Playwright runtime was
  unavailable.

The aggregate full non-Playwright command exceeded the local five-minute
execution limit without producing a test failure. It is not reported as a
full-suite pass; the bounded batch results above are the evidence used for
this audit.

## Non-claims

- No `LIVE_VERIFIED` claim is made: no external provider call was required for
  this local acceptance run.
- CI state, branch SHA, PR state, and remote content must be read back after
  the final commit/push; this document does not substitute for that remote
  verification.
- The two workspace ZIPs named `PPH_五份综述_20260731.zip` and
  `PPH_完整资料包_20260731.zip` were not read, modified, staged, committed,
  ignored, or used as fixtures.

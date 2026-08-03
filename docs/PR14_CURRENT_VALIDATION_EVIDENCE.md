# PR14 Current Validation Evidence

Date: 2026-08-03 (Asia/Shanghai)

Repository: `super-lee-hub/literature-review-generator`

Branch: `codex/platform-hardening-outline-v3`

This document records the current implementation evidence after the baseline
gap audit in `PR14_GAP_CLOSURE_PLAN.md`. It does not claim that Draft PR #14
has been merged or that live-provider coverage has been completed.

## Implemented current-chain changes

| Area | Current evidence | Disposition |
| --- | --- | --- |
| Stage 1 | `services/stage1_analysis_service.py` is called by production `analyze` for raw PDF/Zotero `SourceBundle` input; it preprocesses, builds the Reader request, validates the canonical summary, writes paper artifacts, and records provider receipts. | INTEGRATED |
| Outline v3 | Relation adjudication is provider-bound and fail-closed; candidate generation receives evidence views and confirmed relations; actual candidate content, critique inputs, arbitration inputs, quality gate, coverage audit, and full-decision stability variants are persisted. | INTEGRATED |
| Exact replay/invalidation | Every succeeded node persists the exact execution binding. Summary, candidate-count, review-intent, route, prompt, schema, quality-gate, and relevant-runtime changes invalidate the affected node and descendants while preserving unaffected upstream nodes. Replay also includes the binding hash and closes response, normalized, registered-artifact, node, and receipt identities. | INTEGRATED |
| Section evidence | Section packets contain paper assignments, evidence items, source-summary/view hashes, field-level evidence, relation evidence, and retrieval provenance. Coverage and stability audits are computed from those packets. | INTEGRATED |
| Adoption | An outline without explicit adoption ends at `ready_for_adoption`; adoption creates a content-hash versioned identity plus `outline-v3:adoption:current` pointer/role, and the current review path resolves that pointer without implicit promotion. | INTEGRATED |
| Writer review | `services/review_generation_service.py` builds the citation-reference catalog before Writer calls, sends per-section evidence packets, pre-binds the exact request, requires structured citation tokens/spans, and produces durable review and manifest artifacts. | INTEGRATED |
| Validation and repair | `ValidationExecutionService` has the explicit job/attempt/workspace/Registry/settings/input/provider/cancellation/logger constructor; it binds request identity before transport and normalized/output artifact identity after transport. Repair uses typed issues/actions/patches, `RepairStructuralClosure`, and versioned promotion artifacts without canonical overwrite. | INTEGRATED |
| Queue | GUI progress reads the public runtime-list contract; CLI exposes JSON list/add/run/retry/cancel/remove/export/import over `PersistentQueueService`; cross-process locks, revision/CAS leases, heartbeat, expiry/crash recovery, and cancellation are explicit. | INTEGRATED |

## Focused verification

The current architecture/runtime group passed 27 tests, including:

```text
tests/test_current_runtime_full_e2e.py
tests/test_outline_v3_executor_invalidation.py
tests/test_outline_v3_dag_replay.py
tests/test_runtime_validation_bridge.py
tests/test_outline_adoption_identity.py
tests/test_queue_claim_leases.py
tests/test_repair_promotion.py
tests/test_current_artifact_validators.py
tests/test_pr14_current_architecture.py
27 passed
```

The full-chain test uses three raw PDFs, the current `AgentRuntimeBridge`,
current Stage 1, Outline v3 with explicit adoption, Writer review generation,
citation-manifest spans, validation closure, export, and finalization. Provider
responses are deterministic test adapters at the real provider boundary; this
does not substitute for a live API run.

## Required final gates

The following local gates passed:

```text
python -m compileall -q .
python -m pyright
python -m pytest --collect-only -q
python -m pytest -q --strict-markers -m "not live_api and not playwright and not heavy_ocr"
git diff --check
```

Results: `compileall` passed; `pyright` reported `0 errors, 0 warnings, 0
informations`; collection reported `674 tests collected`; the strict suite
reported `652 passed, 22 deselected`; and `git diff --check` passed with only
Git's normal LF-to-CRLF conversion warnings for touched files. PR #14 must
remain Draft/open/unmerged; the two user-owned untracked ZIP files must remain
unmodified and unstaged. Remote SHA and CI are recorded after the final
allowlist push.

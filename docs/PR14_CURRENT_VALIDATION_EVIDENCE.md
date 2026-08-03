# PR14 Current Validation Evidence

Date: 2026-08-03 (Asia/Shanghai)

Repository: `super-lee-hub/literature-review-generator`

Branch: `codex/platform-hardening-outline-v3`

This document records the current implementation evidence after the baseline
gap audit in `PR14_GAP_CLOSURE_PLAN.md`. It is not a claim that the Draft PR
has been merged or that live-provider coverage has been completed.

## Implemented current-chain changes

| Area | Current evidence | Disposition |
| --- | --- | --- |
| Stage 1 | `services/stage1_analysis_service.py` is called by the production `analyze` stage for raw PDF/Zotero `SourceBundle` input; it preprocesses, builds the Reader request, validates the canonical summary, writes paper artifacts, and records provider receipts. | INTEGRATED |
| Outline v3 | Relation adjudication is provider-bound and fail-closed; every relation must be confirmed or rejected; candidate generation receives evidence views and confirmed relations; invalid candidate output and arbitration IDs block. | INTEGRATED |
| Section evidence | Section packets contain paper assignments, evidence items, source-summary/view hashes, field-level evidence, relation evidence, and retrieval provenance. Coverage and stability audits are computed from those packets. | INTEGRATED |
| Adoption | An outline without explicit adoption ends at `ready_for_adoption`; the current review path requires a ready adoption record and never promotes implicitly. | INTEGRATED |
| Writer review | `services/review_generation_service.py` builds the citation-ref catalog before Writer calls, sends per-section evidence packets, requires structured citation tokens, and produces the durable review draft and citation manifest. | INTEGRATED |
| Validation and repair | Repair planning binds the ready Registry dependency bundle; apply is report-first, mapping-first, preserves READY inputs, performs targeted revalidation, and writes only quarantined derived artifacts after a pass. | INTEGRATED |
| Queue | GUI progress reads the public runtime-list contract; CLI exposes JSON list/add/run/retry/cancel/remove/export/import over `PersistentQueueService`; cancellation is acknowledged at pending and worker boundaries. | INTEGRATED |

## Focused verification

The following current-architecture tests passed locally:

```text
python -m pytest -q tests/test_current_runtime_full_e2e.py
1 passed

python -m pytest -q tests/test_current_stage1_generation.py tests/test_current_stage1_multimodal_generation.py tests/test_current_stage1_provider_fallback.py
5 passed

python -m pytest -q tests/test_current_review_generation.py
2 passed

python -m pytest -q tests/test_outline_v3_semantic_execution.py
5 passed

python -m pytest -q tests/test_current_validation_repair_e2e.py
3 passed

python -m pytest -q tests/test_current_queue_e2e.py
2 passed

python -m pytest -q tests/test_current_runtime_full_e2e.py tests/test_current_stage1_generation.py tests/test_current_stage1_multimodal_generation.py tests/test_current_stage1_provider_fallback.py tests/test_current_review_generation.py tests/test_outline_v3_semantic_execution.py tests/test_current_validation_repair_e2e.py tests/test_current_queue_e2e.py
18 passed
```

The exact required focused commands also passed independently:

```text
tests/test_current_runtime_full_e2e.py       1 passed
tests/test_current_stage1_generation.py      3 passed
tests/test_current_review_generation.py      2 passed
tests/test_outline_v3_semantic_execution.py  5 passed
tests/test_current_validation_repair_e2e.py  3 passed
tests/test_current_queue_e2e.py              2 passed
```

The full-chain test uses three raw PDFs, the current `AgentRuntimeBridge`,
current Stage 1, Outline v3 with explicit adoption, Writer review generation,
citation-manifest spans, validation closure, export, and finalization. Provider
responses are deterministic test adapters at the real provider boundary; this
does not substitute for a live API run.

## Required final gates

The final local gate passed:

```text
python -m compileall -q .
python -m pyright
python -m pytest --collect-only -q
python -m pytest -q --strict-markers -m "not live_api and not playwright and not heavy_ocr"
git diff --check
```

Results: `compileall` passed; `pyright` reported `0 errors, 0 warnings, 0
informations`; collection reported `658 tests collected`; the strict suite
reported `636 passed, 22 deselected`; and `git diff --check` passed (with only
Git's LF-to-CRLF conversion warnings for touched files). PR #14 must remain
Draft/open/unmerged; the two user-owned untracked ZIP files must remain
unmodified and unstaged.

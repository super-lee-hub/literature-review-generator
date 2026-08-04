# PR14 Current Validation Evidence

Date: 2026-08-04 (Asia/Shanghai)
Repository: `super-lee-hub/literature-review-generator`
Branch: `codex/platform-hardening-outline-v3`
Scope: current-only validation and its production caller
Code verification commit: `3166a73e4b9ac036570a58bba899ebab579ba162`

## Current execution evidence

`AgentRuntimeBridge.build_validation_service()` constructs an explicit
`ValidationExecutionService` from the job ID, attempt ID, workspace, current
Registry records, settings, provider boundary, cancellation checker, and
logger. The service does not import the historical top-level `validator`
module. `reviewctl validate` executes this service; `validation-status` only
reads the durable closure.

The service now persists and checks:

- exact review-draft, citation-manifest, paper/evidence, and visual input
  identities;
- recursive provider request-budget estimates before transport;
- pre-transport job/attempt/stage/node/call/prompt/input/config/schema binding;
- provider response, normalized output, artifact payload, artifact envelope,
  node output, Registry file, and replay hash domains separately;
- expected-call closure including missing, stale, failed, incomplete,
  unexpected, out-of-scope, usage, retry, and hash mismatches;
- immutable `ValidationRunResultV1` output and exact Registry dependency closure;
- report, manual-review, alignment, and completion files as projections only.

Zero validated claims are `needs_review`; a registered JSON file or a closure
inspection cannot turn an unexecuted validation into `clean`.

## Fresh tests

| Evidence | Result |
| --- | --- |
| `tests/test_current_production_full_e2e.py` | 1 passed; runner → current Stage 1 → Outline → explicit adoption → Review → current Validation → Export → Attestation |
| `tests/test_current_validation_repair_e2e.py` | 1 passed; real control-plane repair revalidation, DOCX rebuild, audit promotion, and atomic `CurrentArtifactSet` switch |
| `tests/test_outline_v3_full_stability.py` | 2 passed; order-sensitive full-decision perturbation and blocking-critic quarantine |
| `tests/test_queue_multiprocess_leases.py` | 2 passed; Windows `spawn` lease winner/fence and stale-worker publication rejection |
| current repair/GUI/queue focused group | **36 passed** |
| `tests/test_runtime_validation_bridge.py`, current validation/repair tests | passed in the focused production validation group |
| `tests/test_provider_receipt_closure.py` | out-of-scope receipt blocks closure |
| Full offline gate: `python -m pytest -q --strict-markers -m "not live_api and not playwright and not heavy_ocr"` | **678 passed, 22 deselected** |
| Collection: `python -m pytest --collect-only -q` | **700 tests collected** |
| `python -m compileall -q .` | passed |
| `python -m pyright` | **0 errors, 0 warnings, 0 informations** |
| current test-config doctor | `ok=true`, 0 provider network calls; `warn` only for pre-existing stale locks |

The production E2E injects deterministic responses at the configured provider
transport boundary. It does not use Outline fixture mode, manually register a
final validation artifact, write completion status, or pass forged completion/
closure claims to export. It is `E2E_VERIFIED`, not `LIVE_VERIFIED`.

## Remaining validation limits

- No external live-provider call was made; live verification is blocked/not
  run for this offline acceptance pass.
- The consolidated negative production matrix (missing receipt, malformed
  relation, stale adoption, section crash recovery, cancellation, semantic
  repair failure, and export registration failure) is not yet one E2E suite;
  individual focused failure tests exist. Successful control-plane repair
  promotion is now covered separately by the current repair E2E.
- Playwright and heavy OCR were excluded by the offline gate.
- Remote SHA, CI, and PR state are recorded only after the final push.

The user-owned ZIP files `PPH_五份综述_20260731.zip` and
`PPH_完整资料包_20260731.zip` are not validation inputs and remain untouched.

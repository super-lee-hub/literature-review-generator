# PR14 Current Validation Evidence

Date: 2026-08-04 (Asia/Shanghai)
Repository: `super-lee-hub/literature-review-generator`
Branch: `codex/platform-hardening-outline-v3`
Code/test verification commit: `8464b5934ba9dde03de46e0723347728a6a4c4d5`

## Current execution evidence

`AgentRuntimeBridge.build_validation_service()` constructs an explicit
`ValidationExecutionService` from the job/attempt identity, workspace, current
Registry records, settings, provider boundary, cancellation checker, and
logger. The current runner executes this service; status inspection only reads
the durable closure.

The current path persists and checks:

- exact review, citation, paper/evidence, and visual input identities;
- recursive provider request-budget estimates before transport;
- pre-transport job/attempt/stage/node/call/prompt/input/config/schema binding;
- separate response, normalized-output, artifact, envelope, node-output,
  Registry-file, and replay hash domains;
- expected-call closure including missing, stale, failed, incomplete,
  unexpected, out-of-scope, usage, retry, and hash mismatches;
- immutable `ValidationRunResultV1` plus exact Registry dependency closure;
- stage-indexed provider closure derived from the durable requested-stage spec;
- reports and manual-review projections without treating closure inspection as
  validation execution.

## Fresh tests and gates

| Evidence | Result |
|---|---|
| Complete focused PR14/runtime group | `58 passed` |
| Legacy registry/validation/repair compatibility group | `51 passed` |
| Strict offline full gate | `688 passed, 22 deselected` from `710 collected` |
| `python -m compileall -q .` | passed |
| `python -m pyright` | `0 errors, 0 warnings, 0 informations` |
| Current production-shaped three-PDF chain | passed; explicit adoption, current validation, export, and attestation |
| Current repair control-plane E2E | passed; revalidation, DOCX rebuild, audit promotion, and atomic current-set switch |
| Full stability and replay | passed; full perturbation path and zero-transport exact replay |
| Queue Windows `spawn` fencing | passed; stale-worker publication rejected |

The production-shaped chain injects deterministic responses at the configured
transport boundary. It is `E2E_VERIFIED`, not `LIVE_VERIFIED`.

## Remaining limits

- No external live-provider call was made.
- Playwright and heavy OCR were excluded by the offline gate.
- Focused negative tests cover the requested boundaries, but are not one
  monolithic failure-chain suite.
- The two user-owned root ZIP files named in the task remain untouched and are
  not validation inputs.

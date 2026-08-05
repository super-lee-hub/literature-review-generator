# PR14 Current Validation Evidence

Date: 2026-08-05 (Asia/Shanghai)
Repository: `super-lee-hub/literature-review-generator`
Branch: `codex/platform-hardening-outline-v3`
Code/test verification: refreshed at the final pre-publication branch tip; the
final commit SHA is recorded in the PR description and delivery report.

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
- durable stage-plan policy for `run_all`: validation-enabled runs request
  analyze/outline/review/validate; optional validation-disabled runs request
  analyze/outline/review but still require the current artifact set;
- derivation and outline-only jobs cannot become canonical-ready without a
  current artifact set, and review requires explicit adoption where applicable;
- typed `CurrentArtifactSetV1` target/version checks at both switch and resolve;
  optional disabled validation uses `ValidationDispositionV1/v1` plus a
  zero-call validation closure, while canonical-unvalidated export repeats its
  policy and warning in the ZIP provenance;
- reports and manual-review projections without treating closure inspection as
  validation execution.

## Fresh tests and gates

| Evidence | Result |
|---|---|
| Focused boundary regressions | passed; queue, closure, repair, export, Stage 1, validation, GUI-controller, adoption, multimodal, and Outline groups |
| Strict offline full gate | not accepted as an aggregate: `752 selected, 22 deselected` from `774 collected`; one run timed out after 30 minutes and a second exited 1 before an aggregate summary |
| `python -m compileall -q .` | passed |
| `python -m pyright` | `0 errors, 0 warnings, 0 informations` |
| Current production-shaped three-PDF chain | passed; explicit adoption, current validation, export, and attestation |
| Current repair control-plane E2E | passed; revalidation, DOCX rebuild, audit promotion, and atomic current-set switch |
| Full stability and replay | passed; full perturbation path and zero-transport exact replay |
| Queue Windows `spawn` fencing | passed; stale-worker publication rejected |
| Current runtime-shaped E2E | passed; current optional-policy file `3 passed` |
| Current production-shaped E2E | passed; verified and optional `canonical_unvalidated` paths |

Outline stability admission is persisted before transport. For candidate count
`c`, core calls are `c+5`; with the default `c=5`, `off/smoke/full` estimate
10/20/60 provider calls. Smoke contains one non-replay perturbation plus a
zero-transport exact replay; full contains five non-replay perturbations plus
that replay. The default policy is 24 calls and a 5,000,000 total-token hard
ceiling; default pricing is unknown and the monetary ceiling is disabled until
provider/model-bound rates are supplied. Critic retry preserves completed
candidate nodes and reruns the failed critic plus its downstream closure.

The production-shaped chain injects deterministic responses at the configured
transport boundary. It is `E2E_VERIFIED`, not `LIVE_VERIFIED`.

## Remaining limits

- No external live-provider call was made.
- Playwright and heavy OCR were excluded by the offline gate.
- Focused negative tests cover the requested boundaries, but are not one
  monolithic failure-chain suite.
- The ZIP files are absent from the committed diff and remote branch. The local
  operator reports that they were not read or staged; remote GitHub evidence
  cannot independently verify local read access.

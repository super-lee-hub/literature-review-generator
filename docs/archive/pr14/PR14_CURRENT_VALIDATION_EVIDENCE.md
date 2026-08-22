# PR14 Current Validation Evidence

Date: 2026-08-08 (Asia/Shanghai)
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
- typed `stage1_reusable_summary_manifest/v1` source-manifest validation and
  canonical one-item-array `summary_file` payloads for per-paper reuse sources;
- Registry-detached typed-manifest verification that still requires the
  referenced source summary, provider closure, and provider ledger when calls
  occurred to remain available and hash-valid;
- path-independent multimodal Stage 1 equality over visual content bytes,
  page/range/region, type, rank, policy, caption/context semantics, score,
  dedupe identity, and bundle counts while excluding machine-local paths;
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
| Changed Stage 1 generation/reuse module | passed: `22 passed` in `185.84s`; supported preprocess settings, moved-path multimodal reuse, visual invalidation, and missing typed-manifest authority blobs |
| Consolidated frozen-contract suite | passed: `204 passed` in `1844.75s`; Stage 1 trust boundaries, Queue/Registry publication, JobOutcome projection, stage-specific zero-call, Outline v3, and architecture guards |
| Validation policy/parity suite | passed: `13 passed` in `438.33s`; direct, CLI, GUI, queue, resume, required, findings, and optional-validation behavior |
| Strict offline full gate | passed: `843 passed, 22 deselected` from `865 collected` in `3251.61s` (`54:11`) |
| `python -m compileall -q .` | passed |
| `python -m pyright` | `0 errors, 0 warnings, 0 informations` |
| `python -m reviewctl doctor --config config.ini.example` | exit `0`, `ok=true`, read-only, zero provider calls; `status=warn` only for pre-existing stale locks |
| `git diff --check` | passed on the final working tree and explicit staged diff |
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

`3251.61s` is the local pytest duration. The final GitHub Actions run/job IDs,
conclusion, and CI duration are reported separately in the PR description for
the final remote SHA; the local duration is not reused as CI timing evidence.

## Remaining limits

- No external live-provider call was made.
- Playwright and heavy OCR were excluded by the offline gate.
- Multi-host publication and fencing were not run.
- Focused negative tests cover the requested boundaries, but are not one
  monolithic failure-chain suite.
> The ZIP files are absent from the committed diff and remote branch. The local operator reports that they were not read or staged; remote GitHub evidence cannot independently verify local read access.

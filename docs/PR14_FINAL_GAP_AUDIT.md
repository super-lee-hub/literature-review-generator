# PR #14 Final Gap Audit at Current Head

Date: 2026-08-03

Repository: `super-lee-hub/literature-review-generator`

Branch: `codex/platform-hardening-outline-v3`

Audited HEAD: `1e7851b7282196b323e13992167eade2be250ebe`

Base: `origin/main` at `ecac15976ebb3b6ee754fe5c0dfe44efacd72e9a`

This is a fact audit of the current checkout before the next remediation pass. It is
not an implementation claim. The status vocabulary below is intentionally limited to
the values required by the PR #14 remediation contract:

`NOT_IMPLEMENTED`, `IMPLEMENTED_ONLY`, `INTEGRATED`, `E2E_VERIFIED`,
`LIVE_VERIFIED`, `REGRESSED`, `BLOCKED`.

## Evidence boundary

The audit was produced from the current branch and current production source tree,
not by copying `PR14_GAP_CLOSURE_PLAN.md`. The following facts were read back before
this document was written:

- `HEAD` and `origin/codex/platform-hardening-outline-v3` both resolve to
  `1e7851b7282196b323e13992167eade2be250ebe`.
- `origin/main` resolves to
  `ecac15976ebb3b6ee754fe5c0dfe44efacd72e9a`; the branch is 24 commits ahead and
  is not behind.
- PR #14 is `OPEN`, `isDraft=true`, `mergedAt=null`, with base `main` and the
  expected head branch.
- The current GitHub Windows `test` check is successful, but that green check does
  not by itself prove live API execution, Playwright execution, heavy OCR execution,
  or the production full-chain contract below.
- The only untracked workspace entries at audit time are the two user-owned PPH ZIP
  files. They are explicitly out of scope and are not read, staged, modified,
  renamed, deleted, ignored, or used as fixtures.

## Current implementation matrix

| Requirement | Current component | Production caller | Current test | Status | Remaining work |
| --- | --- | --- | --- | --- | --- |
| Direct PDF Stage 1 current service | `services/stage1_analysis_service.py` | runtime Stage 1 bridge/orchestrator | `tests/test_current_stage1_generation.py` | INTEGRATED | Prove mixed reuse, crash resume, and full-run control-plane execution. |
| Current multimodal Stage 1 boundary | `services/stage1_analysis_service.py`, visual resolver | Stage 1 service | `tests/test_current_stage1_multimodal_generation.py` | INTEGRATED | Add capability-policy failure/degradation and missing-visual negative paths. |
| Zotero report-to-PDF identity and attachment resolution | `runtime/source_intake.py`, source identity services | runtime source intake | `tests/test_runtime_source_intake.py` | IMPLEMENTED_ONLY | Add the required current-only Zotero Stage 1 production E2E and all fail-closed identity cases. |
| One authoritative retry policy | `services/settings.py`, configuration service, example config | settings/configuration callers | existing configuration tests | REGRESSED | Remove `[Retry_Settings]` and `[Stage2_Retry]`; expose one typed current policy and explicit obsolete-key errors. |
| One authoritative provider parameter source | `config.ini`, `config.ini.example`, `ai_interface.py`, `validator.py` | legacy and current provider callers | `tests/test_configuration_service.py`, API tests | REGRESSED | Move provider-owned limits/timeouts to provider sections and remove production `[API_Parameters]` duplication. |
| Production fixture isolation | `services/settings.py`, `runtime/orchestrator.py`, example config | runtime outline path | `tests/test_current_runtime_full_e2e.py` | REGRESSED | Remove fixture switch from production configuration and fail closed if a fixture provider reaches production. |
| Registry current-only schema | `services/artifact_registry.py`, `runtime/stage_terminal.py` | runtime and services | `tests/test_artifact_registry.py`, registry transaction tests | REGRESSED | Accept only registry v2, remove v1/legacy constructor and field-shape reads; retain any migration only as offline tooling if evidence requires it. |
| Summary current-only input shape | `summary_schema.py` | Stage 1 and validation callers | `tests/test_summary_schema.py` | REGRESSED | Stop normal runtime normalization of legacy shapes; keep formal current output and isolate any historical conversion. |
| Architecture/import gate | `runtime/architecture_gates.py` | tests and CI | `tests/test_pr14_current_architecture.py`, `tests/test_runtime_architecture_gate.py` | IMPLEMENTED_ONLY | Add AST/import checks for migrations, old adapters, auto-adoption, CWD output inference, and token-estimation shortcuts. |
| Bound provider runtime for every production call | `runtime/provider_runtime.py`, Stage 1/review services | provider adapters | `tests/test_provider_runtime.py` | IMPLEMENTED_ONLY | Eliminate unbound `ProviderRuntime()` production construction; require explicit `test_only` for unit-only runtimes and bind all receipt fields. |
| Recursive canonical request budgeting | `runtime/provider_context.py`, provider callers | Stage 1/review/provider paths | existing provider tests | REGRESSED | Estimate the actual structured request, reserves, visual/file metadata, and block before transport without silent truncation. |
| Explicit timeout/retry/mutation receipts | provider runtime and AI interface | provider transport | provider runtime tests | IMPLEMENTED_ONLY | Make timeout fields and payload mutations typed, bounded, and present in every receipt. |
| Expected provider receipt graph | `runtime/provider_completion.py`, completion evaluator | outline/review/validation completion | `tests/test_completion_evaluator.py` | IMPLEMENTED_ONLY | Add typed expected-vs-observed closure and make completion consume it rather than ledger non-emptiness. |
| Artifact-specific READY validators | registry/runtime validators | READY, resume, adoption, completion, export | scattered artifact tests | IMPLEMENTED_ONLY | Validate each current artifact contract before every trust transition. |
| Explicit outline adoption transaction only | `outline/v3_executor.py`, `outline/adoption_transaction.py`, orchestrator | outline/review boundary | `tests/test_outline_v3_semantic_execution.py` | REGRESSED | Remove executor/metadata auto-adoption and require actor, reason, expected hashes, and receipt closure in `OutlineAdoptionTransaction`. |
| Exact replay integrated into Outline executor | `runtime/outline_v3_replay.py`, DAG helpers | outline node execution | `tests/test_outline_v3_dag_replay.py` | IMPLEMENTED_ONLY | Recompute replay keys at execution, validate READY output/receipt hashes, preserve unaffected upstream nodes, and rerun stale descendants. |
| Metamorphic Outline stability audit | `outline/v3_executor.py` | outline finalization | current semantic outline tests | REGRESSED | Execute permutation/shard variants and persist definitions, hashes, metrics, thresholds, diagnostics, and status. |
| Durable per-section Review execution | `services/review_generation_service.py` | orchestrator review stage | `tests/test_current_review_generation.py` | REGRESSED | Persist/validate/register/checkpoint/replay each section and resume only the failed section. |
| Exact citation token spans and DOCX projection | citation manifest/catalog and `docx_writer.py` | review export | `tests/test_docx_writer.py`, current review tests | IMPLEMENTED_ONLY | Add separate/clustered/repeated/Unicode boundary tests and prove cited-only bibliography and DOCX projection integrity. |
| Typed current Validation service | `validation/` plus `runtime/validation_adapter.py` and `validator.py` | orchestrator and `reviewctl validate` | adapter/bridge tests | REGRESSED | Replace generator-shaped adapter and old validator call graph with `ValidationExecutionService`; add real execution and status-only command split. |
| Validation receipts and claim-batch recovery | validation runtime | validation stage | validation closure/run-result tests | IMPLEMENTED_ONLY | Execute claims against current evidence/provider boundary, persist receipts and immutable results, and resume by claim batch. |
| Repair issue/action/patch separation | `validation/repair_models.py`, repair transaction | repair integration | repair transaction tests | IMPLEMENTED_ONLY | Reject empty report-only patches, bind full dependency bundles, and separate manual actions from safe patches. |
| Semantic repair revalidation and promotion | `validation/repair_transaction.py` | repair apply/promotion | `tests/test_current_validation_repair_e2e.py` | IMPLEMENTED_ONLY | Validate affected evidence/citations/visuals/projections/closures and add explicit quarantine-to-canonical promotion transaction. |
| Queue canonical output root | `services/queue_service.py`, job runner | queue worker/cancellation | current queue and persistent queue tests | REGRESSED | Persist resolved roots in QueueJobSpec and remove CWD-derived fallback from worker and cancellation paths. |
| QueueRunner production E2E and cancellation | queue service/cancellation | queue control plane | current queue tests | IMPLEMENTED_ONLY | Run real worker with restart, dependency, parallel claim, progress/log/artifact, cancellation acknowledgement, retry, and changed-input cases. |
| GUI canonical state and transactions | `gui/app.py` | GUI workflow | GUI controller/copy tests | REGRESSED | Remove legacy retry surface, display canonical lifecycle states, and route adoption/validation buttons through formal services. |
| Trust-bound Export/Forensic Attestation | `runtime/export_bundle.py` | export command/control plane | `tests/test_export_bundle.py` | REGRESSED | Derive trust from Registry/current services, isolate forensic mode, and make ZIP publication/registration atomic and status-consistent. |
| True production full-chain E2E | `tests/test_current_runtime_full_e2e.py` | `AgentRuntimeRunner`/`reviewctl run` | current test manually invokes bridge and writes validation | REGRESSED | Replace fixture/metadata/manual-artifact path with real runner, explicit adoption, real validation, closure, export, and attestation. |
| Failure-chain E2E coverage | current focused tests | runtime control plane | partial negative tests | IMPLEMENTED_ONLY | Add all required missing-receipt, malformed relation, stale adoption, overflow, crash-resume, cancellation, repair, and export-registration failures. |
| Evidence/documentation synchronization | existing PR14 docs and feature matrices | PR description/reviewer handoff | existing docs only | IMPLEMENTED_ONLY | Update final audit, validation evidence, final verification, bilingual matrices/runbooks, README, and PR description with only current evidence. |

## Explicit non-claims at this baseline

- The existing `tests/test_current_runtime_full_e2e.py` is not accepted as the
  required production E2E because it enables fixture mode, sets adoption through
  metadata, manually constructs/registers a validation result, and passes caller
  completion state to export.
- Existing deterministic provider adapters prove a provider boundary contract only;
  they are not `LIVE_VERIFIED` evidence for a live API.
- The current green Windows CI run is not evidence that Playwright or heavy OCR ran;
  those categories remain `NOT_RUN` unless a corresponding job is actually read back.
- Existing closure inspection is not counted as execution of Validation.
- Existing files and passing unit/component tests do not upgrade a row to
  `INTEGRATED` or `E2E_VERIFIED` without a production caller trace and a matching
  test.

## Next remediation order

1. Cut configuration and runtime schemas to current-only, including architecture
   gates, because every downstream trust claim depends on an unambiguous contract.
2. Bind provider requests and expected receipt closure, then connect exact replay and
   metamorphic stability to Outline execution.
3. Remove auto-adoption and make Review, Validation, Repair, Queue, and Export trust
   transitions explicit and durable.
4. Replace the current manually assembled full-chain test with a real production
   runner E2E and failure-chain coverage.
5. Update final evidence documents and PR #14 only after fresh local and remote
   verification.

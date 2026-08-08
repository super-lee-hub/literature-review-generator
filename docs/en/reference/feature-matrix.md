# Feature Implementation Status Matrix

Status reflects the current `codex/platform-hardening-outline-v3` branch and
fresh offline verification. `IMPLEMENTED` means the component is present;
`INTEGRATED` means its production caller is wired; `CONTROLLER_VERIFIED` means
the controller/label boundary is covered without claiming browser automation;
`E2E_VERIFIED` means the current production-shaped chain exercised it;
`LIVE_VERIFIED` is reserved for an actual external-provider run; `NOT_VERIFIED`
means the required live or UI evidence was not run.

The current offline collection is `855`; the strict marker gate selected `833`
and deselected `22`, with the exact aggregate finishing `833 passed, 22
deselected` in `2644.77s` (`44:04`). This is local pytest time; final-SHA GitHub
Actions timing is reported separately in the PR description. Live API,
Playwright, heavy OCR, and multi-host verification remain unrun.

| Feature | Status | Canonical implementation |
|---|---|---|
| Source inventory and identity gate | E2E_VERIFIED | content hashes; DOI or title plus real author/year evidence; match/ambiguous/mismatch; quarantine before Stage 1 |
| Zotero parsing and FileIndex | IMPLEMENTED | diagnostic parse result, root-isolated read-only multi-candidate index |
| Artifact Registry v2 | E2E_VERIFIED | revisioned locked transactions, typed multi-record atomic save, version-aware READY validation, immediate recursive fail-closed verification of READY local/external dependencies, lease-manifest target type/version/path/hash binding, and protection for pre-existing identical immutable publication targets |
| Version-aware artifact validators | E2E_VERIFIED | `(artifact_type, artifact_version)` dispatch for current production and Outline artifacts, explicit known compatibility projections, malformed-fixture rejection, and fail-closed unknown current versions |
| Job outcome and attempts | E2E_VERIFIED | Registry-authoritative canonical outcomes, append-only attempts, fenced compatibility projection writes, stale/tampered projection repair, and pointer ownership |
| Stage 1 summaries and paper evidence | E2E_VERIFIED | immutable content-addressed summaries, paper artifacts, evidence manifests, typed `stage1_reusable_summary_manifest/v1` source manifests and edge checkpoints; same-epoch stable provider authority, conditional zero-call closure, all-reuse/mixed-reuse provenance, real Registry source-artifact bindings, and no fabricated receipt ledger; summary -> source_bundle -> source PDF lineage |
| ReviewBatch derivation | INTEGRATED | deterministic parent-hash subsets, zero child Stage 1 calls, derivation/coordinator leases, monotonic generation, immutable max-head projection receipts |
| Outline Intelligence v3 | E2E_VERIFIED | registered artifact validation surface, deterministic node DAG, exact execution-binding/replay closure, typed quality gate, `off`/`smoke`/`full` stability modes, preflight call/cost budgets, checkpointed subruns, health, critic retry scope, and explicit versioned adoption pointer |
| Review and citation chain | E2E_VERIFIED | current review draft v3, complete section binding, citation manifest v3, token spans, cited bibliography, DOCX |
| Validation truth source | E2E_VERIFIED | explicit `ValidationExecutionService` constructor and shared direct/CLI/GUI/queue entrypoint policy; pre-transport request binding; response/normalized/artifact/node receipt closure; stage-indexed provider closure from the durable requested-stage spec; durable `ValidationRunResultV1` read-back with job/attempt/hash binding; typed `ValidationDispositionV1` for optional `not_requested` validation; exact review/citation/evidence `depends_on` closure; reports are projections |
| Stage plan and `run_all` policy | E2E_VERIFIED | durable stage plan requests analyze/outline/review/validate when validation is enabled, omits only optional validation when disabled, publishes a typed not-requested disposition and zero-call validation closure in that case, still requires `CurrentArtifactSet`, and blocks derivation/outline-only canonical readiness without it |
| Outline quality and stability gates | E2E_VERIFIED | typed `OutlineQualityGate`, effective-section/duplicate/placeholder/empty-stream audits, one additional full reversed-summary smoke chain, comprehensive full-decision stability variants, checkpointed subruns, per-node call/token/cost plans, provider/model-bound pricing, hard call/context/prompt/total-token admission, exact replay with zero transport calls, and gate-hash invalidation |
| Repair promotion boundary | E2E_VERIFIED | typed issues/actions/auto-safe patches, current-service revalidation, immutable prepared promotion transaction, transaction-hash-bound `CurrentArtifactSet`, one Registry lock/CAS pointer switch, pointer unchanged on failure, quarantined derived versions, and no canonical overwrite |
| AgentRuntimeRunner | E2E_VERIFIED | run/resume/status/reconcile over `AgentRuntimeBridge`; durable `BaseException` terminals and canonical Validation disposition recovery |
| Queue outcome mapping | E2E_VERIFIED | Queue reads `job_status`; `success` remains readiness compatibility only; lease-generation staging publishes immutable bytes under queue-lock -> Registry order; target and lease publication manifest commit atomically with live target/schema/recursive-dependency verification; failed commits leave only unreferenced immutable bytes; repeated/direct aliases preserve pre-existing identical files; lease loss and Windows `spawn` stale-worker current-set races are fenced |
| Trust-bound canonical export | E2E_VERIFIED | `canonical_verified` and real `canonical_unvalidated` admission through completion, typed disposition, CurrentArtifactSet, stage closure, Registry dependency, ZIP provenance, and forensic read-back; unvalidated exports retain an explicit warning and never claim clean validation |
| Publication architecture gate | E2E_VERIFIED | current writers use the publication boundary; the architecture scan rejects canonical-path write/replace followed by a separate Registry registration, with narrow private-staging/cache/rendering/read-only legacy exceptions |
| MinerU/Docling/OCR safety | IMPLEMENTED | preflight, shared auth circuit breaker, bounded subprocess timeouts |
| Windows machine progress | IMPLEMENTED | UTF-8 console and ASCII-safe JSON progress |
| GUI workflow and queue | CONTROLLER_VERIFIED | local workflow controller/status labels, atomic cross-process queue snapshots, CAS worker leases, heartbeat, expiry/crash recovery, and serial persistent queue; Playwright remains unrun |
| Stale workspace handling | IMPLEMENTED | missing current identity/readiness fields are rejected and remain non-ready |
| Live provider smoke tests | NOT_VERIFIED | marker, explicit enable flag, and credentials are required; no live call was made |

Future work must be recorded as a specific limitation with an owner and
testable acceptance criterion; deterministic offline evidence must not be
described as live verification.

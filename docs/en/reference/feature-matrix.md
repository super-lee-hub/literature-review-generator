# Feature Implementation Status Matrix

Status reflects the current `codex/platform-hardening-outline-v3` branch and
fresh offline verification. `IMPLEMENTED` means the component is present;
`INTEGRATED` means its production caller is wired; `CONTROLLER_VERIFIED` means
the controller/label boundary is covered without claiming browser automation;
`E2E_VERIFIED` means the current production-shaped chain exercised it;
`LIVE_VERIFIED` is reserved for an actual external-provider run; `NOT_VERIFIED`
means the required live or UI evidence was not run.

The current offline baseline is 748 collected tests, 726 passed, and 22
deselected. Live API, Playwright, and heavy OCR are `NOT_RUN` in this pass.

| Feature | Status | Canonical implementation |
|---|---|---|
| Source inventory and identity gate | E2E_VERIFIED | content hashes; DOI or title plus real author/year evidence; match/ambiguous/mismatch; quarantine before Stage 1 |
| Zotero parsing and FileIndex | IMPLEMENTED | diagnostic parse result, root-isolated read-only multi-candidate index |
| Artifact Registry v2 | E2E_VERIFIED | revisioned locked transactions, atomic save, version-aware READY validation, and immediate fail-closed verification of READY local/external dependencies |
| Version-aware artifact validators | E2E_VERIFIED | `(artifact_type, artifact_version)` dispatch for current production and Outline artifacts, explicit known compatibility projections, malformed-fixture rejection, and fail-closed unknown current versions |
| Job outcome and attempts | E2E_VERIFIED | `job_outcome_v1.json`, append-only attempts, pointer ownership |
| Stage 1 summaries and paper evidence | E2E_VERIFIED | immutable content-addressed summaries, paper artifacts, evidence manifests and edge checkpoints; expected-call closure records reuse explicitly, performs zero calls for reused summaries, and never synthesizes receipts; summary -> source_bundle -> source PDF lineage |
| ReviewBatch derivation | INTEGRATED | deterministic parent-hash subsets, zero child Stage 1 calls, derivation/coordinator leases, monotonic generation, immutable max-head projection receipts |
| Outline Intelligence v3 | E2E_VERIFIED | registered artifact validation surface, deterministic node DAG, exact execution-binding/replay closure, typed quality gate, `off`/`smoke`/`full` stability modes, preflight call/cost budgets, checkpointed subruns, health, critic retry scope, and explicit versioned adoption pointer |
| Review and citation chain | E2E_VERIFIED | current review draft v3, complete section binding, citation manifest v3, token spans, cited bibliography, DOCX |
| Validation truth source | E2E_VERIFIED | explicit `ValidationExecutionService` constructor and current runner boundary; pre-transport request binding; response/normalized/artifact/node receipt closure; stage-indexed provider closure from the durable requested-stage spec; durable `ValidationRunResultV1` read-back with job/attempt/hash binding; typed `ValidationDispositionV1` for optional `not_requested` validation; exact review/citation/evidence `depends_on` closure; reports are projections |
| Stage plan and `run_all` policy | E2E_VERIFIED | durable stage plan requests analyze/outline/review/validate when validation is enabled, omits only optional validation when disabled, publishes a typed not-requested disposition and empty closure in that case, still requires `CurrentArtifactSet`, and blocks derivation/outline-only canonical readiness without it |
| Outline quality and stability gates | E2E_VERIFIED | typed `OutlineQualityGate`, effective-section/duplicate/placeholder/empty-stream audits, one additional full reversed-summary smoke chain, comprehensive full-decision stability variants, checkpointed subruns, per-node call/token/cost plans, explicit pricing-source handling, exact replay with zero transport calls, and gate-hash invalidation |
| Repair promotion boundary | E2E_VERIFIED | typed issues/actions/auto-safe patches, current-service revalidation, immutable prepared promotion transaction, transaction-hash-bound `CurrentArtifactSet`, one Registry lock/CAS pointer switch, pointer unchanged on failure, quarantined derived versions, and no canonical overwrite |
| AgentRuntimeRunner | E2E_VERIFIED | run/resume/status/reconcile over `AgentRuntimeBridge`; durable `BaseException` terminals and canonical Validation disposition recovery |
| Queue outcome mapping | E2E_VERIFIED | Queue reads `job_status`; `success` remains readiness compatibility only; lease-generation staging publishes immutable bytes under queue-lock -> Registry order, records a lease publication manifest, fences lease loss, and includes Windows `spawn` stale-worker/orphan coverage |
| MinerU/Docling/OCR safety | IMPLEMENTED | preflight, shared auth circuit breaker, bounded subprocess timeouts |
| Windows machine progress | IMPLEMENTED | UTF-8 console and ASCII-safe JSON progress |
| GUI workflow and queue | CONTROLLER_VERIFIED | local workflow controller/status labels, atomic cross-process queue snapshots, CAS worker leases, heartbeat, expiry/crash recovery, and serial persistent queue; Playwright remains unrun |
| Stale workspace handling | IMPLEMENTED | missing current identity/readiness fields are rejected and remain non-ready |
| Live provider smoke tests | NOT_VERIFIED | marker, explicit enable flag, and credentials are required; no live call was made |

Future work must be recorded as a specific limitation with an owner and
testable acceptance criterion; deterministic offline evidence must not be
described as live verification.

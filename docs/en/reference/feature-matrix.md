# Feature Implementation Status Matrix

Status reflects the current `codex/platform-hardening-outline-v3` branch and
fresh offline verification.

| Feature | Status | Canonical implementation |
|---|---|---|
| Source inventory and identity gate | implemented | content hashes; DOI or title plus real author/year evidence; match/ambiguous/mismatch; quarantine before Stage 1 |
| Zotero parsing and FileIndex | implemented | diagnostic parse result, root-isolated read-only multi-candidate index |
| Artifact Registry v2 | implemented | revisioned locked transactions, atomic save, and immediate fail-closed verification of READY local/external dependencies |
| Job outcome and attempts | implemented | `job_outcome_v1.json`, append-only attempts, pointer ownership |
| Stage 1 summaries and paper evidence | implemented | canonical summaries, paper artifacts, evidence manifests and edge checkpoints; summary -> source_bundle -> source PDF lineage |
| ReviewBatch derivation | implemented | deterministic parent-hash subsets, zero child Stage 1 calls, derivation/coordinator leases, monotonic generation, immutable max-head projection receipts |
| Outline Intelligence v3 | implemented | registered 26-type artifact validation surface, deterministic node DAG, exact execution-binding/replay closure, typed quality gate and stability variants, health, and explicit versioned adoption pointer |
| Review and citation chain | implemented | current review draft v3, citation manifest v3, cited bibliography, DOCX |
| Validation truth source | implemented | explicit `ValidationExecutionService` constructor and current runner boundary; pre-transport request binding; response/normalized/artifact/node receipt closure; durable `ValidationRunResultV1` read-back with job/attempt/hash binding; exact review/citation/evidence `depends_on` closure; reports are projections |
| Outline quality and stability gates | implemented | typed `OutlineQualityGate`, effective-section/duplicate/placeholder/empty-stream audits, candidate/summary/route variants, full-decision stability comparison, and gate-hash invalidation |
| Repair promotion boundary | implemented | typed issues/actions/auto-safe patches, semantic `RepairStructuralClosure`, and versioned draft/manifest/DOCX/audit/lineage promotion without canonical overwrite |
| AgentRuntimeRunner | implemented | run/resume/status/reconcile over `AgentRuntimeBridge`; durable `BaseException` terminals and canonical Validation disposition recovery |
| Queue outcome mapping | implemented | Queue reads `job_status`; `success` remains readiness compatibility only |
| MinerU/Docling/OCR safety | implemented | preflight, shared auth circuit breaker, bounded subprocess timeouts |
| Windows machine progress | implemented | UTF-8 console and ASCII-safe JSON progress |
| GUI workflow and queue | implemented | local workflow UI, atomic cross-process queue snapshots, CAS worker leases, heartbeat, expiry/crash recovery, and serial persistent queue |
| Stale workspace handling | fail-closed | missing current identity/readiness fields are rejected and remain non-ready |
| Live provider smoke tests | optional | marker, explicit enable flag, and credentials required |

There is no separate roadmap entry for features already marked implemented. Future work must be recorded as a specific limitation with an owner and testable acceptance criterion.

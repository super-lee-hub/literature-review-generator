# Feature Implementation Status Matrix

Status reflects code and offline tests in the reliability-upgrade branch.

| Feature | Status | Canonical implementation |
|---|---|---|
| Source inventory and identity gate | implemented | content hashes; DOI or title plus real author/year evidence; match/ambiguous/mismatch; quarantine before Stage 1 |
| Zotero parsing and FileIndex | implemented | diagnostic parse result, root-isolated read-only multi-candidate index |
| Artifact Registry v2 | implemented | revisioned locked transactions, atomic save, and immediate fail-closed verification of READY local/external dependencies |
| Job outcome and attempts | implemented | `job_outcome_v1.json`, append-only attempts, pointer ownership |
| Stage 1 summaries and paper evidence | implemented | canonical summaries, paper artifacts, evidence manifests and edge checkpoints; summary -> source_bundle -> source PDF lineage |
| ReviewBatch derivation | implemented | deterministic parent-hash subsets, zero child Stage 1 calls, derivation/coordinator leases, monotonic generation, immutable max-head projection receipts |
| Outline Intelligence v2 | implemented | full artifact chain, prompt budgets, health sidecar, explicit adoption |
| Review and citation chain | implemented | review draft v2, citation manifest v3, cited bibliography, DOCX |
| Validation truth source | implemented | durable `ValidationRunResultV1` read-back with job/attempt/hash binding; zero claims require explicit citation-free status; reports are projections |
| AgentRuntimeRunner | implemented | run/resume/status/reconcile over `AgentRuntimeBridge`; durable `BaseException` terminals and canonical Validation disposition recovery |
| Queue outcome mapping | implemented | Queue reads `job_status`; `success` remains readiness compatibility only |
| MinerU/Docling/OCR safety | implemented | preflight, shared auth circuit breaker, bounded subprocess timeouts |
| Windows machine progress | implemented | UTF-8 console and ASCII-safe JSON progress |
| GUI workflow | implemented | local workflow UI and serial persistent queue |
| Legacy workspace reading | compatibility | additive readers mark missing new identity/readiness fields `legacy_unverified` |
| Live provider smoke tests | optional | marker, explicit enable flag, and credentials required |

There is no separate roadmap entry for features already marked implemented. Future work must be recorded as a specific limitation with an owner and testable acceptance criterion.

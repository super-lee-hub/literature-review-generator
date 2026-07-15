# Runtime Truth Sources and Contracts

This document names the canonical durable facts used by the current runtime. Files not listed as canonical are projections, exports, caches, or compatibility inputs.

## Job and source identity

- `source_inventory_v1.json` is the content-hashed source identity truth for Zotero reports, PDFs, explicit summaries, and classification files.
- Without a DOI, canonical source identity requires a normalized title match plus a real first-author or year match. A title-only observation remains quarantined.
- `artifact_registry.json` v2 is the artifact/dependency graph. Registry writes use a workspace lease, revisioned transaction, atomic replacement, and fail-closed corruption handling. Every READY dependency is verified against durable Registry identity, status, path, and content hash before commit.
- `job_outcome_v1.json` is the current job-head projection: lifecycle status, disposition, readiness policy, required/completed stages, and `canonical_ready`.
- `artifacts/job_attempts/snapshot-*.json` is append-only attempt history. A stale running attempt becomes `interrupted`; it is never rewritten as the next attempt.
- `runtime_stage_terminals/*/*.json` proves stage completion only when every output, hash, schema, dependency, and terminal record validates.

`legacy success` is only a projection of `canonical_ready`. Queue lifecycle reads `job_status`.

## Pipeline truth sources

| Stage | Canonical truth | Projections / exports |
|---|---|---|
| Source intake | `source_inventory_v1.json`, `source_bundle.json` | parser compatibility `List[PaperInfo]` |
| Stage 1 | canonical `*_summaries.json`; registered `paper_artifacts/*.json`; evidence manifests; READY summaries depend on the registered `source_bundle`, which depends on source PDFs | Excel and legacy summary shapes |
| Outline v2 | registered literature map, synthesis flow, candidates, critiques, arbitration, `final_outline`, coverage audit, and independent `outline_stage_health_v1.json`; downstream review consumes only registered `adopted_final_outline` when v2 is enabled | legacy Markdown outline when v2 is explicitly disabled |
| Review | `*_review_draft_v2.json` plus `*_citation_manifest_v3.json` and citation-ref catalog | review draft v1 and DOCX |
| Validation | `validation_run_result_v1.json` (`ValidationRunResultV1`) | TXT report, manual-review JSON, alignment audit, and completion report derived from the canonical JSON |
| Repair | registered repair plan and apply result tied to the validation-run artifact | human-readable repair summaries |

## Public outcomes

`job_status`: `pending | running | completed | failed | cancelled`.

`job_disposition`: `clean | findings | needs_review | unvalidated`.

`claim_verdict`: `supported | partial_support | evidence_gap | unsupported | contradicted | wrong_source | needs_review`.

No evidence maps to `evidence_gap`, never automatically to `unsupported`. Identity `ambiguous` or `mismatch` completes diagnostics but quarantines canonical generation and sets `canonical_ready=false`.

A zero-claim Validation result is clean only when the review is explicitly citation-free. Otherwise claim completeness is false and the result cannot publish a clean disposition. Successful Validation is published only after canonical JSON read-back confirms the job ID, attempt ID, and content hash.

## Derived review batches

`SummarySelectionSpecV1` fixes the parent job, parent artifact ID/hash, ordered paper keys, optional classification-file hash, selection policy, and selection hash. Child artifacts use `external_job` dependencies. Child derivation must not cross the Stage 1 provider boundary.

Each multi-variant derivation reserves a durable monotonic `projection_generation` before child or manifest writes. Per-derivation and coordinator leases serialize ownership and projection publication. The fully validated immutable Registry manifest with the unique maximum generation is the coordinator head; `review_batch_manifest.json` is only a repairable projection of that head. `review-batch-projection-generation-v1` reservations and `review-batch-projection-receipt-v2` receipts record the durable order plus `projected` or `superseded` head identity/generation/hash. Ordering never depends on mtime or wall-clock timestamps.

## AI-native runtime

`RuntimeJobSpec` and `AgentRuntimeRunner` are the public execution contract layered on the existing `AgentRuntimeBridge`:

- `run`: new job and attempt;
- `resume`: new append-only attempt, reusing only proven durable stages;
- `status`: read-only job head;
- `reconcile`: provider-free repair of Registry, pointer, and terminal projections.

`SystemExit` and other `BaseException` paths persist a durable terminal state before re-raising the original exception. Resume reconstructs Validation disposition from the canonical terminal artifact and never infers it from human-readable projections.

Relative paths are resolved from their owning spec/config/summary file, never silently from the process CWD.

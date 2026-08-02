# Runtime Truth Sources and Contracts

This document names the durable facts used by the current runtime. Files not
listed as canonical are projections, exports, caches, or diagnostics; they
cannot satisfy a readiness or completion gate.

## Job and source identity

- `source_inventory_v1.json` is the content-hashed source identity for Zotero
  reports, PDFs, explicit summaries, and classification files.
- Without a DOI, canonical source identity requires a normalized title plus a
  real first-author or year match. A title-only observation remains quarantined.
- `artifact_registry.json` is the artifact/dependency graph. Registry writes
  use a workspace lease, revisioned transaction, atomic replacement, and
  fail-closed corruption handling.
- `job_outcome_v1.json` is the job-head projection: lifecycle status,
  disposition, readiness policy, required/completed stages, and
  `canonical_ready`.
- `artifacts/job_attempts/snapshot-*.json` is append-only attempt history.
  A stale running attempt becomes `interrupted`; it is never rewritten as the
  next attempt.
- `runtime_stage_terminals/*/*.json` proves stage completion only when every
  output, hash, schema, dependency, and terminal record validates.

Queue lifecycle reads `job_status`; a human-readable success flag is never a
source of truth.

## Pipeline truth sources

| Stage | Canonical truth | Projections / exports |
|---|---|---|
| Source intake | `source_inventory_v1.json`, `source_bundle.json` | parser diagnostics and read-only paper views |
| Stage 1 | canonical `*_summaries.json`, registered `paper_artifacts/*.json`, evidence manifests, and source lineage | Excel and display summaries |
| Outline Intelligence v3 | registered evidence views, corpus ledger, multi-view matrix, review intent, coverage contract, relation map, candidate plan, node DAG, receipts, final outline, stage health, and adoption record | Markdown or human-readable outline displays |
| Review | `review_draft.json` with `artifact_version=v3`, `citation_manifest_v3.json`, and the citation-reference catalog | DOCX and text reports |
| Validation | `validation_run_result_v1.json` plus its exact Registry `depends_on` closure over the review draft, citation manifest, and evidence manifests | TXT report, manual-review JSON, alignment audit, and completion projection |
| Repair | registered repair plan and apply result bound to the validation-run artifact | human-readable repair summaries |

## Public outcomes

`job_status` is `pending | running | completed | failed | cancelled`.

`job_disposition` is `clean | findings | needs_review | unvalidated`.

`claim_verdict` is `supported | partial_support | evidence_gap | unsupported |
contradicted | wrong_source | needs_review`.

Missing evidence maps to `evidence_gap`, never automatically to `unsupported`.
Ambiguous or mismatched identity quarantines canonical generation and keeps
`canonical_ready=false`.

A zero-claim validation result is clean only when the review is explicitly
citation-free. Successful validation is published only after canonical JSON
read-back confirms job ID, attempt ID, and content hash.

Every declared review, citation, and evidence input hash is a 64-character
lowercase SHA-256. The canonical payload's artifact ID/type/hash multiset must
exactly equal the Registry dependency multiset. Missing, extra, duplicate,
wrong-type, wrong-job-kind, wrong-path, or wrong-hash edges fail closed.

## Derived review batches

`SummarySelectionSpecV1` fixes the parent job, parent artifact ID/hash, ordered
paper keys, optional classification-file hash, selection policy, and selection
hash. Child artifacts use `external_job` dependencies and cannot cross the
Stage 1 provider boundary.

Each derivation reserves a durable monotonic `projection_generation` before
child or manifest writes. Leases serialize ownership and publication. The
validated immutable Registry manifest with the unique maximum generation is
the coordinator head; the human-readable manifest is only a repairable
projection.

## AI-native runtime

`RuntimeJobSpec` and `AgentRuntimeRunner` are the public execution contract over
the internal `AgentRuntimeBridge`:

- `run`: new job and attempt;
- `resume`: new append-only attempt, reusing only proven durable stages;
- `status`: read-only job head;
- `reconcile`: provider-free repair of Registry, pointer, and terminal projections.

Provider calls are bound to a job, attempt, stage, and node through a typed
context profile. Each call emits a redacted receipt with request identity,
retry/timeout accounting, response hash, and completion-evaluator result.

Relative paths resolve from their owning spec, config, or summary file. Runtime
reconciliation never calls a provider. `SystemExit` and other terminal paths
persist a durable result before re-raising the original exception.

## Outline v3 and control-plane projections

Outline Intelligence v3 is a deterministic, registered DAG. Its node outputs
bind their source summary hashes, retain replay receipts, and resume only the
failed dependency closure. A final outline is adoptable only after the
coverage, stage-health, identity, and canonical-completion gates pass.

`reviewctl` is the single control plane. `status`, `next-action`, `validate`,
`inspect`, and `attest` are provider-free reads. `run`, `resume`, `retry-node`,
`cancel`, `repair-plan`, `repair-apply`, `adopt`, and `export` are explicit
Registry-backed transitions. Cancellation is cooperative and a cancelled job
cannot publish a completed queue state.

Validation closure requires the current review draft, citation manifest, and
`ValidationRunResultV1` input IDs and hashes to match. Repair defaults to
`report_only`; an explicit safe transaction creates only quarantined derived
artifacts. Adoption never silently promotes an intermediate candidate.

Export bundles contain verified files, provenance, checksums, completion
evidence, and validation-closure evidence. `canonical_verified`,
`manual_repaired`, and `untrusted` are attestation labels, not aliases for job
success. A DOCX alone is never an export or completion proof.

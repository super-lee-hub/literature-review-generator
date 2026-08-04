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
- Queue-owned canonical bytes are published through a lease-generation-aware
  staging context. The publication boundary takes the queue store lock first,
  rechecks lease/worker/generation/fence, then registers the immutable
  content-addressed file under the Registry transaction; the reverse lock order
  is not allowed. A successful byte publication also records an immutable
  `lease_publication_manifest`; a Registry failure leaves the immutable orphan
  for diagnosis rather than restoring a mutable fixed target.
- `job_outcome_v1.json` is the job-head projection: lifecycle status,
  disposition, readiness policy, required/completed stages, and
  `canonical_ready`.
- `artifacts/job_attempts/snapshot-*.json` is append-only attempt history.
  A stale running attempt becomes `interrupted`; it is never rewritten as the
  next attempt.
- `runtime_stage_terminals/*/*.json` proves stage completion only when every
  output, hash, schema, dependency, and terminal record validates.
- `current-artifact-set:pointer` resolves one immutable `CurrentArtifactSetV1`
  containing the exact current targets (draft, citation manifest, DOCX,
  validation result, and validation receipt closure) with their hashes and the
  bound `repair_promotion_transaction` ID/hash. Promotion first prepares and
  validates every immutable READY output and its transaction record, then
  changes the set and pointer inside one Registry OS-lock/CAS boundary. A
  failed validation, registration, or CAS leaves the previous pointer and
  current set unchanged; a staged set file may remain quarantined for
  diagnosis. READY promotion transactions are immutable and are never mutated
  in place.

`artifacts/runtime_job_spec_v1.json` also stores the durable `StagePlan`. For
`run_all`, validation-enabled jobs request `analyze`, `outline`, `review`, and
`validate`; when validation is explicitly optional and disabled, only
`validate` is omitted. Both paths still require the current artifact set for
canonical readiness. Derivation and outline-only jobs cannot become canonical
without a current set, and intermediate outline candidates require explicit
adoption before they can feed the review path.

Queue lifecycle reads `job_status`; a human-readable success flag is never a
source of truth.

## Pipeline truth sources

| Stage | Canonical truth | Projections / exports |
|---|---|---|
| Source intake | `source_inventory_v1.json`, `source_bundle.json` | parser diagnostics and read-only paper views |
| Stage 1 | immutable content-addressed canonical `*_summaries.json`, registered `paper_artifacts/*.json`, evidence manifests, source lineage, expected-call closure, reuse records, and current-epoch receipt ledger | Excel and display summaries |
| Outline Intelligence v3 | registered evidence views, corpus ledger, multi-view matrix, review intent, coverage contract, relation map, candidate plan, typed quality gate, exact execution bindings, node DAG, receipts, full-decision stability audit, final outline, stage health, versioned adoption record, and current adoption pointer | Markdown or human-readable outline displays |
| Review | `review_draft.json` with `artifact_version=v3`, `citation_manifest_v3.json`, and the citation-reference catalog, resolved through the current artifact set | DOCX and text reports |
| Validation | `validation_run_result_v1.json` plus its exact Registry `depends_on` closure over the review draft, citation manifest, and evidence manifests; when optional validation is disabled, typed `ValidationDispositionV1(status=not_requested, allow_unvalidated=true)` plus an empty receipt closure binds the current set; `CurrentStageClosureMapV1` resolves only the current set | TXT report, manual-review JSON, alignment audit, and completion projection |
| Stage plan | durable `stage_plan` inside `runtime_job_spec_v1.json`, including requested/required stages, validation policy, current-set requirement, and completion policy | job outcome and UI status projections |
| Repair | typed repair issues/actions/patches, quarantined derived inputs, current-service revalidation and receipt closure, and explicit versioned promotion transaction with atomic current-set switching | human-readable repair summaries |

Current production artifacts are validated by the pair
`(artifact_type, artifact_version)`. Unknown versions for a current production
type fail closed; they are not silently treated as legacy. The canonical
export type is `export_bundle` (`v1`), not `export_manifest`. Review, citation,
DOCX, validation, receipt, repair, promotion, lineage, current-set, pointer,
export, and forensic artifacts each have an explicit versioned validator.

## Public outcomes

`job_status` is `pending | running | completed | failed | cancelled`.

`job_disposition` is `clean | findings | needs_review | unvalidated`.

`claim_verdict` is `supported | partial_support | evidence_gap | unsupported |
contradicted | wrong_source | needs_review`.

Missing evidence maps to `evidence_gap`, never automatically to `unsupported`.
Ambiguous or mismatched identity quarantines canonical generation and keeps
`canonical_ready=false`.

A zero-claim validation result is always `needs_review`; citation-free intent
does not turn an unexecuted or empty validation into `clean`. Successful
validation is published only after canonical JSON read-back confirms job ID,
attempt ID, content hash, and the exact Registry dependency closure.

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

Outline Intelligence v3 is a deterministic, registered DAG. Each node persists
an exact execution binding covering node/schema versions, dependency and
summary hashes, review intent, coverage contract, quality gate, route/model,
prompt payload, context profile, and relevant runtime configuration. Replay is
reusable only when that binding, the provider receipt, normalized output,
registered artifact, node output, and expected graph all close exactly. A
binding change marks the node stale and descendants pending while preserving
unaffected upstream nodes. A final outline is adoptable only after the
coverage, quality, stability, stage-health, identity, and canonical-completion
gates pass; adoption writes a versioned identity and a current-pointer record.

The stability policy is explicit: `off`, `smoke` (the default), or `full`.
Smoke executes one additional full reversed-summary decision chain plus exact
replay; full executes the comprehensive release/audit matrix. Stability writes
per-node call/token/cost plans and a preflight estimate before transport and
rejects a configured `max_provider_calls` or `max_estimated_cost` breach before
any provider call. Monetary admission is enforced only when a named pricing
source and complete rates are present; otherwise `cost_status=unknown` keeps
call/token ceilings but does not claim a monetary ceiling. Reported provider
usage is calculated locally and is explicitly not billing data. Subruns are
checkpointed. Candidate order, source order, and alternative shard size are
represented in the execution input; exact replay uses a fresh executor and
records zero provider transport calls. Natural-language outputs are compared
through documented semantic thresholds, not byte identity unless the replay
contract explicitly requires it.

`reviewctl` is the single control plane. `status`, `next-action`,
`validation-status`, `inspect`, and `attest` are provider-free reads.
`validate` executes the current `ValidationExecutionService` and persists a
new validation attempt; it is not a closure-only inspection. `run`, `resume`,
`retry-node`, `cancel`, `repair-plan`,
`repair-apply`, `adopt`, `export`, and the queue list/add/run/retry/cancel/
remove/import/export commands are explicit Registry- or queue-backed
transitions. Queue workers claim a job with a cross-process lease generation
and fence token and must heartbeat or lose the claim; expired claims are
recoverable and stale workers cannot publish. The canonical byte publication
boundary stages output privately, then takes the queue store lock before the
Registry transaction and rechecks lease/fence at that point. A stale worker
cannot publish merely because queue metadata has not yet converged; immutable
bytes are never restored to a mutable fixed target. Cancellation is cooperative
and a cancelled job cannot publish a completed queue state.

Completion and export use `CurrentStageClosureMapV1`, not the validation
closure as a proxy for every provider stage. The map derives requested stages
from the durable `artifacts/runtime_job_spec_v1.json` (with only the documented
legacy fallback), maps logical stages to their physical runtime names, and
records for each required stage the closure epoch, expected call graph,
current-input/config/schema hashes, terminal artifact ID/hash, status, and
Registry dependency IDs/hashes. A missing, stale, mismatched, or incomplete
stage entry blocks canonical readiness. When a current set exists, its targets
and hashes are authoritative.

The map aggregates all required provider stages; validation is not a proxy for
analyze, outline, or review. Completion therefore fails closed when any
stage-indexed closure is missing, even if a historical READY artifact or a
human-readable report exists.

Validation closure requires the current review draft, citation manifest, and
`ValidationRunResultV1` input IDs and hashes to match. The production path
constructs an explicit `ValidationExecutionService` and records validation
request identity before transport and normalized/output artifact identity after
transport. Repair defaults to `report_only`; an explicit safe transaction
creates only quarantined derived versioned artifacts, re-runs current
validation against those exact files, and records receipt closure. Only
`repair-promote` can write a new version and advance current pointers; it
never replaces an older canonical READY file in place. Adoption never silently
promotes an intermediate candidate.

Export bundles contain verified files, provenance, checksums, completion
evidence, and validation-closure evidence. If canonical registration fails,
the export is marked `untrusted`, its ZIP path and artifact ID are empty, and
the temporary bundle is removed. `canonical_verified`,
`manual_repaired`, and `untrusted` are attestation labels, not aliases for job
success. A DOCX alone is never an export or completion proof.

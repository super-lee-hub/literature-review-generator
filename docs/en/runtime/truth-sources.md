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
- A publication that creates more than one authoritative record uses one typed
  multi-record Registry transaction. Queue publication commits the target
  artifact and its `lease_publication_manifest` together or commits neither;
  a transaction failure after byte finalization may leave only an immutable,
  unreferenced orphan, never a READY target without its evidence manifest.
- Queue-owned canonical bytes are published through a lease-generation-aware
  staging context. The publication boundary takes the queue store lock first,
  rechecks lease/worker/generation/fence, then registers the immutable
  content-addressed file under the Registry transaction; the reverse lock order
  is not allowed. A successful byte publication also records an immutable
  `lease_publication_manifest`; a Registry failure leaves the immutable orphan
  for diagnosis rather than restoring a mutable fixed target.
- Direct publication tracks whether the content-addressed final path was
  created by the current publication. An existing file with the same hash is
  reused and is never deleted if an alias registration fails; an existing file
  with different bytes fails before any Registry mutation.
- The immutable Registry record with `artifact_id=job_outcome`,
  `artifact_type=job_outcome`, and `artifact_version=v1` is the sole canonical `JobOutcomeV1`
  authority. Readers load it through the Registry and verify its job ID,
  ready status, content hash, payload contract, and mirrored metadata.
- The fixed path `job_outcome_v1.json` is only the mutable
  `job_outcome_compatibility_projection/v1`. It records the canonical
  `job_outcome` ID/hash and outcome revision; readers must validate those
  fields against the Registry head. A projection write failure produces only
  a warning/reconcile issue after the canonical commit and never changes the
  canonical outcome.
- `artifacts/job_attempts/snapshot-*.json` is append-only attempt history.
  A stale running attempt becomes `interrupted`; it is never rewritten as the
  next attempt.
- `resume_state_report` is an immutable Registry-owned `resume_state_report/v1`
  artifact and is the resume-report authority. Reconciliation may use the
  fixed `resume_state_report.json` path only as an explicit legacy fallback
  when no Registry record exists, and still validates its typed payload.
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
  diagnosis. Each target is checked by both artifact ID/hash and its accepted
  type/version: `review_draft/v3`, `citation_manifest/v3`, `review_docx/v1`,
  `validation_run_result/v1` for `clean` or `findings`,
  `validation_disposition/v1` for `not_requested`, and
  `provider_receipt_closure/v1`. The prepared promotion transaction must name
  the same conditional validation evidence as the set. READY promotion
  transactions are immutable and are never mutated in place.

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
| Stage 1 | immutable content-addressed canonical `*_summaries.json`, registered `paper_artifacts/*.json`, evidence manifests, typed `stage1_reusable_summary_manifest/v1` source manifests, source lineage, expected-call closure, typed reuse records, and current-epoch receipt evidence | Excel and display summaries |
| Outline Intelligence v3 | registered evidence views, corpus ledger, multi-view matrix, review intent, coverage contract, relation map, candidate plan, typed quality gate, exact execution bindings, node DAG, receipts, full-decision stability audit, final outline, stage health, versioned adoption record, and current adoption pointer | Markdown or human-readable outline displays |
| Review | `review_draft.json` with `artifact_version=v3`, `citation_manifest_v3.json`, and the citation-reference catalog, resolved through the current artifact set | DOCX and text reports |
| Validation | `validation_run_result_v1.json` plus its exact Registry `depends_on` closure over the review draft, citation manifest, and evidence manifests; when optional validation is disabled, typed `ValidationDispositionV1(status=not_requested, allow_unvalidated=true)` plus a zero-call closure binds the current set; `CurrentStageClosureMapV1` resolves only the current set | TXT report, manual-review JSON, alignment audit, and completion projection |
| Stage plan | durable `stage_plan` inside `runtime_job_spec_v1.json`, including requested/required stages, validation policy, current-set requirement, and completion policy | job outcome and UI status projections |
| Repair | typed repair issues/actions/patches, quarantined derived inputs, current-service revalidation and receipt closure, and explicit versioned promotion transaction with atomic current-set switching | human-readable repair summaries |

Current production artifacts are validated by the pair
`(artifact_type, artifact_version)`. Unknown versions for a current production
type fail closed; they are not silently treated as legacy. The canonical
export type is `export_bundle` (`v1`), not `export_manifest`. Review, citation,
DOCX, validation, receipt, repair, promotion, lineage, current-set, pointer,
export, and forensic artifacts each have an explicit versioned validator.

Stage 1 provider closure is conditional on the expected transport count. When
the count is positive, a current, hash-valid receipt ledger and an exact
expected/observed call set are mandatory. When the count is zero, observed
receipt IDs and terminal model calls must both be zero, the expected-call graph
and its dependencies must still verify, and no empty receipt ledger may be
fabricated. All-reuse runs additionally require one unique reuse record for
each SourceBundle paper identity. A reuse record must point to a registered
source artifact and preserve separate `summary_payload_hash`,
`registered_source_artifact_hash`, and `registry_file_hash` fields, plus source
manifest, runtime-spec, evidence, and available original-receipt dependencies.
Summary-source zero-call stages use typed summary-source evidence instead of an
empty provider ledger.

Stage1 reuse has two admissible authority paths: a source artifact resolved from
the parent/current Registry through the external resolver, or a self-binding
typed `stage1_reusable_summary_manifest/v1` whose manifest ID/hash and canonical
summary bytes all verify. A current-run snapshot is marked
`current_snapshot_derived_from_external_authority=true` and is derived evidence,
never authority. Path-only input, a current snapshot, a bare summary, or
synthetic IDs/hashes cannot authorize reuse.

The typed-manifest authority path is Registry-detached: the manifest is portable
across Registry boundaries, but it is not a self-contained cryptographically
authenticated archive. Its referenced source summary artifact, provider receipt
closure, and required provider receipt ledger must remain available and
hash-valid. This does not claim a single-file portable bundle, signed provenance,
or cross-host portability.

Exact reuse equality includes the real PDF byte SHA
(`source_pdf_content_sha256`), extracted-text and semantic-input hashes
(`stage1_extracted_text_hash`, `stage1_semantic_input_hash`), preprocess/input
policy hashes, prompt hash, provider/model binding, schema hash, visual-input
hash, and normalized summary payload hash. The same PDF bytes moved to another
path remain reusable and the original/current locations plus `location_changed`
are traced; different PDF bytes fail closed even when extracted or semantic text
hashes match. A provider-generated source with any transport calls additionally
requires its original provider receipt closure and receipt ledger, both
Registry-verified. All-reuse and mixed-reuse closure must still cover the
SourceBundle identities exactly and preserve one Registry-verifiable reuse
record per reused paper. Per-paper `summary_file` reuse sources are stored as a
one-item canonical summary array with the typed manifest envelope; snapshot,
source-authority, summary-payload, and Registry-file hashes remain separate.

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
rejects a configured provider-call, context-input, per-call prompt, or hard
`max_estimated_total_tokens` breach before any provider call. Monetary
admission is enforced only when a named provider/model-bound pricing source
and every required rate are present; otherwise `cost_status=unknown` keeps
call and token ceilings but does not claim a monetary ceiling. An estimated
cost and a locally calculated usage cost are local evidence only, never
provider billing or an invoice. Subruns are checkpointed. Candidate order,
source order, and alternative shard size are represented in the execution
input; exact replay uses a fresh executor and records zero provider transport
calls. Natural-language outputs are compared through documented semantic
thresholds, not byte identity unless the replay contract explicitly requires
it.

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

Completion and export consume `CurrentStageClosureMapV1` for every required
provider stage. A zero-call stage is complete only with a valid expected-call
graph, zero terminal model calls, zero observed receipts, and the correct typed
source evidence. A missing or unexpected receipt, stale identity/hash, missing
dependency, or unbound stage blocks completion even when a historical READY
artifact exists.

Export bundles contain verified files, provenance, checksums, completion
evidence, and validation-closure evidence. `canonical_verified` is admitted
only with clean validation. `canonical_unvalidated` is admitted only when the
typed current `ValidationDispositionV1` is hash-valid and bound to the exact
current set, `validation_status=not_requested`,
`validation_required=false`, `validation_enabled=false`,
`allow_unvalidated=true`, all requested provider closures are complete, and
the outline is explicitly adopted. The ZIP provenance and `EXPORT_STATUS.txt`
repeat that policy, the disposition ID/hash, stage-plan hash, runtime-spec
hash, and a warning that semantic validation was not performed. If canonical
registration fails, the export is marked `untrusted`, its ZIP path and artifact
ID are empty, and the temporary bundle is removed. `canonical_verified`,
`canonical_unvalidated`, `manual_repaired`, and `untrusted` are attestation
labels, not aliases for job success. A DOCX alone is never an export or
completion proof.

## Canonical publication boundary

Current production writers must publish canonical bytes through the typed
publication context before Registry registration. The architecture gate
rejects the unsafe sequence "write or replace a canonical artifact path, then
call `Registry.register_file` separately". Private staging files, never-
canonical caches, temporary rendering sources, and read-only legacy
compatibility code are the only documented exceptions; they cannot enter
current completion.

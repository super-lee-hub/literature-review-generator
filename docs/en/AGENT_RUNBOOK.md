# Agent Runbook

`reviewctl` is the control surface for an existing job workspace. Read commands
are provider-free; `validate` is the explicit command that executes the current
`ValidationExecutionService` and persists a new validation attempt. The control
plane reads the durable Registry, canonical `job_outcome` record, stage terminals,
provider receipts, and Outline v3 DAG/replay state. The Registry record with
`artifact_id=job_outcome` is the sole `JobOutcomeV1` authority; fixed
`job_outcome_v1.json` is only the mutable `job_outcome_compatibility_projection/v1`
and must validate its canonical ID/hash. A projection write failure is only a
warning/reconcile issue. `resume_state_report/v1` is Registry-owned and immutable;
the fixed resume-report path is a legacy fallback only when that Registry record is
absent.

Completion and export resolve the atomic `CurrentArtifactSetV1` through
`current-artifact-set:pointer` and then build `CurrentStageClosureMapV1`; a
historical READY artifact is not a substitute for the current set. Every target
slot is checked by ID/hash and accepted type/version: review draft `v3`, citation
manifest `v3`, DOCX `v1`, validation result `v1` for clean/findings, typed
validation disposition `v1` for not-requested, and provider receipt closure
`v1`. The prepared promotion transaction must bind the same conditional
validation evidence.

The durable `StagePlan` controls completion. `run_all` requests analyze,
outline, review, and validate when validation is enabled; an explicitly
optional disabled validation policy requests only analyze, outline, and review,
but still requires a current set. Derivation and outline-only actions cannot
become canonical-ready without that set, and intermediate Outline v3 candidates
are not silently adopted.

## Safe command order

```text
python -m reviewctl doctor --config <config.ini>
python -m reviewctl status --job <job_id>
python -m reviewctl next-action --job <job_id>
python -m reviewctl validate --job <job_id>
python -m reviewctl validation-status --job <job_id>
python -m reviewctl repair-plan --job <job_id>
python -m reviewctl export --job <job_id>
python -m reviewctl attest --job <job_id>
```

Use `--workspace <workspace_path>` when the job ID cannot be resolved. All commands emit one machine-readable JSON object.

## Execution and recovery

- `plan` is read-only.
- `run` starts a new runtime attempt; `resume` creates an append-only attempt and reuses only verified durable stages.
- `retry-node` changes only the persisted failed Outline v3 node scope. It never regenerates completed candidates.
- `reconcile --dry-run` is read-only. A non-dry reconciliation is allowed to repair only registered projections through the existing runtime reconciler.
- `cancel` persists a cooperative cancellation request. It never kills processes. Workers observe it at safe checkpoints and must not publish `completed` afterward.
- Queue workers use atomic snapshots, input/config fingerprints, lease
  generations, and fence tokens. An expired or fenced worker cannot publish a
  result; retry/cancel/recovery decisions must use the persisted queue state.
- Canonical bytes are staged under the lease generation. Publication takes the
  queue store lock, rechecks lease/worker/generation/fence, then takes the
  Registry transaction lock; this queue -> Registry lock order is part of the
  contract. The target artifact and its immutable lease publication manifest
  are committed in one Registry transaction. A Registry failure leaves only
  unreferenced immutable bytes: neither READY record nor the current pointer is
  advanced. A stale worker, including a Windows `spawn` child whose claim
  expired, cannot publish a canonical artifact merely because its local queue
  snapshot is stale.
- Direct publication reuses an existing content-addressed file only when its
  bytes have the requested hash. A failed alias registration never removes
  that pre-existing file; a different-byte collision fails before Registry
  mutation.

## Validation, repair, and adoption

- `validate` executes current validation over the current v3 review draft, v3 citation manifest, and evidence inputs, then persists `ValidationRunResultV1`, receipts, and Registry dependencies. If validation is explicitly optional and disabled, the runner instead persists typed `ValidationDispositionV1(status=not_requested, allow_unvalidated=true)` plus a zero-call closure; this is evidence of non-requesting, not a passed validation. `validation-status` is the read-only closure check, including Registry identity and hash equality.
- Stage closure is conditional on expected transport count: positive-call stages
  require a current hash-valid receipt ledger with an exact call set; zero-call
  stages require a valid expected graph, zero terminal model calls and receipts,
  and typed source evidence, without a fabricated empty ledger. Stage 1 reuse
  accepts only a parent/current Registry source resolved by the external resolver
  or a self-binding typed `stage1_reusable_summary_manifest/v1`; a current snapshot
  is `current_snapshot_derived_from_external_authority=true` and never authority.
  This typed-manifest path is Registry-detached, not a self-contained
  cryptographically authenticated archive: its referenced summary, receipt closure,
  and receipt ledger required when calls occurred must remain available and
  hash-valid. It does not provide a single-file
  portable bundle, signed provenance, or cross-host portability.
  Path-only/current-snapshot/bare-summary inputs and synthetic IDs/hashes are
  insufficient. Exact equality covers the real PDF byte SHA, extracted/semantic
  hashes, preprocess/input/prompt/provider/model/schema/visual hashes, and normalized
  summary payload hash. Same bytes at a moved path are allowed with original/current
  locations and `location_changed` traced; different bytes invalidate reuse even if
  text hashes match. Provider-generated sources with calls require the original
  Registry-verified receipt closure and ledger. Per-paper `summary_file` authorities
  remain canonical one-item arrays with the typed manifest; payload and Registry
  file hashes stay separate.
- For a Stage 1 all-reuse run, expect no current-epoch provider receipts and
  one unique reuse record per SourceBundle paper. A mixed run must show provider
  calls only for generated papers; a summary-source zero-call run must use its
  typed summary-source dependency. A logical summary hash or synthetic artifact
  ID is never sufficient provenance.
- `repair-plan` is report-first. It may persist a hash-bound plan and transaction record, but it does not edit canonical artifacts.
- `repair-apply` accepts only an explicitly `auto_apply_safe` plan. Its outputs are registered as `quarantined` derived versions; canonical READY draft, manifest, outline, and DOCX artifacts are not replaced.
- `repair-promote --transaction <id> --actor <actor> --reason <reason>` revalidates the quarantined derived inputs through the current service, requires a complete closure, and only then creates a new version and advances current pointers.
- `adopt --artifact <final_outline_id> --actor <actor>` is explicit. It requires a READY final outline, passing coverage audit, adoptable stage health, matching hashes, and no blocking critique. It writes `adopted_final_outline` and an immutable adoption audit record. Outline v3 candidate plans are not silently promoted by this command.

Outline stability uses `off`, `smoke`, or `full`: smoke adds one full
reversed-summary decision chain and exact replay; full runs the complete
perturbation matrix. Call/token/cost plans are persisted per node. Provider
call, context, per-call prompt, and total estimated token ceilings are hard
local admission limits. A monetary ceiling is enforced only with a named
provider/model-bound pricing source and complete rates; unknown pricing is
reported as `cost_status=unknown`, and estimated/calculated usage is not
provider billing.

## Export and trust

`export` creates a declarative ZIP containing verified Registry files, provenance, checksums, completion evidence, and validation closure. A canonical registration failure returns `untrusted` with an empty bundle path and artifact ID after deleting the temporary ZIP. `attest` records a dependency-graph and file-hash audit. Trust labels are:

- `canonical_verified`: completion and validation closure are clean and all included READY files verify;
- `canonical_unvalidated`: completion and all current-set/Registry/closure evidence verify under an explicit typed `ValidationDispositionV1(status=not_requested, validation_required=false, validation_enabled=false, allow_unvalidated=true)`; semantic validation was not performed;
- `manual_repaired`: hashes verify but manual-modification metadata is present;
- `untrusted`: any integrity, completion, closure, or registration condition is unresolved.

`canonical_unvalidated` is a real export status, not an alias for clean
validation. Its provenance and `EXPORT_STATUS.txt` must repeat the
`not_requested` policy, disposition ID/hash, stage-plan hash, runtime-spec
hash, and warning. The status is admitted only when the exact disposition is
current and hash-valid and every requested provider closure is complete.

Cost evidence has separate meanings: the provider-call ceiling and total-token
ceiling are hard local admission limits; an estimated cost is a local estimate;
calculated usage cost uses locally supplied provider/model rates; provider
billing or an invoice is external evidence and is never asserted by the
runtime. Empty/default pricing keeps `cost_status=unknown` and disables only
the monetary ceiling, not call, context, prompt, or total-token limits.

Never infer completion from a DOCX existing on disk, a human-readable report, a mutable queue flag, or a manually edited Registry/Stage Health file.

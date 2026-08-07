# Recovery Runbook

This runbook is fail-closed. Preserve the workspace and inspect evidence before retrying.

## 1. Inspect

```text
python -m reviewctl status --workspace <workspace>
python -m reviewctl inspect --workspace <workspace>
python -m reviewctl next-action --workspace <workspace>
python -m reviewctl attest --workspace <workspace>
```

The status and attestation outputs identify the job state, failed node, provider error kind, Registry integrity, dependency graph, and preserved completed nodes.

Treat the Registry record with `artifact_id=job_outcome` as the sole canonical
`JobOutcomeV1` authority. Validate any fixed `job_outcome_v1.json` file as the
mutable `job_outcome_compatibility_projection/v1` against that Registry ID/hash;
a projection write failure is only a warning/reconcile issue. Treat the immutable
Registry-owned `resume_state_report/v1` as the resume-report authority. Use the
fixed `resume_state_report.json` path only as the explicit legacy fallback when
the Registry record is absent.

## 2. Choose the smallest safe action

- Provider quota, retryable HTTP, transient network, or invalid-response failure: inspect the provider receipt and retry only the failed node when `next-action.safe_to_retry` is true.
- Stale or tampered artifact: do not resume from it. Run `reconcile --dry-run`, retain the evidence, and create a report-only repair plan.
- Missing validation closure: run `validate` to execute the current validation service, then use `validation-status` to inspect the durable closure. Repair the input chain or rerun validation; a legacy text report is not a validation source. If validation was explicitly optional and disabled, require the current typed `ValidationDispositionV1/v1`, `validation_status=not_requested`, `validation_required=false`, `validation_enabled=false`, `allow_unvalidated=true`, its exact CurrentArtifactSet binding, and its zero-call closure instead; it is not evidence that validation passed.
- A running or pending queue job: use `cancel`; retry only after the cancellation marker is cleared by a new `resume`/retry attempt. A worker that loses its lease heartbeat is fenced from completing or releasing the old claim. Queue publication stages bytes privately, then uses queue-store -> Registry lock order; target and `lease_publication_manifest` are committed atomically. An immutable orphan after Registry failure is evidence to retain, not a reason to restore a fixed target. A pre-existing identical content-addressed file is reused and never deleted by a failed alias registration; different bytes fail closed before Registry mutation.
- Invalid current-set target: stop and retain the evidence. `switch_current_artifact_set` and `resolve_current_artifact_set` require the accepted type/version for every slot and matching promotion validation evidence; an arbitrary READY JSON file is not a substitute.
- Adoption failure: inspect coverage audit, stage health, final-outline hash, and blocking critiques. Do not bypass the gate.
- A `run_all` job that stops before validation is not automatically complete: inspect
  the durable `StagePlan` and current-set requirement. If validation was optional
  and disabled, analyze/outline/review still need complete stage-indexed closure
  and a current set; if validation was required, run the missing `validate` stage.
- A zero-call stage is complete only when its expected-call graph and dependencies
  verify, terminal model calls are zero, observed receipts are empty, and the
  typed source evidence is present. Do not create an empty receipt ledger to make
  the stage look complete. For all-reuse Stage 1, check unique SourceBundle
  identities, real Registry source-artifact bindings, and no current-epoch
  provider receipts. For mixed reuse/generation, verify that only generated
  papers appear in the expected call graph; summary-source zero-call stages use
  typed summary-source evidence. A per-paper reused `summary_file` must be a
  canonical one-item array with a valid `stage1_reusable_summary_manifest/v1`;
  do not substitute a JSON object envelope or an unregistered path. Stage 1 reuse
  must resolve authority from the parent/current Registry through the external
  resolver or from a self-binding typed manifest. A current snapshot marked
  `current_snapshot_derived_from_external_authority=true` is derived evidence,
  never authority; path-only/current-snapshot/bare-summary inputs and synthetic
  IDs/hashes are insufficient. Exact equality includes the real PDF byte SHA,
  extracted/semantic hashes, preprocess/input/prompt/provider/model/schema/visual
  hashes, and normalized summary payload hash. Same bytes moved to another path
  are allowed with location tracing; different bytes invalidate reuse even when
  text hashes match. Provider-generated sources with calls require the original
  Registry-verified receipt closure and ledger.
  During Registry-detached typed-manifest recovery, retain the referenced source
  summary, provider receipt closure, and receipt ledger required when calls occurred,
  and verify their hashes. The manifest alone is not a self-contained
  cryptographically authenticated archive, signed provenance, single-file portable
  bundle, or cross-host portability proof.

## 3. Resume

```text
python -m reviewctl resume --workspace <workspace>
```

Resume creates a new append-only attempt. It may reuse only Registry-verified artifacts and replay records whose node, schema, route, model, prompt-template, payload, input, and configuration hashes match exactly. A stale replay is a cache miss, not permission to reuse it.

## 4. Export only after closure

```text
python -m reviewctl validate --workspace <workspace>
python -m reviewctl validation-status --workspace <workspace>
python -m reviewctl export --workspace <workspace>
```

`canonical_unvalidated` is publishable only under the exact typed not-requested
policy and complete stage closure; its provenance must say that semantic
validation was not performed. If the export is `untrusted`, keep it as forensic
evidence and do not publish it as a completed review. Do not delete the
workspace, rewrite `artifact_registry.json`, or mark a stage healthy by hand.

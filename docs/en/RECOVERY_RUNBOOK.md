# Recovery Runbook

This runbook is fail-closed. Preserve the workspace and inspect evidence before retrying.

## 0. Pin the runtime before production use

Use the maintainer-approved final clean source SHA, Python 3.11, and the lock file from that same checkout. Do not treat a dirty workspace, an old batch wrapper, a local fixture, or an unreviewed executable path as production acceptance. The public CLI is `python -m reviewctl`; it consumes a `RuntimeJobSpec` and uses the same Runner/Bridge/Registry contracts as the GUI.

Replace the placeholders before running this PowerShell block. The SHA must come from the final release record; this manual does not declare a current checkout accepted.

```powershell
$RepoRoot = (Resolve-Path -LiteralPath '<approved final checkout path>').Path
$ExpectedSha = '<approved final clean SHA>'
$ActualSha = (& git -C $RepoRoot rev-parse HEAD).Trim()
$GitExit = $LASTEXITCODE
$Dirty = @(& git -C $RepoRoot status --porcelain)
$StatusExit = $LASTEXITCODE
if ($GitExit -ne 0 -or $StatusExit -ne 0 -or $ActualSha -cne $ExpectedSha -or $Dirty.Count -ne 0) {
    throw 'Source SHA mismatch or dirty checkout; stop before production use.'
}

$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    py -3.11 -m venv (Join-Path $RepoRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 venv creation failed.' }
}
$PythonVersion = (& $Python --version 2>&1 | Out-String).Trim()
if ($PythonVersion -notmatch '^Python 3\.11\.') { throw "Expected Python 3.11; found $PythonVersion" }
& $Python -m pip install -r (Join-Path $RepoRoot 'requirements-py311-windows-prod.lock')
if ($LASTEXITCODE -ne 0) { throw 'Production lock install failed.' }
$Workspace = (Resolve-Path -LiteralPath '<existing workspace path>').Path
```

Use that `$Python` path for every CLI example below. Keep the private `config.ini` and `.env` outside version control. `doctor`, `preflight`, `plan`, and `status` do not authorize provider calls; `run`, `resume`, `retry-node`, `validate`, and `repair-promote` may execute provider work under the saved route and budget controls.

## 1. Inspect

```powershell
& $Python -m reviewctl status --workspace $Workspace
& $Python -m reviewctl inspect --workspace $Workspace
& $Python -m reviewctl next-action --workspace $Workspace
& $Python -m reviewctl attest --workspace $Workspace
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
- Unknown/ambiguous provider outcome, incomplete raw response, timeout after dispatch, or lost connection: do not resend the request or start a second job while the outcome is unknown. Read the request audit, receipt ledger and registered response snapshot; run `reconcile --dry-run`. Retry only after evidence resolves the prior attempt as not dispatched/not accepted and the resulting `next-action.safe_to_retry` is true. An unresolved outcome remains blocked and must not be treated as “no call happened.”
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

```powershell
& $Python -m reviewctl resume --workspace $Workspace
```

Resume creates a new append-only attempt. It may reuse only Registry-verified artifacts and replay records whose node, schema, route, model, prompt-template, payload, input, and configuration hashes match exactly. A stale replay is a cache miss, not permission to reuse it.

Ordinary runs use a strict finite `provider-aggregate-budget-contract-v2` envelope. The admitted run reserves calls, output allowance and retry attempts just in time before each provider request; aggregate usage plus open reservations must stay within the original limits. `resume` does not clear consumed calls/tokens/retries, reset the first-start time, extend the original absolute deadline, or turn an exhausted budget into fresh authority. Do not edit the budget state, envelope, saved spec or job ID to recover room. If the deadline or budget has expired, stop and use only a separately supported and approved continuation path; it cannot re-open an old paid-pilot authorization.

Use the `retry-node` CLI only for the node named by `next-action` and only when its exact retry binding is current:

```powershell
$Node = Read-Host 'Exact failed Outline v3 node ID from next-action'
& $Python -m reviewctl retry-node --workspace $Workspace --node $Node
```

`retry-node` does not accept an arbitrary rerun request. If the outcome is unknown, the retry gate must remain closed until reconciled.

## 4. Export only after closure

```powershell
& $Python -m reviewctl validation-status --workspace $Workspace
```

`canonical_unvalidated` is publishable only under the exact typed not-requested
policy and complete stage closure; its provenance must say that semantic
validation was not performed. If the export is `untrusted`, keep it as forensic
evidence and do not publish it as a completed review. Do not delete the
workspace, rewrite `artifact_registry.json`, or mark a stage healthy by hand.

`validation-status` is a read-only inspection. Run `validate` only when the current `next-action` or missing closure calls for a new validation and its budget and route authorization remain valid; it executes the current ValidationExecutionService and may make Validator calls. Read `validation-status` again after it returns. `export` writes an export bundle and forensic evidence. Export only the current Registry revision after adoption, Writer and required validation closure are complete; if a repair, promotion, validation run or current pointer change occurs, export that newer revision again. Record the returned status, bundle path, current-set/validation provenance and bundle SHA256. A zero exit code alone is not acceptance.

```powershell
# Only after the current next action requires a fresh Validator run:
& $Python -m reviewctl validate --workspace $Workspace
& $Python -m reviewctl validation-status --workspace $Workspace

# Only after adoption, Writer and the required validation closure are complete:
& $Python -m reviewctl export --workspace $Workspace
```

## 5. SourceRevision is a narrow, plan-bound reuse path

The `source-revision-plan` command writes a plan file and returns a normalized child spec bound to that file; it makes zero provider POSTs, creates no workspace, and does not mutate the parent. The plan output must be new and outside both workspaces. Normal `reviewctl run --spec <bound-child-spec>` verifies and consumes `metadata.source_revision_plan` through the Runner/Bridge; it is not a sidecar cache that must be applied manually.

Both the already-completed parent RuntimeJobSpec and new child spec must have explicitly declared `metadata.provider_semantic_contract_version` equal to `source-closed-provider/v2` before their runs. Missing versions are treated as `legacy/v1`; an old v1 parent cannot grant successor reuse. Do not patch old metadata or rewrite an old receipt ledger. If a new eligible parent is ever needed, it requires a separately approved new run with v2 declared before execution.

The currently admitted mode is `replace-existing-pdf/v1`: the parent must have a complete successful canonical run and clean current set; config bytes and ReviewIntent must match; the ordered unique canonical paper key list must stay the same; exactly one PDF byte identity may change; and the child requires a new job ID/workspace plus its own finite budget. V2 canonicalization is limited to verified PDF locator/fingerprint fields. Full Stage 1 text and source audit remain in the provider request identity. At each response lookup, an exact request, prompt, route/config, schema or source-scope mismatch forbids parent reuse; when the plan remains admissible, the child may recompute under its own finite budget. Config or source-closure changes that invalidate admission must be rejected and re-reviewed, not treated as a way around the plan. A planned revision is not a guarantee that a particular parent response will be reused.

Run the current-parser form after preparing those specs and an approved plan output path:

```powershell
& $Python -m reviewctl source-revision-plan --spec '<child-spec.json>' `
    --parent-workspace '<completed-parent-workspace>' --output '<new-plan-path.json>'
```

Save the returned `normalized_child_spec` in UTF-8 JSON and use that exact spec for `plan` and `run`. If plan admission rejects the parent, source, config, budget or closed authority, retain the evidence; do not downgrade to a less strict manual response-copy path.

## 6. Adoption, repair, and revalidation

Outline adoption is an explicit human decision bound to the exact READY artifact ID and hash. Review it in `inspect` before running:

```powershell
$Artifact = Read-Host 'Reviewed READY final-outline artifact ID'
$ExpectedHash = Read-Host 'Exact artifact content hash from inspect'
$Actor = Read-Host 'Actual adopting researcher/operator'
$Reason = Read-Host 'Evidence-based adoption reason'
& $Python -m reviewctl adopt --workspace $Workspace --artifact $Artifact `
    --expected-hash $ExpectedHash --actor $Actor --reason $Reason
& $Python -m reviewctl resume --workspace $Workspace
```

The resumed stage plan, not an ad hoc CLI flag, determines whether Writer and Validator run. A failed adoption gate, incomplete Writer stage, or missing required validation closure is not complete.

For an approved repair, inspect the current plan first. `repair-plan` is the report/plan step; review its exact proposal and inputs before applying it. `repair-apply` accepts a plan ID, and a manual proposal additionally requires a typed proposal file plus actor and reason:

```powershell
& $Python -m reviewctl repair-plan --workspace $Workspace
$PlanId = Read-Host 'Exact reviewed READY repair plan ID'
& $Python -m reviewctl repair-apply --workspace $Workspace --plan $PlanId
$TransactionId = Read-Host 'Exact repair transaction ID returned by repair-apply'
$RepairActor = Read-Host 'Actual owner approving promotion'
$RepairReason = Read-Host 'Reason for promotion and revalidation'
& $Python -m reviewctl repair-promote --workspace $Workspace --transaction $TransactionId `
    --actor $RepairActor --reason $RepairReason
& $Python -m reviewctl validation-status --workspace $Workspace
```

Applied draft/manifest artifacts remain quarantined until `repair-promote` is given the exact transaction ID, actor and reason. Promotion reruns the current Validator and advances pointers only after its gates pass. Append `--validator-host-ack-file <approved-file>` only when the current Validator preflight requires that exact route acknowledgement. Afterwards inspect `validation-status` and `status`; run a separate `validate` only when the current next action says a fresh validation is still required. Then export the resulting current revision.

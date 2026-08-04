# Agent Runbook

`reviewctl` is the control surface for an existing job workspace. Read commands
are provider-free; `validate` is the explicit command that executes the current
`ValidationExecutionService` and persists a new validation attempt. The control
plane reads the durable Registry, job outcome, stage terminals, provider
receipts, and Outline v3 DAG/replay state.

Completion and export resolve the atomic `CurrentArtifactSetV1` through
`current-artifact-set:pointer` and then build `CurrentStageClosureMapV1`; a
historical READY artifact is not a substitute for the current set.

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
- A lease is rechecked at the ArtifactRegistry publication boundary. A stale
  worker, including a Windows `spawn` child whose claim expired, cannot publish
  a canonical artifact merely because its local queue snapshot is stale.

## Validation, repair, and adoption

- `validate` executes current validation over the current v3 review draft, v3 citation manifest, and evidence inputs, then persists `ValidationRunResultV1`, receipts, and Registry dependencies. `validation-status` is the read-only closure check, including Registry identity and hash equality.
- `repair-plan` is report-first. It may persist a hash-bound plan and transaction record, but it does not edit canonical artifacts.
- `repair-apply` accepts only an explicitly `auto_apply_safe` plan. Its outputs are registered as `quarantined` derived versions; canonical READY draft, manifest, outline, and DOCX artifacts are not replaced.
- `repair-promote --transaction <id> --actor <actor> --reason <reason>` revalidates the quarantined derived inputs through the current service, requires a complete closure, and only then creates a new version and advances current pointers.
- `adopt --artifact <final_outline_id> --actor <actor>` is explicit. It requires a READY final outline, passing coverage audit, adoptable stage health, matching hashes, and no blocking critique. It writes `adopted_final_outline` and an immutable adoption audit record. Outline v3 candidate plans are not silently promoted by this command.

## Export and trust

`export` creates a declarative ZIP containing verified Registry files, provenance, checksums, completion evidence, and validation closure. A canonical registration failure returns `untrusted` with an empty bundle path and artifact ID after deleting the temporary ZIP. `attest` records a dependency-graph and file-hash audit. Trust labels are:

- `canonical_verified`: completion and validation closure are clean and all included READY files verify;
- `manual_repaired`: hashes verify but manual-modification metadata is present;
- `untrusted`: any integrity, completion, closure, or registration condition is unresolved.

Never infer completion from a DOCX existing on disk, a human-readable report, a mutable queue flag, or a manually edited Registry/Stage Health file.

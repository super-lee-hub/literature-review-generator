# Agent Runbook

`reviewctl` is the provider-free control surface for an existing job workspace. It reads the durable Registry, job outcome, stage terminals, provider receipts, and Outline v3 DAG/replay state. It does not edit those sources directly.

## Safe command order

```text
python -m reviewctl doctor --config <config.ini>
python -m reviewctl status --job <job_id>
python -m reviewctl next-action --job <job_id>
python -m reviewctl validate --job <job_id>
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

## Validation, repair, and adoption

- `validate` performs a read-only closure check over the v2 review draft, v3 citation manifest, and `ValidationRunResultV1`, including Registry identity and hash equality.
- `repair-plan` is report-first. It may persist a hash-bound plan and transaction record, but it does not edit canonical artifacts.
- `repair-apply` accepts only an explicitly `auto_apply_safe` plan. Its outputs are registered as `quarantined` derived versions; canonical READY draft, manifest, outline, and DOCX artifacts are not replaced.
- `adopt --artifact <final_outline_id> --actor <actor>` is explicit. It requires a READY final outline, passing coverage audit, adoptable stage health, matching hashes, and no blocking critique. It writes `adopted_final_outline` and an immutable adoption audit record. Outline v3 candidate plans are not silently promoted by this command.

## Export and trust

`export` creates a declarative ZIP containing verified Registry files, provenance, checksums, completion evidence, and validation closure. `attest` records a dependency-graph and file-hash audit. Trust labels are:

- `canonical_verified`: completion and validation closure are clean and all included READY files verify;
- `manual_repaired_legacy`: hashes verify but manual-modification metadata is present;
- `untrusted`: any integrity, completion, closure, or registration condition is unresolved.

Never infer completion from a DOCX existing on disk, a human-readable report, a mutable queue flag, or a manually edited Registry/Stage Health file.

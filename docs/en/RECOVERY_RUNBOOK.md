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

## 2. Choose the smallest safe action

- Provider quota, retryable HTTP, transient network, or invalid-response failure: inspect the provider receipt and retry only the failed node when `next-action.safe_to_retry` is true.
- Stale or tampered artifact: do not resume from it. Run `reconcile --dry-run`, retain the evidence, and create a report-only repair plan.
- Missing validation closure: run `validate` to execute the current validation service, then use `validation-status` to inspect the durable closure. Repair the input chain or rerun validation; a legacy text report is not a validation source.
- A running or pending queue job: use `cancel`; retry only after the cancellation marker is cleared by a new `resume`/retry attempt. A worker that loses its lease heartbeat is fenced from completing or releasing the old claim.
- Adoption failure: inspect coverage audit, stage health, final-outline hash, and blocking critiques. Do not bypass the gate.

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

If the export is `untrusted`, keep it as forensic evidence and do not publish it as a completed review. Do not delete the workspace, rewrite `artifact_registry.json`, or mark a stage healthy by hand.

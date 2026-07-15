# Compatibility Contract

Compatibility is additive and fail-closed. Old artifacts remain readable, but reading them does not grant new identity, validation, or readiness guarantees.

## Legacy workspace rules

- Missing `SourceInventoryV1`, readiness policy, attempt history, or V2 dependency identity is projected as `legacy_unverified`.
- Old `success` reads as a compatibility projection of `canonical_ready`; Queue state is never derived from it.
- Old validation reports are readable through a compatibility adapter but do not satisfy `ValidationRunResultV1`.
- Old Registry dependencies are normalized to V2 fields when possible. Missing artifact identity/hash never becomes `ready` by inference.
- Legacy Markdown outlines are usable only when Outline v2 is explicitly disabled. V2 review fails closed without a current registered adopted outline and health sidecar.

`status` and `reconcile` are read-only with respect to a summary-only legacy workspace. They report `legacy_unverified` and the need for an explicit migration or rerun, but do not create a Registry, job outcome, or audit record. The only public compatibility migration command is:

```powershell
python -m runtime.cli migrate-legacy <workspace> --actor <operator> --reason <reason>
```

`--actor` and `--reason` are required. The command makes no provider calls and materializes only a fail-closed compatibility head: `compatibility_status=legacy_unverified`, `canonical_ready=false`, and `requires_attention=true`, plus an immutable `AuditRecordV1`. Repeating the same migration is byte-idempotent. Native or non-summary-only workspaces are rejected; migration never upgrades legacy evidence to canonical readiness.

## Audited compatibility actions

Explicit legacy summary reuse, ambiguous identity selection, manual outline adoption, force deletion, and quarantine release produce immutable `AuditRecordV1` artifacts. An audit includes actor, reason, scope, input hashes, policy snapshot, and artifact ID/hash references. A long-lived boolean bypass is not supported.

## Path and dependency rules

- Spec paths resolve from the spec directory; config paths from the config directory; summary-owned relative paths from the summary directory.
- Cross-job dependencies use `dependency_kind=external_job` with `job_id + artifact_id + content_hash` as identity. `path` is a location projection.
- A parent artifact cannot be deleted while a non-invalid child dependency remains, unless a force-delete audit is written and affected children are invalidated.

## Optional integration boundaries

Live API, Playwright, and heavy OCR tests are optional markers. They require explicit enablement and prerequisites. Strict-offline tests reject external network access while allowing loopback and propagate the offline boundary to Python subprocesses.

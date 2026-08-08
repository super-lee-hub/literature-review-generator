# Validation Adjudication Reuse

Date: 2026-08-09 (Asia/Shanghai)
Repository: `super-lee-hub/literature-review-generator`

## Single-flight

Adjudication uses thread-keyed locks plus Windows `msvcrt` file locks and
POSIX `flock` for single-host, cross-process coordination. Raw checkpoint
files are coordination/cache state only; they never authorize a canonical
validation result.

## Typed reuse authority

The canonical reuse authority is `validation_adjudication_reuse_record/v1`.
It binds:

- job, source attempt, citation set, and stage
- canonical adjudication packet hash, prompt version, and validation schema
  version
- provider, model, endpoint type, and redacted provider config hash
- call ID, prompt hash, input hash, and schema hash
- provider output artifact ID/hash and normalized result hash
- source receipt ID/hash, source ledger identity when closure-bound, and
  source provider closure epoch/artifact identity when closure-bound
- current input dependency hashes

## Closure accounting

Fresh provider calls remain normal expected calls with receipts. Verified
reuse calls remain in the expected call set but are marked `verified_reuse`
and carry `reuse_evidence_artifact_id/hash`; the provider receipt closure
records them in `verified_reuse_call_ids` instead of requiring a receipt.
The closure artifact depends on the reuse records and provider output
artifacts, so the authority chain is Registry-backed.

## Fail-closed matrix

Reuse is rejected for raw checkpoint tamper, reuse-record tamper, provider
output tamper, missing provider output, provider config/model change, packet
change, prompt/schema version change, missing or mismatched source receipt,
missing source provider closure, Registry dependency mismatch, and wrong
job/attempt/source authority. The safe fallback is one fresh provider call.

## Scope

This is single-host only. No multi-host distributed lock or cryptographic
provenance is claimed.

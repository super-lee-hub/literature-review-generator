# Validation Adjudication Reuse

Date: 2026-08-09 (Asia/Shanghai)
Repository: `super-lee-hub/literature-review-generator`

## Single-flight

Adjudication uses thread-keyed locks plus Windows `msvcrt` file locks and
POSIX `flock` for single-host, cross-process coordination. Raw checkpoint
files are coordination/cache state only; they never authorize a canonical
validation result.

## Typed reuse authority

`validation_adjudication_reuse_record/v1` has two explicit authority states:

- `provisional` is usable only by the live service that owns the record, after
  that service observed the source output and receipt. It prevents duplicate
  transports inside the same single-flight context; it is not durable resume
  authority.
- `durable` is a closure-bound record. A reconstructed service may trust it
  only when the exact ready output, receipt, ledger, complete source closure,
  and Registry dependency graph all verify.

Both states bind:

- job, source attempt, citation set, and stage
- canonical adjudication packet hash, prompt version, and validation schema
  version
- provider, model, endpoint type, and redacted provider config hash
- call ID, prompt hash, input hash, and schema hash
- provider output artifact ID/hash and normalized result hash
- source receipt ID/hash, source ledger identity when durable, and source
  provider closure epoch/artifact identity when durable
- current input dependency hashes

## Closure accounting

Fresh provider calls remain normal expected calls with receipts. Same-service
followers consume the published result without replacing that normal expected
call, so the source closure records one observed transport and no verified
reuse call. A later service using a durable closure-bound record creates a
`verified_reuse` expected call, records no current transport, and places the
call in `verified_reuse_call_ids`. The closure artifact depends on the reuse
evidence and provider output artifacts, so the authority chain is
Registry-backed.

## Fail-closed matrix

Reuse is rejected for raw checkpoint tamper, reuse-record tamper, provider
output tamper, missing provider output, provider config/model change, packet
change, prompt/schema version change, missing or mismatched source receipt,
missing source provider closure, Registry dependency mismatch, and wrong
job/attempt/source authority. The safe fallback is one fresh provider call.

## Scope

This is single-host only. No multi-host distributed lock or cryptographic
provenance is claimed.

# Current Artifact Boundary

The production runtime has one clean-cut contract. Only current typed settings,
current job workspaces, and current registered artifacts may enter a run. Stale
inputs are rejected with an explicit diagnostic; they are not projected into a
new readiness state and are never silently upgraded.

## Accepted inputs

- `config.ini` is validated against the current typed settings schema.
- A job must have a source inventory, readiness policy, append-only attempt
  history, Registry identity, and current stage terminals.
- Outline Intelligence v3 is the only outline production path. Its evidence
  views, corpus ledger, review intent, coverage contract, relation map,
  candidate plan, node DAG, receipts, and adoption record are registered
  artifacts.
- Review, citation, validation, repair, export, and attestation consume the
  current versioned contracts and verify their Registry dependencies and hashes.

## Deprecated preprocess setting

`[Preprocess].strategy_policy` is accepted only while reading legacy config
files. It is not a production parser-routing control, is removed by config
normalization, and is excluded from the Stage 1 semantic reuse fingerprint.
Use `parser_mode`, `primary_parser`, `fallback_parser`, `extractor_profile`,
`ocr_mode`, `ocr_languages`, and `use_markdown_as_stage1_input` for current
preprocessing behavior.

## Rejected stale inputs

- Old configuration sections, old workspace projections, and unregistered
  report files fail closed.
- A Markdown outline or a human-readable report cannot satisfy an outline,
  review, validation, readiness, or completion gate.
- The runtime exposes no migration command, old CLI, external stage handler, or
  adapter that turns a stale artifact into a current artifact.
- Missing identity, dependency, receipt, terminal, or content-hash evidence
  produces a quarantined diagnostic and leaves `canonical_ready=false`.

## Audited state changes

Explicit summary reuse, ambiguous identity decisions, outline adoption, repair
application, force deletion, and quarantine release write immutable audit
records containing actor, reason, scope, input hashes, policy snapshot, and
artifact identifiers. No long-lived boolean bypass is supported.

## Path and dependency rules

- Spec, config, and summary relative paths resolve from their owning file.
- Cross-job dependencies use `external_job` with `job_id`, `artifact_id`, and
  `content_hash` as identity; a path is only a location projection.
- A parent artifact cannot be deleted while an invalidated child dependency is
  absent, unless the force-delete audit also invalidates affected children.

## Optional integration boundaries

Live API, Playwright, and heavy OCR tests are optional markers and require
explicit enablement and prerequisites. Strict-offline tests reject external
network access while allowing loopback and propagate the boundary to Python
subprocesses.

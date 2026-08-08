# Artifact Trust Model

Trust is established from durable, hash-bound facts rather than filenames or successful process exit alone.

| Layer | Canonical evidence | Rule |
|---|---|---|
| Source identity | `source_inventory_v1.json`, source bundle, registered paper artifacts | identity ambiguity remains quarantined |
| Artifact graph | `artifact_registry.json` v2 | READY paths, hashes, schemas, and dependencies must verify |
| Runtime | `job_outcome_v1.json`, append-only attempts, stage terminals | completion is evaluated by `CanonicalCompletionEvaluator` |
| Outline | registered v2 chain plus Outline v3 evidence/ledger/matrix/relation/DAG artifacts | candidate and replay projections are not adopted truth |
| Review | review draft v2 and citation manifest v3 | the manifest drives citation identity and render policy |
| Validation | `ValidationRunResultV1` and exact Registry input closure | human-readable reports are projections |
| Repair | hash-bound report-first plan and transaction | applied outputs are new quarantined artifacts |
| Export | declarative bundle and forensic attestation | trust label is explicit and reproducible |

`ready` means the Registry can verify the file and all required dependencies. `quarantined` means the file is preserved for review but cannot become canonical input. `invalid` means it must not be used for resume or publication.

Canonical artifacts are never overwritten by the v3, validation, repair, export, or adoption surfaces. An explicit adoption transaction creates `adopted_final_outline` only after final-outline, coverage-audit, stage-health, and completion gates pass. Cancellation is cooperative and is itself recorded as a derived request artifact.

The absence of evidence is a blocked or unvalidated state, not evidence of support, completion, or publication readiness.

# Current feature matrix

This matrix describes the current main surface. It intentionally avoids dated
pytest totals; exact counts belong in PR and release verification evidence.
The Windows GitHub Actions workflow is the current remote evidence gate for
strict-offline tests, Pyright, compile, collection, public CLI smoke, doctor,
and committed-range whitespace checks.

| Capability | Current status | Boundary |
| --- | --- | --- |
| Outline Intelligence v3 | `E2E_VERIFIED` | Only current production Outline path |
| Free Mode typed intent | `E2E_VERIFIED` | `free_mode_intent_input/v1` |
| Free Mode -> ReviewIntent | `E2E_VERIFIED` | Literal idea projection and writer binding |
| Free Mode Writer replay binding | `E2E_VERIFIED` | Context and identity remain bound on replay |
| Stage 1 independence from Free Mode | `E2E_VERIFIED` | Stage 1 has its own current contracts |
| Concept Mode | `DISABLED` | Stale requests are rejected |
| Validation adjudication single-flight | `E2E_VERIFIED` | Single-host scope only |
| Registry-backed durable adjudication reuse | `E2E_VERIFIED` | Provisional and durable closure-bound authority |
| `reviewctl` control plane | `E2E_VERIFIED` | `RuntimeJobSpec` -> `AgentRuntimeRunner` |
| Stage 3 review contract | `E2E_VERIFIED` | `review_draft` v3, `citation_manifest` v3, DOCX |
| Queue fencing/publication | `E2E_VERIFIED` | Current lease and Registry boundary |
| JobOutcome / CurrentArtifactSet | `E2E_VERIFIED` | Registry-backed canonical authority |
| Repair/promotion and export admission | `E2E_VERIFIED` | Current transaction and dependency gates |

## Evidence limitations

The following are not claimed by strict-offline evidence:

- live API/provider verification — `NOT VERIFIED`
- Playwright — `NOT VERIFIED`
- heavy OCR — `NOT VERIFIED`
- multi-host single-flight — `NOT VERIFIED`
- multi-host publication/fencing — `NOT VERIFIED`
- cryptographic provenance verification — `NOT CLAIMED`

The stale-lock warning that `reviewctl doctor` may report is a non-blocking
diagnostic. This matrix does not authorize automatic lock deletion.

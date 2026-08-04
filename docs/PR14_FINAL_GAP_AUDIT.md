# PR #14 Final Gap Audit

Date: 2026-08-04 (Asia/Shanghai)
Repository: `super-lee-hub/literature-review-generator`
Branch: `codex/platform-hardening-outline-v3`
PR: #14 (`Draft` / `Open` / `Unmerged`)
Code/test verification commit: `65e9d24a9695c21846e3ab6868bec5212fdb5ad5`

This audit covers the requested current-path remediation. It does not promote
deterministic provider injection to live-provider verification.

The ZIP files are absent from the committed diff and remote branch. The local
operator reports that they were not read or staged; remote GitHub evidence
cannot independently verify local read access.

## Requirement matrix

| Requirement | Implementation and evidence | Status | Limitation |
|---|---|---|---|
| Atomic repair promotion | Immutable prepared promotion transaction, output/hash/lineage binding, one Registry OS lock/CAS for transaction + `CurrentArtifactSet` + pointer, unchanged pointer on validation/CAS failure, and immutable READY transaction checks | `E2E_VERIFIED` | No live-provider run |
| Version-aware validation | Dispatch keyed by `(artifact_type, artifact_version)` for current production and Outline artifacts; explicit known compatibility projections; malformed fixtures and unknown current versions fail closed | `E2E_VERIFIED` | Historical compatibility versions remain explicitly readable |
| Outline stability | `off`/`smoke`/`full`, compact smoke perturbations, comprehensive full perturbations, checkpointed subruns, explicit order, thresholds, exact replay, zero-transport fresh executor, and typed critic retry scope | `E2E_VERIFIED` | No live-provider run |
| Call/cost admission | `max_provider_calls` and `max_estimated_cost` preflight before transport, with persisted preflight evidence and rejection tests | `E2E_VERIFIED` | Cost estimate is policy input, not billing data |
| Stage-indexed provider closure | Durable requested-stage spec drives analyze/outline/review/validate closure entries with epoch, graph, input, config, schema, status, artifact identity/hash, and Registry dependencies; `CurrentArtifactSet` remains authoritative | `E2E_VERIFIED` | No external-provider evidence |
| Stage plan and `run_all` completion | Durable stage plan normalizes `run_all` to analyze/outline/review/validate when validation is enabled, omits only optional validation when disabled, still requires a current set, and blocks derivation/outline-only canonical readiness without that set | `E2E_VERIFIED` | Validation is still provider-free only when explicitly disabled or not requested |
| Receipt closure | Fully bound calls, expected/missing/unexpected/out-of-scope/hash mismatch cases, same-epoch checks, and historical isolation | `E2E_VERIFIED` | No live-provider run |
| Queue fencing | Canonical Registry writes require the active lease; Windows `spawn` stale-worker and lease-loss tests reject stale publication | `E2E_VERIFIED` | No production multi-host run |
| Negative/UI boundary coverage | Focused malformed-artifact, closure, promotion, export-failure, queue, GUI-controller, and label/status tests | `E2E_VERIFIED` | Playwright was not run in the offline gate; no single umbrella failure-chain suite |
| Trust-bound export | Versioned `export_bundle` validator, stage/receipt/current-set evidence, registration-failure cleanup, and forensic read-back | `E2E_VERIFIED` | Future work may add more checksum/read-failure permutations |

## Fresh local evidence

- `26 passed` in the queue/lease/Windows-spawn group.
- `77 passed` in the closure/completion/stage-plan/current-artifact/GUI/export group.
- `17 passed` in the Outline v3 DAG/replay/stability group.
- The current runtime-shaped E2E passed (`1 passed`), and the current
  production-shaped E2E passed (`1 passed`).
- `708 passed, 22 deselected` from `730 collected` under
  `--strict-markers -m "not live_api and not playwright and not heavy_ocr"`.
- `python -m compileall -q .`: passed.
- `python -m pyright`: `0 errors, 0 warnings, 0 informations`.
- `python -m reviewctl doctor --config config.ini.example`: `ok=true`,
  read-only, zero provider network calls; status warning only reports the
  repository's pre-existing stale locks.
- `git diff --check`: passed before commit.

## Stage, stability, and retry evidence

- A three-paper Stage 1 graph predeclares the complete `stage1_analyze`
  expected-call set, binds each call to job/attempt/node/epoch/graph/config/
  schema/input identities, then finalizes the receipt closure after transport.
  Reused summaries remain explicit `reused` nodes and do not create provider
  calls.
- With candidate count `c`, the Outline v3 core provider call count is
  `c + 5` (relation adjudication, `c` candidate generations, three critics,
  and arbitration). The default `c=5` therefore has 10 core calls: `off=10`,
  `smoke=20` (one compact non-replay perturbation plus exact replay with zero
  transport), and `full=60` (five non-replay perturbations plus exact replay).
  The configured smoke admission ceiling is 24 calls and the estimated-cost
  ceiling is 5.0; preflight rejects before transport and records the estimate.
- `test_node_dag_contains_required_nodes_and_preserves_completed_candidates_on_critique_retry`
  proves that a failed `structure_critique` reruns the critic and downstream
  arbitration/health closure while preserving completed candidate generation;
  the retry does not regenerate those candidates.

## Explicit non-claims

- No external live API, Playwright, or heavy OCR result is claimed.
- The deterministic three-PDF production-shaped chain is `E2E_VERIFIED`, not
  `LIVE_VERIFIED`.
- No merge, main update, Ready-for-review transition, or auto-merge is part of
  this task.
- Remote SHA, fresh GitHub Actions result, final PR body, and final PR state
  are verified after the final push rather than inferred from older evidence.

# PR #14 Final Gap Audit

Date: 2026-08-04 (Asia/Shanghai)
Repository: `super-lee-hub/literature-review-generator`
Branch: `codex/platform-hardening-outline-v3`
PR: #14 (`Draft` / `Open` / `Unmerged`)
Code/test verification: refreshed at the final pre-publication branch tip; the
final commit SHA is recorded in the PR description and delivery report.

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
| Outline stability | `off`/`smoke`/`full`, one additional full reversed-summary smoke chain, comprehensive full perturbations, checkpointed subruns, explicit order, thresholds, exact replay, zero-transport fresh executor, and typed critic retry scope | `E2E_VERIFIED` | No live-provider run |
| Call/cost admission | Per-node `OutlineProviderCallPlan` with conservative critic/arbitration input bounds, call/token preflight, explicit pricing-source policy, persisted actual usage/cost when rates and usage are known | `E2E_VERIFIED` | Calculated cost is local policy evidence, never provider billing; unknown pricing disables only the monetary ceiling |
| Stage-indexed provider closure | Durable requested-stage spec drives analyze/outline/review/validate closure entries with epoch, graph, input, config, schema, status, artifact identity/hash, and Registry dependencies; `CurrentArtifactSet` remains authoritative | `E2E_VERIFIED` | No external-provider evidence |
| Stage plan and `run_all` completion | Durable stage plan normalizes `run_all` to analyze/outline/review/validate when validation is enabled, omits only optional validation when disabled, still requires a current set, and blocks derivation/outline-only canonical readiness without that set | `E2E_VERIFIED` | Validation is still provider-free only when explicitly disabled or not requested |
| Optional validation disposition | Typed `ValidationDispositionV1` with `status=not_requested`, `allow_unvalidated=true`, stage/spec hashes, review/citation/DOCX identities, an empty validation receipt closure, and current-set binding | `E2E_VERIFIED` | It records intentional non-requesting; it does not claim validation passed |
| Receipt closure | Fully bound calls, expected/missing/unexpected/out-of-scope/hash mismatch cases, same-epoch checks, and historical isolation | `E2E_VERIFIED` | No live-provider run |
| Queue fencing | Lease-generation staging, queue-lock -> Registry publication order, immutable byte finalization, lease publication manifests, Windows `spawn` stale-worker races, and Registry-failure orphan tests | `E2E_VERIFIED` | No production multi-host run |
| Negative/UI boundary coverage | Focused malformed-artifact, closure, promotion, export-failure, queue, GUI-controller, and label/status tests | `CONTROLLER_VERIFIED` | Playwright was not run in the offline gate; no single umbrella failure-chain suite |
| Trust-bound export | Versioned `export_bundle` validator, stage/receipt/current-set evidence, registration-failure cleanup, and forensic read-back | `E2E_VERIFIED` | Future work may add more checksum/read-failure permutations |

## Fresh local evidence

- Focused queue, closure, repair, export, Stage 1, validation, GUI-controller,
  adoption, multimodal, and Outline regressions passed during implementation;
  the strict full gate below is the authoritative aggregate.
- `726 passed, 22 deselected` from `748 collected` under
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
  Reused summaries remain explicit `reused` nodes with reuse evidence and do
  not create provider calls or synthetic receipts; an adversarial missing-paper
  closure remains blocked.
- With candidate count `c`, the Outline v3 core provider call count is
  `c + 5` (relation adjudication, `c` candidate generations, three critics,
  and arbitration). The default `c=5` therefore has 10 core calls: `off=10`,
  `smoke=20` (one additional full reversed-summary decision chain plus exact
  replay with zero transport), and `full=60` (five additional full chains plus
  exact replay). The configured smoke admission ceiling is 24 calls. Monetary
  admission is enforced only when the durable configuration supplies a named
  pricing source and complete rates; otherwise the persisted `cost_status` is
  `unknown` and only call/token ceilings apply.
- The executor-level failure/resume regression injects a failed
  `candidate_2_provider_generation`, then records the exact resumed transport
  sequence (`candidate_2_provider_generation`, `structure_critique`,
  `coverage_critique`, `evidence_critique`, `arbitration`) while reusing
  unaffected candidates. The DAG planning test is not used as executor E2E
  evidence.

## Explicit non-claims

- No external live API, Playwright, or heavy OCR result is claimed.
- The deterministic three-PDF production-shaped chain is `E2E_VERIFIED`, not
  `LIVE_VERIFIED`.
- No merge, main update, Ready-for-review transition, or auto-merge is part of
  this task.
- Remote SHA, fresh GitHub Actions result, final PR body, and final PR state
  are verified after the final push rather than inferred from older evidence.

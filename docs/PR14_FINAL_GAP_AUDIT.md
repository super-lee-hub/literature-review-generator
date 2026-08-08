# PR #14 Final Gap Audit

Date: 2026-08-08 (Asia/Shanghai)
Repository: `super-lee-hub/literature-review-generator`
Branch: `codex/platform-hardening-outline-v3`
PR: #14 (`Draft` / `Open` / `Unmerged`)
Code/test verification: refreshed at the final pre-publication branch tip; the
final commit SHA is recorded in the PR description and delivery report.

This audit covers the requested current-path remediation. It does not promote
deterministic provider injection to live-provider verification.

Continuation verification covered supported `parser_mode` and `primary_parser`
invalidation; the post-merge cleanup deprecates the semantically inert
`strategy_policy` compatibility field. It also added path-independent
multimodal exact reuse plus visual-content/policy/selection invalidation and
verified that Registry-detached typed manifests reject missing source-summary,
provider-closure, required provider-ledger authority blobs, and the manifest
file itself.

> The ZIP files are absent from the committed diff and remote branch. The local operator reports that they were not read or staged; remote GitHub evidence cannot independently verify local read access.

## Requirement matrix

| Requirement | Implementation and evidence | Status | Limitation |
|---|---|---|---|
| Atomic repair promotion | Immutable prepared promotion transaction, output/hash/lineage binding, one Registry OS lock/CAS for transaction + `CurrentArtifactSet` + pointer, unchanged pointer on validation/CAS failure, and immutable READY transaction checks | `E2E_VERIFIED` | No live-provider run |
| Version-aware validation | Dispatch keyed by `(artifact_type, artifact_version)` for current production and Outline artifacts; explicit known compatibility projections; malformed fixtures and unknown current versions fail closed | `E2E_VERIFIED` | Historical compatibility versions remain explicitly readable |
| Outline stability | `off`/`smoke`/`full`, one additional full reversed-summary smoke chain, comprehensive full perturbations, checkpointed subruns, explicit order, thresholds, exact replay, zero-transport fresh executor, and typed critic retry scope | `E2E_VERIFIED` | No live-provider run |
| Call/cost admission | Per-node `OutlineProviderCallPlan` with conservative critic/arbitration input bounds, hard call/token preflight, provider/model-bound pricing only when all rates are explicit, and persisted usage/cost evidence | `E2E_VERIFIED` | Default pricing is unknown; calculated cost is local policy evidence, never provider billing |
| Stage-indexed provider closure | Durable requested-stage spec drives analyze/outline/review/validate closure entries with epoch, graph, input, config, schema, status, artifact identity/hash, and Registry dependencies; `CurrentArtifactSet` remains authoritative | `E2E_VERIFIED` | No external-provider evidence |
| Stage plan and `run_all` completion | Durable stage plan normalizes `run_all` to analyze/outline/review/validate when validation is enabled, omits only optional validation when disabled, still requires a current set, and blocks derivation/outline-only canonical readiness without that set | `E2E_VERIFIED` | Validation is still provider-free only when explicitly disabled or not requested |
| Optional validation disposition/export | Typed `ValidationDispositionV1` with `status=not_requested`, `allow_unvalidated=true`, stage/spec hashes, review/citation/DOCX identities, a zero-call validation receipt closure, current-set binding, and a generated `canonical_unvalidated` ZIP | `E2E_VERIFIED` | It records intentional non-requesting; it does not claim validation passed |
| Receipt closure | Fully bound calls, expected/missing/unexpected/out-of-scope/hash mismatch cases, zero-call/all-reuse and summary-source paths, same-epoch checks, and historical isolation | `E2E_VERIFIED` | No live-provider run |
| Stage 1 exact reuse and detached authority | Real supported preprocess settings invalidate reuse; equivalent visual evidence survives a path move; visual bytes, policy, or selection changes regenerate; missing referenced authority blobs fail closed | `E2E_VERIFIED` | The typed manifest is not a signed, self-contained, single-file, or cross-host portable archive |
| Queue fencing/publication | Lease-generation staging, queue-lock -> Registry publication order, one atomic target-plus-manifest Registry commit, manifest/fsync/CAS failure orphan tests, immutable byte finalization, and Windows `spawn` stale-worker current-set race | `E2E_VERIFIED` | No production multi-host run |
| Negative/UI boundary coverage | Focused malformed-artifact, closure, promotion, export-failure, queue, GUI-controller, and label/status tests | `CONTROLLER_VERIFIED` | Playwright was not run in the offline gate; no single umbrella failure-chain suite |
| Trust-bound export | Versioned `export_bundle` validator, typed current-set targets, stage/receipt/current-set evidence, canonical verified and canonical unvalidated admission, registration-failure cleanup, and forensic read-back | `E2E_VERIFIED` | Future work may add more checksum/read-failure permutations |

## Fresh local evidence

- `python -m pytest --collect-only -q`: `865` tests collected; the strict marker
  selection is `843 selected / 22 deselected`.
- The exact strict command
  `python -m pytest -q --strict-markers -m "not live_api and not playwright and not heavy_ocr"`
  passed: `843 passed, 22 deselected` in `3251.61s` (`54:11`).
- The changed Stage 1 module passed: `22 passed in 185.84s`. The consolidated
  frozen-contract suite passed: `204 passed in 1844.75s`. The validation-policy/
  parity suite passed separately: `13 passed in 438.33s`.
- `python -m compileall -q .`: passed.
- `python -m pyright`: `0 errors, 0 warnings, 0 informations`.
- `python -m reviewctl doctor --config config.ini.example`: `ok=true`,
  exit `0`, read-only, zero provider network calls; `status=warn` only reports
  the repository's pre-existing stale locks. Artifact-integrity and running-job
  checks are skipped because no workspace was supplied.
- `git diff --check`: passed on the final working tree and explicit staged diff.
- `3251.61s` is the local pytest duration. Final-SHA GitHub Actions run/job IDs,
  conclusion, and CI duration are reported separately in the PR description.

## Stage, stability, and retry evidence

- A three-paper Stage 1 graph predeclares the complete `stage1_analyze`
  expected-call set, binds each call to job/attempt/node/epoch/graph/config/
  schema/input identities, then finalizes the receipt closure after transport.
  The adversarial matrix covers missing/unexpected/historical receipts, all
  identity/hash bindings, expected-graph and dependency loss, paper/evidence
  loss, duplicate/unknown reuse identities, mixed reuse/generation, all reuse,
  and zero-call terminal/receipt violations. Reused summaries remain explicit
  `reused` nodes with real Registry source-artifact, source-manifest, runtime,
  and evidence dependencies; they do not create provider calls or synthetic
  receipts.
- With candidate count `c`, the Outline v3 core provider call count is
  `c + 5` (relation adjudication, `c` candidate generations, three critics,
  and arbitration). The default `c=5` therefore has 10 core calls: `off=10`,
  `smoke=20` (one additional full reversed-summary decision chain plus exact
  replay with zero transport), and `full=60` (five additional full chains plus
  exact replay). The configured smoke admission ceiling is 24 calls. Monetary
  admission is enforced only when the durable configuration supplies a named
  provider/model-bound pricing source and complete rates; otherwise the
  persisted `cost_status` is `unknown` and hard call/context/prompt/total-token
  ceilings still apply.
- The executor-level failure/resume regression separately injects a failed
  `coverage_critique` after relation adjudication, both candidate generations,
  and structure critique have durable receipts. Resume performs exactly
  `coverage_critique`, `evidence_critique`, and `arbitration`; candidate and
  structure hashes/receipt IDs remain unchanged. A separate binding-change
  test reruns only candidate 2 and its dependent critics/arbitration. The
  candidate-generation failure test remains a separate regression.

## Explicit non-claims

- No external live API, Playwright, heavy OCR, or multi-host result is claimed.
- The deterministic three-PDF production-shaped chain is `E2E_VERIFIED`, not
  `LIVE_VERIFIED`.
- No merge, main update, Ready-for-review transition, or auto-merge is part of
  this task.
- Remote SHA, fresh GitHub Actions result, final PR body, and final PR state
  are verified after the final push rather than inferred from older evidence.

## Non-blocking polish

- Production visual equality binds page number and bounding box, plus page range
  when present. The cleanup branch now includes an isolated bbox-only mutation
  regression with unchanged PDF and image bytes.
- The cleanup branch now includes a direct post-import deletion regression for
  the typed manifest file itself; the verifier remains fail-closed.

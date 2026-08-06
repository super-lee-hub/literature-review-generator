# PR #14 Final Verification

Verification date: 2026-08-06 (Asia/Shanghai)
Branch: `codex/platform-hardening-outline-v3`
Target: existing PR #14, kept Draft/Open/Unmerged
Code/test verification: refreshed at the final pre-publication branch tip; the
final commit SHA is recorded in the PR description and delivery report.

This refresh includes the typed Stage 1 reusable-summary source-manifest
validator and the canonical one-item-array representation for per-paper
`summary_file` reuse sources.

## Local acceptance

| Gate | Result |
|---|---|
| `python -m compileall -q .` | PASS |
| `python -m pyright` | PASS: 0 errors, 0 warnings, 0 informations |
| `python -m pytest --collect-only -q` | PASS: 784 tests collected; filtered gate selects 762 and deselects 22 |
| Strict offline gate | PASS: `762 passed, 22 deselected` in `1010.96s` (`16:50`) |
| Focused boundary regressions | PASS: PR14 remediation `12`, Stage1 closure/reuse `7`, critic/invalidation `8`, semantic pricing/token `24`, queue/lease `14`, runtime/export `3`, and stage/UI/controller `39` tests, plus supporting closure/bridge suites |
| Current production-shaped chain | PASS: three-PDF runner through validation, export, and attestation |
| Stage 1 reuse/closure | PASS: production-shaped all-reuse, mixed-reuse, summary-source zero-call, adversarial identity/hash/dependency matrix, and real Registry source-artifact provenance; zero-call paths do not create synthetic receipts |
| Atomic repair promotion | PASS: prepared transaction/hash/current-set/pointer fault boundaries |
| Provider receipt closure | PASS: binding, historical isolation, unexpected/out-of-scope/hash cases |
| Stability/replay | PASS: `off`/`smoke`/`full` contracts, coverage-critic failure/resume exact descendant sequence, candidate-specific invalidation, per-node call/token/cost plans, explicit pricing status, checkpoints, zero-transport replay |
| Optional validation/export | PASS: real runner/control-plane generated a valid `canonical_unvalidated` ZIP with typed disposition, stage/spec/current-set binding, policy fields, warning, and tamper fail-closed checks |
| Queue lease fencing/publication | PASS: lease-generation byte staging, queue-store -> Registry lock order, atomic target-plus-manifest commit, manifest/fsync/CAS failure variants, orphan evidence, heartbeat, and Windows `spawn` stale current-set race |
| GUI/controller boundary | CONTROLLER_VERIFIED: controller/status labels and queue transitions; Playwright not run |
| CurrentArtifactSet target typing | PASS: switch and resolve reject wrong validation target types/versions and mismatched conditional promotion evidence |
| Export failure cleanup | PASS: untrusted result and bundle cleanup |
| `python -m reviewctl doctor --config config.ini.example` | PASS: read-only `ok=true`, zero provider calls; warning only for existing stale locks |
| `git diff --check` | PASS before final staging; rerun after final staging |

## Scope and non-claims

- Deterministic provider injection is reported as `E2E_VERIFIED`, not live
  provider verification.
- Playwright, heavy OCR, and live API tests were not run.
- The exact strict offline aggregate completed with `762 passed, 22 deselected`
  in `1010.96s`; no live-provider, Playwright, or heavy-OCR result is included.
- No merge, main update, Ready-for-review transition, or auto-merge is part of
  this task.
- The ZIP files are absent from the committed diff and remote branch. The local
  operator reports that they were not read or staged; remote GitHub evidence
  cannot independently verify local read access.
- Final remote SHA, fresh CI result, and PR state are added to the PR
  description after the final push; older CI is not reused as evidence.

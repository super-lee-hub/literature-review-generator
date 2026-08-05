# PR #14 Final Verification

Verification date: 2026-08-05 (Asia/Shanghai)
Branch: `codex/platform-hardening-outline-v3`
Target: existing PR #14, kept Draft/Open/Unmerged
Code/test verification: refreshed at the final pre-publication branch tip; the
final commit SHA is recorded in the PR description and delivery report.

## Local acceptance

| Gate | Result |
|---|---|
| `python -m compileall -q .` | PASS |
| `python -m pyright` | PASS: 0 errors, 0 warnings, 0 informations |
| `python -m pytest --collect-only -q` | PASS: 774 tests collected; filtered gate selects 752 and deselects 22 |
| Strict offline gate | NOT_ACCEPTED: first exact run hit the 30-minute timeout (exit 124); second exited 1 at 9% without an aggregate summary; no aggregate pass count claimed |
| Focused boundary regressions | PASS: prior 54-test boundary set plus the new optional-policy file (`3 passed` in the current file) |
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
| `git diff --check` | PASS before commit; rerun after final staging |

## Scope and non-claims

- Deterministic provider injection is reported as `E2E_VERIFIED`, not live
  provider verification.
- Playwright, heavy OCR, and live API tests were not run.
- The exact strict offline aggregate was attempted twice with the required
  marker filter: first run timed out after 30 minutes; second run exited 1 at
  9% without an aggregate summary. The current-runtime test at the apparent
  boundary passes independently (`1 passed in 87.29s`); focused regressions and
  static gates remain independently verified.
- No merge, main update, Ready-for-review transition, or auto-merge is part of
  this task.
- The ZIP files are absent from the committed diff and remote branch. The local
  operator reports that they were not read or staged; remote GitHub evidence
  cannot independently verify local read access.
- Final remote SHA, fresh CI result, and PR state are added to the PR
  description after the final push; older CI is not reused as evidence.

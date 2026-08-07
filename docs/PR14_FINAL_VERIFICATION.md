# PR #14 Final Verification

Verification date: 2026-08-07 (Asia/Shanghai)
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
| `python -m pytest --collect-only -q` | PASS: 855 tests collected; filtered gate selects 833 and deselects 22 |
| Strict offline gate | PASS: `833 passed, 22 deselected` in `2644.77s` (`44:04`) |
| Focused boundary regressions | PASS: `147 passed in 693.64s` across Stage1 closure/reuse, Registry/lease publication, dependency lifecycle, validation parity, JobOutcome projection, zero-call, and architecture guards |
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
| `python -m reviewctl doctor --config config.ini.example` | PASS (exit 0): read-only `ok=true`, zero provider calls; `status=warn` only for existing stale locks; workspace-only checks skipped because no workspace was supplied |
| `git diff --check` | PASS on the final working tree and explicit staged diff |

## Scope and non-claims

- Deterministic provider injection is reported as `E2E_VERIFIED`, not live
  provider verification.
- Playwright, heavy OCR, live API, and multi-host tests were not run.
- The exact strict offline aggregate completed with `833 passed, 22 deselected`
  in `2644.77s`; no live-provider, Playwright, heavy-OCR, or multi-host result is
  included.
- No merge, main update, Ready-for-review transition, or auto-merge is part of
  this task.
- The ZIP files are absent from the committed diff and remote branch. The local
  operator reports that they were not read or staged; remote GitHub evidence
  cannot independently verify local read access.
- Final remote SHA, fresh CI result, and PR state are added to the PR
  description after the final push; older CI is not reused as evidence.
- `2644.77s` is local pytest time. The final GitHub Actions duration is recorded
  separately with its run/job IDs and is not inferred from local timing.

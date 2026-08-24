# PR #14 Final Verification

Verification date: 2026-08-08 (Asia/Shanghai)
Branch: `codex/platform-hardening-outline-v3`
Target: existing PR #14, kept Draft/Open/Unmerged
Code/test verification: refreshed at the final pre-publication branch tip; the
final commit SHA is recorded in the PR description and delivery report.

This refresh includes supported preprocess invalidation, path-independent
multimodal exact reuse, visual semantic invalidation, and Registry-detached
typed-manifest authority-blob failure coverage.

## Local acceptance

| Gate | Result |
|---|---|
| `python -m compileall -q .` | PASS |
| `python -m pyright` | PASS: 0 errors, 0 warnings, 0 informations |
| `python -m pytest --collect-only -q` | PASS: 865 tests collected; filtered gate selects 843 and deselects 22 |
| Strict offline gate | PASS: `843 passed, 22 deselected` in `3251.61s` (`54:11`) |
| Changed Stage 1 module | PASS: `22 passed in 185.84s` for supported preprocess settings, multimodal path portability/invalidation, and missing detached-authority blobs |
| Consolidated frozen-contract suite | PASS: `204 passed in 1844.75s` across Stage 1 trust boundaries, Registry/lease publication, JobOutcome projection, zero-call, Outline v3, and architecture guards |
| Validation policy/parity suite | PASS: `13 passed in 438.33s` across direct, CLI, GUI, queue, resume, required, findings, and optional-validation behavior |
| Current production-shaped chain | PASS: three-PDF runner through validation, export, and attestation |
| Stage 1 reuse/closure | PASS: production-shaped all-reuse, mixed-reuse, summary-source zero-call, supported preprocess invalidation, moved-path multimodal reuse, visual semantic invalidation, missing authority-blob failure, and adversarial identity/hash/dependency coverage; zero-call paths do not create synthetic receipts |
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
- The exact strict offline aggregate completed with `843 passed, 22 deselected`
  in `3251.61s`; no live-provider, Playwright, heavy-OCR, or multi-host result is
  included.
- No merge, main update, Ready-for-review transition, or auto-merge is part of
  this task.
> The ZIP files are absent from the committed diff and remote branch. The local operator reports that they were not read or staged; remote GitHub evidence cannot independently verify local read access.
- Final remote SHA, fresh CI result, and PR state are added to the PR
  description after the final push; older CI is not reused as evidence.
- `3251.61s` is local pytest time. The final GitHub Actions duration is recorded
  separately with its run/job IDs and is not inferred from local timing.

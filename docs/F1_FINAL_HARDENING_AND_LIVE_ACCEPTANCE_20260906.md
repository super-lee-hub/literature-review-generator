# F1 final hardening and live acceptance

Date: 2026-09-07 (Asia/Shanghai)

Decision: `NOT_READY_TO_MERGE`

This report records the exact evidence available in the isolated PR #24
worktree. No live Provider call was made and the PR was not merged.

## 1. Inspected base/head and PR state

- Repository: `super-lee-hub/literature-review-generator`
- PR: #24, `OPEN`, not draft, base `main`
- Inspected base/initial PR head: `2e6bdb32aca84c8d948adc846e3db76cd15b46b5`
- Final local and remote PR head: `0ff53dd4f11b409c486ff14a139c1c9483091089`
- Hosted Windows check observed before edits: `SUCCESS` (workflow run `34017515111`)
- Final hardening/report chain pushed to the existing PR branch through `0ff53dd4f11b409c486ff14a139c1c9483091089`.
- Post-push Hosted Windows run: `34082941338`, terminal `SUCCESS` on that exact SHA.
- Local branch `codex/f1-validation-authority-closure` is clean.

The original checkout at `D:\auto-generate` was an unborn local branch with a
415-file staged snapshot, so all work was performed in a separate worktree at
`D:\auto-generate\.worktrees\f1-hardening-20260906`.

## 2. Bugs found

Confirmed and addressed:

- `python-dotenv` process-environment precedence was implicit and could silently select a stale credential.
- Template credentials could pass configuration admission and reach transport paths.
- Provider admission was globally oriented instead of derived from the requested StagePlan.
- No canonical public provider preflight existed.
- Registry atomic replacement had no bounded Windows sharing-violation retry.
- Persistent queue corruption was converted to an empty queue.
- Queue and latest-pointer locks could wait indefinitely.
- `InProcessQueueService` could execute a job twice after an internal `TypeError`.
- Workspace/job identity and child filenames could escape the configured output root.
- Normal-stop semantic/schema failures escalated output budgets instead of using a bounded corrective retry.
- MinerU binary/ZIP handling lacked response, entry-count, uncompressed-size, entry-size, compression-ratio, and JSON limits.
- CI had no generated Python 3.11/Windows lock or `pip check` gate.
- There was no explicit opt-in formal live-acceptance entrypoint.

Remaining blockers are factual, not hidden: no current runtime credential,
no `config.ini`, no F1 runtime spec bound to the 15-paper corpus in this
worktree, no live Stage1/Validator run, and no downstream F1 artifacts.

## 3. Files changed

- `.github/workflows/windows-tests.yml`
- `ai_interface.py`
- `config_loader.py`
- `config_validator.py`
- `preprocess/service.py`
- `pytest.ini`
- `reviewctl.py`
- `runtime/control_plane.py`
- `runtime/orchestrator.py`
- `runtime/runner.py`
- `services/artifact_registry.py`
- `services/job_workspace.py`
- `services/queue_service.py`
- `services/stage1_analysis_service.py`
- `services/credential_provenance.py`
- `services/durable_io.py`
- `scripts/release_acceptance.py`
- `requirements-py311-windows.lock`
- `docs/en/runtime/credential-provenance.md`
- `tests/test_final_hardening_contracts.py`
- `tests/test_current_stage1_provider_fallback.py` (updated for the new same-budget semantic retry contract)

## 4. Credential provenance policy

The runtime now resolves credentials as:

`process environment > .env beside the selected config.ini > config.ini`.

Meaningful conflicting values fail closed. Template sentinels such as
`YOUR_*_API_KEY_HERE` and `loaded_from_.env_file` are allowed only for example
or explicit offline-test validation. A required production route rejects them
before transport. Diagnostics expose only source presence, selected source,
equality flags, provider family, model, endpoint, proxy policy, route, hashes,
and size/time limits. No raw credential or reusable credential hash is logged
or persisted.

`analyze` with `Stage1_Input.primary_reader_only=true` admits the Primary Reader
only; it does not require unused Backup Reader or Writer credentials.

## 5. Windows durability changes

`services/durable_io.py` centralizes fsynced temp-file replacement with bounded
retry for safe Windows sharing/permission errors and a typed timeout. Registry
publication preserves revision/CAS/publication-fence behavior and raises a
typed Registry error after the deadline. Queue and pointer writes use the same
bounded boundary.

Queue and pointer OS locks now use bounded acquisition and typed timeout errors.
The original queue bytes are never overwritten after corruption; a typed
`QueueCorruption` retains a quarantine copy and diagnostic metadata.

## 6. Queue, corruption, and path safety

- `QueueCorruption` fails closed instead of producing an empty canonical queue.
- Queue JSON structure is checked before normalization.
- In-process invocation determines `cancel_token` compatibility from the callable signature; internal `TypeError` is not retried.
- `JobWorkspace` rejects separators, absolute paths, reserved Windows names, dot components, invalid characters, and explicit resume identity mismatches.
- Artifact/checkpoint/report/log child paths are checked to remain descendants of their owning root.

## 7. Stage1 retry changes

Length/truncation retries may advance the output budget. Canonical schema and
semantic failures use a bounded corrective prompt at the same budget. Separate
length, schema, and semantic counters are carried in Stage1 projections and
retry/receipt diagnostics where the receipt is created. Retry ceilings remain
bounded by configuration and the ProviderRuntime budget.

## 8. Remote artifact resource limits

MinerU now bounds response bytes, ZIP entry count, total uncompressed bytes,
per-entry bytes, compression ratio, and structured/page-index JSON bytes. It
uses streamed binary reads when available, never `extractall`, and raises
typed format/limit errors for malformed or oversized archives.

## 9. Dependency reproducibility

- Human-readable direct requirements remain in `requirements.txt` and `requirements-dev.txt`.
- `requirements-py311-windows.lock` was generated with:

  `uv pip compile requirements-dev.txt --python-version 3.11 --python-platform windows --output-file requirements-py311-windows.lock --no-annotate`

- CI installs the generated lock and runs `python -m pip check`.
- The current local interpreter is Python 3.13.7, so the exact Python 3.11
  lock installation was not performed in this worktree.
- Current local `python -m pip check`: `No broken requirements found.`

## 10. Offline commands and results

Fresh full strict-offline run after the offline fixture admission fix:

`python -m pytest -q --strict-markers -m "not live_api and not playwright and not heavy_ocr and not live_acceptance"`

Result: `1404 passed, 23 deselected in 1411.03s (0:23:31)`.

Additional evidence:

- `python -m compileall -q .`: PASS.
- `python -m pyright`: PASS (`0 errors, 0 warnings`) on the elevated CI-shaped interpreter check before the final transport-diagnostic delta; a later sandbox-only rerun was `NOT VERIFIED` because the worktree has no `venv` and the sandbox interpreter could not resolve declared packages.
- Targeted hardening suite: `76 passed`.
- Latest template-transport/preflight delta checks: `2 passed` in the safe sandbox.
- Registry/queue transaction slice: `75 passed` plus the two repaired regressions (`2 passed`).
- `git diff --check`: exit code 0; only LF/CRLF normalization warnings.
- `python -m pip check`: PASS.
- `python -m reviewctl doctor --config config.ini.example`: `ok=true`, `status=warn`, `provider_network_calls=0`; warning is expected because no real credentials are present.
- `python -m reviewctl preflight --config config.ini.example --action analyze --stages analyze`: FAIL CLOSED on the template Primary Reader credential; `network_calls=0`.

## 11. Live acceptance gates attempted

### Gate A — offline regression

PASS for the full run above. The final small transport/preflight delta has
targeted compile and test evidence; its elevated full-suite recheck is not
available after the sandbox approval token was revoked.

### Gate B — one real F1 paper

`NOT RUN / BLOCKED`. No `config.ini`, no current rotated credential, and no
formal F1 spec/corpus binding are present in the worktree. The example config
preflight correctly fails before HTTP. Provider transport count: `0`.

### Gate C — three heterogeneous real papers

`NOT RUN / BLOCKED` because Gate B prerequisites are absent. Provider transport
count: `0`.

### Gate D — real crash/cancel/resume

`NOT RUN / BLOCKED` because no live three-paper run exists. Fixture/offline
resume tests are not substituted for this gate.

### Gate E — real Windows contention

Offline Windows transaction/lease tests and new corruption/exactly-once tests
passed. A dedicated live contention run against the final pushed branch was
not performed; classify the real-contention portion as `NOT VERIFIED`.

### Gate F — GUI

`NOT VERIFIED`. Playwright package import exists, but starting the Playwright
subprocess failed with `WinError 5` in the current sandbox. No GUI PASS claim
is made.

### Gate G — full F1 15-paper E2E

`NOT RUN / BLOCKED`. The authoritative live counts remain unearned:
`paper_artifact = 0/15` for this acceptance round; Stage1 Provider closure,
Outline, Review, Citation, DOCX, Validator, repair/revalidation, and final
QA were not executed.

## 12. Provider transport counts by stage

| Stage | Live transport | Evidence |
| --- | ---: | --- |
| Stage1/analyze | 0 | no credential/spec; preflight is no-network |
| Outline | 0 | not attempted |
| Review | 0 | not attempted |
| Validation | 0 | not attempted |
| MinerU remote | 0 | no token; no live run |

## 13. Artifact, ledger, closure, and validation status

- `paper_artifact`: `0/15` live acceptance evidence.
- Provider receipt ledger/closure: no live F1 ledger or closure.
- Current validation binding/fingerprint: no live F1 binding selected or verified.
- Formal Validator receipts: not run.
- Findings/repair/revalidation: not run.
- DOCX structural/render QA: not run.
- `canonical_ready=true`: not asserted.

## 14. GUI/OCR status

- GUI: `NOT VERIFIED` due Playwright process-start `WinError 5`.
- Heavy OCR: not enabled or run for a live corpus.

## 15. Security/secret hygiene

- Working-tree scan for long `sk-`, Bearer, and x-api-key-shaped values: 0 suspect files.
- History pickaxe scan for long `sk-` values: 0 suspect commits.
- `.env` is ignored by Git.
- Secret values were not emitted by the scan or report.

## 16. Remaining blockers and owner actions

1. Provide a currently valid rotated credential through the approved runtime
   secret mechanism; no pasted/exposed key was reused.
2. Provide or select the actual F1 runtime spec and authoritative 15-paper
   corpus in an acceptance workspace.
3. Re-run Gates B through G through the public `reviewctl` control plane with
   strict call/token budgets.
4. Keep PR #24 open until the live acceptance prerequisites are supplied and
   Gates B-G are completed; the PR remains unmerged.

## 17. Post-report verification delta

After the main offline run, the source was further tightened with symlink-aware
descendant checks, truthful sentinel-presence provenance flags, complete
non-secret transport-diff fields, retry-index receipt metadata, and spec-bound
acceptance preflight configuration. Targeted `compileall` passed.

A safe sandbox rerun of the focused tests reported `8 passed, 5 errors`; all
five errors were pytest setup failures creating Windows temporary `.lock`
directories. No assertion failure was reported in that rerun. The elevated
full-suite/type-check recheck and Git staging/commit/push remain unavailable
because the prior approval token's refresh token had been revoked at the time
of the initial report. Authorization was subsequently refreshed, the report
was committed, and the complete chain was pushed successfully.

## 18. Final decision

`NOT_READY_TO_MERGE`

The hardening code and offline regression evidence are materially improved,
but the required live Stage1 authority, three-paper crash/resume, real Windows
contention, GUI, Validator/repair, DOCX QA, and 15-paper F1 closure evidence
remain missing.

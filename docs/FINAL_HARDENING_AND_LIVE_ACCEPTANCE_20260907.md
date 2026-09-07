# Final hardening and live acceptance — 2026-09-07

## Release decision

```text
FINAL_STATUS = NOT_READY_TO_MERGE
PR = #24
PR_STATE = OPEN
PR_DRAFT = false
BASE_SHA = 4a5d56e83bf00a7eea529115798c772e0e1f15d6
FINAL_EXECUTABLE_SHA = 15e896d4d97a723f7081737dcd09f44d85c0e5fd
REMOTE_HEAD_AT_CODE_ACCEPTANCE = 15e896d4d97a723f7081737dcd09f44d85c0e5fd
HOSTED_CI_RUN = 34101339072
HOSTED_CI_CONCLUSION = SUCCESS
```

The executable acceptance claim is bound to `FINAL_EXECUTABLE_SHA`. The PR was
not merged. No live provider call, paid API call, Playwright acceptance run,
heavy-OCR acceptance run, or real F1 corpus run was made in this round.

## Git, PR, and Hosted CI read-back

| Item | Read-back |
|---|---|
| Branch | `codex/f1-validation-authority-closure` |
| PR | [#24](https://github.com/super-lee-hub/literature-review-generator/pull/24), OPEN, non-draft |
| Base | `main` at `4a5d56e83bf00a7eea529115798c772e0e1f15d6` |
| Remote head at final code push | `15e896d4d97a723f7081737dcd09f44d85c0e5fd` |
| Hosted run | [34101339072](https://github.com/super-lee-hub/literature-review-generator/actions/runs/34101339072) |
| Hosted head SHA | `15e896d4d97a723f7081737dcd09f44d85c0e5fd` |
| Hosted result | `SUCCESS` |

Hosted run steps all succeeded: Python 3.11 setup, installation from
`requirements-py311-windows.lock`, `pip check`, compile check, collection,
public CLI smoke, strict-offline suite, Pyright, Doctor, and committed-range
whitespace check. Collection reported `1451 tests collected`; the strict-
offline step reported:

```text
1428 passed, 23 deselected in 1407.11s
Pyright: 0 errors, 0 warnings, 0 informations
```

The 23 deselected tests are the repository's explicitly live/provider,
Playwright, and heavy-OCR surfaces. No deselection was used to hide an
offline assertion failure.

## Bugs fixed in this round

- Replaced the release acceptance false-PASS path. A completed `reviewctl` job
  can no longer mark crash/resume, GUI, OCR, heterogeneity, Validator, or
  15-paper gates as PASS. Every specialized gate has a purpose, prerequisites,
  actual action, live/offline requirement, required facts, and an independent
  evidence validator bound to the tested SHA.
- Added typed `ReleaseAcceptanceSpec` and `ReleaseAcceptanceBudget` with hard
  aggregate limits for provider calls, output tokens, retries, and wall time.
  `ProviderRuntime` reserves possible transport attempts and requested output
  before transport, shares the controller across routes, persists usage state
  across a process boundary, and records usage snapshots in durable receipts.
- Added `reviewctl micro-probe` as a distinct real route probe. It uses the
  normal config loader, StagePlan route admission, endpoint/proxy construction,
  `ai_interface._call_ai_api_detailed`, `ProviderRuntime`, and a receipt ledger.
  It is never implied by the zero-network dry preflight and requires explicit
  third-party/custom-host acknowledgement.
- Made `RuntimeJobSpec.from_dict`, its source object, metadata, and acceptance
  budget schema reject unknown keys and invalid types instead of silently
  defaulting them.
- Completed StagePlan-aware config admission. Analyze-only primary-reader
  jobs no longer require physically present Backup, Writer, Outline, or
  Validator sections unless those stages are reachable; reachable missing
  routes still fail closed.
- Replaced preprocess freshness based on PDF size/mtime with canonical source
  path plus PDF SHA-256, processing fingerprint, per-artifact hashes, and an
  fsynced active-generation pointer. New generations are staged completely
  before publication; missing/tampered/incompatible generations rebuild or
  fail closed and are never mixed with an old manifest.
- Added strict source-bound evidence loading. Required text, chunks, page
  index, and manifests are read as immutable bytes, decoded/parsed from those
  bytes, and checked against typed artifact hashes. Missing, locked, truncated,
  invalid-UTF-8, invalid-JSON, and hash-mismatched evidence raises
  `ValidationSourceAuthorityError` before formal Validator transport.
- Hardened Free Mode profile names and output roots against traversal, reserved
  Windows names, separators, reparse ancestors, and reparse profile leaves;
  profile writes now use the durable atomic JSON writer.
- Hardened `JobWorkspace` production artifact paths against existing symlink,
  junction, and other reparse leaves while retaining a separate inspection-only
  helper and review-batch domain-level fail-closed errors.
- Hardened Zotero managed `storage:`/`attachments:` paths with key validation,
  realpath containment, reparse checks, and explicit linked-file provenance.
  Legitimate external linked files remain allowed and are marked as such;
  managed-storage escapes are rejected.
- Added MinerU source-PDF size admission and streaming upload. Upload bytes are
  tracked; retries do not first create an unbounded whole-PDF bytes copy.
- Preserved the earlier queue corruption quarantine, exactly-once callable
  invocation, bounded Windows lock/replace retry, credential provenance,
  Registry snapshot closure, Stage1 retry taxonomy, and remote ZIP/response
  resource limits.

## StagePlan admission matrix

| Action / policy | Reachable provider sections | Missing section result |
|---|---|---|
| `analyze`, `primary_reader_only=true` | `Primary_Reader_API` (plus `Validator_API` only if Stage1 validation is enabled) | Primary missing: fail closed; Backup/Writer/Outline absence is allowed |
| `analyze`, `primary_reader_only=false` | Primary + Backup (and optional Stage1 Validator) | Missing Backup: fail closed |
| `generate_outline` | Outline route plus every section named by reachable `OutlineModels` roles | Missing route or role mapping: fail closed |
| `generate_review` / `run_all` | Outline + Writer; `run_all` also reaches Validator when review validation is enabled | Missing reachable route: fail closed |
| `validate_review` | Validator | Missing Validator: fail closed |
| Free Mode | `Free_Mode_API` in addition to the stages explicitly reached | Incomplete Free Mode route: zero-call fail closed |

The physically minimal analyze regression has only `Application`, `Paths`,
`Primary_Reader_API`, and `Stage1_Input`; it contains no Backup, Writer,
Outline API, Outline model, or Outline cost-control sections and passes config
load plus analyze admission when the primary-only policy is enabled.

## Credential and trust policy

Credential resolution is deterministic:

```text
process environment > .env beside the selected config > config.ini
```

Conflicting meaningful values fail closed. Template credentials are rejected
for production routes. Diagnostics expose source presence and route identity,
never credential values, hashes, or authorization headers.

The current shipped example routes classify as follows. This is configuration
read-back, not live connectivity evidence.

| Semantic role | Section/model | Endpoint host | Trust classification |
|---|---|---|---|
| Stage 1 primary reader | `Primary_Reader_API` / `deepseek-v4-flash-vision-exp` | `api.deepseek.com` | official provider host |
| Stage 1 backup reader | `Backup_Reader_API` / `deepseek-v4-flash` | `api.deepseek.com` | official provider host |
| Outline candidate/arbitrator | `Outline_API` / `claude-opus-5` | `chat.178266.xyz` | third-party gateway |
| Outline relation/coverage critique | `Free_Mode_API` / `deepseek-v4-pro` | `api.deepseek.com` | official provider host |
| Outline structure/evidence critique and Writer | `Writer_API` / `gpt-5.6-sol` | `ai.saigou.work` | third-party gateway |
| Validator | `Validator_API` / `deepseek-v4-flash` | `api.deepseek.com` | official provider host |

Third-party and custom hosts require explicit acknowledgement and exact-host
binding before content is sent. Protocol compatibility does not make a gateway
an official OpenAI or Anthropic endpoint.

## Acceptance budget and ledger

The typed budget fields are:

```text
max_provider_calls_total
max_output_tokens_total
max_retry_attempts_total
max_wall_seconds
```

Admission reserves `1 + possible retries` physical provider calls and the
requested output allowance before transport. Completion reconciles actual
reported usage conservatively; unreported usage retains its reservation rather
than becoming an artificial zero. Durable receipt metadata contains the
aggregate budget snapshot, while `ProviderRuntimeLedger.usage_summary()`
recomputes physical calls, reported/unreported output, retries, and stage/
provider breakdowns from receipts.

No acceptance budget was exercised against a real provider in this round.
Therefore actual live calls, output tokens, and retries are:

```text
provider calls = 0
output tokens = 0 reported
retries = 0
```

These are evidence counts, not a claim that a future live run will consume
zero.

## Preprocess, validation, path, and MinerU evidence

- Cache manifests bind `canonical_source_path`, `source_pdf_sha256`, source
  byte size, implementation/schema/selector versions, parser/OCR/MinerU
  configuration, and a `processing_fingerprint`.
- Required cache artifacts carry relative identity, type, schema version, size,
  and SHA-256. Publication is through a complete staged generation and an
  atomic `active_generation.json` pointer.
- Formal Validation uses strict evidence mode when source-bound manifests are
  present. The existing compatibility mode remains available to isolated
  legacy unit callers without authority paths.
- `source_pdf_max_bytes` is part of `[Preprocess]` and the shipped example;
  MinerU upload uses a file handle, not `handle.read()` into a whole-PDF bytes
  object.
- Managed Zotero storage is contained under the attachment key's real storage
  root. External linked files carry `attachment_source_type=linked_file`,
  `external_to_library`, canonical resolved path, link mode, and raw-path
  provenance.

## Verification evidence on the final code SHA

### Local exact-SHA checks

The local worktree was clean at `15e896d...` before this report was authored.

| Check | Result | Scope |
|---|---|---|
| Focused hardening/provider/config/cache suite | `25 passed, 1 skipped` | Local Python 3.13 environment; the one skip is optional symlink privilege |
| Changed-file focused Pyright | `0 errors, 0 warnings, 0 informations` | Local changed runtime and integration files |
| `compileall` | PASS | Current runtime/services/preprocess/validation/outline/free-mode/scripts surface |
| `pip check` | PASS | Local interpreter |
| `git diff --check` | PASS | Final code commit |
| Full local strict-offline suite | `1415 passed, 12 failed, 1 skipped, 23 deselected` | The 12 failures are setup-time named-pipe `PermissionError: [WinError 5]`, not assertions |

The local full-suite command was:

```text
python -m pytest -q --strict-markers -p no:cacheprovider \
  -m "not live_api and not playwright and not heavy_ocr and not live_acceptance"
```

The 12 local failures occurred while creating Windows multiprocessing named
pipes in queue, Registry, review-batch, lifecycle, and validation checkpoint
tests. The same selected cross-process surface passed in Hosted Windows CI;
the local result is retained as an environment limitation, not converted into
a local PASS.

### Hosted exact-SHA checks

Hosted Windows run `34101339072` passed all steps on `15e896d...`, including
the full selected suite (`1428 passed, 23 deselected`), Python 3.11 lock
installation and `pip check`, compile, CLI smoke, Pyright with zero diagnostics,
Doctor, and whitespace verification.

## Gate results

| Gate | Status | Exact evidence / boundary |
|---|---|---|
| A — exact-final-SHA offline closure | `PASS` (Hosted) / `NOT_VERIFIED_LOCAL` | Hosted run `34101339072` passed on `15e896d...`; local has 12 named-pipe permission failures and one optional symlink skip |
| B — dry transport preflight | `BLOCKED_CREDENTIALS` / `BLOCKED_INPUT` | Example config rejects template Primary credential before HTTP; active checkout has no production `config.ini` or `.env`; `network_calls=0` |
| B-live — route micro-probe | `BLOCKED_CREDENTIALS` | `reviewctl micro-probe` is implemented and explicit, but no approved credential exists; no call was made |
| C — one real F1 paper | `BLOCKED_F1_SPEC` / `BLOCKED_CREDENTIAL` | No authoritative F1 spec/corpus or approved credential in the scoped checkout; no production run |
| D — three heterogeneous real papers | `BLOCKED_PREREQUISITE` | Gate C is blocked; no synthetic PDFs were substituted |
| E — real process crash/cancel/resume | `NOT_VERIFIED` | No real three-paper provider run and no process-boundary interruption evidence |
| F — real multi-provider Outline v3 | `BLOCKED_CREDENTIALS` | Role mapping is present in the example config, but no real gateway transport or receipts |
| G — real Free Mode | `BLOCKED_CREDENTIALS` | No Free Mode credential or live route call; incomplete-route zero-call tests pass offline |
| H — Validator defect injection/repair/revalidation | `BLOCKED_CREDENTIALS` | No real Validator transport or real-review baseline; controlled offline contracts are not substituted |
| I — GUI Playwright | `NOT_VERIFIED` | No real browser flow/evidence artifact in this round |
| J — heavy OCR | `BLOCKED_CORPUS` | No approved scanned/OCR-poor acceptance PDF in the scoped checkout |
| K — real Windows contention | `PASS_OFFLINE_HOSTED`, `NOT_VERIFIED_AS_LIVE_ACCEPTANCE` | Hosted exact Windows suite passed the selected cross-process queue/Registry/review-batch/lifecycle checks; no separate release acceptance manifest was produced |
| Q — F1 15-paper full chain | `BLOCKED_F1_SPEC` / `BLOCKED_F1_CORPUS` | No authoritative 15-paper binding; canonical Stage 1 artifacts `0/15`, downstream Outline/Review/Citation/DOCX/Validation not run |
| R — negative production behavior | `PASS_OFFLINE`, `NOT_VERIFIED_RELEASE_MANIFEST` | Typed config/auth/queue/path/cache/evidence/budget and false-PASS regressions are in the selected suite; no separate gate evidence manifest |
| S — secret/privacy scan | `NOT_VERIFIED` | No production secret was used or printed; a full release-scope history/privacy evidence manifest was not generated |
| T — branch governance | `BLOCKED` / `OWNER_ACTION_REQUIRED` | `GET /branches/main/protection` returned HTTP 404 (`Branch not protected`); required checks/protection are not configured |

No gate is marked PASS from an unrelated completed job, fixture transport,
sentinel response, recorded response, or zero-call dry preflight.

## Real-provider and F1 artifact counts

```text
REAL_PRIMARY_READER_CALLS = 0
REAL_BACKUP_READER_CALLS = 0
REAL_OUTLINE_CALLS = 0
REAL_WRITER_CALLS = 0
REAL_FREE_MODE_CALLS = 0
REAL_VALIDATOR_CALLS = 0
REAL_MINERU_REMOTE_CALLS = 0
REAL_F1_SOURCE_COUNT = 0/15 (not bound)
REAL_STAGE1_CANONICAL_COUNT = 0/15 (not run)
REAL_OUTLINE = not run
REAL_REVIEW = not run
REAL_CITATION_MANIFEST = not run
REAL_DOCX = not run
REAL_VALIDATION = not run
REAL_REPAIR_REVALIDATION = not run
FINAL_CANONICAL_CLOSURE = not asserted
```

The selected offline suite does exercise mocked/injected contract boundaries,
but those receipts and callbacks are not counted as live provider evidence.

## Files changed in this hardening round

Production/runtime changes include:

```text
ai_interface.py
config.ini.example
config_loader.py
config_validator.py
free_mode/profile_manager.py
free_mode/service.py
outline/v3_executor.py
preprocess/service.py
reviewctl.py
runtime/control_plane.py
runtime/job_spec.py
runtime/orchestrator.py
runtime/provider_runtime.py
runtime/release_acceptance.py
runtime/runner.py
runtime/source_intake.py
runtime/zotero_attachment_resolver.py
scripts/release_acceptance.py
services/configuration_service.py
services/job_workspace.py
services/review_batch.py
services/review_generation_service.py
services/settings.py
services/stage1_analysis_service.py
setup_wizard.py
validation/current_validation.py
validation/evidence_loader.py
validation/llm_adjudicator.py
validation/review_validator.py
```

Regression coverage includes `tests/test_release_hardening.py` and the
preprocess cache behavior update in `tests/test_preprocess_service.py`.

## Remaining owner actions

1. Supply an approved rotated credential through the supported `.env` or
   process-environment mechanism, without placing it in tracked files.
2. Supply/select the authoritative F1 runtime spec and bound 15-paper corpus in
   the scoped acceptance workspace; do not substitute another corpus.
3. Run the explicit live route micro-probes with exact third-party host
   acknowledgement and the typed aggregate budget.
4. Execute Gates C-K and Q through the public control plane, capturing
   final-SHA-bound gate evidence, durable receipts/ledger deltas, Registry
   closure, DOCX QA, Validator defect detection, repair, and revalidation.
5. Configure `main` branch protection and required Windows checks as a
   repository-owner action; current API read-back proves it is not configured.

**FINAL_STATUS: NOT_READY_TO_MERGE**

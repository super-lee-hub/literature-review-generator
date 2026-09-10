# Final hardening and live acceptance — 2026-09-10

## Release decision

```text
FINAL_STATUS = NOT_READY_TO_MERGE
PR = #24
PR_STATE = OPEN
PR_DRAFT = false
BASE_SHA = 4a5d56e83bf00a7eea529115798c772e0e1f15d6
PRODUCTION_RUNTIME_SHA = 9ceef08e9cb62aac8b13f0199df9c98ea975eacd
FINAL_EXECUTABLE_SHA = 9ceef08e9cb62aac8b13f0199df9c98ea975eacd
PR_HEAD_SHA_AT_CODE_ACCEPTANCE = LOCAL_ONLY_PUSH_BLOCKED
REPORT_ONLY_SHA = recorded locally after this report-only commit; not pushed
HOSTED_CI_RUN = NOT_RUN_FOR_FINAL_EXECUTABLE_SHA
HOSTED_CI_CONCLUSION = NOT_RUN
CODE_HARDENING_STATUS = PASS
OFFLINE_HOSTED_STATUS = LOCAL_PASS_HOSTED_PENDING
LIVE_ACCEPTANCE_STATUS = BLOCKED_OWNER_INPUTS
PARENT_ACCEPTANCE_RUN_ID = NOT_RUN_BLOCKED_OWNER_INPUTS
CHILD_SCENARIO_IDS = C,D,E,F,G,H,I,J,K,Q (implemented; not live-executed)
```

The executable acceptance claim is bound to `FINAL_EXECUTABLE_SHA`. The PR was
not merged. This executable commit is currently local because SSH transport was
denied and the HTTPS push requires an external authorization that was rejected.
No live provider call, paid API call, Playwright acceptance run,
heavy-OCR acceptance run, parent acceptance plan, or real F1 corpus run was
made in this round. The report-only commit SHA is recorded in the PR body after
push because an immutable Git object cannot truthfully include its own SHA.

## Pasted-audit follow-up

This follow-up closes the remaining false-PASS and provenance gaps identified
in the pasted audit. The executable code/test SHA is
`53b18f51b5fee58e23ed3014383a6660baa2471d`; any later report commit is
documentation-only and is not substituted for that SHA.

- Windows provider liveness now uses non-destructive process inspection and
  persists PID creation identity plus host identity; it never uses
  `os.kill(pid, 0)` on Windows.
- `AcceptanceExecutionContextV1` binds the acceptance run ID, executable SHA,
  absolute deadline, aggregate budget, budget state, evidence root, process
  event log, and scenario state. State binding reconnects orphan reservation
  reconciliation before resume.
- Each specialized acceptance scenario now reopens and hash-checks its durable
  inputs. A blocked runtime or role-only inventory cannot become READY, and the
  verifier rejects evidence belonging to a different acceptance run before
  semantic facts are considered.
- Gate D derives modality profiles from source artifacts; Gate E requires typed
  interruption/resume lineage and ledger deltas; Gate F compares every enabled
  semantic role to the authoritative route plan; Gate H requires a controlled
  defect challenge and repair/revalidation; Gate I requires typed localhost
  Playwright evidence; Gate J requires typed OCR lineage; Gate K runs two real
  independent Windows processes; Gate Q counts distinct paper identities.
- Preprocess cache keys are interprocess-locked. Formal Stage 1 now acquires a
  typed, expiring generation lease only after freshness and artifact hashes
  pass; the lease survives GC until the job-owned snapshot and Registry-backed
  EvidenceManifest are durable, then is released in both success and exception
  paths. GC rejects reparse paths and removes expired or malformed leases;
  legacy `pin_id` callers remain compatible.
- Doctor stale-lock reporting probes lock contention instead of mtime alone.
  Local RAG identity changes create immutable collections, and the cache now
  retains the current identity plus a configurable number of recent identities
  per collection family. Only hash-valid sidecars can authorize deletion, the
  current collection is always protected, and backend deletion failure retains
  the sidecar for a later retry. Legacy manual diagnostics are outside pytest
  and network-disabled by default; the public CLI smoke covers both
  `micro-probe` and `acceptance-run` help.
- Parent-plan child execution now binds every runtime child to the declared
  `RuntimeJobSpec` job/workspace identity, rejects shared child workspaces or
  explicit job IDs, and records a non-PASSED receipt when the final scenario
  action fails after preliminary evidence collection.
- Gate D production modality references are carried into the child evidence
  manifest after Registry publication. Gate E proves the terminated child is
  dead by PID creation identity. Gate I registers trace, browser metadata,
  screenshot manifest, and screenshot artifacts in the resulting job Registry;
  the screenshot manifest is required and bound to the browser run.
- Parent aggregation now compares every child receipt against the parent plan,
  runtime-spec hash, input identity, job identity, and budget domain, and each
  child persists its own state path. A PASS projection with a non-PASSED
  execution receipt is rejected.

## Second hardening round

This code revision adds the following fail-closed boundaries:

- runtime.provider_routes.ReachableProviderRoutePlan is now the shared
  StagePlan-to-semantic-role projection. Configuration admission, doctor,
  preflight, micro-probe, Outline v3, acceptance state, and route reporting
  expose the same enabled OutlineModels routes; disabled critique roles are
  not admitted or silently remapped.
- GateEvidenceProducer, DurableEvidenceRefV1, and GateEvidenceVerifier replace
  handwritten acceptance facts with durable path/identity/size/SHA references.
  Evidence indexes now also require the exact producer and gate binding and
  reject unknown top-level fact fields. The verifier reopens the runtime spec, Registry and ready artifact hashes,
  stage/attempt/outcome/closure artifacts, provider ledgers, and typed role
  artifacts before deriving gate facts; test-only or malformed provider
  receipts cannot satisfy a live gate.
- reviewctl acceptance-run --acceptance-spec <path> now persists a typed
  resumable state and executes/resumes a supplied runtime spec only after the
  explicit owner authorization flag. Missing credentials, corpus, or durable
  evidence remains blocked.
- Aggregate provider budgets persist an absolute wall-clock deadline,
  process-owned reservations, transport-start markers, and cross-process
  locked read/modify/write transactions. ProviderRuntimeLedger uses the same
  OS-level lock and retains duplicate-ID conflict detection across processes.
- Strict evidence reads are bounded and stable, require an exact manifest
  identity plus expected lowercase SHA-256, and reject missing/ambiguous
  identities; basename-only fallback is gone.
- Preprocess generations are atomically renamed from .generation.tmp-* to
  finalized generation-* directories, retain only safe non-active history,
  clean stale staging directories, and bind source identity to a stable stat
  tuple plus content hash. The reachable legacy pdf_extractor fallback was
  removed; its standalone compatibility tests cover partial-parser duplication.
- Local RAG identity binds source/fingerprint/chunk-schema/embedding model,
  embedding-model download is opt-in, and immutable identity retention is
  bounded by `local_rag_retain_recent_identities` (default `2`, allowed
  `0..1000`). Flat runtime-spec mappings reject unknown fields, and action-bound
  config validation no longer falls back to a legacy one-argument validator.
- Added docs/implementation/PRODUCTION_REACHABILITY_INVENTORY_20260907.md.

The executable acceptance claim remains bound to `FINAL_EXECUTABLE_SHA`. Hosted
run `34381147976` completed successfully on that exact SHA. Any later
docs-only report commit is not used as executable validation evidence.

## Git, PR, and Hosted CI read-back

| Item | Read-back |
|---|---|
| Branch | `codex/f1-validation-authority-closure` |
| PR | [#24](https://github.com/super-lee-hub/literature-review-generator/pull/24), OPEN, non-draft |
| Base | `main` at `4a5d56e83bf00a7eea529115798c772e0e1f15d6` |
| Remote head at exact code/CI acceptance | `53b18f51b5fee58e23ed3014383a6660baa2471d` |
| Hosted run | [34381147976](https://github.com/super-lee-hub/literature-review-generator/actions/runs/34381147976) |
| Hosted head SHA | `53b18f51b5fee58e23ed3014383a6660baa2471d` |
| Hosted result | `SUCCESS` |

The Hosted evidence is bound to the exact final executable SHA. The report
commit itself is a docs-only descendant and is not substituted for that SHA.

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

The current executable acceptance SHA is
`53b18f51b5fee58e23ed3014383a6660baa2471d`. The report-only update is a child
commit and does not change the executable source under test.

| Check | Result | Scope |
|---|---|---|
| Typed generation-lease and Stage 1 lifecycle regressions | `8 passed` | Final local bytes; covers lease retention/release, expiry cleanup, stale-generation rejection, reparse rejection, and success/exception release |
| Local RAG retention focused regressions | `9 passed` | Final local bytes; covers bounded retention, current protection, deletion retry, config forwarding, and strict invalid-value rejection |
| Preprocess/release/config/setup adjacency suite | `131 passed, 1 skipped` | Final local bytes; optional Windows symlink privilege is the only skip |
| Test collection | `1527 collected` | Final local bytes; collection only, not a local full-suite execution claim |
| Full Pyright | `0 errors, 0 warnings, 0 informations` | Final local bytes across the repository |
| `compileall` | PASS | Current runtime/services/preprocess/validation/outline/free-mode/scripts surface |
| `pip check` | PASS | Local interpreter |
| `git diff --check` | PASS | Final code commit |
| Full local strict-offline suite | `NOT RUN_TO_COMPLETION_THIS_ROUND` | Hosted exact-SHA execution is required for the full 1,400+ test surface |

The local full-suite command was:

```text
python -m pytest -q --strict-markers -p no:cacheprovider \
  -m "not live_api and not playwright and not heavy_ocr and not live_acceptance"
```

No complete local full-suite run was completed in this round. Earlier local
attempts were blocked by Windows temporary-directory and multiprocessing
permission errors; no local full-suite PASS is claimed. The exact selected
surface is therefore closed by the Hosted run below.

### Hosted exact-SHA checks

Hosted run `34381147976` passed all five Windows matrix jobs on
`53b18f51b5fee58e23ed3014383a6660baa2471d`. Each job passed installation,
compile, collection, public CLI smoke, strict-offline tests, Pyright, Doctor,
and committed-range whitespace checks. The exact job read-back was:

```text
test (1) job 102566030002: SUCCESS
test (2) job 102566030122: SUCCESS
test (3) job 102566029664: SUCCESS
test (4) job 102566029990: SUCCESS
test (5) job 102566030794: SUCCESS
all five matrix jobs returned zero workflow exit status
```

The three base shards used deterministic file isolation; the release-hardening
file used node isolation with redirected child stdout/stderr and explicit
process exit-code checks. The matrix is offline contract evidence only: it
does not constitute live provider, F1-corpus, GUI-browser, or heavy-OCR
acceptance evidence.

## Gate results

| Gate | Status | Exact evidence / boundary |
|---|---|---|
| A — exact-final-SHA offline closure | `PASS_HOSTED_MATRIX` / `PASS_LOCAL_FOCUSED` | Hosted run `34381147976` passed on exact SHA `53b18f51b5fee58e23ed3014383a6660baa2471d`; focused local lease/retention tests and static checks passed |
| B — dry transport preflight | `BLOCKED_CREDENTIALS` / `BLOCKED_INPUT` | Example config rejects template Primary credential before HTTP; active checkout has no production `config.ini` or `.env`; `network_calls=0` |
| B-live — route micro-probe | `BLOCKED_CREDENTIALS` | `reviewctl micro-probe` is implemented and explicit, but no approved credential exists; no call was made |
| C — one real F1 paper | `BLOCKED_F1_SPEC` / `BLOCKED_CREDENTIAL` | No authoritative F1 spec/corpus or approved credential in the scoped checkout; no production run |
| D — three heterogeneous real papers | `BLOCKED_PREREQUISITE` | Gate C is blocked; no synthetic PDFs were substituted |
| E — real process crash/cancel/resume | `NOT_VERIFIED` | No real three-paper provider run and no process-boundary interruption evidence |
| F — real multi-provider Outline v3 | `BLOCKED_CREDENTIALS` | Role mapping is present in the example config, but no real gateway transport or receipts |
| G — real Free Mode | `BLOCKED_CREDENTIALS` | No Free Mode credential or live route call; incomplete-route zero-call tests pass offline |
| H — Validator defect injection/repair/revalidation | `BLOCKED_CREDENTIALS` | No real Validator transport or real-review baseline; controlled offline contracts are not substituted |
| I — GUI Playwright | `NOT_VERIFIED` | Executor now requires Registry-backed trace/browser/screenshot artifacts, but no real browser flow was run this round |
| J — heavy OCR | `BLOCKED_CORPUS` | No approved scanned/OCR-poor acceptance PDF in the scoped checkout |
| K — real Windows contention | `PASS_OFFLINE_HOSTED`, `NOT_VERIFIED_AS_LIVE_ACCEPTANCE` | Exact-SHA Hosted matrix passed the independent-process queue/Registry/budget/lifecycle checks, including the Gate K scenario test; no separate live release-acceptance manifest was produced |
| Q — F1 15-paper full chain | `BLOCKED_F1_SPEC` / `BLOCKED_F1_CORPUS` | No authoritative 15-paper binding; canonical Stage 1 artifacts `0/15`, downstream Outline/Review/Citation/DOCX/Validation not run |
| R — negative production behavior | `PASS_OFFLINE_HOSTED`, `NOT_VERIFIED_RELEASE_MANIFEST` | Typed config/auth/queue/path/cache/evidence/budget and false-PASS regressions passed in the exact-SHA selected suite; no separate release manifest |
| S — secret/privacy scan | `NOT_VERIFIED` | No production secret was used or printed; a full release-scope history/privacy evidence manifest was not generated |
| T — branch governance | `BLOCKED` / `OWNER_ACTION_REQUIRED` | `GET /branches/main/protection` returned HTTP 404 (`Branch not protected`); required checks/protection are not configured |

No gate is marked PASS from an unrelated completed job, fixture transport,
sentinel response, recorded response, role-only inventory, cross-run manifest,
or zero-call dry preflight.

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
runtime/playwright_evidence.py
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

Regression coverage includes `tests/test_acceptance_execution_scenarios.py`,
`tests/test_playwright_evidence.py`, `tests/test_release_hardening.py`, and the
preprocess cache behavior update in `tests/test_preprocess_service.py`.

The new executable/report scope also includes runtime/provider_routes.py,
runtime/architecture_gates.py, services/durable_io.py, rag/local_rag.py, and
docs/implementation/PRODUCTION_REACHABILITY_INVENTORY_20260907.md.

## Continuation evidence — 2026-09-10

The executable hardening commit is
`9ceef08e9cb62aac8b13f0199df9c98ea975eacd`.

Implemented and locally verified in this continuation:

- APA7/CSL-like citation style boundary with creator normalization, author
  count rules, narrative/parenthetical modes, same-author/year suffixes,
  style ordering, DOI normalization, CJK and organization support, and richer
  journal metadata.
- DOCX reference runs with real italic formatting, hanging indents and
  spacing; scanner rejection of legacy/unresolved tokens, leaked `R###` IDs,
  uncited bibliography entries, and malformed manifest occurrences.
- Local RAG sidecar identity recomputation, filename derivation checks,
  Chroma metadata cross-checks, reparse rejection, and deletion fail-closed
  behavior.
- Bounded MinerU JSON handling is present in the executable baseline, with
  streaming reads and limit tests.
- Stage1 lease cleanup preserves a primary exception and records a durable
  integrity-blocked cleanup failure; Stage1 graph predeclaration now retains
  lightweight declarations and JIT-materializes heavy per-paper inputs while
  checking stable semantic binding identity.

Fresh local evidence for this continuation:

| Check | Result |
|---|---|
| Full collection | `1549 collected` |
| Strict-offline executed suite | `1524 passed, 2 skipped, 23 deselected` |
| Same-process Stage1/core suite | `89 passed, 1 skipped` |
| Citation/DOCX targeted suite | `43 passed` |
| Local RAG targeted suite | `12 passed, 1 skipped` |
| MinerU/preprocess targeted suite | `42 passed` |
| Lease cleanup targeted suite | `4 passed` |
| Pyright | `0 errors, 0 warnings, 0 informations` |
| Compileall | passed |
| `pip check` | `No broken requirements found` |
| Fatal Ruff (`E9,F821,F841`) | passed |
| Strict `pip-audit` on hashed production lock | `No known vulnerabilities found` |

Release installation now uses separate hashed Windows/Python 3.11 production
and development locks. Optional Chroma local RAG is isolated in
`requirements-optional-rag.txt` because the current audit feed reports
unresolved advisories for the available Chroma line; it remains disabled by
default and is not silently suppressed in release audit.

The new executable SHA has not yet received Hosted CI because external push
authorization was rejected in this environment. Hosted run `34381147976`
remains valid only for the older executable SHA
`53b18f51b5fee58e23ed3014383a6660baa2471d`; it is not reused as evidence for
`9ceef08e9cb62aac8b13f0199df9c98ea975eacd`.

## Remaining engineering follow-up

The release locks now carry package hashes and the production lock passes the
strict vulnerability audit. The remaining release-engineering gap is Hosted
CI on the new executable SHA, followed by the existing owner-gated live
acceptance and branch-protection checks.

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

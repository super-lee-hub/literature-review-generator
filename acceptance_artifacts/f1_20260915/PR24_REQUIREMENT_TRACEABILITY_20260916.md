# PR24 final-fix requirement traceability

C/D/Q live acceptance executable freeze: `09477d666a0f31c2b1d95a2a6cb25c00b4e7bfca`.
Gate I live acceptance executable freeze: `24884c2adaeac2d98af85349dbbccc6980d2c814` on `codex/f1-validation-authority-closure`.
Gate K offline acceptance executable freeze: `96e6f829e4fc0b99d636190b742129e74226c8b0`.
Gate E current local controlled-probe executable freeze: `a2c934081e93834338249f44e17d4e9af89b2ddc`.

The code result below is `PASS_OFFLINE` only where the current repository checks prove it. Gate I is separately recorded as a scoped live acceptance. This is not a claim that live Provider, MinerU, OCR, or semantic full-F1 acceptance passed. The separate [F1 acceptance report](F1_LIVE_ACCEPTANCE_REPORT_20260916.md) records those boundaries.

| ID | Current judgment | Implementation / authoritative boundary | Fresh evidence | Remaining boundary |
|---|---|---|---|---|
| A01 | PASS_OFFLINE | `runtime/trust_admission.py` binds normalized scheme/host/port/path, provider/model/endpoint, proxy mode, parser policy, and v2 ACK timestamps/schema. `runtime/release_acceptance.py` consumes the same ACK schema. | `tests/test_pr24_final_fixes.py::test_a01_fingerprint_binds_full_transport_endpoint_and_ack_expiry`; trust-admission suite; Pyright/Ruff/compile; current Q child admission. | No live custom-host content was sent because D remained an unmet prerequisite; the historical ACK is now expired and must be refreshed before Q. |
| A02 | PASS_OFFLINE | `services/mineru_policy.py` is the shared effective host policy used by admission and `preprocess/service.py`; invalid host values fail closed. | `tests/test_pr24_final_fixes.py::test_a02_manager_and_admission_share_effective_mineru_hosts`; no-network preflight. | No real MinerU request was made. |
| A03 | PASS_OFFLINE | Ordinary `AUTO_GENERATE_OFFLINE_TESTS` no longer bypasses production admission. `runtime/test_dependencies.py` is explicit, process-local test injection only. | `tests/test_pr24_final_fixes.py::test_a03_ordinary_offline_environment_does_not_skip_runner_admission`; full suite. | C/D DeepSeek execution used explicit owner authorization; Q content remained blocked by D. |
| B01 | PASS_OFFLINE | `Stage1AnalysisService._preprocess` now uses `is_blocked_stage1_quality`; rejected text is never reopened by the old plain/Markdown write-back. Scanned-primary remains an explicit visual path. | `tests/test_pr24_final_fixes.py::test_b01_stage1_does_not_reopen_quality_blocked_text`; full suite `1694 passed, 28 skipped`. | Real F1 semantic source review is not complete. |
| B02 | PASS_OFFLINE | Published preprocess generations are validated before snapshotting; derived manifests, structured text, and OCR lineage must agree, and no published leaf is edited in place. | `tests/test_pr24_final_fixes.py::test_b02_snapshot_rejects_changed_leaf_against_published_manifest`; preprocess suite. | No production kill/resume against a live parser was run. |
| C01 | PASS_OFFLINE | Job-scoped durable MinerU budget and trace state use atomic/interprocess persistence; unknown POST outcomes remain non-retryable; restart reconciles an uncommitted task reservation. | `tests/test_pr24_final_fixes.py::test_c01_mineru_budget_is_cross_process_durable`, `test_c01_pending_marker_reconciles_a_reservation_before_post`, `test_c01_unknown_mineru_post_is_persisted_and_not_retried`. | No live MinerU task or real kill/resume was authorized. |
| C02 | PASS_OFFLINE | MinerU transport events have stable sequence/ID updates and bounded truncation without overwriting event 64. | `tests/test_pr24_final_fixes.py::test_c02_transport_event_overflow_does_not_mutate_event_64`. | High-volume live ledger behavior remains untested. |
| D01 | PASS_OFFLINE | JSON requests force streaming, check Content-Length before body consumption, enforce bounded chunk reads/deadline/cancellation, and close responses. | `tests/test_pr24_final_fixes.py::test_d01_json_response_is_streamed_and_bounded`; local transport regression suite; C/D DeepSeek receipts. | No real MinerU parser response was consumed. |
| D02 | PASS_OFFLINE | Remote upload reads one job-owned frozen source object, binds size/hash to the trace, and closes upload responses. | `tests/test_pr24_final_fixes.py::test_d02_upload_uses_frozen_bytes_after_source_replacement`. | No external upload occurred. |
| E01 | PASS_OFFLINE | The six timeout/task/http/upload environment values are in `PREPROCESS_ENV_MAPPING`, wizard/config/example surfaces, and the resolved load chain. | `tests/test_pr24_final_fixes.py::test_e01_normal_load_chain_applies_all_six_mineru_environment_values`; `reviewctl preflight` diagnostics. | Values with no configured source remain explicit defaults, not silently claimed owner settings. |
| E02 | PASS_OFFLINE | Queue snapshots retain a secret-free effective runtime projection plus credential identities/HMACs and reject process/.env drift. | `tests/test_pr24_final_fixes.py::test_e02_queue_freezes_effective_settings_and_rejects_process_env_drift`; queue suite. | No cross-machine queue resume was run. |
| F01 | PASS_OFFLINE | `config_loader` diagnostics expose configured/source booleans and recursively redact secret-bearing fields; no token value is logged. | `tests/test_pr24_final_fixes.py::test_f01_config_loader_diagnostic_never_prints_mineru_token`. | No claim is made about historical logs outside this checkout. |
| G01 | PASS_OFFLINE | Free Mode normalization accepts only a JSON boolean for `ready_to_apply`; strings/numbers fail closed and record normalization errors. | `tests/test_pr24_final_fixes.py::test_g01_ready_to_apply_accepts_only_json_boolean`; Free Mode suite. | No live Free Mode route was needed for this F1 run. |
| G02 | PASS_OFFLINE | `_is_local_host` handles IPv4/IPv6 loopback and URL forms while rejecting ordinary IPv6, malformed ports, and lookalike domains. | `tests/test_pr24_final_fixes.py::test_g02_local_host_parser_handles_ipv6_and_urls`, `test_g02_local_host_parser_does_not_overmatch`. | No production network call was made. |
| J01 | PASS_OFFLINE + SCOPED_ACCEPTANCE | The locked RapidOCR runtime is now used automatically when a scanned page is detected; Tesseract remains an optional fallback. Gate J verification searches all canonical Stage 1 refs for the source-bound OCR lineage. | `F1_OCR_AUXILIARY_PLAN_20260916.json`; current-SHA Gate J `PASS` with `ocr_engine=rapidocr`, 17 OCR pages, page/text hashes, Stage1 consumption, and Registry lineage; `tests/test_acceptance_execution_scenarios.py::test_gate_j_finds_ocr_lineage_across_all_canonical_refs`. | Auxiliary scan only; it is not an F1 primary source and does not satisfy the main D three-way F1 corpus criterion. |
| I01 | SCOPED_PASS_LIVE | `GateIScenario` executes the production-v2 localhost GUI/Playwright collector and binds the GUI-created RuntimeJobSpec hash and job ID into the parent acceptance binding. | `f1-gui-acceptance-20260917-r2`: real browser evidence, trace archive, screenshot manifest, completed canonical JobOutcome, one non-test Primary Reader receipt; `tests/test_acceptance_execution_scenarios.py::test_gate_i_parent_binds_runtime_spec_created_by_production_gui_flow`. | Scoped to one F1-01 PDF analyze flow; it does not prove F1 scanned-primary OCR, D, Q, Outline v3, Writer, validation, or DOCX closure. |
| K01 | PASS_OFFLINE | Gate K runs two independent Windows/Python contention workers and verifies bounded lock waiting, atomic JSON/Registry/Queue updates, conflict handling, and non-mutation of the live parent budget. | `F1_K_OFFLINE_ACCEPTANCE_PLAN_20260917.json`; current-SHA parent `f1-k-offline-acceptance-20260917-96e6f829e4fc` with `process_count=2`, `bounded_wait=true`, `no_corrupt_json=true`, `no_lost_update=true`, `offline_contention_calls=2`, and `live_budget_unchanged=true`; `tests/test_acceptance_execution_scenarios.py::test_gate_k_acceptance_binds_contention_receipt_from_evidence_root`. | Offline supporting evidence only; real live crash/resume and MinerU recovery remain unverified. |
| K02 | PASS_OFFLINE | Windows `msvcrt.locking` retries now reset the lock-file offset before every attempt in pointer, durable-I/O, and queue locks, preventing a contention failure from moving the locked byte range. | `tests/test_job_workspace.py::test_latest_pointer_claim_is_cross_process_atomic` passed in five repeated local runs; related hardening tests 23 passed; Hosted CI run `35159017730` passed all 6 jobs. | OS-level contention remains bounded by the configured timeout; no claim of cross-machine locking is made. |
| E03 | NOT_VERIFIED_FAIL_CLOSED (historical r13) | Gate E's earlier local controlled probe reached a durable receipt, terminated the initial process, and refused resume when a transport-started aggregate reservation had no matching receipt. | `F1_E_LOCAL_ACCEPTANCE_PLAN_20260917.json`; parent `f1-e-local-acceptance-20260917-r13`; interruption receipt and safe resume diagnostic show the ambiguous reservation; no external calls. | Superseded for the positive local boundary by E04; live-provider resume remains unverified. |
| E04 | PASS_OFFLINE_SCOPED | Gate E now terminates immediately after observing the durable receipt/Registry boundary, and counts a provider call as completed for resume purposes only after explicit semantic `passed`. | `F1_E_LOCAL_ACCEPTANCE_PLAN_20260917_R14.json`; parent `f1-e-local-acceptance-20260917-positive`; current executable `a2c934081e93834338249f44e17d4e9af89b2ddc`; interruption/resume evidence records 1 receipt before and 2 after, `duplicate_receipts=0`, `reexecuted_completed_call_ids=[]`, and zero nonlocal calls. | Local stub only; live-provider crash/resume and MinerU recovery remain unverified. |

## Current-route authorization and protocol recheck (2026-09-17)

The current task contains explicit owner authorization for F1 extracted text and
rendered images to the configured custom LLM gateways. The durable
[F1_EXTERNAL_HOST_ACKNOWLEDGEMENT_20260917.json](F1_EXTERNAL_HOST_ACKNOWLEDGEMENT_20260917.json)
records the v2 acknowledgement for `ai.saigou.work` and `chat.178266.xyz`,
bound to route fingerprint
`8c5443705306d6a1df9b726579faef434297215ac3b1dbf95a4ce5ba0f608309` and
validated by `runtime.trust_admission`. The authorized, non-secret Q spec is
[Q_RUNTIME_SPEC_20260917_AUTHORIZED.json](Q_RUNTIME_SPEC_20260917_AUTHORIZED.json).
Its transport policy remains `send_original_pdf=never` and base64-rendered image
transport.

The Writer route passed one real minimal `/v1/responses` probe with HTTP 200 and
reported usage. The Outline route was probed twice with two attempts each; all
four physical attempts failed before HTTP response. A separate header-only
probe showed DNS/TCP/443 reachability but TLS failed with
`SEC_E_CERT_EXPIRED`. The authorized Q therefore was not started; certificate
verification was not disabled. No F1 payload was sent by these probes. This is
current route evidence, not a claim that Q, D, MinerU, DOCX, or semantic
validation has passed.

## Current repository verification

- Full clean pytest command: `python -m pytest -q --maxfail=1` → `1694 passed, 28 skipped` (Windows multiprocessing run in the elevated local environment).
- Focused PR24 regression: `29 passed`.
- F1 corpus source binding: `11 passed, 1 skipped` across manifest/source-intake/runner tests.
- Latest acceptance source-selection/durable-reference regression: `62 passed, 1 skipped`.
- Post-K focused acceptance/Stage 1 regression: `38 passed, 1 skipped`.
- Post-resume-diagnostics focused acceptance/Stage 1 regression: `39 passed, 1 skipped`.
- Pyright on changed production modules: `0 errors, 0 warnings, 0 informations`.
- Ruff fatal checks (`E9,F`) on changed production/test files: `All checks passed!`.
- `python -m py_compile` on changed modules/tests: passed.
- `python -m pip check`: `No broken requirements found.`
- `git diff --check`: passed.
- Current Hosted Windows CI: run `35159017730` at head `e8b6837ec1165b65c3dce124a3bcd20bfbae6c78`, `SUCCESS`, 6/6 jobs.
- Current Gate E local r14: `PASS_OFFLINE_SCOPED` at executable `a2c934081e93834338249f44e17d4e9af89b2ddc`; two local stub calls, positive interruption/resume, no duplicate receipt or semantically completed-call reexecution.

## F1 evidence boundary

- Formal manifest: [F1_CORPUS_MANIFEST_20260915.json](F1_CORPUS_MANIFEST_20260915.json), content SHA `ebaf5c2a9220ed23b527e279c0fd82a6770fa70e5d4ab5d1e1e64f2150ce4319`, file SHA `f741776ea2eda6b5937f4fc13e40569216eb3173ff597fb80eeea2e46dab3e90`.
- The controlled staging root contains 15 copied PDFs; all 15 current bytes match the prior canonical-resolution manifest. The original Zotero PDFs were not modified or deleted.
- Machine-only source ledger: [F1_SOURCE_GROUND_TRUTH_20260915.md](F1_SOURCE_GROUND_TRUTH_20260915.md). It deliberately marks semantic ground truth and human review as not done.
- The authorized live acceptance parent `f1-acceptance-20260916-r3-09477d666a0f` bound C/D/Q evidence to executable SHA `09477d666a0f31c2b1d95a2a6cb25c00b4e7bfca`, plan identity SHA `6cf51ea5...`, and manifest file SHA `f741776ea2eda6b5937f4fc13e40569216eb3173ff597fb80eeea2e46dab3e90`. C passed with one DeepSeek transport; D completed three DeepSeek transports but failed the required three-way modality criterion; Q was stopped by the unmet D prerequisite.
- The authorized Gate I parent `f1-gui-acceptance-20260917-r2` bound one real GUI submission and one successful non-test `Primary_Reader_API` transport to executable SHA `24884c2adaeac2d98af85349dbbccc6980d2c814`; its parent projection is `SCOPED_PASS` because the plan contains only Gate I.
- The authorized Gate K parent `f1-k-offline-acceptance-20260917-96e6f829e4fc` passed as `PASS_OFFLINE` on executable SHA `96e6f829e4fc0b99d636190b742129e74226c8b0`; it used two independent local workers and zero external calls.
- The Gate E local controlled probe `f1-e-local-acceptance-20260917-r13` reached interruption but remained `NOT_VERIFIED` because resume fail-closed on an ambiguous transport-started reservation; it used only a local stub and is not a live-provider PASS.
- The current Gate E r14 local probe `f1-e-local-acceptance-20260917-positive` passed the positive interruption/resume contract offline at executable SHA `a2c934081e93834338249f44e17d4e9af89b2ddc`; the parent remains `NOT_VERIFIED` for live acceptance because all transport was localhost.
- The separate authorized Gate J parent `f1-ocr-aux-20260916-r5-09477d666a0f` passed the clearly labelled auxiliary OCR sample. It does not alter the Q 15-paper set.
- The current Q child and parent plan carry a recorded v2 ACK for `ai.saigou.work,chat.178266.xyz` with route fingerprint `344765ad52227adc74d73030a8c0942f527bc84bf136babf0c873144c30392a9`; it expired at `2026-09-17T11:51:39Z`. Q made zero custom-host calls because D did not pass.

## Delivery state

- The stored F1 plan is executable-SHA-neutral; the control plane records the exact clean checkout SHA at each run. C/D/Q evidence is frozen to `09477d666a0f31c2b1d95a2a6cb25c00b4e7bfca`; Gate I evidence is frozen to `24884c2adaeac2d98af85349dbbccc6980d2c814`; later report-only commits do not change either runtime evidence set.
- PR #24 remains `OPEN`, `isDraft=false`, and no merge or force-push was performed. Hosted CI run `35159017730` is `SUCCESS` 6/6 for head `e8b6837ec1165b65c3dce124a3bcd20bfbae6c78`.
- The acceptance-only budget copy is bounded at 4 Provider calls and 128,000 output tokens; the live C/D ledgers record 4 successful DeepSeek calls and 49,787 output tokens.

## Current-SHA repair and live recheck addendum (2026-09-17)

The current executable repair freeze is
`5835a7be7c654785da751c6f29aa6223d59f1819`, pushed to
`codex/f1-validation-authority-closure`. It remains separate from the
historical C/D/Gate-I/Gate-K evidence freezes above.

Current-SHA local verification: the combined provider-closure, graph-refresh,
Stage 1 fallback/publication, and PR24 regression command completed with
`59 passed` in `210.63s`; Pyright and fatal Ruff checks also passed on the
changed modules. Hosted Windows CI `35226855031` completed `6/6 SUCCESS` for
this exact commit.

| Current item | Current evidence | Judgment / boundary |
|---|---|---|
| Stage 1 semantic fallback | `services/stage1_analysis_service.py`; `tests/test_current_stage1_provider_fallback.py::test_stage1_backup_reader_gets_one_semantic_corrective_retry` | PASS_OFFLINE_TARGETED. Primary and Backup canonical validation now share a bounded corrective retry, with explicit engine and retry accounting. |
| Stage 1 call-graph closure | `services/stage1_analysis_service.py`; `runtime/provider_receipt_closure.py`; `tests/test_stage1_graph_identity_refresh.py`; `tests/test_provider_receipt_closure.py` | PASS_OFFLINE_TARGETED. Deterministic semantic prompt/config variants are registered and prompt variance is accepted only when an exact declared variant matches. |
| Stage 1 publication durability | `services/stage1_analysis_service.py`; `preprocess/service.py`; `tests/test_current_stage1_generation.py::test_current_stage1_publishes_normalized_routing_fields`; `tests/test_pr24_final_fixes.py` | PASS_OFFLINE_TARGETED. Routing normalization occurs before hashing/publication, and preprocess generation directory replacement uses bounded Windows retry. |
| Official DeepSeek F1 R10 | `F1_STAGE1_FULL_RUNTIME_SPEC_20260917_R10.json`; R10 workspace `runs_stage1_r7/f1_stage1_official_fallback_r10__f1-stage1-full-20260917-r10` | BLOCKED_INVALID_CREDENTIAL. Source intake was 15/15; the first real Primary request returned `fatal_config_or_auth` because the configured key is invalid. The runner reports no accepted provider-receipt snapshot and no Stage 1 authority. |
| Full F1 Q | F1 corpus manifest and the existing Q/Outline evidence | NOT_RUN. D remains blocked by the strict three-way `ocr_scanned` requirement; Q also still requires a healthy Outline route and a fresh ACK before custom-host content. |

The R10 attempt used official DeepSeek only and did not send any original PDF.
The custom `ai.saigou.work` and `chat.178266.xyz` routes were not used by the
R10 run. The root `.env` values were loaded only into the child process and
were not printed, persisted, or committed.

## R11–R14 current F1 Stage 1 evidence addendum (2026-09-17)

| Run | Evidence | Current judgment |
|---|---|---|
| R11 | `F1_STAGE1_FULL_RUNTIME_SPEC_20260917_R11.json`; 3 successful Primary receipts followed by invalid configured Backup credential | BLOCKED_INVALID_BACKUP_CREDENTIAL; no authority promoted. |
| R12 + resume | `F1_STAGE1_FULL_RUNTIME_SPEC_20260917_R12.json`; explicit Primary-credential reuse for the official Backup model; placeholder rejection followed by bounded resume and output-budget exhaustion | BLOCKED_PROVIDER_CONTENT; fail-closed, no authority promoted. |
| R13 | `F1_STAGE1_FULL_RUNTIME_SPEC_20260917_R13.json`; one finite 32k→64k length retry; bounded Primary/Backup semantic retries | BLOCKED_PROVIDER_CONTENT; F1-02 remained placeholder, no authority promoted. |
| R14 | `F1_STAGE1_FULL_RUNTIME_SPEC_20260917_R14.json`; execution SHA `420663e4eea4e9e2a1ffdee1d5d16193d7e5d08e`; 15/15 paper artifacts, 18 expected call IDs, 22 successful published receipts, complete provider closure | PASS_LIVE_STAGE1_ONLY. Current closure/status rechecked with `9e42105e529a964396c52320d67aaa719a047817`: `issues=[]`, `completion_status=complete`, `canonical_ready=true`, `STAGE1_AUTHORITY_READY=true`, `VISUAL_QUALIFICATION_READY=true`. |

R14 is bound to the exact 15-paper manifest and used only
`https://api.deepseek.com`; original PDFs remained `send_original_pdf=never`.
The controlled run explicitly reused the verified Primary credential for the
configured official Backup model because the configured Backup credential had
failed in R11. This is recorded as a run-specific credential-source deviation,
not a silent production-config change. R14 proves Stage 1 source/visual/
canonical/receipt closure, not human semantic ground truth or full Q.

The strict D gate is still not met: the three selected production-derived
profiles are F1-01 `text_heavy`, F1-03 `text_heavy`, and F1-14
`visual_table_heavy`; the auxiliary scanned sample remains separate and does
not enter the F1-15 corpus. Q therefore remains blocked before any Outline or
custom-gateway payload. The existing custom-route ACK is stale and the Outline
route still has the recorded `SEC_E_CERT_EXPIRED` TLS blocker.

The secret-free, hash-bound R14 count/identity projection is
[F1_STAGE1_R14_EVIDENCE_SUMMARY_20260918.json](F1_STAGE1_R14_EVIDENCE_SUMMARY_20260918.json).

The current executable D modality recheck is
[F1_D_MODALITY_RECHECK_20260918.json](F1_D_MODALITY_RECHECK_20260918.json).
It confirms the 1/21 OCR/scanned-candidate pages in F1-10 do not qualify as
an `ocr_scanned` primary modality and preserves the strict D `FAIL_MODALITY`
result.
The formal v2 profile derivation used
[F1_D_MODALITY_RUNTIME_SPEC_20260918.json](F1_D_MODALITY_RUNTIME_SPEC_20260918.json)
with the existing R14 Registry and zero provider calls.

The complete 15-paper R14 production diagnostic projection is recorded in
[F1_D_MODALITY_ALL_CORPUS_DIAGNOSTICS_20260918.json](F1_D_MODALITY_ALL_CORPUS_DIAGNOSTICS_20260918.json):
15/15 diagnostic files checked, only F1-10 has a scanned/OCR candidate, and
its 1/21 coverage is below the 25% primary-modality threshold. D therefore
remains `FAIL_MODALITY` on complete-corpus evidence.

R14 machine-level content/source integrity is recorded in
[F1_STAGE1_R14_CONTENT_INTEGRITY_20260918.json](F1_STAGE1_R14_CONTENT_INTEGRITY_20260918.json):
15/15 canonical summaries, zero placeholder/empty-core/source-hash/receipt
binding defects. Human semantic ground truth remains explicitly pending.

Independent source-content cross-check is recorded in
[F1_R14_SOURCE_CONTENT_AUDIT_20260918.json](F1_R14_SOURCE_CONTENT_AUDIT_20260918.json):
15/15 manifest SHA bindings and summary-source paths match; rendered/OCR
evidence resolves the four text-layer numeric gaps. This is agent-level
source verification only and does not satisfy the required human semantic
ground-truth or full C→D→Q acceptance.

# PR24 / F1 current-SHA requirement traceability

Checked at: `2026-09-19T13:27:00Z`
Current checkout: `d7c4a4d117ff2706aa673f7f0b4cd029cd66cf51`
Last executable code SHA used for the D acceptance: `36bcf118c159491aa7ce02a0b941104e8f00dab1`
Branch: `codex/f1-validation-authority-closure`
PR: `#24`, OPEN, not merged

This is a current-state evidence projection. It does not promote historical
receipts or auxiliary OCR into the F1-15 corpus.

## Code-repair matrix

| ID | Current judgment | Current implementation boundary | Fresh evidence | Remaining boundary |
|---|---|---|---|---|
| A01 | PASS_OFFLINE | v2 external-host policy binds normalized scheme/host/port/path, provider/model/endpoint, proxy and parser policy; ACK schema enforces version and issued/expiry timestamps. | `tests/test_pr24_final_fixes.py::test_a01_fingerprint_binds_full_transport_endpoint_and_ack_expiry`; targeted result `30 passed`. | No claim of live custom-host content execution. |
| A02 | PASS_OFFLINE | Shared effective MinerU host policy is consumed by admission and `PreprocessManager`; invalid hosts fail closed. | `test_a02_manager_and_admission_share_effective_mineru_hosts`; targeted result `30 passed`. | No real MinerU request. |
| A03 | PASS_OFFLINE | Ordinary `AUTO_GENERATE_OFFLINE_TESTS` cannot bypass runner admission; only explicit injected test dependencies may do so. | `test_a03_ordinary_offline_environment_does_not_skip_runner_admission`; targeted result `30 passed`. | No external request was made by this regression. |
| B01 | PASS_OFFLINE | Stage 1 quality decisions are fail-closed; blocked/reprocess text is not reopened through plain/Markdown fallback. | `test_b01_stage1_does_not_reopen_quality_blocked_text`; targeted result `30 passed`. | Semantic F1 review remains separate. |
| B02 | PASS_OFFLINE | Published generation leaves are checked against manifest, structured text and OCR lineage before snapshot; no in-place compatibility write-back remains. | `test_b02_snapshot_rejects_changed_leaf_against_published_manifest`; targeted result `30 passed`. | No live parser kill/resume. |
| C01 | PASS_OFFLINE | Durable interprocess MinerU budget/trace state reserves before POST and refuses unknown POST retry; stable reservations reconcile on restart. | Three C01 tests in `test_pr24_final_fixes.py`; targeted result `30 passed`. | Live MinerU recovery is not verified. |
| C02 | PASS_OFFLINE | Transport events use stable IDs and bounded truncation; overflow cannot mutate the prior event. | `test_c02_transport_event_overflow_does_not_mutate_event_64`; targeted result `30 passed`. | No high-volume live ledger. |
| D01 | PASS_OFFLINE | JSON transport is streaming, bounded, deadline/cancellation-aware, and closes responses. | `test_d01_json_response_is_streamed_and_bounded`; targeted result `30 passed`. | No real MinerU response. |
| D02 | PASS_OFFLINE | Remote upload uses one job-owned frozen source object and binds source hash/size to the trace. | `test_d02_upload_uses_frozen_bytes_after_source_replacement`; targeted result `30 passed`. | No external upload. |
| E01 | PASS_OFFLINE | All six MinerU timeout/task/http/upload environment values are mapped through the normal `load_config` chain. | `test_e01_normal_load_chain_applies_all_six_mineru_environment_values`; targeted result `30 passed`. | Runtime values remain configuration-dependent. |
| E02 | PASS_OFFLINE | Queue snapshots freeze a secret-free effective settings projection, provenance identities and reject process/.env drift. | `test_e02_queue_freezes_effective_settings_and_rejects_process_env_drift`; targeted result `30 passed`. | No cross-machine resume. |
| F01 | PASS_OFFLINE | Diagnostics expose configured/source state and redact token-bearing fields. | `test_f01_config_loader_diagnostic_never_prints_mineru_token`; targeted result `30 passed`. | Historical logs outside this checkout are not audited. |
| G01 | PASS_OFFLINE | `ready_to_apply` accepts only a JSON boolean; strings/numbers fail closed. | `test_g01_ready_to_apply_accepts_only_json_boolean`; targeted result `30 passed`. | No live Free Mode route required. |
| G02 | PASS_OFFLINE | Loopback parsing uses URL/IP normalization and handles IPv4/IPv6 forms without lookalike overmatch. | G02 parameterized tests; targeted result `30 passed`. | No production network call required. |
| H01 | PASS_OFFLINE | F1-bound Outline reuse retains local PDF source intake for manifest binding instead of converting the source bundle to summary-only. Gate F empty child selection resolves to the exact RuntimeSpec manifest set. | `tests/test_runtime_orchestrator.py::test_f1_bound_outline_with_reused_summary_still_intakes_local_pdfs`; `tests/test_f1_corpus_manifest.py::test_outline_gate_f_empty_selection_uses_exact_runtime_binding`; both focused tests pass. | Current-SHA live Outline remains provider-credential blocked. |

## Current offline verification

Commands run from the current worktree:

```text
D:\Anaconda\python.exe -m pytest -q tests/test_pr24_final_fixes.py
30 passed, 1 warning in 11.52s

D:\Anaconda\python.exe -m pytest -q
1689 passed, 28 skipped, 13 failed in 3508.12s

The 13 failures all occurred while creating Windows multiprocessing named
pipes (`WinError 5`) before test bodies ran in the sandbox. The same 13 node
IDs reran outside the sandbox and passed: `13 passed in 86.01s`. Hosted CI
run `35438729262` for current checkout `8415f6b1...` completed all 6 jobs
successfully. The sandbox full-run result is retained as an environment
limitation, not relabeled as a product pass.

D:\Anaconda\python.exe -m pip check
FAILED in the shared Anaconda environment because of unrelated installed-package conflicts; clean-lock pip check passed in Hosted CI run `35438729262`.

D:\Anaconda\python.exe -m compileall -q runtime preprocess services free_mode config_loader.py reviewctl.py
PASS

D:\Anaconda\python.exe -m ruff check runtime/trust_admission.py runtime/runner.py preprocess/service.py services/credential_provenance.py services/queue_service.py services/stage1_analysis_service.py free_mode/service.py config_loader.py --select E9,F
All checks passed.

Hosted CI run `35438729262` completed the clean-install Pyright and fatal-Ruff checks successfully.
```

The full suite's 28 skips remain explicit capability/environment skips; they
are not counted as live Provider, MinerU, GUI, OCR, or F1 semantic passes.

## Current Q execution readback

The Q preparation was executed through the current runtime rather than left at
preflight:

- Runtime config: `D:/auto-generate/F1_Q_RUNTIME_CONFIG_20260919_LOCAL.ini`;
  parser mode is local, original-PDF transport is never, and the F1 manifest
  remains the exact 15-source manifest with SHA-256
  `f741776ea2eda6b5937f4fc13e40569216eb3173ff597fb80eeea2e46dab3e90`.
- Acceptance plan: `F1_Q_LIVE_ACCEPTANCE_PLAN_20260919.json`, parent
  `f1-acceptance-Q-20260919-local-r8`, with the acknowledged two-host policy
  and the bounded budget of 64 calls / 2,000,000 output tokens / 16 retries /
  7200 seconds.
- Durable job workspace:
  `D:/auto-generate/.acceptance_runs/f1_q_local_r3/f1_acceptance_Q_local_parser__f1-acceptance-Q-20260919-local-r3`.
  Its Stage 1 progress snapshot records 15 summaries and zero failed papers.
- Canonical `reviewctl status` readback is `job_status=failed`,
  `failed_stage=outline`, `attempt_number=6`, with only `source_intake` and
  `analyze` completed; `outline`, `review`, and `validate` remain absent from
  the current artifact set.
- The latest attempt snapshot records the terminal reason
  `provider transport attempts exceeded the pre-admitted aggregate
  reservation`. Earlier large Outline requests to the configured
  `api.yhlxj.ai` route produced the recorded 524/ProxyError failure; the
  latest retry therefore remains `NOT_VERIFIED`, not a Q pass.
- The latest `resume_state_report` says `strong_resumable`, but its referenced
  checkpoint file is not present in the current workspace. Resume integrity is
  therefore not independently verified and must not be reported as PASS.
- A current, minimal `Writer_API` probe against `ai.saigou.work` was admitted
  and made three attempts; the receipt reported HTTP 503 with
  `error_kind=retryable_http`. This does not establish a usable fallback for
  the Outline roles and did not send F1 documents.

This section records an incomplete live execution. It does not promote Stage 1
outputs, the successful relation receipt, a ping probe, or any historical green
run into full Q acceptance.

## AihubMix Outline-only retry readback

The user explicitly authorized sending the complete F1 extracted text/rendered
images to AihubMix; the original PDFs remained prohibited. To honor the user's
correction, this retry reused the existing canonical 15-paper Stage 1 summary
artifact and did not rerun Stage 1 or issue Stage 1 provider calls.

- The previous failed node was `candidate_1_provider_generation` on
  `api.yhlxj.ai`, with node route fingerprint
  `43a238be2ee491a28d8ee59cb145d89ec8cc0eb8ea2bd91b26f16e135933763c`.
- The AihubMix Outline route fingerprint is
  `2550d1a10fac4fcd4bb2890859435800d3c8e0908d5b2eb75a0adc0c82954d34`;
  the Outline-only external-host policy fingerprint is
  `6b0e8045e1dc36009667cf6574f64e905c9416fa1727d3c3eceb0dfbfe72b5c4`.
- Four minimal physical-route probes passed with zero retries and zero
  timeouts. The real Outline-only Gate F child then completed the
  `relation_adjudication` call with HTTP 200 and 6,986 output tokens, but the
  first large `candidate_1_provider_generation` request failed with
  `transient_network` / `ProxyError: RemoteDisconnected`. No 524 was observed;
  this gateway closed the connection before a provider receipt could be
  persisted. The child terminal reason was
  `provider transport attempts exceeded the pre-admitted aggregate reservation`.
- Gate F is `NOT_VERIFIED`; Outline closure is incomplete and no full Q PASS is
  claimed. Evidence projection:
  `F1_AIHUBMIX_OUTLINE_RETRY_SUMMARY_20260919.json`.

## Current-SHA AihubMix changed-shape retry readback (R13)

The current executable `d7c4a4d117ff2706aa673f7f0b4cd029cd66cf51` was used in a
clean acceptance worktree. The child reused the exact 15-paper canonical Stage1
summary artifact, kept `send_original_pdf=never`, and changed only the
`Outline_API.max_output_tokens` request shape from 65536 to 32000. Runtime
source intake and Outline call-plan publication completed without Stage1
provider calls.

- The first persisted provider call was `relation_adjudication` through
  `aihubmix_claude` / `claude-fable-5-1`; AihubMix returned HTTP 401 with
  `fatal_config_or_auth` / invalid-key rejection.
- No candidate-generation request was attempted, so this run does not answer
  whether the lower output cap fixes the earlier large-request disconnect.
- Provider calls used: 1; retries: 0. Gate F remains `NOT_VERIFIED` because
  closure is incomplete. Evidence projection:
  `F1_AIHUBMIX_OUTLINE_RETRY_SUMMARY_20260919_R13.json`.

## Current-SHA valid-key retry readback (R14/R15)

The user-provided key was written with the project's atomic dotenv writer to
the four AihubMix route variables only. Three no-document route probes then
passed for Outline, Free Mode, and Writer.

- R14 received HTTP 200 and 6,536 relation-adjudication output tokens, but the
  model returned a rejected relation ID with a `_placeholder_ignore` suffix
  that was not present in the candidate set. The existing fail-closed validator
  correctly stopped before candidate generation.
- R15 received HTTP 200 and 7,972 relation-adjudication output tokens, then
  attempted the 32,000-output candidate request; AihubMix closed the
  connection with `ProxyError: RemoteDisconnected`. The lower output cap did
  not eliminate the large-request disconnect.
- No original PDFs were transported and Stage1 was not rerun. Evidence:
  `F1_AIHUBMIX_OUTLINE_RETRY_SUMMARY_20260919_R14_R15.json`.

### Provider-side dashboard correlation (owner-provided, not local closure)

The user's AihubMix usage export exactly matches the R14/R15 relation receipts:
`68080 -> 6536` and `68080 -> 7972`, both marked successful upstream. It also
shows two `AWS/claude-opus-5` requests with `56835` input tokens and `18474` /
`18964` output tokens over 291/309 seconds. Those rows are consistent with the
candidate-generation sequence and show that AihubMix may have completed and
billed the upstream work even when the local proxy returned
`RemoteDisconnected`.

This is corroborating provider-side evidence only. The candidate response was
not delivered to the local runtime, so there is no local provider receipt,
content hash, schema validation, Registry artifact, or receipt-closure proof;
Gate F and Q therefore remain `NOT_VERIFIED`. See
`F1_AIHUBMIX_PROVIDER_DASHBOARD_CORRELATION_20260919.json`.

### AihubMix LLM recovery probe after user-enabled async tasks

After the user reported enabling AihubMix asynchronous tasks, a new single
candidate probe still ended in `ProxyError: RemoteDisconnected`. The API key
then returned HTTP 200 with zero LLM recovery tasks for both the exact model
filter and the account-wide LLM list; twelve additional polls over three
minutes also returned zero tasks. The two dashboard Tids were not task IDs and
returned HTTP 404 from the task-detail endpoint. This proves that no
recoverable response was observable through the API key for this probe; it does
not prove that upstream computation was discarded. Evidence:
`F1_AIHUBMIX_RECOVERY_PROBE_20260919.json`.

### Recovery-enabled formal retry (R16)

The first formal recovery-enabled retry bound to executable SHA
`5be593af2d0666a8f07d5ac62032eb09d3e470a6` was stopped at
`relation_adjudication` by HTTP 403 `quota_exhausted` with provider Tid
`2026091915565775613189009841924`. Candidate generation was not attempted and
no recovery task could be created. This is an account-balance gate, not a
semantic or local-code PASS. Evidence:
`F1_AIHUBMIX_OUTLINE_RECOVERY_RETRY_SUMMARY_20260919_R16.json`.

## F1 corpus and runtime boundary

- Formal corpus: `F1_CORPUS_MANIFEST_20260915.json`, SHA-256
  `f741776ea2eda6b5937f4fc13e40569216eb3173ff597fb80eeea2e46dab3e90`.
- R14 Stage 1/source-integrity evidence is bound to the same 15 sources:
  `F1_R14_SOURCE_CONTENT_AUDIT_20260918.json`, SHA-256
  `b4be9cc714acd6dadca580dcd662e56b6860514d02e5187bb4d5c855fc0deea2`.
  It proves 15/15 source hashes and machine content cross-checks; human semantic
  ground truth remains `PENDING_HUMAN_REVIEW`.
- Complete R14 D diagnostics:
  `F1_D_MODALITY_ALL_CORPUS_DIAGNOSTICS_20260918.json`, SHA-256
  `7cd113489a288a409069c014fea7205e426afbc47052e6986b141c7a8175f652`.
  All 15 diagnostics were checked; only F1-10 has 1 scanned/OCR page of 21,
  so no source reaches the 25% `ocr_scanned` primary threshold under the
  original strict policy; this historical diagnostic is `D=FAIL_MODALITY`.
- The user-approved D policy was subsequently changed to
  `f1-two-in-corpus-plus-auxiliary-ocr-v1`. Under that explicitly scoped policy,
  D child acceptance is PASS at executable SHA `36bcf118c159491aa7ce02a0b941104e8f00dab1`
  for F1-01/F1-03/F1-14, with two in-corpus modalities and a separate Zotero
  OCR auxiliary fixture. The D parent is `SCOPED_PASS`, not full-release PASS;
  F1 Q corpus binding is unchanged.
- Q route admission and authorization:
  `F1_EXTERNAL_HOST_ACKNOWLEDGEMENT_20260919_Q_LOCAL.json` and
  `Q_RUNTIME_SPEC_20260919_LOCAL_AUTHORIZED.json`. The acknowledged hosts are
  `ai.saigou.work` and `api.yhlxj.ai`; local parsing is enforced and only
  extracted text/rendered images are in scope for provider transport.
- Six historical synthetic production-path provider probes passed in
  `F1_PROVIDER_MICRO_PROBE_20260918.json` (SHA-256
  `26f92d8668eb58cb7cf16a074ad633cb7a66b02b06dbffbc2996b24b172d20c2`).
  These used only `ping`; they do not prove Outline quality, Writer, Validator,
  DOCX, or F1 Q.

## Auxiliary Zotero OCR evidence

The user-requested read-only Zotero search found `WFX3F52Q`,
*The Persuasion Knowledge Model: How People Cope with Persuasion Attempts*,
DOI `10.1086/209380`. Its 62/62 pages meet the repository scan-candidate rule;
visual renders and RapidOCR samples for pages 2/32/62 succeeded. The audit is
`zotero_auxiliary_scan_20260918/F1_ZOTERO_AUXILIARY_SCAN_CANDIDATE_AUDIT_20260918.json`,
SHA-256 `c355350f16ba9d55089eddd7dcbcd5df0fac2fe51f4788a18dac58cef17d51c9`.

This PDF is explicitly auxiliary: it is not in the F1 manifest, was not sent to
an external provider, does not change F1 D, and does not start Q.

A directed Zotero audit then matched all 15 F1 identities (14 DOIs plus the
F1-11 Chinese title/author identity) and inspected 19 PDF attachments. It found
no new F1 scan-primary source; the only scanned-candidate attachment was an
F1-10 duplicate with 1/21 pages and the same SHA as the manifest source. See
`F1_ZOTERO_F1_DUPLICATE_ATTACHMENT_MODALITY_AUDIT_20260919.json`.

## Final status matrix

```text
FINAL_EXECUTABLE_SHA: d7c4a4d117ff2706aa673f7f0b4cd029cd66cf51
CURRENT_CHECKOUT_SHA: d7c4a4d117ff2706aa673f7f0b4cd029cd66cf51
EVIDENCE_COMMIT_SHA: d7c4a4d117ff2706aa673f7f0b4cd029cd66cf51 (execution base)
PR_STATE: OPEN
PR_MERGED: false
CODE_REPAIR_STATUS: PASS_OFFLINE
OFFLINE_REGRESSION_STATUS: CI PASS (6/6); local sandbox 1689 passed, 28 skipped, 13 named-pipe ACL-blocked; blocked nodes 13/13 pass outside sandbox
PRODUCTION_INTEGRATION_STATUS: CURRENT-SHA OUTLINE ATTEMPTED; SEMANTIC/NETWORK BLOCKED; NOT_VERIFIED
AIHUBMIX_OUTLINE_RETRY_STATUS: R14 SEMANTIC CONTRACT BLOCK; R15 LOCAL CANDIDATE RECEIPT MISSING AFTER REMOTE-DISCONNECT; RECOVERY PROBE FOUND NO TASK; R16 ACCOUNT BALANCE 403 BEFORE CANDIDATE; NOT_VERIFIED
F1_CORPUS_BINDING_STATUS: PASS_MACHINE_SOURCE_BINDING (15/15); HUMAN_SEMANTIC_GROUND_TRUTH_PENDING
F1_C_D_Q_STATUS: C STAGE1 15/15; D SCOPED_PASS UNDER APPROVED POLICY; Q NOT_VERIFIED
F1_CONTENT_AND_DOCX_QA_STATUS: NOT_VERIFIED_FOR_FINAL_Q
PRODUCTION_GUI_STATUS: SCOPED_GATE_I_ONLY; NOT_FULL_F1_Q
REAL_OCR_STATUS: PASS_AUXILIARY_ZOTERO_ONLY; NO F1 OCR-PRIMARY SOURCE
RESUME_AND_BUDGET_STATUS: BUDGETED_Q_ATTEMPTS_RECORDED; CHECKPOINT_READBACK_INCONSISTENT; LIVE MINERU RECOVERY UNVERIFIED
GOVERNANCE_STATUS: NOT_COMPLETE; main branch protection remains disabled
FINAL_RELEASE_STATUS: NOT_READY_TO_MERGE
REMAINING_BLOCKERS: relation-output contract repair or valid retry; AihubMix large-candidate transport stability; Q Outline/Writer/Validator/DOCX closure; Q resume checkpoint integrity; human semantic ground truth; live MinerU recovery; full GUI Q; governance/branch protection
```

# PR24 / F1 current-SHA requirement traceability

Checked at: `2026-09-18T15:41:36.4067700Z`
Executable checkout: `b6fa86b7b75fbd35888f871066e0e00aae70eac0`
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

## Current offline verification

Commands run from the current worktree:

```text
D:\Anaconda\python.exe -m pytest -q tests/test_pr24_final_fixes.py
30 passed, 1 warning in 6.46s

D:\Anaconda\python.exe -m pytest -q -p no:cacheprovider
1700 passed, 28 skipped in 2434.50s

D:\Anaconda\python.exe -m pip check
No broken requirements found.

D:\Anaconda\python.exe -m compileall -q runtime preprocess services free_mode config_loader.py reviewctl.py
PASS

D:\Anaconda\python.exe -m ruff check ... --select E9,F
All checks passed.

D:\Anaconda\python.exe -m pyright runtime/trust_admission.py runtime/runner.py preprocess/service.py services/credential_provenance.py services/queue_service.py services/stage1_analysis_service.py free_mode/service.py config_loader.py
0 errors, 0 warnings, 0 informations
```

The full suite's 28 skips remain explicit capability/environment skips; they
are not counted as live Provider, MinerU, GUI, OCR, or F1 semantic passes.

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
  so no source reaches the 25% `ocr_scanned` primary threshold. `D=FAIL_MODALITY`.
- Q route admission:
  `F1_Q_ROUTE_ADMISSION_RECHECK_20260918_R2.json`, SHA-256
  `25e0035148807aa69ce6a6a268b205693a25674aa6c4e096ea0abb03800071f4`.
  Fresh v2 ACK validation is PASS for the current five-host policy, but Q made
  zero calls and remains `BLOCKED_D_PREREQUISITE`.
- Six synthetic production-path provider probes passed in
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

## Final status matrix

```text
FINAL_EXECUTABLE_SHA: b6fa86b7b75fbd35888f871066e0e00aae70eac0
PR_STATE: OPEN
PR_MERGED: false
CODE_REPAIR_STATUS: PASS_OFFLINE
OFFLINE_REGRESSION_STATUS: PASS (1700 passed, 28 skipped)
PRODUCTION_INTEGRATION_STATUS: SYNTHETIC_ROUTE_REACHABILITY_ONLY; Q NOT STARTED
F1_CORPUS_BINDING_STATUS: PASS_MACHINE_SOURCE_BINDING (15/15); HUMAN_SEMANTIC_GROUND_TRUTH_PENDING
F1_C_D_Q_STATUS: C/R14 STAGE1 EVIDENCE EXISTS; D FAIL_MODALITY; Q BLOCKED_D_PREREQUISITE
F1_CONTENT_AND_DOCX_QA_STATUS: NOT_VERIFIED_FOR_FINAL_Q
PRODUCTION_GUI_STATUS: SCOPED_GATE_I_ONLY; NOT_FULL_F1_Q
REAL_OCR_STATUS: PASS_AUXILIARY_ZOTERO_ONLY; NO F1 OCR-PRIMARY SOURCE
RESUME_AND_BUDGET_STATUS: PASS_OFFLINE_SCOPED; LIVE PROVIDER/MinerU RECOVERY UNVERIFIED
GOVERNANCE_STATUS: NOT_COMPLETE; main branch protection remains disabled
FINAL_RELEASE_STATUS: NOT_READY_TO_MERGE
REMAINING_BLOCKERS: F1 D modality or explicitly approved D-policy change; human semantic ground truth; Q 15-paper Outline/Writer/Validator/DOCX closure; live MinerU recovery; full GUI Q; governance/branch protection
```

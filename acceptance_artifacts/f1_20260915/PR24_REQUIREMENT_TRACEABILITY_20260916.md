# PR24 final-fix requirement traceability

Live acceptance executable freeze: `09477d666a0f31c2b1d95a2a6cb25c00b4e7bfca` on `codex/f1-validation-authority-closure`.

The code result below is `PASS_OFFLINE` only where the current repository checks prove it. It is not a claim that live Provider, MinerU, GUI, OCR, or semantic F1 acceptance passed. The separate [F1 acceptance report](F1_LIVE_ACCEPTANCE_REPORT_20260916.md) records those boundaries.

| ID | Current judgment | Implementation / authoritative boundary | Fresh evidence | Remaining boundary |
|---|---|---|---|---|
| A01 | PASS_OFFLINE | `runtime/trust_admission.py` binds normalized scheme/host/port/path, provider/model/endpoint, proxy mode, parser policy, and v2 ACK timestamps/schema. `runtime/release_acceptance.py` consumes the same ACK schema. | `tests/test_pr24_final_fixes.py::test_a01_fingerprint_binds_full_transport_endpoint_and_ack_expiry`; trust-admission suite; Pyright/Ruff/compile; current Q child admission. | No live custom-host content was sent because D remained an unmet prerequisite. |
| A02 | PASS_OFFLINE | `services/mineru_policy.py` is the shared effective host policy used by admission and `preprocess/service.py`; invalid host values fail closed. | `tests/test_pr24_final_fixes.py::test_a02_manager_and_admission_share_effective_mineru_hosts`; no-network preflight. | No real MinerU request was made. |
| A03 | PASS_OFFLINE | Ordinary `AUTO_GENERATE_OFFLINE_TESTS` no longer bypasses production admission. `runtime/test_dependencies.py` is explicit, process-local test injection only. | `tests/test_pr24_final_fixes.py::test_a03_ordinary_offline_environment_does_not_skip_runner_admission`; full suite. | C/D DeepSeek execution used explicit owner authorization; Q content remained blocked by D. |
| B01 | PASS_OFFLINE | `Stage1AnalysisService._preprocess` now uses `is_blocked_stage1_quality`; rejected text is never reopened by the old plain/Markdown write-back. Scanned-primary remains an explicit visual path. | `tests/test_pr24_final_fixes.py::test_b01_stage1_does_not_reopen_quality_blocked_text`; full suite `1692 passed, 28 skipped`. | Real F1 semantic source review is not complete. |
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

## Current repository verification

- Full clean pytest command: `python -m pytest -q --maxfail=1` → `1692 passed, 28 skipped` (Windows multiprocessing run in the elevated local environment).
- Focused PR24 regression: `29 passed`.
- F1 corpus source binding: `11 passed, 1 skipped` across manifest/source-intake/runner tests.
- Latest acceptance source-selection/durable-reference regression: `62 passed, 1 skipped`.
- Pyright on changed production modules: `0 errors, 0 warnings, 0 informations`.
- Ruff fatal checks (`E9,F`) on changed production/test files: `All checks passed!`.
- `python -m py_compile` on changed modules/tests: passed.
- `python -m pip check`: `No broken requirements found.`
- `git diff --check`: passed.

## F1 evidence boundary

- Formal manifest: [F1_CORPUS_MANIFEST_20260915.json](F1_CORPUS_MANIFEST_20260915.json), content SHA `ebaf5c2a9220ed23b527e279c0fd82a6770fa70e5d4ab5d1e1e64f2150ce4319`, file SHA `f741776ea2eda6b5937f4fc13e40569216eb3173ff597fb80eeea2e46dab3e90`.
- The controlled staging root contains 15 copied PDFs; all 15 current bytes match the prior canonical-resolution manifest. The original Zotero PDFs were not modified or deleted.
- Machine-only source ledger: [F1_SOURCE_GROUND_TRUTH_20260915.md](F1_SOURCE_GROUND_TRUTH_20260915.md). It deliberately marks semantic ground truth and human review as not done.
- The authorized live acceptance parent `f1-acceptance-20260916-r3-09477d666a0f` bound C/D/Q evidence to executable SHA `09477d666a0f31c2b1d95a2a6cb25c00b4e7bfca`, plan identity SHA `6cf51ea5...`, and manifest file SHA `f741776ea2eda6b5937f4fc13e40569216eb3173ff597fb80eeea2e46dab3e90`. C passed with one DeepSeek transport; D completed three DeepSeek transports but failed the required three-way modality criterion; Q was stopped by the unmet D prerequisite.
- The separate authorized Gate J parent `f1-ocr-aux-20260916-r5-09477d666a0f` passed the clearly labelled auxiliary OCR sample. It does not alter the Q 15-paper set.
- The current Q child and parent plan both carry a valid v2 ACK for `ai.saigou.work,chat.178266.xyz` with route fingerprint `344765ad52227adc74d73030a8c0942f527bc84bf136babf0c873144c30392a9`; Q made zero custom-host calls because D did not pass.

## Delivery state

- The stored F1 plan is executable-SHA-neutral; the control plane records the exact clean checkout SHA at each run. The live evidence is frozen to `09477d666a0f31c2b1d95a2a6cb25c00b4e7bfca`; later report-only commits do not change it.
- PR #24 remains `OPEN`, `isDraft=false`, and no merge or force-push was performed. Hosted CI run `35115650237` passed all six jobs for executable SHA `09477d666a0f31c2b1d95a2a6cb25c00b4e7bfca`; this report update is documentation-only.
- The acceptance-only budget copy is bounded at 4 Provider calls and 128,000 output tokens; the live C/D ledgers record 4 successful DeepSeek calls and 49,787 output tokens.

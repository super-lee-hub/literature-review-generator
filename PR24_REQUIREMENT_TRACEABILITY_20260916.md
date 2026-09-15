# PR24 final-fix requirement traceability

Executable freeze: `12bf400d3cf6fd7711da8750856e25ebc7d0da85` on `codex/f1-validation-authority-closure`.

The code result below is `PASS_OFFLINE` only where the current repository checks prove it. It is not a claim that live Provider, MinerU, GUI, OCR, or semantic F1 acceptance passed. The separate [F1 acceptance report](acceptance_artifacts/f1_20260915/F1_LIVE_ACCEPTANCE_REPORT_20260916.md) records those boundaries.

| ID | Current judgment | Implementation / authoritative boundary | Fresh evidence | Remaining boundary |
|---|---|---|---|---|
| A01 | PASS_OFFLINE | `runtime/trust_admission.py` binds normalized scheme/host/port/path, provider/model/endpoint, proxy mode, parser policy, and v2 ACK timestamps/schema. `runtime/release_acceptance.py` consumes the same ACK schema. | `tests/test_pr24_final_fixes.py::test_a01_fingerprint_binds_full_transport_endpoint_and_ack_expiry`; trust-admission suite; Pyright/Ruff/compile. | No live custom-host ACK was exercised. |
| A02 | PASS_OFFLINE | `services/mineru_policy.py` is the shared effective host policy used by admission and `preprocess/service.py`; invalid host values fail closed. | `tests/test_pr24_final_fixes.py::test_a02_manager_and_admission_share_effective_mineru_hosts`; no-network preflight. | No real MinerU request was made. |
| A03 | PASS_OFFLINE | Ordinary `AUTO_GENERATE_OFFLINE_TESTS` no longer bypasses production admission. `runtime/test_dependencies.py` is explicit, process-local test injection only. | `tests/test_pr24_final_fixes.py::test_a03_ordinary_offline_environment_does_not_skip_runner_admission`; full suite. | A real external subprocess remains blocked until explicit payload authorization. |
| B01 | PASS_OFFLINE | `Stage1AnalysisService._preprocess` now uses `is_blocked_stage1_quality`; rejected text is never reopened by the old plain/Markdown write-back. Scanned-primary remains an explicit visual path. | `tests/test_pr24_final_fixes.py::test_b01_stage1_does_not_reopen_quality_blocked_text`; full suite `1687 passed, 28 skipped`. | Real F1 semantic source review is not complete. |
| B02 | PASS_OFFLINE | Published preprocess generations are validated before snapshotting; derived manifests, structured text, and OCR lineage must agree, and no published leaf is edited in place. | `tests/test_pr24_final_fixes.py::test_b02_snapshot_rejects_changed_leaf_against_published_manifest`; preprocess suite. | No production kill/resume against a live parser was run. |
| C01 | PASS_OFFLINE | Job-scoped durable MinerU budget and trace state use atomic/interprocess persistence; unknown POST outcomes remain non-retryable; restart reconciles an uncommitted task reservation. | `tests/test_pr24_final_fixes.py::test_c01_mineru_budget_is_cross_process_durable`, `test_c01_pending_marker_reconciles_a_reservation_before_post`, `test_c01_unknown_mineru_post_is_persisted_and_not_retried`. | No live MinerU task or real kill/resume was authorized. |
| C02 | PASS_OFFLINE | MinerU transport events have stable sequence/ID updates and bounded truncation without overwriting event 64. | `tests/test_pr24_final_fixes.py::test_c02_transport_event_overflow_does_not_mutate_event_64`. | High-volume live ledger behavior remains untested. |
| D01 | PASS_OFFLINE | JSON requests force streaming, check Content-Length before body consumption, enforce bounded chunk reads/deadline/cancellation, and close responses. | `tests/test_pr24_final_fixes.py::test_d01_json_response_is_streamed_and_bounded`; local transport regression suite. | No real provider/parser response was consumed. |
| D02 | PASS_OFFLINE | Remote upload reads one job-owned frozen source object, binds size/hash to the trace, and closes upload responses. | `tests/test_pr24_final_fixes.py::test_d02_upload_uses_frozen_bytes_after_source_replacement`. | No external upload occurred. |
| E01 | PASS_OFFLINE | The six timeout/task/http/upload environment values are in `PREPROCESS_ENV_MAPPING`, wizard/config/example surfaces, and the resolved load chain. | `tests/test_pr24_final_fixes.py::test_e01_normal_load_chain_applies_all_six_mineru_environment_values`; `reviewctl preflight` diagnostics. | Values with no configured source remain explicit defaults, not silently claimed owner settings. |
| E02 | PASS_OFFLINE | Queue snapshots retain a secret-free effective runtime projection plus credential identities/HMACs and reject process/.env drift. | `tests/test_pr24_final_fixes.py::test_e02_queue_freezes_effective_settings_and_rejects_process_env_drift`; queue suite. | No cross-machine queue resume was run. |
| F01 | PASS_OFFLINE | `config_loader` diagnostics expose configured/source booleans and recursively redact secret-bearing fields; no token value is logged. | `tests/test_pr24_final_fixes.py::test_f01_config_loader_diagnostic_never_prints_mineru_token`. | No claim is made about historical logs outside this checkout. |
| G01 | PASS_OFFLINE | Free Mode normalization accepts only a JSON boolean for `ready_to_apply`; strings/numbers fail closed and record normalization errors. | `tests/test_pr24_final_fixes.py::test_g01_ready_to_apply_accepts_only_json_boolean`; Free Mode suite. | No live Free Mode route was needed for this F1 run. |
| G02 | PASS_OFFLINE | `_is_local_host` handles IPv4/IPv6 loopback and URL forms while rejecting ordinary IPv6, malformed ports, and lookalike domains. | `tests/test_pr24_final_fixes.py::test_g02_local_host_parser_handles_ipv6_and_urls`, `test_g02_local_host_parser_does_not_overmatch`. | No production network call was made. |

## Current repository verification

- Full clean pytest command: `python -m pytest -q --maxfail=1` → `1687 passed, 28 skipped`.
- Focused PR24 regression: `29 passed`.
- F1 corpus source binding: `11 passed, 1 skipped` across manifest/source-intake/runner tests.
- Pyright on changed production modules: `0 errors, 0 warnings, 0 informations`.
- Ruff fatal checks (`E9,F`) on changed production/test files: `All checks passed!`.
- `python -m py_compile` on changed modules/tests: passed.
- `python -m pip check`: `No broken requirements found.`
- `git diff --check`: passed.

## F1 evidence boundary

- Formal manifest: [F1_CORPUS_MANIFEST_20260915.json](acceptance_artifacts/f1_20260915/F1_CORPUS_MANIFEST_20260915.json), content SHA `ebaf5c2a9220ed23b527e279c0fd82a6770fa70e5d4ab5d1e1e64f2150ce4319`, file SHA `f741776ea2eda6b5937f4fc13e40569216eb3173ff597fb80eeea2e46dab3e90`.
- The controlled staging root contains 15 copied PDFs; all 15 current bytes match the prior canonical-resolution manifest. The original Zotero PDFs were not modified or deleted.
- Machine-only source ledger: [F1_SOURCE_GROUND_TRUTH_20260915.md](acceptance_artifacts/f1_20260915/F1_SOURCE_GROUND_TRUTH_20260915.md). It deliberately marks semantic ground truth and human review as not done.
- The current no-network acceptance dry-run bound all C/D/Q receipts to executable SHA `12bf400d...` and manifest file SHA `f741776e...`, then stopped at owner authorization before Provider transport. Therefore C, D, and Q are `NOT_VERIFIED`, not PASS.

## Delivery state

- Local executable commit exists at the SHA above.
- PR #24 was read back as `OPEN`, `isDraft=false`, with remote head still `1fcc5b5a4c4f78de9378419d9a981f149edc5a08` and the previously reported six Hosted CI successes. The push of `12bf400d...` failed over both SSH and HTTPS; the branch was not force-pushed and no merge was performed.
- The code and evidence remain locally available. A later evidence-only commit may change PR HEAD without changing the frozen executable SHA; any live receipt must continue to bind to the frozen SHA explicitly.

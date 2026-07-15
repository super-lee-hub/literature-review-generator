# auto-generate reliability upgrade implementation report

## Delivery result

All Phase 0-8 requirements and the post-review reliability closure are
implemented and locally verified on the single branch
`codex/auto-generate-reliability-upgrade`.

- Base: `origin/main@5becc4e50e234244162dca980062fe68e4e0e1e9`
- Validated code SHA: `ed6f0430c2aeb326e2fc26e183c1656ecc40e29e`
- Validation disposition: `passed`
- Adversarial ReviewBatch review: local verdict `PASS`
- Six-blocker audit: all contracts passed after fixes
- Final required test gate: `1082 passed, 22 skipped`
- Final strict-offline gate: `1082 passed, 22 deselected`
- Repository-wide Pyright: `0 errors, 0 warnings, 0 informations`

The report intentionally records the validated code SHA rather than its own
commit. The final PR head SHA and PR URL belong in the PR body.

## Checkpoint history

| Phase | Code checkpoint | Evidence checkpoint | Status |
| --- | --- | --- | --- |
| 0 - hotfix/offline baseline | `63aeba8` | `3ebd8b2` | passed |
| 1 - Zotero/FileIndex | `24f318d` | `c5f25d2` | passed |
| 2 - identity/Registry/audit | `0d0f879` | `18c06c6` | passed |
| 3 - sentence/Validation truth | `89a3c9a` | `6a3cb4d` | passed |
| 4 - ReviewBatch derivation | `99a0ce7` | `f348716` | passed |
| 5 - runtime/recovery | `2597c5d` | `5619a56` | passed |
| 6 - Outline health/budget | `d7243c3` | `bcdc9a6` | passed |
| 7 - evidence/edge checkpoints | `49e2275` | `ff667c1` | passed |
| 8 - platform/docs/E2E | `0fda818` | `245c0bc` | passed |
| Post-Phase-8 - Windows locale CI repair | `b9b1cf1` | `7688de9` | passed |
| Post-review reliability closure | `ed6f043` | this report | passed |

## Verification evidence

The traceability tables below refer to these executed evidence sets.

| ID | Executed command | Result |
| --- | --- | --- |
| `V0` | `python -m pytest tests/test_zotero_stage1_pdf_resolution.py tests/test_main_dispatch_and_free_mode.py tests/test_outline_candidates.py tests/test_paper_identity.py tests/test_runtime_validation_bridge.py tests/test_runtime_source_intake.py -q --strict-markers` | `86 passed in 45.83s` |
| `V0-offline` | `python -m pytest tests/test_offline_guard.py -q --strict-markers` and `python -m pytest --collect-only -q --strict-markers` | `5 passed`; `696 tests collected` |
| `V1` | Phase 1 Zotero/FileIndex six-file suite, then the cumulative ten-file suite | `33 passed`; `114 passed in 35.84s` |
| `V2` | Phase 2 identity/Registry/audit suites and strict-offline Milestone A | `58 + 39 + 19 passed`; `749 passed, 22 deselected` |
| `V3` | Phase 3 Validation/sentence/projection suites and strict-offline gate | `129 + 17 passed`; `784 passed, 22 deselected` |
| `V4` | Phase 4 ReviewBatch/dependency lifecycle suites | `14 passed`; cumulative `87 passed` |
| `V5` | Phase 5 runner/reconcile/resume suite and Milestone B | `82 passed`; `816 passed, 22 deselected` |
| `V6` | Phase 6 Outline health/budget suite and all Outline tests | `34 passed`; `202 passed, 643 deselected` |
| `V7` | Phase 7 evidence/checkpoint suite and Milestone C preflight | `67 + 10 passed`; `830 passed, 22 deselected` |
| `V8-platform` | `python -m pytest -q tests/test_console_io.py tests/test_preprocess_platform_safety.py tests/test_quarantine_lifecycle.py tests/test_reconcile_schema_contracts.py tests/test_runtime_legacy_workspace.py tests/test_config_path_origin.py tests/test_documentation_contract.py --strict-markers` | `57 passed in 11.61s` |
| `V8-e2e` | `python -m pytest -q tests/test_synthetic_runtime_e2e.py --strict-markers` | `8 passed in 442.59s` |
| `V8-review-fix` | Summary/legacy/Registry/reconcile/runner regression suite | `101 passed in 55.68s` |
| `VF-required` | `python -m pytest -q --strict-markers` | `1082 passed, 22 skipped in 552.69s` |
| `VF-offline` | `python -m pytest -q --strict-markers -m "not live_api and not playwright and not heavy_ocr"` | `1082 passed, 22 deselected in 544.06s` |
| `VF-static` | `python -m pyright`; `python -m compileall -q main.py runtime services validation outline preprocess`; Phase 8 production Ruff `E9,F821,F841`; `git diff --check` | all passed; Pyright reported zero diagnostics |
| `V-CI-locale` | locale-strict Stage 2/report regressions and static checks | `15 passed`; historical local reviewer verdicts were `PASS` and `CLEAR`; final suite evidence is superseded by `V-post-review` |
| `V-post-review` | ReviewBatch, six-blocker, Stage 1 lineage, modified-module, required, strict-offline, Pyright, compileall, Ruff, and diff gates | `58 + 18 + 134 + 363 passed`; final `1082 passed, 22 skipped`; final `1082 passed, 22 deselected`; all static checks passed |

Historical phase-level command lines and artifact hashes are retained in
`docs/implementation/auto-generate-reliability-upgrade-progress.md`.

## MUST traceability

### Phase 0 - hotfix and offline baseline

| MUST requirement | Implementation symbol/surface | Automated test | Evidence | Status |
| --- | --- | --- | --- | --- |
| Isolated worktree/branch at the pinned base; import only the four approved hotfix groups | Git history `5becc4e..63aeba8`; `main.py`; `outline/candidates.py`; `services/paper_identity.py` | hotfix groups in `test_zotero_stage1_pdf_resolution.py`, `test_main_dispatch_and_free_mode.py`, `test_outline_candidates.py`, `test_paper_identity.py` | `V0` | passed |
| Preserve `library_path` priority, backup Reader metadata-only behavior, Outline prompt/schema repair, and DOI ampersands | `main.LiteratureReviewGenerator`; Outline candidate schema builders; `services.paper_identity.normalize_doi` | same four test groups | `V0` | passed |
| Windows Python 3.11 CI and strict markers | `.github/workflows/windows-tests.yml`; `pytest.ini` | collection and marker validation | `V0-offline`, `VF-required` | passed |
| Block external network during pytest while allowing loopback | `tests.offline_guard.install_offline_guard`; `tests/offline_sitecustomize/sitecustomize.py` | `tests/test_offline_guard.py` | `V0-offline`, `VF-offline` | passed |
| Require marker, explicit enable flag, and credentials for live tests; never fall back to default real config | pytest configuration and live-test guards | collection plus final optional selection | `V0-offline`, `VF-required` | passed |
| Propagate offline policy to subprocesses and block curl/wget/PowerShell bypasses | `tests.offline_guard._guarded_popen_init` | `tests/test_offline_guard.py` | `V0-offline` | passed |
| Treat only declared live API/Playwright/heavy OCR tests as optional | `pytest.ini`; `tests/conftest.py` | final suite skip accounting | `VF-required`, `VF-offline` | passed |

### Phase 1 - Zotero and FileIndex

| MUST requirement | Implementation symbol/surface | Automated test | Evidence | Status |
| --- | --- | --- | --- | --- |
| Parse multiline Zotero fields with a state machine and return `ZoteroParseResultV1` | `zotero_parser.ZoteroParseResultV1`; `parse_zotero_report_result` | `tests/test_zotero_parser.py`, `tests/test_retry_report_roundtrip.py` | `V1` | passed |
| Scope FileIndex instances by normalized Zotero root instead of a process singleton | root-keyed cache in `file_finder.py` | `tests/test_file_finder.py` | `V1` | passed |
| Preserve multiple candidates for duplicate basenames | FileIndex candidate map and resolver | `tests/test_file_finder.py`, `tests/test_zotero_stage1_pdf_resolution.py` | `V1` | passed |
| Keep index scans read-only and remove `.access_test` writes | FileIndex scan path | read-only-library cases in `tests/test_file_finder.py` | `V1` | passed |
| Cover multiline reports, separate libraries, duplicate names, read-only roots, and diagnostics | parser and source-intake fixtures | Phase 1 six-file suite | `V1` | passed |

### Phase 2 - identity, Registry, audit, and fingerprint

| MUST requirement | Implementation symbol/surface | Automated test | Evidence | Status |
| --- | --- | --- | --- | --- |
| Persist `SourceInventoryV1`, content fingerprints, and random-suffix job IDs | `services.source_inventory.SourceInventoryV1`; job workspace/fingerprint helpers | `tests/test_source_inventory.py`, `tests/test_job_workspace.py` | `V2` | passed |
| Run identity gate/quarantine before any Stage 1 provider call | `services.source_identity.inspect_text_identity`; runtime source intake gate | `tests/test_source_identity.py`, Stage 1 zero-call spies | `V2` | passed |
| Provide Registry revision, lock, CAS, typed failures, and `ArtifactDependencyRefV2` | `services.artifact_registry.ArtifactRegistry`; `ArtifactDependencyRefV2` | `tests/test_artifact_registry.py`, `tests/test_registry_transactions.py` | `V2` | passed |
| Persist immutable `AuditRecordV1`, readiness policy hash/snapshot, and job outcome foundation | `services.audit_record.AuditRecordV1`; `services.job_outcome.JobOutcomeV1` | `tests/test_audit_record.py`, `tests/test_job_outcome.py` | `V2` | passed |
| Read older Registry/workspace formats additively without auto-passing identity | Registry v1 reader; legacy readiness projections | identity and Registry compatibility tests | `V2`, `V8-platform` | passed |
| Prevent concurrent lost updates and expose lock timeout/revision conflict/corruption | Registry transaction implementation | process/thread/CAS cases in `tests/test_registry_transactions.py` | `V2`, `V8-review-fix` | passed |
| Change fingerprint when same-path content changes; quarantine ambiguous/mismatch with zero Stage 1 calls | content hash fingerprinting and identity verdicts | source identity and inventory regressions | `V2` | passed |

### Phase 3 - sentence and Validation truth

| MUST requirement | Implementation symbol/surface | Automated test | Evidence | Status |
| --- | --- | --- | --- | --- |
| Use one raw-offset sentence segmentation contract | `services.sentence_segmenter` | `tests/test_sentence_segmenter.py` | `V3` | passed |
| Preserve Chinese no-space text, citation-only/line citations, decimals, and abbreviations | sentence segmenter rules | targeted fixtures in `tests/test_sentence_segmenter.py` | `V3` | passed |
| Make `ValidationRunResultV1` the canonical structured result and verdict summary | `validation.run_result.ValidationRunResultV1` | `tests/test_validation_run_result.py` | `V3` | passed |
| Derive TXT, audit, runtime metadata, completion, and alignment reports from canonical JSON | `validator.py`; validation projection helpers | `tests/test_validation_projections.py`, `tests/test_runtime_validation_bridge.py` | `V3` | passed |
| Keep old Validation readers without treating old output as satisfying the new contract | compatibility adapter in Validation loaders | compatibility cases in `tests/test_validation_run_result.py` | `V3` | passed |

### Phase 4 - ReviewBatch and cross-job derivation

| MUST requirement | Implementation symbol/surface | Automated test | Evidence | Status |
| --- | --- | --- | --- | --- |
| Reuse `SummaryCatalog` and define selection/batch schemas | `services.review_batch.ReviewBatchSpecV1`; `SummarySelectionSpecV1` | `tests/test_review_batch.py` | `V4` | passed |
| Bind parent artifact ID/hash, ordered keys, classification hash/columns/filter/count/duplicate policy, and selection hash | ReviewBatch selection validation | `tests/test_review_batch.py` | `V4` | passed |
| Derive ABC=61, A=20, and AB=45 from one parent Stage 1 | `services.review_batch.derive_review_batch`; `AgentRuntimeBridge.derive_review_batch` | ReviewBatch and synthetic E2E fixtures | `V4`, `V8-e2e` | passed |
| Use `external_job` dependencies and make child Stage 1 calls fail immediately | `ArtifactDependencyRefV2.dependency_kind`; child runtime policy | Stage 1 fail-fast spies in `tests/test_review_batch.py` | `V4`, `V8-e2e` | passed |
| Refuse parent deletion while non-invalid child dependencies exist | `services.dependency_lifecycle` | `tests/test_dependency_lifecycle.py` | `V4` | passed |
| Force deletion only with `dependency_force_delete` audit and invalid/needs-review children | dependency lifecycle force path | `tests/test_dependency_lifecycle.py` | `V4` | passed |

### Phase 5 - runner, recovery, and pointer ownership

| MUST requirement | Implementation symbol/surface | Automated test | Evidence | Status |
| --- | --- | --- | --- | --- |
| Add one high-level runner on the existing bridge, without a second runtime | `runtime.runner.AgentRuntimeRunner`; `runtime.orchestrator.AgentRuntimeBridge` | `tests/test_runtime_runner.py` | `V5` | passed |
| Separate read-only status, repair-only reconcile, new-attempt resume, and new-job run | public `AgentRuntimeRunner` methods | runner/reconcile/attempt tests | `V5` | passed |
| Require file, Registry record, hash, schema, dependencies, and terminal record for completion | `RuntimeReconciler.validate_record`; stage terminal store | `tests/test_runtime_reconcile.py`, `tests/test_runtime_runner.py` | `V5`, `V8-review-fix` | passed |
| Resolve summary/spec/config relative paths from their owning files, never implicit CWD | runtime path-origin helpers | `tests/test_config_path_origin.py`, summary reuse path tests | `V5`, `V8-platform` | passed |
| Enforce latest-pointer job ownership | lifecycle pointer claim/finalize helpers | `tests/test_runtime_lifecycle_parity.py` | `V5` | passed |
| Drive Queue from `job_status`; keep `success` only as compatibility projection | `services.queue_service`; `JobOutcomeV1` projection | `tests/test_persistent_queue_service.py`, `tests/test_job_outcome.py` | `V5` | passed |
| Survive write/Registry, report/pointer, cancellation, and KeyboardInterrupt fault windows without history rewrite or repeated model calls | `AttemptStore`; runner fault injector; reconcile | runner/reconcile fault tests | `V5`, `V8-e2e` | passed |
| Fail closed on stale/missing Outline dependencies and audit explicit legacy fallback | runtime dependency resolver and audited compatibility path | runner/Outline/legacy tests | `V5`, `V8-review-fix` | passed |

### Phase 6 - Outline health and prompt budget

| MUST requirement | Implementation symbol/surface | Automated test | Evidence | Status |
| --- | --- | --- | --- | --- |
| Persist independent StageHealth with provider/schema/attempt/hash/degradation/fallback data | `outline.stage_health.OutlineStageHealthV1` | `tests/test_outline_stage_health.py` | `V6` | passed |
| Require current non-degraded health at the existing Outline v2 adoption gate | `outline.adoption.verify_adoption_prerequisites`; runtime resolver | `tests/test_outline_adoption_gate.py`, `tests/test_outline_runtime_alignment.py` | `V6` | passed |
| Prevent automatic production fallback adoption; audit manual adoption | Outline pipeline/adoption and `AuditRecordV1` | Outline adoption tests | `V6` | passed |
| Enforce context minus max output minus ten-percent safety margin | `outline.prompt_budget.PromptBudgetV1.input_budget_tokens` | `tests/test_outline_prompt_budget.py` | `V6` | passed |
| Partition by research stream and hierarchically merge rather than truncate evidence | budgeted synthesis in `outline.candidates`/pipeline | prompt-budget and artifact-loop tests | `V6` | passed |

### Phase 7 - evidence dependencies and edge checkpoints

| MUST requirement | Implementation symbol/surface | Automated test | Evidence | Status |
| --- | --- | --- | --- | --- |
| Bind paper artifacts to normalized text, chunks, page index, and evidence manifest | `services.evidence_manifest.EvidenceManifestV1`; paper artifact persistence | `tests/test_evidence_manifest.py`, `tests/test_paper_artifact_durability.py` | `V7` | passed |
| Project parent evidence artifact identities/hashes into child jobs | `services.review_batch` evidence dependency projection | `tests/test_review_batch.py` | `V7` | passed |
| Use English concepts/keywords only to improve bilingual recall; ground conclusions in original evidence | `validation.evidence_resolver`; Stage 1 query builder | alignment and evidence resolver tests | `V7`, `V8-e2e` | passed |
| Persist checkpoints by claim-unit x paper | `validation.edge_checkpoint.ValidationEdgeCheckpointStore` | `tests/test_validation_edge_checkpoint.py` | `V7` | passed |
| Resume serial interruption after durable edge 20/43 with exactly 23 remaining | edge checkpoint replay | 43-edge interruption fixture | `V7` | passed |
| In parallel, execute only edges without durable checkpoints and do not repeat completed work | edge/adjudication checkpoint stores | parallel replay fixtures | `V7` | passed |

### Phase 8 - platform, compatibility, documentation, and synthetic E2E

| MUST requirement | Implementation symbol/surface | Automated test | Evidence | Status |
| --- | --- | --- | --- | --- |
| Replace naked legacy-summary allowance with actor/reason/scope/input-hash audit | legacy projection in `main.LiteratureReviewGenerator`; `AuditRecordV1` | `tests/test_summary_reuse.py` | `V8-review-fix` | passed |
| Keep legacy source/summary/manifest quarantined until audit registration; abort Stage 1 on audit failure | `_materialize_effective_summaries`; `_assert_registered_summary_is_ready` | audit failure/retry and Stage 1 zero-call regressions | `V8-review-fix` | passed |
| Preflight MinerU and open a job-level circuit on 401/403 | `preprocess.provider_circuit.ProviderCircuitBreaker`; preprocess service | `tests/test_preprocess_platform_safety.py` | `V8-platform` | passed |
| Run Docling/OCR in timeout-bounded subprocesses; metadata-only never invokes heavy OCR | `preprocess.docling_worker`; `preprocess.ocr_worker`; preprocess service | `tests/test_preprocess_platform_safety.py` | `V8-platform` | passed |
| Configure UTF-8 stdout/stderr and ASCII-safe JSON progress on Windows | `services.console_io.configure_utf8_stdio`; `write_ascii_json_line` | `tests/test_console_io.py` | `V8-platform`, `V8-e2e` | passed |
| Keep Chinese local-time labels safe under English Windows CPython 3.11 locales | `main._format_chinese_datetime`; Stage 2 DOCX and report-header callers | locale-strict cases in `tests/test_review_draft_durability.py` | `V-CI-locale` | passed |
| Synchronize truth sources, feature matrix, compatibility, schema versions, and bilingual docs | `docs/en`, `docs/zh-CN`, README files | `tests/test_documentation_contract.py` | `V8-platform` | passed |
| Keep old workspace status/reconcile byte-read-only, including corrupt outcomes; require explicit audited migration | `RuntimeReconciler.legacy_read_only_result`; `AgentRuntimeRunner.migrate_legacy` | `tests/test_runtime_legacy_workspace.py` | `V8-platform`, `V8-review-fix` | passed |
| Validate Registry top-level version/owner/revision and reject malformed/future/foreign registries | `ArtifactRegistry._read_registry_unlocked` | `tests/test_registry_transactions.py`, `tests/test_reconcile_schema_contracts.py` | `V8-platform`, `V8-review-fix` | passed |
| Launch synthetic E2E through public CLI/runtime with fake providers and no network | runtime CLI, runner, `tests.synthetic_runtime_fakes` | `tests/test_synthetic_runtime_e2e.py` | `V8-e2e`, `VF-offline` | passed |
| Verify parent Stage 1=61, child Stage 1=0, ABC/A/AB counts, common parent hash, and PDF quarantine | ReviewBatch/runtime/source quarantine chain | parent/derived/quarantine E2E cases | `V8-e2e` | passed |
| Verify Chinese sentence/evidence behavior and complete Outline/Review/Citation/DOCX/Validation chain | canonical pipeline and validators | full-chain E2E cases | `V8-e2e` | passed |
| Keep Validation JSON and projections consistent | `ValidationRunResultV1` projection chain | E2E projection assertions | `V8-e2e` | passed |
| Reconcile post-report crashes without repeated checkpoints/model calls | runner/reconciler/attempt store | crash/reconcile E2E case | `V8-e2e` | passed |
| Map clean/findings/needs-review/cancelled, latest pointer, attempts, recursive hashes, Chinese paths, and console output | `JobOutcomeV1`; lifecycle; Registry recursion; console helpers | outcome matrix/public lifecycle E2E cases | `V8-e2e` | passed |

## Schemas and compatibility adapters

| Contract | Version/role | Compatibility behavior |
| --- | --- | --- |
| `ZoteroParseResultV1` | parser result v1 | `parse_zotero_report` remains a list projection |
| `SourceInventoryV1` and source identity result | intake/identity v1 | old workspaces remain readable but cannot auto-pass identity |
| Artifact Registry | v2 with `ArtifactDependencyRefV2` | valid v1 reads remain supported; explicit writes upgrade; malformed/future/foreign headers fail |
| `AuditRecordV1` | immutable audit v1 | shared by identity, legacy reuse/migration, manual adoption, deletion, and quarantine actions |
| `JobOutcomeV1`, `AttemptV1`, terminal stage record | runtime durability v1 | Queue `success` is a compatibility projection; `job_status` is canonical |
| `ValidationRunResultV1` | Validation truth v1 | TXT/manual/completion/alignment are projections; legacy reports do not satisfy v1 |
| `SummarySelectionSpecV1`, `ReviewBatchSpecV1`, derivation result | batch derivation v1 | children reuse parent canonical summaries/evidence through external dependencies |
| `OutlineStageHealthV1`, `PromptBudgetV1` | additive Outline sidecars | Outline v2 schema remains canonical and unchanged |
| `EvidenceManifestV1`, edge/adjudication checkpoints | evidence/recovery v1 | additive hash-bearing dependencies and replay checkpoints |
| Summary source manifest | v2 | records contributing sources separately from rejected audit scope |
| Legacy workspace projection | `legacy_unverified` | status/reconcile are read-only; `migrate-legacy` is explicit, audited, fail-closed, and non-canonical |
| Review draft/citation manifests | existing canonical v2/v3 contracts | legacy forms remain explicit compatibility projections only |

## Change surface

The validated range contains 137 files, 24,654 insertions, and 1,881 deletions.
The complete machine-readable manifest is reproducible with:

```text
git diff --name-status 5becc4e50e234244162dca980062fe68e4e0e1e9..b9b1cf1fa572a31201e395855ee623752cc708af
```

| Area | Files | Principal changes |
| --- | ---: | --- |
| Repository root | 11 | entrypoints, schemas/config, README, pytest configuration |
| `.github` | 1 | Windows Python 3.11 workflow |
| `docs` | 8 | bilingual truth sources, compatibility, feature matrix, implementation ledger and report |
| `gui` | 1 | runtime/config compatibility typing and behavior |
| `outline` | 8 | health sidecar, prompt budget, adoption/resolution gates |
| `preprocess` | 4 | provider circuit and bounded Docling/OCR workers |
| `runtime` | 10 | runner, attempts, terminals, reconcile, lifecycle, CLI |
| `services` | 22 | Registry/audit/outcome/identity/batch/evidence/quarantine/console contracts |
| `validation` | 8 | canonical result, evidence resolution, edge/adjudication recovery |
| `tools` | 1 | preprocessing audit compatibility |
| `tests` | 63 | unit, integration, concurrency, platform, documentation, and public synthetic E2E |

## Optional skips and validation boundaries

- `22` Playwright tests were skipped because Playwright execution was not
  explicitly enabled. A focused optional selection reported the same reason for
  all 22 skips.
- Live-provider and heavy-OCR tests remain opt-in and credential/tool gated.
  They do not replace the completed offline acceptance gates.
- External network access was disabled during the strict-offline gate; loopback
  remained available for local subprocess/runtime tests.
- Targeted correctness Ruff checks are clean. Repository-wide Ruff still has
  historical style findings outside the changed correctness surface; this was
  not an approved final gate and was not mass-auto-fixed.

## Review closure

The first independent code review found two high-severity fail-open paths:

1. Explicit legacy-summary audit failure could leave ready canonical artifacts
   and later bypass the audit.
2. A corrupt job outcome in a recognizable legacy workspace could bypass the
   read-only guard and create Registry state during reconcile.

Both were fixed with quarantine-before-audit promotion, Stage 1 fail-fast
behavior, and corrupt-outcome legacy read-only projection. The post-fix suite
reported `101 passed`; local reviewer verdicts were `PASS` and `CLEAR`. These
were engineering review results, not a formal GitHub approval.

The first Windows Python 3.11 runs on PR head `7d98e54` then exposed a separate
locale defect: ten Stage 2 durability tests returned before their first section
call because English Windows could not encode Chinese literals inside a
`strftime` format string. Code checkpoint `b9b1cf1` now renders the identical
Chinese local-time labels from numeric datetime fields and covers both affected
paths with a locale-strict proxy. That checkpoint's local reviews returned
`PASS` and `CLEAR`; its test counts are superseded by the final post-review
gates above.

The final audit then closed six release blockers: title plus author/year source
identity, durable `SystemExit` terminal persistence and rethrow, Validation
disposition recovery, canonical Validation read-back with job/attempt/hash
binding, fail-closed READY dependency verification, and explicit citation-free
handling for zero claims. Stage 1 READY summaries now depend on the registered
`source_bundle`, which transitively depends on source PDFs.

ReviewBatch now reserves a monotonic `projection_generation` before child or
manifest writes, serializes writers with derivation and coordinator leases, and
uses the fully validated Registry manifest with the unique maximum generation
as its durable head. The mutable projection is repaired only from that head;
projection receipts record `projected` or `superseded` plus the observed head
identity and hash. Tests cover manifest, Registry, projection, receipt, child,
terminal, and resume crash windows without using mtime ordering. Independent
adversarial review found no remaining crash/order blocker.

## Remaining risks

- Windows Python 3.11 GitHub CI must pass on the final pushed documentation head before the
  single draft PR is marked ready.
- Browser, live-provider, and heavy-OCR smoke tests require their explicit
  external prerequisites and remain optional.
- No new runtime dependency was added, no second runtime/truth source was
  created, and no required implementation remains as a TODO or stub.

# auto-generate reliability upgrade progress ledger

This ledger is the durable execution record for the single-branch reliability
upgrade. It is append-oriented: completed phase evidence is not rewritten except
to add the immutable checkpoint commit that could not be known from inside the
code commit itself.

## Execution identity

- Baseline: `origin/main@5becc4e50e234244162dca980062fe68e4e0e1e9`
- Branch: `codex/auto-generate-reliability-upgrade`
- Worktree: `D:\auto-generate-reliability-upgrade`
- Target: `origin/main`
- Git writer: leader only
- Last-known-good checkpoint: `99a0ce7` (Phase 4)

## Phase status

| Phase | Status | Code checkpoint | Evidence checkpoint |
| --- | --- | --- | --- |
| 0 - hotfix and offline baseline | completed | `63aeba8` | current ledger commit |
| 1 - Zotero and FileIndex | completed | `24f318d` | current ledger commit |
| 2 - identity, registry, audit, fingerprint | completed | `0d0f879` | current ledger commit |
| 3 - sentence and validation truth | completed | `89a3c9a` | current ledger commit |
| 4 - ReviewBatch derivation | completed | `99a0ce7` | current ledger commit |
| 5 - runtime runner and recovery | pending | - | - |
| 6 - outline health and budget | pending | - | - |
| 7 - evidence and edge checkpoints | pending | - | - |
| 8 - platform, compatibility, docs, E2E | pending | - | - |

## Phase 0 - hotfix and offline baseline

### Scope and provenance

- Pre-phase commit: `5becc4e50e234244162dca980062fe68e4e0e1e9`
- Imported by file-scoped hunks from the user's original working tree:
  `main.py`, `outline/candidates.py`, `services/paper_identity.py`, and the four
  corresponding test groups.
- Imported behavior: runtime `library_path` precedence; backup Reader
  metadata-only branch; Outline prompt/schema projection; DOI ampersand support.
- Explicitly excluded: `config_validator.py`, `services/model_capabilities.py`,
  their tests, local commits `676ee5a` and `8781b7c`, and every unrelated dirty
  working-tree change.

### Changed files

- `.github/workflows/windows-tests.yml`
- `pytest.ini`
- `tests/conftest.py`
- `tests/offline_guard.py`
- `tests/offline_sitecustomize/sitecustomize.py`
- `tests/test_offline_guard.py`
- `tests/test_gui_playwright.py`
- Imported hotfix implementation and test files listed above.

### Verification evidence

| Command | Exit | Evidence |
| --- | ---: | --- |
| `python -m pytest tests/test_zotero_stage1_pdf_resolution.py tests/test_main_dispatch_and_free_mode.py tests/test_outline_candidates.py tests/test_paper_identity.py tests/test_runtime_validation_bridge.py tests/test_runtime_source_intake.py -q --strict-markers` | 0 | `86 passed in 45.83s` after final offline-guard hardening |
| `python -m pytest tests/test_offline_guard.py -q --strict-markers` | 0 | `5 passed in 1.48s` |
| `python -m pytest --collect-only -q --strict-markers` | 0 | `696 tests collected in 14.01s` |
| `python -m compileall -q main.py runtime services validation outline preprocess tests/offline_guard.py` | 0 | no diagnostics |
| `git diff --check` | 0 | no whitespace errors; Git reported only expected LF-to-CRLF notices |
| `python -m pyright` | 1 | baseline debt: 105 errors and 2 warnings; final gate remains open and must be closed before delivery |

### Artifact hashes

- `tests/offline_guard.py`: `3fa254a3a15ed81d55e7eff16087415135b55d8c1d4b8ab030239f7a0e38f3dc`
- `tests/conftest.py`: `9a047ee74738765b3e2deae4998dc5818bf34f7a960a0d5ede8231cb0434b352`
- `.github/workflows/windows-tests.yml`: `61d7cbf56b58b25e23d8abb1f2686373f84256128078b4104c417b0516fb2251`

### Remaining risks

- The repository-wide pyright baseline is red. This is not caused solely by the
  Phase 0 changes, but the approved final gate requires zero errors, so it is a
  tracked delivery obligation rather than an accepted limitation.
- Native non-Python executables cannot inherit Python socket monkeypatches.
  Strict-offline CI therefore excludes explicitly marked Playwright/heavy OCR
  tests and blocks common shell network clients at the subprocess boundary.

## Phase 1 - Zotero parsing and root-scoped FileIndex

### Scope and provenance

- Pre-phase commit: `3ebd8b2`
- Code checkpoint: `24f318d`
- Replaced the lossy flat Zotero report parser with the versioned
  `ZoteroParseResultV1` contract while retaining the old `List[PaperInfo]`
  projection for compatibility.
- Replaced the process-global PDF index with immutable, root-scoped instances;
  duplicate basenames remain explicit candidates and indexing performs no
  writes to the Zotero library.
- Migrated first-party source-intake and Stage 1 preparation paths to the
  structured parser and `PdfMatchResultV1`, so ambiguous candidates fail closed
  before provider execution.

### Changed files

- `zotero_parser.py`
- `file_finder.py`
- `runtime/source_intake.py`
- `main.py`
- `models.py`
- `report_generator.py`
- `tests/test_zotero_parser.py`
- `tests/test_file_finder.py`
- `tests/test_runtime_source_intake.py`
- `tests/test_zotero_stage1_pdf_resolution.py`

### Verification evidence

| Command | Exit | Evidence |
| --- | ---: | --- |
| `python -m pytest tests/test_zotero_parser.py tests/test_retry_report_roundtrip.py tests/test_file_finder.py tests/test_runtime_source_intake.py tests/test_zotero_stage1_pdf_resolution.py tests/test_main_flow.py -q --strict-markers` | 0 | `33 passed` |
| `python -m pytest tests/test_zotero_parser.py tests/test_retry_report_roundtrip.py tests/test_file_finder.py tests/test_runtime_source_intake.py tests/test_zotero_stage1_pdf_resolution.py tests/test_main_flow.py tests/test_main_dispatch_and_free_mode.py tests/test_outline_candidates.py tests/test_paper_identity.py tests/test_runtime_validation_bridge.py -q --strict-markers` | 0 | `114 passed in 35.84s` |
| `python -m pyright zotero_parser.py file_finder.py runtime/source_intake.py models.py` | 0 | `0 errors, 0 warnings, 0 informations` |
| `python -m compileall -q main.py runtime/source_intake.py zotero_parser.py file_finder.py models.py report_generator.py` | 0 | no diagnostics |
| `git diff --check` | 0 | no whitespace errors; Git reported only expected LF-to-CRLF notices |

### Artifact hashes

- `zotero_parser.py`: `e12171093da70a14c94d456caaf5ac3575481c04a3fe1713f03bf734089e027b`
- `file_finder.py`: `4c78fc75569e8796ba4f274fdc7c7d35fb1261bd9d62e51ec0780ff31e5a17d8`
- `runtime/source_intake.py`: `f25b773b928664ab006d816d74081d57e097f69e5a17b7432356ea76c1b33948`

### Remaining risks

- Legacy parser and `find_pdf()` wrappers remain intentionally available for
  compatibility, but all first-party ingestion paths now use structured,
  fail-closed results.
- A failed Zotero parse currently terminates source intake with a typed
  diagnostic encoded in the exception. Phase 2 will persist the same failure
  as inventory, quarantine, audit, and job-outcome artifacts.
- Repository-wide pyright debt remains part of the final delivery gate; the
  four Phase 1 contract modules themselves are type-clean.

## Phase 2 - source identity, transactional Registry, audit, and fingerprint

### Scope and provenance

- Pre-phase commit: `c5f25d2`
- Code checkpoint: `0d0f879`
- Added `SourceInventoryV1` as the content-hashed source truth used by runtime
  fingerprints. A file changed at the same path now produces a different
  fingerprint and a distinct Stage 1 workspace.
- Added the DOI/title-author-year identity gate and persisted
  `SourceIdentityResultV1`. Ambiguous or mismatched Zotero sources are
  quarantined before any Stage 1 provider boundary is reached.
- Replaced Artifact Registry writes with revisioned, cross-process locked,
  atomic read-modify-write transactions and the additive
  `ArtifactDependencyRefV2` contract. Missing ready files, corruption, lock
  timeout, revision conflict, and artifact identity conflict fail closed with
  typed errors.
- Added immutable `AuditRecordV1`, `JobOutcomeV1`, readiness policy snapshot
  hashing, append-only attempt primitives, random-suffixed workspace IDs, and
  fail-closed readers for legacy unverified outcomes.

### Changed files

- Source contracts: `services/source_inventory.py`,
  `services/source_identity.py`, `runtime/source_intake.py`.
- Runtime/outcome integration: `runtime/lifecycle.py`,
  `runtime/orchestrator.py`, `services/job_runner.py`,
  `services/job_workspace.py`, `services/job_outcome.py`.
- Transaction/audit contracts: `services/artifact_registry.py`,
  `services/audit_record.py`.
- Stage 1 compatibility projections: `main.py`, `models.py`,
  `report_generator.py`.
- Contract, concurrency, integration, and compatibility tests under `tests/`.

### Verification evidence

| Command | Exit | Evidence |
| --- | ---: | --- |
| `python -m pytest tests/test_source_identity.py tests/test_source_inventory.py tests/test_job_outcome.py tests/test_audit_record.py tests/test_job_workspace.py tests/test_artifact_registry.py tests/test_registry_transactions.py -q --strict-markers` | 0 | `58 passed in 8.38s` |
| `python -m pytest tests/test_job_runner.py tests/test_runtime_lifecycle_parity.py tests/test_runtime_orchestrator.py tests/test_runtime_source_intake.py tests/test_zotero_stage1_pdf_resolution.py tests/test_outline_adoption_gate.py -q --strict-markers` | 0 | `39 passed in 17.62s` |
| `python -m pytest tests/test_report_generator.py tests/test_report_generator_fields.py tests/test_report_generator_excel_layout.py tests/test_stage1_resume_retry.py tests/test_runtime_stage1_bridge.py tests/test_zotero_parser.py -q --strict-markers` | 0 | `19 passed in 11.12s` |
| `python -m pytest -q --strict-markers -m "not live_api and not playwright and not heavy_ocr" -ra` | 0 | Milestone A: `749 passed, 22 deselected in 45.73s`; zero skips |
| `python -m pyright models.py report_generator.py runtime/lifecycle.py runtime/orchestrator.py runtime/source_intake.py services/artifact_registry.py services/audit_record.py services/job_outcome.py services/job_runner.py services/job_workspace.py services/source_identity.py services/source_inventory.py` | 0 | `0 errors, 0 warnings, 0 informations` |
| `python -m compileall -q main.py models.py report_generator.py runtime services` | 0 | no diagnostics |
| `git diff --check` | 0 | no whitespace errors; Git reported only expected LF-to-CRLF notices |

### Artifact hashes

- `services/source_inventory.py`: `25feeb9307eb25fc073f157b9d3ebc27d20e8ae6998ba43986e662caf4fa2a14`
- `services/source_identity.py`: `2c0155810550d7547cb21c80782ac6af0102441aeeb42f856c0af66663db27bc`
- `services/artifact_registry.py`: `68159e29c9f628350e028d67499964dc780a6f8b254ed97b6b745f8d554e0159`
- `services/audit_record.py`: `87271e5e5ba9a0d7d1805452265923a00bd5f0532b95e4d82987544115b93e56`
- `services/job_outcome.py`: `1173431ee6343002dcc2a492162f2d09b8a3e308f17537963c4eb0346be3d4f4`

### Remaining risks

- Attempt artifact persistence and stale-running recovery are intentionally
  integrated in Phase 5; Phase 2 establishes and tests the immutable attempt
  state contract used there.
- Unified audits are now immutable and registry-compatible, but the concrete
  legacy reuse, Outline adoption, dependency force-delete, and quarantine
  release actions are wired in their owning later phases.
- The repository-wide pyright baseline remains open for the final gate. All
  Phase 2 contract and coordination modules listed above are type-clean.

## Phase 3 - sentence segmentation and canonical Validation truth

### Scope and provenance

- Pre-phase commit: `18c06c6`
- Code checkpoint: `89a3c9a`
- Replaced three divergent sentence splitters with one offset-preserving
  segmenter. Every sentence retains `span_start`, `span_end`, `raw_text`, and
  `display_text`, with raw spans anchored to the original block text.
- Added `ValidationRunResultV1` as the sole structured truth source, including
  the formal claim verdict enum, execution status, run disposition, exact
  verdict counts, and fail-closed legacy reader.
- Changed TXT, manual-review JSON, completion JSON, alignment audit, and runtime
  metadata into projections of the canonical JSON. Unknown adjudicator states
  now become `needs_review`; missing evidence remains `evidence_gap` and cannot
  silently become `unsupported`.
- Runtime registration now records the canonical result first and makes each
  projection depend on its artifact ID and content hash.

### Changed files

- Sentence contract and consumers: `services/sentence_segmenter.py`,
  `services/review_draft.py`, `services/citation_manifest.py`,
  `validation/review_validator.py`.
- Validation truth and projections: `validation/run_result.py`, `validator.py`,
  `validation/claim_alignment_audit.py`, `validation/llm_adjudicator.py`,
  `validation/__init__.py`, `runtime/orchestrator.py`.
- Contract, projection-parity, sentence-offset, compatibility, and runtime
  registration tests under `tests/`.

### Verification evidence

| Command | Exit | Evidence |
| --- | ---: | --- |
| `python -m pytest -q tests/test_validation_run_result.py tests/test_sentence_segmenter.py tests/test_runtime_validation_bridge.py tests/test_claim_paper_alignment_validation.py tests/test_review_draft_durability.py tests/test_structured_citations.py tests/test_week3_validation.py tests/test_week4_repair_integration.py tests/test_runtime_orchestrator.py tests/test_runtime_subagent_contract.py tests/test_runtime_validation_adapter_contract.py tests/test_review_validation_replay.py` | 0 | `129 passed in 13.42s` after compatibility fixes |
| `python -m pytest -q tests/test_validation_projections.py tests/test_citation_v3_cutover.py tests/test_validator_adjudication_flow.py tests/test_runtime_validation_bridge.py` | 0 | `17 passed in 3.74s` |
| `python -m pytest -q --strict-markers -m "not live_api and not playwright and not heavy_ocr"` | 0 | `784 passed, 22 deselected in 59.00s`; zero skips |
| `python -m pyright validation/run_result.py tests/test_validation_run_result.py tests/test_validation_projections.py services/sentence_segmenter.py services/review_draft.py services/citation_manifest.py validation/review_validator.py validation/claim_alignment_audit.py runtime/orchestrator.py validator.py` | 0 | `0 errors, 0 warnings, 0 informations` |
| `python -m compileall -q validator.py runtime/orchestrator.py validation services/sentence_segmenter.py services/review_draft.py services/citation_manifest.py` | 0 | no diagnostics |
| `git diff --check` | 0 | no whitespace errors; Git reported only expected LF-to-CRLF notices |

### Artifact hashes

- `services/sentence_segmenter.py`: `efab84fd9cf5829d6edd3dd225343067c86e4cb31b681f370faa5f72e7cea76b`
- `validation/run_result.py`: `a598abb84e548ba030cf98b0e1c03a70daa3867fdba60f16fef40f895766d2d2`
- `validator.py`: `6ef89c9f94518030f9fb108dbbe7d17d7261a6a05cdde262960c934c6f03d6ca`
- `runtime/orchestrator.py`: `c85cd3cd0c7ceee4f869d042ae297fbf7261bc1f434cd484933857dbef978755`

### Remaining risks

- Job-level readiness/Queue mapping and append-only attempt recovery remain in
  Phase 5; Phase 3 supplies the canonical validation inputs for those rules.
- Existing repair code intentionally continues to consume the compatibility
  `ReviewValidationReport`; it no longer controls persisted validation truth.
- Repository-wide pyright debt remains a final-gate obligation. Every Phase 3
  implementation and test module listed in the targeted command is type-clean.

## Phase 4 - ReviewBatch derivation and cross-job lifecycle

### Scope and provenance

- Pre-phase commit: `6a3cb4d`
- Code checkpoint: `99a0ce7`
- Added `SummarySelectionSpecV1` and `ReviewBatchSpecV1`, including verified
  parent Registry identity, parent content hash, ordered canonical paper keys,
  classification-file hash, column/filter policy, expected count, duplicate
  policy, and a stable selection hash.
- Reused `SummaryCatalog.resolve_for_paper()` for every selection. Missing,
  ambiguous, duplicate, count-mismatched, registry-mismatched, or hash-stale
  inputs fail closed; no second paper matcher was introduced.
- Added the runtime `stage1_derive` local-only path. Child summaries depend on
  the canonical external `(job_id, artifact_id, content_hash)` and expose a
  durable `stage1_model_calls=0` contract.
- Added cross-workspace reverse-dependency scanning, locked Registry status and
  dependency-edge mutation, local materialization, default deletion refusal,
  and audited force breaking that invalidates child artifacts and makes an
  existing child outcome non-ready and attention-required.
- Routed the existing cleanup command through the workspace dependency guard
  before `rmtree`.

### Changed files

- Batch contracts and derivation: `services/review_batch.py`.
- Cross-job lifecycle: `services/dependency_lifecycle.py`,
  `services/artifact_registry.py`, `main.py`.
- Runtime exposure and execution policy: `runtime/orchestrator.py`,
  `runtime/subagent_policy.py`.
- Integration and lifecycle tests: `tests/test_review_batch.py`,
  `tests/test_dependency_lifecycle.py`.

### Verification evidence

| Command | Exit | Evidence |
| --- | ---: | --- |
| `python -m pytest -q tests/test_review_batch.py tests/test_dependency_lifecycle.py tests/test_runtime_validation_bridge.py tests/test_runtime_subagent_contract.py` | 0 | `14 passed in 5.59s` |
| `python -m pytest -q --strict-markers tests/test_review_batch.py tests/test_dependency_lifecycle.py tests/test_summary_reuse.py tests/test_artifact_registry.py tests/test_registry_transactions.py tests/test_runtime_stage1_bridge.py tests/test_runtime_orchestrator.py tests/test_runtime_subagent_contract.py tests/test_job_runner.py tests/test_main_dispatch_and_free_mode.py` | 0 | `87 passed in 13.78s` |
| `python -m pyright services/review_batch.py services/dependency_lifecycle.py services/artifact_registry.py runtime/orchestrator.py runtime/subagent_policy.py tests/test_review_batch.py tests/test_dependency_lifecycle.py` | 0 | `0 errors, 0 warnings, 0 informations` |
| `python -m compileall -q main.py runtime services` | 0 | no diagnostics |
| `git diff --check` | 0 | no whitespace errors; Git reported only expected LF-to-CRLF notices |

### Artifact hashes

- `services/review_batch.py`: `3b67ab73cc74e4e9798c8c9c90871b44d658a4f69079067e5b70e93ff79c4a91`
- `services/dependency_lifecycle.py`: `404230023bab0683da9439f8005197e46b7a8949e76c0e23407fcb4e3e32e06e`
- `services/artifact_registry.py`: `d51092c29d4edf467afa9b3b49568871b775936890e7a411d910a8a283456266`
- `runtime/orchestrator.py`: `9821ef07c0a305153ce7c1a899ba9d197696d959e02eb119b6b29f521ffd7636`

### Remaining risks

- Phase 5 must make batch specs runnable through the high-level runner and CLI,
  with append-only attempts and reconcile/resume semantics.
- The cleanup CLI currently exposes the safe default refusal. Audited force
  breaking is available through the lifecycle service; user-facing force
  option documentation is completed with the Phase 8 compatibility/docs pass.
- Job-outcome invalidation is applied when a canonical outcome already exists;
  Phase 5 makes outcome creation universal for runner-managed children.

## Phase 5 - AI-native runner, append-only recovery, and strict reconcile

### Scope and provenance

- Pre-phase commit: `f348716`
- Code checkpoint: `2597c5d`
- Added the single high-level `AgentRuntimeRunner` on top of
  `AgentRuntimeBridge`, plus public `run`, `resume`, `status`, and `reconcile`
  commands. Generation remains an explicit host/subagent callback; lifecycle,
  persistence, validation, and recovery stay local.
- Persisted immutable attempt snapshots and terminal stage records. A stale
  running attempt is appended as `interrupted` before a new pending/running
  attempt is created; prior snapshots are never rewritten.
- Added provider-free strict reconciliation requiring a ready Registry record,
  matching file hash, schema validation, resolvable recursive dependencies,
  and a registered terminal stage record before a stage is considered complete.
- Added latest-pointer claim locking and finalize ownership checks, Queue
  lifecycle mapping from `job_status`, CWD-independent spec/config/summary path
  origins, and fail-closed Outline Registry resolution.
- Integrated verified ReviewBatch derivation into Runner with zero Stage 1
  provider calls, and repaired the review/citation dependency graph so resume
  can prove durable completion without regenerating content.

### Changed files

- Runner and recovery: `runtime/runner.py`, `runtime/cli.py`,
  `runtime/attempt_store.py`, `runtime/stage_terminal.py`,
  `runtime/reconcile.py`, `runtime/lifecycle.py`.
- Bridge and durable dependencies: `runtime/orchestrator.py`, `main.py`,
  `services/job_runner.py`, `services/job_workspace.py`,
  `services/queue_service.py`.
- Path and outline contracts: `runtime/job_spec.py`, `config_loader.py`,
  `services/summary_reuse.py`, `outline/runtime_resolver.py`.
- Runner, fault-injection, reconcile, Queue, path-origin, pointer, and batch
  integration tests under `tests/`.

### Verification evidence

| Command | Exit | Evidence |
| --- | ---: | --- |
| `python -m pytest -q tests/test_runtime_runner.py tests/test_runtime_attempt_store.py tests/test_runtime_reconcile.py tests/test_review_batch.py tests/test_runtime_lifecycle_parity.py tests/test_job_workspace.py tests/test_runtime_job_spec_bridge.py tests/test_persistent_queue_service.py tests/test_summary_reuse.py tests/test_outline_runtime_alignment.py tests/test_runtime_validation_bridge.py tests/test_runtime_review_chain.py tests/test_job_runner.py` | 0 | `82 passed in 21.33s` |
| `python -m pytest -q --strict-markers -m "not live_api and not playwright and not heavy_ocr"` | 0 | Milestone B: `816 passed, 22 deselected in 63.43s`; zero skips |
| `python -m pyright runtime/runner.py runtime/cli.py runtime/attempt_store.py runtime/stage_terminal.py runtime/reconcile.py runtime/lifecycle.py runtime/job_spec.py runtime/orchestrator.py services/job_runner.py services/job_workspace.py services/queue_service.py services/summary_reuse.py outline/runtime_resolver.py config_loader.py` | 0 | `0 errors, 0 warnings, 0 informations` |
| `git diff --check` | 0 | no whitespace errors; Git reported only expected LF-to-CRLF notices |

### Artifact hashes

- Full strict-offline pytest log: `87159c4678b59ab550ef2be62e25f15764f22311652ff6c1f26298fd5c5b5a44`

### Remaining risks

- Outline provider/schema health and prompt-budget enforcement are Phase 6.
- Evidence preprocessing dependencies and claim-unit by paper checkpoints are
  Phase 7.
- Platform preflight/circuit breakers, migration/documentation closure, and
  public synthetic E2E remain Phase 8.
- Repository-wide pyright debt remains a final-gate obligation; every changed
  Phase 5 runtime/service module is type-clean.

## Phase 6 - Outline provider health and prompt-budget enforcement

### Scope and provenance

- Pre-phase commit: `5619a56`
- Code checkpoint: `d7243c3`
- Added the independent `outline_stage_health_v1.json` sidecar without changing
  the schema/version of candidates, critiques, arbitration, final outline, or
  coverage-audit artifacts.
- Recorded logical provider calls, schema validity, input/output hashes, prompt
  budgets, fallback provenance, and degradation reasons across candidate
  generation, research-stream synthesis, critiques, and arbitration.
- Made registered, current, non-degraded stage health a prerequisite for both
  explicit adoption surfaces and downstream v2 resolution. Production
  deterministic fallback/top-up is not adoptable; explicit test/dev doubles
  remain testable without being confused with production fallback.
- Added immutable `outline_manual_adoption` audit records referencing final,
  audit, health, and adopted artifact identities/hashes.
- Enforced the exact input-budget formula `context - max output - ceil(10% of
  context)`, removed the `research_streams[:80]` truncation, and added complete
  paper-packet research-stream synthesis plus hierarchical merge. Atomic inputs
  that cannot fit fail closed rather than losing evidence.

### Changed files

- Health and budget contracts: `outline/stage_health.py`,
  `outline/prompt_budget.py`, `outline/v2_config.py`.
- Pipeline, prompt, adoption, and consumption gates: `outline/pipeline.py`,
  `outline/candidates.py`, `outline/adoption.py`,
  `outline/runtime_resolver.py`, `main.py`.
- Health, budget, adoption, resolver, and regression tests under `tests/`.

### Verification evidence

| Command | Exit | Evidence |
| --- | ---: | --- |
| `python -m pytest -q tests/test_outline_stage_health.py tests/test_outline_prompt_budget.py tests/test_outline_adoption_gate.py tests/test_outline_runtime_alignment.py tests/test_outline_artifact_loop.py` | 0 | `34 passed in 4.46s` |
| `python -m pytest -q tests -k "outline" --strict-markers` | 0 | `202 passed, 643 deselected in 12.46s` |
| `python -m pytest -q --strict-markers -m "not live_api and not playwright and not heavy_ocr"` | 0 | `823 passed, 22 deselected in 61.83s`; zero skips |
| `python -m pyright outline/stage_health.py outline/prompt_budget.py outline/candidates.py outline/pipeline.py outline/adoption.py outline/runtime_resolver.py` | 0 | `0 errors, 0 warnings, 0 informations` |
| `python -m compileall -q outline main.py` | 0 | no diagnostics |
| `git diff --check` | 0 | no whitespace errors; Git reported only expected LF-to-CRLF notices |

### Artifact hashes

- `outline/stage_health.py`: `236004a07d45d2d16f2a90998ee8988bd8b057e8b694466a34de3cd6fd89bb5e`
- `outline/prompt_budget.py`: `58484c827f1404bb939e2fa999c74ac1c54f146fe42f05e24ef5c1b6a0ab862d`
- `outline/pipeline.py`: `10b94fb3bafbad0b19b45942a4114a65e707f45f55694c093b71f5909d048093`
- `outline/candidates.py`: `ddcec5794981a6274bca838bef5f7a2f620fb2e3f19aa5d82989236c7e7fc5b6`

### Remaining risks

- Phase 7 must make normalized text/chunks/page index/evidence manifest explicit
  dependencies and move Validation recovery to claim-unit by paper edges.
- Phase 8 must close provider preflight/circuit breakers, subprocess isolation,
  Windows UTF-8 progress, compatibility documentation, and public synthetic E2E.
- Repository-wide pyright debt remains a final-gate obligation; every changed
  Phase 6 Outline module is type-clean.

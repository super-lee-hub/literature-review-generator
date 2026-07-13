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
- Last-known-good checkpoint: `24f318d` (Phase 1)

## Phase status

| Phase | Status | Code checkpoint | Evidence checkpoint |
| --- | --- | --- | --- |
| 0 - hotfix and offline baseline | completed | `63aeba8` | current ledger commit |
| 1 - Zotero and FileIndex | completed | `24f318d` | current ledger commit |
| 2 - identity, registry, audit, fingerprint | pending | - | - |
| 3 - sentence and validation truth | pending | - | - |
| 4 - ReviewBatch derivation | pending | - | - |
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

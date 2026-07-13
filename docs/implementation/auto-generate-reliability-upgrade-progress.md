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
- Last-known-good checkpoint: `63aeba8` (Phase 0)

## Phase status

| Phase | Status | Code checkpoint | Evidence checkpoint |
| --- | --- | --- | --- |
| 0 - hotfix and offline baseline | completed | `63aeba8` | current ledger commit |
| 1 - Zotero and FileIndex | pending | - | - |
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

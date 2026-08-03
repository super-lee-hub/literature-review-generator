# PR #14 Final Verification

Verification date: 2026-08-03 (Asia/Shanghai)
Branch: `codex/platform-hardening-outline-v3`
Target: existing PR #14, kept Draft/Open/Unmerged
Code verification commit: `4fa38893868c80dc855faf18e8d1b7c54e1dada3`

## Local acceptance

| Gate | Result |
| --- | --- |
| `python -m compileall -q .` | PASS |
| `python -m pyright` | PASS: 0 errors, 0 warnings, 0 informations |
| current test-config doctor | PASS: `ok=true`, 0 provider network calls; warning only for pre-existing stale locks |
| `python -m pytest --collect-only -q` | PASS: 678 tests collected |
| `python -m pytest -q --strict-markers -m "not live_api and not playwright and not heavy_ocr"` | PASS: 656 passed, 22 deselected |
| Current production full chain | PASS: `tests/test_current_production_full_e2e.py` |
| Provider receipt closure | PASS: expected, unexpected, out-of-scope, and separate hash domains covered |
| Repair promotion | PASS: 18 focused Repair/Validation transaction tests |
| Queue lease/heartbeat | PASS: 14 focused queue/lease tests |
| Export registration failure | PASS: untrusted result, empty path/id, manifest status, and ZIP cleanup |
| Architecture forbidden-pattern scan | PASS: no findings |
| Git diff hygiene | pending final allowlist staging/push readback |

## Production-shaped positive chain

The new test uses `AgentRuntimeRunner.run()` with three raw PDFs and provider
responses injected at the configured transport boundary. It executes source
intake, preprocessing, Stage 1 Reader calls, Outline relation/candidate/
critique/arbitration/stability nodes, explicit `OutlineAdoptionTransaction`,
durable Review sections, citation catalog/spans, DOCX projection, real current
`ValidationExecutionService`, receipt closure, canonical completion, verified
export, and forensic attestation. The test does not hand-register final
validation/completion/export artifacts.

## Boundary and non-claims

- No live API, Playwright, or heavy OCR result is claimed.
- Repair report/apply is integrated and quarantined; a consolidated successful
  control-plane repair E2E remains a documented limitation.
- The full offline gate is **656 passed, 22 deselected** from **678 collected**;
  `compileall` passes and Pyright reports **0 errors, 0 warnings, 0
  informations**.
- No merge, main update, Ready-for-review transition, or auto-merge is part of
  this task.
- The two user ZIP files remain untracked, unread, unstaged, uncommitted, and
  outside all fixtures.
- GitHub Actions status for the new SHA is recorded after push; the old CI run
  is not reused as evidence.

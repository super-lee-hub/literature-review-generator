# PR #14 Final Verification

Verification date: 2026-08-04 (Asia/Shanghai)
Branch: `codex/platform-hardening-outline-v3`
Target: existing PR #14, kept Draft/Open/Unmerged
Code verification commit: `3166a73e4b9ac036570a58bba899ebab579ba162`

## Local acceptance

| Gate | Result |
| --- | --- |
| `python -m compileall -q .` | PASS |
| `python -m pyright` | PASS: 0 errors, 0 warnings, 0 informations |
| current test-config doctor | PASS: `ok=true`, 0 provider network calls; warning only for pre-existing stale locks |
| `python -m pytest --collect-only -q` | PASS: 700 tests collected |
| `python -m pytest -q --strict-markers -m "not live_api and not playwright and not heavy_ocr"` | PASS: 678 passed, 22 deselected |
| Current production full chain | PASS: `tests/test_current_production_full_e2e.py` |
| Provider receipt closure | PASS: expected, unexpected, out-of-scope, and separate hash domains covered |
| Repair promotion | PASS: focused contract/promotion tests plus successful control-plane repair E2E |
| Queue lease/heartbeat | PASS: persistent queue tests plus Windows `spawn` lease/fence tests |
| Export registration failure | PASS: untrusted result, empty path/id, manifest status, and ZIP cleanup |
| Architecture forbidden-pattern scan | PASS: no findings |
| Code/test diff hygiene | PASS: explicit 39-file allowlist staged and committed; ZIPs not staged |

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
- Repair report/apply is integrated and quarantined; the successful control-plane
  repair E2E now covers current revalidation, exact DOCX rebuild, audit
  promotion, and atomic `CurrentArtifactSet` switching. The consolidated
  negative failure-chain matrix remains a limitation.
- The full offline gate is **678 passed, 22 deselected** from **700 collected**;
  `compileall` passes and Pyright reports **0 errors, 0 warnings, 0
  informations**.
- No merge, main update, Ready-for-review transition, or auto-merge is part of
  this task.
- The two user ZIP files remain untracked, unread, unstaged, uncommitted, and
  outside all fixtures.
- GitHub Actions status for the new SHA is recorded after push; the old CI run
  is not reused as evidence.

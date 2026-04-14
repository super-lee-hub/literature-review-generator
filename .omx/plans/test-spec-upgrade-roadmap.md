# Test Specification — upgrade-roadmap

## Scope
This test spec covers the planned upgrade defined in `.omx/plans/prd-upgrade-roadmap.md`:
- queue completion and execution-path unification,
- exact-citation-set validation redesign,
- repair/revalidation cutover,
- citation refresh/rebuild behavior,
- outline simplification,
- GUI productization and scholarly visual quality.

## Brownfield Anchors
- Sequential queue loop: `services/queue_service.py:521-550`
- GUI queue form still single-job: `gui/app.py:2453-2475`
- GUI queue add controller still single-item: `gui/app.py:864-886`
- Immediate workflow dispatch path: `gui/app.py:1644-1704`
- Legacy stage1 validation call: `main.py:3002-3005`
- Stage2 validation + repair entry: `main.py:4717-4765`
- Summary recheck still in validation report: `validator.py:481-504`
- Structured citation extraction: `services/review_draft.py:184-255`
- Paper-centric citation clusters today: `services/citation_manifest.py:417-500`
- DOCX citation render path: `docx_writer.py:42-65`, `docx_writer.py:318-360`
- Outline critique/arbitration path: `main.py:4866-5008`

## Quality Gates
1. No active stage1 validation call remains in the main pipeline.
2. Exact citation-set grouping is deterministic and order-insensitive (`A+B == B+A`).
3. Low-confidence findings never disappear silently; they always land in the final manual-review report.
4. DOCX output remains internally consistent after repair-triggered rebuilds.
5. Removed outline review layers do not leave broken downstream loaders or tests.
6. GUI flows remain usable and visually aligned with the scholarly style direction.

## Test Matrix

### A. Unit tests

#### A1. Citation-set key normalization
**Target**
- New citation-set bundle builder / helper functions

**Cases**
- `{A}` produces single-key group
- `{A,B}` and `{B,A}` normalize to the same key
- `{A,B}` remains distinct from `{A}` and `{A,B,C}`
- Duplicate citations within one block collapse correctly to one set membership
- Empty/unresolved citations are handled explicitly rather than silently merged

**Expected**
- Deterministic normalized set keys
- Stable ordering and serialization

#### A2. Review block extraction
**Target**
- `services/review_draft.py` and any new cluster-extraction helpers

**Cases**
- Structured `[[cite:...]]` tokens produce exact-set membership
- Mixed structured + regex-fallback citations still produce stable sets
- Block ID, section number, and raw claim span survive into the bundle

**Expected**
- Bundle members retain traceability back to review blocks and occurrences

#### A3. Queue job construction
**Target**
- Queue builders in GUI/service layers

**Cases**
- Multiple PDF jobs
- Multiple Zotero jobs
- Mixed PDF + Zotero batch
- Retry/cancel metadata preserved

**Expected**
- Queue records serialize/deserialise correctly and preserve intended ordering

### B. Integration tests

#### B1. Sequential queue execution
**Target**
- `services/queue_service.py`, `services/job_runner.py`, workflow integration

**Cases**
- Queue of 3+ jobs runs sequentially without manual restart
- Failed job does not prevent later retry path from working
- Cancelled running job transitions correctly and does not corrupt queue state

**Expected**
- Runtime states transition correctly
- Next pending job starts automatically

#### B2. Validation against exact citation sets
**Target**
- New validation bundle builder + `validation/review_validator.py`

**Fixtures**
- Review fixture with:
  - one claim citing only paper A,
  - one claim citing papers A+B together,
  - one claim citing papers A+B+C together

**Expected**
- Three distinct validation groups are produced
- A+B claim is not reclassified into A-only or B-only buckets
- Evidence resolution loads the full cited paper set for each group

#### B3. Repair sequencing
**Target**
- `validator.py`, `services/repair_integration.py`, `validation/repair_*`

**Cases**
- Summary-only drift
- Review-only drift
- Compound drift (summary + review)
- Low-confidence unresolved case

**Expected**
- Summary-first patch order when summary is implicated
- Review regeneration/patch follows summary repair
- Citation manifest and downstream outputs rebuild after repair
- Low-confidence case skips auto-fix and lands in report

#### B4. Citation rebuild / DOCX consistency
**Target**
- `services/citation_manifest.py`, `docx_writer.py`, review rebuild path

**Cases**
- Repair changes cited content but not citation set
- Repair changes citation set membership
- Repair removes one cited paper from a group

**Expected**
- Bibliography reflects the rebuilt truth source
- In-text citation rendering and reference list remain aligned
- No manual Word editing required

#### B5. Outline simplification regression
**Target**
- `main.py`, `outline/*`, outline-loading helpers

**Cases**
- Outline generation still writes a usable downstream outline
- Review generation still consumes the simplified outline path
- Removed critique/arbitration/adopt artifacts are not required for success

**Expected**
- No runtime dependency on removed outline review layers

### C. GUI / Playwright tests

#### C1. Queue builder flow
**Target**
- `gui/app.py`

**Cases**
- Add multiple jobs from GUI
- Inspect queue list and status
- Start queue
- Retry/cancel/clear from GUI

**Expected**
- User can manage a batch without dropping to CLI

#### C2. Productized workflow journey
**Cases**
- Setup/configure
- Build jobs
- Run and observe progress
- Reach outputs/report surfaces
- Use recovery/retry path

**Expected**
- IA feels lifecycle-oriented, not like disconnected utility pages

#### C3. Validation/report visibility
**Cases**
- Successful run with no low-confidence findings
- Run with low-confidence findings

**Expected**
- Final report surfaces clearly in GUI
- Low-confidence items are visible and actionable for manual review

### D. Visual / manual QA

#### D1. Scholarly style review
**Reference**
- `C:\Users\12130\.codex\skills\advanced-science-ui\SKILL.md:1-40`

**Checklist**
- No SaaS card-wall feel
- Low-glare / eye-friendly palette
- Clear reading hierarchy for long-form knowledge work
- Understated, textured, refined presentation
- Queue/progress/output views still feel consistent with the same system

#### D2. Recovery and output readability
**Checklist**
- Long reports remain readable
- Queue/job detail pages are calm rather than visually noisy
- Error/retry states are understandable without feeling like debugging consoles

## Fixtures Needed
1. Small mixed queue fixture:
   - two PDF-folder jobs
   - one Zotero-report job
2. Review draft fixture with exact citation-set combinations `{A}`, `{A+B}`, `{A+B+C}`
3. Paper artifact fixtures with enough evidence to test:
   - supported
   - partial support
   - unsupported
   - ambiguous/low-confidence
4. Outline generation fixture covering the simplified path
5. GUI seed data for queue, outputs, and report pages

## Regression Targets
- `tests/test_workflow_facade.py`
- `tests/test_gui_playwright.py`
- `tests/test_week3_validation.py`
- `tests/test_week4_repair_pipeline.py`
- `tests/test_week4_repair_integration.py`
- `tests/test_week5_outline_json_first.py` (trimmed/replaced to match retained behavior)
- new queue/cluster/rebuild tests as needed

## Exit Criteria
The plan is considered execution-ready when:
1. every acceptance criterion in the PRD maps to at least one concrete automated or manual verification step,
2. exact citation-set grouping has dedicated fixtures and regression tests,
3. stage1 validation removal is covered by tests or diagnostics,
4. outline review removal is covered by regression tests,
5. queue and GUI flows have both functional and user-visible verification coverage,
6. low-confidence reporting is explicitly asserted in tests,
7. final DOCX rebuild consistency is checked after repair scenarios.

## Suggested Verification Order During Execution
1. Lock queue behavior with backend tests.
2. Add exact citation-set bundle tests.
3. Add repair/revalidation integration tests.
4. Remove legacy stage1 validation with regression coverage.
5. Simplify outline path with regression coverage.
6. Refactor GUI and add/update Playwright coverage.
7. Finish with manual visual QA against the installed scholarly UI reference.

## Applied Review Improvements
- Added deterministic normalization tests for exact citation sets.
- Added explicit low-confidence persistence checks to prevent silent drops.
- Added visual QA gates tied to the installed advanced-science-ui reference.
- Added regression coverage requirements for removing legacy stage1 validation and outline review layers.

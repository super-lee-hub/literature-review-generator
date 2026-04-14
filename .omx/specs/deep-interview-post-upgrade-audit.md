# Deep-Interview Spec — post-upgrade-audit

## Metadata
- Profile: standard
- Rounds: 4
- Final ambiguity: 0.13
- Threshold: 0.20
- Context type: brownfield
- Context snapshot: `.omx/context/post-upgrade-audit-20260414T110000Z.md`
- Transcript: `.omx/interviews/post-upgrade-audit-20260414T110000Z.md`

## Clarity breakdown
| Dimension | Score | Notes |
| --- | ---: | --- |
| Intent | 0.90 | User wants to restore trust by verifying prior OMX work against real runnable behavior and preventing another bug cascade. |
| Outcome | 0.90 | Finish with a repo where key functions run and code/tests/docs/GUI are aligned. |
| Scope | 0.80 | Includes prior-task verification, queue-page repair, project-wide runtime-risk audit, and stale-doc updates; excludes unrelated upgrades. |
| Constraints | 0.90 | Total processes <= 3 including main process; prefer minimal/reversible changes; use subagents sparingly within the cap. |
| Success | 0.85 | Prior task counts as complete only when key functions run and code/tests/docs/GUI all align. |
| Context | 0.85 | Brownfield facts gathered from .omx artifacts, working tree, and queue-page source inspection. |

## Intent
Re-establish confidence that the project can be changed without introducing cascading runtime bugs. The user specifically wants verification that the last OMX-built task was not just “marked complete” but actually integrated, runnable, and aligned across UI, code, tests, and documentation.

## Desired outcome
1. Determine whether the previous OMX `upgrade-roadmap` work is truly complete by the user’s stricter standard.
2. Repair the GUI queue page, especially the visible question-mark/mojibake regressions.
3. Audit the whole project for defects that could break normal operation, fixing issues incrementally instead of waiting for a final report.
4. Update stale or misleading documentation so it matches actual behavior.
5. End with verification evidence that key flows run and the project surfaces are aligned.

## In scope
- Inspect `.omx` artifacts, current code, tests, and runtime behavior.
- Use prior OMX artifacts as binding truth source **when they explicitly specify behavior**.
- Otherwise use runnable behavior + existing tests + current code as the operative truth source.
- Fix discovered defects incrementally during the audit.
- Repair queue-page static copy/encoding issues and any directly related queue UX breakage needed for correctness.
- Add or tighten regression tests where missing and necessary.
- Refresh stale docs/help text/README material when inconsistent with implementation.

## Out of scope / Non-goals
- Large refactors unless they are truly necessary for correctness/stability.
- Opportunistic new features.
- Unnecessary visual redesign or architecture reshaping.
- Changes unrelated to restoring correctness/alignment for the audited surfaces.

## Decision boundaries (what OMX may decide without confirmation)
- May audit and fix in one pass rather than pausing after a findings report.
- May prioritize defects by runtime risk and fix order.
- May use at most 2 child/subagent processes at a time so the total process count with the leader stays <= 3.
- May perform minimal structural cleanup if required to prevent recurring failures.
- Must avoid broad redesign unless necessity is evidence-backed.

## Constraints
- Hard process cap: total concurrent processes (main + child/subagents) <= 3.
- Prefer small, reversible diffs.
- Verification must be evidence-based, not status-based.
- Docs must be updated when behavior changes or when prior docs are demonstrably stale.

## Truth-source precedence
1. If the previous OMX run explicitly documented a requirement/decision in its artifacts, follow that.
2. Otherwise, prioritize actual runnable behavior + existing tests + current code.
3. OMX state flags such as “complete” are not sufficient by themselves to prove repository-level completion.

## Testable acceptance criteria
- The queue page no longer shows the corrupted question-mark/misencoded static labels.
- The prior `upgrade-roadmap` surfaces are checked against explicit OMX artifacts and either confirmed aligned or corrected.
- Key user-visible flows run successfully (at minimum the core paths implicated by the audited changes).
- Relevant tests pass, with new regression tests added where practical for fixed defects.
- Documentation/help text reviewed in the touched areas and updated when stale.
- Final report lists what was verified, what was fixed, and any remaining risks/gaps.

## Assumptions exposed + resolutions
- **Assumption:** OMX completion state equals actual completion.  
  **Resolution:** Rejected. Explicit OMX artifacts outrank only when they specify behavior; otherwise runnable behavior/tests/current code win.
- **Assumption:** An audit should happen before any repair.  
  **Resolution:** Rejected. User wants iterative audit-and-fix because issues are often chained.
- **Assumption:** Larger cleanup is acceptable by default.  
  **Resolution:** Rejected. Major changes are out of scope unless truly necessary.

## Pressure-pass findings
- Revisited the success/truth-source criteria after discovering a contradiction: OMX state marked prior work complete while repo/UI evidence showed unresolved issues.
- User clarified precedence: explicit prior OMX artifact > runnable behavior/tests/current code > completion status flag.

## Brownfield evidence vs inference
### Evidence
- `.omx/specs/deep-interview-upgrade-roadmap.md`
- `.omx/plans/prd-upgrade-roadmap.md`
- `.omx/plans/test-spec-upgrade-roadmap.md`
- Prior Ralph state records `py_compile + 126 targeted tests passed`
- `gui/app.py` queue-page block contains corrupted static literals
- Working tree remains dirty after prior upgrade work

### Inference
- Repo-level closure is incomplete despite OMX state-level completion.
- Queue-page corruption likely came from a local/uncommitted regression rather than backend queue data.

## Technical context findings
- Queue page route: `gui/app.py` `@ui.page("/queue")`
- Queue-page corruption centered around lines 2532-2591 in the queue-builder section.
- `gui/i18n.py` returns keys unchanged for `zh-CN`, so corrupted source literals display as-is.
- Prior queue-page issue appears weakly tested because dedicated queue-page assertions are absent.

## Condensed transcript
See `.omx/interviews/post-upgrade-audit-20260414T110000Z.md`.

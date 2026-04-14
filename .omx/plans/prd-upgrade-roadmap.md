# PRD — upgrade-roadmap

## Metadata
- Source spec: `.omx/specs/deep-interview-upgrade-roadmap.md`
- Context snapshot: `.omx/context/upgrade-roadmap-20260414T052412Z.md`
- Planning mode: `ralplan` consensus, short mode
- Status: approved planning draft

## RALPLAN-DR Summary

### Principles
1. Reuse the strongest existing substrates and delete the weakest legacy layers instead of building a parallel architecture.
2. Keep citation identity machine-readable until final DOCX rendering so repairs can rebuild downstream artifacts deterministically.
3. Validate review claims by **exact citation set**, never by single-paper approximation.
4. Make GUI flows product-first and queue-backed, not a thin mirror of CLI flags.
5. Preserve the scholarly, eye-friendly, understated visual language and use the installed `advanced-science-ui` skill as a concrete design reference.

### Decision Drivers
1. Citation correctness and repairability matter more than preserving legacy validation behavior.
2. Brownfield risk must stay bounded around the existing `main.py` orchestrator and current persistence artifacts.
3. Product coherence matters: queueing, validation, outputs, and recovery should feel like one workbench.

### Viable Options
#### Option A — Incremental in-place upgrade with compatibility seams **(Chosen)**
**Approach:** Keep the existing queue runner, `review_draft_v2`, and `citation_manifest_v2` backbone; replace validation grouping/repair logic, remove outline review complexity, and redesign the GUI on the existing controller/state model.

**Pros**
- Lowest migration cost because the sequential queue runner already exists (`services/queue_service.py:521-550`).
- Preserves the current machine-readable citation path already extracted from review blocks (`services/review_draft.py:184-255`).
- Lets the team remove legacy layers directly where the user has explicitly approved deletion (`main.py:3002-3005`, `main.py:4866-5008`).

**Cons**
- Requires careful refactoring around `main.py` review orchestration (`main.py:4717-4765`).
- Existing artifact semantics must evolve without breaking downstream consumers mid-cutover.

#### Option B — Parallel vNext pipeline beside the current one
**Approach:** Build a new queue/validation/gui pipeline beside the existing flow, then switch users over after parity.

**Pros**
- Conceptually cleaner separation.
- Less chance of transient breakage inside legacy flow during early development.

**Cons**
- Duplicates orchestration the repo already has in too many places.
- Increases migration and verification cost.
- Delays value by requiring another cutover path.

**Invalidation rationale**
- The current codebase already has split paths between CLI, GUI, queue, and outline subsystems; adding another “v2 of everything” would amplify the exact complexity the user wants reduced.

#### Option C — Minimal queue/UI cleanup without validation redesign
**Approach:** Only polish queue UX and GUI, leaving current validation and outline complexity mostly intact.

**Pros**
- Fastest visible UX win.

**Cons**
- Fails the user’s main correctness goal.
- Leaves the unwanted legacy stage1 validation and outline review complexity in place.

**Invalidation rationale**
- Explicitly incompatible with the user’s approved deletion boundary and new validation model.

## Requirements Summary
The project must:
1. Support sequential processing of multiple PDF-folder and/or Zotero-report jobs from one queue-backed product flow.
2. Remove legacy stage1 validation and replace it with an exact-citation-set review validation, repair, and revalidation loop.
3. Keep citations internally refreshable/rebuildable while still exporting a final stable DOCX without Word-native live fields.
4. Remove outline critique/arbitration/adopt complexity and keep a simpler outline generation path.
5. Redesign GUI information architecture and visual execution so major workflows feel coherent, productized, and visually aligned with the intended scholarly style.

## Brownfield Evidence
- Queue execution is already sequential and pending-job based in `QueueRunner.run` (`services/queue_service.py:521-550`).
- GUI queue creation is still a single-job form with one project name, one PDF folder, and one Zotero report (`gui/app.py:2453-2475`), and controller insertion is single-item (`gui/app.py:864-886`).
- The main workflow page still dispatches one immediate action at a time through `run_dispatch` (`gui/app.py:1644-1704`).
- Legacy stage1 validation still runs after stage1 analysis when enabled (`main.py:3002-3005`).
- The review path already has stage2 validation + repair integration (`main.py:4717-4765`).
- Current validation reporting still appends `summary_recheck` results, reflecting the older summary-centric validation model (`validator.py:481-504`).
- Review draft extraction already preserves structured citation tokens and block-level citation metadata (`services/review_draft.py:184-255`).
- `citation_manifest_v2` currently clusters by single `paper_id`, not exact citation combinations (`services/citation_manifest.py:417-500`).
- DOCX rendering currently resolves structured citations into plain visible text and builds bibliography from the manifest (`docx_writer.py:42-65`, `docx_writer.py:318-360`).
- Outline generation still persists outline review/critique/arbitration artifacts (`main.py:4866-5008`).
- The installed `advanced-science-ui` skill explicitly targets science/educational/museum-like longform interfaces and warns against SaaS card-wall aesthetics (`C:\Users\12130\.codex\skills\advanced-science-ui\SKILL.md:1-40`).

## Chosen Design
Use a **single queue-backed product flow** and an **artifact-first validation model**:

1. **Queue as backend truth**
   - Keep the existing sequential queue runner as the execution backend.
   - Convert “run now” into an implicit one-item queued execution instead of maintaining a separate orchestration mindset.
   - Preserve cancel/retry/clear/status semantics already present in queue runtime records.

2. **Citation-set validation seam**
   - Do not mutate review validation directly against single-paper clusters.
   - Introduce a new intermediate artifact, tentatively `citation_set_validation_bundle`, derived from `review_draft_v2` + citation manifest occurrences.
   - Each bundle groups review claim spans by the normalized exact citation set key, e.g. `A`, `A+B`, `A+B+C`.

3. **Repair loop with bounded cutover**
   - Validation decides whether a finding targets summary drift, review drift, both, or low-confidence/manual review.
   - High-confidence findings trigger repair in a deterministic order:
     1. patch summary artifacts,
     2. patch review content or regenerate the impacted section(s),
     3. rebuild citation manifest / references / DOCX,
     4. rerun validation for the impacted citation-set groups.
   - Low-confidence findings are never auto-fixed; they go to a final manual review report.

4. **Outline simplification**
   - Remove the outline critique/arbitration/adopt branch and the reviewed-outline preference path.
   - Keep markdown outline generation as the downstream truth unless a lightweight JSON representation is still required purely for internal parsing.

5. **GUI redesign on the existing state/controller base**
   - Keep `WorkspaceController` and NiceGUI foundation.
   - Rework IA around a clearer lifecycle:
     - Setup
     - Job Builder / Queue
     - Run & Progress
     - Outputs & Reports
     - Recovery / Retry
   - Adopt the installed advanced-science-ui style direction rather than a utility-dashboard feel.

## Workstreams

### WS1 — Queue completion and execution-path unification
**Goal:** Make multi-job queueing the default execution model without losing single-job convenience.

**Primary touchpoints**
- `services/queue_service.py`
- `services/job_runner.py`
- `services/workflow_facade.py`
- `gui/app.py`
- `tests/test_workflow_facade.py`

**Changes**
- Extend queue job creation UX to support multiple source items in one session.
- Refactor GUI workflow actions so “run now” internally creates/runs a queued job.
- Keep queue file persistence, retry, cancel, clear, and status visibility.
- Ensure both PDF-folder and Zotero-report jobs can be mixed in the same queue.

**Deliverables**
- Productized queue/job-builder flow
- Unified execution backend
- Regression tests for queue serialization and sequential execution

### WS2 — Exact-citation-set artifact model
**Goal:** Replace paper-centric validation grouping with exact-set grouping.

**Primary touchpoints**
- `services/review_draft.py`
- `services/citation_manifest.py`
- `validation/review_validator.py`
- `validation/evidence_resolver.py`
- `validation/evidence_loader.py`
- new validation artifact helpers under `validation/` or `services/`

**Changes**
- Build deterministic citation-set keys from each review block’s cited papers.
- Group claim spans by exact set, not by individual paper.
- Preserve links back to block IDs, section numbers, and citation occurrence IDs.
- Keep a compatibility layer so old manifest consumers can still read occurrences while new validators read set bundles.

**Deliverables**
- Citation-set bundle schema
- Builders/loaders
- Unit fixtures for `{A}`, `{A+B}`, `{A+B+C}` grouping behavior

### WS3 — Validation cutover and legacy stage1 removal
**Goal:** Make review-stage validation authoritative and delete the old stage1 validation path.

**Primary touchpoints**
- `main.py`
- `validator.py`
- `validation/review_validator.py`
- `validation/summary_recheck.py`
- `tests/test_week3_validation.py`
- `tests/test_validator_diagnostics.py`

**Changes**
- Remove the call site to legacy stage1 validation (`main.py:3002-3005`).
- Replace `summary_recheck`-centric reporting with citation-set validation reporting.
- Keep stage2 validation entrypoint but rewire internals to exact-set grouping and direct source comparison.
- Emit low-confidence findings into a final manual-review report artifact.

**Deliverables**
- New review validation verdict schema
- Removed legacy stage1 path/config references
- Updated validation report format

### WS4 — Repair/revalidation orchestration
**Goal:** Repair summary and review artifacts from validation findings, then rebuild downstream outputs and revalidate.

**Primary touchpoints**
- `main.py`
- `services/repair_integration.py`
- `validation/repair_models.py`
- `validation/repair_planner.py`
- `validation/repair_apply.py`
- summary persistence helpers and paper artifact writers

**Changes**
- Distinguish summary drift, review drift, compound drift, and low-confidence/manual-review.
- Patch summary artifacts first when required.
- Regenerate only impacted review sections where possible; fall back to broader regeneration when dependencies are too coupled.
- Rebuild `citation_manifest_v2`, bibliography output, and final DOCX from the refreshed truth source.
- Rerun validation for touched citation-set groups before marking completion.

**Deliverables**
- Deterministic repair order
- Revalidation loop
- Manual-review carry-forward report

### WS5 — Outline simplification
**Goal:** Remove low-value outline review complexity while preserving outline generation output.

**Primary touchpoints**
- `main.py`
- `outline/*`
- `tests/test_week5_outline_json_first.py`

**Changes**
- Remove/bypass critique, arbitration, adopt, and reviewed-outline preference logic.
- Keep simple markdown outline generation working.
- Reduce test coverage to the retained outline behavior rather than removed review layers.

**Deliverables**
- Simpler outline generation path
- Cleaned-up downstream outline loading
- Updated regression tests

### WS6 — GUI productization and scholarly visual pass
**Goal:** Make the GUI coherent, queue-first, and visually aligned with the intended academic style.

**Primary touchpoints**
- `gui/app.py`
- GUI-specific support services and i18n assets
- `tests/test_gui_playwright.py`
- installed skill reference at `C:\Users\12130\.codex\skills\advanced-science-ui\`

**Changes**
- Reorganize navigation and workflow grouping around actual user journey.
- Replace awkward single-job / recovery separation with clearer lifecycle-oriented entry points.
- Improve readability, spacing, hierarchy, and calm low-glare styling while preserving existing implementation stack.
- Add validation/review-report surfaces to the outputs/recovery experience.

**Deliverables**
- Updated IA/navigation map
- GUI parity for major current CLI workflows through productized flows
- Visual QA checklist against the installed style reference

## Risks and Mitigations
| Risk | Why it matters | Mitigation |
|---|---|---|
| Citation-set grouping breaks legacy consumers | `citation_manifest_v2` is currently paper-centric (`services/citation_manifest.py:417-500`) | Introduce new bundle artifact first; migrate validators before pruning old assumptions |
| Repair loop over-regenerates too much review text | Can produce unstable downstream diffs | Start with impacted-section regeneration plus fallback rules; verify touched sections explicitly |
| Outline simplification breaks reviewed-outline loading | Current code prefers reviewed/adopted outline paths (`main.py:4866-5008`) | Remove review path and update loading logic/tests in the same change set |
| GUI redesign regresses working actions | Current GUI binds directly into controller state and `run_dispatch` (`gui/app.py:1644-1704`) | Keep controller state model, add Playwright regression coverage before large UX changes |
| Low-confidence findings pile up without clear action | User still needs closure on unresolved items | Standardize manual-review report schema with severity, affected artifacts, and recommended human action |

## Verification Strategy
1. Add unit tests for exact citation-set key generation and cluster extraction.
2. Add integration tests for repair sequencing: validate → repair summary/review → rebuild references → revalidate.
3. Add regression tests proving stage1 validation is gone and outline review path is removed.
4. Add queue tests proving sequential mixed-source jobs continue automatically.
5. Add Playwright coverage for the reworked GUI queue/product flows.
6. Run manual visual QA against the installed `advanced-science-ui` guidance before final UI approval.

## Acceptance Criteria

### Queue
- Users can build a queue containing multiple PDF-folder and/or Zotero-report jobs from the GUI.
- Running the queue processes those jobs in order without manual restart.
- Cancel, retry, clear-completed, and status visibility continue to work.
- Single-job runs use the same backend execution path as queued runs.

### Validation
- Legacy stage1 validation no longer executes anywhere in the main pipeline.
- Validation groups claims by normalized exact citation set; `{A}` and `{A+B}` remain distinct.
- Each citation-set group is compared against the full evidence set of the papers in that exact group.
- Findings classify at minimum: supported, partial support, unsupported, wrong source/mapping, compound drift, low confidence/manual review.
- High-confidence fixes patch the relevant summary/review artifacts and trigger targeted revalidation.
- Low-confidence items are preserved in a final report and do not block overall completion.

### Citation refreshability
- `review_draft_v2`-derived citation identity survives until final render.
- Citation manifest and bibliography can be rebuilt after repairs without manual document editing.
- Final DOCX remains citation/bibliography consistent even though it does not use Word-native live fields.

### Outline
- No new outline validation is introduced.
- Existing outline critique/arbitration/adopt complexity is removed or fully bypassed.
- Outline generation still produces a usable downstream outline artifact for review writing.

### GUI
- Major current CLI workflows are reachable through coherent GUI flows.
- Navigation reflects setup → job build/queue → run/progress → outputs/recovery.
- The visual tone remains academic, eye-friendly, understated, and closer to the installed advanced-science-ui direction than to a SaaS dashboard.

## ADR
### Decision
Adopt an incremental in-place upgrade using the existing queue and structured-citation backbone, with compatibility seams for new exact-citation-set validation and a simplified outline path.

### Drivers
- Citation correctness and repairability
- Lower brownfield migration risk
- Faster convergence to a coherent product experience

### Alternatives considered
- Parallel vNext pipeline beside current flow
- Minimal queue/UI cleanup without validation redesign

### Why chosen
The repo already contains the core assets needed for queueing, citation extraction, stage2 validation, repair, and GUI orchestration. Reusing those assets while deleting the explicitly rejected legacy layers produces the fastest path to the user’s stated outcome with the least duplication.

### Consequences
- Artifact schemas will evolve.
- Tests become the primary safety net during deletion/refactor.
- Queue-backed execution will become more central to both GUI and CLI mental models.

### Follow-ups
- Define the exact citation-set bundle schema before repair work starts.
- Decide targeted-section regeneration rules before patch logic is implemented.
- Produce a GUI design checklist distilled from the installed advanced-science-ui skill.

## Available-Agent-Types Roster
Use these agent types for execution follow-up, preferring `xhigh` reasoning where the runtime supports it and falling back to the role’s fixed/default level otherwise:
- `code-mapper` — fast brownfield path mapping
- `executor` — implementation owner for Python refactors/features
- `architect` / `architect-reviewer` — architectural guardrails
- `critic` / `code-reviewer` — quality gate
- `test-engineer` / `verifier` — test/spec completion evidence
- `ui-designer` / `designer` — GUI IA and interaction design
- `browser-debugger` / `accessibility-tester` — runtime and UX validation
- `build-fixer` — stabilization after large refactors
- `writer` / `api-documenter` — user-facing docs and migration notes
- Additional locally installed custom agents may be used if the active runtime exposes them.

## Follow-up Staffing Guidance

### Ralph path (single-owner, sequential)
- Owner: `executor` (`xhigh` if supported)
- Embedded checkpoints:
  - `code-mapper` at start for impacted-file map
  - `architect-reviewer` after design-sensitive cutovers
  - `test-engineer` before final merge
  - `verifier` for completion evidence
- Best when: one owner should drive the whole refactor in dependency order.

### Team path (parallel, recommended for this scope)
- Lane 1: Queue backend + workflow unification — `executor` / `fullstack-developer`
- Lane 2: Validation bundle + repair loop — `executor` / `ai-engineer`
- Lane 3: Outline simplification + downstream cleanup — `executor` / `refactoring-specialist`
- Lane 4: GUI IA/style redesign — `ui-designer` + `frontend-developer` / `ui-fixer`
- Lane 5: Regression/Playwright/verification — `test-engineer` + `browser-debugger` + `verifier`
- Shared oversight: `architect-reviewer` and `code-reviewer`

## Launch Hints
### Ralph
```text
$ralph .omx/plans/prd-upgrade-roadmap.md
```

### Team
```text
$team .omx/plans/prd-upgrade-roadmap.md
```

Suggested team order:
1. map queue/validation/outline/gui ownership boundaries,
2. land schema/cutover groundwork,
3. execute backend and GUI lanes in parallel,
4. run verification and fix loop,
5. close with final report.

## Team Verification Path
Before shutdown, the team should prove:
1. mixed-source queues run sequentially,
2. exact-citation-set grouping works for single and multi-paper citations,
3. repair/revalidation updates summary and review artifacts correctly,
4. low-confidence findings appear in a final manual-review report,
5. outline review complexity is removed without breaking outline generation,
6. GUI flows cover the major user journey and pass visual/functional QA.

After team execution, a final Ralph/verifier pass should confirm:
- no legacy stage1 validation path remains active,
- no outline critique/arbitration code path remains in the normal workflow,
- final DOCX regeneration stays citation-consistent,
- tests and diagnostics pass.

## Applied Improvement Changelog
- Added compatibility-seam guidance so exact-citation-set validation does not force a reckless all-at-once artifact migration.
- Clarified that “queue-first” means one backend path for both single-job and multi-job execution, not a degraded user experience for simple runs.
- Added explicit outline-loading cleanup to avoid leaving reviewed-outline dead paths behind.
- Expanded staffing guidance, launch hints, and verification checkpoints for the eventual execution handoff.

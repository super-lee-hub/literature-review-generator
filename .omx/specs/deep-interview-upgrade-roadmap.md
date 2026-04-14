# Deep Interview Spec — upgrade-roadmap

## Metadata
- Profile: standard
- Rounds: 6
- Final ambiguity: 18%
- Threshold: 20%
- Context type: brownfield
- Residual risk: low
- Context snapshot: `.omx/context/upgrade-roadmap-20260414T052412Z.md`
- Transcript: `.omx/interviews/upgrade-roadmap-20260414T052412Z.md`

## Intent
The user wants to turn `auto-generate` from a partially upgraded literature-review workbench with scattered incomplete features into a more coherent, reliable product. The main goals are to improve batch throughput, make citations and validation source-grounded and repairable, simplify/remove low-value outline review complexity, and make the GUI feel like a complete product rather than a thin or awkward shell over CLI-era flows.

## Desired outcome
Deliver a coherent upgrade plan and implementation lane that:
1. lets the product run multiple jobs sequentially without manual babysitting,
2. replaces weak/slow legacy validation with a citation-set-grounded validation + repair + revalidation workflow,
3. removes outline-validation/critique complexity rather than adding more,
4. upgrades the GUI to product-level equivalent coverage and better information architecture.

## In scope
### 1) Queue mode completion
- Support true multi-job sequential processing for multiple PDF folders and/or multiple Zotero report jobs.
- Use the existing queue infrastructure as the base rather than rebuilding from scratch.
- Improve GUI queue UX so users can add/manage multiple jobs naturally.
- Ensure queue processing automatically starts the next job after the previous one finishes.
- Productize the queue entry flow instead of keeping it as a hidden/secondary single-item form.

### 2) Validation redesign
- Remove the current legacy stage1 validation path.
- Keep the new validation centered on the review-writing / review-validation stage.
- Use the internal structured citation truth source (`review_draft_v2`, `citation_manifest_v2`, or successor artifacts) as the review-citation backbone.
- Group validation work by **exact citation set** (e.g. `{A}`, `{A,B}`, `{A,B,C}`), not by single paper.
- For each citation-set group, extract the review statements/claim blocks belonging to that exact set and compare them against the full text/evidence of all papers in that set together.
- When errors are found, inspect whether the problem came from stage1 summary drift, review drift, or both.
- Auto-correct the affected summary and the affected review content where confidence is sufficient.
- Re-run validation after repairs.
- Preserve low-confidence findings in a final report for human review instead of blocking completion.

### 3) Citation / cross-reference model
- The system must maintain an internally refreshable citation truth source.
- The system must be able to rebuild references and review output after repairs.
- Final output should be a stable DOCX with consistent citations and bibliography.
- Word-native live editable/refreshable Zotero-like fields are **not** required.

### 4) GUI productization
- GUI should cover CLI-equivalent capabilities through productized flows, not necessarily parameter-for-parameter mirroring.
- Improve page design, information hierarchy, and user journey; current GUI layout and workflow movement are explicitly considered awkward and in need of redesign.
- Bring incomplete CLI-adjacent capabilities into coherent GUI flows where useful.
- Preserve and strengthen the intended visual direction: **academic, eye-friendly, simple, textured, understated, high-quality**.
- OMX may reuse/install the user-provided UI reference `https://github.com/cyjjjj-21/codex-advanced-science-ui/tree/main` during implementation if it is reachable and compatible.
- If useful during implementation, OMX may also use locally installed custom Codex subagents in addition to the default roster, provided those roles are actually exposed by the active runtime/session.

### 5) Outline simplification
- Do **not** add the proposed new outline validation workflow.
- Remove/clean out the existing outline critique / arbitration / adopt complexity if it no longer serves the desired product direction.
- Prefer a simpler outline generation path.

## Out of scope / non-goals
- No requirement to implement Zotero/Word-plugin-style live editable citation fields, bookmarks, or field-refresh operations inside Word.
- No requirement for manual in-Word post-processing as part of the normal pipeline.
- No requirement to preserve the legacy stage1 validation path.
- No requirement to preserve the existing outline critique/arbitration/adopt workflow.
- GUI parity does not mean reproducing every CLI flag literally as-is in the interface.

## Decision boundaries (what OMX may decide without further confirmation)
OMX may, without further confirmation:
- redesign the internal validation artifacts and grouping model as long as the exact-citation-set rule is preserved,
- remove old stage1 validation codepaths and configs,
- remove existing outline critique/arbitration flows,
- rebuild queue UX and GUI navigation/product flow,
- reuse/install the user-provided advanced-science UI reference if reachable and compatible,
- delegate to suitable locally installed custom subagents when the active runtime exposes them,
- choose the concrete repair-loop sequencing and report schemas,
- choose whether repairs regenerate only impacted sections or regenerate larger downstream artifacts, as long as behavior remains correct and explainable.

OMX should still escalate if:
- a proposed change would remove major user-visible capabilities unrelated to the requested areas,
- a destructive migration would invalidate existing user data/artifacts without a compatibility path,
- a redesign requires introducing new dependencies or external runtime requirements not currently present.

## Constraints
- Brownfield Python codebase with `main.py` still acting as the large orchestration center.
- Existing queue, review draft, citation manifest, validation, repair, and GUI subsystems should be reused and completed rather than replaced wholesale where practical.
- The user wants all four requested upgrade areas addressed together, not deferred to a later concept-only phase.
- Validation must support contextual comparison against original paper evidence and not just summary-to-review checking.
- Low-confidence findings must survive to a human-review report.
- GUI redesign must stay within the project's intended scholarly visual language rather than switching to a flashy/consumer-app aesthetic.

## Testable acceptance criteria
### Queue
- A user can enqueue multiple jobs covering different PDF folders and/or Zotero reports.
- Running the queue processes jobs sequentially to completion without manual restart between jobs.
- Queue state, retry, cancel, and completed/failed visibility remain available.
- GUI offers a clear productized way to add and manage multiple jobs.

### Validation
- Legacy stage1 validation no longer runs.
- Review validation groups claims by exact citation set.
- Each exact-set cluster is validated against the corresponding full set of cited papers together.
- The system can classify issues into at least: summary drift, review drift, both/compound, low-confidence/manual-review.
- When auto-fix confidence is sufficient, affected summary content and affected review content are updated and then revalidated.
- Low-confidence unresolved items are emitted in a final review report for manual checking.

### Citation system
- Review text continues to preserve machine-tractable citation identity before DOCX rendering.
- After repairs, citation manifests / references can be rebuilt without manual editing.
- Final DOCX output has consistent in-text citations and bibliography derived from the internal truth source.

### Outline
- The outline path is simplified; new outline-validation work is absent.
- Existing critique/arbitration/adopt complexity is removed or bypassed in the final design.

### GUI
- GUI allows users to perform equivalent end-to-end operations for the important current CLI workflows.
- GUI structure and navigation are cleaner and more understandable than the current layout.
- Queue and validation/reporting workflows are surfaced in understandable product flows.
- The final UI remains visually aligned with an academic, low-glare, understated, refined style.

## Brownfield evidence vs inference
### Evidence-backed
- Queue primitives and runner already exist in `services/queue_service.py`.
- CLI queue handlers already exist in `main.py`.
- GUI queue page already exists in `gui/app.py`.
- Structured citation tokens and review-draft/citation-manifest persistence already exist in `main.py`, `services/review_draft.py`, and `services/citation_manifest.py`.
- DOCX rendering currently resolves citations into visible text in `docx_writer.py`.
- Validation and repair modules already exist in `validator.py` and `validation/*`.
- Outline JSON-first and critique/arbitration paths already exist in `main.py` and `outline/*`.
- Zotero official docs show their integration model uses Add/Edit Citation, Add/Edit Bibliography, Refresh, Fields/Bookmarks, and hidden field/document data via integration APIs.

### Inference
- The current user pain with queueing is likely more about incomplete UX and coordination than missing low-level queue mechanics.
- The cleanest way to satisfy the user is likely to keep the existing internal structured citation model and strengthen it, rather than trying to emulate Zotero’s Word-native runtime integration.
- The current outline complexity may now be negative value relative to the simplified product direction.
- The user values visual tone as part of product quality, not just feature completeness.

## Technical direction hints for planning
- Validation may need a new intermediate artifact representing citation-set clusters, extracted claim spans, associated evidence bundle references, validation verdicts, repair decisions, and low-confidence carry-forward items.
- Repair orchestration likely needs deterministic dependency order: validate clusters -> decide repair targets -> patch summary -> patch review -> rebuild citation artifacts -> regenerate affected outputs -> revalidate.
- Queue UX likely needs integration with the main workflow page rather than remaining a separate secondary utility page.
- GUI redesign should likely revisit navigation groups, workflow page mental model, and recovery/queue/report placement.

## Assumptions exposed + resolutions
- Assumption: “Cross-reference” means Word-native live editable fields. Resolution: false. It means an internal truth source that can be recomputed and re-exported.
- Assumption: “Do all four areas” means preserve every legacy implementation. Resolution: false. The user explicitly approved removing stage1 validation and removing outline critique/arbitration.
- Assumption: validation grouping can be paper-centric. Resolution: false. It must be exact-citation-set-centric.

## Recommended handoff
### Recommended next step: `$ralplan`
Use the deep-interview spec as the requirements source of truth and produce:
- PRD for queue + validation + citation refresh + GUI productization + outline simplification
- Test spec covering queue behavior, exact-citation-set validation, repair/revalidation loop, low-confidence reporting, and GUI equivalence/UX expectations

Suggested invocation:
`$plan --consensus --direct .omx/specs/deep-interview-upgrade-roadmap.md`

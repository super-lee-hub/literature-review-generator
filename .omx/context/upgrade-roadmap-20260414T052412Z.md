# Deep Interview Context Snapshot

- Task statement: Plan an upgrade roadmap for auto-generate covering queue mode, rebuilt validation around citation/source grounding, outline verification, and GUI parity.
- Desired outcome: A clarified, execution-ready spec for the next planning step.
- Stated solution: Add multi-job queueing; redesign validation around citation-aware source checks and repair loops; add outline self-check/cross-model critique; finish GUI coverage for CLI features.
- Probable intent hypothesis: Improve throughput, citation correctness, and product completeness while reducing weak/slow validation paths.

## Known facts / evidence
- Queue infrastructure already exists in `services/queue_service.py` and CLI handlers in `main.py` (`--queue-add`, `--queue-run`, `--queue-list`, etc.).
- GUI already has `/queue` page and queue actions in `gui/app.py`, but the queue add form is still single-job oriented (`project_name`, one `pdf_folder`, one `zotero_report`), and the main workflow page still launches one workflow at a time.
- Review generation already uses structured citation tokens like `[[cite:paper_key|...]]` in prompts and persists `review_draft_v2` plus `citation_manifest_v2` (`main.py`, `services/review_draft.py`, `services/citation_manifest.py`).
- Word export currently resolves those tokens into plain-text in-text citations and a generated reference list (`docx_writer.py`), not Word-native dynamic citation fields.
- Stage 2 validation already exists (`validator.py`, `validation/review_validator.py`, `validation/summary_recheck.py`, `validation/repair_*`), but it currently combines citation support checks with summary recheck heuristics rather than the user's proposed direct AI-vs-source comparison loop.
- Outline generation already has a JSON-first outline path with critique and arbitration scaffolding in `main.py` / `outline/*`.
- Official Zotero docs/dev docs indicate the Word plugin maintains citation/bibliography state dynamically and uses document-linked citation objects (fields/bookmarks + document data) rather than only emitting static text.

## Constraints
- Brownfield Python project with `main.py` as legacy orchestrator and GUI/services/validation layers partially extracted.
- No direct implementation in deep-interview mode.

## Unknowns / open questions
- First-release boundary: internal citation truth source only vs Word-native dynamic citation objects.
- Whether all four ideas must ship together or can be phased.
- Which current validation behavior is worth preserving.
- GUI parity target: literal 1:1 CLI exposure or productized equivalents.

## Decision-boundary unknowns
- Can OMX redesign the validation data model/artifact format if migration shims are preserved?
- Can stage1 validation be removed entirely, or only disabled/replaced behind a flag first?
- Is Word-native field/bookmark support mandatory for v1 of the citation overhaul?

## Likely codebase touchpoints
- `main.py`
- `validator.py`
- `validation/*`
- `services/review_draft.py`
- `services/citation_manifest.py`
- `docx_writer.py`
- `services/workflow_facade.py`
- `services/job_runner.py`
- `services/queue_service.py`
- `gui/app.py`

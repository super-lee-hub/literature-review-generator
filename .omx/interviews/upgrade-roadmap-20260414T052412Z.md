# Deep Interview Transcript Summary — upgrade-roadmap

- Profile: standard
- Context type: brownfield
- Interview ID: 5d295978-6a2b-4843-b783-c1a418b154ba
- Final ambiguity: 18%
- Threshold: 20%
- Context snapshot: `.omx/context/upgrade-roadmap-20260414T052412Z.md`

## Brownfield findings
- Existing queue infrastructure already exists in `services/queue_service.py`, `services/job_runner.py`, `services/workflow_facade.py`, and `main.py` (`--queue-add`, `--queue-run`, `--queue-list`, etc.). GUI also already exposes `/queue` in `gui/app.py`, but the UX is still single-job oriented.
- Structured citation infrastructure already exists: prompts emit `[[cite:paper_key|...]]`, `review_draft_v2` is persisted by `main.py` + `services/review_draft.py`, and `citation_manifest_v2` is built by `services/citation_manifest.py`.
- Current DOCX export in `docx_writer.py` renders structured citations into visible text and generates a reference list, but does not maintain a Word-native editable field system.
- Current validation stack exists in `validator.py` + `validation/*`, including review validation, summary recheck, and repair pipeline. Legacy stage1 validation still exists in `validator.validate_paper_analysis`.
- Current outline path already has JSON-first outline, critique, arbitration, and adopt scaffolding in `main.py` + `outline/*`.
- Official Zotero docs confirm that Zotero’s plugin model uses dynamic citation/bibliography operations and field/bookmark-backed document integration, but the user explicitly does **not** require that level of Word-native edit/refresh behavior for this project.

## Q&A
### Round 1 — citation implementation boundary
**Q:** Must the citation system become Word-native refreshable/editable like Zotero, or is an internally consistent cross-reference system plus stable DOCX output enough?

**A:** Only a stable document with reliable cross-reference behavior is required. Word-native editable/refreshable citation fields are not necessary.

### Round 2 — non-goals / deferral
**Q:** If this must be phased, which parts are explicitly out of scope now?

**A:** The user does not want to defer the four requested upgrade areas. These are existing partial systems that should be completed and cleaned up together.

### Round 3 — removal boundary + validation grouping model
**Q:** Should existing stage1 validation be preserved or replaced? How should validation grouping work?

**A:** Existing stage1 validation should be removed and replaced. New validation groups must be keyed by the **exact citation set** used together in the review text, not by a single cited paper. Examples: `{paper1}`, `{paper1,paper2}`, `{paper1,paper2,paper3}` are all distinct validation groups.

### Round 4 — low-confidence fallback
**Q:** What should happen when the system cannot confidently auto-resolve a validation issue?

**A:** Low-confidence findings should be kept, skipped, and summarized in a final human-review report. They should not block completion of the whole pipeline.

### Round 5 — outline and GUI direction
**Q:** Should outline validation be added/kept, and should GUI parity mirror CLI exactly?

**A:** The new outline validation idea should be dropped. Existing outline critique/arbitration should also be cleaned out. GUI parity should be productized rather than 1:1 parameter mirroring, and the current page layout / flow should be improved.

## Pressure-pass findings
- The interview revisited the initial Zotero-style requirement and clarified that the user does **not** need Word-native live refresh; instead the requirement is an **internally refreshable truth source** plus final stable DOCX regeneration.
- The interview also pushed on “do everything” versus “preserve everything,” yielding an explicit deletion decision: stage1 validation should be removed entirely, and the existing outline critique/arbitration path should be removed as well.

## Later requirement additions
- GUI visual direction is explicitly constrained to an academic, eye-friendly, simple, textured, understated, high-quality style.
- The user suggested reusing/installing `https://github.com/cyjjjj-21/codex-advanced-science-ui/tree/main` during UI work as a style/component reference if available.
- The user also indicated that additional custom subagents were installed from `https://github.com/VoltAgent/awesome-codex-subagents` and authorized using those roles in addition to the session's built-in subagent roster where the runtime exposes them.

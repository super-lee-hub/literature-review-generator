# Architecture Overview & Module Map

> Audience: Developers, maintainers, AI agents.
> Source: AGENTS.md §4, §8, §11, §12.

## Current Architecture Overview

```text
User Entry Points
├─ CLI: main.py -> dispatch_command() -> services/job_runner.py
└─ GUI: launch_gui.py -> gui/app.py -> services/workflow_facade.py -> services/job_runner.py

AI-Native Entry Point
└─ Codex / OMX: .codex/skills/auto-generate-orchestrator/SKILL.md
   -> runtime/job_spec.py
   -> runtime/orchestrator.py
   -> services/job_runner.py / validator.py / workspace artifacts

Runtime / Workspace Layer
├─ services/job_workspace.py
├─ services/artifact_registry.py
├─ services/progress_state.py
├─ services/job_fingerprint.py
└─ services/queue_service.py

Stage 1: Input, Preprocessing, Structured Summaries
├─ zotero_parser.py
├─ file_finder.py
├─ preprocess/service.py
├─ preprocess/visual_artifacts.py
├─ services/stage1_input_builder.py
├─ summary_schema.py
├─ services/paper_artifact.py
└─ services/summary_reuse.py

Stage 2/3: Outline, Review, Citation Chain
├─ main.py / LiteratureReviewGenerator
├─ services/review_draft.py
├─ services/citation_manifest.py
├─ report_generator.py
└─ docx_writer.py

Validation / Repair Layer
├─ validator.py
├─ validation/review_validator.py
├─ validation/summary_recheck.py
├─ validation/repair_planner.py
├─ validation/repair_apply.py
└─ services/repair_integration.py

Production / Configuration / Extensions
├─ gui/app.py
├─ services/configuration_service.py
├─ services/environment_service.py
├─ free_mode/service.py
├─ free_mode/profile_manager.py
└─ rag/local_rag.py
```

## GUI and CLI Relationship

### Established Facts

- CLI entry point: `python main.py ...`
- GUI entry point: `python launch_gui.py`
- GUI does NOT have a fully independent engine; GUI and CLI share underlying job request / workspace / artifact logic, but only GUI provides user-visible background queue interaction
- The repo also has a **repo-local Codex skill** as an AI-native entry point; this is additive, not a replacement for GUI or CLI

### Current Execution Chain

- GUI constructs parameters through `services/workflow_facade.py`
- Actual execution is coordinated by `services/job_runner.py`
- `main.py`'s `dispatch_command()` remains a compatibility entry point, but real execution increasingly depends on the job workspace / artifact / resume layer
- The AI-native skill normalizes input via `runtime/job_spec.py` + `runtime/orchestrator.py` and reuses the same workspace / artifact / validation foundation locally

### Deprecated Judgments

Do not assume:
- "GUI is just a thin shell; main.py is the core"
- "All outputs live in `output/<project>/`"
- "Word/Excel are the only primary artifacts"

These judgments are no longer accurate.

## Technical Debt & Notes

Key current realities of the repository:

- `main.py` remains large, with historical and new logic coexisting
- Project naming is not fully unified; `llm_reviewer_generator` still appears in places
- Some old help text / legacy docs / migration notes lag behind current reality
- Compatibility paths still exist, particularly outline adopt and some legacy output / citation fallback
- The GUI is significantly productized but still shares the historical main flow; do not assume all page text equals underlying capability
- Word / Excel are export layers; do not reverse-engineer all questions from these exports
- When encountering old-style `output/<project>/` content, first confirm it is a compatibility pointer, not a real workspace

## Module Map: What to Edit for What Task

When the task is:

- **Change user documentation split**: `README.md`, `README.zh-CN.md`, `README.en.md`, `AGENTS.md`
- **Change Stage 1 summary structure**: `summary_schema.py`, `services/paper_artifact.py`, `report_generator.py`, related tests
- **Change Stage 1 reuse**: `services/summary_reuse.py`, `tests/test_summary_reuse.py`
- **Change workspace / output / resume**: `services/job_runner.py`, `services/job_workspace.py`, `services/artifact_registry.py`, `services/progress_state.py`
- **Change GUI workbench**: `gui/app.py`, `services/configuration_service.py`, `tests/test_gui_playwright.py`
- **Change PDF preprocessing**: `preprocess/service.py`, `rag/local_rag.py`, `tests/test_preprocess_service.py`
- **Change review draft / citation chain**: `services/review_draft.py`, `services/citation_manifest.py`, `docx_writer.py`
- **Change validation / repair**: `validator.py`, `validation/*.py`, `services/repair_integration.py`
- **Change queue**: `services/queue_service.py`, `gui/app.py`, `tests/test_queue_service.py`
- **Change AI-native skill / runtime bridge**: `.codex/skills/auto-generate-orchestrator/SKILL.md`, `runtime/*.py`, `tests/test_runtime_*.py`

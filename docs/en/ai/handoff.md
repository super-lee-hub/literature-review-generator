# AI Handoff Document

> Audience: AI agents, new maintainers.
> Source: AGENTS.md §1-3, §9, §10, §14.

## Documentation Split

Current documentation division:

- `README.md` — Project landing page / router; no longer carries all detail
- `README.zh-CN.md` — Complete Chinese user guide
- `README.en.md` — Complete English user guide
- `AGENTS.md` — AI / new maintainer handoff entry point (fat stub with reading order and links)
- `TRUTH_SOURCES.md` — Runtime truth, durable artifacts, compatibility paths (thin stub → docs/)
- `FEATURE_MATRIX.md` — Feature status matrix (thin stub → docs/)
- `ARCHITECTURE_BASELINE.md` — Migration-era baseline (thin stub → docs/)

## Project Summary

`auto-generate` is a local AI literature analysis and review-writing workbench supporting:

- PDF folder input
- Zotero report + Zotero library input
- CLI, GUI, and repo-local Codex skill — three entry points
- Stage 1 summaries / Stage 2 outline / Stage 3 review draft
- GUI background queue, checkpoint resume, summary reuse, preprocess cache, validation/repair, optional local RAG

It should no longer be understood as a "single-script literature review generator" but as a local workbench with workspace / artifact / GUI background queue infrastructure.

## Recommended Reading Order

If you are a new AI conversation, build context in this order:

1. `AGENTS.md` (fat stub at root)
2. `TRUTH_SOURCES.md` → [../runtime/truth-sources.md](../runtime/truth-sources.md)
3. `FEATURE_MATRIX.md` → [../reference/feature-matrix.md](../reference/feature-matrix.md)
4. `summary_schema.py`
5. `services/job_runner.py`
6. `main.py`
7. `gui/app.py`
8. `.codex/skills/auto-generate-orchestrator/SKILL.md`
9. `runtime/orchestrator.py`
10. `preprocess/service.py`
11. `services/summary_reuse.py`
12. `validation/review_validator.py`
13. `services/repair_integration.py`
14. `tests/test_summary_reuse.py`
15. `tests/test_gui_playwright.py`
16. `tests/test_runtime_orchestrator.py` / `tests/test_runtime_subagent_contract.py`
17. `tests/test_job_runner.py`
18. `tests/test_week3_validation.py` / `tests/test_week4_repair_integration.py`

Key notes: `summary_schema.py` is the canonical summary fact source; `services/job_runner.py` is the primary job workspace / resume / artifact coordination entry; `tests/*` are often more trustworthy than old comments and help text.

## Configuration System

Recommended convention: secrets in `.env`, non-sensitive parameters in `config.ini`.

Key config sections: `Paths`, `Primary_Reader_API`, `Backup_Reader_API`, `Writer_API`, `Outline_API`, `Free_Mode_API`, `Validator_API`, `Performance`, `Preprocess`, `Retry_Settings`, `Stage2_Retry`, `Validation`, `Styling`, `GUI`, `API_Parameters`

Key environment variables: `LLM_PRIMARY_READER_API`, `LLM_BACKUP_READER_API`, `LLM_WRITER_API`, `LLM_OUTLINE_API`, `LLM_FREE_MODE_API`, `LLM_VALIDATOR_API`, `MINERU_*`

## Current Capabilities

When updating README or introducing the project, at minimum cover:
- PDF folder / Zotero dual input
- GUI + CLI + repo-local Codex skill three entry points
- job workspace + artifact registry + latest pointer
- stage-1 summary reuse
- downstream `--summary-file` + `--summary-source`
- GUI workflow-page queue system
- partial rerun / failed retry
- preprocess cache + OCR fallback
- free mode profile / idea
- review_draft_v2 + citation_manifest_v3
- optional validation / repair
- optional local RAG
- AI-native runtime bridge + `source_bundle.json` / `runtime_stage_trace.json`

## Conclusion for Future AI Conversations

Default understanding of this project:

- A local AI literature analysis / review-writing workbench built on job workspace / artifact / GUI background queue infrastructure
- Entry points: `main.py` CLI, `launch_gui.py` + `gui/app.py` GUI, and repo-local Codex skill
- Stage 1 truth is canonical summaries; Stage 3 truth is `review_draft_v2 + citation_manifest_v3`
- `README.md` is now only a router page; user details in `README.zh-CN.md` / `README.en.md`
- For deeper artifact / compatibility details, see `docs/en/runtime/`

Read this file first, then switch to the relevant module for the task. Do not treat old migration docs or scattered comments as the sole fact source.

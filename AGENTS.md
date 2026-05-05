# AGENTS.md

> AI agent and developer handoff entry point. The authoritative documentation lives in `docs/`. This fat stub preserves the project identity, full reading order, and navigation map so a fresh AI agent can get oriented from ONE file in 3-5 minutes.

## What This Project Is

`auto-generate` is a local, corpus-controlled, full-text-first AI literature analysis and review-writing workbench. It supports PDF folder mode and Zotero mode, with three entry points: GUI (beginner-friendly), CLI (repeatable batch runs), and Codex/OMX skill (AI-native execution). The pipeline runs: PDF preprocessing → Stage 1 structured summaries → Stage 2 literature-review outline → Stage 3 review draft + citation manifest + DOCX → optional validation/repair.

## Recommended Reading Order

If you are a new AI conversation, build context in this order (1-2 sentence summary per item):

1. **AGENTS.md** (this file) — project identity, reading order, and "where to find everything" directory
2. **TRUTH_SOURCES.md** → [docs/en/runtime/truth-sources.md](./docs/en/runtime/truth-sources.md) — canonical truth sources per stage, data contracts, compatibility projections
3. **FEATURE_MATRIX.md** → [docs/en/reference/feature-matrix.md](./docs/en/reference/feature-matrix.md) — implementation status of all features
4. **summary_schema.py** — canonical Stage 1 summary schema; the structural fact source for `*_summaries.json`
5. **services/job_runner.py** — primary job workspace / resume / artifact coordination entry point
6. **main.py** — still large; now more of a compatibility entry + main flow orchestration role
7. **gui/app.py** — GUI workbench; shares underlying logic with CLI via workflow_facade and job_runner
8. **.codex/skills/auto-generate-orchestrator/SKILL.md** — the Codex/OMX skill contract for AI-native execution
9. **runtime/orchestrator.py** — AgentRuntimeBridge; bootstraps workspace and delegates stages to subagents
10. **preprocess/service.py** — PDF preprocessing pipeline (normalized.md, page_index, diagnostics, OCR fallback)
11. **services/summary_reuse.py** — Stage 1 summary reuse, strong/weak/non-resumable classification
12. **validation/review_validator.py** — review validation against evidence (draft + manifest + preprocess + metadata)
13. **services/repair_integration.py** — repair pipeline integration (plan, apply, recheck)
14. **tests/test_summary_reuse.py** — tests for summary reuse and resume logic
15. **tests/test_gui_playwright.py** — Playwright-based GUI E2E tests (optional, skipped if Playwright not installed)
16. **tests/test_runtime_orchestrator.py / tests/test_runtime_subagent_contract.py** — AI-native runtime tests
17. **tests/test_job_runner.py** — job workspace and runner tests
18. **tests/test_week3_validation.py / tests/test_week4_repair_integration.py** — validation and repair integration tests

Key meta-notes: `summary_schema.py` is the canonical fact source for summaries; `services/job_runner.py` is the primary coordination layer; `tests/*` are often more trustworthy than old comments and help text.

## Where To Find Everything

| If you need... | Go to... |
|----------------|----------|
| AI agent onboarding (full handoff) | [docs/en/ai/handoff.md](./docs/en/ai/handoff.md) |
| Architecture overview & module map | [docs/en/developer/architecture.md](./docs/en/developer/architecture.md) |
| Dev environment setup | [docs/en/developer/setup.md](./docs/en/developer/setup.md) |
| Architecture baseline (historical) | [docs/en/developer/architecture-baseline.md](./docs/en/developer/architecture-baseline.md) |
| Codex/OMX skill documentation | [docs/en/ai/skill.md](./docs/en/ai/skill.md) |
| AI runtime bridge details | [docs/en/ai/runtime-bridge.md](./docs/en/ai/runtime-bridge.md) |
| Runtime truth sources (all stages) | [docs/en/runtime/truth-sources.md](./docs/en/runtime/truth-sources.md) |
| Compatibility & deprecation paths | [docs/en/runtime/compatibility.md](./docs/en/runtime/compatibility.md) |
| Workspace layout & hard constraints | [docs/en/runtime/workspace-layout.md](./docs/en/runtime/workspace-layout.md) |
| Feature status matrix | [docs/en/reference/feature-matrix.md](./docs/en/reference/feature-matrix.md) |
| Migration history | [docs/en/reference/migration-history.md](./docs/en/reference/migration-history.md) |
| Chinese docs (完整中文文档) | [docs/zh-CN/](./docs/zh-CN/) |

## Documentation Map

| Audience | Entry Point | Full Content |
|----------|-------------|--------------|
| Users (中文) | [README.zh-CN.md](./README.zh-CN.md) | Complete CN user guide |
| Users (English) | [README.en.md](./README.en.md) | Complete EN user guide |
| Developers | [docs/en/developer/](./docs/en/developer/) | Architecture, setup, module map, baseline |
| AI agents | [docs/en/ai/](./docs/en/ai/) | Handoff, skill, runtime bridge |
| Maintainers | [docs/en/runtime/](./docs/en/runtime/) | Truth sources, compatibility, workspace |
| Everyone | [docs/en/reference/](./docs/en/reference/) | Feature matrix, migration history |

> The original AGENTS.md content (all 14 sections) has been migrated into `docs/zh-CN/` and `docs/en/` with full bilingual mirroring. This fat stub preserves the complete reading order and navigation map. The git history retains the full original.

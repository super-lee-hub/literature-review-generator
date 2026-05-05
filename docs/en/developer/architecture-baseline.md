# Architecture Baseline (Historical Reference)

> Last updated: `2026-04-02`
> Audience: maintainers / AI agents.
> Note: This file is a migration-era baseline, not current runtime truth. For current reality, see AGENTS.md and the runtime docs under docs/.

## Baseline

- Repository: `super-lee-hub/literature-review-generator`
- Branch: `main`
- Commit: `a3ba7ebfc10eaabda62d08ca3dfc47e7fafe2755`
- Scope frozen by this document: `Week 0` and the migration baseline for `Week 1+`

## Current Source Of Truth (at snapshot time)

Runtime truth at snapshot time was spread across:

- `main.py` — Primary orchestration center, CLI argument routing, output path decisions, Stage 1/2/3 checkpoint handling
- `validator.py` — Legacy stage-2 validation entrypoint
- `docx_writer.py` — Legacy review rendering backend
- `services/workflow_facade.py` — Thin GUI/CLI compatibility facade
- `output/<project>/` — Legacy mixed workspace

## Target Source Of Truth

After migration, the durable source of truth is fixed to job workspace artifacts:

- `review_draft.json`
- `citation_manifest.json`
- `paper_artifact.json`
- `visual_manifest.json`
- `outline.json`
- `artifact_registry.json`

These files are the only durable truth for downstream execution and recovery. Legacy projections may exist for compatibility, but they are never the primary source of truth.

## Compatibility Roles

- `main.py` — Compatibility entrypoint; must stop accumulating new long-term domain logic
- `validator.py` — Compatibility entrypoint; must delegate into future `validation/` services
- `docx_writer.py` — Rendering backend only; must not decide future citation or bibliography truth
- `services/workflow_facade.py` — Migration buffer layer shared by GUI and CLI; must remain until both entrypoints use the same bottom-layer execution semantics

## Hard Constraints

- Hidden dual-write is prohibited
- Except for `output/<project_name>/_latest_job.json`, no code path may write `summary / checkpoint / outline / review / report` back into `output/<project_name>/`
- Real artifacts must be written once, inside the active job workspace
- Downstream code must read durable artifacts from the job workspace or registry, not from legacy project-root copies

## Pointer Atomicity Contract

Pointer updates are required to be atomic:

1. Write a temp file in the same target directory
2. Flush file contents
3. `fsync` the temp file
4. `rename` / `os.replace` over the destination

Any non-atomic pointer update is considered a migration bug.

## Source-of-Truth Matrix

| Domain | Current Source | Target Source | Compat Projection | Stop-Write Time |
|--------|---------------|---------------|-------------------|-----------------|
| Summaries | `*_summaries.json` | `paper_artifact.json` | `*_summaries.json` | Week 1 |
| Outline | `*_outline.md` | `outline.json` | `*_outline.md` | Week 5 |
| Review Draft | Checkpoint + Word | `review_draft_v2.json` | `*_review_checkpoint.json` | Week 3 |
| Citations | Word / regex | `citation_manifest_v2.json` | `citation_manifest_v1.json` | Week 3 |
| Validation Reports | Legacy validator | `validation/review_validator.py` outputs | TBD | Week 4 |
| Configuration | `Performance` section | `Validation` section | Bi-directional sync | Week 0 |
| Queue | N/A | `Queue` section | N/A | Week 5 |
| Stage1 Visual | N/A | `Stage1_Visual` section | N/A | Week 5 |
| Multimodal | N/A | `Multimodal` section | N/A | Week 5 |

## Write-Stop Timeline

- **Week 0**: Freeze this baseline document. Bi-directional config sync between `Validation` and `Performance` sections.
- **Week 1**: Stop writing real artifacts into `output/<project_name>/`. Keep only `_latest_job.json` in the project-root compatibility directory.
- **Week 2+**: Add new durable contracts only inside job workspace and registry.

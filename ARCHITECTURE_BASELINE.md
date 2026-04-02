# ARCHITECTURE_BASELINE.md

Last updated: `2026-04-02`

## Baseline

- Repository: `super-lee-hub/literature-review-generator`
- Branch: `main`
- Commit: `a3ba7ebfc10eaabda62d08ca3dfc47e7fafe2755`
- Scope frozen by this document: `Week 0` and the migration baseline for `Week 1+`

## Current Source Of Truth

Current runtime truth is still spread across legacy entrypoints and output files:

- `main.py`
  - Primary orchestration center
  - CLI argument routing
  - Output path decisions
  - Stage 1/2/3 checkpoint handling
- `validator.py`
  - Legacy stage-2 validation entrypoint
- `docx_writer.py`
  - Legacy review rendering backend
- `services/workflow_facade.py`
  - Thin GUI/CLI compatibility facade
- `output/<project>/`
  - Legacy mixed workspace containing summaries, checkpoints, outline, review, reports

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

- `main.py`
  - Compatibility entrypoint
  - Must stop accumulating new long-term domain logic
- `validator.py`
  - Compatibility entrypoint
  - Must delegate into future `validation/` services
- `docx_writer.py`
  - Rendering backend only
  - Must not decide future citation or bibliography truth
- `services/workflow_facade.py`
  - Migration buffer layer shared by GUI and CLI
  - Must remain until both entrypoints use the same bottom-layer execution semantics

## Current Directories

- `free_mode/`
- `gui/`
- `logs/`
- `output/`
- `preprocess/`
- `prompts/`
- `rag/`
- `services/`
- `tests/`

## Planned Directories

- `citation/`
- `citation/renderers/`
- `outline/`
- `validation/`

## Job Workspace Layout

Real outputs must live only inside a job workspace:

```text
output/<project_name>__<job_id>/
├─ artifacts/
├─ checkpoints/
├─ logs/
├─ reports/
└─ artifact_registry.json
```

Compatibility pointer directory:

```text
output/<project_name>/
└─ _latest_job.json
```

## Compatibility Projection

Project-root compatibility is restricted to pointers and projections only.

- Allowed:
  - `output/<project_name>/_latest_job.json`
- Forbidden:
  - Writing real summaries, checkpoints, outlines, reviews, validation artifacts, or reports back into `output/<project_name>/`

## Hard Constraints

- Hidden dual-write is prohibited.
- Except for `output/<project_name>/_latest_job.json`, no code path may write `summary / checkpoint / outline / review / report` back into `output/<project_name>/`.
- Real artifacts must be written once, inside the active job workspace.
- Downstream code must read durable artifacts from the job workspace or registry, not from legacy project-root copies.

## Pointer Atomicity Contract

Pointer updates are required to be atomic:

1. Write a temp file in the same target directory.
2. Flush file contents.
3. `fsync` the temp file.
4. `rename` / `os.replace` over the destination.

Any non-atomic pointer update is considered a migration bug.

## Write-Stop Timeline

- Week 0:
  - Freeze this baseline document.
- Week 1:
  - Stop writing real artifacts into `output/<project_name>/`.
  - Keep only `_latest_job.json` in the project-root compatibility directory.
- Week 2+:
  - Add new durable contracts only inside job workspace and registry.


# Workspace Layout & Artifact Registry

> Audience: Maintainers, AI agents.
> Source: AGENTS.md §7; TRUTH_SOURCES.md.

## Current Output Directory

Main output lives at `output/<project_name>__<job_id>/` with typical structure:

```text
output/<project_name>__<job_id>/
├─ artifacts/
│  ├─ <project>_summaries.json
│  ├─ <project>_summary_source_manifest.json
│  ├─ <project>_summary_reuse_report.json
│  ├─ <project>_literature_review_outline.md
│  ├─ paper_artifacts/
│  ├─ review_drafts/
│  ├─ citation_manifests/
│  └─ validation / repair JSON files
├─ checkpoints/
├─ logs/
├─ reports/
└─ artifact_registry.json
```

## Compatibility Directory

`output/<project_name>/` now typically only holds pointers (e.g. `_latest_job.json`). Do not assume it is the primary artifact directory.

## Preprocess Cache

Preprocess cache at `output/_preprocess_cache/`. Common cache files:

- `normalized.md`
- `plain_text.txt`
- `page_index.json`
- `chunks.json`
- `diagnostics.json`
- `structured.json`
- `prepare_manifest.json`

## Job Workspace Layout

Real artifacts must live only inside a job workspace:

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

## Hard Constraints

- Hidden dual-write is prohibited
- Except for `output/<project_name>/_latest_job.json`, no code path may write `summary / checkpoint / outline / review / report` back into `output/<project_name>/`
- Real artifacts must be written once, inside the active job workspace
- Downstream code must read durable artifacts from the job workspace or registry

## Pointer Atomicity Contract

1. Write a temp file in the same target directory
2. Flush file contents
3. `fsync` the temp file
4. `rename` / `os.replace` over the destination

Any non-atomic pointer update is considered a migration bug.

## Artifact Registry

`artifact_registry.json` is the central artifact registry within the job workspace, tracking dependencies and versions for all artifacts.

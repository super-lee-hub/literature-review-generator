# Workspace Layout & Artifact Registry

> Audience: Maintainers, AI agents.
> Source: AGENTS.md and [runtime truth sources](./truth-sources.md).

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

- `<content-addressed-source-key>/active_generation.json`
- `<content-addressed-source-key>/<generation>/normalized.md`
- `<content-addressed-source-key>/<generation>/plain_text.txt`
- `<content-addressed-source-key>/<generation>/page_index.json`
- `<content-addressed-source-key>/<generation>/chunks.json`
- `<content-addressed-source-key>/<generation>/diagnostics.json`
- `<content-addressed-source-key>/<generation>/structured.json`
- `<content-addressed-source-key>/<generation>/prepare_manifest.json`

The active pointer is published only after the generation's required files are
complete, fsynced, schema/fingerprint checked, and hashed. Missing, tampered,
or fingerprint-incompatible generations are rebuilt through a new staging
generation; an old active generation is never mixed with partially written
files.

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

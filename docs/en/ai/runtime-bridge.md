# AI Runtime Bridge

> Audience: AI agents, runtime developers.
> Source: AGENTS.md §5.5, §6.4, §7; TRUTH_SOURCES.md.

## Validation / Repair Pipeline

The project now has a dedicated validation / repair pipeline:

- `validation_report`
- `repair_plan`
- `repair_apply_result`

The user-visible entry is still primarily `--validate-review`, but internally there are more granular evidence resolver, summary recheck, and repair planner / apply structures.

## Stage 4 (Validation / Repair) Truth Sources

When enabled, the following appear:

- `validation_report*.json`
- `repair_plan_*.json`
- `repair_apply_result_*.json`
- Related patch records

## Job Workspace, Output Directory, and Cache

### Current Output Directory

Main output lives at `output/<project_name>__<job_id>/`:

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

### Compatibility Directory

`output/<project_name>/` now typically only holds pointers (e.g. `_latest_job.json`). Do not assume it is the primary artifact directory by default.

### Preprocess Cache

Preprocess cache lives at `output/_preprocess_cache/`. Common cache files: `normalized.md`, `plain_text.txt`, `page_index.json`, `chunks.json`, `diagnostics.json`, `structured.json`, `prepare_manifest.json`.

## AI-native Runtime Bridge

- `RuntimeJobSpec` adapts AI-native requests into canonical `JobRunRequest`
- `AgentRuntimeBridge` bootstraps workspace/latest-pointer handling locally and persists `source_bundle.json` + `runtime_stage_trace.json`
- Generation stages may be delegated to subagents, but workspace/artifact/validation transitions remain local and canonical
- This surface is additive: it does not replace the normal human CLI/GUI entrypoints

### Stage 7 Artifacts

- `source_bundle.json`: Normalized AI-native input/source snapshot
- `runtime_stage_trace.json`: Local-vs-subagent stage execution trace
- Canonical downstream artifacts persist through the same workspace/registry substrate as CLI/GUI runs

# Truth Sources and Compatibility Guide

> Audience: maintainers / AI agents.
> Use this document for runtime truth and compatibility reasoning, not as the primary end-user guide.

This document defines the canonical truth sources, compatibility projections, deprecated paths, and removal timeline for the auto-generate project.

## Main Truth Sources

### Stage 1: Paper Analysis
- **Primary Truth Source**: canonical `*_summaries.json`
- **Companion durable artifact**: `paper_artifact.json` when the workspace path is active
- **Fallback**: legacy summary projections normalized into the canonical summary schema
- **Key Artifacts**:
  - `*_summaries.json` (canonical summary structure)
  - `paper_artifact.json` (persistent paper analysis record when available)
  - `*_analyzed_papers.xlsx` (generated export, not the source of truth)

### Stage 2: Outline Generation
- **Primary Truth Source**: registered markdown outline artifact `*_literature_review_outline.md`
- **Fallback**: legacy output-folder markdown outline when the workspace/registry artifact is unavailable
- **Optional compatibility artifact**: `*_reviewed_outline.json` created only by explicit/manual adopt flow
- **Key Artifacts**:
  - `*_literature_review_outline.md` (downstream outline used by review generation)
  - `*_reviewed_outline.json` (manual compatibility artifact, not normal downstream default)

### Stage 3: Review Draft
- **Primary Truth Source**: `*_review_draft_v2.json` + `*_citation_manifest_v3.json`
- **Fallback**: Legacy review draft structure (marked as legacy in metadata)
- **Key Artifacts**:
  - `*_review_draft_v2.json` (with block structure, `block_source`, `span_map`, and durable section context)
  - `*_citation_manifest_v3.json` (structured citations as primary truth)
  - `*_citation_manifest_v2.json` (legacy-compatible compatibility artifact when still present)

### Stage 4: DOCX Generation
- **Primary Truth Source**: `*_review_draft_v2.json` + `*_citation_manifest_v3.json` (cited bibliography only)
- **Fallback**: Legacy summary-based bibliography (explicit legacy mode only)
- **Key Artifacts**:
  - `*_literature_review.docx` (generated from manifest cited references)

### Stage 5: Validation and Repair
- **Primary Truth Source**: `validation_report.json` + `repair_plan.json` + `repair_apply_result.json`
- **Key Artifacts**:
  - `validation_report.json` (review validation results)
  - `repair_plan.json` (mapping-first repair proposals)
  - `repair_apply_result.json` (repair application results)
  - `applied_patch_*.json` (individual patch records)

### Stage 6: Queue System
- **Primary Truth Source**: `queue.json` with complete task snapshots
- **Key Artifacts**:
  - `queue.json` (persistent queue storage)
  - `QueueJobSpec` (complete task snapshots with fingerprints and paths)
  - `QueueJobRuntime` (runtime state tracking with retry counts)

### Stage 7: AI-native Runtime Bridge
- **Primary Truth Source**: the active job workspace + artifact registry, driven by `RuntimeJobSpec` and `AgentRuntimeBridge`
- **Key Artifacts**:
  - `source_bundle.json` (normalized AI-native input/source snapshot)
  - `runtime_stage_trace.json` (local-vs-subagent stage execution trace)
  - canonical downstream artifacts persisted through the same workspace/registry substrate as CLI/GUI runs

## Compatibility Projections

### Field Compatibility
- **Canonical Fields**: Driven by `summary_schema.py` and the canonical stage-1 summary structure
- **Repair ownership hints**: `FIELD_OWNER_REGISTRY` in `validation/summary_recheck.py`
- **Legacy Fields**: Only supported in projection / normalization layers
- **Mapping**: Old fields are mapped to canonical structure during normalization

### API Compatibility
- **Primary_Reader_API**: Used for literature analysis
- **Backup_Reader_API**: Fallback for extraction failures
- **Writer_API**: Used for review section generation / regeneration
- **Outline_API**: Used for outline generation
- **Free_Mode_API**: Used for free mode planning
- **Validator_API**: Used for review validation
- **Optional Week-5 outline-review helpers**: not part of the normal runtime contract

### Input/Output Compatibility
- **PDF Folder Mode**: Direct input of PDF files
- **Zotero Mode**: Input via `Zotero report + Zotero library`
- **Queue Mode**: Batch processing via queue files
- **AI-native Mode**: Repo-local Codex skill / runtime bridge that still writes into the same workspace layout
- **Primary Durable Output Directory**: `output/<project_name>__<job_id>/`
- **Compatibility Pointer Directory**: `output/<project_name>/` (for pointers such as `_latest_job.json`)
- **Preprocess Cache**: All preprocess artifacts in `output/_preprocess_cache/`

## Deprecated Paths

### Stage 1
- **Deprecated**: Legacy summary structure without canonical schema
- **Deprecated**: Regex-based citation extraction as primary source
- **Deprecated**: OCR without preprocess validation

### Stage 2
- **Deprecated**: Auto-accept/auto-adopt of outline
- **Deprecated**: Using `Writer_API` for outline generation (should use `Outline_API`)

### Stage 3
- **Deprecated**: APA in-text citations without structured refs
- **Deprecated**: Review draft without `block_source` and `span_map`

### Stage 4
- **Deprecated**: Summary-based bibliography (use manifest cited bibliography)
- **Deprecated**: DOCX generation without citation manifest

## Removal Timeline

### Phase 1: Current Release (v1.0)
- **Status**: All deprecated paths still available as fallback
- **Action**: Mark deprecated paths in metadata and logs

### Phase 2: Next Minor Release (v1.1)
- **Status**: Deprecated paths disabled by default, but can be re-enabled via config
- **Action**: Add warning messages for deprecated paths usage

### Phase 3: Next Major Release (v2.0)
- **Status**: Deprecated paths removed entirely
- **Action**: Clean up codebase and remove compatibility layers

## Key Implementation Notes

### Citation Object Main Chain
- Structured citations in `citation_manifest_v3` are the primary truth source
- Regex-based citations are only allowed as legacy fallback
- All citations must be mapped to canonical paper keys
- DOCX bibliography only includes actually cited items

### Validation and Repair
- `ReviewValidator` uses `review_draft + citation_manifest + preprocess/visual evidence + paper metadata`
- `SummaryRechecker` is canonical-only with `FIELD_OWNER_REGISTRY`-driven field attribution
- Repair root cause classification:
  - `citation_mapping_error -> manifest mapping + rerender`
  - `summary_drift -> targeted summary recheck`
  - `review_drift -> block/span patch`
- Repair application triggers targeted recheck and persists secondary report

### Queue System
- `QueueJobSpec/QueueJobRuntime` extended with `source_snapshot`, `fingerprints`, `current_stage`, `paths`, and `produced_artifacts`
- GUI supports complete queue operations: add, delete, reorder, save, load, run, cancel, retry, resume
- CLI supports batch queue files and single task override
- Default queue policy: serial execution, fail-continue, explicit recovery, retry failed items

### AI-native Runtime Bridge
- `RuntimeJobSpec` adapts AI-native requests into canonical `JobRunRequest`
- `AgentRuntimeBridge` bootstraps workspace/latest-pointer handling locally and persists `source_bundle.json` + `runtime_stage_trace.json`
- Generation stages may be delegated to subagents, but workspace/artifact/validation transitions remain local and canonical
- This surface is additive: it does not replace the normal human CLI/GUI entrypoints

### Optional Outline Review Compatibility Surface
- `Outline_API` remains the normal outline-generation API
- Downstream review generation loads the markdown outline artifact from the registry/workspace path
- `--outline-adopt` is an explicit/manual compatibility command, not part of the default workflow
- Any reviewed-outline JSON artifact is optional and not preferred by the normal runtime path

## Testing and Validation

### Test Coverage
- Full pytest suite must pass with clean exit
- Windows-specific tests for pymupdf4llm/onnxruntime stability
- Integration tests for validation/repair pipeline
- End-to-end tests for queue system

### Validation Scenarios
- **Citation Validation**: Structured citations on disk, manifest priority, legacy regex only in compat scenarios, bibliography with only cited items
- **Validation/Repair**: Five conclusion types, root cause classification, mapping repair effects, auto-recheck after repair
- **Queue Validation**: GUI/CLI mixed tasks, fail-continue, cancel, resume, `--queue-file` and `--zotero-report` functionality
- **Outline Runtime**: Main generation, markdown outline loading, downstream review generation

### Manual Validation
- **PDF analyze** path
- **Zotero analyze** path
- **Queue run_all** path
- **Validate+repair** path
- **Reviewed outline adopt** path (optional compatibility/manual flow only)

## Conclusion

This document serves as the single source of truth for the auto-generate project's data flow, compatibility layers, and deprecation timeline. All future development should adhere to these guidelines to ensure a consistent and maintainable codebase.

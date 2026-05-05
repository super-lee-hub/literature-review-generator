# Compatibility Paths & Removal Timeline

> Audience: Maintainers, AI agents.
> Source: TRUTH_SOURCES.md.

## Compatibility Projections

### Field Compatibility
- **Canonical Fields**: Driven by `summary_schema.py` and the canonical stage-1 summary structure
- **Repair ownership hints**: `FIELD_OWNER_REGISTRY` in `validation/summary_recheck.py`
- **Legacy Fields**: Only supported in projection / normalization layers

### API Compatibility
- `Primary_Reader_API`: Literature analysis
- `Backup_Reader_API`: Fallback for extraction failures
- `Writer_API`: Review section generation / regeneration
- `Outline_API`: Outline generation
- `Free_Mode_API`: Free mode planning
- `Validator_API`: Review validation

### Input/Output Compatibility
- PDF Folder Mode, Zotero Mode, GUI Queue Mode, AI-native Mode
- Primary Durable Output Directory: `output/<project_name>__<job_id>/`
- Compatibility Pointer Directory: `output/<project_name>/` (for `_latest_job.json` only)
- Preprocess Cache: `output/_preprocess_cache/`

## Deprecated Paths

### Stage 1
- Legacy summary structure without canonical schema
- Regex-based citation extraction as primary source
- OCR without preprocess validation

### Stage 2
- Auto-accept/auto-adopt of outline
- Using `Writer_API` for outline generation (should use `Outline_API`)

### Stage 3
- APA in-text citations without structured refs
- Review draft without `block_source` and `span_map`

### Stage 4
- Summary-based bibliography (use manifest cited bibliography)
- DOCX generation without citation manifest

## Removal Timeline

### Phase 1: Current Release (v1.0)
- All deprecated paths still available as fallback
- Mark deprecated paths in metadata and logs

### Phase 2: Next Minor Release (v1.1)
- Deprecated paths disabled by default, re-enable via config
- Add warning messages for deprecated paths usage

### Phase 3: Next Major Release (v2.0)
- Deprecated paths removed entirely
- Clean up codebase and remove compatibility layers

## Key Implementation Notes

### Citation Object Main Chain
- Structured citations in `citation_manifest_v3` are the primary truth source
- Regex-based citations are only allowed as legacy fallback
- All citations must be mapped to canonical paper keys
- DOCX bibliography only includes actually cited items

### Validation and Repair
- `ReviewValidator` uses `review_draft + citation_manifest + preprocess/visual evidence + paper metadata`
- Repair root cause classification: `citation_mapping_error` (manifest mapping + rerender), `summary_drift` (targeted summary recheck), `review_drift` (block/span patch)

### GUI Queue System
- Default queue policy: serial execution, fail-continue, explicit recovery, retry of failed/cancelled GUI jobs
- CLI and AI-native runtime are direct-run surfaces and do not expose public queue workflows

### Optional Outline Review Compatibility
- `--outline-adopt` is an explicit/manual compatibility command, not part of the default workflow

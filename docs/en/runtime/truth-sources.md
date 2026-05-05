# Truth Sources & Data Contracts

> Audience: Maintainers, AI agents.
> Source: TRUTH_SOURCES.md; AGENTS.md §5-7.

This document defines the canonical truth sources, data contracts, and compatibility projections for the auto-generate project.

## Main Truth Sources

### Stage 1: Paper Analysis
- **Primary Truth Source**: canonical `*_summaries.json`
- **Companion durable artifact**: `paper_artifact.json` when the workspace path is active
- **Fallback**: legacy summary projections normalized into the canonical summary schema
- **Key Artifacts**: `*_summaries.json`, `paper_artifact.json`, `*_analyzed_papers.xlsx` (export, not source of truth)

Canonical summary core blocks: `routing`, `paper_metadata`, `core_analysis`, `specialized_details`, `quality_audit`

### Stage 2: Outline Generation
- **Primary Truth Source**: registered markdown outline artifact `*_literature_review_outline.md`
- **Fallback**: legacy output-folder markdown outline when workspace/registry artifact unavailable

### Stage 3: Review Draft
- **Primary Truth Source**: `*_review_draft_v2.json` + `*_citation_manifest_v3.json`
- **Fallback**: legacy review draft structure (marked as legacy in metadata)
- `review_draft_v2 + citation_manifest_v3` are the structured truth sources; `docx` is the final export

### Stage 4: DOCX Generation
- **Primary Truth Source**: `*_review_draft_v2.json` + `*_citation_manifest_v3.json` (cited bibliography only)

### Stage 5: Validation and Repair
- **Primary Truth Source**: `validation_report.json` + `repair_plan.json` + `repair_apply_result.json`

### Stage 6: GUI Queue System
- **Primary Truth Source**: GUI-internal `queue.json` with immutable workflow submission snapshots

### Stage 7: AI-native Runtime Bridge
- **Primary Truth Source**: active job workspace + artifact registry, driven by `RuntimeJobSpec` and `AgentRuntimeBridge`
- **Key Artifacts**: `source_bundle.json`, `runtime_stage_trace.json`

## Current Main Pipeline

### Input Modes
- PDF folder mode: scans a folder of PDFs directly
- Zotero mode: uses `Paths.zotero_report` + `Paths.library_path`

### Stage 1 Chain
1. Collect source paper descriptors → 2. Resolve and locate PDFs → 3. Preprocess layer → 4. Build stage1 input → 5. Reader API generates structured summary → 6. Normalize to canonical summary schema → 7. Write `*_summaries.json` → 8. Write `paper_artifact` → 9. Output Excel

### Stage 2 Chain
Main output: `*_literature_review_outline.md`. Default API: `Outline_API`.

### Stage 3 Chain
`review_draft_v2` → `citation_manifest_v3` → DOCX. `review_draft_v2 + citation_manifest_v3` are the primary structured truth sources; `docx` is the final export.

### Validation / Repair Chain
Dedicated pipeline: `validation_report` → `repair_plan` → `repair_apply_result`

## Data Contracts

### Stage 1
- Primary truth: canonical `*_summaries.json`
- Companion durable artifact: `paper_artifacts/*.json`
- Structure fact source: `summary_schema.py`

### Stage 2
- Primary truth: `*_literature_review_outline.md`

### Stage 3
- Primary truth: `review_drafts/*_review_draft_v2.json`
- Citation primary truth: `citation_manifests/*_citation_manifest_v3.json`

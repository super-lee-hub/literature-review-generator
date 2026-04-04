# LLM Literature Review Generator

[中文说明](./README.zh-CN.md) | [English Guide](./README.en.md)

This project is a local AI literature analysis and literature review generation workbench that batch-analyzes PDFs or Zotero exports and generates literature review outlines and full drafts.

## Quick Start

```bash
pip install -r requirements.txt
python main.py --setup
```

For development setup, testing, type checking, and Playwright GUI tests, see [DEVELOPMENT.md](./DEVELOPMENT.md).

Common commands:

```bash
python main.py --pdf-folder "D:\YourPdfFolder"
python main.py --pdf-folder "D:\YourPdfFolder" --generate-outline
python main.py --pdf-folder "D:\YourPdfFolder" --generate-review
python main.py --pdf-folder "D:\YourPdfFolder" --generate-section <section_number>
python main.py --pdf-folder "D:\YourPdfFolder" --retry-review-failed
python main.py --gui
```

Replace `<section_number>` with the outline section number you actually want to regenerate.

Detailed guides:

- 中文说明: [README.zh-CN.md](./README.zh-CN.md)
- English: [README.en.md](./README.en.md)

## Current Architecture (Week 0-5 Completed)

This project has evolved from a simple three-stage script into a structured workbench. The Week 0-5 main skeleton is now complete:

### Core Infrastructure (Week 1)
- **Job Workspace**: Isolated execution environment with artifact registry
- **Artifact Registry**: Durable tracking of all generated artifacts
- **Workflow Facade**: Unified entry for both CLI and GUI

### Core Contracts (Week 2-3)
- **Review Draft V2**: Block/span structured review content
- **Citation Manifest V2**: Truth-source layer for citations with occurrence/cluster semantics
- **Paper Artifact**: Durable paper analysis records
- **Validation Pipeline**: Evidence-based validation with evidence resolver

### Repair Pipeline (Week 4)
- **Repair Planner**: Identifies issues without immediate application
- **Repair Apply**: Applies fixes only when explicitly approved
- **Integration**: Full pipeline integration

### Outline Critique & Arbitration (Week 5)
- **JSON-first Outline**: Structured outline representation
- **Critique**: Automated outline quality critique
- **Arbitration**: Conflict resolution between multiple critiques
- **Adopt**: Safe adoption of arbitration results

## Workspace Layout

Week 1 introduces a job workspace layout. Real artifacts now live under:

```text
output/<project_name>__<job_id>/
  artifacts/
  checkpoints/
  logs/
  reports/
  artifact_registry.json
```

`output/<project_name>/` is now reserved for compatibility pointers such as `_latest_job.json`. The project root output directory should not receive direct writes for summaries, checkpoints, outlines, reviews, or reports.

## Resume Semantics

- `stage1_progress_snapshot.json` is written alongside successful `*_summaries.json` saves.
- `summaries-only` states are treated as `weak_resumable`.
- Fingerprint mismatches are treated as `non_resumable`.
- CLI and GUI both enter the same Week 1 execution boundary through `workflow_facade` and the shared config compatibility layer.

## Persistent Queue (Infrastructure Only)

The project includes a persistent queue infrastructure layer (see `services/queue_service.py`), but it is **not yet integrated into the main GUI or CLI workflow**:

- `QueueJobSpec`: Job specification with parameters and dependencies
- `QueueJobRuntime`: Runtime state tracking with retry count
- `PersistentQueueService`: JSON-based persistent queue storage
- Supports: add_job, update_job_state, retry_failed_jobs, etc.

**Note**: The queue system is currently storage-layer only. Full productization (GUI/CLI integration, queue runner, etc.) is planned for a future release.

## Citation Truth-Source (Week 6 Runtime Upgrade)

The citation system has been upgraded to V2 as the **primary runtime truth source**:

### Architecture
- **`CitationManifestV2`** is now the primary durable artifact (registered in artifact registry)
- **`CitationManifestV1`** is kept as explicit compatibility projection only
- **Occurrence/Cluster/Bibliography** structure replaces flat citations list

### Key Components
- `CitationOccurrence`: Block/span-level citation occurrences with context
- `CitationCluster`: Paper-level citation clusters aggregating occurrences
- `BibliographyEntry`: Bibliography entries with `is_cited` flag and cluster linkage
- `build_citation_manifest_v2_from_review_draft()`: Builds v2 from review_draft_v2 blocks

### Consumer Alignment
- **Validator** (`validation/review_validator.py`): Consumes `occurrences` as primary input (v2), falls back to `citations` (v1)
- **Repair Pipeline** (`services/repair_integration.py`): Receives v2 data through validation reports
- **Main Flow** (`main.py`): Produces v2 as primary, v1 as projection for compatibility

### File Locations
```
output/<project>__<job_id>/
  citation_manifests/
    <project>_citation_manifest_v2.json   # Primary truth source
    <project>_citation_manifest_v1.json   # Compatibility projection
```

### Backward Compatibility
- V1 manifests auto-migrate to V2 via `migrate_v1_to_v2()`
- Validator maintains fallback to v1 `citations` field for legacy data

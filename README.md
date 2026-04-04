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

## Persistent Queue (MVP)

The project now includes a persistent queue system (see `services/queue_service.py`):

- `QueueJobSpec`: Job specification with parameters and dependencies
- `QueueJobRuntime`: Runtime state tracking with retry count
- `PersistentQueueService`: JSON-based persistent queue storage
- Supports: add_job, update_job_state, retry_failed_jobs, etc.

## Citation Truth-Source

The citation system has been upgraded to V2 (see `services/citation_manifest.py`):

- `CitationOccurrence`: Block/span-level citation occurrences
- `CitationCluster`: Paper-level citation clusters
- `BibliographyEntry`: Bibliography entries with `is_cited` flag
- `get_cited_bibliography()`: Generates bibliography only from actually cited papers
- Backward compatible: V1 manifests can be migrated to V2

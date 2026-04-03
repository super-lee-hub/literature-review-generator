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

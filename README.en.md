# auto-generate English Guide

> The root README (`README.md`) is now a landing page / router. This file owns the full English user guide.

## 1. Document split

- `README.md`: landing page / router / choose-your-entry document
- `README.zh-CN.md`: full Chinese user guide
- `README.en.md`: full English user guide (this file)
- `AGENTS.md`: AI + maintainer handoff document
- `TRUTH_SOURCES.md`: deeper runtime truth, durable artifacts, and compatibility notes
- `FEATURE_MATRIX.md`: implementation-status matrix

## 2. What this project is

`auto-generate` is now a local AI literature-analysis and review-writing workbench, not just the original single-script generator.

It supports two main input modes:

- **PDF folder mode**: scan a folder of PDF papers directly
- **Zotero mode**: use `Zotero report + Zotero library`

It now has three main entry surfaces:

- **CLI**: `python main.py ...`
- **GUI**: `python launch_gui.py`
- **Codex / OMX skill**: the repo-local `auto-generate-orchestrator`, for AI-native execution inside Codex

The main pipeline still follows three classic stages:

1. **Stage 1: paper analysis** -> structured `summaries.json`
2. **Stage 2: outline generation** -> `outline.md`
3. **Stage 3: review generation** -> `docx`

Around that core pipeline, the project now also includes:

- a local GUI workbench
- job workspaces / artifact registry / resume state
- GUI-managed serial background queueing and recovery
- PDF preprocessing cache, OCR fallback, and `normalized.md` intermediates
- stage-1 summary reuse across runs
- free-mode profile / idea flows
- persisted review-draft + citation-manifest artifacts
- optional validation / repair pipeline
- optional local RAG

## 3. Current capabilities

### 3.1 What you can do

- Batch-analyze papers from a PDF folder
- Work from a Zotero report plus library path
- Generate stage-1 summaries, stage-2 outlines, and stage-3 review documents
- Regenerate one section only
- Retry failed papers or failed review sections only
- Reuse historical `summaries.json`
- Merge multiple historical `summaries.json` files into one downstream run
- Use either the GUI or the direct-run CLI
- Use the repo-local Codex / OMX skill entry surface for AI-native execution
- Submit GUI workflow jobs into the built-in serial background queue
- Run optional review validation

### 3.2 How the project currently works

- **GUI and CLI share the same execution chain** instead of owning two separate engines.
- **Codex skill mode is a third additive surface**: it does not replace GUI / CLI, and it still reuses the same workspace / artifact / validation substrate.
- The real durable outputs now primarily live inside **job workspaces**, not the old mixed `output/<project>/` layout.
- Word and Excel files are important exports, but many runtime truths now live in structured JSON artifacts.

## 4. Install and initialize

### 4.1 Install dependencies

```bash
pip install -r requirements.txt
```

### 4.2 Run setup

```bash
python main.py --setup
```

### 4.3 Launch the GUI

```bash
python launch_gui.py
```

For development:

```bash
python launch_gui.py --reload --no-show
```

## 5. Quick start

### 5.1 Shortest CLI path (PDF folder)

```bash
python main.py --pdf-folder "D:\papers" --analyze-only
python main.py --pdf-folder "D:\papers" --generate-outline
python main.py --pdf-folder "D:\papers" --generate-review
```

Or run the full pipeline:

```bash
python main.py --pdf-folder "D:\papers" --run-all
```

It is usually better to give the run a stable project name:

```bash
python main.py --pdf-folder "D:\papers" --project-name "my_review" --run-all
```

### 5.2 Shortest GUI path

1. Run `python launch_gui.py`
2. Fill in paths, APIs, and models on the Setup pages
3. Go to Workflow, choose PDF or Zotero mode
4. Submit from Workflow; the GUI automatically queues jobs serially in the background while the form stays editable

## 6. Common workflows

### 6.1 PDF folder mode

```bash
python main.py --pdf-folder "D:\papers" --analyze-only
python main.py --pdf-folder "D:\papers" --generate-outline
python main.py --pdf-folder "D:\papers" --generate-review
python main.py --pdf-folder "D:\papers" --run-all
```

### 6.2 Zotero mode

Set these in `config.ini` or the GUI first:

- `Paths.zotero_report`
- `Paths.library_path`

You can also pass them directly:

```bash
python main.py --project-name "my_review" --zotero-report "D:\zotero_report.txt" --library-path "D:\ZoteroLibrary" --analyze-only
python main.py --project-name "my_review" --generate-outline
python main.py --project-name "my_review" --generate-review
```

### 6.3 Reuse existing stage-1 summaries

There are now two reuse patterns:

1. **Explicit downstream loading of historical summary files**
   - `--summary-file`
   - repeatable `--summary-source <path>`
2. **Automatic incremental stage-1 reuse across historical runs**
   - `--reuse-stage1`
   - repeatable `--reuse-summary-file <path>`

Automatic reuse currently tries matches in this order:

1. exact DOI
2. exact canonical paper key
3. unique high-confidence `title + first author + year`

Examples:

```bash
python main.py --project-name "subset_outline" --summary-file "D:\subset\subset_summaries.json" --generate-outline

python main.py --project-name "subset_review" --summary-file "D:\subset\subset_a_summaries.json" --summary-source "D:\subset\subset_b_summaries.json" --generate-review

python main.py --pdf-folder "D:\new_papers" --project-name "pdf_overlap" --analyze-only --reuse-stage1

python main.py --pdf-folder "D:\new_papers" --project-name "pdf_overlap" --analyze-only --reuse-stage1 --reuse-summary-file "D:\cache\curated_summaries.json"
```

### 6.4 Partial reruns and failure recovery

```bash
python main.py --pdf-folder "D:\papers" --generate-section 3
python main.py --pdf-folder "D:\papers" --retry-failed
python main.py --pdf-folder "D:\papers" --retry-review-failed
python main.py --project-name "my_review" --validate-review
```

Meaning:

- `--generate-section <n>`: regenerate one section only
- `--retry-failed`: retry only failed stage-1 papers
- `--retry-review-failed`: retry only failed or missing review sections
- `--validate-review`: run an extra validation pass; lower-level validation / repair artifacts are written into the active workspace

### 6.5 GUI background queue

Queueing is now a **GUI-first** interaction model. On the Workflow page, buttons such as "Analyze only", "Generate outline", "Generate review", and "Run all" submit jobs into the GUI's internal persistent serial queue. The queue drains in the background, and the form remains editable so you can configure the next job immediately.

The CLI no longer exposes public queue commands. Command-line usage is direct-run only, e.g. `--analyze-only`, `--generate-outline`, `--generate-review`, and `--run-all`. The AI-native Codex / OMX skill also runs directly and stays out of the GUI queue.

## 7. Advanced capabilities

These are part of the current product surface, but not always required for a first run:

- `auto-generate-orchestrator`: repo-local skill for AI-native execution inside Codex / OMX
- `--prime-with-folder` + `--concept`: concept priming
- `--free-mode-profile`: load a free-mode profile JSON
- `--free-mode-idea`: pass a free-mode idea as text
- `--merge`: merge multiple `summaries.json` files into one
- `--outline-adopt`: outline-adopt compatibility path (manual / explicit flow, not the default main chain)
- preprocess cache artifacts such as `normalized.md`, `page_index.json`, `diagnostics.json`, and `chunks.json`
- optional local RAG built during preprocessing

### 7.1 Codex / skill AI-native entry surface

If you are operating this repository from Codex / OMX instead of manually using the GUI or CLI, you can use the repo-local `auto-generate-orchestrator` skill.

That surface is:

- a **third entry surface**, not a replacement for GUI / CLI
- still workspace-compatible with the existing `job workspace`, `artifact registry`, `resume`, and `validation / repair` substrate
- better suited for AI-native orchestration where Codex normalizes inputs, runs stages, persists artifacts, and validates results directly inside the repo

When you use that surface, the workspace may also contain:

- `artifacts/source_bundle.json`
- `artifacts/runtime_stage_trace.json`

If you are a regular end user, start with the GUI / CLI mental model first. Reach for the skill surface when you are already working inside Codex.

## 8. Output layout and key artifacts

### 8.1 Primary output layout

Most real outputs now live under:

```text
output/<project_name>__<job_id>/
```

Typical structure:

```text
output/<project_name>__<job_id>/
?? artifacts/
?  ?? <project>_summaries.json
?  ?? <project>_summary_source_manifest.json
?  ?? <project>_summary_reuse_report.json
?  ?? <project>_literature_review_outline.md
?  ?? paper_artifacts/
?  ?? review_drafts/
?  ?? citation_manifests/
?  ?? validation / repair JSON artifacts (when enabled)
?? checkpoints/
?? logs/
?? reports/
?? artifact_registry.json
```

### 8.2 Compatibility directory

```text
output/<project_name>/
```

This directory is now mainly used for pointers such as:

- `_latest_job.json`

It should not be your first assumption for where the real outputs live.

### 8.3 Common exports

- `reports/*_analyzed_papers.xlsx`
- `reports/*_literature_review.docx`
- `reports/*_failed_papers_report.txt`
- `checkpoints/*_review_checkpoint.json`
- `artifacts/review_drafts/*_review_draft_v2.json`
- `artifacts/citation_manifests/*_citation_manifest_v3.json`

### 8.4 Preprocess cache

```text
output/_preprocess_cache/
```

Common cached files:

- `normalized.md`
- `plain_text.txt`
- `page_index.json`
- `chunks.json`
- `diagnostics.json`
- `structured.json`
- `prepare_manifest.json`

### 8.5 AI-native runtime extras

When the repo-local Codex skill surface is used, the active workspace may also include:

- `artifacts/source_bundle.json`: normalized source intake snapshot for the AI-native run
- `artifacts/runtime_stage_trace.json`: stage trace that distinguishes local runtime steps from subagent generation steps

## 9. Configuration guidance

Recommended split:

- put **sensitive values** in `.env`
- put **non-sensitive runtime settings** in `config.ini`

Important config sections include:

- `Paths`
- `Primary_Reader_API`
- `Backup_Reader_API`
- `Writer_API`
- `Outline_API`
- `Free_Mode_API`
- `Validator_API`
- `Performance`
- `Preprocess`
- `Retry_Settings`
- `Stage2_Retry`
- `Validation`
- `Styling`
- `GUI`
- `API_Parameters`

Important environment variables include:

- `LLM_PRIMARY_READER_API`
- `LLM_BACKUP_READER_API`
- `LLM_WRITER_API`
- `LLM_OUTLINE_API`
- `LLM_FREE_MODE_API`
- `LLM_VALIDATOR_API`
- `MINERU_*`

## 10. Troubleshooting shortcuts

- **First run**: start with `--analyze-only`
- **Want the GUI**: run `python launch_gui.py`
- **Want the repo to run itself from Codex**: use the repo-local `auto-generate-orchestrator` skill
- **Cannot find outputs**: check `output/<project_name>__<job_id>/` first
- **Need partial repair**: use `--generate-section` or `--retry-review-failed`
- **Want incremental stage-1 reuse**: use `--reuse-stage1`
- **Need deeper runtime truth**: read `TRUTH_SOURCES.md`
- **Need AI / maintainer handoff context**: read `AGENTS.md`

## 11. For developers and maintainers

If you are here to work on the repository rather than just run it, start with:

1. `AGENTS.md`
2. `TRUTH_SOURCES.md`
3. `FEATURE_MATRIX.md`
4. `summary_schema.py`
5. `services/job_runner.py`
6. `main.py`
7. `gui/app.py`
8. `.codex/skills/auto-generate-orchestrator/SKILL.md`
9. `runtime/orchestrator.py`
10. `preprocess/service.py`
11. `validation/review_validator.py`

## 12. One-line summary

Think of this repository as:

- a local AI literature-analysis / review-writing workbench with GUI, CLI, and a repo-local Codex skill entrypoint
- a project that already has product-style capabilities such as workspaces, artifacts, GUI background queueing, reuse, and validation
- a user surface documented mainly in this file and the Chinese README
- a deeper runtime reality documented in `AGENTS.md` and `TRUTH_SOURCES.md`

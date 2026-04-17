# LLM Literature Review Generator

[Chinese guide](./README.zh-CN.md) | [Project landing page](./README.md)

This is a local AI literature workbench for ordinary users. You can give it a folder of PDFs or a Zotero library, then generate paper summaries, an outline, and a full literature review.

## What you can do

- Analyze a folder of PDF papers
- Use a Zotero report plus library path
- Generate paper summaries, an outline, and a full review
- Retry failed papers or failed review sections
- Regenerate one section when only part of the review needs a fix
- Use either the command line or the GUI
- Queue multiple jobs and run them later

## Before you start

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Run setup once:

```bash
python main.py --setup
```

3. If your project needs API keys or file paths, fill them in through `config.ini` or the GUI setup page.

## Quick start

If you already have a PDF folder, this is the easiest path:

```bash
python main.py --pdf-folder "D:\YourPdfFolder" --analyze-only
```

Then continue with:

```bash
python main.py --pdf-folder "D:\YourPdfFolder" --generate-outline
python main.py --pdf-folder "D:\YourPdfFolder" --generate-review
```

Or run the full pipeline:

```bash
python main.py --pdf-folder "D:\YourPdfFolder" --run-all
```

If you want a named output, add `--project-name`:

```bash
python main.py --pdf-folder "D:\YourPdfFolder" --project-name "my_review" --run-all
```

## Use PDF folders

Use this mode when your papers are already in one folder.

Common commands:

```bash
python main.py --pdf-folder "D:\YourPdfFolder" --analyze-only
python main.py --pdf-folder "D:\YourPdfFolder" --generate-outline
python main.py --pdf-folder "D:\YourPdfFolder" --generate-review
python main.py --pdf-folder "D:\YourPdfFolder" --run-all
```

If you only need to redo one section:

```bash
python main.py --pdf-folder "D:\YourPdfFolder" --generate-section <section_number>
```

Use the real section number from your outline.

If some papers failed in stage 1:

```bash
python main.py --pdf-folder "D:\YourPdfFolder" --retry-failed
```

## Use Zotero

If you use Zotero, set these paths in `config.ini` or the GUI:

- `Paths.zotero_report`
- `Paths.library_path`

You can also pass them on the command line when you start a run:

```bash
python main.py --project-name "my_review" --zotero-report "D:\\zotero_report.txt" --library-path "D:\\ZoteroLibrary" --analyze-only
```

Then run the same main flows:

```bash
python main.py --project-name "my_review" --analyze-only
python main.py --project-name "my_review" --generate-outline
python main.py --project-name "my_review" --generate-review
```

## Reuse existing stage-1 summaries

You can now reuse previous stage-1 work in two ways:

1. **Multi-source summary loading for downstream steps**
   - Use `--summary-file` for the common single-file case, or add repeatable `--summary-source <path>` entries when you want to combine multiple historical `summaries.json` files for `--generate-outline`, `--generate-review`, `--generate-section`, or `--validate-review`.
   - The selected summaries are merged and materialized into the current workspace so later downstream commands stay reproducible.
2. **Automatic stage-1 reuse for overlapping future runs**
   - Use `--reuse-stage1` during stage 1 (`--analyze-only` or `--run-all`).
   - The program now scans the configured `Paths.output_path` automatically, including both current workspaces and compatible legacy `output/<project>/<project>_summaries.json` layouts.
   - Matching priority is: exact DOI -> exact canonical paper key -> unique high-confidence `title + first author + year`.
   - Only unique high-confidence matches are reused automatically. Ambiguous matches are reported and then analyzed normally.
   - You can still add extra reuse pools outside `output_path` with repeatable `--reuse-summary-file <path>`.

Examples:

```bash
# Generate a subset outline from one explicit summary file
python main.py --project-name "subset_outline" --summary-file "D:\subset\subset_summaries.json" --generate-outline

# Generate a review from multiple historical summary files
python main.py --project-name "subset_review" --summary-file "D:\subset\subset_a_summaries.json" --summary-source "D:\subset\subset_b_summaries.json" --generate-review

# Stage 1 automatically scans historical outputs and reuses overlapping papers when the identity match is unique
python main.py --pdf-folder "D:\new_papers" --project-name "pdf_overlap" --analyze-only --reuse-stage1

# The same automatic reuse works in Zotero mode
python main.py --project-name "zotero_overlap" --zotero-report "D:\zotero_report.txt" --library-path "D:\ZoteroLibrary" --analyze-only --reuse-stage1

# Add an extra curated reuse pool outside the configured output directory
python main.py --pdf-folder "D:\new_papers" --project-name "pdf_overlap" --analyze-only --reuse-stage1 --reuse-summary-file "D:\cache\curated_summaries.json"
```

Rules:
- Automatic cross-run reuse is no longer limited to DOI. It now reuses exact DOI matches first, then exact canonical paper keys, then unique `title + first author + year` matches.
- If more than one historical candidate matches at the same priority level, the paper is **not** reused automatically; it is listed as ambiguous in the reuse report and then analyzed normally.
- Papers that still have no unique reusable match are analyzed normally, so partial overlap now becomes an incremental run instead of a forced full rerun.
- If you want to keep both a full-set run and a subset run, use different `--project-name` values so they do not overwrite each other's workspace state.

## GUI

Start the GUI with:

```bash
python launch_gui.py
```

Use the GUI if you prefer clicking through setup, analysis, queue management, and progress tracking.

## Queue runs

Queue mode is useful when you want to prepare multiple jobs first and run them later.

Common queue commands:

```bash
python main.py --queue-add --pdf-folder "D:\YourPdfFolder" --project-name "my_review" --analyze-only
python main.py --queue-run
python main.py --queue-list
python main.py --queue-cancel <job_id>
python main.py --queue-retry <job_id>
python main.py --queue-clear
```

If you want to use a specific queue file:

```bash
python main.py --queue-file "custom_queue_file.json" --queue-list
```

If you want to load several queue files at once:

```bash
python main.py --queue-run --queue-files "queue1.json" "queue2.json"
```

## Optional features

These are useful, but not required for a basic run:

- `--prime-with-folder` + `--concept`: warm up the run with a concept folder
- `--free-mode-profile`: load a free-mode profile JSON
- `--free-mode-idea`: pass a free-mode idea text directly
- `--summary-source`: add extra downstream `summaries.json` inputs beyond `--summary-file`
- `--merge`: merge multiple `summaries.json` files as a compatibility utility
- `--validate-review`: validate a generated review when you want an extra check
- `--outline-adopt`: use a manually adopted outline when needed
- `--cleanup`: remove old workspace files and keep the latest job files
- `--retry-review-failed`: retry failed or missing review sections only

## Output files

Most current results are written to the active job workspace:

```text
output/<project_name>__<job_id>/
```

Typical files:

- `artifacts/*_summaries.json`
- `artifacts/*_summary_source_manifest.json`
- `artifacts/*_summary_reuse_report.json`
- `artifacts/*_literature_review_outline.md`
- `reports/*_analyzed_papers.xlsx`
- `reports/*_literature_review.docx`
- `reports/*_failed_papers_report.txt`
- `checkpoints/*_review_checkpoint.json`
- `logs/`

If you see a compatibility folder under `output/<project_name>/`, treat it as a pointer or legacy path, not the first place to look.

## Troubleshooting

- Not sure where to begin? Start with `--analyze-only`.
- Want the GUI? Run `python launch_gui.py`.
- Need only part of a review? Use `--generate-section <section_number>`.
- Want to retry only failures? Use `--retry-failed` or `--retry-review-failed`.
- Want to name the run? Add `--project-name`.
- Using Zotero? Make sure the report path and library path are set.

If you need technical or development-only details, use the separate internal docs instead of this README.

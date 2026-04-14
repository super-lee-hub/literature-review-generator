# LLM Literature Review Generator

[中文说明](./README.zh-CN.md) | [English](./README.en.md)

This project is a local AI literature analysis and literature review generation workbench with two main input modes:

- `PDF folder mode`: analyze a folder of PDFs directly
- `Zotero mode`: use a Zotero report plus library path

It runs in stages:

1. preprocessing and stage-1 paper analysis
2. outline generation
3. full review generation

The GUI and CLI are now aligned:

- major CLI features are available in the GUI
- both GUI and CLI show clear progress
- stage 2 supports both automatic retry and manual retry for failed sections

## 1. Install

### Runtime only

```bash
pip install -r requirements.txt
```

For development setup, testing, type checking, and Playwright GUI tests, see [DEVELOPMENT.md](./DEVELOPMENT.md).

Recommended first step after installation:

```bash
python main.py --setup
```

## 2. Most Common Commands

### 2.1 Run stage 1 only

```bash
python main.py --pdf-folder "D:\YourPdfFolder"
```

### 2.2 Generate the outline

```bash
python main.py --pdf-folder "D:\YourPdfFolder" --generate-outline
```

### 2.3 Generate the full review

```bash
python main.py --pdf-folder "D:\YourPdfFolder" --generate-review
```

### 2.4 Run the full pipeline

```bash
python main.py --pdf-folder "D:\YourPdfFolder" --run-all
```

### 2.5 Regenerate one specific section

```bash
python main.py --pdf-folder "D:\YourPdfFolder" --generate-section <section_number>
```

Replace `<section_number>` with the section number from your outline or the chapter you want to regenerate. It is not a fixed default value.

### 2.6 Retry failed review sections only

```bash
python main.py --pdf-folder "D:\YourPdfFolder" --retry-review-failed
```

### 2.7 Retry failed stage-1 papers

```bash
python main.py --pdf-folder "D:\YourPdfFolder" --retry-failed
```

### 2.8 Start the GUI

```bash
python main.py --gui
```

Or use:

```bash
start_gui.bat
```

## 2.9 Queue Commands

### 2.9.1 Add task to queue

```bash
python main.py --queue-add --pdf-folder "D:\YourPdfFolder" --project-name "your-project" --analyze-only
```

### 2.9.2 Run queue tasks

```bash
python main.py --queue-run
```

### 2.9.3 List queue tasks

```bash
python main.py --queue-list
```

### 2.9.4 Cancel task

```bash
python main.py --queue-cancel <job_id>
```

### 2.9.5 Retry task

```bash
python main.py --queue-retry <job_id>
```

### 2.9.6 Clear completed tasks

```bash
python main.py --queue-clear
```

### 2.9.7 Specify queue file

```bash
python main.py --queue-file "custom_queue_file.json" --queue-list
```

### 2.9.8 Batch load queue files

```bash
python main.py --queue-run --queue-files "queue1.json" "queue2.json"
```

## 3. Zotero Mode

If you use Zotero, configure these paths in `config.ini` or in the GUI:

- `Paths.zotero_report`
- `Paths.library_path`

Then run:

```bash
python main.py --project-name "your-project"
python main.py --project-name "your-project" --generate-outline
python main.py --project-name "your-project" --generate-review
```

## 4. GUI / CLI Mapping

The GUI workspace now maps directly to CLI actions:

- `Analyze Only` -> `python main.py --pdf-folder "..."`
- `Generate Outline` -> `python main.py --pdf-folder "..." --generate-outline`
- `Generate Full Review` -> `python main.py --pdf-folder "..." --generate-review`
- `Run All` -> `python main.py --pdf-folder "..." --run-all`
- `Retry Failed Papers` -> `python main.py --pdf-folder "..." --retry-failed`
- `Generate Selected Section` -> `python main.py --pdf-folder "..." --generate-section N`
- `Retry Failed Sections` -> `python main.py --pdf-folder "..." --retry-review-failed`

`validate` is still available, but it is now off by default and moved into the GUI’s `Advanced / Experimental` area.

## 5. Progress Visualization

Progress is now exposed across all main steps:

- PDF preprocessing
- stage-1 paper analysis
- stage-1 failed-paper retry
- outline generation
- stage-2 section generation
- stage-2 failed-section retry
- single-section regeneration

Display behavior:

- CLI keeps `tqdm` plus logs
- GUI shows a persistent task progress card with current task, stage, current paper/section, success/failed/remaining counts, retry round, and elapsed time

Notes:

- countable stages use determinate progress bars
- long single API calls use indeterminate progress bars

## 6. Stage-2 Automatic and Manual Retry

### Automatic retry

After the first `--generate-review` pass, the program automatically retries failed sections.

Config:

```ini
[Stage2_Retry]
enabled = true
max_retry_rounds = 2
base_retry_delay = 30
max_retry_delay = 120
```

### Manual retry

If sections are still missing after a run, use:

```bash
python main.py --pdf-folder "D:\YourPdfFolder" --retry-review-failed
```

This retries only failed or missing sections and keeps successful ones intact.

### Single section regeneration

If you only want one section:

```bash
python main.py --pdf-folder "D:\YourPdfFolder" --generate-section <section_number>
```

Use the actual outline section number here, for example the chapter number you need to rerun.

## 7. Output Files

After a run, check:

```text
output/project_name/
```

Common files:

- `*_summaries.json`: structured stage-1 summaries
- `*_analyzed_papers.xlsx`: Excel export
- `*_literature_review_outline.md`: outline
- `*_literature_review.docx`: full review
- `*_failed_papers_report.txt`: stage-1 failure report
- `*_review_checkpoint.json`: stage-2 checkpoint
- `*_sections/`: per-section artifacts for stage 2

## 8. Paper-Type Routing

Stage 1 now keeps the cost model of one main call per paper. That call handles:

1. `paper_type` routing
2. `common_core` extraction
3. routed `type_specific_details`

Current top-level types:

- `empirical`
- `review`
- `conceptual`
- `uncertain`

Compatibility is preserved, so older `summaries.json`, Excel export, and stage-2 flows still work.

## 9. Validate

`validate` is still present, but it was not expanded in this round and is not recommended as part of the default workflow.

Default status:

- `enable_stage1_validation = false`
- `enable_stage2_validation = false`

In the GUI, it now lives under `Advanced / Experimental`.

## 10. Recommended Order

For a stable first run:

1. `python main.py --setup`
2. run stage 1 first:

```bash
python main.py --pdf-folder "D:\YourPdfFolder"
```

3. check the summaries and Excel export
4. generate the outline:

```bash
python main.py --pdf-folder "D:\YourPdfFolder" --generate-outline
```

5. generate the full review:

```bash
python main.py --pdf-folder "D:\YourPdfFolder" --generate-review
```

If a chapter is missing afterward:

- use `--generate-section N` for one section
- use `--retry-review-failed` for all failed / missing sections

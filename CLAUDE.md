# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is `llm_reviewer_generator`, an industrial-grade literature review automation tool written in Python. It analyzes PDF research papers using AI and generates structured literature reviews in Word format. The system supports identity-based checkpoint/resume, dual AI engine architecture, multi-stage validation, and concept enhancement modes.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run interactive setup wizard (first time only)
python main.py --setup

# One-click mode: analyze papers and generate complete review
python main.py --project-name "我的研究项目" --run-all

# Or use direct PDF folder mode
python main.py --pdf-folder "D:\我的PDF文献" --run-all
```

## Common Commands

### Development Workflow
```bash
# Stage 1: Analyze literature only
python main.py --project-name "我的研究项目"

# Stage 2a: Generate outline from existing summaries
python main.py --project-name "我的研究项目" --generate-outline

# Stage 2b: Generate full review from outline
python main.py --project-name "我的研究项目" --generate-review

# Validate generated review (requires Validator_API config)
python main.py --project-name "我的研究项目" --validate-review

# Retry failed papers from previous run
python main.py --project-name "我的研究项目" --retry-failed

# Merge additional summaries into existing project
python main.py --project-name "我的研究项目" --merge ./additional_summaries.json
```

### Concept Enhancement Mode
```bash
# Step 1: Learn concept from seed papers
python main.py --prime-with-folder "D:\核心论文" --concept "消费者成熟度" --project-name "消费者研究"

# Step 2: Analyze all papers with concept enhancement
python main.py --project-name "消费者研究" --concept "消费者成熟度" --run-all
```

### Testing
```bash
# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_main_flow.py -v

# Run with coverage
pytest tests/ --cov=. --cov-report=html
```

## Architecture

### Core Components

**LiteratureReviewGenerator** (`main.py:252`)
- Main orchestrator class managing the entire workflow
- Two modes: "zotero" (from Zotero report) and "direct" (from PDF folder)
- Three processing phases: Analysis → Outline → Review
- Coordinates all services and components

**ReportingService** (`main.py:119`)
- Generates Excel reports, failure reports, and retry reports
- Delegates to `report_generator` module

**CheckpointManager** (`main.py:146`)
- Implements identity-based checkpoint/resume mechanism
- Uses DOI or title+author as paper identity (not position-based)
- Survives paper list reordering, additions, and deletions

**RateLimiter** (`ai_interface.py:395`)
- Dual-engine token bucket rate controller
- Manages TPM/RPM for both primary and backup AI engines
- Supports proactive (token bucket) and reactive (429 handling) modes
- Automatic engine switching when primary fails

### Processing Flow

```
┌─────────────────────────────────────────────────────────┐
│ Stage 1: Literature Analysis                             │
├─────────────────────────────────────────────────────────┤
│ 1. Parse source (Zotero report or scan PDF folder)      │
│ 2. Extract PDF text (dual-engine: pdfplumber → PyMuPDF) │
│ 3. AI analysis (primary → backup on failure)            │
│ 4. Optional Stage 1 validation                          │
│ 5. Generate summaries.json + Excel report               │
└─────────────────────────────────────────────────────────┘
                      ↓
┌─────────────────────────────────────────────────────────┐
│ Stage 2a: Outline Generation                            │
├─────────────────────────────────────────────────────────┤
│ 1. Load existing summaries                              │
│ 2. Generate structured outline with continuation loop   │
│ 3. Save outline to .md file                             │
└─────────────────────────────────────────────────────────┘
                      ↓
┌─────────────────────────────────────────────────────────┐
│ Stage 2b: Review Generation                             │
├─────────────────────────────────────────────────────────┤
│ 1. Load outline + summaries                             │
│ 2. Generate chapter-by-chapter with continuation loop   │
│ 3. Append to Word document with styling                 │
│ 4. Auto-generate TOC + APA references                   │
│ 5. Optional Stage 2 validation                          │
└─────────────────────────────────────────────────────────┘
```

### Identity-Based Checkpoint System

Unlike simple position-based resume, this system uses paper identity:
- **Primary key**: DOI (normalized)
- **Fallback key**: title_clean + author_surnames
- Survives: reordering, additions, deletions, renames
- Checkpoint file: `{project_name}_checkpoint.json`
- Review checkpoint: `{project_name}_review_checkpoint.json`

Key logic in `main.py:446-497`

## Configuration System

### Configuration Precedence
1. **Environment variables** (`.env` file) - **PRIORITY for API keys**
2. **config.ini** - General settings

### Environment Variables (`.env`)
```bash
# Copy from .env.example
LLM_PRIMARY_READER_API=your_primary_api_key
LLM_BACKUP_READER_API=your_backup_api_key
LLM_WRITER_API=your_writer_api_key
LLM_VALIDATOR_API=your_validator_api_key  # Only if validation enabled
```

### API Configuration Structure

**Primary_Reader_API** - Main analysis engine (fast, cost-effective)
**Backup_Reader_API** - Fallback engine (larger context, higher cost)
**Writer_API** - Outline and review generation
**Validator_API** - Stage 1 & 2 validation (optional)

Each requires:
- `api_key`: API authentication key
- `model`: Model name (e.g., "gemini-2.5-pro", "moonshotai/Kimi-K2-Instruct-0905")
- `api_base`: API endpoint (e.g., "https://api.openai.com/v1", "https://api.siliconflow.cn/v1")

### Performance Tuning

```ini
[Performance]
max_workers = 3                    # Concurrent processing threads
api_retry_attempts = 5            # API call retries
primary_tpm_limit = 900000        # Primary TPM (0=reactive mode)
primary_rpm_limit = 9000          # Primary RPM (0=reactive mode)
backup_tpm_limit = 2000000        # Backup TPM
backup_rpm_limit = 9000           # Backup RPM
enable_stage1_validation = false  # Validate individual papers
enable_stage2_validation = false  # Validate final review
```

## Key Implementation Details

### AI Interface (`ai_interface.py`)

**Unified API Call** (`_call_ai_api:10-127`)
- Handles JSON/text response formats
- Smart JSON parser with auto-correction
- Exponential backoff retry logic
- 300-second timeout

**Smart JSON Parser** (`_smart_json_parser:130-179`)
- Multiple parsing strategies in priority order
- Extracts from ```json blocks, { } blocks, or raw content
- Falls back to `_auto_correct_json` for damaged JSON

**Rate Limiting** (`RateLimiter:395-686`)
- Dual-engine independent control
- Token bucket (proactive) or reactive mode
- Automatic engine switching on "SWITCH_TO_BACKUP" signal
- Thread-safe with locks

**Summary Generation** (`get_summary_from_ai:738-945`)
- Estimates token usage before API call
- Two-pass validation (common_core + type_specific_details)
- Fallback to regex extraction if JSON parsing fails

### PDF Processing (`pdf_extractor.py`)

**Dual-Engine Extraction**
1. Primary: `pdfplumber` - Better formatting, tables
2. Fallback: `PyMuPDF` (fitz) - Broader compatibility
3. Minimum 500 characters threshold for success

### File Finding (`file_finder.py`)

**Intelligent PDF Matching** (Zotero mode)
- Multi-pass matching algorithm
- Prefers: DOI match → Title fuzzy match → Author match
- Creates file index for fast lookups
- Handles author variations and title abbreviations

### Validation System (`validator.py`)

**Stage 1 Validation** (Individual Papers)
- Enabled via `enable_stage1_validation = true`
- Runs during Stage 1 (paper analysis)
- Cross-validates AI summaries
- Auto-corrects errors in summaries.json
- No separate report (inline correction)

**Stage 2 Validation** (Complete Review)
- Enabled via `enable_stage2_validation = true`
- Runs after review generation
- Validates citations and claims
- Generates `validation_report.txt`
- Uses independent Validator_API

### Concept Enhancement Mode

**Two-Phase Process** (`run_priming_phase:2168-2295`)
1. **Priming Phase**: Analyze 1-5 seed papers to build concept profile
2. **Investigation Phase**: Analyze all papers with concept context

**Concept Profile** (`_generate_concept_profile:2320-2380`)
- Generated from seed paper summaries
- JSON structure with concept definition, historical development, key dimensions
- Saved to `{project_name}_concept_profile.json`
- Used in Stage 1 analysis via `get_concept_analysis`

### Word Document Generation (`docx_writer.py`)

**Features**
- APA-style references with hanging indent
- Auto-generated Table of Contents
- Configurable fonts (default: Times New Roman 12pt)
- Chinese font support via `qn('w:eastAsia')`
- Section-by-section append (supports checkpoint/resume)

## Prompts System (`prompts/`)

All AI interactions use template files from `prompts/` directory:

- `prompt_analyze.txt` - Paper analysis (full text)
- `prompt_synthesize_outline.txt` - Outline generation
- `prompt_synthesize_section.txt` - Section generation
- `prompt_continue_outline.txt` - Outline continuation
- `prompt_continue_section.txt` - Section continuation
- `prompt_system_*.txt` - System prompts
- `prompt_prime_concept.txt` - Concept profile generation
- `prompt_concept_analysis.txt` - Concept-enhanced analysis
- `prompt_validate_*.txt` - Validation prompts

Templates support placeholder substitution:
- `{{SUMMARIES_JSON_ARRAY}}` - All paper summaries
- `{{REVIEW_OUTLINE}}` - Full outline content
- `{{SECTION_TITLE}}` - Current section title
- `{{CONCEPT_PROFILE}}` - Concept learning notes
- `{{PAPER_SUMMARY}}` - Individual paper summary

## Output Files

### Stage 1 Output (in `output/{project_name}/`)
- `{project_name}_summaries.json` - AI-generated structured summaries
- `{project_name}_analyzed_papers.xlsx` - Excel analysis report
- `{project_name}_failed_papers_report.txt` - Failed papers list
- `{project_name}_checkpoint.json` - Identity-based checkpoint
- `{project_name}_concept_profile.json` - Concept profile (if enabled)

### Stage 2a Output
- `{project_name}_literature_review_outline.md` - Structured outline

### Stage 2b Output
- `{project_name}_literature_review.docx` - Complete Word review
- `{project_name}_review_checkpoint.json` - Resume checkpoint

### Validation Output
- `{project_name}_validation_report.txt` - Validation results

## Important Files

| File | Purpose |
|------|---------|
| `main.py` | Main orchestrator, LiteratureReviewGenerator class |
| `config_loader.py` | Config loading with .env support, env variable precedence |
| `ai_interface.py` | AI API abstraction, rate limiting, smart JSON parsing |
| `validator.py` | Stage 1 & 2 validation logic |
| `pdf_extractor.py` | Dual-engine PDF text extraction |
| `file_finder.py` | Intelligent PDF file matching (Zotero mode) |
| `docx_writer.py` | Word document generation with styling |
| `zotero_parser.py` | Parse Zotero export reports |
| `report_generator.py` | Excel and failure report generation |
| `setup_wizard.py` | Interactive configuration wizard |
| `utils.py` | Utility functions (path sanitization, etc.) |
| `config_validator.py` | Configuration validation |

## Security Best Practices

1. **API Keys**: Always use `.env` file, never commit to git
2. **Priority**: `.env` variables override `config.ini`
3. **Ignored Files**: `.env`, `config.ini`, `output/` in `.gitignore`
4. **Documentation**: See `config.ini.example` and `.env.example` templates

## Testing

The project has a pytest-based test suite in `tests/`:
- `test_main_flow.py` - Integration tests for full workflow
- `test_ai_interface.py` - AI API and rate limiter tests
- `test_pdf_extractor.py` - PDF extraction tests
- `test_zotero_parser.py` - Zotero parsing tests
- `test_docx_writer.py` - Word document generation tests
- `conftest.py` - Pytest fixtures and configuration

Note: Some test failures exist but don't affect core functionality (see `PROJECT_IMPROVEMENT_SUMMARY.md`).

## Common Development Tasks

### Adding a New Feature
1. Check if it fits existing phase structure (Stage 1, 2a, or 2b)
2. Add configuration options to `config.ini.example` if needed
3. Add environment variable support in `config_loader.py` if it involves API keys
4. Update prompts if AI behavior changes
5. Add tests in `tests/`

### Modifying AI Behavior
- Edit templates in `prompts/`
- System prompts in `prompt_system_*.txt`
- User prompts in `prompt_*.txt`
- For validation: `prompt_validate_*.txt`

### Debugging Checkpoint Issues
- Checkpoint files are JSON in `output/{project_name}/`
- Identity keys: DOI or title+author
- Delete checkpoint to force restart
- Version check: currently "2.0"

### Performance Tuning
- Adjust `max_workers` in `[Performance]` section
- Switch to reactive mode by setting TPM/RPM to 0
- Reduce API timeout (currently 300s in `ai_interface.py:67`)
- Increase retry attempts (currently 3 in `ai_interface.py:60`)

## Error Handling Patterns

### API Failures
- Automatic retry with exponential backoff
- Switch to backup engine on token limit or primary failure
- Save progress before each paper
- Retry failed papers with `--retry-failed`

### Network Issues
- All progress saved to checkpoint files
- Resume from last checkpoint on restart
- Identity-based system survives source changes

### Invalid Configurations
- Validated by `config_validator.py`
- Environment variables validated at load time
- Missing sections cause early exit

## Known Limitations

1. Test suite has some failures (see `PROJECT_IMPROVEMENT_SUMMARY.md`)
2. PDF extraction requires text-based PDFs (not scanned images)
3. Direct PDF mode lacks metadata (authors, year, journal)
4. Very long papers may require backup engine
5. Concept mode requires 1-5 seed papers

## Project Health

- **Status**: Production ready
- **Code Quality**: Industrial-grade
- **Test Coverage**: Core functionality covered
- **Security**: Follows .env best practices
- **Documentation**: Complete README + inline docs

See `PROJECT_IMPROVEMENT_SUMMARY.md` for recent refactoring details.

# Development Setup

> Audience: contributors and maintainers.
> This document is development-facing; end users should start from the root README files.

## Recommended Environment

The project is maintained primarily in the `auto-generate-gui` conda environment.

```bash
conda env create -f environment.yml
conda activate auto-generate-gui
pip install -r requirements-dev.txt
```

## Dependency Split

- `requirements.txt`: runtime dependencies for normal use
- `requirements-dev.txt`: development, testing, and type-checking dependencies
- `environment.yml`: recommended conda environment bootstrap

## Optional GUI E2E Tests

The Playwright-based GUI tests are optional. If you want to run them locally:

```bash
python -m playwright install chromium
pytest -q tests/test_gui_playwright.py
```

If Playwright is not installed, that test file will be skipped by design.

## Common Developer Commands

Run all tests:

```bash
pytest -q
```

Run type checking:

```bash
pyright
```

Start the GUI in development mode:

```bash
start_gui_dev.bat
```

Or:

```bash
python launch_gui.py --reload --no-show
```

## What Not To Commit

Do not commit local or generated files such as:

- `.env`
- `config.ini`
- `output/`
- `logs/`
- `tmp/`
- `venv/`
- IDE settings or cache directories

These are already covered by `.gitignore`.

# 📚 auto-generate — Traceable, Recoverable AI Literature Review Workbench

[![Windows tests](https://github.com/super-lee-hub/literature-review-generator/actions/workflows/windows-tests.yml/badge.svg)](https://github.com/super-lee-hub/literature-review-generator/actions/workflows/windows-tests.yml)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/super-lee-hub/literature-review-generator)

[中文指南](./README.zh-CN.md) · [DeepWiki](https://deepwiki.com/super-lee-hub/literature-review-generator)

`auto-generate` turns a literature-review workflow — reading papers, extracting
evidence, building an outline, drafting, validating citations, repairing errors,
and resuming interrupted jobs — into a corpus-controlled and auditable local
pipeline.

It is not just a "send PDFs to one LLM and ask it to write a review" script.

Its current design combines full-text and visual Stage 1 reading, durable
artifacts, machine-trackable citations, evidence-grounded validation, guarded
repair, queue execution, and multi-model outline review.

## Why it is different

- **Full-text + visual evidence:** Stage 1 can inspect PDF pages, figures,
  tables, formulas, captions, and normalized text.
- **Multi-model outline review:** Claude generates and arbitrates; GPT and
  DeepSeek provide independent specialist critiques. A critique that collapses
  onto the generator's own model is reported as self-review, never hidden.
- **Machine-trackable citations:** `review_draft` and `citation_manifest` are
  durable truth sources rather than post-hoc `(Author, Year)` guessing.
- **Evidence-grounded validation:** citation claims can be checked against source
  chunks, pages, captions, OCR, and visual observations.
- **Guarded repair:** repairs are dependency-bound block/span patches instead of
  uncontrolled chapter rewrites.
- **Durable runtime:** JobWorkspace, Artifact Registry, provider receipts, replay
  and resume semantics make long jobs recoverable.
- **Queue execution:** multiple PDF or Zotero jobs can run sequentially with
  retry/cancel/resume support.

## Quick start

```bash
pip install -r requirements.txt
python setup_wizard.py
python launch_gui.py
```

For the durable CLI:

```bash
python -m reviewctl --help
python -m reviewctl plan --spec examples/runtime_specs/direct-run-all.json
python -m reviewctl run --spec my-run.json
```

## Choose an entry point

| Need | Command or file |
| --- | --- |
| Initial configuration | `python setup_wizard.py` |
| Guided GUI workflow | `python launch_gui.py` |
| Machine-readable CLI | `python -m reviewctl` |
| AI-native execution | `RuntimeJobSpec` -> `AgentRuntimeRunner` -> `AgentRuntimeBridge` |

`main.py` is a small compatibility-free shim into `reviewctl`. It is not the
current orchestration engine and is not the public direct-run CLI.

## CLI runtime

Edit a version-controlled `RuntimeJobSpec` example. The examples use
placeholders only:

```bash
python -m reviewctl plan --spec examples/runtime_specs/direct-run-all.json
python -m reviewctl plan --spec examples/runtime_specs/zotero-run-all.json
python -m reviewctl plan --spec examples/runtime_specs/free-mode-idea.json
python -m reviewctl run --spec my-run.json
```

The `plan` command validates the source, action, paths, Free Mode input, and
stage policy without performing a provider run. `run` executes the durable
runtime described by the spec.

## Runtime spec shape

A direct run uses `source.mode = "direct"` and a `pdf_folder`; a Zotero run
uses `source.mode = "zotero"`, `zotero_report`, and `library_path`. The current
action for a complete pipeline is `run_all`. Other typed actions are validated
by `RuntimeJobSpec`, including `analyze`, `generate_outline`,
`generate_review`, `generate_section`, and `validate_review`.

Free Mode input is typed at the spec boundary. Use either `free_mode_idea` or
`free_mode_profile`, never both. An idea is projected to the current
`ReviewIntent` contract and remains bound to the writer context.

Concept Mode is currently disabled. Stale Concept Mode requests are rejected;
the runtime does not silently downgrade them or make a provider call.

## Current model routing and endpoint ownership

| Work step | Model | Endpoint ownership |
| --- | --- | --- |
| PDF preprocessing | MinerU `vlm` | MinerU official service |
| Stage 1 primary reader | DeepSeek V4 Flash Vision | DeepSeek official API |
| Stage 1 fallback / validation | DeepSeek V4 Flash | DeepSeek official API |
| Free Mode and Outline relation/coverage critique | DeepSeek V4 Pro | DeepSeek official API |
| Outline candidate generation / arbitration | Claude Opus 5 | Native Anthropic Messages through `chat.178266.xyz`, third-party gateway |
| Outline structure/evidence critique | GPT-5.6-sol | OpenAI Responses-compatible transport through `ai.saigou.work`, third-party gateway |
| Stage 3 Review Writer | GPT-5.6-sol | `Writer_API`, same third-party Responses gateway |

Stage 3 Review is one stage. `Writer_API` is the provider called once per
adopted outline section inside it; it is not a separate pipeline stage. A
gateway host identifies where this project sends the request, not whether the
gateway's upstream is officially operated by Anthropic or OpenAI. Live provider
verification is an opt-in check and is not claimed by offline tests.

## Existing jobs and validation

```bash
python -m reviewctl status --job <job_id>
python -m reviewctl inspect --job <job_id>
python -m reviewctl next-action --job <job_id>
python -m reviewctl resume --job <job_id>
python -m reviewctl validate --job <job_id>
python -m reviewctl validation-status --job <job_id>
```

The current validation service is `ValidationExecutionService`. Its
adjudication reuse authority is bound to the appropriate provider ledger,
receipt, source closure, attempt identity, and Registry dependency closure.

## Stage 1 and prompt authority

Stage 1 is vision-first with the experimental
`deepseek-v4-flash-vision-exp`. MinerU text remains primary evidence, every
non-blank PDF page is rendered and tracked in visual coverage, and long papers
use recoverable visual-scan batches before final synthesis. Vision failures
fall back to `deepseek-v4-flash`. Validation remains text-only
`deepseek-v4-flash`. Production prompts are loaded through the hash-verified
[Prompt inventory](./docs/en/reference/prompt-inventory.md).

## Queue and maintenance commands

The current parser also exposes `doctor`, `queue-list`, `queue-add`,
`queue-run`, `queue-retry`, `queue-cancel`, `queue-remove`, `queue-export`,
and `queue-import`. Run any command with `--help` for its exact options.

```bash
python -m reviewctl doctor --config config.ini.example
python -m reviewctl queue-list --queue-file output/_queue/queue.json
```

## Evidence boundaries

The normal Windows CI gate covers compile, test collection, public CLI smoke,
strict-offline tests, Pyright, doctor, and committed-range whitespace checks.
Live API/provider calls, Playwright, heavy OCR, multi-host publication/fencing,
multi-host single-flight, and cryptographic provenance verification are
separate opt-in scopes and are not implied by offline evidence.

See [AGENTS.md](./AGENTS.md), the [runtime truth sources](./docs/en/runtime/truth-sources.md),
the [architecture map](./docs/en/developer/architecture.md), and the
[feature matrix](./docs/en/reference/feature-matrix.md), [Stage 1 Vision
pipeline](./docs/en/runtime/stage1-vision.md), and [configuration
reference](./docs/en/reference/configuration.md) for maintainer and AI
contracts.

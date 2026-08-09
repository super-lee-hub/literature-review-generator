# auto-generate — English user guide

`auto-generate` is a local, corpus-controlled, full-text-first literature
analysis and review-writing workbench. The pipeline is:

```text
PDF folder or Zotero report + library
  -> preprocessing and Stage 1 structured summaries
  -> Outline Intelligence v3
  -> review_draft v3 + citation_manifest v3 + DOCX
  -> optional validation and repair
```

The source corpus, workspace, artifact registry, stage closures, and validation
evidence remain inspectable and resumable.

## Choose an entry point

| Need | Command or file |
| --- | --- |
| Initial configuration | `python setup_wizard.py` |
| Guided GUI workflow | `python launch_gui.py` |
| Machine-readable CLI | `python -m reviewctl` |
| AI-native execution | `RuntimeJobSpec` -> `AgentRuntimeRunner` -> `AgentRuntimeBridge` |

`main.py` is a small compatibility-free shim into `reviewctl`. It is not the
current orchestration engine and is not the public direct-run CLI.

## Quick start

```bash
pip install -r requirements.txt
python setup_wizard.py
python launch_gui.py
```

For CLI use, edit a version-controlled `RuntimeJobSpec` example. The examples
use placeholders only:

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
[feature matrix](./docs/en/reference/feature-matrix.md) for maintainer and AI
contracts.

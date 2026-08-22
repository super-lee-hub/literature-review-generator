# auto-generate

`auto-generate` is a local, corpus-controlled, full-text-first workbench for
AI literature analysis and review writing. It accepts a PDF folder or a Zotero
report plus library, then produces durable Stage 1 summaries, an Outline
Intelligence v3 outline, a v3 review draft, a v3 citation manifest, DOCX, and
optional validation or repair artifacts.

## Current public surfaces

- GUI: `python launch_gui.py`
- Initial setup: `python setup_wizard.py`
- Machine control plane: `python -m reviewctl`
- AI-native input: `RuntimeJobSpec`, executed by `AgentRuntimeRunner` through
  `AgentRuntimeBridge`

`main.py` is a small compatibility-free shim into `reviewctl`; it is not the
current orchestration engine or the documented direct-run CLI.

## Quick start

```bash
pip install -r requirements.txt
python setup_wizard.py
python launch_gui.py
```

For a repeatable CLI run, copy and edit a current spec example:

```bash
python -m reviewctl plan --spec examples/runtime_specs/direct-run-all.json
python -m reviewctl run --spec my-run.json
```

For an existing durable job:

```bash
python -m reviewctl status --job <job_id>
python -m reviewctl inspect --job <job_id>
python -m reviewctl next-action --job <job_id>
python -m reviewctl resume --job <job_id>
python -m reviewctl validate --job <job_id>
python -m reviewctl validation-status --job <job_id>
```

Concept Mode is currently disabled. Stale Concept Mode requests are rejected
instead of being silently downgraded.

## Stage 1 and Prompt authority

Stage 1 is vision-first with the experimental `deepseek-v4-flash-vision-exp`:
MinerU text remains primary evidence, every non-blank PDF page is rendered and
tracked in visual coverage, and long papers use recoverable visual-scan batches
before final synthesis. Vision failures fall back to `deepseek-v4-flash`.
Validation remains text-only `deepseek-v4-flash`. Production prompts are loaded
through the hash-verified [Prompt inventory](./docs/en/reference/prompt-inventory.md).

## Documentation

- [English user guide](./README.en.md)
- [中文用户指南](./README.zh-CN.md)
- [AI/developer handoff](./AGENTS.md)
- [Runtime truth sources](./docs/en/runtime/truth-sources.md)
- [Architecture](./docs/en/developer/architecture.md)
- [Feature matrix](./docs/en/reference/feature-matrix.md)
- [Stage 1 Vision pipeline](./docs/en/runtime/stage1-vision.md)
- [Configuration reference](./docs/en/reference/configuration.md)
- [Prompt inventory](./docs/en/reference/prompt-inventory.md)
- [Codex/OMX Skill](./.codex/skills/auto-generate-orchestrator/SKILL.md)

Historical migration and baseline documents are retained as historical
evidence. They are not current user or AI execution instructions.

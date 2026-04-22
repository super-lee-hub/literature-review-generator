# auto-generate

> Local AI literature analysis & review writing workbench.
> This root README is intentionally a **landing page / router**. Full user guides live in the Chinese and English README files.

## Choose the document you need

| Need | Go to |
| --- | --- |
| 中文完整使用说明 | [README.zh-CN.md](./README.zh-CN.md) |
| English user guide | [README.en.md](./README.en.md) |
| Repo-local Codex / OMX skill surface | [README.zh-CN.md#71-codex--skill-ai-native-入口](./README.zh-CN.md#71-codex--skill-ai-native-入口) / [README.en.md#71-codex--skill-ai-native-entry-surface](./README.en.md#71-codex--skill-ai-native-entry-surface) |
| AI / maintainer handoff | [AGENTS.md](./AGENTS.md) |
| Runtime truth + durable artifacts | [TRUTH_SOURCES.md](./TRUTH_SOURCES.md) |
| Feature status / implementation reality | [FEATURE_MATRIX.md](./FEATURE_MATRIX.md) |
| Migration-era baseline / history | [ARCHITECTURE_BASELINE.md](./ARCHITECTURE_BASELINE.md) |

## What this project is

`auto-generate` is no longer just a single literature-review script.
It is now a local workbench that supports:

- PDF folder input or Zotero report + library input
- Stage 1 paper analysis -> Stage 2 outline -> Stage 3 DOCX review
- Local GUI, CLI, and repo-local Codex skill entry surfaces
- Queueing, retries, partial regeneration, and stage-1 summary reuse
- PDF preprocessing, cache artifacts, OCR fallback, and optional local RAG
- Review-draft / citation-manifest artifacts plus optional validation & repair flows
- AI-native runtime traces such as `source_bundle.json` and `runtime_stage_trace.json` when the repo-local skill surface is used

## Quick start

```bash
pip install -r requirements.txt
python main.py --setup
python launch_gui.py
```

Or go straight to the CLI:

```bash
python main.py --pdf-folder "D:\papers" --run-all
```

If you are operating the repo from Codex / OMX instead of the human CLI or GUI, the repo-local skill `auto-generate-orchestrator` is the AI-native entry surface. The user-facing explanation lives in the language guides, while runtime truth lives in `AGENTS.md` and `TRUTH_SOURCES.md`.

## Current output layout

Most real outputs now live in a job workspace:

```text
output/<project_name>__<job_id>/
```

The compatibility folder below usually only stores pointers such as `_latest_job.json`:

```text
output/<project_name>/
```

## Recommended next step

- If you are a user, start with [README.zh-CN.md](./README.zh-CN.md) or [README.en.md](./README.en.md).
- If you are an AI agent or maintainer, start with [AGENTS.md](./AGENTS.md).

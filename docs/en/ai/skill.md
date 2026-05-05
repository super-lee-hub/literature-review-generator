# Codex/OMX Skill Documentation

> Audience: AI agents, Codex/OMX users.
> Source: `.codex/skills/auto-generate-orchestrator/SKILL.md`

## Intent

- Add a third entry surface alongside CLI and GUI
- Reuse the current durable substrate (`services/job_runner.py`, `services/job_workspace.py`, `services/artifact_registry.py`, `services/progress_state.py`, `validator.py`)
- Keep deterministic lifecycle / persistence / render / validation transitions local
- Route generation stages through subagents, not legacy CLI shelling or external-API wrappers

## Canonical Constraints

1. `services.job_runner.JobRunRequest` remains the canonical request model
2. CLI and GUI remain first-class human surfaces; do not replace them
3. AI mode is additive and out-of-queue for MVP, but must remain workspace-compatible
4. Canonical downstream artifacts remain: summaries, markdown outline, `review_draft_v2`, `citation_manifest_v3`, docx, validation/repair artifacts

## Primary Runtime Helpers

- `runtime.job_spec.RuntimeJobSpec`
- `runtime.orchestrator.AgentRuntimeBridge`
- `runtime.source_intake.*`
- `runtime.subagent_policy.*`
- `runtime.stage_contracts.*`
- `runtime.lifecycle.*`

## Expected Operating Pattern

1. Normalize AI input into `RuntimeJobSpec`
2. Compile it into canonical `JobRunRequest`
3. Build source intake bundles locally
4. Bootstrap workspace / registry / resume state locally
5. Delegate generation stages to subagents: Stage 1 analyze, Stage 2 outline, Stage 3 review
6. Persist outputs through existing canonical artifact helpers
7. Run validation locally through existing validation seams
8. Register runtime stage trace artifacts so execution mode is observable

## Hard Prohibitions

- Do not shell out to `python main.py ...` as the canonical AI runtime
- Do not introduce a second peer request model
- Do not bypass latest-pointer / artifact-registry / resume-state behavior
- Do not replace `review_draft_v2` / `citation_manifest_v3` with alternate canonical schemas
- Do not treat summary-only evidence as validation truth when richer artifact evidence exists

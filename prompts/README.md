# Prompt inventory

Prompt files are implementation assets, not a public CLI or runtime contract.
The current execution truth is defined by `reviewctl`, `RuntimeJobSpec`,
`AgentRuntimeRunner`, the current stage services, and their Registry-backed
artifacts.

## Current usage

The current Stage 1 analysis service loads the prompt it names in
`services/stage1_analysis_service.py`. Outline Intelligence v3 and the current
review service own their production contracts in code and durable artifacts;
do not infer a public execution path from a prompt filename alone.

Validation is owned by `ValidationExecutionService` and current validation
contracts. Older validation prompt files may remain for compatibility or
historical comparison, but they are not a peer public control plane.

Concept Mode is currently disabled. Concept-related prompt files are retained
only as historical/compatibility assets and are not current user instructions.

When changing a prompt, update the owning service contract and its tests, then
verify the resulting artifact, dependency, receipt, and stage-closure bindings.

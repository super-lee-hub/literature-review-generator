# Production reachability inventory — 2026-09-07

This inventory is tied to the current 'codex/f1-validation-authority-closure'
implementation. “Reachable” means imported or invoked by the current public
control plane ('python -m reviewctl') or by the GUI facade, not merely present
in the repository.

| Surface | Disposition | Current authority / boundary |
| --- | --- | --- |
| 'reviewctl.py' | active | Public CLI; delegates to 'ReviewControlPlane'. |
| 'runtime/control_plane.py' | active | Provider-free planning, preflight, acceptance-run, inspection, and lifecycle commands. |
| 'runtime/job_spec.py' | active | Strict structured runtime-spec boundary; flat 'from_mapping' is a checked compatibility bridge. |
| 'runtime/runner.py' / 'runtime/orchestrator.py' | active | 'RuntimeJobSpec' → 'AgentRuntimeRunner' → 'AgentRuntimeBridge'; Registry and stage closure remain authoritative. |
| 'runtime/provider_routes.py' | active | Single 'ReachableProviderRoutePlan' for StagePlan/provider admission and route reporting. |
| 'outline/v3_executor.py' | active | Only current production Outline executor; receives enabled semantic routes from the route plan. |
| 'services/review_generation_service.py' | active | Current Stage 3 writer and DOCX publication path. |
| 'validation/execution_service.py' / 'validation/current_validation.py' | active | Current validation, repair, provisional/durable closure path. |
| 'pdf_extractor.py' | compatibility-only / unreachable from current runtime | No current `reviewctl`, GUI, or `preprocess.service` import/invocation; retained only for isolated compatibility tests. It cannot publish canonical artifacts, and its fallback buffer is reset on partial parser failure. |
| 'validator.py' | legacy isolated | No current runtime import or public control-plane call. Current validation enters through 'ValidationExecutionService'; direct legacy callers must migrate and are not release evidence. |
| 'main.py' | compatibility-only | Thin shim to 'reviewctl'; it is not an orchestration implementation. |
| 'rag/local_rag.py' | optional active | Opt-in only; source/fingerprint identity is persisted and model downloads are blocked unless explicitly enabled. |
| 'config_loader.py' / 'config_validator.py' | active | Current schema and route-aware admission; action-bound calls require the formal validator signature. |
| 'outline' v2/old direct flags | deprecated / unreachable | Not imported or accepted by the current StagePlan and architecture guards. |
| old flat preprocess cache files | deprecated / unreadable as current authority | Current generation pointer, hashes, source identity, and processing fingerprint are required. |

## Evidence limits

The table is a code reachability inventory, not proof that a provider, corpus,
GUI browser, OCR engine, or branch-protection API was exercised. Those claims
require the corresponding durable acceptance references and are reported as
'NOT_VERIFIED' when absent.

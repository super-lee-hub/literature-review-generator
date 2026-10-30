"""Thin runtime adapter for the explicit typed Stage 1 source partition."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from runtime.job_spec import RuntimeJobSpec
from runtime.provider_routes import (
    ReachableProviderRoutePlan,
    build_reachable_provider_route_plan,
)
from runtime.stage_planning import StagePlan, build_stage_plan
from runtime.stage_contracts import SourceBundle
from runtime.source_intake import build_source_bundle_for_request
from services.artifact_registry import ArtifactRegistry, RegistryError
from services.explicit_stage1_typed_import import (
    ExplicitTypedImportPlan,
    resolve_explicit_typed_imports,
)


RegistryResolver = Callable[[str], ArtifactRegistry | None]


class ExplicitStage1ScopeError(ValueError):
    """Raised when the explicit Stage 1 runtime partition is not authoritative."""


@dataclass(frozen=True)
class EffectiveProviderRoutePlan(ReachableProviderRoutePlan):
    """Reachable route plan with a direct requested-stages projection."""

    @property
    def requested_stages(self) -> tuple[str, ...]:
        return self.stage_plan.requested_stages


@dataclass(frozen=True)
class ExplicitStage1Scope:
    """Freshly verified explicit inputs and the runtime projections they imply."""

    import_plan: ExplicitTypedImportPlan
    full_bundle: SourceBundle
    stage_plan: StagePlan
    provider_route_plan: EffectiveProviderRoutePlan
    config: Mapping[str, Mapping[str, Any]]
    remote_preprocess_required: bool

    @property
    def effective_provider_route_plan(self) -> EffectiveProviderRoutePlan:
        return self.provider_route_plan

    @property
    def all_imported(self) -> bool:
        return not self.import_plan.pending_keys


def _free_mode_enabled(spec: RuntimeJobSpec) -> bool:
    return bool(
        spec.free_mode_profile
        or spec.free_mode_idea
        or spec.metadata.get("free_mode_input")
    )


def _effective_stage_plan(stage_plan: StagePlan, *, suppress_analyze: bool) -> StagePlan:
    if not suppress_analyze or "analyze" not in stage_plan.requested_stages:
        return stage_plan
    return replace(
        stage_plan,
        requested_stages=tuple(
            stage for stage in stage_plan.requested_stages if stage != "analyze"
        ),
        required_stages=tuple(
            stage for stage in stage_plan.required_stages if stage != "analyze"
        ),
    )


def _require_explicit_spec(spec: RuntimeJobSpec) -> None:
    spec.validate()
    if spec.metadata.get("stage1_summary_input_mode") != "explicit_typed_import":
        raise ExplicitStage1ScopeError(
            "explicit Stage 1 scope requires metadata.stage1_summary_input_mode=explicit_typed_import"
        )
    if spec.reuse_stage1 is not True or not spec.reuse_summary_files:
        raise ExplicitStage1ScopeError(
            "explicit Stage 1 scope requires reuse_stage1=true and selected typed manifest files"
        )


def _summary_rows(spec: RuntimeJobSpec) -> list[Mapping[str, Any]]:
    from runtime.orchestrator import InternalStageExecutorRegistry

    rows: list[Mapping[str, Any]] = []
    for raw_path in spec.reuse_summary_files:
        path = Path(raw_path).expanduser().resolve()
        try:
            selected_payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ExplicitStage1ScopeError(
                f"selected typed manifest is unreadable: {path}"
            ) from exc
        if not (
            isinstance(selected_payload, Mapping)
            and selected_payload.get("artifact_type")
            == "stage1_reusable_summary_manifest"
            and selected_payload.get("artifact_version") in {"v1", "v2"}
        ):
            raise ExplicitStage1ScopeError(
                "explicit typed import selected file must be a v1/v2 "
                f"stage1_reusable_summary_manifest: {path}"
            )
        try:
            loaded = InternalStageExecutorRegistry._summary_payloads_from_file(path)
        except (OSError, UnicodeError, TypeError, ValueError, RuntimeError) as exc:
            raise ExplicitStage1ScopeError(
                f"cannot load selected typed Stage 1 input: {path}: {exc}"
            ) from exc
        rows.extend(loaded)
    if not rows:
        raise ExplicitStage1ScopeError("selected typed Stage 1 inputs are empty")
    return rows


def _manifest_body(path: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExplicitStage1ScopeError(f"selected typed manifest is unreadable: {path}") from exc
    if isinstance(payload, Mapping) and isinstance(payload.get("payload"), Mapping):
        payload = payload["payload"]
    if not isinstance(payload, Mapping):
        raise ExplicitStage1ScopeError(f"selected typed manifest is not an object: {path}")
    return payload


def _authorized_registry_resolver(
    rows: list[Mapping[str, Any]],
    supplied: RegistryResolver | None,
) -> RegistryResolver:
    """Resolve only owner registries declared by the selected authorities."""

    exact_paths: dict[str, Path] = {}
    declared_jobs: set[str] = set()

    def add_locator(job_id: Any, raw_path: Any, label: str) -> None:
        job = str(job_id or "").strip()
        value = str(raw_path or "").strip()
        if not job:
            return
        declared_jobs.add(job)
        if not value:
            return
        path = Path(value).expanduser().resolve()
        previous = exact_paths.get(job)
        if previous is not None and previous != path:
            raise ExplicitStage1ScopeError(f"{label} registry locators conflict for {job}")
        exact_paths[job] = path

    for row in rows:
        reuse = row.get("stage1_reuse")
        if not isinstance(reuse, Mapping):
            continue
        binding = reuse.get("binding")
        if not isinstance(binding, Mapping):
            continue
        manifest_path = str(reuse.get("typed_manifest_path") or "").strip()
        if not manifest_path:
            continue
        manifest = _manifest_body(manifest_path)
        add_locator(
            binding.get("source_authority_job_id") or manifest.get("job_id"),
            binding.get("source_authority_registry_path"),
            "source authority",
        )
        owner = manifest.get("owner_correction_authority")
        if isinstance(owner, Mapping):
            for name in (
                "origin_registry",
                "correction_registry",
                "origin_source",
                "origin_prior_manifest",
            ):
                locator = owner.get(name)
                if isinstance(locator, Mapping):
                    add_locator(
                        locator.get("job_id"),
                        locator.get("registry_path"),
                        f"owner correction {name}",
                    )

    def resolve(job_id: str) -> ArtifactRegistry | None:
        job = str(job_id or "").strip()
        if job not in declared_jobs:
            return None
        exact = exact_paths.get(job)
        if exact is not None:
            if not exact.is_file():
                raise ExplicitStage1ScopeError(
                    f"authorized owner registry path is not an existing file: {exact}"
                )
            if supplied is not None:
                candidate = supplied(job)
                if candidate is not None:
                    candidate_path = Path(candidate.registry_path).expanduser().resolve()
                    if candidate.job_id != job or candidate_path != exact:
                        raise ExplicitStage1ScopeError(
                            f"authorized registry resolver disagrees with selected owner: {job}"
                        )
                    return candidate
            try:
                return ArtifactRegistry(exact, job)
            except (OSError, RegistryError, TypeError, ValueError) as exc:
                raise ExplicitStage1ScopeError(
                    f"authorized owner registry is unavailable: {job}"
                ) from exc
        # A declared owner without a physical registry path is not safe to
        # resolve here; do not delegate to a resolver that may sweep output
        # directories.
        return None

    return resolve


def authorized_explicit_stage1_registry_resolver(
    rows: Sequence[Mapping[str, Any]],
    supplied: RegistryResolver | None = None,
) -> RegistryResolver:
    """Return an exact-locator resolver for selected typed authorities."""

    return _authorized_registry_resolver(list(rows), supplied)


def _require_complete_visual_coverage(config: Mapping[str, Mapping[str, Any]]) -> bool:
    from services.config_values import parse_strict_bool

    return parse_strict_bool(
        config.get("Stage1_Input", {}).get("require_complete_visual_coverage"),
        field="Stage1_Input.require_complete_visual_coverage",
        default=True,
    )


def _resolve_with_config(
    spec: RuntimeJobSpec,
    config: Mapping[str, Mapping[str, Any]],
    stage_plan: StagePlan,
    *,
    external_registry_resolver: RegistryResolver | None,
) -> ExplicitStage1Scope:
    full_bundle = build_source_bundle_for_request(
        spec.to_job_request(), project_name=spec.project_name
    )
    rows = _summary_rows(spec)
    resolver = _authorized_registry_resolver(rows, external_registry_resolver)
    import_plan = resolve_explicit_typed_imports(
        rows,
        full_bundle,
        f1_binding=spec.metadata.get("f1_corpus_binding"),
        external_registry_resolver=resolver,
        require_complete_visual_coverage=_require_complete_visual_coverage(config),
    )
    effective_stage_plan = _effective_stage_plan(
        stage_plan,
        suppress_analyze=not import_plan.pending_keys,
    )
    route_plan = build_reachable_provider_route_plan(
        config,
        action=spec.action,
        requested_stages=effective_stage_plan.requested_stages,
        free_mode_enabled=_free_mode_enabled(spec),
        stage_plan=effective_stage_plan,
    )
    effective_routes = EffectiveProviderRoutePlan(
        action=route_plan.action,
        stage_plan=effective_stage_plan,
        routes=route_plan.routes,
        diagnostics=route_plan.diagnostics,
    )
    return ExplicitStage1Scope(
        import_plan=import_plan,
        full_bundle=full_bundle,
        stage_plan=effective_stage_plan,
        provider_route_plan=effective_routes,
        config=config,
        remote_preprocess_required=bool(import_plan.pending_keys),
    )


def resolve_explicit_stage1_scope(
    spec: RuntimeJobSpec,
    config: Mapping[str, Mapping[str, Any]],
    stage_plan: StagePlan,
    *,
    external_registry_resolver: RegistryResolver | None = None,
) -> ExplicitStage1Scope:
    """Freshly resolve the selected typed files and effective provider scope."""

    _require_explicit_spec(spec)
    return _resolve_with_config(
        spec,
        config,
        stage_plan,
        external_registry_resolver=external_registry_resolver,
    )


def load_explicit_stage1_scope(
    spec: RuntimeJobSpec,
    *,
    external_registry_resolver: RegistryResolver | None = None,
    allow_template_credentials: bool = False,
) -> ExplicitStage1Scope:
    """Load credentials once, verify the partition, then reload effective routes."""

    _require_explicit_spec(spec)
    from config_loader import load_config
    from services.settings import ApplicationSettings

    free_mode_enabled = _free_mode_enabled(spec)
    bootstrap = load_config(
        spec.config,
        action=spec.action,
        requested_stages=(),
        free_mode_enabled=free_mode_enabled,
        allow_template_credentials=allow_template_credentials,
    )
    settings = ApplicationSettings.from_config(bootstrap)
    actual_plan = build_stage_plan(
        action=spec.action,
        requested_stages=spec.metadata.get("requested_stages"),
        validation_enabled=settings.review_validation_enabled(),
        validation_required=spec.metadata.get("validation_required"),
        require_clean_validation=spec.metadata.get("require_clean_validation"),
        allow_unvalidated_when_validation_optional=spec.metadata.get(
            "allow_unvalidated_when_validation_optional"
        ),
    )
    provisional = _resolve_with_config(
        spec,
        bootstrap,
        actual_plan,
        external_registry_resolver=external_registry_resolver,
    )
    effective_plan = _effective_stage_plan(
        actual_plan,
        suppress_analyze=not provisional.import_plan.pending_keys,
    )
    final_config = load_config(
        spec.config,
        action=spec.action,
        requested_stages=effective_plan.requested_stages,
        free_mode_enabled=free_mode_enabled,
        allow_template_credentials=allow_template_credentials,
    )
    return _resolve_with_config(
        spec,
        final_config,
        actual_plan,
        external_registry_resolver=external_registry_resolver,
    )


__all__ = [
    "EffectiveProviderRoutePlan",
    "ExplicitStage1Scope",
    "ExplicitStage1ScopeError",
    "authorized_explicit_stage1_registry_resolver",
    "load_explicit_stage1_scope",
    "resolve_explicit_stage1_scope",
]

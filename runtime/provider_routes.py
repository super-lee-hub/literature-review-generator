"""Authoritative reachable-provider route planning.

The runtime has several semantic stages which are served by named provider
sections.  This module is the single, side-effect-free projection of that
relationship.  Callers use it for admission and reporting before they resolve
credentials or construct a transport; no caller may infer reachability from a
physical section name alone.

The planner intentionally records missing sections as unresolved required
routes.  That makes a dry preflight explain the exact missing semantic role and
prevents a caller from silently falling back to another provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, cast

from services.model_capabilities import resolve_model_capability
from services.settings import ApplicationSettings
from runtime.stage_planning import StagePlan, build_stage_plan


OUTLINE_ROLE_TO_SETTING: dict[str, str] = {
    "candidate_provider_generation": "outline_model",
    "relation_adjudication": "relation_adjudicator_model",
    "structure_critique": "structure_critic_model",
    "coverage_critique": "coverage_critic_model",
    "evidence_critique": "evidence_critic_model",
    "arbitration": "arbitrator_model",
}

_OUTLINE_FLAG_FOR_ROLE: dict[str, str | None] = {
    "candidate_provider_generation": None,
    "relation_adjudication": "relation_adjudication_enabled",
    "structure_critique": "structure_critique_enabled",
    "coverage_critique": "coverage_critique_enabled",
    "evidence_critique": "evidence_critique_enabled",
    "arbitration": None,
}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().casefold() in {"1", "true", "yes", "on", "y"}


def _primary_reader_only(config: Mapping[str, Mapping[str, Any]]) -> bool:
    return _as_bool(config.get("Stage1_Input", {}).get("primary_reader_only"), False)


@dataclass(frozen=True)
class ReachableProviderRoute:
    """One semantic role and its required physical configuration section."""

    stage: str
    semantic_role: str
    section_name: str
    required: bool = True
    enabled: bool = True
    provider_family: str = ""
    model: str = ""
    endpoint_type: str = ""
    api_base_host: str = ""
    resolved: bool = False

    @property
    def identity(self) -> tuple[str, str, str, str]:
        """A secret-free physical route identity used for deduplication."""

        return (
            self.provider_family,
            self.model,
            self.endpoint_type,
            self.api_base_host,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "semantic_role": self.semantic_role,
            "section": self.section_name,
            "required": self.required,
            "enabled": self.enabled,
            "resolved": self.resolved,
            "provider_family": self.provider_family,
            "model": self.model,
            "endpoint_type": self.endpoint_type,
            "api_base_host": self.api_base_host,
            "physical_identity": list(self.identity),
        }


@dataclass(frozen=True)
class ReachableProviderRoutePlan:
    """The complete provider surface reachable from one durable StagePlan."""

    action: str
    stage_plan: StagePlan
    routes: tuple[ReachableProviderRoute, ...] = ()
    diagnostics: tuple[str, ...] = ()

    @property
    def semantic_roles(self) -> tuple[str, ...]:
        return tuple(route.semantic_role for route in self.routes if route.enabled)

    @property
    def required_provider_sections(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                route.section_name
                for route in self.routes
                if route.required and route.enabled and route.section_name
            )
        )

    @property
    def physical_routes(self) -> tuple[tuple[str, str, str, str], ...]:
        """Unique physical routes, retaining first semantic-role order."""

        seen: dict[tuple[str, str, str, str], None] = {}
        for route in self.routes:
            if not route.enabled or not route.resolved:
                continue
            seen.setdefault(route.identity, None)
        return tuple(seen)

    @property
    def unresolved_required_routes(self) -> tuple[ReachableProviderRoute, ...]:
        return tuple(
            route
            for route in self.routes
            if route.enabled and route.required and not route.resolved
        )

    def route_for_role(self, semantic_role: str) -> ReachableProviderRoute:
        target = str(semantic_role or "").strip()
        for route in self.routes:
            if route.semantic_role == target and route.enabled:
                return route
        raise KeyError(f"no reachable provider route for semantic role {target!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "reachable-provider-route-plan-v1",
            "action": self.action,
            "stage_plan": self.stage_plan.to_dict(),
            "semantic_roles": list(self.semantic_roles),
            "required_provider_sections": list(self.required_provider_sections),
            "physical_routes": [list(item) for item in self.physical_routes],
            "unresolved_required_routes": [
                route.to_dict() for route in self.unresolved_required_routes
            ],
            "routes": [route.to_dict() for route in self.routes],
            "diagnostics": list(self.diagnostics),
        }


def _safe_host(api_base: Any) -> str:
    text = str(api_base or "").strip()
    if not text:
        return ""
    try:
        from urllib.parse import urlsplit

        parsed = urlsplit(text)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return host.casefold()
    except (TypeError, ValueError):
        return ""


def _route_from_section(
    *,
    stage: str,
    semantic_role: str,
    section_name: str,
    config: Mapping[str, Mapping[str, Any]],
) -> ReachableProviderRoute:
    section = config.get(section_name)
    if not isinstance(section, Mapping):
        return ReachableProviderRoute(
            stage=stage,
            semantic_role=semantic_role,
            section_name=section_name,
        )
    model = str(section.get("model") or "").strip()
    endpoint_type = str(section.get("endpoint_type") or "").strip()
    provider_family = str(section.get("provider_family") or "").strip()
    if model:
        try:
            capability = resolve_model_capability(cast(Any, dict(section)))
            provider_family = provider_family or str(capability.provider_family)
            endpoint_type = endpoint_type or str(capability.endpoint_type)
        except (TypeError, ValueError, KeyError):
            # Config validation owns the detailed error.  The planner keeps an
            # unresolved route so admission cannot accidentally treat it as a
            # valid physical route.
            pass
    resolved = bool(
        model
        and str(section.get("api_base") or "").strip()
        and endpoint_type
        and provider_family
    )
    return ReachableProviderRoute(
        stage=stage,
        semantic_role=semantic_role,
        section_name=section_name,
        provider_family=provider_family,
        model=model,
        endpoint_type=endpoint_type,
        api_base_host=_safe_host(section.get("api_base")),
        resolved=resolved,
    )


def _add_stage_route(
    routes: list[ReachableProviderRoute],
    *,
    stage: str,
    role: str,
    section: str,
    config: Mapping[str, Mapping[str, Any]],
) -> None:
    routes.append(
        _route_from_section(
            stage=stage,
            semantic_role=role,
            section_name=section,
            config=config,
        )
    )


def build_reachable_provider_route_plan(
    config: Mapping[str, Mapping[str, Any]],
    *,
    action: str,
    requested_stages: Iterable[Any] | None,
    free_mode_enabled: bool = False,
    stage_plan: StagePlan | None = None,
) -> ReachableProviderRoutePlan:
    """Build the one route plan used by all current runtime entrypoints."""

    settings = ApplicationSettings.from_config(config)
    plan = stage_plan or build_stage_plan(
        action=action,
        requested_stages=requested_stages,
        validation_enabled=settings.review_validation_enabled(),
    )
    routes: list[ReachableProviderRoute] = []
    stages = set(plan.requested_stages)

    if "analyze" in stages:
        _add_stage_route(
            routes,
            stage="analyze",
            role="primary_reader",
            section="Primary_Reader_API",
            config=config,
        )
        if not _primary_reader_only(config):
            _add_stage_route(
                routes,
                stage="analyze",
                role="backup_reader",
                section="Backup_Reader_API",
                config=config,
            )
        if settings.stage1_validation_enabled():
            _add_stage_route(
                routes,
                stage="analyze",
                role="stage1_validator",
                section="Validator_API",
                config=config,
            )

    if "outline" in stages:
        role_sections = settings.outline_role_sections()
        for role, setting_key in OUTLINE_ROLE_TO_SETTING.items():
            flag = _OUTLINE_FLAG_FOR_ROLE[role]
            if flag is not None and not bool(getattr(settings.outline, flag)):
                continue
            _add_stage_route(
                routes,
                stage="outline",
                role=role,
                section=str(role_sections.get(setting_key) or "").strip(),
                config=config,
            )

    if "review" in stages:
        _add_stage_route(
            routes,
            stage="review",
            role="writer",
            section="Writer_API",
            config=config,
        )

    if "validate" in stages:
        _add_stage_route(
            routes,
            stage="validate",
            role="validator",
            section="Validator_API",
            config=config,
        )

    if free_mode_enabled:
        _add_stage_route(
            routes,
            stage="free_mode",
            role="free_mode",
            section="Free_Mode_API",
            config=config,
        )

    diagnostics: list[str] = []
    for route in routes:
        if not route.section_name:
            diagnostics.append(
                f"reachable semantic role {route.semantic_role!r} has no configured section"
            )
        elif not route.resolved:
            diagnostics.append(
                f"reachable semantic role {route.semantic_role!r} requires incomplete "
                f"[{route.section_name}] route"
            )

    return ReachableProviderRoutePlan(
        action=str(action or "analyze"),
        stage_plan=plan,
        routes=tuple(routes),
        diagnostics=tuple(dict.fromkeys(diagnostics)),
    )


__all__ = [
    "OUTLINE_ROLE_TO_SETTING",
    "ReachableProviderRoute",
    "ReachableProviderRoutePlan",
    "build_reachable_provider_route_plan",
]

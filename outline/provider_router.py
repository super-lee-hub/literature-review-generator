"""Role-aware provider routing for Outline Intelligence v3.

Why this module exists
----------------------
``OutlineV3Executor`` previously received a *single* ``provider`` and a *single*
``ProviderContextProfile``.  Every semantic node -- relation adjudication, the
candidate generations, the three critiques, and the final arbitration -- was
therefore planned and executed against one provider identity.  That made the
configured ``[OutlineModels]`` roles cosmetic: the "critics" were the same model
reviewing its own output.

This module turns ``[OutlineModels]`` into a real, auditable routing table.
It is deliberately *not* a second configuration truth source: it only maps an
existing settings key to an already-resolved transport, and it never invents
defaults that would silently collapse distinct roles onto one provider.

Fail-closed contract
--------------------
``route_for()`` raises ``KeyError`` for an unknown node instead of falling back
to the Outline provider.  A silent fallback here would recreate exactly the
bug this module exists to close.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from runtime.provider_context import ProviderContextProfile

# Maps the semantic Outline node role to the ``[OutlineModels]`` setting key
# that selects which API section serves that role.
ROLE_SETTING_KEYS: dict[str, str] = {
    "relation_adjudication": "relation_adjudicator_model",
    "candidate_provider_generation": "outline_model",
    "structure_critique": "structure_critic_model",
    "coverage_critique": "coverage_critic_model",
    "evidence_critique": "evidence_critic_model",
    "arbitration": "arbitrator_model",
}

# ``[OutlineModels]`` setting key -> Settings property name.
SETTING_ACCESSORS: dict[str, str] = {
    "relation_adjudicator_model": "relation_adjudicator_model",
    "outline_model": "outline_model",
    "structure_critic_model": "structure_critic_model",
    "coverage_critic_model": "coverage_critic_model",
    "evidence_critic_model": "evidence_critic_model",
    "arbitrator_model": "arbitrator_model",
}

_CANDIDATE_NODE_RE = re.compile(r"^candidate_\d+_provider_generation$")

GENERATION_ROLE = "candidate_provider_generation"
ARBITRATION_ROLE = "arbitration"

# The roles whose job is to review the generated candidate outlines.
#
# "Self-review" is not "any two roles share a provider" -- two critics sharing a
# model is fine, because they critique the *candidates*, not each other.  The
# defect this module exists to catch is narrower and specific: a critique that
# shares the candidate generator's identity is the same model grading its own
# homework, and it carries no independent judgement.
CANDIDATE_REVIEWER_ROLES: tuple[str, ...] = (
    "structure_critique",
    "coverage_critique",
    "evidence_critique",
)

# Candidate generation and arbitration are *meant* to share one reasoning model:
# the arbitrator has to absorb peer critiques using the same model that produced
# the candidates.  That pairing is reported as an explanatory note, never as a
# self-review defect.
INTENDED_SHARED_IDENTITY_ROLES = frozenset({GENERATION_ROLE, ARBITRATION_ROLE})

ProviderTransport = Callable[[str, Mapping[str, Any]], Any]


def semantic_role(node_id: str) -> str:
    """Collapse a concrete node id onto its semantic role.

    ``candidate_3_provider_generation`` -> ``candidate_provider_generation``.
    Concrete candidate indices are *not* routing identities: all candidates
    must come from the same configured generation model, otherwise the
    candidate set is not a like-for-like comparison.
    """

    node = str(node_id or "").strip()
    if _CANDIDATE_NODE_RE.match(node):
        return "candidate_provider_generation"
    return node


@dataclass(frozen=True)
class OutlineRoleRoute:
    """One resolved route: which provider serves one semantic role."""

    role: str
    config_section: str
    provider_name: str
    model: str
    endpoint_type: str
    profile: ProviderContextProfile
    transport: ProviderTransport | None = None

    @property
    def identity(self) -> tuple[str, str, str]:
        """The identity that receipts and replay hashes must bind to."""

        return (self.provider_name, self.model, self.endpoint_type)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "config_section": self.config_section,
            "provider": self.provider_name,
            "model": self.model,
            "endpoint_type": self.endpoint_type,
        }


@dataclass(frozen=True)
class OutlineProviderRouter:
    """Resolve a concrete Outline node id to its configured provider route."""

    routes: Mapping[str, OutlineRoleRoute]
    diagnostics: tuple[str, ...] = field(default=())

    def route_for(self, node_id: str) -> OutlineRoleRoute:
        role = semantic_role(node_id)
        route = self.routes.get(role)
        if route is None:
            raise KeyError(
                f"no Outline provider route configured for node {node_id!r} "
                f"(semantic role {role!r}); refusing to fall back to a "
                f"different provider"
            )
        return route

    def provider_for(self, node_id: str) -> ProviderTransport | None:
        return self.route_for(node_id).transport

    def profile_for(self, node_id: str) -> ProviderContextProfile:
        return self.route_for(node_id).profile

    def distinct_identities(self) -> tuple[tuple[str, str, str], ...]:
        seen: dict[tuple[str, str, str], None] = {}
        for role in ROLE_SETTING_KEYS:
            route = self.routes.get(role)
            if route is not None:
                seen.setdefault(route.identity, None)
        return tuple(seen)

    def routing_plan(self) -> dict[str, dict[str, Any]]:
        """Audit-facing projection of the full role -> provider table."""

        return {role: self.routes[role].to_dict() for role in ROLE_SETTING_KEYS if role in self.routes}


def collect_routing_diagnostics(routes: Mapping[str, OutlineRoleRoute]) -> tuple[str, ...]:
    """Report review relationships that carry no independent judgement.

    Two roles sharing a provider is legal -- a user may deliberately run a
    single-model configuration -- but it must never happen invisibly.  The check
    is deliberately narrow and relationship-based rather than "any two roles
    match": two *critics* sharing a model is fine because they critique the
    candidates rather than each other.  What must be surfaced is a critique that
    shares the *generator's* identity, since that critique is the same model
    grading its own homework.
    """

    diagnostics: list[str] = []
    generation = routes.get(GENERATION_ROLE)

    if generation is not None:
        provider, model, endpoint = generation.identity
        for role in CANDIDATE_REVIEWER_ROLES:
            route = routes.get(role)
            if route is not None and route.identity == generation.identity:
                diagnostics.append(
                    f"outline role {role} shares the candidate generator's provider identity "
                    f"(provider={provider!r}, model={model!r}, endpoint_type={endpoint!r}); "
                    "this critique is self-review and carries no independent judgement"
                )

    arbitration = routes.get(ARBITRATION_ROLE)
    if generation is not None and arbitration is not None and generation.identity == arbitration.identity:
        diagnostics.append(
            "outline candidate generation and arbitration share one provider identity; "
            "this is the intended default because arbitration must absorb peer critiques "
            "using the same reasoning model that produced the candidates"
        )
    return tuple(diagnostics)


def build_outline_provider_router(
    *,
    settings: Any,
    config: Mapping[str, Any],
    route_resolver: Callable[[str, str], OutlineRoleRoute | None],
) -> OutlineProviderRouter:
    """Build the router from ``[OutlineModels]`` via an injected resolver.

    ``route_resolver`` receives ``(role, section_name)`` where the section name
    is the API section selected by that role's settings getter (for example
    ``"Outline_API"``), and returns the already resolved route.  Returning
    ``None`` means "this role is not configured"; it is reported as a
    diagnostic rather than being silently remapped onto another provider.
    """

    del config  # retained for signature stability; resolution is injected.

    routes: dict[str, OutlineRoleRoute] = {}
    missing: list[str] = []

    for role, setting_key in ROLE_SETTING_KEYS.items():
        accessor = SETTING_ACCESSORS.get(setting_key, setting_key)
        getter = getattr(settings, accessor, None)
        section_name = str(getter() if callable(getter) else "").strip()
        if not section_name:
            missing.append(f"{setting_key} (role {role})")
            continue
        route = route_resolver(role, section_name)
        if route is None:
            missing.append(f"{setting_key} -> section {section_name!r} (role {role})")
            continue
        routes[role] = route

    diagnostics: list[str] = []
    if missing:
        diagnostics.append(
            "outline roles without a resolved provider route: " + ", ".join(sorted(missing))
        )
    diagnostics.extend(collect_routing_diagnostics(routes))

    return OutlineProviderRouter(routes=routes, diagnostics=tuple(diagnostics))

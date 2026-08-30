"""Regression tests for role-aware multi-model Outline routing.

These tests exist to prove the Outline stage is no longer a single provider
reviewing itself.  They assert on the *routing table*, the *call plans*, and
the *replay binding* -- not merely on the presence of config keys.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from outline.provider_router import (
    OutlineProviderRouter,
    OutlineRoleRoute,
    build_outline_provider_router,
    collect_routing_diagnostics,
    semantic_role,
)
from outline.v3_executor import OutlineV3Executor
from runtime.provider_context import ProviderContextProfile
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace

from test_outline_v3_semantic_execution import (
    _configured_test_provider,
    _summary,
)

# ---------------------------------------------------------------------------
# The target local routing table. Provider families and endpoint types are
# deliberately distinct so a same-model collapse cannot pass by accident.
# ---------------------------------------------------------------------------
CLAUDE = ("anthropic", "claude-opus-5", "messages")
GPT = ("openai_responses", "gpt-5.6-sol", "responses")
DEEPSEEK = ("deepseek", "deepseek-v4-pro", "chat_completions")

TARGET_ROUTES: dict[str, tuple[str, str, str]] = {
    "relation_adjudication": DEEPSEEK,
    "candidate_provider_generation": CLAUDE,
    "structure_critique": GPT,
    "coverage_critique": DEEPSEEK,
    "evidence_critique": GPT,
    "arbitration": CLAUDE,
}


def _route(role: str, identity: tuple[str, str, str], *, transport: Any = None) -> OutlineRoleRoute:
    provider, model, endpoint_type = identity
    return OutlineRoleRoute(
        role=role,
        config_section=f"{role.upper()}_API",
        provider_name=provider,
        model=model,
        endpoint_type=endpoint_type,
        profile=ProviderContextProfile.conservative(
            provider=provider,
            model=model,
            endpoint_type=endpoint_type,
            model_context_limit=200_000,
            max_output_tokens=8_000,
        ),
        transport=transport,
    )


def _target_router(*, transport: Any = None) -> OutlineProviderRouter:
    routes = {role: _route(role, identity, transport=transport) for role, identity in TARGET_ROUTES.items()}
    return OutlineProviderRouter(routes=routes, diagnostics=collect_routing_diagnostics(routes))


def _executor(tmp_path: Path, *, router: OutlineProviderRouter, **kwargs: Any) -> OutlineV3Executor:
    workspace = JobWorkspace.create(str(tmp_path), "outline", job_id="outline-routing-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    return OutlineV3Executor(
        job_id=workspace.job_id,
        summaries=[
            _summary("paper-a", "Study A", "The treatment improved the outcome."),
            _summary("paper-b", "Study B", "The treatment improved the outcome under a different context."),
        ],
        workspace=workspace,
        artifact_registry=registry,
        provider=_configured_test_provider,
        provider_router=router,
        candidate_count=2,
        stability_mode=kwargs.pop("stability_mode", "smoke"),
        pricing_source=kwargs.pop("pricing_source", "tests:explicit-rates-v1"),
        input_cost_per_1k_tokens=0.0,
        output_cost_per_1k_tokens=0.001,
        reasoning_cost_per_1k_tokens=0.001,
        cache_read_cost_per_1k_tokens=0.0,
        cache_write_cost_per_1k_tokens=0.0,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Unit level: routing table semantics
# ---------------------------------------------------------------------------


def test_candidate_indices_collapse_to_one_generation_role() -> None:
    assert semantic_role("candidate_1_provider_generation") == "candidate_provider_generation"
    assert semantic_role("candidate_5_provider_generation") == "candidate_provider_generation"


def test_each_role_resolves_to_its_configured_model() -> None:
    router = _target_router()
    assert router.route_for("candidate_1_provider_generation").identity == CLAUDE
    assert router.route_for("structure_critique").identity == GPT
    assert router.route_for("coverage_critique").identity == DEEPSEEK
    assert router.route_for("evidence_critique").identity == GPT
    assert router.route_for("arbitration").identity == CLAUDE
    assert router.route_for("relation_adjudication").identity == DEEPSEEK


def test_three_distinct_model_identities_enter_the_routing_table() -> None:
    identities = _target_router().distinct_identities()
    assert set(identities) == {CLAUDE, GPT, DEEPSEEK}
    assert len(identities) == 3


def test_unrouted_node_fails_closed_instead_of_falling_back() -> None:
    router = _target_router()
    with pytest.raises(KeyError):
        router.route_for("totally_unknown_node")


def test_same_model_configuration_is_reported_not_hidden() -> None:
    routes = {role: _route(role, CLAUDE) for role in TARGET_ROUTES}
    diagnostics = collect_routing_diagnostics(routes)

    joined = " ".join(diagnostics)
    assert "self-review" in joined
    # The generation/arbitration pairing is expected and must be explained,
    # not lumped in with the accidental-collapse warning.
    assert "intended default" in joined


def test_distinct_roles_produce_no_self_review_diagnostic() -> None:
    diagnostics = _target_router().diagnostics
    assert not any("self-review" in item for item in diagnostics), diagnostics


# ---------------------------------------------------------------------------
# Executor level: call plans and replay binding
# ---------------------------------------------------------------------------


def test_call_plans_carry_per_node_route_identity(tmp_path: Path) -> None:
    executor = _executor(tmp_path, router=_target_router())
    executor.run()

    plans = executor.provider_call_plans
    assert plans, "expected the executor to build provider call plans"

    by_node = {plan.node_id: plan for plan in plans}
    for node_id in executor._provider_node_ids():
        expected = TARGET_ROUTES[semantic_role(node_id)]
        plan = by_node[node_id]
        assert (plan.provider, plan.model, plan.endpoint_type) == expected, (
            f"{node_id} was planned against the wrong provider identity"
        )


def test_call_plan_contains_three_model_families(tmp_path: Path) -> None:
    executor = _executor(tmp_path, router=_target_router())
    executor.run()

    models = {plan.model for plan in executor.provider_call_plans}
    assert models == {"claude-opus-5", "gpt-5.6-sol", "deepseek-v4-pro"}, models


def test_stability_variants_keep_role_routing(tmp_path: Path) -> None:
    executor = _executor(tmp_path, router=_target_router(), stability_mode="smoke")
    executor.run()

    seen_variants = {plan.variant_name for plan in executor.provider_call_plans}
    assert len(seen_variants) > 1, "smoke mode should plan more than one variant"

    for plan in executor.provider_call_plans:
        expected = TARGET_ROUTES[semantic_role(plan.node_id)]
        assert (plan.provider, plan.model, plan.endpoint_type) == expected, (
            f"variant {plan.variant_name} lost role routing on {plan.node_id}"
        )


def test_replay_identity_binds_to_the_role_route(tmp_path: Path) -> None:
    """Changing one critic's model must invalidate the replay binding.

    If the route identity were not part of the context hash, a receipt produced
    by one model could be replayed onto a differently routed node.
    """

    base = _executor(tmp_path / "base", router=_target_router())
    base.run()
    baseline_hash = base._context_profile_hash()

    swapped = dict(TARGET_ROUTES)
    swapped["evidence_critique"] = CLAUDE  # was GPT
    routes = {role: _route(role, identity) for role, identity in swapped.items()}
    changed = _executor(
        tmp_path / "changed",
        router=OutlineProviderRouter(routes=routes, diagnostics=collect_routing_diagnostics(routes)),
    )
    changed.run()

    assert changed._context_profile_hash() != baseline_hash


def test_role_routing_survives_resume_without_changing_roles(tmp_path: Path) -> None:
    first = _executor(tmp_path, router=_target_router())
    first.run()

    second = _executor(tmp_path, router=_target_router())
    second.run()

    def table(executor: OutlineV3Executor) -> dict[str, tuple[str, str, str]]:
        return {
            plan.node_id: (plan.provider, plan.model, plan.endpoint_type)
            for plan in executor.provider_call_plans
        }

    assert table(first) == table(second)


def test_build_router_reports_unresolvable_roles() -> None:
    class _Settings:
        def outline_model(self) -> str:
            return "Outline_API"

        def relation_adjudicator_model(self) -> str:
            return "Free_Mode_API"

        def structure_critic_model(self) -> str:
            return "Writer_API"

        def coverage_critic_model(self) -> str:
            return "Free_Mode_API"

        def evidence_critic_model(self) -> str:
            return "Writer_API"

        def arbitrator_model(self) -> str:
            return "Outline_API"

    def resolver(role: str, section: str) -> OutlineRoleRoute | None:
        mapping = {
            "Outline_API": CLAUDE,
            "Writer_API": GPT,
            "Free_Mode_API": DEEPSEEK,
        }
        identity = mapping.get(section)
        if identity is None:
            return None
        return _route(role, identity)

    router = build_outline_provider_router(
        settings=_Settings(), config={}, route_resolver=resolver
    )

    assert router.route_for("candidate_2_provider_generation").identity == CLAUDE
    assert router.route_for("structure_critique").identity == GPT
    assert router.route_for("coverage_critique").identity == DEEPSEEK
    assert router.route_for("evidence_critique").identity == GPT
    assert router.route_for("arbitration").identity == CLAUDE
    assert router.route_for("relation_adjudication").identity == DEEPSEEK
    assert not any("without a resolved provider route" in item for item in router.diagnostics)


def test_build_router_reports_unresolvable_role_instead_of_guessing() -> None:
    class _Settings:
        def outline_model(self) -> str:
            return "Outline_API"

        def relation_adjudicator_model(self) -> str:
            return "Free_Mode_API"

        def structure_critic_model(self) -> str:
            return "Writer_API"

        def coverage_critic_model(self) -> str:
            return "Free_Mode_API"

        def evidence_critic_model(self) -> str:
            return "Writer_API"

        def arbitrator_model(self) -> str:
            return "Outline_API"

    def resolver(role: str, section: str) -> OutlineRoleRoute | None:
        if section != "Outline_API":
            return None
        return _route(role, CLAUDE)

    router = build_outline_provider_router(
        settings=_Settings(), config={}, route_resolver=resolver
    )

    assert any("without a resolved provider route" in item for item in router.diagnostics)
    # The unresolvable roles are simply absent; they are never remapped.
    with pytest.raises(KeyError):
        router.route_for("structure_critique")

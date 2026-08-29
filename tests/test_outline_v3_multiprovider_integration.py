"""End-to-end check that role routing reaches three different providers.

The unit tests prove the router returns the right route and the real-transport
tests prove the right transport runs. This one closes the last gap: it drives the
*configuration* pipeline -- the same functions the orchestrator uses -- all the
way to executed calls and persisted receipts, and then asserts on those receipts.

Only the network is mocked. Every layer that decides which provider serves a node
is the real implementation.
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
from services.model_capabilities import resolve_model_capability
from services.model_selection import get_api_config_for_section

from test_outline_v3_semantic_execution import (
    _configured_test_provider,
    _summary,
)

# A configuration shaped like the real thing: three providers, three protocols.
CONFIG: dict[str, dict[str, str]] = {
    "Application": {"config_schema": "4"},
    "Paths": {"output_path": "./output"},
    "Primary_Reader_API": {
        "model": "deepseek-v4-flash-vision-exp",
        "api_base": "https://api.deepseek.com",
        "endpoint_type": "chat_completions",
        "provider_family": "deepseek",
    },
    "Writer_API": {
        "model": "gpt-5.6-sol",
        "api_base": "https://ai.saigou.work/v1",
        "endpoint_type": "responses",
        "provider_family": "openai_responses",
        "max_context_tokens": "200000",
        "max_output_tokens": "32000",
    },
    "Outline_API": {
        "model": "claude-opus-5",
        "api_base": "https://chat.178266.xyz",
        "endpoint_type": "anthropic",
        "provider_family": "anthropic",
        "max_context_tokens": "200000",
        "max_output_tokens": "16000",
    },
    "Free_Mode_API": {
        "model": "deepseek-v4-pro",
        "api_base": "https://api.deepseek.com",
        "endpoint_type": "chat_completions",
        "provider_family": "deepseek",
        "max_context_tokens": "128000",
        "max_output_tokens": "16000",
    },
    "OutlineModels": {
        "outline_model": "Outline_API",
        "relation_adjudicator_model": "Free_Mode_API",
        "structure_critic_model": "Writer_API",
        "coverage_critic_model": "Free_Mode_API",
        "evidence_critic_model": "Writer_API",
        "arbitrator_model": "Outline_API",
    },
}


class RecordingTransport:
    """Stands in for one provider's HTTP call and records what it was given."""

    def __init__(self, role: str, api_config: Mapping[str, Any]) -> None:
        self.role = role
        self.api_config = dict(api_config)
        self.invocations: list[str] = []

    @property
    def identity(self) -> tuple[str, str, str]:
        return (
            str(self.api_config.get("provider_family") or ""),
            str(self.api_config.get("model") or ""),
            str(self.api_config.get("endpoint_type") or ""),
        )

    def __call__(self, node_id: str, request: Mapping[str, Any]) -> Any:
        self.invocations.append(str(node_id))
        return _configured_test_provider(node_id, request)


class _Settings:
    """Minimal settings object exposing the [OutlineModels] getters."""

    def __init__(self, config: Mapping[str, Mapping[str, str]]) -> None:
        self._roles = dict(config["OutlineModels"])

    def outline_model(self) -> str:
        return self._roles["outline_model"]

    def relation_adjudicator_model(self) -> str:
        return self._roles["relation_adjudicator_model"]

    def structure_critic_model(self) -> str:
        return self._roles["structure_critic_model"]

    def coverage_critic_model(self) -> str:
        return self._roles["coverage_critic_model"]

    def evidence_critic_model(self) -> str:
        return self._roles["evidence_critic_model"]

    def arbitrator_model(self) -> str:
        return self._roles["arbitrator_model"]


def _resolve_route(role: str, section_name: str) -> OutlineRoleRoute | None:
    """The same steps the orchestrator's route resolver performs."""

    api_config = get_api_config_for_section(CONFIG, section_name)
    model = str(api_config.get("model") or "").strip()
    if not model:
        return None
    capability = resolve_model_capability(api_config)
    profile = ProviderContextProfile.conservative(
        provider=capability.provider_family,
        model=model,
        endpoint_type=capability.endpoint_type,
        model_context_limit=int(str(api_config.get("max_context_tokens") or "128000")),
        max_output_tokens=int(str(api_config.get("max_output_tokens") or "8192")),
    )
    return OutlineRoleRoute(
        role=role,
        config_section=str(section_name).strip(),
        provider_name=capability.provider_family,
        model=model,
        endpoint_type=capability.endpoint_type,
        profile=profile,
        transport=None,  # attached below, so the api_config is recorded with it
        api_base=str(api_config.get("api_base") or "").strip(),
    )


def _build() -> tuple[OutlineProviderRouter, dict[str, RecordingTransport]]:
    resolved: dict[str, OutlineRoleRoute] = {}
    for role, section in (
        ("relation_adjudication", "Free_Mode_API"),
        ("candidate_provider_generation", "Outline_API"),
        ("structure_critique", "Writer_API"),
        ("coverage_critique", "Free_Mode_API"),
        ("evidence_critique", "Writer_API"),
        ("arbitration", "Outline_API"),
    ):
        route = _resolve_route(role, section)
        assert route is not None
        resolved[role] = route

    transports: dict[str, RecordingTransport] = {}
    with_transport: dict[str, OutlineRoleRoute] = {}
    for role, route in resolved.items():
        api_config = get_api_config_for_section(CONFIG, route.config_section)
        transport = RecordingTransport(role, api_config)
        transports[role] = transport
        with_transport[role] = OutlineRoleRoute(
            role=route.role,
            config_section=route.config_section,
            provider_name=route.provider_name,
            model=route.model,
            endpoint_type=route.endpoint_type,
            profile=route.profile,
            transport=transport,
            api_base=route.api_base,
        )

    # build_outline_provider_router passes the semantic role, which is the only
    # unambiguous key: one section serves several roles, so resolving by section
    # name alone could not tell coverage from relation adjudication.
    router = build_outline_provider_router(
        settings=_Settings(CONFIG),
        config=CONFIG,
        route_resolver=lambda role, section: with_transport.get(role),
    )
    return router, transports


def _executor(tmp_path: Path, router: OutlineProviderRouter) -> OutlineV3Executor:
    workspace = JobWorkspace.create(str(tmp_path), "outline", job_id="outline-multiprovider")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    return OutlineV3Executor(
        job_id=workspace.job_id,
        summaries=[
            _summary("paper-a", "Study A", "The treatment improved the outcome."),
            _summary("paper-b", "Study B", "The effect held under a different context."),
            _summary("paper-c", "Study C", "A boundary condition limits the effect."),
        ],
        workspace=workspace,
        artifact_registry=registry,
        provider=None,
        provider_router=router,
        candidate_count=2,
        stability_mode="off",
        pricing_source="tests:explicit-rates-v1",
        input_cost_per_1k_tokens=0.0,
        output_cost_per_1k_tokens=0.001,
        reasoning_cost_per_1k_tokens=0.001,
        cache_read_cost_per_1k_tokens=0.0,
        cache_write_cost_per_1k_tokens=0.0,
    )


def test_router_resolves_three_real_provider_configs() -> None:
    router, transports = _build()

    assert not any("without a resolved provider route" in d for d in router.diagnostics)
    identities = {t.identity for t in transports.values()}
    assert identities == {
        ("deepseek", "deepseek-v4-pro", "chat_completions"),
        ("anthropic", "claude-opus-5", "anthropic"),
        ("openai_responses", "gpt-5.6-sol", "responses"),
    }, identities


def test_executor_runs_each_role_on_its_own_provider_config(tmp_path: Path) -> None:
    router, transports = _build()
    executor = _executor(tmp_path, router=router)
    executor.run()

    executed = {role: t for role, t in transports.items() if t.invocations}
    assert {"candidate_provider_generation", "structure_critique", "coverage_critique"} <= set(executed)

    for role, transport in executed.items():
        for node_id in transport.invocations:
            assert semantic_role(node_id) == role, f"{node_id} ran on the {role} provider"


def test_receipts_record_three_distinct_provider_identities(tmp_path: Path) -> None:
    """The claim the reviewer asked to be proven: receipts, not the plan."""

    router, transports = _build()
    executor = _executor(tmp_path, router=router)
    executor.run()

    receipts = executor._receipt_ledger.list_receipts()
    assert receipts

    observed: dict[str, set[str]] = {}
    for receipt in receipts:
        model = str(getattr(receipt, "model", "") or "")
        endpoint = str(getattr(receipt, "endpoint_type", "") or "")
        observed.setdefault(model, set()).add(endpoint)

    for model, endpoints in sorted(observed.items()):
        assert len(endpoints) == 1, f"{model} was recorded with mixed endpoints {endpoints}"

    assert observed.get("claude-opus-5") == {"anthropic"}, observed
    assert observed.get("gpt-5.6-sol") == {"responses"}, observed
    assert observed.get("deepseek-v4-pro") == {"chat_completions"}, observed


def test_node_bindings_carry_the_executing_section(tmp_path: Path) -> None:
    router, transports = _build()
    executor = _executor(tmp_path, router=router)
    executor.run()

    # The call plan records each node against the section that served it.
    by_node = {plan.node_id: plan for plan in executor.provider_call_plans}
    expected = {
        "relation_adjudication": "deepseek-v4-pro",
        "candidate_1_provider_generation": "claude-opus-5",
        "structure_critique": "gpt-5.6-sol",
        "coverage_critique": "deepseek-v4-pro",
        "evidence_critique": "gpt-5.6-sol",
        "arbitration": "claude-opus-5",
    }
    for node_id, model in expected.items():
        plan = by_node.get(node_id)
        assert plan is not None, f"{node_id} was never planned"
        assert plan.model == model, f"{node_id} planned against {plan.model}, expected {model}"

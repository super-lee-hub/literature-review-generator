"""Prove the routed transport is the transport that actually runs.

The routing-table tests in ``test_outline_v3_multi_model_routing.py`` assert on
what the router *returns* and what the call plan *records*. Both can be correct
while the executor still calls the single provider it was constructed with --
which is precisely what used to happen, and what a plan-level test cannot see.

These tests close that gap:

* three sentinel transports stand in for Claude, GPT and DeepSeek, and each one
  records every node it is actually asked to execute;
* the executor's legacy ``provider`` is replaced with an object that raises if
  touched, so any silent fallback to single-provider behaviour fails loudly;
* the assertions are on captured invocations and on persisted receipt identity,
  never on the routing plan alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from outline.provider_router import (
    OutlineProviderRouter,
    OutlineRoleRoute,
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

CLAUDE = ("anthropic", "claude-opus-5", "anthropic")
GPT = ("openai_responses", "gpt-5.6-sol", "responses")
DEEPSEEK = ("deepseek", "deepseek-v4-pro", "chat_completions")

# Which role must execute on which model.
ROLE_MODEL: dict[str, tuple[str, str, str]] = {
    "relation_adjudication": DEEPSEEK,
    "candidate_provider_generation": CLAUDE,
    "structure_critique": GPT,
    "coverage_critique": DEEPSEEK,
    "evidence_critique": GPT,
    "arbitration": CLAUDE,
}

SECTION_FOR_ROLE: dict[str, str] = {
    "relation_adjudication": "Free_Mode_API",
    "candidate_provider_generation": "Outline_API",
    "structure_critique": "Writer_API",
    "coverage_critique": "Free_Mode_API",
    "evidence_critique": "Writer_API",
    "arbitration": "Outline_API",
}

HOST_FOR_ROLE: dict[str, str] = {
    "relation_adjudication": "api.deepseek.com",
    "candidate_provider_generation": "chat.178266.xyz",
    "structure_critique": "ai.saigou.work",
    "coverage_critique": "api.deepseek.com",
    "evidence_critique": "ai.saigou.work",
    "arbitration": "chat.178266.xyz",
}


class ExplodingProvider:
    """Stands in for the legacy single provider. Any use is a routing failure."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, node_id: str, request: Mapping[str, Any]) -> Any:
        self.calls.append(str(node_id))
        raise AssertionError(
            f"legacy single provider must not be used when a role router is active "
            f"(node {node_id!r})"
        )

    def call(self, node_id: str, request: Mapping[str, Any]) -> Any:
        return self(node_id, request)


class SentinelTransport:
    """A real transport for one model that records every node it executes."""

    def __init__(self, label: str, identity: tuple[str, str, str]) -> None:
        self.label = label
        self.identity = identity
        self.invocations: list[str] = []

    def __call__(self, node_id: str, request: Mapping[str, Any]) -> Any:
        self.invocations.append(str(node_id))
        # Delegate to the project's configured fixture provider so the response
        # is valid for the node; only the invocation is being asserted here.
        return _configured_test_provider(node_id, request)


def _build_router() -> tuple[OutlineProviderRouter, dict[str, SentinelTransport]]:
    transports: dict[str, SentinelTransport] = {}
    routes: dict[str, OutlineRoleRoute] = {}

    for role, identity in ROLE_MODEL.items():
        provider, model, endpoint_type = identity
        transport = SentinelTransport(role, identity)
        transports[role] = transport
        routes[role] = OutlineRoleRoute(
            role=role,
            config_section=SECTION_FOR_ROLE[role],
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
            api_base=f"https://{HOST_FOR_ROLE[role]}",
        )

    return OutlineProviderRouter(routes=routes, diagnostics=collect_routing_diagnostics(routes)), transports


def _executor(
    tmp_path: Path,
    *,
    router: OutlineProviderRouter,
    poison: ExplodingProvider,
    **kwargs: Any,
) -> OutlineV3Executor:
    workspace = JobWorkspace.create(str(tmp_path), "outline", job_id="outline-real-transport")
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
        provider=poison,
        provider_router=router,
        candidate_count=2,
        stability_mode=kwargs.pop("stability_mode", "off"),
        pricing_source=kwargs.pop("pricing_source", "tests:explicit-rates-v1"),
        input_cost_per_1k_tokens=0.0,
        output_cost_per_1k_tokens=0.001,
        reasoning_cost_per_1k_tokens=0.001,
        cache_read_cost_per_1k_tokens=0.0,
        cache_write_cost_per_1k_tokens=0.0,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The core assertion: which transport actually ran each node
# ---------------------------------------------------------------------------


def test_every_node_executes_on_its_own_transport(tmp_path: Path) -> None:
    router, transports = _build_router()
    poison = ExplodingProvider()
    executor = _executor(tmp_path, router=router, poison=poison)
    executor.run()

    assert poison.calls == [], "executor fell back to the single legacy provider"

    seen: dict[str, str] = {}
    for role, transport in transports.items():
        for node_id in transport.invocations:
            seen[semantic_role(node_id)] = role

    assert seen, "no routed transport was invoked at all"

    # Every role that must run has run, on the transport bound to that role.
    for role in ROLE_MODEL:
        if role == "relation_adjudication":
            continue  # only runs when relation adjudication is enabled
        assert transports[role].invocations, f"{role} never executed"

    for role, transport in transports.items():
        for node_id in transport.invocations:
            assert semantic_role(node_id) == role, (
                f"node {node_id!r} was executed by the {role} transport "
                f"(model {transport.identity[1]}), which is the wrong route"
            )


def test_three_distinct_transports_are_actually_used(tmp_path: Path) -> None:
    router, transports = _build_router()
    poison = ExplodingProvider()
    executor = _executor(tmp_path, router=router, poison=poison)
    executor.run()

    used = {role for role, transport in transports.items() if transport.invocations}
    assert {"candidate_provider_generation", "structure_critique", "coverage_critique"} <= used, used

    models_used = {transports[role].identity[1] for role in used}
    assert len(models_used) == 3, f"expected three distinct models to run, got {sorted(models_used)}"


def test_routed_node_without_transport_fails_closed(tmp_path: Path) -> None:
    """A route with no transport must not fall back to the single provider."""

    routes = {
        role: OutlineRoleRoute(
            role=role,
            config_section=SECTION_FOR_ROLE[role],
            provider_name=identity[0],
            model=identity[1],
            endpoint_type=identity[2],
            profile=ProviderContextProfile.conservative(
                provider=identity[0], model=identity[1], endpoint_type=identity[2]
            ),
            transport=None if role == "structure_critique" else _noop_transport(),
            api_base=f"https://{HOST_FOR_ROLE[role]}",
        )
        for role, identity in ROLE_MODEL.items()
    }
    router = OutlineProviderRouter(routes=routes, diagnostics=collect_routing_diagnostics(routes))
    executor = _executor(tmp_path / "no-transport", router=router, poison=ExplodingProvider())

    with pytest.raises(Exception) as excinfo:  # noqa: BLE001
        executor._resolve_node_transport("structure_critique", routes["structure_critique"])
    assert "refusing to fall back" in str(excinfo.value)


def _noop_transport():
    def _call(node_id: str, request: Mapping[str, Any]) -> Any:
        return _configured_test_provider(node_id, request)

    return _call


# ---------------------------------------------------------------------------
# Receipts carry the identity of the model that really ran
# ---------------------------------------------------------------------------


def test_receipts_carry_the_executing_model_identity(tmp_path: Path) -> None:
    router, transports = _build_router()
    poison = ExplodingProvider()
    executor = _executor(tmp_path, router=router, poison=poison)
    executor.run()

    receipts = executor._receipt_ledger.list_receipts()
    assert receipts, "expected provider receipts to be recorded"

    by_model: dict[str, set[str]] = {}
    for receipt in receipts:
        model = str(getattr(receipt, "model", "") or "")
        endpoint = str(getattr(receipt, "endpoint_type", "") or "")
        by_model.setdefault(model, set()).add(endpoint)

    executed_models = {t.identity[1] for t in transports.values() if t.invocations}
    for model in executed_models:
        assert model in by_model, f"no receipt recorded for executed model {model}"

    # gpt-5.6-sol must be recorded as responses, never as anthropic.
    if "gpt-5.6-sol" in by_model:
        assert by_model["gpt-5.6-sol"] == {"responses"}, by_model["gpt-5.6-sol"]


def test_stability_variants_use_the_same_role_transports(tmp_path: Path) -> None:
    """Stability variants are extra real calls, not a different routing.

    Note the transport is invoked with the *semantic* node id, not the
    ``stability:<audit>:<base>`` audit id, so the assertion is on call volume and
    on role mapping rather than on the audit prefix.
    """

    off_router, off_transports = _build_router()
    off_poison = ExplodingProvider()
    _executor(tmp_path / "off", router=off_router, poison=off_poison, stability_mode="off").run()
    off_total = sum(len(t.invocations) for t in off_transports.values())

    smoke_router, smoke_transports = _build_router()
    smoke_poison = ExplodingProvider()
    _executor(tmp_path / "smoke", router=smoke_router, poison=smoke_poison, stability_mode="smoke").run()
    smoke_total = sum(len(t.invocations) for t in smoke_transports.values())

    assert off_poison.calls == [] and smoke_poison.calls == [], "stability fell back to the legacy provider"
    assert smoke_total > off_total, (
        f"stability smoke should add real provider calls (off={off_total}, smoke={smoke_total})"
    )

    for role, transport in smoke_transports.items():
        for node_id in transport.invocations:
            assert semantic_role(node_id) == role, (
                f"stability variant {node_id!r} ran on the {role} transport"
            )

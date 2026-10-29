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

from dataclasses import replace
import hashlib
import json
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
from outline.v3_models import OutlineQualityGate
from runtime.provider_context import ProviderContextProfile
from runtime.provider_runtime import ProviderRuntimeLedger, hash_json
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry, RegistryError
from services.job_workspace import JobWorkspace

from test_outline_v3_semantic_execution import _executor as _fixture_executor, _summary

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
        # Use the same source-complete local fixture as the production-path
        # HTTP tests. The older generic fixture emitted a cross-group claim
        # without source_claim_ids, so candidate-scope validation stopped the
        # DAG before the routed critique transports could run.
        from tests.test_current_runtime_full_e2e import _outline_provider_response

        response = dict(_outline_provider_response(node_id, request))
        # Routed provider receipts require usage evidence for non-fixture
        # endpoint types. The sentinel represents a successful transport, so
        # provide deterministic usage metadata rather than testing an unrelated
        # usage-reporting failure.
        response.update(
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "usage_status": "reported",
            }
        )
        return response


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
            config_identity={"transport_retries": "0"},
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
        from tests.test_current_runtime_full_e2e import _outline_provider_response

        return _outline_provider_response(node_id, request)

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


def _route_change_router(
    base_router: OutlineProviderRouter,
    *,
    changed_role: str = "evidence_critique",
    changed_identity: tuple[str, str, str] = (
        "openai_responses",
        "gpt-5.6-sol-v2",
        "responses",
    ),
    changed_api_base: str = "https://ai.saigou-alt.work/v1",
) -> tuple[OutlineProviderRouter, dict[str, SentinelTransport]]:
    """Clone the router while changing only one role route."""

    routes: dict[str, OutlineRoleRoute] = {}
    transports: dict[str, SentinelTransport] = {}
    for role, route in base_router.routes.items():
        identity = route.identity
        api_base = route.api_base
        profile = route.profile
        if role == changed_role:
            identity = changed_identity
            api_base = changed_api_base
            profile = ProviderContextProfile.conservative(
                provider=identity[0],
                model=identity[1],
                endpoint_type=identity[2],
                model_context_limit=200_000,
                max_output_tokens=8_000,
            )
        transport = SentinelTransport(role, identity)
        transports[role] = transport
        routes[role] = replace(
            route,
            model=identity[1],
            provider_name=identity[0],
            endpoint_type=identity[2],
            profile=profile,
            transport=transport,
            api_base=api_base,
        )
    return OutlineProviderRouter(
        routes=routes,
        diagnostics=collect_routing_diagnostics(routes),
    ), transports


def _selective_replay_quality_gate() -> OutlineQualityGate:
    # The transport/replay test intentionally uses a compact fixture with
    # repeated paper assignments. Relax only semantic quality thresholds so the
    # assertion reaches the current closure rather than adoption policy.
    return OutlineQualityGate(
        coverage_scope="full",
        min_canonical_coverage_full=0.0,
        min_canonical_coverage_local=0.0,
        min_effective_sections=1,
        max_duplicate_assignments=20,
        block_placeholder_sections=True,
        block_empty_research_streams=False,
    )


def _local_source_scope_executor(
    tmp_path: Path,
    calls: list[str],
    *,
    workspace: JobWorkspace | None = None,
    logical_attempt_identity: str | None = None,
    quality_gate: OutlineQualityGate | None = None,
) -> OutlineV3Executor:
    """Use the existing internal-profile semantic fixture and local responder."""

    from tests.test_current_runtime_full_e2e import _outline_provider_response

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(node_id)
        return _outline_provider_response(node_id, request)

    summaries = [
        _summary("paper-a", "Study A", "The treatment improved the outcome."),
        _summary("paper-b", "Study B", "The effect held under a different context."),
        _summary("paper-c", "Study C", "A boundary condition limits the effect."),
        _summary("paper-d", "Study D", "The result depends on the measured moderator."),
        _summary("paper-e", "Study E", "The effect weakens in a different sample."),
        _summary("paper-f", "Study F", "A second boundary condition limits the effect."),
    ]
    if workspace is None:
        executor = _fixture_executor(
            tmp_path,
            provider=provider,
            stability_mode="off",
            technical_shard_target_tokens=32_000,
            summaries=summaries,
        )
    else:
        executor = OutlineV3Executor(
            job_id=workspace.job_id,
            summaries=summaries,
            workspace=workspace,
            artifact_registry=ArtifactRegistry(workspace.paths.registry_path, workspace.job_id),
            provider=provider,
            candidate_count=2,
            stability_mode="off",
            technical_shard_target_tokens=32_000,
            logical_attempt_identity=logical_attempt_identity,
            quality_gate=quality_gate,
            pricing_source="tests:explicit-rates-v1",
            input_cost_per_1k_tokens=0.0,
            output_cost_per_1k_tokens=0.001,
            reasoning_cost_per_1k_tokens=0.001,
            cache_read_cost_per_1k_tokens=0.0,
            cache_write_cost_per_1k_tokens=0.0,
        )
    # This is the existing explicit offline semantic-source-scope mode. The
    # responder is local and source-bound; no transport or provider API is used.
    executor.semantic_provider_synthesis_enabled = True
    return executor


def _assert_local_projection_result_or_quality_fixture_block(
    result: Any, executor: OutlineV3Executor
) -> None:
    if result.ok:
        return
    health_record = executor.registry.get("outline-v3:stage_health")
    assert health_record is not None and health_record.status == "ready"
    health = json.loads(Path(health_record.path).read_text(encoding="utf-8"))["payload"]
    checks = health["quality_decision"]["enforced_checks"]
    assert checks["max_duplicate_assignments"] is False
    assert all(value for key, value in checks.items() if key != "max_duplicate_assignments")
    assert health["provider_receipt_closure"]["complete"] is True


def _assert_local_candidate_projection_closure(
    closure_payload: Mapping[str, Any], registry: ArtifactRegistry,
    *, closure_epoch_id: str, source_closure_epoch_id: str,
) -> None:
    expected = {str(value) for value in closure_payload["expected_call_ids"]}
    observed = {str(value) for value in closure_payload["observed_call_ids"]}
    reused = {str(value) for value in closure_payload["verified_reuse_call_ids"]}
    local_children = {
        call_id for call_id in expected if "_provider_generation:local:" in call_id
    }
    assert local_children
    assert {call_id for call_id in local_children if ":candidate_1_provider_generation:local:" in call_id}
    assert {call_id for call_id in local_children if ":candidate_2_provider_generation:local:" in call_id}
    assert observed == set()
    assert expected == reused
    assert local_children <= reused

    scope_record = registry.get("outline-v3:candidate_output_scope")
    assert scope_record is not None and scope_record.status == "ready"
    registry.verify_ready_artifact_closure(scope_record)
    scope_envelope = json.loads(Path(scope_record.path).read_text(encoding="utf-8"))
    scope_payload = scope_envelope.get("payload")
    if not isinstance(scope_payload, Mapping):
        scope_payload = scope_envelope
    assert isinstance(scope_payload, Mapping)
    expected_group_ids = {
        str(row["claim_group_id"])
        for row in scope_payload.get("claim_groups") or ()
        if isinstance(row, Mapping) and str(row.get("claim_group_id") or "")
    }
    assert expected_group_ids

    projections = closure_payload["logical_candidate_reuse_projections"]
    assert len(projections) == 2, projections
    assert {str(row["call_id"]) for row in projections} == {
        "outline:candidate_1_provider_generation",
        "outline:candidate_2_provider_generation",
    }
    assert all(row["status"] == "verified" for row in projections)
    assert all(row["schema_version"] == "logical-candidate-reuse-projection/v1" for row in projections)
    for projection in projections:
        child_call_ids = {str(value) for value in projection["child_call_ids"]}
        assert child_call_ids
        assert child_call_ids <= local_children
        assert child_call_ids <= reused
        assignment = dict(projection["group_assignment"])
        deferred = {str(value) for value in projection["deferred_group_ids"]}
        assert assignment
        assert not set(assignment).intersection(deferred)
        assert set(assignment) | deferred == expected_group_ids
        assert {f"outline:{node_id}" for node_id in assignment.values()} == child_call_ids
        assert projection["assignment_coverage_hash"] == hash_json(assignment)

        merged_record = registry.get(str(projection["merged_artifact_id"]))
        assert merged_record is not None and merged_record.status == "ready"
        assert merged_record.content_hash == projection["merged_artifact_hash"]
        registry.verify_ready_artifact_closure(merged_record)
        merged_envelope = json.loads(Path(merged_record.path).read_text(encoding="utf-8"))
        merged_payload = merged_envelope.get("payload")
        assert isinstance(merged_payload, Mapping)
        assert merged_payload["shard_plan"]["group_assignment"] == assignment
        assert merged_payload["shard_plan"]["assignment_coverage_hash"] == projection["assignment_coverage_hash"]

        evidence_by_call = dict(projection["verified_reuse_evidence_artifact_ids"])
        assert set(evidence_by_call) == child_call_ids
        for child_call_id, evidence_id in evidence_by_call.items():
            evidence_record = registry.get(str(evidence_id))
            assert evidence_record is not None and evidence_record.status == "ready"
            registry.verify_ready_artifact_closure(evidence_record)
            evidence = json.loads(Path(evidence_record.path).read_text(encoding="utf-8"))
            assert evidence["call_id"] == child_call_id
            assert evidence["current_closure_epoch_id"] == closure_epoch_id
            binding = evidence["current_binding"]
            assert binding["call_id"] == child_call_id
            for field in ("prompt_hash", "input_hash", "config_hash", "schema_hash"):
                value = str(binding[field])
                assert len(value) == 64 and all(char in "0123456789abcdef" for char in value)
            authority = evidence["source_authority"]
            assert authority["source_closure_epoch_id"] == source_closure_epoch_id
            source_ledger_id = str(authority["source_receipt_ledger_artifact_id"])
            source_ledger = registry.get(source_ledger_id)
            assert source_ledger is not None and source_ledger.status == "ready"
            registry.verify_ready_artifact_closure(source_ledger)
            assert source_ledger.content_hash == authority["source_receipt_ledger_content_hash"]
            receipt_id = str(authority["source_receipt_id"])
            receipts = [
                receipt
                for receipt in ProviderRuntimeLedger(source_ledger.path).list_receipts()
                if receipt.receipt_id == receipt_id
            ]
            assert len(receipts) == 1
            source_receipt = receipts[0]
            assert source_receipt.call_id == child_call_id
            assert source_receipt.closure_epoch_id == source_closure_epoch_id
            assert source_receipt.status == "success"
            assert hash_json(source_receipt.to_dict()) == authority["source_receipt_record_hash"]
            for field in ("source_receipt_ledger_content_hash", "source_receipt_record_hash"):
                value = str(authority[field])
                assert len(value) == 64 and all(char in "0123456789abcdef" for char in value)
            reused_output = evidence["reused_output"]
            source_output = registry.get(str(reused_output["registered_artifact_id"]))
            assert source_output is not None and source_output.status == "ready"
            evidence_dependency_ids = {dependency.artifact_id for dependency in evidence_record.depends_on}
            assert source_ledger.artifact_id in evidence_dependency_ids
            assert source_output.artifact_id in evidence_dependency_ids
            registry.verify_ready_artifact_closure(source_output)
            source_output_envelope = json.loads(Path(source_output.path).read_text(encoding="utf-8"))
            assert source_output.content_hash == reused_output["registered_artifact_file_hash"]
            assert source_output_envelope["content_hash"] == reused_output["registered_artifact_content_hash"]
            assert hashlib.sha256(Path(source_output.path).read_bytes()).hexdigest() == source_output.content_hash


def _assert_fragment_bank_call_closure(
    closure_payload: Mapping[str, Any], *, observed_call_ids: set[str]
) -> None:
    """Assert the exact provider-call partition for this external-route fixture.

    All routes from ``_build_router`` use real provider endpoint types, so this
    fixture takes the candidate fragment-bank/organization path. It does not
    create local hierarchical shard children or logical shard projections.
    """

    assert closure_payload["complete"] is True, closure_payload
    expected = {str(value) for value in closure_payload["expected_call_ids"]}
    observed = {str(value) for value in closure_payload["observed_call_ids"]}
    reused = {str(value) for value in closure_payload["verified_reuse_call_ids"]}
    assert expected == observed | reused, closure_payload
    assert observed.isdisjoint(reused), closure_payload
    assert observed == observed_call_ids, closure_payload
    assert reused == expected - observed, closure_payload

    candidate_calls = {call_id for call_id in expected if call_id.startswith("outline:candidate_")}
    fragment_calls = {
        call_id for call_id in candidate_calls
        if call_id.startswith("outline:candidate_comparison_fragments_provider:")
    }
    organization_calls = {
        call_id for call_id in candidate_calls
        if call_id.startswith("outline:candidate_organization_provider:")
    }
    assert len(fragment_calls) == 1, candidate_calls
    assert organization_calls == {
        "outline:candidate_organization_provider:candidate_1",
        "outline:candidate_organization_provider:candidate_2",
    }, candidate_calls
    assert expected == {
        "outline:arbitration",
        "outline:coverage_critique",
        "outline:evidence_critique",
        "outline:relation_adjudication",
        "outline:structure_critique",
        *fragment_calls,
        *organization_calls,
    }, expected
    assert not any("_provider_generation:local:" in call_id for call_id in expected)
    assert closure_payload["logical_candidate_reuse_projections"] == []


def test_route_only_change_reuses_unchanged_nodes_and_closes_current_epoch(
    tmp_path: Path,
) -> None:
    """Exercise run -> replay -> Registry -> current closure end to end."""

    first_router, first_transports = _build_router()
    first = _executor(
        tmp_path,
        router=first_router,
        poison=ExplodingProvider(),
        stability_mode="off",
        quality_gate=_selective_replay_quality_gate(),
    )
    first_result = first.run()
    assert first_result.ok is True, first_result

    second_router, second_transports = _route_change_router(first_router)
    second = _executor(
        tmp_path,
        router=second_router,
        poison=ExplodingProvider(),
        stability_mode="off",
        logical_attempt_identity=first.logical_attempt_identity,
        quality_gate=_selective_replay_quality_gate(),
    )
    second_result = second.run()
    assert second_result.ok is True, second_result

    changed_roles = {
        role for role, transport in second_transports.items() if transport.invocations
    }
    assert changed_roles == {"evidence_critique", "arbitration"}, changed_roles
    assert all(first_transports[role].invocations for role in ROLE_MODEL)

    closure_record = second.registry.get("outline-v3:provider_receipt_closure")
    assert closure_record is not None
    closure_payload = json.loads(Path(closure_record.path).read_text(encoding="utf-8"))["payload"]
    _assert_fragment_bank_call_closure(
        closure_payload,
        observed_call_ids={"outline:arbitration", "outline:evidence_critique"},
    )
    current_receipts = list(second._receipt_ledger.list_receipts())
    assert len(current_receipts) == 2
    assert {receipt.call_id for receipt in current_receipts} == {
        "outline:arbitration",
        "outline:evidence_critique",
    }
    assert all(receipt.closure_epoch_id == second.closure_epoch_id for receipt in current_receipts)
    reuse_records = [
        record
        for record in second.registry.list_records()
        if record.artifact_type == "provider_verified_reuse"
    ]
    assert reuse_records
    assert all(record.status == "ready" for record in reuse_records)


def test_tampered_prior_receipt_ledger_blocks_verified_reuse(
    tmp_path: Path,
) -> None:
    """A valid-looking but Registry-tampered prior ledger is never trusted."""

    first_router, _first_transports = _build_router()
    first = _executor(
        tmp_path,
        router=first_router,
        poison=ExplodingProvider(),
        stability_mode="off",
        quality_gate=_selective_replay_quality_gate(),
    )
    first_result = first.run()
    assert first_result.ok is True, first_result
    ledger_record = first.registry.get("outline_v3_provider_receipts")
    assert ledger_record is not None
    ledger_path = Path(ledger_record.path)
    ledger_path.write_bytes(ledger_path.read_bytes() + b"\n")

    second_router, second_transports = _route_change_router(first_router)
    second = _executor(
        tmp_path,
        router=second_router,
        poison=ExplodingProvider(),
        stability_mode="off",
        logical_attempt_identity=first.logical_attempt_identity,
        quality_gate=_selective_replay_quality_gate(),
    )
    second_result = second.run()
    assert second_result.ok is True, second_result
    assert all(transport.invocations for transport in second_transports.values())
    closure_record = second.registry.get("outline-v3:provider_receipt_closure")
    assert closure_record is not None
    closure_payload = json.loads(Path(closure_record.path).read_text(encoding="utf-8"))["payload"]
    assert closure_payload["complete"] is True, closure_payload
    assert closure_payload["verified_reuse_call_ids"] == []
    assert not [
        record
        for record in second.registry.list_records()
        if record.artifact_type == "provider_verified_reuse"
    ]
    assert any(
        "replay receipt source rejected" in diagnostic
        for diagnostic in second.replay_diagnostics
    )


def test_all_prior_epoch_reuse_closes_without_current_provider_receipts(
    tmp_path: Path,
) -> None:
    """A fully replayed new epoch must still produce a complete closure."""

    first_router, first_transports = _build_router()
    first = _executor(
        tmp_path,
        router=first_router,
        poison=ExplodingProvider(),
        stability_mode="off",
        quality_gate=_selective_replay_quality_gate(),
    )
    assert first.run().ok is True

    second_router, second_transports = _build_router()
    second = _executor(
        tmp_path,
        router=second_router,
        poison=ExplodingProvider(),
        stability_mode="off",
        logical_attempt_identity=f"{first.logical_attempt_identity}:new-epoch",
        quality_gate=_selective_replay_quality_gate(),
    )
    result = second.run()
    assert result.ok is True, result
    assert all(first_transports[role].invocations for role in ROLE_MODEL)
    assert all(not transport.invocations for transport in second_transports.values())
    assert second._receipt_ledger.list_receipts() == ()

    closure_record = second.registry.get("outline-v3:provider_receipt_closure")
    assert closure_record is not None
    closure_payload = json.loads(Path(closure_record.path).read_text(encoding="utf-8"))["payload"]
    assert closure_payload["complete"] is True, closure_payload
    _assert_fragment_bank_call_closure(closure_payload, observed_call_ids=set())


@pytest.mark.parametrize(
    ("tamper_node_id", "expected_observed_call_ids", "expected_transport_invocations"),
    [
        pytest.param(
            "candidate_1_provider_generation",
            set(),
            [],
            id="local-aggregate-rebuilt-from-clean-physical-children",
        ),
        pytest.param(
            "candidate_organization_provider:candidate_1",
            {"outline:candidate_organization_provider:candidate_1"},
            ["candidate_organization_provider:candidate_1"],
            id="tampered-physical-provider-child-is-rerun-on-its-own-route",
        ),
    ],
)
def test_transitive_dependency_tamper_rejects_reuse_and_rebuilds_or_reruns_child(
    tmp_path: Path,
    tamper_node_id: str,
    expected_observed_call_ids: set[str],
    expected_transport_invocations: list[str],
) -> None:
    """Reject a deep dependency tamper without forcing an unrelated POST.

    The aggregate-control case is locally reconstructed from clean, verified
    fragment/organization outputs. The physical-output case must rerun exactly
    its affected organization provider call and keep the sibling/other calls
    covered by the current verified reuse closure.
    """

    first_router, _first_transports = _build_router()
    first = _executor(
        tmp_path,
        router=first_router,
        poison=ExplodingProvider(),
        stability_mode="off",
        quality_gate=_selective_replay_quality_gate(),
    )
    assert first.run().ok is True

    leaf_path = tmp_path / "tamper-leaf.json"
    middle_path = tmp_path / "tamper-middle.json"
    leaf_path.write_text(json.dumps({"ok": "leaf"}), encoding="utf-8")
    middle_path.write_text(json.dumps({"ok": "middle"}), encoding="utf-8")
    leaf = first.registry.register_file(
        artifact_id="tamper:leaf",
        artifact_role="tamper_fixture",
        artifact_type="tamper_fixture",
        artifact_version="v1",
        path=leaf_path,
        producer="tests",
    )
    middle = first.registry.register_file(
        artifact_id="tamper:middle",
        artifact_role="tamper_fixture",
        artifact_type="tamper_fixture",
        artifact_version="v1",
        path=middle_path,
        producer="tests",
        depends_on=[ArtifactDependencyRefV2.from_record(leaf)],
    )
    tampered_record = next(
        (
            record for record in first.registry.list_records()
            if str(record.metadata.get("node_id") or "") == tamper_node_id
        ),
        None,
    )
    assert tampered_record is not None and tampered_record.status == "ready"
    expected_artifact_type = (
        "outline_candidate"
        if tamper_node_id == "candidate_1_provider_generation"
        else "candidate_organization_map_result"
    )
    assert tampered_record.artifact_type == expected_artifact_type
    first.registry.update_record(
        tampered_record.artifact_id,
        depends_on=[*tampered_record.depends_on, ArtifactDependencyRefV2.from_record(middle)],
    )
    # Keep the selected output bytes unchanged; corrupt only its two-level
    # registered dependency chain so the next run must prove closure handling.
    leaf_path.write_bytes(leaf_path.read_bytes() + b"\n")
    invalid_record = first.registry.get(tampered_record.artifact_id)
    assert invalid_record is not None
    with pytest.raises(RegistryError):
        first.registry.verify_ready_artifact_closure(invalid_record)

    second_router, second_transports = _build_router()
    second = _executor(
        tmp_path,
        router=second_router,
        poison=ExplodingProvider(),
        stability_mode="off",
        logical_attempt_identity=f"{first.logical_attempt_identity}:transitive-tamper:{tamper_node_id}",
        quality_gate=_selective_replay_quality_gate(),
    )
    result = second.run()
    assert result.ok is True, result
    assert second_transports["candidate_provider_generation"].invocations == expected_transport_invocations
    assert all(
        not transport.invocations
        for role, transport in second_transports.items()
        if role != "candidate_provider_generation"
    )

    current_receipts = list(second._receipt_ledger.list_receipts())
    assert len(current_receipts) == len(expected_observed_call_ids)
    if expected_observed_call_ids:
        assert len(current_receipts) == 1
        expected_call_id = next(iter(expected_observed_call_ids))
        assert current_receipts[0].call_id == expected_call_id
        assert current_receipts[0].attempt_id.startswith(expected_call_id)
        provider, model, endpoint_type = ROLE_MODEL["candidate_provider_generation"]
        assert current_receipts[0].provider == provider
        assert current_receipts[0].model == model
        assert current_receipts[0].endpoint_type == endpoint_type
    assert all(receipt.closure_epoch_id == second.closure_epoch_id for receipt in current_receipts)

    closure_record = second.registry.get("outline-v3:provider_receipt_closure")
    assert closure_record is not None
    closure_payload = json.loads(Path(closure_record.path).read_text(encoding="utf-8"))["payload"]
    _assert_fragment_bank_call_closure(
        closure_payload,
        observed_call_ids=expected_observed_call_ids,
    )

    # Regardless of whether the aggregate itself or a physical child was
    # tampered, the adopted local aggregate must close only over clean current
    # fragment and organization artifacts after this run.
    rebuilt_candidate = second.registry.get("outline-v3:candidate_1_provider_generation")
    assert rebuilt_candidate is not None and rebuilt_candidate.status == "ready"
    second.registry.verify_ready_artifact_closure(rebuilt_candidate)
    assert all(
        dependency.artifact_id not in {"tamper:leaf", "tamper:middle"}
        for dependency in rebuilt_candidate.depends_on
    )
    organization_refs = [
        dependency for dependency in rebuilt_candidate.depends_on
        if dependency.artifact_id.startswith("outline-v3:organization-map:")
    ]
    assert len(organization_refs) == 1
    organization_record = second.registry.get(organization_refs[0].artifact_id)
    assert organization_record is not None and organization_record.status == "ready"
    assert organization_record.metadata.get("node_id") == "candidate_organization_provider:candidate_1"
    second.registry.verify_ready_artifact_closure(organization_record)
    assert all(
        dependency.artifact_id not in {"tamper:leaf", "tamper:middle"}
        for dependency in organization_record.depends_on
    )


def test_local_candidate_shard_replay_closes_projection_and_reuse_evidence(
    tmp_path: Path,
) -> None:
    """Exercise actual local shard replay and its sealed projection evidence."""

    case_path = tmp_path / "local-shard-projection"
    first_calls: list[str] = []
    first = _local_source_scope_executor(case_path, first_calls)
    first_result = first.run()
    _assert_local_projection_result_or_quality_fixture_block(first_result, first)
    assert any("candidate_1_provider_generation:local:" in node_id for node_id in first_calls)
    assert any("candidate_2_provider_generation:local:" in node_id for node_id in first_calls)

    second_calls: list[str] = []
    second = _local_source_scope_executor(
        case_path,
        second_calls,
        workspace=first.workspace,
        logical_attempt_identity=f"{first.logical_attempt_identity}:projection-replay",
        quality_gate=first.quality_gate,
    )
    second_result = second.run()
    _assert_local_projection_result_or_quality_fixture_block(second_result, second)
    assert second_calls == []

    closure_record = second.registry.get("outline-v3:provider_receipt_closure")
    assert closure_record is not None
    closure_payload = json.loads(Path(closure_record.path).read_text(encoding="utf-8"))["payload"]
    assert closure_payload["complete"] is True
    _assert_local_candidate_projection_closure(
        closure_payload,
        second.registry,
        closure_epoch_id=second.closure_epoch_id,
        source_closure_epoch_id=first.closure_epoch_id,
    )

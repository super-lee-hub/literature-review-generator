"""Route-derived Outline input and output windows stay separate from hard caps."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from outline.candidate_arbitration_aggregation import (
    ArbitrationSerializationCaps,
    _assert_serialization_fits,
)
from outline.candidate_arbitration_navigation import (
    NAV_KIND,
    SelectedNavigationError,
    _estimate_request,
)
from outline.provider_router import OutlineProviderRouter
from outline.v3_executor import OutlineV3ExecutionError
from runtime.provider_context import ProviderContextProfile
from services.settings import ApplicationSettings
from tests.test_outline_v3_multi_model_routing import _executor, _target_router


@pytest.mark.parametrize(
    ("source_cap", "expected_kind"),
    [
        (None, "route"),
        (0, "route"),
        (32_000, 32_000),
        (64_000, 64_000),
        (200_000, "route"),
    ],
)
def test_source_prompt_cap_uses_route_budget_or_explicit_positive_target(
    tmp_path: Path, source_cap: int | None, expected_kind: int | str
) -> None:
    executor = _executor(
        tmp_path,
        router=_target_router(),
        max_source_prompt_tokens=source_cap,
    )
    profile = executor._role_route("candidate_1_provider_generation").profile

    assert profile.input_budget > 32_000
    expected = (
        profile.input_budget
        if expected_kind == "route"
        else min(int(expected_kind), profile.input_budget)
    )
    assert executor._effective_input_cap(profile) == expected


def test_source_prompt_cap_is_bound_to_the_selected_role_route(tmp_path: Path) -> None:
    base = _target_router()
    routes = dict(base.routes)
    critic_profile = ProviderContextProfile.conservative(
        provider="openai_responses",
        model="gpt-5.6-sol-critic-window",
        endpoint_type="responses",
        model_context_limit=80_000,
        max_output_tokens=8_000,
    )
    routes["structure_critique"] = replace(
        routes["structure_critique"],
        profile=critic_profile,
    )
    router = OutlineProviderRouter(routes=routes, diagnostics=base.diagnostics)
    executor = _executor(
        tmp_path,
        router=router,
        max_source_prompt_tokens=200_000,
    )

    generation_profile = executor._role_route("candidate_1_provider_generation").profile
    routed_critic_profile = executor._role_route("structure_critique").profile
    assert generation_profile.input_budget > routed_critic_profile.input_budget
    assert executor._effective_input_cap(generation_profile) == generation_profile.input_budget
    assert executor._effective_input_cap(routed_critic_profile) == routed_critic_profile.input_budget


class _RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    def __call__(self, node_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append((node_id, dict(request)))
        return {
            "status": "success",
            "content": {"accepted": True},
            "finish_reason": "stop",
            "usage_status": "reported",
            "input_tokens": 120,
            "output_tokens": 12,
            "total_tokens": 132,
        }


def _large_request() -> dict[str, str]:
    # The repo's UTF-8 estimator places this above 32K while well below the
    # target route profile's input budget.
    return {"diagnostic_payload": "x" * 105_000}


def test_private_provider_call_admits_route_sized_input_and_writes_real_receipt(
    tmp_path: Path,
) -> None:
    transport = _RecordingTransport()
    executor = _executor(
        tmp_path,
        router=_target_router(transport=transport),
        max_source_prompt_tokens=0,
        stability_mode="off",
    )
    profile = executor._role_route("candidate_1_provider_generation").profile
    estimate = profile.estimate_tokens(_large_request())
    assert 32_000 < estimate < profile.input_budget

    result = executor._provider_call(
        "candidate_1_provider_generation",
        _large_request(),
        expect_json=True,
        output_tokens=1_024,
    )

    assert result["accepted"] is True
    assert len(transport.calls) == 1
    assert transport.calls[0][0] == "candidate_1_provider_generation"
    receipts = executor._receipt_ledger.list_receipts()
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.status == "success"
    assert receipt.model == "claude-opus-5"
    assert receipt.http_status is None  # Sentinel transport; no network request.
    assert executor._request_payload_audit[-1]["provider_invoked"] is True


def test_same_private_provider_call_is_blocked_by_explicit_32k_before_transport(
    tmp_path: Path,
) -> None:
    transport = _RecordingTransport()
    executor = _executor(
        tmp_path,
        router=_target_router(transport=transport),
        max_source_prompt_tokens=32_000,
        stability_mode="off",
    )
    profile = executor._role_route("candidate_1_provider_generation").profile
    assert profile.estimate_tokens(_large_request()) > 32_000

    with pytest.raises(OutlineV3ExecutionError, match="effective input cap"):
        executor._provider_call(
            "candidate_1_provider_generation",
            _large_request(),
            expect_json=True,
            output_tokens=1_024,
        )

    assert transport.calls == []
    assert not executor._receipt_ledger.list_receipts()
    assert executor._provider_call_count == 0


def _arbitration_caps(input_cap: int, *, verified_context_limit: int) -> ArbitrationSerializationCaps:
    caps = ArbitrationSerializationCaps(
        effective_input_cap=input_cap,
        verified_context_limit=verified_context_limit,
        route_max_output=16_000,
        reasoning_reserve=4_096,
        safety_margin=2_048,
        route_identity={
            "provider": "fixture",
            "model": "fixture-arbitration",
            "endpoint_type": "chat_completions",
            "prompt_sha256": "a" * 64,
            "policy_sha256": "b" * 64,
        },
        assessment_text_tokens=64,
        group_summary_text_tokens=64,
        selection_text_tokens=128,
        max_detail_required_ids=0,
    )
    caps.validate()
    return caps


@pytest.mark.parametrize("input_cap", [64_000, 128_000])
def test_arbitration_serialization_caps_accept_large_context_bound_windows(
    input_cap: int,
) -> None:
    output_cap = 16_000
    reasoning = 4_096
    safety = 2_048
    caps = _arbitration_caps(
        input_cap,
        verified_context_limit=input_cap + output_cap + reasoning + safety + 1_000,
    )

    _assert_serialization_fits(
        input_cap,
        output_cap,
        caps,
        "configured-window regression",
    )


def _navigation_context(
    *,
    input_cap: int,
    verified_context_limit: int,
    output_tokens: int = 16_000,
    reasoning_reserve: int = 4_096,
    safety_margin: int = 2_048,
) -> dict[str, dict[str, Any]]:
    route_identity = "fixture:navigation-route"
    return {
        NAV_KIND: {
            "effective_input_cap": input_cap,
            "verified_context_limit": verified_context_limit,
            "max_output_tokens": output_tokens,
            "reasoning_reserve": reasoning_reserve,
            "safety_margin": safety_margin,
            "route_identity": route_identity,
        }
    }


@pytest.mark.parametrize("input_cap", [64_000, 128_000])
def test_navigation_accepts_route_bound_64k_and_128k_windows(input_cap: int) -> None:
    context = _navigation_context(
        input_cap=input_cap,
        verified_context_limit=input_cap + 16_000 + 4_096 + 2_048 + 1_000,
    )
    route_identity = context[NAV_KIND]["route_identity"]
    estimate = input_cap - 1_000

    _body, measurement = _estimate_request(
        NAV_KIND,
        "navigation-window-test",
        {"decision_scope": "bounded"},
        prompt_authority=lambda _kind, node_id, _body: {"node_id": node_id},
        route_estimator=lambda _kind, _body: {
            "estimated_input_tokens": estimate,
            "route_identity": route_identity,
        },
        context_reserve=context,
        input_cap=input_cap,
    )

    assert measurement["estimated_input_tokens"] == estimate
    assert measurement["effective_input_cap"] == input_cap


def test_navigation_still_rejects_total_context_window_overrun() -> None:
    context = _navigation_context(
        input_cap=128_000,
        verified_context_limit=140_000,
    )
    route_identity = context[NAV_KIND]["route_identity"]

    with pytest.raises(SelectedNavigationError, match="exceeds verified context"):
        _estimate_request(
            NAV_KIND,
            "navigation-context-overrun",
            {"decision_scope": "bounded"},
            prompt_authority=lambda _kind, node_id, _body: {"node_id": node_id},
            route_estimator=lambda _kind, _body: {
                "estimated_input_tokens": 125_000,
                "route_identity": route_identity,
            },
            context_reserve=context,
            input_cap=128_000,
        )


def test_unset_or_zero_semantic_output_uses_route_output_target(tmp_path: Path) -> None:
    settings = ApplicationSettings.from_config({})
    default_value = settings.outline.semantic_output_max_tokens
    assert default_value in (None, 0)

    for output_target in (default_value, 0):
        executor = _executor(
            tmp_path / str(output_target),
            router=_target_router(),
            semantic_output_max_tokens=output_target,
        )
        profile = executor._role_route("candidate_1_provider_generation").profile
        assert executor._semantic_output_token_limit(profile) == profile.max_output_tokens


def test_explicit_semantic_output_target_is_preserved_and_invalid_values_rejected(
    tmp_path: Path,
) -> None:
    executor = _executor(
        tmp_path / "explicit",
        router=_target_router(),
        semantic_output_max_tokens=4_096,
    )
    profile = executor._role_route("candidate_1_provider_generation").profile
    assert executor._semantic_output_token_limit(profile) == 4_096

    for invalid in (True, False, -1):
        with pytest.raises(ValueError, match="semantic_output_max_tokens"):
            _executor(
                tmp_path / f"invalid-{invalid}",
                router=_target_router(),
                semantic_output_max_tokens=invalid,
            )

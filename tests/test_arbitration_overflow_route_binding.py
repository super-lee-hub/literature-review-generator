from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from runtime.arbitration_dispatch import validate_retained_arbitration_receipt
from runtime.provider_runtime import (
    ProviderBudgetV1,
    ProviderRuntime,
    ProviderRuntimeLedger,
)
from tests.test_outline_v3_multi_model_routing import _executor, _target_router

_DYNAMIC_ARBITRATION_NODES = (
    "arbitration-assessment:e025fec58cbcf0bd36f3d412",
    "arbitration-reducer:cae6789f6a4f5e64029086693f288aa2",
    "arbitration-selector-detail:3cc2f949360cd0fdd8317514",
    "arbitration_layout_extract:9ee51fc32a218592a5a44917",
)


def _receipt_for_binding(executor, node_id: str, tmp_path: Path):
    assert executor.router is not None
    route = replace(
        executor.router.route_for("arbitration"),
        api_base="http://127.0.0.1:5444/v1",
    )
    request = {
        "schema_version": "arbitration-route-binding-test/v1",
        "node_id": node_id,
        "source_scope": ["paper:a", "paper:b"],
    }
    binding = executor._provider_binding(
        node_id,
        request,
        expect_json=True,
        input_artifact_hashes=("source-authority-hash",),
        route=route,
    )
    call_id = executor._register_expected_from_binding(node_id, binding)
    expected = executor._expected_provider_calls[call_id]
    api_config = executor._route_transport_identity(route)
    prompt = json.dumps(request, sort_keys=True, ensure_ascii=False)
    content = {"accepted": True, "node_id": node_id}

    runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1, max_total_tokens=100),
        ledger=ProviderRuntimeLedger(tmp_path / "arbitration-route-binding-receipts.jsonl"),
        job_id=executor.job_id,
        attempt_id=call_id,
        stage_name="outline_v3",
        route=node_id,
        node_id=executor._semantic_receipt_node_id(node_id),
        call_id=call_id,
        closure_epoch_id=executor.closure_epoch_id,
        logical_attempt_identity=executor.logical_attempt_identity,
        endpoint_type=route.endpoint_type,
        schema_hash=binding["schema_hash"],
        prompt_id=executor._outline_prompt_identity.prompt_id,
        prompt_version=executor._outline_prompt_identity.version,
        prompt_sha256=executor._outline_prompt_identity.sha256,
    )
    receipt = runtime.complete(
        admission=runtime.admit(estimated_tokens=5, requested_output_tokens=20),
        prompt=prompt,
        input_payload=request,
        api_config=api_config,
        result={
            "status": "success",
            "content": content,
            "finish_reason": "stop",
            "input_tokens": 5,
            "output_tokens": 4,
            "total_tokens": 9,
        },
        metadata={
            "call_id": call_id,
            "config_section": route.config_section,
            "transport_config": api_config,
        },
    )
    return binding, expected, receipt, content, route


@pytest.mark.parametrize("node_id", _DYNAMIC_ARBITRATION_NODES)
def test_explicit_arbitration_route_binds_dynamic_receipt_identity(
    tmp_path: Path,
    node_id: str,
) -> None:
    executor = _executor(tmp_path, router=_target_router(), stability_mode="off")
    binding, expected, receipt, content, route = _receipt_for_binding(executor, node_id, tmp_path)

    assert binding["provider_route"] == route.config_section
    assert binding["provider_family"] == route.provider_name
    assert binding["model_name"] == route.model
    assert binding["endpoint_type"] == route.endpoint_type
    assert binding["api_base_host"] == route.api_base_host
    assert binding["route_fingerprint"] == route.safe_config_fingerprint()
    assert binding["prompt_id"] == receipt.prompt_id
    assert binding["prompt_version"] == receipt.prompt_version
    assert binding["prompt_sha256"] == receipt.prompt_sha256

    validate_retained_arbitration_receipt(
        expected,
        receipt,
        content,
        route=node_id,
        node_id=executor._semantic_receipt_node_id(node_id),
        config_section=route.config_section,
    )


def test_provider_execution_false_keeps_explicit_route_local(tmp_path: Path) -> None:
    executor = _executor(tmp_path, router=_target_router(), stability_mode="off")
    assert executor.router is not None
    route = executor.router.route_for("arbitration")

    binding = executor.build_current_node_binding(
        "arbitration-assessment:local-projection",
        model="deterministic-local-model",
        provider="local",
        route=route,
        provider_execution=False,
        dependency_hashes={"test": "dependency-hash"},
    )

    assert binding["provider_route"] == "local"
    assert binding["provider_family"] == "local"
    assert binding["model_name"] == "deterministic-local-model"
    assert binding["endpoint_type"] == "internal"
    assert binding["api_base_host"] == ""
    assert binding["route_fingerprint"] == ""
    assert binding["provider_config_hash"] == ""

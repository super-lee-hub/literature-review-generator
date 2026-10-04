from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_current_review_generation import _stage1_summary

from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderBudgetController,
    ProviderBudgetExceeded,
    canonical_provider_request_payload,
)
from services import review_generation_service
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.review_generation_service import ReviewGenerationService
from services.settings import ApplicationSettings
from tests.writer_source_fixture import bind_production_writer_sources, scoped_writer_content


def _writer_fixture(
    tmp_path: Path,
    *,
    writer: Any,
) -> tuple[ReviewGenerationService, dict[str, Any], list[dict[str, Any]]]:
    summary, _pdf_path = _stage1_summary(tmp_path)
    config = {
        "Writer_API": {
            "api_key": "writer",
            "model": "writer",
            "api_base": "https://writer.test/v1",
            "max_output_tokens": "256",
            "total_timeout_seconds": "30",
        },
        "Runtime": {"transport_retries": "2", "node_retry_limit": "1"},
    }
    workspace = JobWorkspace.create(
        str(tmp_path / "review-output"),
        "preflight",
        job_id="writer-preflight-job",
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    service = ReviewGenerationService(
        job_id=workspace.job_id,
        attempt_id="writer-preflight-attempt",
        workspace=workspace,
        artifact_registry=registry,
        settings=ApplicationSettings.from_config(config),
        summaries=[summary],
        writer=writer,
    )
    paper_key = str(summary["paper_info"]["canonical_paper_key"])
    sections = [
        {"section_id": f"section_{number}", "title": f"Results {number}", "goal": "Synthesize the evidence"}
        for number in (1, 2)
    ]
    packets = [
        {
            "section_id": section["section_id"],
            "section_goal": section["goal"],
            "planned_claims": ["The treatment improves the outcome."],
            "paper_keys": [paper_key],
            "source_summary_hashes": [f"summary-hash-{number}"],
            "retrieval_provenance": {"source": "stage1_summary", "section_id": section["section_id"]},
        }
        for number, section in enumerate(sections, start=1)
    ]
    bind_production_writer_sources(service, packets)
    return service, {"title": "Evidence-led review", "sections": sections}, packets


def _successful_writer_result(**kwargs: Any) -> Mapping[str, Any]:
    return {
        "status": "success",
        "content": scoped_writer_content(
            str(kwargs.get("prompt_text") or ""),
            "The controlled result supports the mechanism [[cite_ref:R001]].",
        ),
        "usage_status": "provider_not_supported",
    }


def _run(service: ReviewGenerationService, outline: Mapping[str, Any], packets: list[dict[str, Any]]) -> Any:
    return service.run(outline_payload=outline, evidence_packets=packets)


def test_writer_inventory_precedes_first_writer_call_and_matches_bound_request_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: dict[str, ReviewGenerationService] = {}
    observed: list[str] = []

    def stop_after_preflight(**_kwargs: Any) -> Mapping[str, Any]:
        service = holder["service"]
        inventory = service.provider_request_inventory
        assert inventory is not None
        observed.append("writer")
        raise RuntimeError("stop after inventory preflight")

    service, outline, packets = _writer_fixture(tmp_path, writer=stop_after_preflight)
    holder["service"] = service
    controller = ProviderBudgetController(
        ProviderAggregateBudgetV2(
            max_provider_calls_total=4,
            max_output_tokens_total=1_024,
            max_retry_attempts_total=2,
            max_wall_seconds=600.0,
        )
    )
    monkeypatch.setattr(
        review_generation_service,
        "provider_budget_controller_from_environment",
        lambda: controller,
    )
    before = controller.snapshot()

    with pytest.raises(RuntimeError, match="stop after inventory preflight"):
        _run(service, outline, packets)

    inventory = service.provider_request_inventory
    assert inventory is not None
    assert inventory.stage_name == "review"
    assert len(inventory.requests) == 2
    assert inventory.unknown_exposures == ()
    assert observed == ["writer"]
    expected_by_request_key = {
        review_generation_service.hash_json(
            {"stage": "review", "request_id": item.call_id}
        ): item
        for item in service._expected_provider_calls.values()
    }
    for row in inventory.requests:
        expected = expected_by_request_key[row.request_key_hash]
        assert row.request_estimate.request_hash == expected.input_hash
        assert row.request_key_hash == review_generation_service.hash_json(
            {"stage": "review", "request_id": expected.call_id}
        )
        assert row.physical_attempt_upper_bound == 2
    after = controller.snapshot()
    for field in (
        "calls_used",
        "output_tokens_used",
        "retry_attempts_used",
        "calls_reserved",
        "output_tokens_reserved",
        "retry_attempts_reserved",
    ):
        assert after[field] == before[field]


@pytest.mark.parametrize(
    ("dimension", "limit", "prior_output", "prior_retries"),
    [
        ("calls", 4, 0, 0),
        ("output", 1_024, 513, 0),
    ],
)
def test_strict_v2_preflight_blocks_insufficient_remaining_budget_before_writer_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dimension: str,
    limit: int,
    prior_output: int,
    prior_retries: int,
) -> None:
    writer_calls: list[str] = []

    def writer(**kwargs: Any) -> Mapping[str, Any]:
        writer_calls.append(str(kwargs["section"]["section_id"]))
        return _successful_writer_result(**kwargs)

    service, outline, packets = _writer_fixture(tmp_path, writer=writer)
    budget = ProviderAggregateBudgetV2(
        max_provider_calls_total=limit if dimension == "calls" else 100,
        max_output_tokens_total=limit if dimension == "output" else 100_000,
        max_retry_attempts_total=limit if dimension == "retries" else 100,
        max_wall_seconds=600.0,
    )
    controller = ProviderBudgetController(budget)
    controller.admit(
        requested_output_tokens=prior_output,
        requested_retry_attempts=prior_retries,
    )
    monkeypatch.setattr(
        review_generation_service,
        "provider_budget_controller_from_environment",
        lambda: controller,
    )
    before = controller.snapshot()

    with pytest.raises(ProviderBudgetExceeded, match="Writer stage preflight"):
        _run(service, outline, packets)

    assert writer_calls == []
    after = controller.snapshot()
    for field in (
        "calls_used",
        "output_tokens_used",
        "retry_attempts_used",
        "calls_reserved",
        "output_tokens_reserved",
        "retry_attempts_reserved",
    ):
        assert after[field] == before[field]


@pytest.mark.parametrize("remaining_retries", [0, 1, 6])
def test_writer_optional_retries_share_remaining_run_allowance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remaining_retries: int,
) -> None:
    writer_calls: list[str] = []
    projections: list[dict[str, Any]] = []

    def writer(**kwargs: Any) -> Mapping[str, Any]:
        writer_calls.append(str(kwargs["section"]["section_id"]))
        return {**_successful_writer_result(**kwargs), "attempts": 1}

    service, outline, packets = _writer_fixture(tmp_path, writer=writer)
    controller = ProviderBudgetController(ProviderAggregateBudgetV2(
        max_provider_calls_total=100,
        max_output_tokens_total=100_000,
        max_retry_attempts_total=remaining_retries + 1,
        max_wall_seconds=600.0,
    ))
    controller.admit(requested_output_tokens=0, requested_retry_attempts=1)
    monkeypatch.setattr(
        review_generation_service, "provider_budget_controller_from_environment",
        lambda: controller,
    )
    original_projection = review_generation_service.build_full_stage_request_plan_v1

    def capture_projection(**kwargs: Any) -> dict[str, Any]:
        projection = original_projection(**kwargs)
        projections.append(projection)
        return projection

    monkeypatch.setattr(
        review_generation_service, "build_full_stage_request_plan_v1", capture_projection
    )
    _run(service, outline, packets)

    assert writer_calls == ["section_1", "section_2"]
    assert service.provider_request_inventory is not None
    rows = service.provider_request_inventory.requests
    assert all(row.retry_policy == "shared_optional" for row in rows)
    assert len(projections) == 1
    totals = projections[0]["totals"]
    assert totals["retry_attempts_required_reserve"] == 0
    assert totals["retry_attempts_possible_upper_bound"] == min(
        remaining_retries, sum(int(row.retry_attempts or 0) for row in rows)
    )
    assert projections[0]["budget_status"]["provider_retries"] == "within_limit"


@pytest.mark.parametrize("shared_retries", [0, 1])
def test_formal_writer_adapter_clamps_transport_to_shared_retry_allowance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shared_retries: int,
) -> None:
    import ai_interface
    import runtime.provider_runtime as runtime_module

    service, outline, packets = _writer_fixture(tmp_path, writer=None)
    controller = ProviderBudgetController(ProviderAggregateBudgetV2(
        max_provider_calls_total=10,
        max_output_tokens_total=100_000,
        max_retry_attempts_total=shared_retries,
        max_wall_seconds=600.0,
    ))
    monkeypatch.setattr(
        runtime_module, "provider_budget_controller_from_environment", lambda: controller
    )
    monkeypatch.setattr(
        review_generation_service, "provider_budget_controller_from_environment",
        lambda: controller,
    )
    calls: list[dict[str, int]] = []

    def local_transport(*_args: Any, **kwargs: Any) -> Mapping[str, Any]:
        calls.append({
            key: int(kwargs[key])
            for key in ("retry_attempts", "max_retries_per_call", "attempt_limit")
        })
        return {
            **_successful_writer_result(prompt_text=_args[0]),
            "attempts": 1,
            "usage_status": "reported",
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "finish_reason": "stop",
        }

    monkeypatch.setattr(ai_interface, "_call_ai_api_detailed_uninstrumented", local_transport)
    _run(service, outline, packets)

    assert len(calls) == 2
    assert all(call["retry_attempts"] == min(2, shared_retries + 1) for call in calls)
    assert all(call["attempt_limit"] == call["retry_attempts"] for call in calls)
    assert all(call["max_retries_per_call"] == call["retry_attempts"] - 1 for call in calls)
    snapshot = controller.snapshot()
    assert snapshot["calls_used"] == 2
    assert snapshot["retry_attempts_used"] == 0
    assert snapshot["calls_reserved"] == snapshot["retry_attempts_reserved"] == 0


@pytest.mark.parametrize("route_retries", [None, 0, "0"])
@pytest.mark.parametrize(
    "reserve_config,expected_reserves",
    [({}, (0, 256)), ({"reasoning_reserve_tokens": "4096", "safety_margin_tokens": "2048"}, (4096, 2048))],
    ids=["omitted-reserves", "configured-reserves"],
)
def test_writer_transport_uses_prepared_prompt_config_and_output_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route_retries: int | str | None,
    reserve_config: Mapping[str, Any],
    expected_reserves: tuple[int, int],
) -> None:
    system_prompt = {"value": "prepared Writer system prompt"}
    holder: dict[str, ReviewGenerationService] = {}
    observed_calls: list[dict[str, Any]] = []

    def fake_call(
        prompt: str,
        api_config: Mapping[str, Any],
        prepared_system_prompt: str,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        request_payload = canonical_provider_request_payload(
            prompt=prompt,
            system_prompt=prepared_system_prompt,
            user_content=None,
            response_format="json",
            max_output_tokens=int(kwargs["max_tokens"]),
            temperature=0.2,
        )
        inventory = holder["service"].provider_request_inventory
        assert inventory is not None
        row = next(
            row
            for row in inventory.requests
            if row.request_estimate.request_hash
            == review_generation_service.hash_json(request_payload)
        )
        assert review_generation_service.hash_json(request_payload) == row.request_estimate.request_hash
        assert prepared_system_prompt == "prepared Writer system prompt"
        assert api_config["model"] == "writer"
        assert kwargs["max_tokens"] == 256
        expected_limit = 2 if route_retries is None else 0
        assert kwargs["retry_attempts"] == expected_limit
        assert row.retry_attempts == max(0, expected_limit - 1)
        assert row.request_estimate.reasoning_reserve_tokens == expected_reserves[0]
        assert row.request_estimate.safety_margin_tokens == expected_reserves[1]
        observed_calls.append(
            {
                "prompt": prompt,
                "system_prompt": prepared_system_prompt,
                "api_config": dict(api_config),
                "request_payload": request_payload,
                "max_tokens": kwargs["max_tokens"],
            }
        )
        return {
            "status": "success",
            "content": scoped_writer_content(prompt, "The controlled result supports the mechanism [[cite_ref:R001]]."),
            "usage_status": "provider_not_supported",
            "attempts": 1,
        }

    import ai_interface

    monkeypatch.setattr(ai_interface, "_call_ai_api_detailed", fake_call)
    service, outline, packets = _writer_fixture(tmp_path, writer=None)
    sections = {name: dict(values) for name, values in service.settings.sections.items()}
    sections["Writer_API"].update(reserve_config)
    if route_retries is not None:
        sections["Writer_API"]["transport_retries"] = route_retries
    service.settings = replace(service.settings, sections=sections)
    holder["service"] = service
    monkeypatch.setattr(service, "_system_prompt", lambda: system_prompt["value"])
    original_preflight = service._preflight_provider_request_inventory

    def mutate_after_preflight(inventory: Any, route_plan: Any) -> None:
        original_preflight(inventory, route_plan)
        system_prompt["value"] = "changed prompt registry contents"
        sections = {
            name: dict(values)
            for name, values in service.settings.sections.items()
        }
        sections["Writer_API"]["model"] = "mutated-model"
        service.settings = replace(service.settings, sections=sections)

    monkeypatch.setattr(
        service,
        "_preflight_provider_request_inventory",
        mutate_after_preflight,
    )
    _run(service, outline, packets)

    assert len(observed_calls) == 2


def test_verified_replay_is_inventory_zero_call_and_writer_only_runs_for_missing_section(
    tmp_path: Path,
) -> None:
    first_calls: list[int] = []

    def crash_on_second(**kwargs: Any) -> Mapping[str, Any]:
        number = int(kwargs["section_number"])
        first_calls.append(number)
        if number == 2:
            raise RuntimeError("simulated second-section crash")
        return _successful_writer_result(**kwargs)

    service, outline, packets = _writer_fixture(tmp_path, writer=crash_on_second)
    with pytest.raises(RuntimeError, match="simulated second-section crash"):
        _run(service, outline, packets)
    assert first_calls == [1, 2]
    first_epoch = service.closure_epoch_id

    resumed_calls: list[int] = []

    def resume_writer(**kwargs: Any) -> Mapping[str, Any]:
        resumed_calls.append(int(kwargs["section_number"]))
        return _successful_writer_result(**kwargs)

    service.writer = resume_writer
    resumed_packets = [dict(packet) for packet in packets]
    revised_claim = "The revised second-section claim changes only its request."
    resumed_packets[1]["planned_claims"] = [revised_claim]
    claim_support = resumed_packets[1].get("claim_support")
    assert isinstance(claim_support, list) and len(claim_support) == 1
    assert isinstance(claim_support[0], dict)
    claim_support[0]["claim"] = revised_claim
    _run(service, outline, resumed_packets)
    assert service.closure_epoch_id != first_epoch

    inventory = service.provider_request_inventory
    assert inventory is not None
    row_by_call = {
        call_id: row
        for call_id, row in zip(
            ("review:section_1", "review:section_2"),
            inventory.requests,
            strict=True,
        )
    }
    assert row_by_call["review:section_1"].verified_reuse
    assert row_by_call["review:section_1"].physical_attempt_upper_bound == 0
    assert not row_by_call["review:section_2"].verified_reuse
    assert resumed_calls == [2]
    reused_call = service._expected_provider_calls["review:section_1"]
    assert reused_call.verified_reuse
    assert reused_call.reuse_evidence_artifact_id == "review_replay"
    assert reused_call.reuse_evidence_artifact_hash
    assert reused_call.reuse_evidence_record_hash
    replay_record = service.registry.get("review_replay")
    assert replay_record is not None
    assert reused_call.reuse_evidence_artifact_hash == replay_record.content_hash
    closure_record = service.registry.get("review:provider_receipt_closure")
    assert closure_record is not None
    closure_document = json.loads(Path(closure_record.path).read_text(encoding="utf-8"))
    closure_payload = closure_document["payload"]
    assert closure_payload["complete"] is True
    assert "review:section_1" in closure_payload["verified_reuse_call_ids"]

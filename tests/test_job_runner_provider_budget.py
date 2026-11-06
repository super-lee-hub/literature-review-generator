from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.provider_runtime import ProviderAggregateBudgetV2, hash_json
from services.job_runner import (
    JobRunner,
    JobRunRequest,
    build_job_request_from_mapping,
)
from services.queue_service import PersistentQueueService, QueueJobSpec

_VALID_BUDGET = {
    "schema_version": "provider-aggregate-budget-contract-v2",
    "max_provider_calls_total": 9,
    "max_output_tokens_total": 120_000,
    "max_retry_attempts_total": 0,
    "max_wall_seconds": 900,
}
_ZERO_BUDGET = {
    "schema_version": "provider-aggregate-budget-contract-v2",
    "max_provider_calls_total": 0,
    "max_output_tokens_total": 0,
    "max_retry_attempts_total": 0,
    "max_wall_seconds": 0,
}


def _legacy_request(*, provider_budget: Any = None) -> JobRunRequest:
    payload: dict[str, Any] = {
        "config": r"D:\budget-compat\config.ini",
        "project_name": "budget-compat",
        "pdf_folder": r"D:\budget-compat\papers",
        "run_all": True,
        "job_id": "budget-compat",
        "reuse_stage1": False,
    }
    if provider_budget is not None:
        payload["provider_budget"] = provider_budget
    return build_job_request_from_mapping(payload)


def _queued_runtime_spec(tmp_path: Path, budget: Any) -> RuntimeJobSpec:
    output_root = tmp_path / "output"
    config = tmp_path / "config.ini"
    pdfs = tmp_path / "papers"
    config.write_text("[Paths]\noutput_path = ./output\n", encoding="utf-8")
    pdfs.mkdir()
    spec = RuntimeJobSpec(
        project_name="queued-budget",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdfs)),
        job_id="queued-budget-01",
        config=str(config),
        action="run_all",
        workspace_path=str(output_root / "queued-budget__queued-budget-01"),
        metadata={"provider_budget": dict(budget) if isinstance(budget, Mapping) else budget},
    )
    spec.validate()
    queue = PersistentQueueService(tmp_path / "queue.json")
    job = QueueJobSpec(
        job_id="queued-budget-01",
        job_type="run_all",
        project_name="queued-budget",
        parameters=spec.to_dict(),
        workspace_path=spec.workspace_path,
        canonical_output_root=str(output_root),
    )
    return queue._strict_runtime_spec(job)


def test_default_legacy_request_preserves_pre_v2_spec_and_request_hashes() -> None:
    request = _legacy_request()
    runtime_spec = JobRunner()._runtime_spec(request)

    assert request.provider_budget is None
    assert "provider_budget" not in runtime_spec.metadata
    legacy_spec_payload = runtime_spec.to_dict()
    legacy_spec_payload["config"] = "<config>"
    legacy_spec_payload["queue_file"] = "<queue>"
    legacy_spec_payload["source"]["pdf_folder"] = "<pdf-folder>"
    assert hash_json(legacy_spec_payload) == (
        "edc820d1136fea72bdc084dcb76a4f8337f0705067bfb5f9e0c977cf23ae8138"
    ), legacy_spec_payload

    legacy_request_snapshot = JobRunner._request_snapshot(request)
    legacy_request_snapshot["config"] = "<config>"
    legacy_request_snapshot["queue_file"] = "<queue>"
    legacy_request_snapshot["pdf_folder"] = "<pdf-folder>"
    assert hash_json(legacy_request_snapshot) == (
        "bd3502245cb39230ed2dbf83e080c0538b390864696dbff2243e3afcc0c2777a"
    ), legacy_request_snapshot


def test_legacy_mapping_forwards_strict_v2_budget_to_runtime_spec() -> None:
    request = _legacy_request(provider_budget=_VALID_BUDGET)
    expected = ProviderAggregateBudgetV2.from_mapping(_VALID_BUDGET).to_dict()

    assert request.provider_budget == expected
    runtime_spec = JobRunner()._runtime_spec(request)
    assert runtime_spec.metadata["provider_budget"] == expected


def test_explicit_zero_budget_is_forwarded_without_truthiness_fallback() -> None:
    request = _legacy_request(provider_budget=_ZERO_BUDGET)

    runtime_spec = JobRunner()._runtime_spec(request)

    assert runtime_spec.metadata["provider_budget"] == _ZERO_BUDGET


@pytest.mark.parametrize(
    "budget",
    [
        {**_VALID_BUDGET, "unrecognized_limit": 1},
        {**_VALID_BUDGET, "schema_version": "provider-aggregate-budget-contract-v1"},
        {key: value for key, value in _VALID_BUDGET.items() if key != "max_retry_attempts_total"},
        {**_VALID_BUDGET, "max_provider_calls_total": -1},
        {**_VALID_BUDGET, "max_provider_calls_total": True},
        "not-an-object",
    ],
)
def test_invalid_budget_is_rejected_before_job_runner_can_post(budget: Any, monkeypatch) -> None:
    with pytest.raises((TypeError, ValueError)):
        _legacy_request(provider_budget=budget)

    request = _legacy_request(provider_budget=_VALID_BUDGET)
    assert request.provider_budget is not None
    # The frozen request owns a normalized copy, but callers may still mutate
    # the mapping value. Revalidate at JobRunner's boundary before runtime I/O.
    request.provider_budget["unrecognized_limit"] = 1  # type: ignore[index]

    def unexpected_runtime_call(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("invalid provider budget reached AgentRuntimeRunner")

    from runtime.runner import AgentRuntimeRunner

    monkeypatch.setattr(AgentRuntimeRunner, "run", unexpected_runtime_call)
    with pytest.raises(ValueError, match="unknown fields"):
        JobRunner().run(request)


def test_legacy_queue_round_trips_budget_through_request_and_runtime_adapter(
    tmp_path: Path,
) -> None:
    queued_spec = _queued_runtime_spec(tmp_path, _VALID_BUDGET)

    request = queued_spec.to_job_request()
    runtime_spec = JobRunner()._runtime_spec(request)

    expected = ProviderAggregateBudgetV2.from_mapping(_VALID_BUDGET).to_dict()
    assert request.provider_budget == expected
    assert runtime_spec.metadata["provider_budget"] == expected


def test_legacy_queue_null_budget_remains_omitted_from_runtime_spec(tmp_path: Path) -> None:
    queued_spec = _queued_runtime_spec(tmp_path, None)

    request = queued_spec.to_job_request()
    runtime_spec = JobRunner()._runtime_spec(request)

    assert request.provider_budget is None
    assert "provider_budget" not in runtime_spec.metadata


def test_legacy_queue_rejects_invalid_budget_before_job_runner(tmp_path: Path) -> None:
    invalid = {**_VALID_BUDGET, "unrecognized_limit": 1}

    with pytest.raises(ValueError, match="unknown fields"):
        queued_spec = _queued_runtime_spec(tmp_path, invalid)
        queued_spec.to_job_request()

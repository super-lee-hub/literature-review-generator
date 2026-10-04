from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests

import ai_interface
from runtime import provider_runtime
from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderBudgetController,
    ProviderBudgetExceeded,
    hash_json,
)
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.settings import ApplicationSettings
from validation import current_validation
from validation.adjudication_reuse import _request_payload
from validation.execution_service import ValidationExecutionService


def _service(
    tmp_path: Path, *, validation_retry_limit: int = 1
) -> ValidationExecutionService:
    workspace = JobWorkspace.create(
        str(tmp_path / "output"), "validation", job_id="validator-preflight"
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    api_config = {
        "api_key": "local-fixture-only",
        "provider_family": "generic",
        "model": "validator-fixture-model",
        "api_base": "http://127.0.0.1:1/v1",
        "endpoint_type": "chat_completions",
        "max_context_tokens": "32768",
        "max_output_tokens": "1024",
        "reasoning_reserve_tokens": "128",
        "safety_margin_tokens": "256",
        "transport_retries": "3",
        "connect_timeout_seconds": "2",
        "read_timeout_seconds": "7",
        "total_timeout_seconds": "9",
        "first_token_timeout_seconds": "1",
        "proxy_mode": "environment",
    }
    settings = ApplicationSettings.from_config(
        {
            "Validator_API": api_config,
            "Runtime": {
                "max_workers": "1",
                "validation_retry_limit": str(validation_retry_limit),
            },
        }
    )
    return ValidationExecutionService(
        job_id=workspace.job_id,
        attempt_id="attempt-1",
        workspace=workspace,
        artifact_registry=registry,
        settings=settings,
        summaries=[],
        review_draft_record=None,
        citation_manifest_record=None,
        paper_artifact_records=[],
        visual_artifact_records=[],
        provider_factory=None,
        cancellation_checker=None,
        logger=None,
        runtime_config={"Validator_API": api_config},
    )


def _citation_result(citation_set_key: str, paper_id: str, claim_text: str) -> Any:
    claim_unit_id = f"{citation_set_key}:unit-1"
    claim_unit = {
        "claim_unit_id": claim_unit_id,
        "claim_text": claim_text,
        "paper_ids": [paper_id],
    }
    return SimpleNamespace(
        citation_id=citation_set_key,
        citation_set_key=citation_set_key,
        paper_id=paper_id,
        paper_ids=[paper_id],
        claim_text=claim_text,
        claim_context=f"Context for {citation_set_key}.",
        block_context=f"Block for {citation_set_key}.",
        claim_type="result",
        claim_type_confidence=0.9,
        claim_type_rationale="fixture result claim",
        claim_units=[claim_unit],
        target_claim_unit=claim_unit,
        details={
            "claim_type": "result",
            "claim_type_confidence": 0.9,
            "claim_type_rationale": "fixture result claim",
            "checked_paper_ids": [paper_id],
            "claim_unit_results": [dict(claim_unit)],
            "paper_identity_hints": {paper_id: {"title": f"Title {paper_id}"}},
            "per_paper_evidence_packets": {
                paper_id: {
                    claim_unit_id: [
                        {
                            "resolver_tier": "normalized_text",
                            "text_excerpt": f"Evidence for {citation_set_key}.",
                        }
                    ]
                }
            },
            "evidence_status": "evidence_gap",
            "disposition": "manual_review",
        },
        evidence_excerpt_list=[f"Evidence for {citation_set_key}."],
        evidence_status="evidence_gap",
        disposition="manual_review",
        low_confidence=True,
    )


def _bind_fixture_reuse_miss(service: ValidationExecutionService, monkeypatch) -> None:
    monkeypatch.setattr(
        service,
        "find_verified_adjudication_reuse",
        lambda **_kwargs: (None, None, ""),
    )
    monkeypatch.setattr(current_validation, "_apply_adjudication", lambda item, _report: item)


def _bind_aggregate_budget(
    monkeypatch: pytest.MonkeyPatch,
    *,
    calls: int,
    output_tokens: int,
    retries: int,
    wall_seconds: float = 120.0,
) -> ProviderBudgetController:
    controller = ProviderBudgetController(
        ProviderAggregateBudgetV2(
            max_provider_calls_total=calls,
            max_output_tokens_total=output_tokens,
            max_retry_attempts_total=retries,
            max_wall_seconds=wall_seconds,
        )
    )
    monkeypatch.setattr(
        provider_runtime,
        "provider_budget_controller_from_environment",
        lambda: controller,
    )
    return controller


def test_validator_pretransport_inventory_binds_only_eligible_requests_before_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    _bind_fixture_reuse_miss(service, monkeypatch)
    eligible = [
        _citation_result("set-a", "paper-a", "Sensitive claim A."),
        _citation_result("set-b", "paper-b", "Sensitive claim B."),
    ]
    skipped_empty_claim = _citation_result("set-empty", "paper-c", "")
    skipped_no_papers = _citation_result("set-no-paper", "paper-d", "Claim without a paper.")
    skipped_no_papers.paper_ids = []
    seen_packets: list[Any] = []

    def fake_run_adjudication_stage(
        observed_service: Any, api_config: Mapping[str, Any], packet: Any
    ) -> Mapping[str, Any]:
        inventory = observed_service._validator_stage_pretransport_inventory
        record = observed_service.artifact_registry.get(
            f"validator-pretransport:{inventory['inventory_hash']}"
        )
        assert record is not None
        observed_service.artifact_registry.verify_ready_artifact_closure(record)
        persisted = json.loads(Path(record.path).read_text(encoding="utf-8"))
        assert persisted["inventory_hash"] == inventory["inventory_hash"]
        assert persisted["request_plan_hash"] == inventory["request_plan_hash"]
        assert inventory["provider_posts_emitted_at_plan"] == 0
        assert inventory["status"] == "materialized_upper_bound"
        row = next(
            item for item in inventory["requests"]
            if item["call_id"] == current_validation.adjudication_call_id(packet)
        )
        assert row["request_hash"] == hash_json(_request_payload(packet, api_config))
        seen_packets.append(packet)
        return {"status": "supported", "confidence": 0.99}

    monkeypatch.setattr(
        current_validation,
        "run_adjudication_stage",
        fake_run_adjudication_stage,
    )

    output = current_validation._adjudicate(
        service,
        [*eligible, skipped_empty_claim, skipped_no_papers],
        scope="repair_revalidation",
    )

    inventory = service._validator_stage_pretransport_inventory
    assert output == [*eligible, skipped_empty_claim, skipped_no_papers]
    assert len(seen_packets) == 2
    assert inventory["scope"] == "repair_revalidation"
    assert inventory["request_count"] == 2
    assert inventory["logical_call_upper_bound"] == 2
    assert inventory["physical_attempts_upper_bound"] == 4
    assert inventory["attempt_contract_mismatch_count"] == 0
    assert inventory["profile_error_count"] == 0
    assert inventory["estimated_output_tokens_all_attempts_upper_bound"] == 4_096
    assert inventory["request_timeout_seconds"] == 7
    assert inventory["requests"][0]["connect_timeout_seconds"] == 2
    assert inventory["requests"][0]["read_timeout_seconds"] == 7
    assert inventory["requests"][0]["total_timeout_seconds"] == 9
    assert inventory["first_token_timeout_enforced"] is False
    assert inventory["verified_reuse_count"] == 0
    assert inventory["transport_call_candidate_count"] == 2
    assert [item["citation_set_key"] for item in inventory["requests"]] == [
        "set-a",
        "set-b",
    ]
    assert [item["paper_ids"] for item in inventory["requests"]] == [
        ["paper-a"],
        ["paper-b"],
    ]
    encoded = json.dumps(inventory, ensure_ascii=False)
    assert "Sensitive claim A." not in encoded
    assert "Sensitive claim B." not in encoded
    assert "local-fixture-only" not in encoded


def test_validator_inventory_binds_explicit_repair_records_before_dispatch(tmp_path, monkeypatch):
    from services.job_workspace import publish_json_artifact

    service = _service(tmp_path)
    _bind_fixture_reuse_miss(service, monkeypatch)
    records = [publish_json_artifact(
        service.publication_context, service.artifact_registry,
        service.workspace.artifact_path(f"input/{kind}.json"), {"revision": "repaired"},
        artifact_id=f"fixture-repaired:{kind}", artifact_role="fixture", artifact_type="fixture",
        artifact_version="v1", producer="test",
    ) for kind in ("draft", "manifest")]

    def provider(observed_service, api_config, packet):
        inventory = observed_service._validator_stage_pretransport_inventory
        assert {item["artifact_id"] for item in inventory["input_artifacts"]} == {
            record.artifact_id for record in records
        }
        saved = observed_service._validator_stage_pretransport_inventory_record
        observed_service.artifact_registry.verify_ready_artifact_closure(saved)
        return {"status": "supported", "confidence": 0.99}

    monkeypatch.setattr(current_validation, "run_adjudication_stage", provider)
    current_validation._adjudicate(service, [_citation_result("set", "paper", "A bounded claim.")],
                                  scope="repair_revalidation", input_records=records)


@pytest.mark.parametrize("failure", [None, "tampered", "foreign_transaction"])
def test_provisional_repair_inventory_preserves_quarantine_and_rejects_drift(tmp_path, monkeypatch, failure):
    from services.job_workspace import publish_json_artifact

    service = _service(tmp_path)
    _bind_fixture_reuse_miss(service, monkeypatch)
    path = service.workspace.artifact_path("repair/draft.json")
    source = publish_json_artifact(
        service.publication_context, service.artifact_registry, path, {"repair": "candidate"},
        artifact_id="repair-derived-draft", artifact_role="repair", artifact_type="fixture",
        artifact_version="v1", producer="test", status="quarantined",
    )
    candidate = service.artifact_registry.register_file(
        path=source.path, artifact_id="repair-validation-draft", artifact_role="repair_validation_candidate_review_draft",
        artifact_type="review_draft", artifact_version="v3", producer="test", status="quarantined",
        metadata={"repair_validation_candidate": True, "source_artifact_id": source.artifact_id},
    )
    transaction = publish_json_artifact(
        service.publication_context, service.artifact_registry,
        service.workspace.artifact_path("repair/transaction.json"),
        {"status": "quarantined", "job_id": service.job_id,
         "applied_artifact_ids": [source.artifact_id] if failure != "foreign_transaction" else []},
        artifact_id="repair-transaction", artifact_role="repair", artifact_type="repair_transaction",
        artifact_version="v1", producer="test", status="quarantined",
    )
    sends = []

    def provider(observed_service, api_config, packet):
        sends.append(packet)
        assert observed_service._validator_stage_pretransport_inventory_record.status == "quarantined"
        assert all(item["status"] == "quarantined" for item in observed_service._validator_stage_pretransport_inventory["input_artifacts"])
        return {"status": "supported", "confidence": 0.99}

    monkeypatch.setattr(current_validation, "run_adjudication_stage", provider)
    if failure == "tampered":
        Path(candidate.path).write_text('{"tampered":true}', encoding="utf-8")
    if failure:
        with pytest.raises(RuntimeError, match="binding changed|outside its repair transaction"):
            current_validation._adjudicate(service, [_citation_result("set", "paper", "A bounded claim.")],
                                          scope="repair_revalidation", input_records=[candidate], repair_transaction_record=transaction)
        assert sends == []
    else:
        current_validation._adjudicate(service, [_citation_result("set", "paper", "A bounded claim.")],
                                      scope="repair_revalidation", input_records=[candidate], repair_transaction_record=transaction)
        assert len(sends) == 1


def test_validator_scope_budget_blocks_before_first_transport_when_one_call_is_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, validation_retry_limit=0)
    _bind_fixture_reuse_miss(service, monkeypatch)
    controller = _bind_aggregate_budget(
        monkeypatch, calls=1, output_tokens=2_048, retries=0
    )
    eligible = [
        _citation_result("set-a", "paper-a", "Claim A."),
        _citation_result("set-b", "paper-b", "Claim B."),
    ]
    dispatched: list[str] = []

    def fake_run_adjudication_stage(
        _service: Any, _api_config: Mapping[str, Any], packet: Any
    ) -> Mapping[str, Any] | None:
        try:
            reservation = controller.admit(
                requested_output_tokens=1_024,
                requested_retry_attempts=0,
                context={"call_id": current_validation.adjudication_call_id(packet)},
            )
        except ProviderBudgetExceeded:
            return None
        dispatched.append(current_validation.adjudication_call_id(packet))
        controller.complete(
            reservation,
            {"status": "success", "attempts": 1, "output_tokens": 1_024},
        )
        return {"status": "supported", "confidence": 0.99}

    monkeypatch.setattr(
        current_validation,
        "run_adjudication_stage",
        fake_run_adjudication_stage,
    )

    with pytest.raises(ProviderBudgetExceeded, match="Validator stage preflight"):
        current_validation._adjudicate(service, eligible, scope="primary_validation")

    assert dispatched == []
    inventory = service._validator_stage_pretransport_inventory
    assert inventory["request_count"] == 2
    assert inventory["aggregate_preflight"]["status"] == "blocked_budget"
    assert inventory["aggregate_preflight"]["budget_status"]["provider_calls"] == "exceeded"
    snapshot = controller.snapshot()
    assert snapshot["calls_used"] == 0
    assert snapshot["calls_reserved"] == 0


def test_validator_scope_budget_admits_exact_materialized_call_and_output_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, validation_retry_limit=0)
    _bind_fixture_reuse_miss(service, monkeypatch)
    controller = _bind_aggregate_budget(
        monkeypatch, calls=2, output_tokens=2_048, retries=0
    )
    eligible = [
        _citation_result("set-a", "paper-a", "Claim A."),
        _citation_result("set-b", "paper-b", "Claim B."),
    ]
    dispatched: list[str] = []

    def fake_run_adjudication_stage(
        _service: Any, _api_config: Mapping[str, Any], packet: Any
    ) -> Mapping[str, Any]:
        reservation = controller.admit(
            requested_output_tokens=1_024,
            requested_retry_attempts=0,
            context={"call_id": current_validation.adjudication_call_id(packet)},
        )
        dispatched.append(current_validation.adjudication_call_id(packet))
        controller.complete(
            reservation,
            {"status": "success", "attempts": 1, "output_tokens": 1_024},
        )
        return {"status": "supported", "confidence": 0.99}

    monkeypatch.setattr(
        current_validation,
        "run_adjudication_stage",
        fake_run_adjudication_stage,
    )

    current_validation._adjudicate(service, eligible, scope="primary_validation")

    inventory = service._validator_stage_pretransport_inventory
    assert len(dispatched) == 2
    assert inventory["aggregate_preflight"]["status"] == "within_budget"
    assert inventory["aggregate_preflight"]["totals"]["logical_calls_upper_bound"] == 2
    assert inventory["aggregate_preflight"]["totals"]["estimated_output_tokens_all_attempts"] == 2_048
    snapshot = controller.snapshot()
    assert snapshot["calls_used"] == 2
    assert snapshot["output_tokens_used"] == 2_048


def test_validator_scope_projection_caps_retries_across_materialized_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, validation_retry_limit=1)
    _bind_fixture_reuse_miss(service, monkeypatch)
    controller = _bind_aggregate_budget(
        monkeypatch, calls=3, output_tokens=3_072, retries=1
    )
    eligible = [
        _citation_result("set-a", "paper-a", "Claim A."),
        _citation_result("set-b", "paper-b", "Claim B."),
    ]
    dispatched: list[str] = []

    def fake_run_adjudication_stage(
        _service: Any, _api_config: Mapping[str, Any], packet: Any
    ) -> Mapping[str, Any]:
        reservation = controller.admit(
            requested_output_tokens=2_048,
            requested_retry_attempts=1,
            context={"call_id": current_validation.adjudication_call_id(packet)},
        )
        dispatched.append(current_validation.adjudication_call_id(packet))
        controller.complete(
            reservation,
            {"status": "success", "attempts": 1, "output_tokens": 1_024},
        )
        return {"status": "supported", "confidence": 0.99}

    monkeypatch.setattr(
        current_validation,
        "run_adjudication_stage",
        fake_run_adjudication_stage,
    )

    current_validation._adjudicate(service, eligible, scope="primary_validation")

    totals = service._validator_stage_pretransport_inventory["aggregate_preflight"]["totals"]
    assert len(dispatched) == 2
    assert totals["logical_calls_upper_bound"] == 2
    assert totals["retry_attempts_configured_upper_bound"] == 2
    assert totals["retry_attempts_possible_upper_bound"] == 1
    assert totals["physical_attempts_upper_bound"] == 3
    assert totals["estimated_output_tokens_all_attempts"] == 3_072


def test_verified_adjudication_reuse_stays_transport_free_in_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    result = _citation_result("set-reused", "paper-reused", "Reused claim.")
    _bind_aggregate_budget(monkeypatch, calls=0, output_tokens=0, retries=0, wall_seconds=0)
    reuse_path = tmp_path / "reuse.json"
    reuse_path.write_text(
        json.dumps(
            {
                "provider_output_artifact_id": "provider-output-fixture",
                "provider_output_artifact_hash": "o" * 64,
                "source_receipt_hash": "r" * 64,
            }
        ),
        encoding="utf-8",
    )
    reuse_record = SimpleNamespace(
        path=str(reuse_path), artifact_id="reuse-fixture", content_hash="a" * 64
    )
    output_record = SimpleNamespace(
        status="ready", artifact_id="provider-output-fixture", content_hash="o" * 64
    )
    monkeypatch.setattr(
        service,
        "find_verified_adjudication_reuse",
        lambda **_kwargs: ({"status": "supported", "confidence": 0.99}, reuse_record, ""),
    )
    monkeypatch.setattr(
        service.artifact_registry,
        "get",
        lambda _artifact_id: output_record,
    )
    registrations: list[dict[str, Any]] = []
    monkeypatch.setattr(
        service,
        "register_verified_reuse_call",
        lambda **kwargs: registrations.append(kwargs),
    )
    monkeypatch.setattr(current_validation, "_apply_adjudication", lambda item, _report: item)
    monkeypatch.setattr(
        current_validation,
        "run_adjudication_stage",
        lambda *_args, **_kwargs: pytest.fail("verified reuse must not call the provider"),
    )

    output = current_validation._adjudicate(
        service, [result], scope="repair_revalidation"
    )

    inventory = service._validator_stage_pretransport_inventory
    assert output == [result]
    assert len(registrations) == 1
    assert inventory["scope"] == "repair_revalidation"
    assert inventory["logical_call_upper_bound"] == 1
    assert inventory["physical_attempts_upper_bound"] == 2
    assert inventory["verified_reuse_count"] == 1
    assert inventory["transport_call_candidate_count"] == 0
    assert inventory["aggregate_preflight"]["status"] == "within_budget"
    assert inventory["aggregate_preflight"]["totals"]["logical_calls_upper_bound"] == 0
    assert inventory["aggregate_preflight"]["totals"]["physical_attempts_upper_bound"] == 0
    assert inventory["outcomes"] == [
        {"call_id": inventory["requests"][0]["call_id"], "status": "verified_reuse"}
    ]


def test_zero_validator_retry_limit_emits_one_post_and_one_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, validation_retry_limit=0)
    result = _citation_result("set-zero-retry", "paper-zero-retry", "Retry bounded claim.")
    monkeypatch.setattr(
        service,
        "find_verified_adjudication_reuse",
        lambda **_kwargs: (None, None, ""),
    )
    posts: list[dict[str, Any]] = []

    class RetryableResponse:
        status_code = 503
        text = '{"error":{"message":"synthetic retryable response"}}'
        content = text.encode("utf-8")

        def __init__(self) -> None:
            self.headers = {
                "content-type": "application/json",
                "x-request-id": "local-503",
            }

        def raise_for_status(self) -> None:
            raise requests.HTTPError("synthetic local 503", response=self)

        def iter_content(self, chunk_size: int):
            assert chunk_size > 0
            yield self.content

        def json(self) -> dict[str, Any]:
            return {"error": {"message": "synthetic retryable response"}}

        def close(self) -> None:
            return None

    def fake_post(url: str, **kwargs: Any) -> RetryableResponse:
        posts.append({"url": url, **kwargs})
        return RetryableResponse()

    # Intercept the transport boundary; no socket or provider is reached.
    monkeypatch.setattr(ai_interface.requests, "post", fake_post)
    monkeypatch.setattr(ai_interface.time, "sleep", lambda *_args, **_kwargs: None)

    current_validation._adjudicate(service, [result])

    inventory = service._validator_stage_pretransport_inventory
    call_id = inventory["requests"][0]["call_id"]
    runtime = service._provider_runtimes[call_id]
    expected_call = service._expected_provider_calls[call_id]
    assert len(posts) == 1
    assert posts[0]["url"] == "http://127.0.0.1:1/v1/chat/completions"
    assert inventory["requests"][0]["attempts_upper_bound"] == 1
    assert inventory["physical_attempts_upper_bound"] == 1
    assert inventory["requests"][0]["expected_call_max_attempts"] == 1
    assert expected_call.max_attempts == 1
    assert len(runtime.receipts) == 1
    assert runtime.receipts[0].attempts == 1


def test_output_dir_does_not_infer_repair_revalidation_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    observed_scopes: list[str] = []
    monkeypatch.setattr(
        current_validation,
        "_load_inputs",
        lambda *_args, **_kwargs: ({}, {}, [], None, None),
    )
    monkeypatch.setattr(
        current_validation,
        "_input_contract",
        lambda *_args, **_kwargs: (
            current_validation.ValidationInputArtifactsV1(),
            0,
            False,
            False,
            (),
        ),
    )
    monkeypatch.setattr(
        current_validation,
        "_adjudicate",
        lambda _service, results, *, scope, input_records=None, paper_records=None, repair_transaction_record=None: observed_scopes.append(scope) or list(results),
    )

    class EmptyValidator:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def validate(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(citation_results=[])

    monkeypatch.setattr(current_validation, "ReviewValidator", EmptyValidator)
    monkeypatch.setattr(current_validation, "_write_reports", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(service, "bind_validation_source_authority", lambda *_args: None)
    monkeypatch.setattr(
        "validation.source_binding.build_validation_source_authority_fingerprint",
        lambda **_kwargs: ({}, "", ()),
    )

    outcome = current_validation.run_current_validation(
        service,
        output_dir=tmp_path / "gate-h-pre-repair-detection",
    )

    assert outcome["revalidation"] is True
    assert observed_scopes == ["current_validation"]


def test_repair_revalidation_passes_explicit_inventory_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    draft_path = tmp_path / "repaired_draft.json"
    manifest_path = tmp_path / "repaired_manifest.json"
    draft_path.write_text("{}", encoding="utf-8")
    manifest_path.write_text("{}", encoding="utf-8")
    draft_record = SimpleNamespace(
        artifact_id="repaired-draft",
        path=str(draft_path),
        status="ready",
    )
    manifest_record = SimpleNamespace(
        artifact_id="repaired-manifest",
        path=str(manifest_path),
        status="ready",
    )
    observed_kwargs: dict[str, Any] = {}

    def fake_run_current_validation(_service: Any, **kwargs: Any) -> dict[str, Any]:
        observed_kwargs.update(kwargs)
        return {}

    monkeypatch.setattr(
        current_validation,
        "run_current_validation",
        fake_run_current_validation,
        raising=False,
    )
    monkeypatch.setattr(
        service,
        "finalize_provider_receipts",
        lambda **_kwargs: {
            "closure": SimpleNamespace(to_dict=dict),
            "closure_record": SimpleNamespace(artifact_id="closure-record"),
        },
    )

    outcome = service.revalidate_review_artifacts(
        review_draft_record=draft_record,
        citation_manifest_record=manifest_record,
        output_dir=str(tmp_path / "repair-revalidation"),
        result_artifact_id="validation-result-repaired",
        paper_artifact_records=[],
    )

    assert observed_kwargs["validation_scope"] == "repair_revalidation"
    assert outcome["provider_receipt_closure_record_id"] == "closure-record"

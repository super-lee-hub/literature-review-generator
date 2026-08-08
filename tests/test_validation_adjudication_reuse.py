from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import types
from typing import Any, Mapping

import pytest

from runtime.provider_receipt_closure import ExpectedProviderCall, ProviderReceiptClosure
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.settings import ApplicationSettings
from validation.adjudication_checkpoint import AdjudicationCheckpointStore
from validation.adjudication_reuse import (
    ADJUDICATION_REUSE_ARTIFACT_TYPE,
    build_reuse_key,
    reuse_record_artifact_id,
    verify_reuse_record,
)
from validation.execution_service import ValidationExecutionService
from validation.llm_adjudicator import AdjudicationPacket
import validation.current_validation as current_validation


def _result(claim_text: str = "claim") -> Any:
    return types.SimpleNamespace(
        claim_text=claim_text,
        paper_ids=["paper-1"],
        details={},
    )


def _packet(claim_text: str = "claim") -> AdjudicationPacket:
    return AdjudicationPacket(
        citation_set_key="set-1",
        stage="primary",
        claim_text=claim_text,
        claim_context="",
        block_context="",
        claim_type="result",
        claim_type_confidence=1.0,
        claim_type_rationale="",
        paper_ids=["paper-1"],
        claim_units=[],
        target_claim_unit={},
        claim_unit_results=[],
        paper_identity_hints={},
        per_paper_evidence_packets={},
        evidence_excerpt_list=[],
        trimmed_candidate_counts={},
        evidence_status="",
        disposition="",
    )


def _service(
    tmp_path: Path,
    *,
    config: Mapping[str, str] | None = None,
    job_id: str = "reuse-job",
) -> ValidationExecutionService:
    workspace = JobWorkspace.create(str(tmp_path / "output"), "validation", job_id=job_id)
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    effective_config = {
        "api_key": "secret-a",
        "model": "validator-model",
        "api_base": "https://example.test",
        **(config or {}),
    }
    settings = ApplicationSettings.from_config(
        {
            "Validator_API": dict(effective_config),
            "Runtime": {"max_workers": "1", "validation_retry_limit": "0"},
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
        runtime_config={"Validator_API": dict(effective_config)},
    )


def _patch_ai_call(monkeypatch: Any, calls: list[int]) -> None:
    def fake_call(*_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        calls.append(1)
        return {"status": "supported", "confidence": 0.99}

    monkeypatch.setattr("validation.llm_adjudicator._call_ai_api", fake_call)
    monkeypatch.setattr(
        current_validation,
        "get_validator_api_config",
        lambda _settings: {
            "api_key": "secret-a",
            "model": "validator-model",
            "api_base": "https://example.test",
        },
    )
    monkeypatch.setattr(
        current_validation,
        "build_adjudication_packet",
        lambda *_args, **_kwargs: _packet(),
    )
    monkeypatch.setattr(current_validation, "_apply_adjudication", lambda item, _report: item)


def test_adjudication_reuse_thread_single_flight_exactly_one_transport(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    service = _service(tmp_path)
    calls: list[int] = []
    _patch_ai_call(monkeypatch, calls)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(current_validation._adjudicate, service, [_result()]) for _ in range(4)]
        outputs = [future.result() for future in futures]

    assert len(calls) == 1
    assert all(len(output) == 1 for output in outputs)
    reuse_key = build_reuse_key(
        packet=_packet(),
        api_config={
            "api_key": "secret-a",
            "model": "validator-model",
            "api_base": "https://example.test",
        },
        input_dependency_hashes=service._input_dependency_hashes,
    )
    reuse_record = service.artifact_registry.get(reuse_record_artifact_id(reuse_key))
    assert reuse_record is not None and reuse_record.status == "ready"
    closure = service.finalize_provider_receipts()
    assert closure["closure"].complete is True
    assert reuse_record.artifact_id in {
        dependency.artifact_id for dependency in closure["closure_record"].depends_on
    }


def test_adjudication_reuse_different_packets_make_independent_calls(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    service = _service(tmp_path)
    calls: list[int] = []
    monkeypatch.setattr(
        current_validation,
        "get_validator_api_config",
        lambda _settings: {
            "api_key": "secret-a",
            "model": "validator-model",
            "api_base": "https://example.test",
        },
    )

    def fake_call(*_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        calls.append(1)
        return {"status": "supported", "confidence": 0.99}

    monkeypatch.setattr("validation.llm_adjudicator._call_ai_api", fake_call)
    monkeypatch.setattr(current_validation, "_apply_adjudication", lambda item, _report: item)

    def packet_for(text: str) -> AdjudicationPacket:
        return AdjudicationPacket(
            citation_set_key="set-1",
            stage="primary",
            claim_text=text,
            claim_context="",
            block_context="",
            claim_type="result",
            claim_type_confidence=1.0,
            claim_type_rationale="",
            paper_ids=["paper-1"],
            claim_units=[],
            target_claim_unit={},
            claim_unit_results=[],
            paper_identity_hints={},
            per_paper_evidence_packets={},
            evidence_excerpt_list=[],
            trimmed_candidate_counts={},
            evidence_status="",
            disposition="",
        )

    monkeypatch.setattr(
        current_validation,
        "build_adjudication_packet",
        lambda result, **_kwargs: packet_for(result.claim_text),
    )
    current_validation._adjudicate(service, [_result("claim-a")])
    current_validation._adjudicate(service, [_result("claim-b")])

    assert len(calls) == 2


def test_adjudication_reuse_different_route_config_has_different_key(tmp_path: Path) -> None:
    service_a = _service(tmp_path / "a", config={"model": "model-a"})
    service_b = _service(tmp_path / "b", config={"model": "model-b"})
    packet = _packet()
    key_a = build_reuse_key(
        packet=packet,
        api_config={"api_key": "k", "model": "model-a", "api_base": "https://x.test"},
        input_dependency_hashes=service_a._input_dependency_hashes,
    )
    key_b = build_reuse_key(
        packet=packet,
        api_config={"api_key": "k", "model": "model-b", "api_base": "https://x.test"},
        input_dependency_hashes=service_b._input_dependency_hashes,
    )
    assert key_a != key_b


def test_adjudication_tampered_raw_checkpoint_is_not_trusted(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    service = _service(tmp_path)
    calls: list[int] = []
    _patch_ai_call(monkeypatch, calls)
    checkpoint_root = Path(service.workspace.paths.checkpoints_dir) / "validation_adjudication"
    store = AdjudicationCheckpointStore(checkpoint_root)
    key = store.key_for(
        packet=_packet().__dict__,
        stage="primary",
        route_hash="tampered-route",
    )
    store.save(key, {"status": "supported", "confidence": 1.0})

    current_validation._adjudicate(service, [_result()])

    assert len(calls) == 1


def test_adjudication_reuse_record_tamper_fails_closed(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    service = _service(tmp_path)
    calls: list[int] = []
    _patch_ai_call(monkeypatch, calls)
    current_validation._adjudicate(service, [_result()])
    reuse_key = build_reuse_key(
        packet=_packet(),
        api_config={
            "api_key": "secret-a",
            "model": "validator-model",
            "api_base": "https://example.test",
        },
        input_dependency_hashes=service._input_dependency_hashes,
    )
    record = service.artifact_registry.get(reuse_record_artifact_id(reuse_key))
    assert record is not None
    Path(record.path).write_text('{"tampered": true}', encoding="utf-8")

    report, error = verify_reuse_record(
        service.artifact_registry,
        service.artifact_registry.get(reuse_record_artifact_id(reuse_key)),
        packet=_packet(),
        api_config={
            "api_key": "secret-a",
            "model": "validator-model",
            "api_base": "https://example.test",
        },
        input_dependency_hashes=service._input_dependency_hashes,
        current_epoch=service.closure_epoch_id,
        service=service,
    )
    assert report is None
    assert error


def test_provider_receipt_closure_allows_verified_reuse_without_receipt() -> None:
    expected = ExpectedProviderCall(
        call_id="validation:primary:abc",
        job_id="job",
        attempt_id="attempt",
        stage_name="stage4_validate",
        node_id="primary:set-1",
        closure_epoch_id="epoch",
        logical_attempt_identity="attempt",
        expected_call_graph_hash="graph",
        prompt_hash="p" * 64,
        input_hash="i" * 64,
        config_hash="c" * 64,
        schema_hash="s" * 64,
        provider_response_hash="r" * 64,
        normalized_output_hash="r" * 64,
        artifact_payload_hash="r" * 64,
        artifact_content_hash="r" * 64,
        registry_file_hash="f" * 64,
        artifact_path="C:/output.json",
        registered_artifact_hash="r" * 64,
        node_output_hash="r" * 64,
        replay_output_hash="r" * 64,
        max_attempts=1,
        verified_reuse=True,
        reuse_evidence_artifact_id="validation_adjudication_reuse:key",
        reuse_evidence_artifact_hash="e" * 64,
        reuse_evidence_record_hash="e" * 64,
    )
    result = ProviderReceiptClosure.evaluate([expected], [])

    assert result.complete is True
    assert result.verified_reuse_call_ids == ("validation:primary:abc",)
    assert result.observed_call_ids == ()

    incomplete = ExpectedProviderCall(
        call_id="validation:primary:abc",
        job_id="job",
        attempt_id="attempt",
        stage_name="stage4_validate",
        node_id="primary:set-1",
        verified_reuse=True,
    )
    failed = ProviderReceiptClosure.evaluate([incomplete], [])
    assert failed.complete is False
    assert failed.stale_call_ids == ("validation:primary:abc",)


def test_adjudication_reuse_after_authoritative_publication_reuses(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    first_service = _service(tmp_path)
    calls: list[int] = []
    _patch_ai_call(monkeypatch, calls)
    current_validation._adjudicate(first_service, [_result()])
    assert len(calls) == 1

    second_service = _service(tmp_path)
    current_validation._adjudicate(second_service, [_result()])
    assert len(calls) == 1
    closure = second_service.finalize_provider_receipts()
    assert closure["closure"].complete is True


def test_adjudication_reuse_before_publication_makes_fresh_call(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    service = _service(tmp_path)
    calls: list[int] = []

    def fake_call(*_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        calls.append(1)
        return {"status": "supported", "confidence": 0.99}

    monkeypatch.setattr("validation.llm_adjudicator._call_ai_api", fake_call)
    monkeypatch.setattr(
        current_validation,
        "get_validator_api_config",
        lambda _settings: {
            "api_key": "secret-a",
            "model": "validator-model",
            "api_base": "https://example.test",
        },
    )
    monkeypatch.setattr(current_validation, "_apply_adjudication", lambda item, _report: item)
    monkeypatch.setattr(
        current_validation,
        "build_adjudication_packet",
        lambda *_args, **_kwargs: _packet(),
    )

    current_validation._adjudicate(service, [_result()])
    assert len(calls) == 1
    assert service.artifact_registry.get(
        reuse_record_artifact_id(
            build_reuse_key(
                packet=_packet(),
                api_config={
                    "api_key": "secret-a",
                    "model": "validator-model",
                    "api_base": "https://example.test",
                },
                input_dependency_hashes=service._input_dependency_hashes,
            )
        )
    ) is not None

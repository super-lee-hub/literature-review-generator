from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from runtime.artifact_validators import CURRENT_PRODUCTION_ARTIFACT_TYPES
from runtime.reconcile import DEFAULT_SCHEMA_VALIDATORS, ReconcileValidationError
from services.artifact_registry import ArtifactRegistry, file_sha256
from tests.test_review_writer_stage_preflight import (
    _successful_writer_result,
    _writer_fixture,
)


@pytest.fixture(scope="module")
def produced_snapshot(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("writer-receipt-snapshot")
    calls: list[int] = []

    def local_writer(**kwargs: Any) -> dict[str, Any]:
        calls.append(int(kwargs["section_number"]))
        return dict(_successful_writer_result(**kwargs))

    service, outline, packets = _writer_fixture(root, writer=local_writer)
    cancellation_checks = 0

    def cancel_before_second_section() -> None:
        nonlocal cancellation_checks
        cancellation_checks += 1
        if cancellation_checks == 2:
            raise RuntimeError("cancelled before second section")

    service.cancellation_checker = cancel_before_second_section
    with pytest.raises(RuntimeError, match="cancelled before second section"):
        service.run(outline_payload=outline, evidence_packets=packets)

    assert calls == [1]
    records = [
        record
        for record in service.registry.list_records()
        if record.artifact_type == "review_provider_receipt_snapshot"
    ]
    assert len(records) == 1
    record = records[0]
    payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
    return service.registry, record, payload


def test_real_partial_writer_snapshot_is_registered_and_passes_default_schema(
    produced_snapshot,
) -> None:
    registry, record, payload = produced_snapshot

    assert "review_provider_receipt_snapshot" in CURRENT_PRODUCTION_ARTIFACT_TYPES
    validator = DEFAULT_SCHEMA_VALIDATORS.get("review_provider_receipt_snapshot")
    assert validator is not None
    assert record.artifact_role == "provider_receipt_snapshot"
    assert record.artifact_version == "v1"
    assert payload["artifact_type"] == "review_provider_receipt_snapshot"
    assert payload["artifact_version"] == "v1"
    assert payload["status"] == "partial"
    assert "complete" not in payload
    assert payload["stage_name"] == "stage3_review"
    assert payload["expected_call_graph_hash"]
    assert payload["receipt"]["artifact_type"] == "provider_call_receipt"
    assert payload["receipt"]["artifact_version"] == "v2"
    assert payload["receipt"]["test_only"] is True

    validator(record, Path(record.path))
    ArtifactRegistry._verify_ready_artifact(record)
    assert registry.get(record.artifact_id) == record


def _mutate_owner(payload: dict[str, Any]) -> None:
    payload["job_id"] = "foreign-job"


def _mutate_stage(payload: dict[str, Any]) -> None:
    payload["stage_name"] = "stage2_analyze"


def _mutate_section(payload: dict[str, Any]) -> None:
    payload["section_id"] = "section_other"


def _mutate_call(payload: dict[str, Any]) -> None:
    payload["call_id"] = "review:section_other"


def _mutate_receipt_hash(payload: dict[str, Any]) -> None:
    payload["receipt_hash"] = "0" * 64


def _mutate_receipt_status(payload: dict[str, Any]) -> None:
    payload["receipt"]["status"] = "failed"
    payload["receipt"]["error_kind"] = "invalid_response"


def _mutate_incomplete_receipt(payload: dict[str, Any]) -> None:
    payload["receipt"]["incomplete_reason"] = "max_output_tokens"


def _mutate_call_graph_hash(payload: dict[str, Any]) -> None:
    payload["expected_call_graph_hash"] = "not-a-sha256"


def _mutate_snapshot_schema_version(payload: dict[str, Any]) -> None:
    payload["artifact_version"] = "v2"


def _mutate_receipt_schema_version(payload: dict[str, Any]) -> None:
    payload["receipt"]["artifact_version"] = "v1"


@pytest.mark.parametrize(
    "mutate",
    (
        _mutate_owner,
        _mutate_stage,
        _mutate_section,
        _mutate_call,
        _mutate_receipt_hash,
        _mutate_receipt_status,
        _mutate_incomplete_receipt,
        _mutate_call_graph_hash,
        _mutate_snapshot_schema_version,
        _mutate_receipt_schema_version,
    ),
    ids=(
        "owner",
        "stage",
        "section",
        "call",
        "receipt-hash",
        "receipt-status",
        "incomplete-receipt",
        "call-graph-hash",
        "snapshot-schema-version",
        "receipt-schema-version",
    ),
)
def test_default_snapshot_schema_rejects_malformed_producer_artifacts(
    produced_snapshot,
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    _registry, record, original = produced_snapshot
    validator = DEFAULT_SCHEMA_VALIDATORS.get("review_provider_receipt_snapshot")
    assert validator is not None

    payload = json.loads(json.dumps(original))
    mutate(payload)
    malformed_path = tmp_path / "malformed-review-receipt-snapshot.json"
    malformed_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    malformed_record = replace(
        record,
        path=str(malformed_path),
        content_hash=file_sha256(malformed_path),
    )

    with pytest.raises(ReconcileValidationError):
        validator(malformed_record, malformed_path)

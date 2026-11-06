from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from outline.v3_artifacts import RelationAdjudicationResult
from outline.v3_executor import (
    OutlineV3ExecutionError,
    _resolve_outline_relation_shard_dependency_record,
)
from runtime.provider_receipt_closure import ExpectedProviderCall
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
)
from validation.closure import (
    _declared_historical_receipt_overlap,
    _is_declared_historical_receipt_row,
    _outline_relation_shard_dependency_issues,
)


def _relation_record(tmp_path: Path, *, job_id: str = "closure-job") -> tuple[ArtifactRegistry, ArtifactRecord]:
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", job_id)
    output_path = tmp_path / "relation-shard.json"
    artifact = RelationAdjudicationResult(
        job_id=job_id,
        payload={"confirmed_relation_ids": [], "rejected_relations": []},
    )
    output_path.write_text(
        json.dumps(artifact.to_dict(), ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    record = registry.register_file(
        artifact_role="outline_v3_relation_shard_output",
        artifact_type="relation_adjudication_result",
        artifact_version="v3",
        path=output_path,
        producer="test.relation_shard",
        artifact_id="relation-shard:registered",
    )
    return registry, record


def _closure_record(
    tmp_path: Path,
    *,
    job_id: str,
    dependencies: list[ArtifactDependencyRefV2],
) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id="outline-v3:provider_receipt_closure",
        artifact_role="provider_receipt_closure",
        artifact_type="provider_receipt_closure",
        artifact_version="v1",
        path=str(tmp_path / "closure.json"),
        producer="test.closure",
        job_id=job_id,
        status="ready",
        content_hash="c" * 64,
        depends_on=dependencies,
    )


def _expected_call(record: ArtifactRecord) -> dict[str, Any]:
    return {
        "call_id": "outline:relation_adjudication:batch_1",
        "node_id": "relation_adjudication:batch_1",
        "artifact_path": record.path,
        "registry_file_hash": record.content_hash,
    }


@pytest.mark.parametrize(
    "declared,observed,expected",
    [({"historical"}, set(), False), ({"historical"}, {"current"}, False),
     ({"same-id"}, {"same-id"}, True)],
)
def test_declared_historical_receipt_ids_must_be_disjoint_from_current_receipts(
    declared: set[str], observed: set[str], expected: bool,
) -> None:
    assert _declared_historical_receipt_overlap(declared, observed) is expected


@pytest.mark.parametrize("ref_count", [1, 2])
def test_relation_shard_dependency_requires_exact_ready_local_typed_ref(tmp_path: Path, ref_count: int) -> None:
    registry, relation = _relation_record(tmp_path)
    closure = _closure_record(
        tmp_path,
        job_id=registry.job_id,
        dependencies=[ArtifactDependencyRefV2.from_record(relation)] * ref_count,
    )

    assert _outline_relation_shard_dependency_issues(
        expected_call=_expected_call(relation),
        closure_record=closure,
        dependency_record=relation,
        registry=registry,
        external_registry_resolver=None,
    ) == []


def test_relation_shard_dependency_rejects_missing_direct_reference(tmp_path: Path) -> None:
    registry, relation = _relation_record(tmp_path)
    closure = _closure_record(tmp_path, job_id=registry.job_id, dependencies=[])

    issues = _outline_relation_shard_dependency_issues(
        expected_call=_expected_call(relation),
        closure_record=closure,
        dependency_record=relation,
        registry=registry,
        external_registry_resolver=None,
    )

    assert issues == [
        "provider_closure_expected_relation_artifact_dependency_missing:outline:"
        "outline:relation_adjudication:batch_1"
    ]


@pytest.mark.parametrize(
    "record_changes,dependency_kind",
    [
        ({"artifact_type": "wrong_type"}, "local_job"),
        ({"artifact_version": "v2"}, "local_job"),
        ({"job_id": "foreign-job"}, "external_job"),
    ],
)
def test_relation_shard_dependency_rejects_wrong_type_version_or_foreign_job(
    tmp_path: Path,
    record_changes: dict[str, str],
    dependency_kind: Any,
) -> None:
    registry, relation = _relation_record(tmp_path)
    candidate = replace(relation, **record_changes)
    closure = _closure_record(
        tmp_path,
        job_id=registry.job_id,
        dependencies=[
            ArtifactDependencyRefV2.from_record(candidate, dependency_kind=dependency_kind)
        ],
    )

    issues = _outline_relation_shard_dependency_issues(
        expected_call=_expected_call(relation),
        closure_record=closure,
        dependency_record=candidate,
        registry=registry,
        external_registry_resolver=None,
    )

    assert any("_identity_mismatch:outline:" in issue for issue in issues)


def test_relation_shard_dependency_rejects_tampered_ready_bytes(tmp_path: Path) -> None:
    registry, relation = _relation_record(tmp_path)
    closure = _closure_record(
        tmp_path,
        job_id=registry.job_id,
        dependencies=[ArtifactDependencyRefV2.from_record(relation)],
    )
    Path(relation.path).write_text("{}", encoding="utf-8")

    issues = _outline_relation_shard_dependency_issues(
        expected_call=_expected_call(relation),
        closure_record=closure,
        dependency_record=relation,
        registry=registry,
        external_registry_resolver=None,
    )

    assert any("_untrusted:outline:" in issue for issue in issues)


def test_relation_shard_dependency_rejects_expected_hash_mismatch(tmp_path: Path) -> None:
    registry, relation = _relation_record(tmp_path)
    closure = _closure_record(
        tmp_path,
        job_id=registry.job_id,
        dependencies=[ArtifactDependencyRefV2.from_record(relation)],
    )
    expected = {**_expected_call(relation), "registry_file_hash": "0" * 64}

    issues = _outline_relation_shard_dependency_issues(
        expected_call=expected,
        closure_record=closure,
        dependency_record=relation,
        registry=registry,
        external_registry_resolver=None,
    )

    assert any("_hash_mismatch:outline:" in issue for issue in issues)


@pytest.mark.parametrize(
    "row,declared,expected",
    [
        (
            {
                "receipt_id": "receipt-historical",
                "call_id": "outline:old-call",
                "job_id": "closure-job",
                "stage_name": "outline_v3",
                "closure_epoch_id": "same-current-epoch",
            },
            {"receipt-historical"},
            True,
        ),
        (
            {
                "receipt_id": "receipt-historical-extra",
                "call_id": "outline:old-call",
                "job_id": "closure-job",
                "stage_name": "outline_v3",
                "closure_epoch_id": "same-current-epoch",
            },
            {"receipt-historical"},
            False,
        ),
        (
            {
                "receipt_id": "receipt-historical",
                "call_id": "outline:old-call",
                "job_id": "foreign-job",
                "stage_name": "outline_v3",
                "closure_epoch_id": "same-current-epoch",
            },
            {"receipt-historical"},
            False,
        ),
        (
            {
                "receipt_id": "receipt-historical",
                "call_id": "outline:old-call",
                "job_id": "closure-job",
                "stage_name": "other_stage",
                "closure_epoch_id": "same-current-epoch",
            },
            {"receipt-historical"},
            False,
        ),
    ],
)
def test_only_exact_verified_same_epoch_history_is_excluded(
    row: dict[str, str], declared: set[str], expected: bool,
) -> None:
    assert _is_declared_historical_receipt_row(
        row,
        declared_receipt_ids=declared,
        job_id="closure-job",
        stage_name="outline_v3",
    ) is expected


def _expected_provider_call(record: ArtifactRecord, *, job_id: str = "closure-job") -> ExpectedProviderCall:
    return ExpectedProviderCall(
        call_id="outline:relation_adjudication:batch_1",
        job_id=job_id,
        attempt_id="attempt-1",
        stage_name="outline_v3",
        node_id="relation_adjudication:batch_1",
        logical_attempt_identity="logical-1",
        expected_call_graph_hash="g" * 64,
        artifact_path=record.path,
        registry_file_hash=record.content_hash,
    )


def test_producer_resolves_cached_relation_output_by_path_hash_and_job(tmp_path: Path) -> None:
    registry, relation = _relation_record(tmp_path)

    assert _resolve_outline_relation_shard_dependency_record(
        registry, _expected_provider_call(relation),
    ) == relation


@pytest.mark.parametrize(
    "expected_changes",
    [
        {"artifact_path": "missing-relation.json"},
        {"registry_file_hash": "0" * 64},
        {"job_id": "foreign-job"},
    ],
)
def test_producer_rejects_missing_foreign_or_hash_mismatched_relation_output(
    tmp_path: Path, expected_changes: dict[str, str],
) -> None:
    registry, relation = _relation_record(tmp_path)
    expected = replace(_expected_provider_call(relation), **expected_changes)

    with pytest.raises(OutlineV3ExecutionError, match="BLOCKED_CLOSURE"):
        _resolve_outline_relation_shard_dependency_record(registry, expected)


def test_producer_rejects_tampered_cached_relation_output(tmp_path: Path) -> None:
    registry, relation = _relation_record(tmp_path)
    Path(relation.path).write_text("{}", encoding="utf-8")

    with pytest.raises(OutlineV3ExecutionError, match="untrusted"):
        _resolve_outline_relation_shard_dependency_record(
            registry, _expected_provider_call(relation),
        )


def test_producer_rejects_wrong_relation_artifact_type(tmp_path: Path) -> None:
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "closure-job")
    output_path = tmp_path / "wrong-relation.json"
    output_path.write_text("{}", encoding="utf-8")
    wrong = registry.register_file(
        artifact_role="wrong",
        artifact_type="wrong_type",
        artifact_version="v3",
        path=output_path,
        producer="test.wrong_relation",
        artifact_id="relation-shard:wrong-type",
    )

    with pytest.raises(OutlineV3ExecutionError, match="found 0"):
        _resolve_outline_relation_shard_dependency_record(
            registry, _expected_provider_call(wrong),
        )



def test_same_call_same_epoch_old_and_current_receipts_use_exact_ids() -> None:
    old_receipt = {
        "receipt_id": "receipt-old",
        "call_id": "outline:relation_adjudication:batch_1",
        "job_id": "closure-job",
        "stage_name": "outline_v3",
        "closure_epoch_id": "same-epoch",
    }
    current_receipt = {
        "receipt_id": "receipt-current",
        "call_id": "outline:relation_adjudication:batch_1",
        "job_id": "closure-job",
        "stage_name": "outline_v3",
        "closure_epoch_id": "same-epoch",
    }
    historical_ids = {"receipt-old"}
    observed_receipt_ids = {"receipt-current"}

    assert _is_declared_historical_receipt_row(
        old_receipt,
        declared_receipt_ids=historical_ids,
        job_id="closure-job",
        stage_name="outline_v3",
    )
    assert not _is_declared_historical_receipt_row(
        current_receipt,
        declared_receipt_ids=historical_ids,
        job_id="closure-job",
        stage_name="outline_v3",
    )
    assert not _declared_historical_receipt_overlap(
        historical_ids,
        observed_receipt_ids,
    )


def test_exact_receipt_declared_both_historical_and_current_is_rejected() -> None:
    assert _declared_historical_receipt_overlap(
        {"receipt-overlap"},
        {"receipt-overlap"},
    )

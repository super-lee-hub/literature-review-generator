"""Focused tests for fenced batching of local portable Stage 1 authorities.

These tests use in-memory Registry doubles and synthetic ArtifactRecords. They
do not execute RuntimeJobSpec, stage services, providers, or live workspaces.
"""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from typing import Any, Mapping, Sequence

import pytest

import validation.closure as closure_module
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    RegistryError,
    RegistrySnapshotChanged,
)


_LOCAL_JOB = "portable-batch-local"
_ORIGIN_JOB = "portable-batch-origin"
_TYPED_MANIFEST_ID = "typed-manifest-current"
_TYPED_MANIFEST_HASH = "f" * 64


class RegistryDouble:
    def __init__(self, records: Sequence[ArtifactRecord]) -> None:
        self.job_id = _LOCAL_JOB
        self.records = {record.artifact_id: record for record in records}
        self.batch_calls: list[tuple[tuple[ArtifactRecord, ...], dict[str, Any]]] = []
        self.direct_calls: list[dict[str, Any]] = []
        self.batch_result: dict[str, Exception] = {}
        self.batch_error: Exception | None = None

    def get(self, artifact_id: str) -> ArtifactRecord | None:
        return self.records.get(artifact_id)

    def verify_ready_artifact_dependency_sets(
        self,
        owner_records: Sequence[ArtifactRecord],
        **kwargs: Any,
    ) -> dict[str, Exception]:
        owners = tuple(owner_records)
        self.batch_calls.append((owners, dict(kwargs)))
        if self.batch_error is not None:
            raise self.batch_error
        return dict(self.batch_result)

    def verify_ready_dependencies(self, dependencies: Any, **kwargs: Any) -> list[Any]:
        self.direct_calls.append(dict(kwargs))
        return list(dependencies)


class ExternalRegistryDouble:
    def __init__(self) -> None:
        self.job_id = _ORIGIN_JOB
        self.reload_calls = 0
        self.direct_calls: list[dict[str, Any]] = []

    def reload(self) -> None:
        self.reload_calls += 1

    def verify_ready_dependencies(self, dependencies: Any, **kwargs: Any) -> list[Any]:
        self.direct_calls.append(dict(kwargs))
        return list(dependencies)


_ORIGINAL_FIELDS = {
    "source": ("source_authority_artifact_id", "source_authority_artifact_hash", "summary_file"),
    "source_manifest": (
        "source_summary_manifest_id",
        "source_summary_manifest_hash",
        "stage1_reusable_summary_manifest",
    ),
    "source_provider_receipt_closure": (
        "source_provider_receipt_closure_id",
        "source_provider_receipt_closure_hash",
        "provider_receipt_closure",
    ),
    "source_provider_receipt_ledger": (
        "source_provider_receipt_ledger_id",
        "source_provider_receipt_ledger_hash",
        "provider_receipt_ledger",
    ),
}


def _digest(label: str) -> str:
    return sha256(label.encode("utf-8")).hexdigest()


def _portable_row(index: int, tmp_path) -> tuple[ArtifactRecord, dict[str, Any], dict[str, ArtifactRecord]]:
    payload: dict[str, Any] = {
        "source_authority_kind": "typed_manifest",
        "source_authority_job_id": _ORIGIN_JOB,
        "typed_manifest_artifact_id": _TYPED_MANIFEST_ID,
        "typed_manifest_artifact_hash": _TYPED_MANIFEST_HASH,
    }
    owners: dict[str, ArtifactRecord] = {}
    refs: list[ArtifactDependencyRefV2] = []
    for spec in closure_module._STAGE1_PORTABLE_AUTHORITY_BATCH_SPECS:
        label, original_id_field, original_hash_field, portable_id_field, portable_hash_field, artifact_type = spec
        original_id = f"origin:{label}:{index}"
        artifact_id = f"portable:{label}:{index}"
        content_hash = _digest(f"{label}:{index}")
        path = str(tmp_path / f"{label}-{index}.json")
        record = ArtifactRecord(
            artifact_id=artifact_id,
            artifact_role=f"portable_{label}",
            artifact_type=artifact_type,
            artifact_version="v1",
            path=path,
            producer="test-stage1-portable-authority-batch",
            job_id=_LOCAL_JOB,
            status="ready",
            content_hash=content_hash,
            depends_on=[],
            metadata={
                "authority_kind": "typed_manifest",
                "stage_name": "stage1_analyze",
                "source_authority_job_id": _ORIGIN_JOB,
                "original_artifact_id": original_id,
                "original_artifact_hash": content_hash,
                "typed_manifest_artifact_id": _TYPED_MANIFEST_ID,
                "typed_manifest_artifact_hash": _TYPED_MANIFEST_HASH,
            },
        )
        owners[artifact_id] = record
        refs.append(ArtifactDependencyRefV2.from_record(record))
        payload[original_id_field] = original_id
        payload[original_hash_field] = content_hash
        payload[portable_id_field] = artifact_id
        payload[portable_hash_field] = content_hash

    reuse_record = ArtifactRecord(
        artifact_id=f"reuse:{index}",
        artifact_role="stage1_summary_reuse_record",
        artifact_type="stage1_summary_reuse_record",
        artifact_version="v1",
        path=str(tmp_path / f"reuse-{index}.json"),
        producer="test-stage1-portable-authority-batch",
        job_id=_LOCAL_JOB,
        status="ready",
        content_hash=_digest(f"reuse:{index}"),
        depends_on=refs,
        metadata={},
    )
    return reuse_record, payload, owners


def _authority_call(
    *,
    label: str,
    reuse_payload: Mapping[str, Any],
    reuse_record: ArtifactRecord,
    registry: RegistryDouble,
    batch_owner_ids: frozenset[str] = frozenset(),
    batch_failures: Mapping[str, Exception] | None = None,
    resolver=None,
    extra_dependency_records: Mapping[str, ArtifactRecord] | None = None,
) -> tuple[ArtifactRecord | None, bool, list[str]]:
    spec = next(
        row for row in closure_module._STAGE1_PORTABLE_AUTHORITY_BATCH_SPECS if row[0] == label
    )
    _, original_id_field, original_hash_field, portable_id_field, portable_hash_field, portable_type = spec
    _original_id, _original_hash, original_type = _ORIGINAL_FIELDS[label]
    dependency_by_id = {item.artifact_id: item for item in reuse_record.depends_on}
    dependency_records = {
        artifact_id: record
        for artifact_id in dependency_by_id
        if (record := registry.get(artifact_id)) is not None
    }
    dependency_records.update(extra_dependency_records or {})
    return closure_module._stage1_reuse_authority_dependency(
        stage="analyze",
        label=label,
        reuse_payload=reuse_payload,
        dependency_by_id=dependency_by_id,
        dependency_records_by_id=dependency_records,
        registry=registry,  # type: ignore[arg-type]
        external_registry_resolver=resolver,
        original_id_field=original_id_field,
        original_hash_field=original_hash_field,
        portable_id_field=portable_id_field,
        portable_hash_field=portable_hash_field,
        original_artifact_type=original_type,
        portable_artifact_type=portable_type,
        required=label in {"source", "source_manifest"},
        batched_owner_ids=batch_owner_ids,
        batched_owner_failures=batch_failures,
    )


def _batch(
    rows: Sequence[tuple[ArtifactRecord, dict[str, Any], dict[str, ArtifactRecord]]],
    registry: RegistryDouble,
    *,
    resolver=None,
    expected_revision: int | None = 41,
) -> tuple[frozenset[str], dict[str, Exception]]:
    reuse_records = [record for record, _payload, _owners in rows]
    payloads = {record.artifact_id: payload for record, payload, _owners in rows}
    owners = closure_module._stage1_portable_reuse_authority_batch_owners(
        reuse_records=reuse_records,
        reuse_payloads=payloads,
        registry=registry,  # type: ignore[arg-type]
    )
    return closure_module._verify_stage1_portable_reuse_authorities_batch(
        registry=registry,  # type: ignore[arg-type]
        owner_records=owners,
        external_registry_resolver=resolver,
        expected_registry_revision=expected_revision,
    )


def test_all_four_local_authorities_across_reuse_rows_use_one_fenced_batch(tmp_path) -> None:
    rows = [_portable_row(index, tmp_path) for index in range(2)]
    all_owners = [record for _reuse, _payload, owners in rows for record in owners.values()]
    registry = RegistryDouble(all_owners)
    resolver = object()

    owner_ids, failures = _batch(rows, registry, resolver=resolver, expected_revision=41)

    assert owner_ids == frozenset(record.artifact_id for record in all_owners)
    assert failures == {}
    assert len(registry.batch_calls) == 1
    batched, kwargs = registry.batch_calls[0]
    assert {record.artifact_id for record in batched} == set(owner_ids)
    assert len(batched) == len(owner_ids)
    assert kwargs == {
        "external_registry_resolver": resolver,
        "expected_snapshot_revision": 41,
    }


@pytest.mark.parametrize(
    ("label", "message"),
    (
        ("source", "missing dependency: referenced portable evidence"),
        ("source_provider_receipt_ledger", "artifact content hash changed"),
    ),
)
def test_per_owner_missing_or_hash_failure_keeps_existing_label_suffix(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    message: str,
) -> None:
    reuse_record, payload, owners = _portable_row(0, tmp_path)
    registry = RegistryDouble(list(owners.values()))
    owner_ids, _ = _batch([(reuse_record, payload, owners)], registry)
    failed_id = str(payload[next(row[3] for row in closure_module._STAGE1_PORTABLE_AUTHORITY_BATCH_SPECS if row[0] == label)])
    failure = RegistryError(message)
    registry.batch_result = {failed_id: failure}
    monkeypatch.setattr(
        ArtifactRegistry,
        "_verify_ready_artifact",
        staticmethod(lambda _record: pytest.fail("batched owner was redundantly reverified")),
    )

    results = {
        spec[0]: _authority_call(
            label=spec[0],
            reuse_payload=payload,
            reuse_record=reuse_record,
            registry=registry,
            batch_owner_ids=owner_ids,
            batch_failures=registry.batch_result,
        )
        for spec in closure_module._STAGE1_PORTABLE_AUTHORITY_BATCH_SPECS
    }

    assert results[label][2] == [
        f"provider_closure_reuse_{label}_untrusted:analyze:{message}"
    ]
    assert all(not outcome[2] for other_label, outcome in results.items() if other_label != label)
    assert registry.direct_calls == []


def test_registry_snapshot_race_fails_every_selected_portable_authority(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reuse_record, payload, owners = _portable_row(0, tmp_path)
    registry = RegistryDouble(list(owners.values()))
    registry.batch_error = RegistrySnapshotChanged("Registry snapshot changed during batch")
    owner_ids, failures = _batch([(reuse_record, payload, owners)], registry, expected_revision=41)

    assert owner_ids == frozenset(owners)
    assert set(failures) == set(owners)
    assert all(isinstance(error, RegistrySnapshotChanged) for error in failures.values())
    monkeypatch.setattr(
        ArtifactRegistry,
        "_verify_ready_artifact",
        staticmethod(lambda _record: pytest.fail("raced owner must not use stale direct verification")),
    )

    for label in _ORIGINAL_FIELDS:
        _record, _portable, issues = _authority_call(
            label=label,
            reuse_payload=payload,
            reuse_record=reuse_record,
            registry=registry,
            batch_owner_ids=owner_ids,
            batch_failures=failures,
        )
        assert issues == [
            f"provider_closure_reuse_{label}_untrusted:analyze:Registry snapshot changed during batch"
        ]


def test_mixed_external_authority_stays_on_resolver_path(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    reuse_record, payload, owners = _portable_row(0, tmp_path)
    local_source_id = str(payload["portable_source_artifact_id"])
    origin_id = "foreign:summary-source"
    origin_hash = _digest("foreign:summary-source")
    external_record = ArtifactRecord(
        artifact_id=origin_id,
        artifact_role="summary_file",
        artifact_type="summary_file",
        artifact_version="v1",
        path=str(tmp_path / "foreign-summary.json"),
        producer="foreign-test",
        job_id=_ORIGIN_JOB,
        status="ready",
        content_hash=origin_hash,
        depends_on=[],
        metadata={},
    )
    external_ref = ArtifactDependencyRefV2(
        dependency_kind="external_job",
        job_id=_ORIGIN_JOB,
        artifact_id=origin_id,
        artifact_type="summary_file",
        path=external_record.path,
        content_hash=origin_hash,
    )
    payload["source_authority_artifact_id"] = origin_id
    payload["source_authority_artifact_hash"] = origin_hash
    payload["portable_source_artifact_id"] = ""
    payload["portable_source_artifact_hash"] = ""
    local_reuse_record = replace(
        reuse_record,
        depends_on=[
            external_ref if item.artifact_id == local_source_id else item
            for item in reuse_record.depends_on
        ],
    )
    registry = RegistryDouble([*owners.values()])
    external_registry = ExternalRegistryDouble()
    resolver_calls: list[str] = []

    def resolver(job_id: str):
        resolver_calls.append(job_id)
        return external_registry if job_id == _ORIGIN_JOB else None

    owner_ids, _failures = _batch(
        [(local_reuse_record, payload, owners)],
        registry,
        resolver=resolver,
    )
    assert local_source_id not in owner_ids
    assert len(owner_ids) == 3
    monkeypatch.setattr(ArtifactRegistry, "_verify_ready_artifact", staticmethod(lambda _record: None))

    record, portable, issues = _authority_call(
        label="source",
        reuse_payload=payload,
        reuse_record=local_reuse_record,
        registry=registry,
        batch_owner_ids=owner_ids,
        resolver=resolver,
        extra_dependency_records={origin_id: external_record},
    )

    assert record == external_record
    assert portable is False
    assert issues == []
    assert external_registry.reload_calls == 1
    assert len(external_registry.direct_calls) == 1
    assert external_registry.direct_calls[0]["external_registry_resolver"] is resolver
    assert resolver_calls == [_ORIGIN_JOB]
    assert registry.direct_calls == []


def test_batched_success_matches_legacy_helper_contract_and_skips_only_io(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reuse_record, payload, owners = _portable_row(0, tmp_path)
    registry = RegistryDouble(list(owners.values()))
    candidate_ids, failures = _batch([(reuse_record, payload, owners)], registry)
    source_record = owners[str(payload["portable_source_artifact_id"])]
    verified_calls: list[str] = []
    monkeypatch.setattr(
        ArtifactRegistry,
        "_verify_ready_artifact",
        staticmethod(lambda record: verified_calls.append(record.artifact_id)),
    )

    legacy = _authority_call(
        label="source",
        reuse_payload=payload,
        reuse_record=reuse_record,
        registry=registry,
    )
    legacy_direct_calls = len(registry.direct_calls)
    legacy_verified_calls = len(verified_calls)
    verified_calls.clear()
    registry.direct_calls.clear()
    batched = _authority_call(
        label="source",
        reuse_payload=payload,
        reuse_record=reuse_record,
        registry=registry,
        batch_owner_ids=candidate_ids,
        batch_failures=failures,
    )

    assert legacy == batched == (source_record, True, [])
    assert legacy_direct_calls == 1
    assert legacy_verified_calls == 1
    assert verified_calls == []
    assert registry.direct_calls == []


def test_portable_hash_and_manifest_bindings_still_fail_closed_after_batch(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reuse_record, payload, owners = _portable_row(0, tmp_path)
    registry = RegistryDouble(list(owners.values()))
    _initial_owner_ids, _initial_failures = _batch([(reuse_record, payload, owners)], registry)
    payload["portable_source_artifact_hash"] = "0" * 64
    source_owner = owners[str(payload["portable_source_artifact_id"])]
    owner_ids, failures = _batch([(reuse_record, payload, owners)], registry)
    assert source_owner.artifact_id not in owner_ids
    monkeypatch.setattr(ArtifactRegistry, "_verify_ready_artifact", staticmethod(lambda _record: None))

    _record, _portable, issues = _authority_call(
        label="source",
        reuse_payload=payload,
        reuse_record=reuse_record,
        registry=registry,
        batch_owner_ids=owner_ids,
        batch_failures=failures,
    )
    assert "provider_closure_reuse_source_portable_hash_mismatch:analyze" in issues
    assert "provider_closure_reuse_source_binding_invalid:analyze" in issues
    # Source field checks are also independent of successful Registry batching.
    payload["portable_source_artifact_hash"] = source_owner.content_hash
    source_owner.metadata["typed_manifest_artifact_hash"] = "0" * 64
    owner_ids, failures = _batch([(reuse_record, payload, owners)], registry)
    assert source_owner.artifact_id in owner_ids
    _record, _portable, manifest_issues = _authority_call(
        label="source",
        reuse_payload=payload,
        reuse_record=reuse_record,
        registry=registry,
        batch_owner_ids=owner_ids,
        batch_failures=failures,
    )
    assert "provider_closure_reuse_source_portable_metadata_invalid:analyze" in manifest_issues

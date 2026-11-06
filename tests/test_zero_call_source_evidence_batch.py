from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from runtime.provider_runtime import hash_json
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
)
from tests.test_stage_specific_zero_call_evidence import _build_fixture, _register_json
import validation.closure as closure_module
from validation.closure import (
    _provider_closure_entry,
    _zero_call_source_evidence_issues,
    zero_call_evidence_policy,
)


# These tests exercise only the batched direct-dependency check for zero-call
# evidence. They create READY, schema-valid typed-reuse records in temporary
# Registries; they do not claim full Stage 1 authority or run providers.

def _stage1_reuse_owner_payload(
    registry: ArtifactRegistry,
    *,
    paper_key: str,
    source_summary: ArtifactRecord,
    source_manifest: ArtifactRecord,
    runtime_spec: ArtifactRecord,
) -> dict[str, Any]:
    summary_payload = {"canonical_paper_key": paper_key, "summary": f"fixture summary {paper_key}"}
    return {
        "artifact_type": "stage1_summary_reuse_record",
        "artifact_version": "v1",
        "job_id": registry.job_id,
        "stage_name": "stage1_analyze",
        "attempt_id": "attempt-reuse-fixture",
        "reused_summary_artifact_id": source_summary.artifact_id,
        "reused_summary_artifact_hash": source_summary.content_hash,
        "summary_payload_hash": hash_json(summary_payload),
        "registry_file_hash": source_summary.content_hash,
        "registered_source_artifact_id": "",
        "registered_source_artifact_hash": "",
        "source_authority_artifact_id": source_summary.artifact_id,
        "source_authority_artifact_hash": source_summary.content_hash,
        "source_authority_job_id": registry.job_id,
        "source_summary_manifest_id": source_manifest.artifact_id,
        "source_summary_manifest_hash": source_manifest.content_hash,
        "source_bundle_paper_identity": {"canonical_paper_key": paper_key},
        "input_manifest_hashes": {source_manifest.artifact_id: source_manifest.content_hash},
        "original_provider_receipt_ids": [],
        "current_runtime_spec_id": runtime_spec.artifact_id,
        "current_runtime_spec_hash": runtime_spec.content_hash,
        "current_evidence_manifest_id": source_manifest.artifact_id,
        "current_evidence_manifest_hash": source_manifest.content_hash,
        "reuse_policy": "exact_summary_reuse_v1",
        "reuse_decision_reason": "batch verification test fixture",
        "created_at": "2026-10-21T00:00:00Z",
        "source_authority_kind": "typed_manifest",
        "content_hash": "",
    }


def _add_reuse_owner(
    registry: ArtifactRegistry,
    root: Path,
    *,
    paper_index: int,
    external_dependency: ArtifactDependencyRefV2 | None = None,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> ArtifactRecord:
    source_bundle = registry.get("source_bundle")
    source_manifest = registry.get("summary_source_manifest")
    runtime_spec = registry.get("runtime_job_spec")
    assert source_bundle is not None and source_manifest is not None and runtime_spec is not None

    paper_key = f"batch-paper-{paper_index:03d}"
    summary_payload = {
        "artifact_type": "stage1_canonical_summaries",
        "artifact_version": "v1",
        "job_id": registry.job_id,
        "summaries": [{"canonical_paper_key": paper_key, "summary": f"fixture summary {paper_key}"}],
    }
    summary_record = _register_json(
        registry,
        root,
        artifact_id=f"stage1-summary:{paper_key}",
        artifact_type="stage1_canonical_summaries",
        artifact_version="v1",
        payload=summary_payload,
        depends_on=(ArtifactDependencyRefV2.from_record(source_bundle),),
    )
    reuse_payload = _stage1_reuse_owner_payload(
        registry,
        paper_key=paper_key,
        source_summary=summary_record,
        source_manifest=source_manifest,
        runtime_spec=runtime_spec,
    )
    reuse_payload["content_hash"] = hash_json(
        {key: value for key, value in reuse_payload.items() if key != "content_hash"}
    )
    path = root / f"stage1-reuse-{paper_index:03d}.json"
    path.write_text(json.dumps(reuse_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    dependencies = [
        ArtifactDependencyRefV2.from_record(source_bundle),
        ArtifactDependencyRefV2.from_record(source_manifest),
        ArtifactDependencyRefV2.from_record(summary_record),
    ]
    if external_dependency is not None:
        dependencies.append(external_dependency)
    return registry.register_file(
        artifact_id=f"stage1:summary_reuse:{paper_key}",
        artifact_role="stage1_summary_reuse_record",
        artifact_type="stage1_summary_reuse_record",
        artifact_version="v1",
        path=path,
        producer="tests.zero_call_source_evidence_batch",
        depends_on=dependencies,
        external_registry_resolver=external_registry_resolver,
    )


def _case_with_reuse_owners(
    tmp_path: Path,
    *,
    owner_count: int = 3,
    external_dependency: ArtifactDependencyRefV2 | None = None,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> tuple[Any, ArtifactRecord, list[ArtifactRecord], int]:
    fixture = _build_fixture(tmp_path, stage="analyze")
    registry = fixture.registry
    owner_root = tmp_path / "typed-reuse-owners"
    owner_root.mkdir(parents=True, exist_ok=True)
    owners = [
        _add_reuse_owner(
            registry,
            owner_root,
            paper_index=index,
            external_dependency=external_dependency,
            external_registry_resolver=external_registry_resolver,
        )
        for index in range(owner_count)
    ]
    closure_record = registry.get(fixture.closure_record.artifact_id)
    assert closure_record is not None
    dependencies = [
        *closure_record.depends_on,
        *(ArtifactDependencyRefV2.from_record(owner) for owner in owners),
    ]
    updated_closure = registry.register_file(
        artifact_id=closure_record.artifact_id,
        artifact_role=closure_record.artifact_role,
        artifact_type=closure_record.artifact_type,
        artifact_version=closure_record.artifact_version,
        path=closure_record.path,
        producer=closure_record.producer,
        status=closure_record.status,
        depends_on=dependencies,
        metadata=closure_record.metadata,
        expected_revision=registry.revision,
    )
    revision, _records = registry.capture_record_snapshot()
    return fixture, updated_closure, owners, revision


def _closure_epoch(record: ArtifactRecord) -> str:
    payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
    return str(payload["closure_epoch_id"])


def _run_zero_call_check(
    fixture: Any,
    closure_record: ArtifactRecord,
    *,
    expected_registry_revision: int | None,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> list[str]:
    return _zero_call_source_evidence_issues(
        stage="analyze",
        closure_epoch_id=_closure_epoch(closure_record),
        closure_record=closure_record,
        registry=fixture.registry,
        current_set=fixture.current_set,
        external_registry_resolver=external_registry_resolver,
        expected_registry_revision=expected_registry_revision,
    )


def _analyze_evidence_owner_ids(closure_record: ArtifactRecord) -> set[str]:
    allowed = set(zero_call_evidence_policy("analyze"))
    return {dep.artifact_id for dep in closure_record.depends_on if dep.artifact_type in allowed}


def test_analyze_typed_reuse_evidence_uses_one_exact_fenced_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, closure_record, reuse_owners, revision = _case_with_reuse_owners(tmp_path)
    registry = fixture.registry
    expected_owner_ids = _analyze_evidence_owner_ids(closure_record)
    assert expected_owner_ids == {fixture.evidence_record.artifact_id, *(r.artifact_id for r in reuse_owners)}

    batch_calls: list[tuple[tuple[ArtifactRecord, ...], dict[str, Any]]] = []
    direct_calls: list[dict[str, Any]] = []
    original_batch = registry.verify_ready_artifact_dependency_sets
    original_direct = registry.verify_ready_dependencies

    def batch_spy(owners: Any, **kwargs: Any) -> dict[str, Exception]:
        owner_tuple = tuple(owners)
        batch_calls.append((owner_tuple, dict(kwargs)))
        return original_batch(owner_tuple, **kwargs)

    def direct_spy(dependencies: Any, **kwargs: Any) -> Any:
        direct_calls.append(dict(kwargs))
        return original_direct(dependencies, **kwargs)

    monkeypatch.setattr(registry, "verify_ready_artifact_dependency_sets", batch_spy)
    monkeypatch.setattr(registry, "verify_ready_dependencies", direct_spy)

    issues = _run_zero_call_check(
        fixture,
        closure_record,
        expected_registry_revision=revision,
    )

    assert issues == []
    assert len(batch_calls) == 1
    batched_owners, kwargs = batch_calls[0]
    assert {record.artifact_id for record in batched_owners} == expected_owner_ids
    assert len(batched_owners) == len(expected_owner_ids)
    assert kwargs["expected_snapshot_revision"] == revision
    assert kwargs["external_registry_resolver"] is None
    assert direct_calls == []


def test_analyze_typed_reuse_batch_maps_one_invalid_owner_only(
    tmp_path: Path,
) -> None:
    fixture, closure_record, reuse_owners, revision = _case_with_reuse_owners(tmp_path)
    bad_owner = reuse_owners[0]
    Path(bad_owner.path).write_text("tampered after READY registration\n", encoding="utf-8")

    issues = _run_zero_call_check(
        fixture,
        closure_record,
        expected_registry_revision=revision,
    )

    prefix = "provider_closure_zero_call_source_evidence_untrusted:analyze:"
    untrusted = [issue for issue in issues if issue.startswith(prefix)]
    assert len(untrusted) == 1, issues
    assert untrusted[0].startswith(f"{prefix}{bad_owner.artifact_id}:")
    assert not any(
        issue.startswith(f"{prefix}{owner.artifact_id}:")
        for owner in reuse_owners[1:]
        for issue in untrusted
    )
    assert not any(issue.startswith(f"{prefix}{fixture.evidence_record.artifact_id}:") for issue in issues)


def test_analyze_typed_reuse_batch_snapshot_fence_blocks_all_selected_owners(
    tmp_path: Path,
) -> None:
    fixture, closure_record, reuse_owners, stale_revision = _case_with_reuse_owners(tmp_path)
    marker_path = tmp_path / "quarantined-race-marker.json"
    marker_path.write_text("{}\n", encoding="utf-8")
    fixture.registry.register_file(
        artifact_id="test:registry-race-marker",
        artifact_role="test_marker",
        artifact_type="test_marker",
        artifact_version="v1",
        path=marker_path,
        producer="tests.zero_call_source_evidence_batch",
        status="quarantined",
    )

    issues = _run_zero_call_check(
        fixture,
        closure_record,
        expected_registry_revision=stale_revision,
    )

    expected_ids = _analyze_evidence_owner_ids(closure_record)
    prefix = "provider_closure_zero_call_source_evidence_untrusted:analyze:"
    untrusted = [issue for issue in issues if issue.startswith(prefix)]
    assert {next((artifact_id for artifact_id in expected_ids if issue.startswith(f"{prefix}{artifact_id}:")), None) for issue in untrusted} == expected_ids
    assert len(untrusted) == len(expected_ids)
    assert all("snapshot changed" in issue.lower() for issue in untrusted)


def test_analyze_typed_reuse_batch_preserves_external_resolver_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    foreign_registry = ArtifactRegistry(foreign_root / "artifact_registry.json", "foreign-stage1-job")
    foreign_record = _register_json(
        foreign_registry,
        foreign_root,
        artifact_id="foreign:typed-reuse-authority",
        artifact_type="test_dependency",
        artifact_version="v1",
        payload={"job_id": foreign_registry.job_id, "artifact_id": "foreign:typed-reuse-authority"},
    )
    resolver_calls: list[str] = []
    def resolver(job_id: str) -> ArtifactRegistry | None:
        resolver_calls.append(job_id)
        return foreign_registry if job_id == foreign_registry.job_id else None
    external_ref = ArtifactDependencyRefV2.from_record(foreign_record, dependency_kind="external_job")
    fixture, closure_record, _reuse_owners, revision = _case_with_reuse_owners(
        tmp_path,
        external_dependency=external_ref,
        external_registry_resolver=resolver,
    )
    # Ignore resolver use during owner registration; measure the closure check.
    resolver_calls.clear()
    batch_calls: list[dict[str, Any]] = []
    original_batch = fixture.registry.verify_ready_artifact_dependency_sets
    def batch_spy(owners: Any, **kwargs: Any) -> dict[str, Exception]:
        batch_calls.append(dict(kwargs))
        return original_batch(tuple(owners), **kwargs)
    monkeypatch.setattr(fixture.registry, "verify_ready_artifact_dependency_sets", batch_spy)

    issues = _run_zero_call_check(
        fixture,
        closure_record,
        expected_registry_revision=revision,
        external_registry_resolver=resolver,
    )

    assert issues == []
    assert len(batch_calls) == 1
    assert batch_calls[0]["external_registry_resolver"] is resolver
    assert batch_calls[0]["expected_snapshot_revision"] == revision
    assert resolver_calls and set(resolver_calls) == {foreign_registry.job_id}


def test_non_analyze_zero_call_evidence_keeps_existing_per_owner_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _build_fixture(tmp_path, stage="outline")
    registry = fixture.registry
    direct_calls: list[dict[str, Any]] = []
    def unexpected_batch(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("non-analyze zero-call evidence must keep the existing direct path")
    original_direct = registry.verify_ready_dependencies
    def direct_spy(dependencies: Any, **kwargs: Any) -> Any:
        direct_calls.append(dict(kwargs))
        return original_direct(dependencies, **kwargs)
    monkeypatch.setattr(registry, "verify_ready_artifact_dependency_sets", unexpected_batch)
    monkeypatch.setattr(registry, "verify_ready_dependencies", direct_spy)
    resolver = lambda _job_id: None

    issues = _zero_call_source_evidence_issues(
        stage="outline",
        closure_epoch_id=_closure_epoch(fixture.closure_record),
        closure_record=fixture.closure_record,
        registry=registry,
        current_set=fixture.current_set,
        external_registry_resolver=resolver,
        expected_registry_revision=registry.capture_record_snapshot()[0],
    )

    assert issues == []
    assert len(direct_calls) == 1
    assert direct_calls[0]["external_registry_resolver"] is resolver


def test_provider_closure_forwards_revision_to_zero_call_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _build_fixture(tmp_path, stage="analyze")
    revision, _records = fixture.registry.capture_record_snapshot()
    seen_revisions: list[int | None] = []
    original_helper = closure_module._zero_call_source_evidence_issues
    def helper_spy(**kwargs: Any) -> list[str]:
        seen_revisions.append(kwargs.get("expected_registry_revision"))
        return original_helper(**kwargs)
    monkeypatch.setattr(closure_module, "_zero_call_source_evidence_issues", helper_spy)

    entry, issues = _provider_closure_entry(
        fixture.stage,
        fixture.closure_record,
        fixture.terminal_record,
        fixture.terminal_payload,
        fixture.registry,
        current_set=fixture.current_set,
        expected_registry_revision=revision,
    )

    assert issues == [], entry
    assert entry["complete"] is True
    assert seen_revisions == [revision]

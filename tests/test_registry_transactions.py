from __future__ import annotations

import json
import multiprocessing
import threading
from pathlib import Path
from queue import Empty
from typing import Any

import pytest

from services import artifact_registry as artifact_registry_module
from services.artifact_registry import (
    ArtifactConflict,
    ArtifactDependencyRefV2,
    ArtifactRegistry,
    RegistryCorruption,
    UnverifiedArtifact,
    UnverifiedDependency,
    RegistryLockTimeout,
    RegistryRevisionConflict,
)


def _write_artifact(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"value": value}), encoding="utf-8")
    return path


def _concurrent_register_worker(
    registry_path: str,
    artifact_path: str,
    artifact_id: str,
    ready_queue: Any,
    result_queue: Any,
    start_event: Any,
) -> None:
    try:
        registry = ArtifactRegistry(registry_path, "job-concurrent")
        ready_queue.put((artifact_id, registry.revision))
        if not start_event.wait(timeout=10):
            raise TimeoutError("parent did not release concurrent registry workers")
        registry.register_file(
            artifact_id=artifact_id,
            artifact_role="test",
            artifact_type="test_artifact",
            artifact_version="v1",
            path=artifact_path,
            producer="tests.concurrent-worker",
        )
        result_queue.put((artifact_id, "ok"))
    except BaseException as exc:  # pragma: no cover - only used to report subprocess failure
        result_queue.put((artifact_id, f"{type(exc).__name__}: {exc}"))


def test_v1_registry_is_read_additively_and_next_write_emits_v2(tmp_path: Path) -> None:
    legacy_path = _write_artifact(tmp_path / "legacy.json", "legacy")
    registry_path = tmp_path / "artifact_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "artifact_registry_version": "v1",
                "job_id": "job-legacy",
                "artifacts": [
                    {
                        "artifact_id": "legacy-artifact",
                        "artifact_role": "summary",
                        "artifact_type": "summary_file",
                        "artifact_version": "v1",
                        "path": str(legacy_path),
                        "producer": "legacy",
                        "job_id": "job-legacy",
                        "status": "ready",
                        "content_hash": "legacy-hash",
                        "depends_on": [
                            {
                                "artifact_type": "source_pdf",
                                "path": str(tmp_path / "source.pdf"),
                                "content_hash": "source-hash",
                            }
                        ],
                        "created_at": "2026-07-13T00:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    legacy_bytes = registry_path.read_bytes()

    registry = ArtifactRegistry(registry_path, "job-legacy")

    assert registry_path.read_bytes() == legacy_bytes

    legacy = registry.get("legacy-artifact")

    assert registry.revision == 0
    assert legacy is not None
    assert legacy.depends_on[0].dependency_kind == "local_job"
    assert legacy.depends_on[0].job_id == "job-legacy"
    assert legacy.depends_on[0].artifact_id == "source_pdf:source.pdf"

    new_path = _write_artifact(tmp_path / "new.json", "new")
    registry.register_file(
        artifact_id="new-artifact",
        artifact_role="summary",
        artifact_type="summary_file",
        artifact_version="v2",
        path=new_path,
        producer="tests",
    )

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    assert payload["artifact_registry_version"] == "v2"
    assert payload["revision"] == 1
    assert {item["artifact_id"] for item in payload["artifacts"]} == {
        "legacy-artifact",
        "new-artifact",
    }
    assert set(payload["artifacts"][0]["depends_on"][0]) == {
        "dependency_kind",
        "job_id",
        "artifact_id",
        "artifact_type",
        "path",
        "content_hash",
    }


@pytest.mark.parametrize(
    ("persisted_version", "persisted_job_id", "expected_error"),
    [
        ("v99", "job-owned", "unsupported artifact_registry_version"),
        ("v2", "job-foreign", "does not match expected owner"),
    ],
)
def test_registry_header_mismatch_fails_closed_without_side_effects(
    tmp_path: Path,
    persisted_version: str,
    persisted_job_id: str,
    expected_error: str,
) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    original_bytes = json.dumps(
        {
            "artifact_registry_version": persisted_version,
            "revision": 0,
            "job_id": persisted_job_id,
            "artifacts": [],
        },
        indent=2,
    ).encode("utf-8")
    registry_path.write_bytes(original_bytes)
    before_entries = sorted(path.name for path in tmp_path.iterdir())

    with pytest.raises(RegistryCorruption, match=expected_error):
        ArtifactRegistry(registry_path, "job-owned")

    assert registry_path.read_bytes() == original_bytes
    assert sorted(path.name for path in tmp_path.iterdir()) == before_entries


def test_registry_rejects_persisted_artifact_owned_by_another_job(tmp_path: Path) -> None:
    artifact_path = _write_artifact(tmp_path / "foreign.json", "foreign")
    registry_path = tmp_path / "artifact_registry.json"
    payload = {
        "artifact_registry_version": "v2",
        "revision": 1,
        "job_id": "job-owned",
        "artifacts": [
            {
                "artifact_id": "foreign-artifact",
                "artifact_role": "summary",
                "artifact_type": "summary_file",
                "artifact_version": "v1",
                "path": str(artifact_path),
                "producer": "tests",
                "job_id": "job-foreign",
                "status": "ready",
                "content_hash": artifact_registry_module.file_sha256(artifact_path),
                "depends_on": [],
                "metadata": {},
                "created_at": "2026-07-14T00:00:00+00:00",
            }
        ],
    }
    original_bytes = json.dumps(payload, indent=2).encode("utf-8")
    registry_path.write_bytes(original_bytes)

    with pytest.raises(RegistryCorruption, match="does not match registry owner"):
        ArtifactRegistry(registry_path, "job-owned")

    assert registry_path.read_bytes() == original_bytes


def test_registry_rejects_registering_artifact_for_another_job(tmp_path: Path) -> None:
    artifact_path = _write_artifact(tmp_path / "foreign.json", "foreign")
    registry_path = tmp_path / "artifact_registry.json"
    registry = ArtifactRegistry(registry_path, "job-owned")

    with pytest.raises(ArtifactConflict, match="not registry owner"):
        registry.register(
            artifact_id="foreign-artifact",
            artifact_type="summary_file",
            artifact_version="v1",
            path=artifact_path,
            producer="tests",
            job_id="job-foreign",
        )

    assert registry.revision == 0
    assert registry.list_records() == []
    assert not registry_path.exists()


def test_dependency_v2_round_trip_preserves_external_identity(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    artifact_path = _write_artifact(tmp_path / "child.json", "child")
    parent_registry_path = tmp_path / "parent" / "artifact_registry.json"
    parent_path = _write_artifact(tmp_path / "parent" / "summary.json", "parent")
    parent_registry = ArtifactRegistry(parent_registry_path, "parent-job")
    parent_record = parent_registry.register_file(
        artifact_id="parent-summary",
        artifact_role="summary",
        artifact_type="summary_file",
        artifact_version="v1",
        path=parent_path,
        producer="tests",
    )
    dependency = ArtifactDependencyRefV2(
        dependency_kind="external_job",
        job_id="parent-job",
        artifact_id="parent-summary",
        artifact_type="summary_file",
        path=parent_record.path,
        content_hash=parent_record.content_hash,
    )

    ArtifactRegistry(registry_path, "child-job").register_file(
        artifact_id="child-review",
        artifact_role="review",
        artifact_type="review_draft",
        artifact_version="v1",
        path=artifact_path,
        producer="tests",
        depends_on=[dependency],
        external_registry_resolver=lambda job_id: parent_registry if job_id == "parent-job" else None,
        metadata={"identity_verdict": "match", "canonical_ready": True},
    )

    loaded = ArtifactRegistry(registry_path, "child-job").get("child-review")
    assert loaded is not None
    assert loaded.depends_on == [dependency]
    assert loaded.metadata == {"identity_verdict": "match", "canonical_ready": True}


def test_ready_registration_rejects_external_dependency_without_resolver(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    artifact_path = _write_artifact(tmp_path / "child.json", "child")
    dependency = ArtifactDependencyRefV2(
        dependency_kind="external_job",
        job_id="parent-job",
        artifact_id="parent-summary",
        artifact_type="summary_file",
        path=str(tmp_path / "parent" / "summary.json"),
        content_hash="a" * 64,
    )

    with pytest.raises(UnverifiedDependency, match="external dependency cannot be verified"):
        ArtifactRegistry(registry_path, "child-job").register_file(
            artifact_id="child-review",
            artifact_role="review",
            artifact_type="review_draft",
            artifact_version="v1",
            path=artifact_path,
            producer="tests",
            depends_on=[dependency],
        )

    assert not registry_path.exists()


def test_ready_registration_rejects_external_dependency_when_parent_is_not_ready(
    tmp_path: Path,
) -> None:
    parent_registry = ArtifactRegistry(tmp_path / "parent" / "artifact_registry.json", "parent-job")
    parent_path = _write_artifact(tmp_path / "parent" / "summary.json", "parent")
    parent_record = parent_registry.register_file(
        artifact_id="parent-summary",
        artifact_role="summary",
        artifact_type="summary_file",
        artifact_version="v1",
        path=parent_path,
        producer="tests",
        status="quarantined",
    )
    child_registry_path = tmp_path / "child" / "artifact_registry.json"
    child_path = _write_artifact(tmp_path / "child" / "review.json", "child")

    with pytest.raises(UnverifiedDependency, match="dependency is not ready"):
        ArtifactRegistry(child_registry_path, "child-job").register_file(
            artifact_id="child-review",
            artifact_role="review",
            artifact_type="review_draft",
            artifact_version="v1",
            path=child_path,
            producer="tests",
            depends_on=[
                ArtifactDependencyRefV2(
                    dependency_kind="external_job",
                    job_id=parent_record.job_id,
                    artifact_id=parent_record.artifact_id,
                    artifact_type=parent_record.artifact_type,
                    path=parent_record.path,
                    content_hash=parent_record.content_hash,
                )
            ],
            external_registry_resolver=lambda job_id: (
                parent_registry if job_id == parent_registry.job_id else None
            ),
        )

    assert not child_registry_path.exists()


def test_ready_registration_rejects_external_dependency_after_parent_hash_changes(
    tmp_path: Path,
) -> None:
    parent_registry = ArtifactRegistry(tmp_path / "parent" / "artifact_registry.json", "parent-job")
    parent_path = _write_artifact(tmp_path / "parent" / "summary.json", "parent")
    parent_record = parent_registry.register_file(
        artifact_id="parent-summary",
        artifact_role="summary",
        artifact_type="summary_file",
        artifact_version="v1",
        path=parent_path,
        producer="tests",
    )
    dependency = ArtifactDependencyRefV2(
        dependency_kind="external_job",
        job_id=parent_record.job_id,
        artifact_id=parent_record.artifact_id,
        artifact_type=parent_record.artifact_type,
        path=parent_record.path,
        content_hash=parent_record.content_hash,
    )
    parent_path.write_text("tampered", encoding="utf-8")
    child_registry_path = tmp_path / "child" / "artifact_registry.json"
    child_path = _write_artifact(tmp_path / "child" / "review.json", "child")

    with pytest.raises(UnverifiedDependency, match="dependency content hash changed"):
        ArtifactRegistry(child_registry_path, "child-job").register_file(
            artifact_id="child-review",
            artifact_role="review",
            artifact_type="review_draft",
            artifact_version="v1",
            path=child_path,
            producer="tests",
            depends_on=[dependency],
            external_registry_resolver=lambda job_id: (
                parent_registry if job_id == parent_registry.job_id else None
            ),
        )

    assert not child_registry_path.exists()


def test_quarantined_external_dependency_is_reverified_before_ready_transition(
    tmp_path: Path,
) -> None:
    parent_registry = ArtifactRegistry(tmp_path / "parent" / "artifact_registry.json", "parent-job")
    parent_path = _write_artifact(tmp_path / "parent" / "summary.json", "parent")
    parent_record = parent_registry.register_file(
        artifact_id="parent-summary",
        artifact_role="summary",
        artifact_type="summary_file",
        artifact_version="v1",
        path=parent_path,
        producer="tests",
    )
    dependency = ArtifactDependencyRefV2(
        dependency_kind="external_job",
        job_id=parent_record.job_id,
        artifact_id=parent_record.artifact_id,
        artifact_type=parent_record.artifact_type,
        path=parent_record.path,
        content_hash=parent_record.content_hash,
    )
    child_registry = ArtifactRegistry(tmp_path / "child" / "artifact_registry.json", "child-job")
    child_path = _write_artifact(tmp_path / "child" / "review.json", "child")
    child_registry.register_file(
        artifact_id="child-review",
        artifact_role="review",
        artifact_type="review_draft",
        artifact_version="v1",
        path=child_path,
        producer="tests",
        status="quarantined",
        depends_on=[dependency],
    )
    quarantined_revision = child_registry.revision

    with pytest.raises(UnverifiedDependency, match="external dependency cannot be verified"):
        child_registry.update_record("child-review", status="ready")

    assert child_registry.revision == quarantined_revision
    assert child_registry.get("child-review").status == "quarantined"  # type: ignore[union-attr]

    ready = child_registry.update_record(
        "child-review",
        status="ready",
        external_registry_resolver=lambda job_id: (
            parent_registry if job_id == parent_registry.job_id else None
        ),
    )

    assert ready.status == "ready"
    assert ready.depends_on == [dependency]


def test_ready_registration_requires_an_existing_file(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    registry = ArtifactRegistry(registry_path, "job-missing")

    with pytest.raises(FileNotFoundError, match="ready artifact does not exist"):
        registry.register_file(
            artifact_id="missing",
            artifact_role="test",
            artifact_type="test",
            artifact_version="v1",
            path=tmp_path / "missing.json",
            producer="tests",
        )

    assert registry.revision == 0
    assert registry.get("missing") is None
    assert not registry_path.exists()


def test_corrupt_registry_raises_instead_of_falling_back_to_empty(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    registry_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(RegistryCorruption, match="cannot read registry"):
        ArtifactRegistry(registry_path, "job-corrupt")


def test_two_stale_instances_merge_without_lost_update(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    first_path = _write_artifact(tmp_path / "first.json", "first")
    second_path = _write_artifact(tmp_path / "second.json", "second")
    first = ArtifactRegistry(registry_path, "job-stale")
    second = ArtifactRegistry(registry_path, "job-stale")

    first.register_file(
        artifact_id="first",
        artifact_role="test",
        artifact_type="test",
        artifact_version="v1",
        path=first_path,
        producer="tests",
    )
    second.register_file(
        artifact_id="second",
        artifact_role="test",
        artifact_type="test",
        artifact_version="v1",
        path=second_path,
        producer="tests",
    )

    reloaded = ArtifactRegistry(registry_path, "job-stale")
    assert reloaded.revision == 2
    assert {record.artifact_id for record in reloaded.list_records()} == {"first", "second"}
    assert {record.artifact_id for record in second.list_records()} == {"first", "second"}


def test_mutable_head_registration_updates_same_artifact_identity(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    artifact_path = _write_artifact(tmp_path / "job_outcome_v1.json", "running")
    registry = ArtifactRegistry(registry_path, "job-head")
    first = registry.register_file(
        artifact_id="job_outcome",
        artifact_role="job_outcome",
        artifact_type="job_outcome",
        artifact_version="v1",
        path=artifact_path,
        producer="runtime.lifecycle",
    )

    _write_artifact(artifact_path, "completed")
    second = registry.register_file(
        artifact_id="job_outcome",
        artifact_role="job_outcome",
        artifact_type="job_outcome",
        artifact_version="v1",
        path=artifact_path,
        producer="runtime.lifecycle",
    )
    reloaded = ArtifactRegistry(registry_path, "job-head")

    assert second.content_hash != first.content_hash
    assert reloaded.get("job_outcome") == second
    assert len(reloaded.list_records()) == 1


def test_explicit_expected_revision_is_compare_and_swap(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    first_path = _write_artifact(tmp_path / "first.json", "first")
    second_path = _write_artifact(tmp_path / "second.json", "second")
    first = ArtifactRegistry(registry_path, "job-cas")
    stale = ArtifactRegistry(registry_path, "job-cas")

    first.register_file(
        artifact_id="first",
        artifact_role="test",
        artifact_type="test",
        artifact_version="v1",
        path=first_path,
        producer="tests",
        expected_revision=0,
    )

    with pytest.raises(RegistryRevisionConflict, match="expected registry revision 0, found 1"):
        stale.register_file(
            artifact_id="second",
            artifact_role="test",
            artifact_type="test",
            artifact_version="v1",
            path=second_path,
            producer="tests",
            expected_revision=0,
        )

    assert ArtifactRegistry(registry_path, "job-cas").get("second") is None


def test_stale_compatibility_save_cannot_overwrite_newer_registry(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    artifact_path = _write_artifact(tmp_path / "artifact.json", "artifact")
    current = ArtifactRegistry(registry_path, "job-save-cas")
    stale = ArtifactRegistry(registry_path, "job-save-cas")
    current.register_file(
        artifact_id="artifact",
        artifact_role="test",
        artifact_type="test",
        artifact_version="v1",
        path=artifact_path,
        producer="tests",
    )

    with pytest.raises(RegistryRevisionConflict, match="expected registry revision 0, found 1"):
        stale.save()

    reloaded = ArtifactRegistry(registry_path, "job-save-cas")
    assert reloaded.revision == 1
    assert reloaded.get("artifact") is not None


def test_record_accessors_return_defensive_copies_before_save(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    parent_path = _write_artifact(tmp_path / "parent.json", "parent")
    child_path = _write_artifact(tmp_path / "child.json", "child")
    registry = ArtifactRegistry(registry_path, "job-defensive-copy")
    parent = registry.register_file(
        artifact_id="parent",
        artifact_role="source",
        artifact_type="source",
        artifact_version="v1",
        path=parent_path,
        producer="tests",
    )
    registry.register_file(
        artifact_id="child",
        artifact_role="summary",
        artifact_type="summary",
        artifact_version="v1",
        path=child_path,
        producer="tests",
        depends_on=[
            ArtifactDependencyRefV2(
                job_id=parent.job_id,
                artifact_id=parent.artifact_id,
                artifact_type=parent.artifact_type,
                path=parent.path,
                content_hash=parent.content_hash,
            )
        ],
        metadata={"nested": {"state": "original"}},
    )

    exposed_by_get = registry.get("child")
    assert exposed_by_get is not None
    exposed_by_get.depends_on.clear()
    exposed_by_get.metadata["nested"]["state"] = "mutated-by-get"
    exposed_by_list = next(record for record in registry.list_records() if record.artifact_id == "child")
    exposed_by_list.metadata["nested"]["state"] = "mutated-by-list"
    registry.save()

    reloaded = ArtifactRegistry(registry_path, "job-defensive-copy").get("child")
    assert reloaded is not None
    assert [dependency.artifact_id for dependency in reloaded.depends_on] == ["parent"]
    assert reloaded.metadata == {"nested": {"state": "original"}}


def test_save_revalidates_ready_artifact_and_local_dependency_hashes(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    parent_path = _write_artifact(tmp_path / "parent.json", "parent")
    child_path = _write_artifact(tmp_path / "child.json", "child")
    registry = ArtifactRegistry(registry_path, "job-save-verify")
    parent = registry.register_file(
        artifact_id="parent",
        artifact_role="source",
        artifact_type="source",
        artifact_version="v1",
        path=parent_path,
        producer="tests",
    )
    registry.register_file(
        artifact_id="child",
        artifact_role="summary",
        artifact_type="summary",
        artifact_version="v1",
        path=child_path,
        producer="tests",
        depends_on=[
            ArtifactDependencyRefV2(
                job_id=parent.job_id,
                artifact_id=parent.artifact_id,
                artifact_type=parent.artifact_type,
                path=parent.path,
                content_hash=parent.content_hash,
            )
        ],
    )
    ready_revision = registry.revision
    child_path.write_text("tampered-child", encoding="utf-8")

    with pytest.raises(UnverifiedArtifact, match="artifact content hash changed: child"):
        registry.save()

    assert registry.revision == ready_revision
    child_path.write_text(json.dumps({"value": "child"}), encoding="utf-8")
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    child_payload = next(item for item in payload["artifacts"] if item["artifact_id"] == "child")
    child_payload["depends_on"][0]["content_hash"] = "0" * 64
    registry_path.write_text(json.dumps(payload), encoding="utf-8")
    registry = ArtifactRegistry(registry_path, "job-save-verify")

    with pytest.raises(UnverifiedDependency, match="dependency declared hash mismatch: parent"):
        registry.save()

    assert registry.revision == ready_revision


def test_save_requires_resolver_for_ready_external_dependency(tmp_path: Path) -> None:
    parent_registry = ArtifactRegistry(tmp_path / "parent" / "artifact_registry.json", "parent-job")
    parent_path = _write_artifact(tmp_path / "parent" / "parent.json", "parent")
    parent = parent_registry.register_file(
        artifact_id="parent",
        artifact_role="source",
        artifact_type="source",
        artifact_version="v1",
        path=parent_path,
        producer="tests",
    )
    child_registry = ArtifactRegistry(tmp_path / "child" / "artifact_registry.json", "child-job")
    child_path = _write_artifact(tmp_path / "child" / "child.json", "child")
    child_registry.register_file(
        artifact_id="child",
        artifact_role="summary",
        artifact_type="summary",
        artifact_version="v1",
        path=child_path,
        producer="tests",
        depends_on=[
            ArtifactDependencyRefV2(
                dependency_kind="external_job",
                job_id=parent.job_id,
                artifact_id=parent.artifact_id,
                artifact_type=parent.artifact_type,
                path=parent.path,
                content_hash=parent.content_hash,
            )
        ],
        external_registry_resolver=lambda job_id: (
            parent_registry if job_id == parent_registry.job_id else None
        ),
    )
    ready_revision = child_registry.revision

    with pytest.raises(UnverifiedDependency, match="external dependency cannot be verified"):
        child_registry.save()

    assert child_registry.revision == ready_revision
    child_registry.save(
        external_registry_resolver=lambda job_id: (
            parent_registry if job_id == parent_registry.job_id else None
        )
    )
    assert child_registry.revision == ready_revision + 1


def test_artifact_identity_conflict_does_not_modify_durable_state(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    first_path = _write_artifact(tmp_path / "first.json", "first")
    second_path = _write_artifact(tmp_path / "second.json", "second")
    registry = ArtifactRegistry(registry_path, "job-conflict")
    registry.register_file(
        artifact_id="stable-id",
        artifact_role="test",
        artifact_type="type-a",
        artifact_version="v1",
        path=first_path,
        producer="tests",
    )

    with pytest.raises(ArtifactConflict, match="artifact_id 'stable-id' conflicts"):
        registry.register_file(
            artifact_id="stable-id",
            artifact_role="test",
            artifact_type="type-b",
            artifact_version="v1",
            path=second_path,
            producer="tests",
        )

    reloaded = ArtifactRegistry(registry_path, "job-conflict")
    assert reloaded.revision == 1
    assert reloaded.get("stable-id") is not None
    assert reloaded.get("stable-id").artifact_type == "type-a"  # type: ignore[union-attr]


def test_atomic_replace_failure_does_not_update_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    artifact_path = _write_artifact(tmp_path / "artifact.json", "artifact")
    registry = ArtifactRegistry(registry_path, "job-atomic")

    def fail_replace(source: str, destination: str) -> None:
        raise OSError(f"injected replace failure: {source} -> {destination}")

    monkeypatch.setattr(artifact_registry_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        registry.register_file(
            artifact_id="artifact",
            artifact_role="test",
            artifact_type="test",
            artifact_version="v1",
            path=artifact_path,
            producer="tests",
        )

    assert registry.revision == 0
    assert registry.get("artifact") is None
    assert not registry_path.exists()
    assert list(tmp_path.glob(".artifact_registry.json.*.tmp")) == []


def test_registry_lock_timeout_is_typed(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    artifact_path = _write_artifact(tmp_path / "artifact.json", "artifact")
    holder = ArtifactRegistry(registry_path, "job-lock")
    contender = ArtifactRegistry(
        registry_path,
        "job-lock",
        registry_lock_timeout_seconds=0.05,
        registry_lock_retry_interval_ms=5,
    )
    entered = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with holder._transaction_lock():
            entered.set()
            assert release.wait(timeout=5)

    thread = threading.Thread(target=hold_lock, daemon=True)
    thread.start()
    assert entered.wait(timeout=5)
    try:
        with pytest.raises(RegistryLockTimeout, match="timed out acquiring process registry lock"):
            contender.register_file(
                artifact_id="artifact",
                artifact_role="test",
                artifact_type="test",
                artifact_version="v1",
                path=artifact_path,
                producer="tests",
            )
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_two_processes_register_without_lost_update(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    first_path = _write_artifact(tmp_path / "first.json", "first")
    second_path = _write_artifact(tmp_path / "second.json", "second")
    context = multiprocessing.get_context("spawn")
    ready_queue = context.Queue()
    result_queue = context.Queue()
    start_event = context.Event()
    processes = [
        context.Process(
            target=_concurrent_register_worker,
            args=(
                str(registry_path),
                str(path),
                artifact_id,
                ready_queue,
                result_queue,
                start_event,
            ),
        )
        for artifact_id, path in (("first", first_path), ("second", second_path))
    ]

    for process in processes:
        process.start()
    try:
        ready = [ready_queue.get(timeout=15) for _ in processes]
        assert sorted(ready) == [("first", 0), ("second", 0)]
        start_event.set()
        results = [result_queue.get(timeout=15) for _ in processes]
    except Empty as exc:  # pragma: no cover - test reports a clearer process failure below
        raise AssertionError("registry worker did not report in time") from exc
    finally:
        start_event.set()
        for process in processes:
            process.join(timeout=15)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert sorted(results) == [("first", "ok"), ("second", "ok")]
    assert [process.exitcode for process in processes] == [0, 0]
    reloaded = ArtifactRegistry(registry_path, "job-concurrent")
    assert reloaded.revision == 2
    assert {record.artifact_id for record in reloaded.list_records()} == {"first", "second"}


def test_lock_file_contents_do_not_grant_or_deny_lock_ownership(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    lock_path = Path(f"{registry_path}.lock")
    lock_path.write_text("stale pid=999999 owner metadata", encoding="utf-8")
    artifact_path = _write_artifact(tmp_path / "artifact.json", "artifact")

    record = ArtifactRegistry(registry_path, "job-diagnostic-lock").register_file(
        artifact_id="artifact",
        artifact_role="test",
        artifact_type="test",
        artifact_version="v1",
        path=artifact_path,
        producer="tests",
    )

    assert record.artifact_id == "artifact"

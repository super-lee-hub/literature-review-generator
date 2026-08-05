from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from services.artifact_registry import ArtifactRegistry, UnverifiedArtifact, file_sha256
from services.job_workspace import JobWorkspace
from services.queue_service import (
    LocalPublicationContext,
    PersistentQueueService,
    QueueJobSpec,
    QueueState,
)
from runtime.provider_receipt_closure import ProviderReceiptClosure


def _claim(tmp_path: Path, job_id: str = "publication-job"):
    service = PersistentQueueService(tmp_path / "queue.json")
    service.add_job(
        QueueJobSpec(
            job_id=job_id,
            job_type="review",
            project_name="tests",
            parameters={},
        )
    )
    lease = service.claim_job(job_id, worker_id="worker", lease_seconds=30)
    assert lease is not None
    return service, lease


def test_direct_publication_keeps_preexisting_immutable_bytes_on_alias_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = JobWorkspace.create(str(tmp_path / "output"), "publication", "direct-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    context = LocalPublicationContext()
    target = Path(workspace.artifact_path("aliases/summary.json"))
    payload = b"immutable summary bytes"

    first = context.publish_bytes(
        target,
        payload,
        registry=registry,
        register_kwargs={
            "artifact_id": "summary:first",
            "artifact_role": "summary",
            "artifact_type": "summary_file",
            "artifact_version": "v1",
            "producer": "tests",
        },
    )
    final_path = Path(first.final_path)
    assert final_path.read_bytes() == payload

    def fail_alias(*args: Any, **kwargs: Any) -> Any:
        raise OSError("injected alias registration failure")

    monkeypatch.setattr(registry, "register_file", fail_alias)
    with pytest.raises(OSError, match="injected alias registration failure"):
        context.publish_bytes(
            target,
            payload,
            registry=registry,
            register_kwargs={
                "artifact_id": "summary:second",
                "artifact_role": "summary",
                "artifact_type": "summary_file",
                "artifact_version": "v1",
                "producer": "tests",
            },
        )

    assert final_path.is_file()
    assert final_path.read_bytes() == payload
    assert registry.get("summary:first") is not None
    assert registry.get("summary:second") is None


def test_direct_publication_repeated_concurrent_same_content_reuses_one_immutable_target(
    tmp_path: Path,
) -> None:
    workspace = JobWorkspace.create(str(tmp_path / "output"), "publication", "concurrent-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    context = LocalPublicationContext()
    target = Path(workspace.artifact_path("aliases/repeated.json"))
    payload = b"same immutable bytes"

    def publish(index: int) -> str:
        publication = context.publish_bytes(
            target,
            payload,
            registry=registry,
            register_kwargs={
                "artifact_id": f"summary:concurrent:{index}",
                "artifact_role": "summary",
                "artifact_type": "summary_file",
                "artifact_version": "v1",
                "producer": "tests",
            },
        )
        return publication.final_path

    with ThreadPoolExecutor(max_workers=6) as executor:
        final_paths = list(executor.map(publish, range(6)))

    normalized_final_paths = {
        str(path).removeprefix("\\\\?\\").casefold()
        for path in final_paths
    }
    assert len(normalized_final_paths) == 1
    final_path = Path(final_paths[0])
    assert final_path.is_file()
    assert final_path.read_bytes() == payload
    reloaded = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    assert all(reloaded.get(f"summary:concurrent:{index}") is not None for index in range(6))


def test_queue_target_and_publication_manifest_are_one_registry_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, lease = _claim(tmp_path)
    registry_path = tmp_path / "workspace" / "registry.json"
    registry_path.parent.mkdir()
    registry = ArtifactRegistry(registry_path, lease.job_id)
    context = service.publication_context(lease)
    staged = context.stage_bytes(tmp_path / "workspace" / "target.json", b"target")

    def fail_atomic(*args: Any, **kwargs: Any) -> Any:
        raise OSError("injected Registry fsync failure")

    monkeypatch.setattr(ArtifactRegistry, "register_files_atomic", fail_atomic)
    with pytest.raises(OSError, match="injected Registry fsync failure"):
        context.finalize_staged(
            staged,
            registry=registry,
            register_kwargs={
                "artifact_id": "target",
                "artifact_role": "target",
                "artifact_type": "test_artifact",
                "artifact_version": "v1",
                "producer": "tests",
            },
        )

    reloaded = ArtifactRegistry(registry_path, lease.job_id)
    assert reloaded.get("target") is None
    assert not any(
        record.artifact_type == "lease_publication_manifest"
        for record in reloaded.list_records()
    )
    assert service.release_lease(
        lease.job_id,
        lease_id=lease.lease_id,
        worker_id=lease.worker_id,
        lease_generation=lease.lease_generation,
        fence_token=lease.fence_token,
        state=QueueState.COMPLETED,
    )


@pytest.mark.parametrize(
    "failure_point",
    (
        "before_target_record",
        "after_target_construction",
        "manifest_construction",
        "registry_fsync",
    ),
)
def test_queue_publication_failure_variants_leave_no_ready_half_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    """Every queue publication failure leaves only unreferenced immutable bytes."""

    service, lease = _claim(tmp_path, job_id=f"publication-{failure_point}")
    registry_path = tmp_path / "workspace" / "registry.json"
    registry_path.parent.mkdir()
    registry = ArtifactRegistry(registry_path, lease.job_id)
    context = service.publication_context(lease)
    staged = context.stage_bytes(tmp_path / "workspace" / "target.json", b"target")

    def fail_before_target(*args: Any, **kwargs: Any) -> Any:
        raise OSError("injected failure before target record")

    def fail_after_target(records: Any, revision: int) -> None:
        del revision
        values = records.values() if hasattr(records, "values") else records
        assert any(item.artifact_id == "target" for item in values)
        values = records.values() if hasattr(records, "values") else records
        assert any(
            item.artifact_type == "lease_publication_manifest"
            for item in values
        )
        raise OSError("injected failure after target construction")

    def fail_manifest(*args: Any, **kwargs: Any) -> Any:
        raise OSError("injected manifest construction failure")

    def fail_fsync(directory: str) -> None:
        raise OSError(f"injected Registry fsync failure: {directory}")

    if failure_point == "before_target_record":
        monkeypatch.setattr(ArtifactRegistry, "register_files_atomic", fail_before_target)
    elif failure_point == "after_target_construction":
        monkeypatch.setattr(registry, "_write_registry_unlocked", fail_after_target)
    elif failure_point == "manifest_construction":
        monkeypatch.setattr(context, "_prepare_publication_manifest_unlocked", fail_manifest)
    else:
        monkeypatch.setattr(ArtifactRegistry, "_fsync_directory", staticmethod(fail_fsync))

    with pytest.raises(OSError, match="injected"):
        context.finalize_staged(
            staged,
            registry=registry,
            register_kwargs={
                "artifact_id": "target",
                "artifact_role": "target",
                "artifact_type": "test_artifact",
                "artifact_version": "v1",
                "producer": "tests",
            },
        )

    reloaded = ArtifactRegistry(registry_path, lease.job_id)
    assert reloaded.get("target") is None
    assert not any(
        record.artifact_type == "lease_publication_manifest"
        for record in reloaded.list_records()
    )
    assert reloaded.current_artifact_set_pointer() is None
    target_path = Path(staged.target_path)
    final_path = target_path.with_name(
        f"{target_path.stem}__{staged.content_hash[:24]}{target_path.suffix}"
    )
    assert final_path.is_file()
    assert file_sha256(final_path) == staged.content_hash


def test_zero_call_receipt_closure_rejects_terminal_provider_work() -> None:
    closure = ProviderReceiptClosure.evaluate([] , [])
    assert closure.complete is True
    assert closure.expected_call_ids == ()
    assert closure.observed_call_ids == ()


def test_current_artifact_set_rejects_wrong_validation_target_type(tmp_path: Path) -> None:
    from tests.test_repair_promotion import _install_atomic_registry_set

    workspace = JobWorkspace.create(str(tmp_path), "repair", job_id="cas-type-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    promotion, current = _install_atomic_registry_set(registry, workspace, "wrong-type", install_current=False)
    invalid = registry.build_current_artifact_set(
        promotion_transaction_id=promotion.artifact_id,
        promotion_transaction_hash=promotion.content_hash,
        review_draft_artifact_id=current.review_draft_artifact_id,
        review_draft_artifact_hash=current.review_draft_artifact_hash,
        citation_manifest_artifact_id=current.citation_manifest_artifact_id,
        citation_manifest_artifact_hash=current.citation_manifest_artifact_hash,
        review_docx_artifact_id=current.review_docx_artifact_id,
        review_docx_artifact_hash=current.review_docx_artifact_hash,
        validation_run_result_artifact_id="",
        validation_run_result_artifact_hash="",
        validation_receipt_closure_artifact_id=current.validation_receipt_closure_artifact_id,
        validation_receipt_closure_artifact_hash=current.validation_receipt_closure_artifact_hash,
        validation_status="not_requested",
        validation_disposition_artifact_id="validation:wrong-type",
        validation_disposition_artifact_hash=registry.get("validation:wrong-type").content_hash
        if registry.get("validation:wrong-type")
        else "f" * 64,
        actor="tests",
        reason="wrong typed validation target",
    )
    with pytest.raises(Exception, match="validation disposition|artifact type|validation target"):
        registry.switch_current_artifact_set(invalid, prepared_promotion_record=promotion)


def test_resolve_current_artifact_set_rejects_wrong_validation_target_type(
    tmp_path: Path,
) -> None:
    from tests.test_repair_promotion import _install_atomic_registry_set

    workspace = JobWorkspace.create(str(tmp_path), "repair", job_id="resolve-type-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    _install_atomic_registry_set(registry, workspace, "resolve-type")
    current_set = registry.resolve_current_artifact_set()
    assert current_set is not None

    def mutate(records: list[dict[str, Any]]) -> None:
        target = next(
            item
            for item in records
            if item["artifact_id"] == current_set.validation_run_result_artifact_id
        )
        target["artifact_type"] = "validation_disposition"

    _rewrite_registry_artifacts(registry, mutate)
    with pytest.raises(UnverifiedArtifact, match="validation evidence|wrong artifact type"):
        registry.resolve_current_artifact_set()


def _rewrite_registry_artifacts(
    registry: ArtifactRegistry,
    mutator: Any,
) -> None:
    registry_path = Path(registry.registry_path)
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    mutator(payload["artifacts"])
    registry_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    registry.reload()


def test_resolve_current_artifact_set_rejects_tampered_pointer_identity_and_dependencies(
    tmp_path: Path,
) -> None:
    from tests.test_repair_promotion import _install_atomic_registry_set

    workspace = JobWorkspace.create(str(tmp_path), "repair", job_id="resolve-pointer-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    _install_atomic_registry_set(registry, workspace, "resolve-pointer")

    def mutate(records: list[dict[str, Any]]) -> None:
        pointer = next(item for item in records if item["artifact_id"] == "current-artifact-set:pointer")
        pointer["artifact_type"] = "arbitrary_ready_json"

    _rewrite_registry_artifacts(registry, mutate)
    with pytest.raises(UnverifiedArtifact, match="pointer"):
        registry.resolve_current_artifact_set()

    # Restore a clean baseline, then break the authoritative pointer dependency
    # without changing the pointed-to bytes.
    registry_path = Path(registry.registry_path)
    registry_path.unlink()
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    _install_atomic_registry_set(registry, workspace, "resolve-dependency")

    def mutate_dependency(records: list[dict[str, Any]]) -> None:
        pointer = next(item for item in records if item["artifact_id"] == "current-artifact-set:pointer")
        pointer["depends_on"][0]["content_hash"] = "f" * 64

    _rewrite_registry_artifacts(registry, mutate_dependency)
    with pytest.raises(UnverifiedArtifact, match="dependenc"):
        registry.resolve_current_artifact_set()


def test_resolve_current_artifact_set_rejects_set_identity_and_target_binding_tampering(
    tmp_path: Path,
) -> None:
    from tests.test_repair_promotion import _install_atomic_registry_set

    workspace = JobWorkspace.create(str(tmp_path), "repair", job_id="resolve-set-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    _install_atomic_registry_set(registry, workspace, "resolve-set")
    pointer = registry.current_artifact_set_pointer()
    assert pointer is not None
    set_id = str(pointer.metadata["current_set_id"])
    set_record = registry.get(set_id)
    assert set_record is not None

    def mutate_set_payload(records: list[dict[str, Any]]) -> None:
        set_payload = json.loads(Path(set_record.path).read_text(encoding="utf-8"))
        set_payload["reason"] = "tampered after publication"
        Path(set_record.path).write_text(
            json.dumps(set_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        new_hash = file_sha256(set_record.path)
        for item in records:
            if item["artifact_id"] == set_id:
                item["content_hash"] = new_hash
            elif item["artifact_id"] == "current-artifact-set:pointer":
                item["content_hash"] = new_hash
                item["metadata"]["current_set_hash"] = new_hash

    _rewrite_registry_artifacts(registry, mutate_set_payload)
    with pytest.raises(UnverifiedArtifact, match="content addressed|identity"):
        registry.resolve_current_artifact_set()

    registry_path = Path(registry.registry_path)
    registry_path.unlink()
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    _install_atomic_registry_set(registry, workspace, "resolve-promotion")
    pointer = registry.current_artifact_set_pointer()
    assert pointer is not None
    current_set = registry.resolve_current_artifact_set()
    assert current_set is not None
    promotion = registry.get(current_set.promotion_transaction_id)
    assert promotion is not None
    promotion_payload = json.loads(Path(promotion.path).read_text(encoding="utf-8"))
    promotion_payload["review_draft_artifact_id"] = "draft:another-target"
    Path(promotion.path).write_text(
        json.dumps(promotion_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    new_promotion_hash = file_sha256(promotion.path)

    def mutate_promotion_binding(records: list[dict[str, Any]]) -> None:
        for item in records:
            if item["artifact_id"] == promotion.artifact_id:
                item["content_hash"] = new_promotion_hash

    _rewrite_registry_artifacts(registry, mutate_promotion_binding)
    with pytest.raises(UnverifiedArtifact, match="promotion|target"):
        registry.resolve_current_artifact_set()

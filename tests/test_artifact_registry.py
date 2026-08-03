import json

import pytest

from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRegistry,
    RegistryCorruption,
    UnverifiedArtifact,
    UnverifiedDependency,
)


def test_artifact_registry_persists_records_and_dependencies(tmp_path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    artifact_path = tmp_path / "artifacts" / "demo.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(json.dumps({"ok": True}), encoding="utf-8")
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(b"%PDF-1.4\n")

    registry = ArtifactRegistry(str(registry_path), "job-123")
    source_record = registry.register_file(
        artifact_id="source:source.pdf",
        artifact_role="source",
        artifact_type="source",
        artifact_version="v1",
        path=source_path,
        producer="tests",
    )
    record = registry.register_file(
        artifact_role="summary",
        artifact_type="summary_file",
        artifact_version="v1",
        path=str(artifact_path),
        producer="tests",
        depends_on=[
            ArtifactDependencyRefV2(
                dependency_kind="local_job",
                job_id="job-123",
                artifact_id=source_record.artifact_id,
                artifact_type=source_record.artifact_type,
                path=source_record.path,
                content_hash=source_record.content_hash,
            )
        ],
    )

    assert record.job_id == "job-123"
    assert record.content_hash
    assert record.depends_on[0].artifact_type == "source"

    reloaded = ArtifactRegistry(str(registry_path), "job-123")
    loaded = reloaded.get(record.artifact_id)

    assert loaded is not None
    assert loaded.path == str(artifact_path.resolve())
    assert loaded.depends_on[0].content_hash == source_record.content_hash
    assert isinstance(loaded.depends_on[0], ArtifactDependencyRefV2)
    assert loaded.depends_on[0].job_id == "job-123"
    assert loaded.depends_on[0].artifact_id == "source:source.pdf"

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    assert payload["artifact_registry_version"] == "v2"
    assert payload["revision"] == 2


def test_ready_registration_rejects_unregistered_local_dependency(tmp_path) -> None:
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "job-123")
    child_path = tmp_path / "child.json"
    child_path.write_text("{}", encoding="utf-8")
    parent_path = tmp_path / "parent.json"
    parent_path.write_text("{}", encoding="utf-8")

    with pytest.raises(UnverifiedDependency, match="not registered"):
        registry.register_file(
            artifact_id="child",
            artifact_role="child",
            artifact_type="child",
            artifact_version="v1",
            path=child_path,
            producer="tests",
            depends_on=[
                ArtifactDependencyRefV2(
                    dependency_kind="local_job",
                    job_id="job-123",
                    artifact_id="parent",
                    artifact_type="parent",
                    path=str(parent_path),
                    content_hash="a" * 64,
                )
            ],
        )

    assert registry.get("child") is None
    assert registry.revision == 0


def test_legacy_path_only_dependency_is_rejected(tmp_path) -> None:
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "job-123")
    shared_path = tmp_path / "shared.json"
    shared_path.write_text("{}", encoding="utf-8")
    for artifact_id in ("source-a", "source-b"):
        registry.register_file(
            artifact_id=artifact_id,
            artifact_role="source",
            artifact_type="source",
            artifact_version="v1",
            path=shared_path,
            producer="tests",
        )
    child_path = tmp_path / "child.json"
    child_path.write_text("{}", encoding="utf-8")

    with pytest.raises(RegistryCorruption, match="missing required fields"):
        registry.register_file(
            artifact_id="child",
            artifact_role="child",
            artifact_type="child",
            artifact_version="v1",
            path=child_path,
            producer="tests",
            depends_on=[
                {"artifact_type": "source", "path": str(shared_path)}
            ],
        )

    assert registry.get("child") is None


def test_quarantined_registration_preserves_unverified_dependency(tmp_path) -> None:
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "job-123")
    child_path = tmp_path / "child.json"
    child_path.write_text("{}", encoding="utf-8")
    dependency = ArtifactDependencyRefV2(
        dependency_kind="local_job",
        job_id="job-123",
        artifact_id="parent",
        artifact_type="parent",
        path=str(tmp_path / "missing-parent.json"),
        content_hash="a" * 64,
    )

    record = registry.register_file(
        artifact_id="child",
        artifact_role="child",
        artifact_type="child",
        artifact_version="v1",
        path=child_path,
        producer="tests",
        status="quarantined",
        depends_on=[dependency],
    )

    assert record.status == "quarantined"
    assert record.depends_on[0].content_hash == "a" * 64


def test_ready_transition_rejects_tampered_artifact(tmp_path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    artifact_path = tmp_path / "candidate.json"
    artifact_path.write_text("original", encoding="utf-8")
    registry = ArtifactRegistry(registry_path, "job-123")
    registry.register_file(
        artifact_id="candidate",
        artifact_role="candidate",
        artifact_type="candidate",
        artifact_version="v1",
        path=artifact_path,
        producer="tests",
        status="quarantined",
    )
    quarantined_revision = registry.revision
    artifact_path.write_text("tampered", encoding="utf-8")

    with pytest.raises(UnverifiedArtifact, match="artifact content hash changed: candidate"):
        registry.update_record("candidate", status="ready")

    assert registry.revision == quarantined_revision
    assert registry.get("candidate").status == "quarantined"  # type: ignore[union-attr]


def test_ready_transition_rejects_empty_artifact_hash(tmp_path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    artifact_path = tmp_path / "candidate.json"
    artifact_path.write_text("original", encoding="utf-8")
    ArtifactRegistry(registry_path, "job-123").register_file(
        artifact_id="candidate",
        artifact_role="candidate",
        artifact_type="candidate",
        artifact_version="v1",
        path=artifact_path,
        producer="tests",
        status="quarantined",
    )
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    payload["artifacts"][0]["content_hash"] = ""
    registry_path.write_text(json.dumps(payload), encoding="utf-8")
    registry = ArtifactRegistry(registry_path, "job-123")
    quarantined_revision = registry.revision

    with pytest.raises(UnverifiedArtifact, match="artifact content hash is missing: candidate"):
        registry.update_record("candidate", status="ready")

    assert registry.revision == quarantined_revision
    assert registry.get("candidate").status == "quarantined"  # type: ignore[union-attr]


def test_current_dependency_requires_explicit_identity() -> None:
    with pytest.raises(RegistryCorruption, match="missing required fields"):
        ArtifactDependencyRefV2.from_dict(
            {"artifact_type": "source_pdf", "path": "/tmp/source.pdf", "content_hash": "abc123"}
        )

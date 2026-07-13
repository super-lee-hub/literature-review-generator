import json

from services.artifact_registry import (
    ArtifactDependencyRef,
    ArtifactDependencyRefV2,
    ArtifactRegistry,
)


def test_artifact_registry_persists_records_and_dependencies(tmp_path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    artifact_path = tmp_path / "artifacts" / "demo.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(json.dumps({"ok": True}), encoding="utf-8")

    registry = ArtifactRegistry(str(registry_path), "job-123")
    record = registry.register_file(
        artifact_role="summary",
        artifact_type="summary_file",
        artifact_version="v1",
        path=str(artifact_path),
        producer="tests",
        depends_on=[
            ArtifactDependencyRef(
                artifact_type="source",
                path="/tmp/source.pdf",
                content_hash="abc123",
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
    assert loaded.depends_on[0].content_hash == "abc123"
    assert isinstance(loaded.depends_on[0], ArtifactDependencyRefV2)
    assert loaded.depends_on[0].job_id == "job-123"
    assert loaded.depends_on[0].artifact_id == "source:source.pdf"

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    assert payload["artifact_registry_version"] == "v2"
    assert payload["revision"] == 1


def test_legacy_dependency_constructor_keeps_positional_field_order() -> None:
    dependency = ArtifactDependencyRef("source_pdf", "/tmp/source.pdf", "abc123")

    assert dependency.artifact_type == "source_pdf"
    assert dependency.path == "/tmp/source.pdf"
    assert dependency.content_hash == "abc123"
    assert dependency.dependency_kind == "local_job"

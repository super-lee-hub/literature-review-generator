from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.orchestrator import AgentRuntimeBridge
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRegistry,
    file_sha256,
)
from services.job_workspace import JobWorkspace
from tests.test_runtime_bridge_helpers import write_json
from validation.run_result import (
    ValidationExecutionStatus,
    ValidationInputArtifactsV1,
    ValidationRunResultV1,
)


def test_runtime_validation_bridge_registers_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    (pdf_dir / "alpha.pdf").write_bytes(b"%PDF-1.4\n%alpha\n")

    queue_file = tmp_path / "output" / "_queue" / "queue.json"
    queue_file.parent.mkdir(parents=True)

    bridge = AgentRuntimeBridge(
        RuntimeJobSpec(
            project_name="demo-ai",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
            action="validate_review",
            queue_file=str(queue_file),
        )
    )
    session = bridge.bootstrap()

    review_draft_path = Path(session.stage_host._review_draft_path())
    citation_manifest_path = Path(session.stage_host._citation_manifest_path())
    parent_workspace = JobWorkspace.create(
        str(tmp_path),
        "validation-parent",
        job_id="job-validation-parent",
    )
    parent_registry = ArtifactRegistry(
        parent_workspace.paths.registry_path,
        parent_workspace.job_id,
    )
    evidence_manifest_path = Path(parent_workspace.artifact_path("alpha.evidence_manifest_v1.json"))
    report_file = Path(session.context.workspace.report_path("demo-ai_validation_report.txt"))
    manual_report_file = Path(session.context.workspace.report_path("demo-ai_manual_review_report.json"))
    validation_run_result_file = Path(
        session.context.workspace.report_path("demo-ai_validation_run_result_v1.json")
    )

    write_json(
        review_draft_path,
        {"artifact_type": "review_draft", "artifact_version": "v3", "content": {"sections": []}},
    )
    write_json(
        citation_manifest_path,
        {"artifact_type": "citation_manifest", "artifact_version": "v3", "occurrences": [], "citation_sets": []},
    )
    write_json(
        evidence_manifest_path,
        {"artifact_type": "evidence_manifest", "artifact_version": "v1"},
    )
    review_record = session.context.registry.register_file(
        artifact_id="review-draft",
        artifact_role="review_draft",
        artifact_type=session.stage_host.REVIEW_DRAFT_ARTIFACT_TYPE,
        artifact_version="v3",
        path=review_draft_path,
        producer="tests",
    )
    citation_record = session.context.registry.register_file(
        artifact_id="citation-manifest-v3",
        artifact_role="citation_manifest",
        artifact_type=session.stage_host.CITATION_MANIFEST_ARTIFACT_TYPE,
        artifact_version="v3",
        path=citation_manifest_path,
        producer="tests",
    )
    evidence_record = parent_registry.register_file(
        artifact_id="evidence-manifest-alpha",
        artifact_role="paper_evidence",
        artifact_type="evidence_manifest",
        artifact_version="v1",
        path=evidence_manifest_path,
        producer="tests",
    )
    external_evidence = ArtifactDependencyRefV2(
        dependency_kind="external_job",
        job_id=parent_workspace.job_id,
        artifact_id=evidence_record.artifact_id,
        artifact_type=evidence_record.artifact_type,
        path=evidence_record.path,
        content_hash=evidence_record.content_hash,
    )
    carrier_path = Path(session.context.workspace.artifact_path("derived-paper.json"))
    write_json(carrier_path, {"artifact_type": "paper_artifact"})

    def external_registry_resolver(job_id: str) -> ArtifactRegistry | None:
        return parent_registry if job_id == parent_workspace.job_id else None

    session.context.registry.register_file(
        artifact_id="derived-paper-alpha",
        artifact_role="paper_artifact",
        artifact_type="paper_artifact",
        artifact_version="v1",
        path=carrier_path,
        producer="tests",
        depends_on=[external_evidence],
        external_registry_resolver=external_registry_resolver,
    )
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text("ok", encoding="utf-8")
    manual_report_file.write_text(json.dumps({"items": []}), encoding="utf-8")
    canonical_result = ValidationRunResultV1.create(
        job_id=session.context.workspace.job_id,
        attempt_id="attempt-1",
        execution_status=ValidationExecutionStatus.SUCCEEDED,
        report_id="validation_report_demo",
        input_artifacts=ValidationInputArtifactsV1(
            review_draft_id=review_record.artifact_id,
            review_draft_hash=review_record.content_hash,
            citation_manifest_id=citation_record.artifact_id,
            citation_manifest_hash=citation_record.content_hash,
            evidence_manifest_ids=(evidence_record.artifact_id,),
            evidence_manifest_hashes=(evidence_record.content_hash,),
        ),
        review_has_citations=False,
        evidence_complete=True,
    )
    write_json(validation_run_result_file, canonical_result.to_dict())

    def _fake_run_review_validation(_adapter):
        return {
            "success": True,
            "validation_run_result": canonical_result,
            "validation_run_result_file": str(validation_run_result_file),
            "manual_review_items": [],
            "report_file": str(report_file),
            "manual_report_file": str(manual_report_file),
        }

    monkeypatch.setattr("validator.run_review_validation", _fake_run_review_validation)
    validation_result = bridge.run_validation(
        session,
        attempt_id="attempt-1",
        external_registry_resolver=external_registry_resolver,
    )

    registry_payload = json.loads(Path(session.context.workspace.paths.registry_path).read_text(encoding="utf-8"))
    artifact_ids = {item["artifact_id"] for item in registry_payload["artifacts"]}
    artifact_types = {item["artifact_type"] for item in registry_payload["artifacts"]}

    assert validation_result.success is True
    assert "validation_report_demo" in artifact_ids
    assert "validation_run_result" in artifact_types
    assert "validation_report_projection" in artifact_types
    assert "manual_review_projection" in artifact_types
    canonical_record = session.context.registry.get("validation_report_demo")
    assert canonical_record is not None
    assert canonical_record.content_hash == file_sha256(validation_run_result_file)
    assert [
        (dependency.artifact_type, dependency.artifact_id, dependency.content_hash)
        for dependency in canonical_record.depends_on
    ] == [
        ("review_draft", review_record.artifact_id, review_record.content_hash),
        ("citation_manifest", citation_record.artifact_id, citation_record.content_hash),
        ("evidence_manifest", evidence_record.artifact_id, evidence_record.content_hash),
    ]
    assert canonical_record.depends_on[-1] == external_evidence

    mismatched_result = ValidationRunResultV1.create(
        job_id=session.context.workspace.job_id,
        attempt_id="attempt-1",
        execution_status=ValidationExecutionStatus.SUCCEEDED,
        report_id="validation_report_mismatch",
        input_artifacts=ValidationInputArtifactsV1(
            review_draft_id=review_record.artifact_id,
            review_draft_hash=review_record.content_hash,
            citation_manifest_id=citation_record.artifact_id,
            citation_manifest_hash=citation_record.content_hash,
            evidence_manifest_ids=(evidence_record.artifact_id,),
            evidence_manifest_hashes=("f" * 64,),
        ),
        review_has_citations=False,
        evidence_complete=True,
    )
    mismatched_path = Path(
        session.context.workspace.report_path("validation_run_result_mismatch.json")
    )
    write_json(mismatched_path, mismatched_result.to_dict())
    monkeypatch.setattr(
        "validator.run_review_validation",
        lambda _adapter: {
            "success": True,
            "validation_run_result": mismatched_result,
            "validation_run_result_file": str(mismatched_path),
        },
    )
    mismatched_stage = bridge.run_validation(
        session,
        attempt_id="attempt-1",
        external_registry_resolver=external_registry_resolver,
    )

    mismatched_record = session.context.registry.get("validation_report_mismatch")
    assert mismatched_stage.success is False
    assert mismatched_stage.metadata["execution_status"] == "failed"
    assert mismatched_stage.metadata["validation_disposition"] == "unvalidated"
    assert mismatched_stage.metadata["declared_execution_status"] == "succeeded"
    assert mismatched_stage.metadata["declared_validation_disposition"] == "clean"
    assert mismatched_stage.metadata["failure_reason"] == (
        "validation_input_dependencies_unverified"
    )
    assert mismatched_record is not None
    assert mismatched_record.status == "quarantined"


def test_runtime_validation_bridge_rejects_legacy_report_as_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    (pdf_dir / "alpha.pdf").write_bytes(b"%PDF-1.4\n%alpha\n")
    queue_file = tmp_path / "output" / "_queue" / "queue.json"
    queue_file.parent.mkdir(parents=True)
    bridge = AgentRuntimeBridge(
        RuntimeJobSpec(
            project_name="legacy-validation",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
            action="validate_review",
            queue_file=str(queue_file),
        )
    )
    session = bridge.bootstrap()

    monkeypatch.setattr(
        "validator.run_review_validation",
        lambda _adapter: {
            "success": True,
            "report": SimpleNamespace(report_id="legacy"),
        },
    )
    result = bridge.run_validation(session)

    assert result.success is False
    assert result.metadata["execution_status"] == "failed"
    assert result.metadata["validation_disposition"] == "unvalidated"


def test_runtime_validation_bridge_rejects_success_without_canonical_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    (pdf_dir / "alpha.pdf").write_bytes(b"%PDF-1.4\n%alpha\n")
    bridge = AgentRuntimeBridge(
        RuntimeJobSpec(
            project_name="missing-canonical-validation",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
            action="validate_review",
            queue_file=str(tmp_path / "queue.json"),
        )
    )
    session = bridge.bootstrap()
    in_memory = ValidationRunResultV1.create(
        job_id=session.context.workspace.job_id,
        attempt_id="attempt-1",
        execution_status="succeeded",
        review_has_citations=False,
        evidence_complete=True,
    )

    monkeypatch.setattr(
        "validator.run_review_validation",
        lambda _adapter: {
            "success": True,
            "validation_run_result": in_memory,
        },
    )
    result = bridge.run_validation(session, attempt_id="attempt-1")

    assert result.success is False
    assert result.metadata["execution_status"] == "failed"
    assert result.metadata["validation_disposition"] == "unvalidated"
    assert result.metadata["failure_reason"] == "canonical_validation_result_missing"


@pytest.mark.parametrize(
    ("persisted_job_id", "persisted_attempt_id", "failure_reason"),
    [
        ("wrong-job", "attempt-1", "canonical_validation_job_mismatch"),
        ("expected-job", "wrong-attempt", "canonical_validation_attempt_mismatch"),
    ],
)
def test_runtime_validation_bridge_rejects_canonical_identity_mismatch(
    tmp_path: Path,
    persisted_job_id: str,
    persisted_attempt_id: str,
    failure_reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    (pdf_dir / "alpha.pdf").write_bytes(b"%PDF-1.4\n%alpha\n")
    bridge = AgentRuntimeBridge(
        RuntimeJobSpec(
            project_name="identity-validation",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
            action="validate_review",
            queue_file=str(tmp_path / "queue.json"),
        )
    )
    session = bridge.bootstrap()
    expected_job_id = session.context.workspace.job_id
    canonical = ValidationRunResultV1.create(
        job_id=(expected_job_id if persisted_job_id == "expected-job" else persisted_job_id),
        attempt_id=persisted_attempt_id,
        execution_status="succeeded",
        review_has_citations=False,
        evidence_complete=True,
    )
    canonical_path = Path(session.context.workspace.report_path("validation_run_result_v1.json"))
    write_json(canonical_path, canonical.to_dict())

    monkeypatch.setattr(
        "validator.run_review_validation",
        lambda _adapter: {
            "success": True,
            "validation_run_result": ValidationRunResultV1.create(
                job_id=expected_job_id,
                attempt_id="attempt-1",
                execution_status="succeeded",
                review_has_citations=False,
                evidence_complete=True,
            ),
            "validation_run_result_file": str(canonical_path),
        },
    )
    result = bridge.run_validation(session, attempt_id="attempt-1")

    assert result.success is False
    assert result.metadata["failure_reason"] == failure_reason

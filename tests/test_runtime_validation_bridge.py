from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.orchestrator import AgentRuntimeBridge
from services.artifact_registry import file_sha256
from tests.test_runtime_bridge_helpers import build_legacy_main, write_json
from validation.run_result import (
    ValidationExecutionStatus,
    ValidationInputArtifactsV1,
    ValidationRunResultV1,
)


def test_runtime_validation_bridge_registers_reports(tmp_path: Path) -> None:
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
    session = bridge.bootstrap(build_legacy_main())

    review_draft_path = Path(session.generator._review_draft_v2_path())
    citation_manifest_path = Path(session.generator._citation_manifest_path())
    report_file = Path(session.context.workspace.report_path("demo-ai_validation_report.txt"))
    manual_report_file = Path(session.context.workspace.report_path("demo-ai_manual_review_report.json"))
    validation_run_result_file = Path(
        session.context.workspace.report_path("demo-ai_validation_run_result_v1.json")
    )

    write_json(
        review_draft_path,
        {"artifact_type": "review_draft", "artifact_version": "v2", "content": {"sections": []}},
    )
    write_json(
        citation_manifest_path,
        {"artifact_type": "citation_manifest", "artifact_version": "v3", "occurrences": [], "citation_sets": []},
    )
    review_record = session.context.registry.register_file(
        artifact_id="review-draft-v2",
        artifact_role="review_draft",
        artifact_type=session.generator.REVIEW_DRAFT_V2_ARTIFACT_TYPE,
        artifact_version="v2",
        path=review_draft_path,
        producer="tests",
    )
    citation_record = session.context.registry.register_file(
        artifact_id="citation-manifest-v3",
        artifact_role="citation_manifest",
        artifact_type=session.generator.CITATION_MANIFEST_ARTIFACT_TYPE,
        artifact_version="v3",
        path=citation_manifest_path,
        producer="tests",
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

    validation_result = bridge.run_validation(
        session,
        attempt_id="attempt-1",
        validator_module=SimpleNamespace(run_review_validation=_fake_run_review_validation),
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


def test_runtime_validation_bridge_rejects_legacy_report_as_verified(tmp_path: Path) -> None:
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
    session = bridge.bootstrap(build_legacy_main())

    result = bridge.run_validation(
        session,
        validator_module=SimpleNamespace(
            run_review_validation=lambda _adapter: {
                "success": True,
                "report": SimpleNamespace(report_id="legacy"),
            }
        ),
    )

    assert result.success is False
    assert result.metadata["execution_status"] == "skipped"
    assert result.metadata["validation_disposition"] == "unvalidated"


def test_runtime_validation_bridge_rejects_success_without_canonical_file(tmp_path: Path) -> None:
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
    session = bridge.bootstrap(build_legacy_main())
    in_memory = ValidationRunResultV1.create(
        job_id=session.context.workspace.job_id,
        attempt_id="attempt-1",
        execution_status="succeeded",
        review_has_citations=False,
        evidence_complete=True,
    )

    result = bridge.run_validation(
        session,
        attempt_id="attempt-1",
        validator_module=SimpleNamespace(
            run_review_validation=lambda _adapter: {
                "success": True,
                "validation_run_result": in_memory,
            }
        ),
    )

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
    session = bridge.bootstrap(build_legacy_main())
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

    result = bridge.run_validation(
        session,
        attempt_id="attempt-1",
        validator_module=SimpleNamespace(
            run_review_validation=lambda _adapter: {
                "success": True,
                "validation_run_result": ValidationRunResultV1.create(
                    job_id=expected_job_id,
                    attempt_id="attempt-1",
                    execution_status="succeeded",
                    review_has_citations=False,
                    evidence_complete=True,
                ),
                "validation_run_result_file": str(canonical_path),
            }
        ),
    )

    assert result.success is False
    assert result.metadata["failure_reason"] == failure_reason

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from runtime.reconcile import (
    ReconcileValidationError,
    RuntimeReconciler,
    _validate_validation_run_result,
)
from runtime.stage_terminal import StageTerminalStore, TerminalStageRecordV1
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    file_sha256,
)
from services.job_workspace import JobWorkspace, atomic_write_json
from validation.input_dependencies import (
    ValidationInputDependencyError,
    resolve_validation_input_dependencies,
)
from validation.run_result import ValidationInputArtifactsV1, ValidationRunResultV1


def _dependency(record: ArtifactRecord, *, dependency_kind: str = "local_job") -> ArtifactDependencyRefV2:
    return ArtifactDependencyRefV2(
        dependency_kind=dependency_kind,  # type: ignore[arg-type]
        job_id=record.job_id,
        artifact_id=record.artifact_id,
        artifact_type=record.artifact_type,
        path=record.path,
        content_hash=record.content_hash,
    )


def _json_schema(_record: ArtifactRecord, path: Path) -> None:
    assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)


def _validation_graph(
    tmp_path: Path,
) -> tuple[RuntimeReconciler, ArtifactRecord, ArtifactRecord]:
    workspace = JobWorkspace.create(
        str(tmp_path),
        "validation-dependency-contract",
        job_id="job-validation-dependency-contract",
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    records: list[ArtifactRecord] = []
    for artifact_id, artifact_type, version in (
        ("review:v2", "review_draft", "v2"),
        ("citation:v3", "citation_manifest", "v3"),
        ("evidence:paper-a", "evidence_manifest", "v1"),
    ):
        path = Path(workspace.artifact_path(f"{artifact_id.replace(':', '-')}.json"))
        atomic_write_json(
            str(path),
            {"artifact_type": artifact_type, "artifact_version": version},
        )
        records.append(
            registry.register_file(
                artifact_id=artifact_id,
                artifact_role=artifact_type,
                artifact_type=artifact_type,
                artifact_version=version,
                path=path,
                producer="tests",
            )
        )
    review, citation, evidence = records
    validation = ValidationRunResultV1.create(
        job_id=workspace.job_id,
        attempt_id="attempt-validation",
        execution_status="succeeded",
        report_id="validation:contract",
        input_artifacts=ValidationInputArtifactsV1(
            review_draft_id=review.artifact_id,
            review_draft_hash=review.content_hash,
            citation_manifest_id=citation.artifact_id,
            citation_manifest_hash=citation.content_hash,
            evidence_manifest_ids=(evidence.artifact_id,),
            evidence_manifest_hashes=(evidence.content_hash,),
        ),
        expected_claim_count=0,
        review_has_citations=False,
        evidence_complete=True,
    )
    validation_path = Path(workspace.artifact_path("validation_run_result_v1.json"))
    atomic_write_json(str(validation_path), validation.to_dict())
    validation_record = registry.register_file(
        artifact_id=validation.validation_run_id,
        artifact_role="validation",
        artifact_type="validation_run_result",
        artifact_version="v1",
        path=validation_path,
        producer="tests",
        depends_on=[_dependency(record) for record in records],
    )
    terminal = TerminalStageRecordV1.create(
        job_id=workspace.job_id,
        attempt_id=validation.attempt_id,
        stage_name="validate",
        status="succeeded",
        producer="tests",
        output_artifact_refs=[_dependency(validation_record)],
    )
    StageTerminalStore(workspace, registry).persist(terminal)
    reconciler = RuntimeReconciler(
        workspace,
        registry,
        schema_validators={
            "review_draft": _json_schema,
            "citation_manifest": _json_schema,
            "evidence_manifest": _json_schema,
        },
    )
    return reconciler, validation_record, evidence


@pytest.mark.parametrize(
    "mismatch",
    ("missing", "extra", "duplicate", "hash", "type", "job", "kind"),
)
def test_validation_declared_inputs_must_exactly_match_registry_dependencies(
    tmp_path: Path,
    mismatch: str,
) -> None:
    _reconciler, validation_record, _evidence = _validation_graph(tmp_path)
    dependencies = list(validation_record.depends_on)
    if mismatch == "missing":
        dependencies.pop()
    elif mismatch == "extra":
        dependencies.append(replace(dependencies[-1], artifact_id="evidence:extra"))
    elif mismatch == "duplicate":
        dependencies.append(dependencies[-1])
    elif mismatch == "hash":
        dependencies[-1] = replace(dependencies[-1], content_hash="f" * 64)
    elif mismatch == "type":
        dependencies[-1] = replace(dependencies[-1], artifact_type="paper_artifact")
    elif mismatch == "job":
        dependencies[-1] = replace(dependencies[-1], job_id="foreign-job")
    else:
        dependencies[-1] = replace(
            dependencies[-1],
            dependency_kind="external_job",
        )

    with pytest.raises(ReconcileValidationError, match="Validation input dependencies"):
        _validate_validation_run_result(
            replace(validation_record, depends_on=dependencies),
            Path(validation_record.path),
        )


def test_validation_payload_hash_must_match_registry_dependency_hash(tmp_path: Path) -> None:
    _reconciler, validation_record, _evidence = _validation_graph(tmp_path)
    path = Path(validation_record.path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["input_artifacts"]["evidence_manifest_hashes"] = ["f" * 64]
    atomic_write_json(str(path), payload)

    with pytest.raises(ReconcileValidationError, match="Validation input dependencies"):
        _validate_validation_run_result(
            replace(validation_record, content_hash=file_sha256(path)),
            path,
        )


def test_validation_dependency_path_must_match_registry_record(tmp_path: Path) -> None:
    reconciler, validation_record, _evidence = _validation_graph(tmp_path)
    dependencies = list(validation_record.depends_on)
    dependencies[-1] = replace(
        dependencies[-1],
        path=str(tmp_path / "wrong-evidence-path.json"),
    )

    with pytest.raises(ReconcileValidationError, match="dependency path mismatch"):
        reconciler.validate_record(
            replace(validation_record, depends_on=dependencies)
        )


@pytest.mark.parametrize("mutation", ("delete", "tamper"))
def test_evidence_manifest_change_invalidates_validation_terminal_and_resume(
    tmp_path: Path,
    mutation: str,
) -> None:
    reconciler, _validation_record, evidence = _validation_graph(tmp_path)
    assert reconciler.stage_is_complete("validate") is True

    evidence_path = Path(evidence.path)
    if mutation == "delete":
        evidence_path.unlink()
    else:
        evidence_path.write_text('{"tampered": true}', encoding="utf-8")

    assert reconciler.stage_is_complete("validate") is False
    assert reconciler.load_completed_stage_result("validate") is None


def test_evidence_manifest_status_change_invalidates_stale_reconciler_snapshot(
    tmp_path: Path,
) -> None:
    reconciler, _validation_record, evidence = _validation_graph(tmp_path)
    assert reconciler.stage_is_complete("validate") is True

    concurrent_registry = ArtifactRegistry(
        reconciler.registry.registry_path,
        reconciler.registry.job_id,
    )
    concurrent_registry.update_record(evidence.artifact_id, status="quarantined")

    assert reconciler.stage_is_complete("validate") is False
    assert reconciler.load_completed_stage_result("validate") is None


def test_validation_dependency_resolution_preserves_external_evidence_identity(
    tmp_path: Path,
) -> None:
    parent_workspace = JobWorkspace.create(str(tmp_path), "parent", job_id="job-parent")
    parent_registry = ArtifactRegistry(
        parent_workspace.paths.registry_path,
        parent_workspace.job_id,
    )
    evidence_path = Path(parent_workspace.artifact_path("evidence.json"))
    atomic_write_json(
        str(evidence_path),
        {"artifact_type": "evidence_manifest", "artifact_version": "v1"},
    )
    evidence = parent_registry.register_file(
        artifact_id="evidence:parent-paper",
        artifact_role="paper_evidence",
        artifact_type="evidence_manifest",
        artifact_version="v1",
        path=evidence_path,
        producer="tests",
    )

    child_workspace = JobWorkspace.create(str(tmp_path), "child", job_id="job-child")
    child_registry = ArtifactRegistry(child_workspace.paths.registry_path, child_workspace.job_id)
    local_records: list[ArtifactRecord] = []
    for artifact_id, artifact_type, version in (
        ("review:v2", "review_draft", "v2"),
        ("citation:v3", "citation_manifest", "v3"),
    ):
        path = Path(child_workspace.artifact_path(f"{artifact_id.replace(':', '-')}.json"))
        atomic_write_json(
            str(path),
            {"artifact_type": artifact_type, "artifact_version": version},
        )
        local_records.append(
            child_registry.register_file(
                artifact_id=artifact_id,
                artifact_role=artifact_type,
                artifact_type=artifact_type,
                artifact_version=version,
                path=path,
                producer="tests",
            )
        )
    carrier_path = Path(child_workspace.artifact_path("derived-paper.json"))
    atomic_write_json(str(carrier_path), {"artifact_type": "paper_artifact"})
    external_evidence = ArtifactDependencyRefV2(
        dependency_kind="external_job",
        job_id=parent_workspace.job_id,
        artifact_id=evidence.artifact_id,
        artifact_type=evidence.artifact_type,
        path=evidence.path,
        content_hash=evidence.content_hash,
    )
    child_registry.register_file(
        artifact_id="derived-paper:1",
        artifact_role="paper_artifact",
        artifact_type="paper_artifact",
        artifact_version="v1",
        path=carrier_path,
        producer="tests",
        depends_on=[external_evidence],
        external_registry_resolver=lambda job_id: (
            parent_registry if job_id == parent_workspace.job_id else None
        ),
    )
    review, citation = local_records
    dependencies = resolve_validation_input_dependencies(
        child_registry,
        ValidationInputArtifactsV1(
            review_draft_id=review.artifact_id,
            review_draft_hash=review.content_hash,
            citation_manifest_id=citation.artifact_id,
            citation_manifest_hash=citation.content_hash,
            evidence_manifest_ids=(evidence.artifact_id,),
            evidence_manifest_hashes=(evidence.content_hash,),
        ),
        external_registry_resolver=lambda job_id: (
            parent_registry if job_id == parent_workspace.job_id else None
        ),
    )

    assert dependencies[-1] == external_evidence

    second_parent_workspace = JobWorkspace.create(
        str(tmp_path),
        "second-parent",
        job_id="job-second-parent",
    )
    second_parent_registry = ArtifactRegistry(
        second_parent_workspace.paths.registry_path,
        second_parent_workspace.job_id,
    )
    second_evidence_path = Path(second_parent_workspace.artifact_path("evidence.json"))
    atomic_write_json(
        str(second_evidence_path),
        {"artifact_type": "evidence_manifest", "artifact_version": "v1"},
    )
    second_evidence = second_parent_registry.register_file(
        artifact_id=evidence.artifact_id,
        artifact_role="paper_evidence",
        artifact_type="evidence_manifest",
        artifact_version="v1",
        path=second_evidence_path,
        producer="tests",
    )
    assert second_evidence.content_hash == evidence.content_hash
    second_carrier_path = Path(child_workspace.artifact_path("derived-paper-2.json"))
    atomic_write_json(str(second_carrier_path), {"artifact_type": "paper_artifact"})
    child_registry.register_file(
        artifact_id="derived-paper:2",
        artifact_role="paper_artifact",
        artifact_type="paper_artifact",
        artifact_version="v1",
        path=second_carrier_path,
        producer="tests",
        depends_on=[
            replace(
                external_evidence,
                job_id=second_parent_workspace.job_id,
                path=second_evidence.path,
            )
        ],
        external_registry_resolver=lambda job_id: (
            second_parent_registry if job_id == second_parent_workspace.job_id else None
        ),
    )
    external_registries = {
        parent_workspace.job_id: parent_registry,
        second_parent_workspace.job_id: second_parent_registry,
    }
    with pytest.raises(ValidationInputDependencyError, match="ambiguous"):
        resolve_validation_input_dependencies(
            child_registry,
            ValidationInputArtifactsV1(
                review_draft_id=review.artifact_id,
                review_draft_hash=review.content_hash,
                citation_manifest_id=citation.artifact_id,
                citation_manifest_hash=citation.content_hash,
                evidence_manifest_ids=(evidence.artifact_id,),
                evidence_manifest_hashes=(evidence.content_hash,),
            ),
            external_registry_resolver=external_registries.get,
        )

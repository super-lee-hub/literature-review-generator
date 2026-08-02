from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import validator
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRegistry,
    file_sha256,
)
from services.job_workspace import JobWorkspace, atomic_write_json
from services.repair_policy import ValidationRepairPolicy
from validation.run_result import ValidationRunResultV1


def _legacy_claim(*, key: str, status: str) -> SimpleNamespace:
    unit_id = f"unit-{key}"
    return SimpleNamespace(
        citation_set_key=key,
        citation_id=key,
        paper_ids=[f"paper-{key}"],
        block_ids=[f"block-{key}"],
        claim_text=f"claim {key}",
        claim_context=f"context {key}",
        evidence_status=status,
        conclusion=status.upper(),
        disposition="manual_review" if status in {"contradicted", "wrong_source"} else "kept",
        reasoning_summary=f"reason {key}",
        repair_hint="inspect source",
        root_causes=[],
        low_confidence=False,
        claim_units=[{"claim_unit_id": unit_id, "claim_text": f"claim {key}"}],
        details={
            "claim_units": [{"claim_unit_id": unit_id, "claim_text": f"claim {key}"}],
            "claim_unit_results": [
                {
                    "claim_unit_id": unit_id,
                    "citation_set_key": key,
                    "claim_text": f"claim {key}",
                    "checked_paper_ids": [f"paper-{key}"],
                    "evidence_status": status,
                }
            ],
        },
        evidence_candidates=[],
    )


def test_all_validation_projections_derive_from_canonical_json(tmp_path: Path) -> None:
    workspace = SimpleNamespace(
        job_id="job-validation-projections",
        project_name="projection-demo",
        paths=SimpleNamespace(reports_dir=str(tmp_path)),
    )
    generator = SimpleNamespace(job_workspace=workspace, project_name=workspace.project_name)
    legacy_report = SimpleNamespace(
        report_id="validation-projection-demo",
        created_at="2026-07-13T00:00:00Z",
        citation_results=[
            _legacy_claim(key="supported", status="supported"),
            _legacy_claim(key="gap", status="evidence_gap"),
            _legacy_claim(key="contradicted", status="contradicted"),
        ],
    )
    canonical = ValidationRunResultV1.from_report(
        legacy_report,
        job_id=workspace.job_id,
        repair_policy=ValidationRepairPolicy.REPORT_ONLY.value,
    )

    paths = validator._write_validation_reports(
        generator,
        canonical,
        [object()],  # compatibility input must not affect projections
        ValidationRepairPolicy.REPORT_ONLY,
    )

    persisted = ValidationRunResultV1.from_dict(
        json.loads(Path(paths["validation_run_result_file"]).read_text(encoding="utf-8"))
    )
    completion = json.loads(Path(paths["completion_report_file"]).read_text(encoding="utf-8"))
    manual = json.loads(Path(paths["manual_report_file"]).read_text(encoding="utf-8"))
    audit = json.loads(Path(paths["claim_alignment_audit_json"]).read_text(encoding="utf-8"))
    text = Path(paths["report_file"]).read_text(encoding="utf-8")

    assert persisted.to_dict() == canonical.to_dict()
    assert completion["claim_verdict_counts"] == canonical.claim_verdict_counts
    assert completion["canonical_result_hash"] == canonical.stable_hash()
    assert manual["total_items"] == 1
    assert manual["items"][0]["claim_verdict"] == "contradicted"
    assert audit["summary"]["contradicted_rows"] == canonical.contradicted_count == 1
    for verdict, count in canonical.claim_verdict_counts.items():
        assert f"{verdict}: {count}" in text


def test_projection_writer_ignores_legacy_manual_item_list(tmp_path: Path) -> None:
    workspace = SimpleNamespace(
        job_id="job-empty-validation",
        project_name="empty",
        paths=SimpleNamespace(reports_dir=str(tmp_path)),
    )
    generator = SimpleNamespace(job_workspace=workspace, project_name=workspace.project_name)
    canonical = ValidationRunResultV1.from_report(
        SimpleNamespace(report_id="empty", citation_results=[]),
        job_id=workspace.job_id,
    )

    paths = validator._write_validation_reports(
        generator,
        canonical,
        [SimpleNamespace(manual_review_reason="must not leak")],
        ValidationRepairPolicy.REPORT_ONLY,
    )

    manual = json.loads(Path(paths["manual_report_file"]).read_text(encoding="utf-8"))
    assert manual == {
        "eligible_for_manual_apply": False,
        "generated_at": canonical.updated_at,
        "items": [],
        "repair_policy": "report_only",
        "requires_manual_confirmation": False,
        "total_items": 0,
        "unsafe_auto_rewrite_enabled": False,
        "validation_run_id": canonical.validation_run_id,
    }


def test_validator_populates_verified_input_artifact_contract(tmp_path: Path) -> None:
    workspace = JobWorkspace.create(
        str(tmp_path),
        "validation-input-contract",
        job_id="job-input-contract",
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    review_path = workspace.artifact_path("review_draft_v3.json")
    citation_path = workspace.artifact_path("citation_manifest_v3.json")
    evidence_path = workspace.artifact_path("paper_artifacts/paper-a.evidence_manifest_v1.json")
    atomic_write_json(review_path, {"artifact_type": "review_draft", "artifact_version": "v3"})
    atomic_write_json(
        citation_path,
        {
            "artifact_type": "citation_manifest",
            "artifact_version": "v3",
            "citation_sets": [{"citation_set_key": "paper-a", "paper_ids": ["paper-a"]}],
            "occurrences": [],
        },
    )
    atomic_write_json(evidence_path, {"artifact_type": "evidence_manifest", "artifact_version": "v1"})
    review_record = registry.register_file(
        artifact_id="review:v3",
        artifact_role="review_draft",
        artifact_type="review_draft",
        artifact_version="v3",
        path=review_path,
        producer="tests",
    )
    citation_record = registry.register_file(
        artifact_id="citation:v3",
        artifact_role="citation_manifest",
        artifact_type="citation_manifest",
        artifact_version="v3",
        path=citation_path,
        producer="tests",
    )
    evidence_record = registry.register_file(
        artifact_id="evidence:paper-a",
        artifact_role="evidence",
        artifact_type="evidence_manifest",
        artifact_version="v1",
        path=evidence_path,
        producer="tests",
    )
    generator = SimpleNamespace(
        job_workspace=workspace,
        artifact_registry=registry,
        _review_draft_path=lambda: review_path,
        _citation_manifest_path=lambda: citation_path,
    )
    paper_artifact = {
        "paper_identity": {"canonical_paper_key": "paper-a"},
        "stage1_inputs": {
            "evidence_manifest_path": evidence_path,
            "evidence_manifest_hash": file_sha256(evidence_path),
        },
    }

    inputs, expected, has_citations, complete, reasons = validator._validation_input_contract(
        generator,
        json.loads(Path(review_path).read_text(encoding="utf-8")),
        json.loads(Path(citation_path).read_text(encoding="utf-8")),
        [paper_artifact],
    )

    assert inputs.review_draft_id == review_record.artifact_id
    assert inputs.review_draft_hash == review_record.content_hash
    assert inputs.citation_manifest_id == citation_record.artifact_id
    assert inputs.citation_manifest_hash == citation_record.content_hash
    assert inputs.evidence_manifest_ids == (evidence_record.artifact_id,)
    assert inputs.evidence_manifest_hashes == (evidence_record.content_hash,)
    assert expected == 1
    assert has_citations is True
    assert complete is True
    assert reasons == ()

    canonical = ValidationRunResultV1.from_report(
        SimpleNamespace(
            report_id="validation-input-contract",
            citation_results=[_legacy_claim(key="paper-a", status="supported")],
        ),
        job_id=workspace.job_id,
        repair_policy=ValidationRepairPolicy.REPORT_ONLY.value,
        input_artifacts=inputs,
        expected_claim_count=1,
        review_has_citations=True,
        evidence_complete=True,
    )
    validator._write_validation_reports(
        generator,
        canonical,
        [],
        ValidationRepairPolicy.REPORT_ONLY,
    )

    canonical_record = registry.get(canonical.validation_run_id)
    assert canonical_record is not None
    assert canonical_record.status == "ready"
    assert [
        (dependency.artifact_type, dependency.artifact_id, dependency.content_hash)
        for dependency in canonical_record.depends_on
    ] == [
        ("review_draft", review_record.artifact_id, review_record.content_hash),
        ("citation_manifest", citation_record.artifact_id, citation_record.content_hash),
        ("evidence_manifest", evidence_record.artifact_id, evidence_record.content_hash),
    ]


def test_validator_registers_external_evidence_only_with_verified_identity(
    tmp_path: Path,
) -> None:
    parent_workspace = JobWorkspace.create(str(tmp_path), "parent", job_id="job-parent")
    parent_registry = ArtifactRegistry(
        parent_workspace.paths.registry_path,
        parent_workspace.job_id,
    )
    evidence_path = parent_workspace.artifact_path("paper-a.evidence_manifest_v1.json")
    atomic_write_json(
        evidence_path,
        {"artifact_type": "evidence_manifest", "artifact_version": "v1"},
    )
    evidence_record = parent_registry.register_file(
        artifact_id="evidence:paper-a",
        artifact_role="paper_evidence",
        artifact_type="evidence_manifest",
        artifact_version="v1",
        path=evidence_path,
        producer="tests",
    )

    workspace = JobWorkspace.create(str(tmp_path), "child", job_id="job-child")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    review_path = workspace.artifact_path("review_draft_v3.json")
    citation_path = workspace.artifact_path("citation_manifest_v3.json")
    atomic_write_json(review_path, {"artifact_type": "review_draft", "artifact_version": "v3"})
    atomic_write_json(
        citation_path,
        {"artifact_type": "citation_manifest", "artifact_version": "v3"},
    )
    review_record = registry.register_file(
        artifact_id="review:v3",
        artifact_role="review_draft",
        artifact_type="review_draft",
        artifact_version="v3",
        path=review_path,
        producer="tests",
    )
    citation_record = registry.register_file(
        artifact_id="citation:v3",
        artifact_role="citation_manifest",
        artifact_type="citation_manifest",
        artifact_version="v3",
        path=citation_path,
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

    def resolve_parent(job_id: str) -> ArtifactRegistry | None:
        return parent_registry if job_id == parent_workspace.job_id else None

    carrier_path = workspace.artifact_path("derived-paper.json")
    atomic_write_json(carrier_path, {"artifact_type": "paper_artifact"})
    registry.register_file(
        artifact_id="derived-paper:1",
        artifact_role="paper_artifact",
        artifact_type="paper_artifact",
        artifact_version="v1",
        path=carrier_path,
        producer="tests",
        depends_on=[external_evidence],
        external_registry_resolver=resolve_parent,
    )
    generator = SimpleNamespace(
        job_workspace=workspace,
        artifact_registry=registry,
        validation_external_registry_resolver=resolve_parent,
    )
    inputs = {
        "review_draft_id": review_record.artifact_id,
        "review_draft_hash": review_record.content_hash,
        "citation_manifest_id": citation_record.artifact_id,
        "citation_manifest_hash": citation_record.content_hash,
        "evidence_manifest_ids": [evidence_record.artifact_id],
        "evidence_manifest_hashes": [evidence_record.content_hash],
    }
    canonical = ValidationRunResultV1.create(
        job_id=workspace.job_id,
        report_id="validation-external-evidence",
        execution_status="succeeded",
        input_artifacts=inputs,
        review_has_citations=False,
        evidence_complete=True,
    )

    validator._write_validation_reports(
        generator,
        canonical,
        [],
        ValidationRepairPolicy.REPORT_ONLY,
    )

    registered = registry.get(canonical.validation_run_id)
    assert registered is not None
    assert registered.status == "ready"
    assert registered.depends_on[-1] == external_evidence

    mismatched_inputs = dict(inputs)
    mismatched_inputs["evidence_manifest_hashes"] = ["f" * 64]
    mismatched = ValidationRunResultV1.create(
        job_id=workspace.job_id,
        report_id=canonical.validation_run_id,
        execution_status="succeeded",
        input_artifacts=mismatched_inputs,
        review_has_citations=False,
        evidence_complete=True,
    )
    validator._write_validation_reports(
        generator,
        mismatched,
        [],
        ValidationRepairPolicy.REPORT_ONLY,
    )

    quarantined = registry.get(canonical.validation_run_id)
    assert quarantined is not None
    assert quarantined.status == "quarantined"
    assert quarantined.depends_on == []
    assert quarantined.metadata["dependency_error"]


def test_validator_does_not_treat_cited_draft_with_empty_manifest_as_citation_free(
    tmp_path: Path,
) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "cited-empty-manifest", job_id="job-empty")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    review_path = workspace.artifact_path("review_draft_v3.json")
    citation_path = workspace.artifact_path("citation_manifest_v3.json")
    review_draft = {
        "artifact_type": "review_draft",
        "artifact_version": "v3",
        "content": {
            "sections": [
                {
                    "blocks": [
                        {
                            "block_id": "block-1",
                            "text": "Claim [[cite_ref:ref-1]].",
                            "citations": [{"ref_id": "ref-1"}],
                        }
                    ]
                }
            ]
        },
    }
    citation_manifest = {
        "artifact_type": "citation_manifest",
        "artifact_version": "v3",
        "citation_sets": [],
        "occurrences": [],
    }
    atomic_write_json(review_path, review_draft)
    atomic_write_json(citation_path, citation_manifest)
    for artifact_id, artifact_type, path in (
        ("review:v3", "review_draft", review_path),
        ("citation:v3", "citation_manifest", citation_path),
    ):
        registry.register_file(
            artifact_id=artifact_id,
            artifact_role=artifact_type,
            artifact_type=artifact_type,
            artifact_version="v3",
            path=path,
            producer="tests",
        )
    generator = SimpleNamespace(
        job_workspace=workspace,
        artifact_registry=registry,
        _review_draft_path=lambda: review_path,
        _citation_manifest_path=lambda: citation_path,
    )

    _inputs, expected, has_citations, complete, reasons = validator._validation_input_contract(
        generator,
        review_draft,
        citation_manifest,
        [],
    )

    assert expected == 1
    assert has_citations is True
    assert complete is False
    assert "citation_manifest_missing_review_citations" in reasons

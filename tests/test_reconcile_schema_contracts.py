from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from outline.stage_health import OutlineStageHealthV1, make_test_double_entry
from outline.v2_models import FinalOutline, FinalSection
from runtime.reconcile import (
    ReconcileValidationError,
    RuntimeReconciler,
    validate_canonical_ai_summary,
)
from services.artifact_registry import ArtifactRecord, ArtifactRegistry
from services.audit_record import AuditArtifactRefV1, AuditRecordV1
from services.citation_manifest import build_citation_manifest_v3_from_review_draft
from services.citation_ref_catalog import build_document_ref_catalog
from services.evidence_manifest import build_evidence_manifest_v1
from services.job_workspace import JobWorkspace
from services.paper_artifact import build_paper_artifact_v1
from services.review_batch import SummarySelectionSpecV1
from services.review_draft import build_review_draft_v2
from summary_schema import normalize_ai_summary


PayloadFactory = Callable[[Path, str], dict[str, Any]]


def _outline_stage_health_payload(_tmp_path: Path, job_id: str) -> dict[str, Any]:
    entry = make_test_double_entry(
        "outline_map",
        "outline-model",
        {"papers": ["paper-1"]},
        {"nodes": ["node-1"]},
    )
    return OutlineStageHealthV1(
        job_id=job_id,
        execution_mode="test_dev",
        stages=(entry,),
        source_final_outline_hash="final-outline-hash",
        source_coverage_audit_hash="coverage-audit-hash",
    ).to_dict()


def _final_outline_payload(_tmp_path: Path, job_id: str) -> dict[str, Any]:
    return FinalOutline(
        created_from_job_id=job_id,
        outline_id="outline-1",
        source_literature_map_id="literature-map-1",
        source_synthesis_flow_id="synthesis-flow-1",
        source_arbitration_report_id="arbitration-report-1",
        source_literature_map_hash="literature-map-hash",
        source_synthesis_flow_hash="synthesis-flow-hash",
        sections=[FinalSection(section_id="section-1", title="Background")],
    ).to_dict()


def _evidence_manifest_payload(tmp_path: Path, job_id: str) -> dict[str, Any]:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    normalized_text = evidence_dir / "normalized.md"
    chunks = evidence_dir / "chunks.json"
    page_index = evidence_dir / "page_index.json"
    normalized_text.write_text("Source text", encoding="utf-8")
    chunks.write_text("[]", encoding="utf-8")
    page_index.write_text("[]", encoding="utf-8")
    return build_evidence_manifest_v1(
        job_id=job_id,
        canonical_paper_key="paper-1",
        preprocess={
            "markdown_path": str(normalized_text),
            "chunks_path": str(chunks),
            "page_index_path": str(page_index),
        },
    ).to_dict()


def _paper_artifact_payload(_tmp_path: Path, job_id: str) -> dict[str, Any]:
    return build_paper_artifact_v1(
        job_id=job_id,
        paper={
            "source_mode": "direct",
            "source_paper_id": "paper-1",
            "canonical_paper_key": "paper-1",
            "title": "Paper One",
        },
        result={
            "status": "success",
            "text_length": 11,
            "preprocess": {"selected_text_source": "normalized_text"},
            "ai_summary": normalize_ai_summary({"summary": "A summary."}),
            "stage1_input": {"input_mode": "full_text"},
        },
        paper_key="paper-1",
    ).to_dict()


def _review_draft_payload(_tmp_path: Path, job_id: str) -> dict[str, Any]:
    return build_review_draft_v2(
        job_id=job_id,
        project_name="schema-contract",
        draft_id="review-draft-1",
        outline_artifact_id="outline-1",
        outline_source_path="outline.json",
        summary_file="summaries.json",
        review_word_path="review.docx",
        sections=[
            {
                "section_number": 1,
                "section_title": "Background",
                "content": "A review paragraph.",
            }
        ],
        references=[],
        generation_mode="test",
    ).to_dict()


def _citation_manifest_payload(tmp_path: Path, job_id: str) -> dict[str, Any]:
    review_draft = _review_draft_payload(tmp_path, job_id)
    return build_citation_manifest_v3_from_review_draft(
        job_id=job_id,
        project_name="schema-contract",
        manifest_id="citation-manifest-1",
        review_draft_path="review-draft.json",
        review_word_path="review.docx",
        review_draft_v2=review_draft,
        paper_summaries=[],
    ).to_dict()


def _summary_source_manifest_payload(tmp_path: Path, _job_id: str) -> dict[str, Any]:
    summary_path = tmp_path / "materialized-summaries.json"
    summary_path.write_text("[]", encoding="utf-8")
    return {
        "artifact_type": "summary_source_manifest",
        "artifact_version": "v2",
        "created_at": "2026-07-14T00:00:00Z",
        "project_name": "schema-contract",
        "source_kind": "synthetic",
        "source_path": "",
        "source_items": [],
        "rejected_candidates": [],
        "materialized_summary_file": str(summary_path),
        "summary_count": 0,
    }


def _citation_ref_catalog_payload(_tmp_path: Path, job_id: str) -> dict[str, Any]:
    return build_document_ref_catalog(
        [
            {
                "paper_info": {
                    "title": "Paper One",
                    "authors": ["Author One"],
                    "year": "2026",
                    "canonical_paper_key": "paper-1",
                },
                "ai_summary": {"summary": "A summary."},
            }
        ],
        project_name="schema-contract",
        job_id=job_id,
    )


def _summary_selection_payload(tmp_path: Path, job_id: str) -> dict[str, Any]:
    parent_summary = tmp_path / "parent-summaries.json"
    parent_summary.write_text("[]", encoding="utf-8")
    selection = SummarySelectionSpecV1(
        parent_job_id="parent-job",
        parent_registry_path=str(tmp_path / "parent-registry.json"),
        parent_artifact_id="parent-summary",
        parent_content_hash="a" * 64,
        parent_summary_path=str(parent_summary),
        ordered_paper_keys=("paper-1",),
        expected_count=1,
    )
    return {
        "artifact_type": "summary_selection",
        "artifact_version": "v1",
        "schema_version": "summary-selection-v1",
        "project_name": "schema-contract",
        "child_job_id": job_id,
        "created_at": "2026-07-14T00:00:00Z",
        "selection": selection.to_dict(),
        "selected_paper_keys": ["paper-1"],
        "selected_count": 1,
        "stage1_model_calls": 0,
    }


def _invalid_citation_ref_catalog(tmp_path: Path, job_id: str) -> dict[str, Any]:
    payload = _citation_ref_catalog_payload(tmp_path, job_id)
    payload["catalog_hash"] = "0" * 64
    return payload


def _empty_payload(_tmp_path: Path, _job_id: str) -> dict[str, Any]:
    return {}


def _wrong_outline_stage_health_version(tmp_path: Path, job_id: str) -> dict[str, Any]:
    payload = _outline_stage_health_payload(tmp_path, job_id)
    payload["artifact_version"] = "v999"
    return payload


def _final_outline_missing_field(tmp_path: Path, job_id: str) -> dict[str, Any]:
    payload = _final_outline_payload(tmp_path, job_id)
    del payload["outline_id"]
    return payload


def _invalid_evidence_manifest(_tmp_path: Path, job_id: str) -> dict[str, Any]:
    return {
        "artifact_type": "evidence_manifest",
        "artifact_version": "v1",
        "job_id": job_id,
        "canonical_paper_key": "paper-1",
        "artifacts": [],
        "created_at": "2026-07-14T00:00:00Z",
    }


def _type_and_version_only(artifact_type: str, artifact_version: str) -> PayloadFactory:
    def build(_tmp_path: Path, _job_id: str) -> dict[str, Any]:
        return {
            "artifact_type": artifact_type,
            "artifact_version": artifact_version,
        }

    return build


def _registered_record(
    tmp_path: Path,
    *,
    artifact_type: str,
    artifact_version: str,
    payload: dict[str, Any],
) -> tuple[RuntimeReconciler, ArtifactRecord]:
    workspace = JobWorkspace.create(
        str(tmp_path),
        "schema-contract",
        job_id="job-schema-contract",
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    artifact_path = Path(workspace.artifact_path(f"{artifact_type}.json"))
    artifact_path.write_text(json.dumps(payload), encoding="utf-8")
    record = registry.register_file(
        artifact_role=artifact_type,
        artifact_type=artifact_type,
        artifact_version=artifact_version,
        path=artifact_path,
        producer="tests.test_reconcile_schema_contracts",
        artifact_id=f"schema-contract:{artifact_type}",
    )
    return RuntimeReconciler(workspace, registry), record


@pytest.mark.parametrize(
    ("artifact_type", "artifact_version", "payload_factory"),
    [
        ("outline_stage_health", "v1", _empty_payload),
        ("outline_stage_health", "v1", _wrong_outline_stage_health_version),
        ("final_outline", "v2", _final_outline_missing_field),
        ("evidence_manifest", "v1", _invalid_evidence_manifest),
        ("paper_artifact", "v1", _type_and_version_only("paper_artifact", "v1")),
        ("review_draft", "v2", _type_and_version_only("review_draft", "v2")),
        ("citation_manifest", "v3", _type_and_version_only("citation_manifest", "v3")),
        ("summary_source_manifest", "v1", _summary_source_manifest_payload),
        ("citation_ref_catalog", "v1", _invalid_citation_ref_catalog),
        ("summary_selection", "v1", _type_and_version_only("summary_selection", "v1")),
    ],
    ids=[
        "outline-health-empty",
        "outline-health-wrong-version",
        "final-outline-missing-field",
        "evidence-manifest-missing-evidence",
        "paper-artifact-missing-fields",
        "review-draft-missing-fields",
        "citation-manifest-missing-fields",
        "summary-source-registry-version-mismatch",
        "citation-ref-catalog-hash-mismatch",
        "summary-selection-missing-fields",
    ],
)
def test_validate_record_rejects_invalid_canonical_payloads(
    tmp_path: Path,
    artifact_type: str,
    artifact_version: str,
    payload_factory: PayloadFactory,
) -> None:
    job_id = "job-schema-contract"
    reconciler, record = _registered_record(
        tmp_path,
        artifact_type=artifact_type,
        artifact_version=artifact_version,
        payload=payload_factory(tmp_path, job_id),
    )

    with pytest.raises((KeyError, TypeError, ValueError)):
        reconciler.validate_record(record)


@pytest.mark.parametrize(
    ("artifact_type", "artifact_version", "payload_factory"),
    [
        ("outline_stage_health", "v1", _outline_stage_health_payload),
        ("final_outline", "v2", _final_outline_payload),
        ("evidence_manifest", "v1", _evidence_manifest_payload),
        ("paper_artifact", "v1", _paper_artifact_payload),
        ("review_draft", "v2", _review_draft_payload),
        ("citation_manifest", "v3", _citation_manifest_payload),
        ("summary_source_manifest", "v2", _summary_source_manifest_payload),
        ("citation_ref_catalog", "v1", _citation_ref_catalog_payload),
        ("summary_selection", "v1", _summary_selection_payload),
    ],
    ids=[
        "outline-health",
        "final-outline",
        "evidence-manifest",
        "paper-artifact",
        "review-draft",
        "citation-manifest",
        "summary-source-manifest",
        "citation-ref-catalog",
        "summary-selection",
    ],
)
def test_validate_record_accepts_builder_backed_canonical_payloads(
    tmp_path: Path,
    artifact_type: str,
    artifact_version: str,
    payload_factory: PayloadFactory,
) -> None:
    job_id = "job-schema-contract"
    reconciler, record = _registered_record(
        tmp_path,
        artifact_type=artifact_type,
        artifact_version=artifact_version,
        payload=payload_factory(tmp_path, job_id),
    )

    reconciler.validate_record(record)


def test_summary_manifest_resolves_relative_target_from_manifest_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = JobWorkspace.create(
        str(tmp_path / "output"),
        "schema-contract",
        job_id="job-schema-contract",
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    manifest_dir = Path(workspace.artifact_path("nested"))
    manifest_dir.mkdir(parents=True, exist_ok=True)
    summary_path = manifest_dir / "materialized-summaries.json"
    summary_path.write_text("[]", encoding="utf-8")
    manifest_path = manifest_dir / "summary-source-manifest.json"
    payload = _summary_source_manifest_payload(tmp_path, workspace.job_id)
    payload["materialized_summary_file"] = summary_path.name
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    record = registry.register_file(
        artifact_role="summary_source",
        artifact_type="summary_source_manifest",
        artifact_version="v2",
        path=manifest_path,
        producer="tests.test_reconcile_schema_contracts",
        artifact_id="summary-source-manifest",
    )
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    RuntimeReconciler(workspace, registry).validate_record(record)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("extraction_confidence", "high"),
        ("completeness_score", 1.0),
        ("needs_manual_review", False),
    ],
)
def test_canonical_summary_rejects_quality_audit_downgrades(field: str, value: Any) -> None:
    canonical = normalize_ai_summary({})
    tampered = deepcopy(canonical)
    tampered["quality_audit"][field] = value

    with pytest.raises(ReconcileValidationError, match="quality_audit"):
        validate_canonical_ai_summary(tampered, label="summary")


def test_canonical_summary_requires_all_derived_quality_findings() -> None:
    canonical = normalize_ai_summary({})
    missing = canonical["quality_audit"]["missing_critical_fields"]
    assert missing
    tampered = deepcopy(canonical)
    tampered["quality_audit"]["missing_critical_fields"] = missing[1:]

    with pytest.raises(ReconcileValidationError, match="quality_audit"):
        validate_canonical_ai_summary(tampered, label="summary")


def test_canonical_summary_allows_conservative_additive_quality_findings() -> None:
    canonical = normalize_ai_summary({})
    enriched = deepcopy(canonical)
    enriched["quality_audit"]["needs_manual_review"] = True
    enriched["quality_audit"]["missing_critical_fields"].append("paper_metadata.year")
    enriched["quality_audit"]["conflict_flags"].append("metadata_needs_manual_review")
    enriched["quality_audit"]["inferred_fields"].append("runtime.operator_annotation")

    validate_canonical_ai_summary(enriched, label="summary")


def test_audit_record_dependencies_must_match_live_audit_references(tmp_path: Path) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "audit", job_id="job-audit")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    source_path = Path(workspace.artifact_path("legacy-source.json"))
    source_path.write_text(json.dumps([{"status": "success"}]), encoding="utf-8")
    source_record = registry.register_file(
        artifact_role="legacy_summary_source",
        artifact_type="legacy_summary_source",
        artifact_version="v1",
        path=source_path,
        producer="tests",
        artifact_id="legacy-source",
    )
    source_ref = AuditArtifactRefV1(
        artifact_id=source_record.artifact_id,
        artifact_type=source_record.artifact_type,
        job_id=source_record.job_id,
        content_hash=source_record.content_hash,
    )
    audit = AuditRecordV1.create(
        audit_type="legacy_reuse",
        job_id=workspace.job_id,
        attempt_id="attempt-1",
        producer="tests",
        actor="operator",
        reason="explicit reuse",
        scope={"operation": "test"},
        target_artifacts=[source_ref],
        input_artifact_refs=[source_ref],
        disposition="reused_with_audit",
    )
    audit_path = Path(workspace.artifact_path("audit.json"))
    audit_path.write_text(json.dumps(audit.to_dict()), encoding="utf-8")
    audit_record = registry.register_file(
        artifact_role="audit_record",
        artifact_type="audit_record",
        artifact_version="v1",
        path=audit_path,
        producer="tests",
        artifact_id=audit.audit_id,
        depends_on=(),
    )

    with pytest.raises(ReconcileValidationError, match="dependencies"):
        RuntimeReconciler(workspace, registry).validate_record(audit_record)

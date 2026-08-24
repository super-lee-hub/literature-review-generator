from __future__ import annotations

import pytest

from services.stage1_reuse import (
    Stage1ReusableSummaryBindingV1,
    Stage1VisualEvidenceQualificationV1,
    _verify_visual_evidence_qualification,
)


def test_prompt_and_visual_hash_changes_block_exact_reuse() -> None:
    original = Stage1ReusableSummaryBindingV1(
        canonical_paper_key="paper",
        source_mode="direct",
        source_pdf_content_sha256="a" * 64,
        stage1_extracted_text_hash="b" * 64,
        stage1_semantic_input_hash="c" * 64,
        preprocess_contract_hash="d" * 64,
        prompt_id="stage1.analysis.user.v3",
        prompt_version="v3",
        prompt_sha256="e" * 64,
        prompt_template_hash="f" * 64,
        input_builder_policy_hash="1" * 64,
        summary_schema_hash="2" * 64,
        visual_input_manifest_hash="3" * 64,
        visual_coverage_hash="4" * 64,
        provider="Primary_Reader_API",
        model="deepseek-v4-flash-vision-exp",
        endpoint_type="chat_completions",
        provider_config_hash="5" * 64,
    )
    current = Stage1ReusableSummaryBindingV1.from_mapping(
        {
            **original.to_dict(),
            "prompt_version": "v4",
            "visual_coverage_hash": "6" * 64,
        }
    )
    result = original.compare(current)
    assert result["equal"] is False
    assert result["mismatches"]["prompt_version"]
    assert result["mismatches"]["visual_coverage_hash"]


def test_v1_visual_observation_qualification_is_invalid_under_current_v2_contract() -> None:
    qualification = Stage1VisualEvidenceQualificationV1(
        required_nonblank_page_count=1,
        required_page_ids=("page-001",),
        sent_page_ids=("page-001",),
        observed_page_ids=("page-001",),
        scan_coverage_status="complete",
        evidence_coverage_status="complete",
        visual_observation_artifact_version="v1",
        visual_scan_prompt_id="stage1.visual_scan.system.v1",
        visual_scan_prompt_version="v1",
        visual_scan_prompt_sha256="a" * 64,
        visual_scan_schema_hash="b" * 64,
    )

    verified, reason = _verify_visual_evidence_qualification(qualification.to_dict())

    assert verified is False
    assert reason == "prior_visual_observation_contract_invalid"


def test_explicit_degraded_policy_allows_unresolved_raw_unit_reuse_gate() -> None:
    qualification = Stage1VisualEvidenceQualificationV1(
        require_complete_visual_coverage=False,
        evidence_coverage_status="degraded",
        required_raw_reinspection_unit_count=1,
        closed_raw_reinspection_unit_count=0,
        unresolved_raw_reinspection_unit_ids=("ambiguous-page-4",),
        raw_reinspection_units=(
            {"unit_id": "ambiguous-page-4", "closed": False},
        ),
    )

    assert "raw_reinspection_units_unresolved" not in qualification.qualification_issues()
    assert qualification.complete_for_reuse() is True


def test_explicit_degraded_policy_allows_typed_raw_transport_omission() -> None:
    qualification = Stage1VisualEvidenceQualificationV1(
        require_complete_visual_coverage=False,
        required_nonblank_page_count=1,
        required_page_ids=("page-004",),
        sent_page_ids=("page-004",),
        observed_page_ids=("page-004",),
        scan_coverage_status="complete",
        evidence_coverage_status="degraded",
        visual_observation_artifact_version="v2",
        visual_scan_prompt_id="stage1.visual_scan.system.v2",
        visual_scan_prompt_version="v2",
        visual_scan_prompt_sha256="a" * 64,
        visual_scan_schema_hash="b" * 64,
        required_raw_reinspection_unit_count=1,
        closed_raw_reinspection_unit_count=0,
        unresolved_raw_reinspection_unit_ids=("ambiguous-page-4",),
        raw_reinspection_units=(
            {"unit_id": "ambiguous-page-4", "closed": False},
        ),
        transport_omissions=(
            {
                "visual_id": "figure-4-a",
                "page_no": 4,
                "reason": "raw_reinspection_group_not_represented",
                "scope": "raw_reinspection",
                "authority_blocking": False,
                "raw_reinspection_group_id": "ambiguous-page-4",
                "raw_reinspection_resolution": "not_represented",
            },
        ),
    )

    assert "raw_reinspection_transport_omitted" not in qualification.qualification_issues()
    assert qualification.complete_for_reuse() is True


@pytest.mark.parametrize(
    ("scan_coverage_status", "evidence_coverage_status"),
    [("partial", "degraded"), ("complete", "complete")],
)
def test_relaxed_raw_omission_requires_complete_degraded_authority(
    scan_coverage_status: str,
    evidence_coverage_status: str,
) -> None:
    qualification = Stage1VisualEvidenceQualificationV1(
        require_complete_visual_coverage=False,
        required_nonblank_page_count=1,
        required_page_ids=("page-004",),
        sent_page_ids=("page-004",),
        observed_page_ids=("page-004",),
        scan_coverage_status=scan_coverage_status,
        evidence_coverage_status=evidence_coverage_status,
        visual_observation_artifact_version="v2",
        visual_scan_prompt_id="stage1.visual_scan.system.v2",
        visual_scan_prompt_version="v2",
        visual_scan_prompt_sha256="a" * 64,
        visual_scan_schema_hash="b" * 64,
        required_raw_reinspection_unit_count=1,
        closed_raw_reinspection_unit_count=0,
        unresolved_raw_reinspection_unit_ids=("ambiguous-page-4",),
        raw_reinspection_units=(
            {"unit_id": "ambiguous-page-4", "closed": False},
        ),
        transport_omissions=(
            {
                "visual_id": "figure-4-a",
                "page_no": 4,
                "reason": "raw_reinspection_group_not_represented",
                "scope": "raw_reinspection",
                "authority_blocking": False,
                "raw_reinspection_group_id": "ambiguous-page-4",
                "raw_reinspection_resolution": "not_represented",
            },
        ),
    )

    assert "raw_reinspection_relaxed_authority_invalid" in qualification.qualification_issues()
    assert qualification.complete_for_reuse() is False


def test_raw_reinspection_omission_must_bind_unresolved_unit() -> None:
    qualification = Stage1VisualEvidenceQualificationV1(
        require_complete_visual_coverage=False,
        scan_coverage_status="complete",
        evidence_coverage_status="degraded",
        required_raw_reinspection_unit_count=1,
        closed_raw_reinspection_unit_count=0,
        unresolved_raw_reinspection_unit_ids=("ambiguous-page-4",),
        raw_reinspection_units=(
            {"unit_id": "ambiguous-page-4", "closed": False},
        ),
        transport_omissions=(
            {
                "visual_id": "figure-4-a",
                "page_no": 4,
                "reason": "raw_reinspection_group_not_represented",
                "scope": "raw_reinspection",
                "authority_blocking": False,
                "raw_reinspection_group_id": "different-unit",
            },
        ),
    )

    assert "raw_reinspection_relaxed_authority_invalid" in qualification.qualification_issues()
    assert qualification.complete_for_reuse() is False


@pytest.mark.parametrize("scope", ["page_coverage", "final_transport"])
def test_non_raw_omissions_cannot_be_nonblocking(scope: str) -> None:
    qualification = Stage1VisualEvidenceQualificationV1(
        require_complete_visual_coverage=False,
        transport_omissions=(
            {
                "visual_id": "visual-1",
                "page_no": 1,
                "reason": "transport_omission",
                "scope": scope,
                "authority_blocking": False,
            },
        ),
    )

    assert "transport_omission_contract_invalid" in qualification.qualification_issues()
    assert qualification.complete_for_reuse() is False


def test_degraded_policy_does_not_relax_typed_page_coverage_omission() -> None:
    qualification = Stage1VisualEvidenceQualificationV1(
        require_complete_visual_coverage=False,
        scan_coverage_status="partial",
        evidence_coverage_status="incomplete",
        transport_omissions=(
            {
                "visual_id": "page-4",
                "page_no": 4,
                "reason": "scan_failed",
                "scope": "page_coverage",
                "authority_blocking": True,
            },
        ),
    )

    assert "page_coverage_transport_omitted" in qualification.qualification_issues()
    assert qualification.complete_for_reuse() is False

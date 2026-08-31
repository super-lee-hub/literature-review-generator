from __future__ import annotations

import pytest

from ai_interface import _typed_transport_omission
from services.stage1_reuse import (
    Stage1ReusableSummaryBindingV1,
    Stage1VisualEvidenceQualificationV1,
    evaluate_stage1_reuse,
    _verify_visual_evidence_qualification,
)


def _coherent_relaxed_qualification() -> dict[str, object]:
    return Stage1VisualEvidenceQualificationV1(
        require_complete_visual_coverage=False,
        required_nonblank_page_count=1,
        required_page_ids=("page-004",),
        sent_page_ids=("page-004",),
        observed_page_ids=("page-004",),
        scan_coverage_status="complete",
        final_raw_visual_recheck_status="partial",
        evidence_coverage_status="degraded",
        visual_observation_artifact_version="v2",
        visual_scan_prompt_id="stage1.visual_scan.system.v3",
        visual_scan_prompt_version="v3",
        visual_scan_prompt_sha256="a" * 64,
        visual_scan_schema_hash="b" * 64,
        required_raw_reinspection_unit_count=1,
        closed_raw_reinspection_unit_count=0,
        unresolved_raw_reinspection_unit_ids=("ambiguous-page-4",),
        raw_reinspection_units=(
            {"unit_id": "ambiguous-page-4", "closed": False},
        ),
    ).to_dict()


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
    qualification = Stage1VisualEvidenceQualificationV1.from_mapping(
        _coherent_relaxed_qualification()
    )

    assert "raw_reinspection_units_unresolved" not in qualification.qualification_issues()
    assert qualification.complete_for_reuse() is True


@pytest.mark.parametrize(
    ("final_status", "evidence_status"),
    [
        ("complete", "degraded"),
        ("not_required", "degraded"),
        ("partial", "complete"),
    ],
)
def test_unresolved_raw_unit_requires_noncomplete_achieved_state(
    final_status: str,
    evidence_status: str,
) -> None:
    payload = _coherent_relaxed_qualification()
    payload["final_raw_visual_recheck_status"] = final_status
    payload["evidence_coverage_status"] = evidence_status

    qualification = Stage1VisualEvidenceQualificationV1.from_mapping(payload)

    assert "raw_reinspection_state_invalid" in qualification.qualification_issues()
    assert qualification.complete_for_reuse() is False


def test_relaxed_unresolved_raw_unit_requires_complete_scan_without_omission() -> None:
    payload = _coherent_relaxed_qualification()
    payload["scan_coverage_status"] = "partial"

    qualification = Stage1VisualEvidenceQualificationV1.from_mapping(payload)

    assert "raw_reinspection_relaxed_authority_invalid" in qualification.qualification_issues()
    assert qualification.complete_for_reuse() is False


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("require_complete_visual_coverage", "truue"),
        ("require_complete_visual_coverage", "false"),
        ("require_complete_visual_coverage", 0),
        ("require_complete_visual_coverage", None),
        ("required_raw_reinspection_unit_count", "1"),
        ("required_raw_reinspection_unit_count", True),
        ("closed_raw_reinspection_unit_count", "garbage"),
        ("required_nonblank_page_count", "garbage"),
    ],
)
def test_current_visual_authority_rejects_malformed_scalar_types(
    field_name: str,
    value: object,
) -> None:
    payload = _coherent_relaxed_qualification()
    payload[field_name] = value

    with pytest.raises(ValueError, match="current visual evidence qualification"):
        Stage1VisualEvidenceQualificationV1.from_current_mapping_strict(payload)


@pytest.mark.parametrize(
    "field_name",
    [
        "required_page_ids",
        "unresolved_raw_reinspection_unit_ids",
        "raw_reinspection_units",
        "transport_omissions",
    ],
)
def test_current_visual_authority_rejects_non_array_fields(field_name: str) -> None:
    payload = _coherent_relaxed_qualification()
    payload[field_name] = {"not": "an array"}

    with pytest.raises(ValueError, match="current visual evidence qualification"):
        Stage1VisualEvidenceQualificationV1.from_current_mapping_strict(payload)


@pytest.mark.parametrize(
    "field_name",
    ["required_page_ids", "unresolved_raw_reinspection_unit_ids"],
)
def test_current_visual_authority_rejects_tuple_json_arrays(field_name: str) -> None:
    payload = _coherent_relaxed_qualification()
    payload[field_name] = tuple(payload[field_name])  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="current visual evidence qualification"):
        Stage1VisualEvidenceQualificationV1.from_current_mapping_strict(payload)


@pytest.mark.parametrize("qualification_state", ["missing", "empty"])
def test_current_visual_binding_cannot_downgrade_missing_qualification_to_legacy(
    qualification_state: str,
) -> None:
    binding = Stage1ReusableSummaryBindingV1(
        canonical_paper_key="paper-a",
        source_mode="direct",
        prompt_id="stage1.analysis.user.v3",
        prompt_version="v3",
        prompt_sha256="a" * 64,
        visual_coverage_hash="b" * 64,
        visual_scan_schema_hash="c" * 64,
        extra={"require_complete_visual_coverage": True},
    )
    raw_binding = binding.to_dict()
    if qualification_state == "missing":
        raw_binding.pop("visual_evidence_qualification", None)
    else:
        raw_binding["visual_evidence_qualification"] = {}
    previous = {
        "paper_info": {"canonical_paper_key": "paper-a"},
        "stage1_reuse": {"binding": raw_binding},
    }

    eligibility = evaluate_stage1_reuse(previous, binding)

    assert eligibility.reusable is False
    assert eligibility.reason == "current_visual_evidence_qualification_missing"


def test_genuine_legacy_binding_without_current_visual_markers_keeps_legacy_path() -> None:
    binding = Stage1ReusableSummaryBindingV1(
        canonical_paper_key="paper-a",
        source_mode="direct",
        source_pdf_hash="legacy-semantic-hash",
        visual_provenance_hash="legacy-visual-hash",
    )
    previous = {
        "paper_info": {"canonical_paper_key": "paper-a"},
        "stage1_reuse": {"binding": binding.to_dict()},
    }

    eligibility = evaluate_stage1_reuse(previous, binding)

    assert eligibility.reusable is False
    assert eligibility.reason == "source_authority_artifact_id_missing"


def test_malformed_current_policy_blocks_reuse_before_projection() -> None:
    malformed = _coherent_relaxed_qualification()
    malformed["require_complete_visual_coverage"] = "truue"
    binding = Stage1ReusableSummaryBindingV1(
        canonical_paper_key="paper-a",
        source_mode="direct",
    )
    previous = {
        "paper_info": {"canonical_paper_key": "paper-a"},
        "stage1_reuse": {
            "binding": {
                **binding.to_dict(),
                "visual_evidence_qualification": malformed,
            }
        },
    }

    eligibility = evaluate_stage1_reuse(previous, binding)

    assert eligibility.reusable is False
    assert eligibility.reason == "current_visual_evidence_qualification_invalid"


def test_typed_transport_omission_resolves_protected_scope_and_authority_fields() -> None:
    omission = _typed_transport_omission(
        {
            "visual_id": "source-visual",
            "page_no": 1,
            "raw_reinspection_group_id": "unit-1",
            "transport_omission_scope": "page_coverage",
            "transport_omission_authority_blocking": True,
        },
        reason="raw_reinspection_group_not_represented",
        visual_id="requested-visual",
        page_no=4,
        scope="final_transport",
        authority_blocking=False,
        raw_reinspection_group_id="unit-1",
        raw_reinspection_resolution="not_represented",
    )

    assert omission["visual_id"] == "requested-visual"
    assert omission["page_no"] == 4
    assert omission["reason"] == "raw_reinspection_group_not_represented"
    assert omission["scope"] == "raw_reinspection"
    assert omission["authority_blocking"] is False
    assert omission["raw_reinspection_group_id"] == "unit-1"


def test_explicit_degraded_policy_allows_typed_raw_transport_omission() -> None:
    qualification = Stage1VisualEvidenceQualificationV1(
        require_complete_visual_coverage=False,
        required_nonblank_page_count=1,
        required_page_ids=("page-004",),
        sent_page_ids=("page-004",),
        observed_page_ids=("page-004",),
        scan_coverage_status="complete",
        final_raw_visual_recheck_status="partial",
        evidence_coverage_status="degraded",
        visual_observation_artifact_version="v2",
        visual_scan_prompt_id="stage1.visual_scan.system.v3",
        visual_scan_prompt_version="v3",
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
        visual_scan_prompt_id="stage1.visual_scan.system.v3",
        visual_scan_prompt_version="v3",
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

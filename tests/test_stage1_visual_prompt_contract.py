"""Regression tests for the Stage 1 visual prompt/schema authority."""

from __future__ import annotations

import pytest

from services.prompt_registry import PromptRegistry
from services.stage1_reuse import (
    Stage1VisualEvidenceQualificationV1,
    _verify_visual_evidence_qualification,
)
from services.stage1_visual_scan import (
    VisualScanBatch,
    VISUAL_OBSERVATIONS_VERSION,
    VISUAL_SCAN_PROMPT_ID,
    build_visual_scan_prompt,
    validate_current_visual_observations_v2,
)
from services.stage1_visual_schema import VISUAL_EVIDENCE_KINDS, visual_evidence_kinds_json


def _payload(*, evidence_kinds: list[str]) -> dict[str, object]:
    return {
        "artifact_type": "stage1_visual_observations",
        "artifact_version": VISUAL_OBSERVATIONS_VERSION,
        "observations": [
            {
                "visual_id": "page-005",
                "page_no": 5,
                "bbox": [0, 0, 100, 100],
                "artifact_type": "page_snapshot",
                "visible_text": ["Figure 1"],
                "title_or_caption": "Figure 1",
                "axes_or_headers": [],
                "legend_or_notes": [],
                "quantitative_values": [],
                "relationships": ["A -> B"],
                "layout_observations": [],
                "ocr_conflicts": [],
                "confidence": "high",
                "needs_manual_review": False,
                "candidate_attribution_status": "resolved",
                "raw_reinspection_candidates": [
                    {
                        "candidate_visual_id": "figure-005-06",
                        "evidence_kinds": evidence_kinds,
                        "reason": "The page caption and visible arrow match the candidate.",
                        "confidence": "high",
                        "requires_raw_reinspection": True,
                    }
                ],
            }
        ],
    }


def _visual_refs() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    expected = [
        {
            "visual_id": "page-005",
            "page_no": 5,
            "bbox": [0, 0, 100, 100],
            "artifact_type": "page_snapshot",
        },
    ]
    candidates = [
        {
            "visual_id": "figure-005-06",
            "page_no": 5,
            "bbox": [10, 10, 90, 90],
            "artifact_type": "figure_crop",
        },
    ]
    return expected, candidates


def test_active_visual_prompt_renders_the_validator_enum_from_one_source() -> None:
    registry = PromptRegistry()
    assert VISUAL_SCAN_PROMPT_ID == "stage1.visual_scan.system.v3"
    rendered = registry.render(
        VISUAL_SCAN_PROMPT_ID,
        {"EVIDENCE_KINDS_JSON": visual_evidence_kinds_json()},
    )

    for kind in VISUAL_EVIDENCE_KINDS:
        assert f'"{kind}"' in rendered
    assert "绝不能把任何 visual_id" in rendered
    assert "figure-005-06" in rendered

    _, production_system = build_visual_scan_prompt(
        VisualScanBatch(
            batch_index=0,
            visual_refs=(
                {
                    "visual_id": "page-005",
                    "page_no": 5,
                    "bbox": [0, 0, 100, 100],
                    "artifact_type": "page_snapshot",
                },
            ),
        )
    )
    assert "{{EVIDENCE_KINDS_JSON}}" not in production_system
    assert production_system == rendered


def test_visual_prompt_v2_is_retained_as_legacy_not_active() -> None:
    identity = PromptRegistry().identity(
        "stage1.visual_scan.system.v2",
        allow_non_active=True,
    )
    assert identity.status == "LEGACY"


def test_v2_prompt_qualification_cannot_be_reused_under_v3_authority() -> None:
    qualification = Stage1VisualEvidenceQualificationV1(
        require_complete_visual_coverage=False,
        required_nonblank_page_count=1,
        required_page_ids=("page-001",),
        sent_page_ids=("page-001",),
        observed_page_ids=("page-001",),
        scan_coverage_status="complete",
        final_raw_visual_recheck_status="not_required",
        evidence_coverage_status="complete",
        visual_observation_artifact_version="v2",
        visual_scan_prompt_id="stage1.visual_scan.system.v2",
        visual_scan_prompt_version="v2",
        visual_scan_prompt_sha256="a" * 64,
        visual_scan_schema_hash="b" * 64,
    )

    verified, reason = _verify_visual_evidence_qualification(qualification.to_dict())

    assert verified is False
    assert reason == "prior_visual_observation_contract_invalid"


@pytest.mark.parametrize("evidence_kinds", [["figure-005-06"], ["table-005-01"]])
def test_candidate_visual_id_cannot_be_used_as_evidence_kind(
    evidence_kinds: list[str],
) -> None:
    expected, candidates = _visual_refs()

    with pytest.raises(ValueError, match="evidence_kinds is invalid"):
        validate_current_visual_observations_v2(
            _payload(evidence_kinds=evidence_kinds),
            allowed_visual_ids=["page-005"],
            expected_visual_refs=expected,
            sent_visual_ids=["page-005"],
            candidate_refs=candidates,
        )


def test_evidence_kind_enum_value_is_accepted_for_candidate_attribution() -> None:
    expected, candidates = _visual_refs()

    normalized = validate_current_visual_observations_v2(
        _payload(evidence_kinds=["relationships"]),
        allowed_visual_ids=["page-005"],
        expected_visual_refs=expected,
        sent_visual_ids=["page-005"],
        candidate_refs=candidates,
    )

    assert normalized["observations"][0]["raw_reinspection_candidates"][0]["evidence_kinds"] == [
        "relationships"
    ]

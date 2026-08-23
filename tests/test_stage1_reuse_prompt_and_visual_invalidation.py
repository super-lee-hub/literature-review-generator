from __future__ import annotations

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

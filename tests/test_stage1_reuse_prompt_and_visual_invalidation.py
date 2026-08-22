from __future__ import annotations

from services.stage1_reuse import Stage1ReusableSummaryBindingV1


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

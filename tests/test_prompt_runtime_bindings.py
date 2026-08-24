from __future__ import annotations

from pathlib import Path

from services.prompt_registry import PromptRegistry
from services.stage1_input_builder import Stage1InputBuilder
from services.stage1_reuse import Stage1ReusableSummaryBindingV1
from summary_schema import build_summary_schema_contract


def test_stage1_builder_persists_prompt_identity_and_replaces_contract() -> None:
    registry = PromptRegistry()
    identity = registry.identity("stage1.analysis.user.v3")
    template = registry.read("stage1.analysis.user.v3")
    built = Stage1InputBuilder().build(
        prompt_template=template,
        paper_text="evidence text",
        reader_api_config={
            "model": "deepseek-v4-flash-vision-exp",
            "api_base": "https://api.deepseek.com",
            "provider_family": "deepseek",
        },
        stage1_input_settings={"send_selected_visuals": "false"},
        prompt_identity=identity.to_dict(),
        prompt_values={
            "SUMMARY_SCHEMA_CONTRACT": build_summary_schema_contract(),
            "VISUAL_COVERAGE_JSON": "{}",
        },
    )
    metadata = built.to_metadata_dict()
    assert metadata["prompt_id"] == identity.prompt_id
    assert metadata["prompt_version"] == identity.version
    assert metadata["prompt_sha256"] == identity.sha256
    assert "{{" not in built.prompt_text


def test_prompt_and_visual_coverage_changes_are_binding_mismatches() -> None:
    base = Stage1ReusableSummaryBindingV1(
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
        model="reader",
        endpoint_type="chat_completions",
        provider_config_hash="5" * 64,
    )
    changed = Stage1ReusableSummaryBindingV1.from_mapping(
        {**base.to_dict(), "prompt_sha256": "6" * 64, "visual_coverage_hash": "7" * 64}
    )
    comparison = base.compare(changed)
    assert comparison["equal"] is False
    assert "prompt_sha256" in comparison["mismatches"]
    assert "visual_coverage_hash" in comparison["mismatches"]

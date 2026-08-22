from __future__ import annotations

from services.prompt_registry import PromptRegistry
from summary_schema import build_summary_schema_contract


def test_stage1_prompt_is_evidence_bound_and_schema_is_dynamic() -> None:
    registry = PromptRegistry()
    system = registry.read("stage1.analysis.system.v3")
    user = registry.read("stage1.analysis.user.v3")
    forbidden = ("合理估计", "宁可给出", "必须填满", "合理推断", "预估一个")
    assert all(marker not in system + user for marker in forbidden)
    assert "summary_v2_lite" in system + user
    contract = build_summary_schema_contract()
    assert '"schema_version":"summary_v2_lite"' in contract
    assert '"invented_facts":"forbidden"' in contract


def test_review_and_validation_prompt_contracts_are_aligned() -> None:
    registry = PromptRegistry()
    review = registry.read("review.section_writer.system.v3")
    validation = registry.read("validation.adjudicator.system.v2")
    assert '"blocks"' in review
    assert "[[cite_ref:R###]]" in review
    for value in ("supported", "partial_support", "evidence_gap", "wrong_source", "low_confidence"):
        assert value in validation
    assert "image_url" not in validation
    assert "local_image_path" not in validation


def test_free_mode_system_prompts_are_registry_owned() -> None:
    registry = PromptRegistry()
    chat = registry.read("free_mode.chat.system.v1")
    profile = registry.read("free_mode.profile.system.v1")
    assert "assistant_message" in chat
    assert "generated_prompt" in chat + profile

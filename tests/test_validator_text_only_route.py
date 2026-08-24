from __future__ import annotations

from types import SimpleNamespace

import validation.llm_adjudicator as llm_adjudicator
from validation.llm_adjudicator import AdjudicationPacket


def test_validator_adjudicator_does_not_send_image_content(monkeypatch) -> None:
    observed: list[dict[str, object]] = []

    def fake_call(prompt, api_config, system_prompt, **kwargs):
        observed.append(dict(kwargs))
        return {
            "status": "supported",
            "confidence": 0.9,
            "repair_scope": "none",
            "disposition": "keep_as_is",
            "low_confidence": False,
            "reasoning": "source evidence supports the claim",
            "repair_hint": "",
            "summary_paper_ids": ["paper-1"],
            "manual_review_reason": "",
            "claim_type": "result",
            "claim_type_confidence": 0.9,
            "claim_type_rationale": "explicit result",
            "adjudication_status": "supported",
        }

    monkeypatch.setattr(llm_adjudicator, "_call_ai_api", fake_call)
    packet = AdjudicationPacket(
        citation_set_key="set-1",
        stage="primary",
        claim_text="The cited result is supported.",
        claim_context="",
        block_context="",
        claim_type="result",
        claim_type_confidence=0.9,
        claim_type_rationale="explicit result",
        paper_ids=["paper-1"],
        claim_units=[],
        target_claim_unit={},
        claim_unit_results=[],
        paper_identity_hints={},
        per_paper_evidence_packets={"paper-1": {"claim_unit": [{"text_excerpt": "evidence"}]}},
        evidence_excerpt_list=["evidence"],
        trimmed_candidate_counts={},
        evidence_status="supported",
        disposition="keep_as_is",
    )
    service = SimpleNamespace(logger=None)
    report = llm_adjudicator.run_adjudication_stage(service, {"model": "deepseek-v4-flash"}, packet)
    assert report is not None
    assert observed
    assert observed[0].get("user_content") is None

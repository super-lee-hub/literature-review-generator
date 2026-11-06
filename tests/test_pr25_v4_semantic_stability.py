"""Scientific stability compares typed evidence and direction, not prose titles."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from test_outline_v3_semantic_execution import (
    _configured_test_provider,
    _executor,
)

from outline.v3_critique import derive_critique_disposition
from outline.v3_executor import OutlineV3Executor


def _section(claim: str) -> dict:
    return {
        "section_id": "S1",
        "title": "Treatment synthesis",
        "goal": "Explain the bounded effect.",
        "paper_keys": ["paper-A"],
        "relation_ids": ["relation-A"],
        "claims": [claim],
        "claim_support": [{
            "claim": claim,
            "paper_key": "paper-A",
            "study_id": "study-1",
            "source_claim_ids": ["source-claim-1"],
            "evidence_ids": ["evidence-1"],
            "source_field_ids": ["field-1"],
            "condition_ids": ["condition-1"],
            "claim_kind": "effect",
            "effect_direction": "positive",
        }],
    }


def _facts(section: dict) -> list[str]:
    return OutlineV3Executor._stability_fact_inventory([section])["fact_hashes"]


def _artifact_payload(result: Any, name: str) -> dict:
    envelope = json.loads(
        Path(result.artifacts[name]).read_text(encoding="utf-8")
    )
    assert isinstance(envelope.get("payload"), dict)
    return envelope["payload"]


def test_typed_supported_paraphrase_and_title_change_keep_fact_identity() -> None:
    original = _section("Treatment improves the measured outcome.")
    paraphrase = _section("The measured outcome improves under treatment.")
    paraphrase["title"] = "Treatment: synthesis"
    paraphrase["goal"] = "Summarize the conditional effect."
    assert _facts(original) == _facts(paraphrase)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("effect_direction", "negative"),
        ("source_claim_ids", ["source-claim-other"]),
        ("condition_ids", ["condition-other"]),
        ("paper_key", "paper-B"),
        ("study_id", "study-2"),
    ],
)
def test_fact_direction_source_and_condition_changes_are_not_equivalent(
    field: str, replacement: object,
) -> None:
    original = _section("Treatment improves the measured outcome.")
    changed = deepcopy(original)
    changed["claim_support"][0][field] = replacement
    assert _facts(original) != _facts(changed)


def test_untyped_claim_rewording_requires_review() -> None:
    original = _section("Treatment improves the measured outcome.")
    original["claim_support"] = []
    changed = deepcopy(original)
    changed["claims"] = ["Outcome improves with the treatment."]
    assert _facts(original) != _facts(changed)


def _typed_claim_pair(baseline_text: str, variant_text: str) -> tuple[dict, dict]:
    baseline = _section(baseline_text)
    variant = _section(variant_text)
    variant["claim_support"][0]["claim"] = variant_text
    return baseline, variant


def test_typed_directional_claim_drift_uses_bounded_existing_critic_review() -> None:
    baseline, variant = _typed_claim_pair(
        "Treatment improves the measured outcome.",
        "Treatment worsens the measured outcome.",
    )
    # Typed support metadata alone remains equal; a bounded semantic reviewer
    # must evaluate the changed proposition before it can remain eligible.
    assert _facts(baseline) == _facts(variant)
    catalog, pairs = OutlineV3Executor._stability_claim_review_material(
        [baseline], [variant], candidate_id="candidate_1"
    )
    assert len(catalog) == 1
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair["evidence_refs"] == ["evidence-1"]
    assert pair["variant_claim_refs"] == [{"section_id": "S1", "claim_index": 0}]

    critique = {
        "passed": True,
        "issues": [],
        "stability_claim_reviews": [{
            "candidate_id": "candidate_1",
            "pair_id": pair["pair_id"],
            "decision": "material_change",
            "evidence_refs": ["evidence-1"],
            "rationale": "The statement reverses the effect direction.",
        }],
    }
    review = OutlineV3Executor._enforce_stability_claim_reviews(
        critique,
        comparisons={"candidate_1": pairs},
        candidate_hashes={"candidate_1": "candidate-hash"},
    )
    disposition = derive_critique_disposition(
        {"evidence_critique": critique},
        candidate_hashes={"candidate_1": "candidate-hash"},
        candidate_contents={"candidate_1": {
            "candidate_id": "candidate_1",
            "sections": [variant],
        }},
    )
    assert review["candidate_statuses"] == {"candidate_1": "blocked"}
    assert disposition["eligible_candidate_ids"] == []
    assert any(issue["scope"] == "candidate" for issue in disposition["issues"])


def test_benign_typed_paraphrase_can_pass_only_with_complete_equivalence_review() -> None:
    baseline, variant = _typed_claim_pair(
        "Treatment improves the measured outcome.",
        "The measured outcome improves under treatment.",
    )
    catalog, pairs = OutlineV3Executor._stability_claim_review_material(
        [baseline], [variant], candidate_id="candidate_1"
    )
    assert catalog and len(pairs) == 1
    pair = pairs[0]
    critique = {
        "passed": True,
        "issues": [],
        "stability_claim_reviews": [{
            "candidate_id": "candidate_1",
            "pair_id": pair["pair_id"],
            "decision": "equivalent",
            "evidence_refs": ["evidence-1"],
            "rationale": "Both statements preserve the same positive effect and scope.",
        }],
    }
    review = OutlineV3Executor._enforce_stability_claim_reviews(
        critique,
        comparisons={"candidate_1": pairs},
        candidate_hashes={"candidate_1": "candidate-hash"},
    )
    disposition = derive_critique_disposition(
        {"evidence_critique": critique},
        candidate_hashes={"candidate_1": "candidate-hash"},
        candidate_contents={"candidate_1": {
            "candidate_id": "candidate_1",
            "sections": [variant],
        }},
    )
    assert review["candidate_statuses"] == {"candidate_1": "equivalent"}
    assert disposition["eligible_candidate_ids"] == ["candidate_1"]


@pytest.mark.parametrize(
    "reviews",
    [[], ["duplicate", "duplicate"], ["malformed_extra"]],
    ids=["missing", "duplicate", "malformed-extra"],
)
def test_missing_or_duplicate_stability_pair_results_block_candidate(reviews: list) -> None:
    baseline, variant = _typed_claim_pair(
        "Treatment improves the measured outcome.",
        "The measured outcome improves under treatment.",
    )
    _catalog, pairs = OutlineV3Executor._stability_claim_review_material(
        [baseline], [variant], candidate_id="candidate_1"
    )
    pair = pairs[0]
    rows = [] if not reviews else [
        {
            "candidate_id": "candidate_1",
            "pair_id": pair["pair_id"],
            "decision": "equivalent",
            "evidence_refs": ["evidence-1"],
            "rationale": "Equivalent meaning.",
        }
        for _ in reviews
        if _ != "malformed_extra"
    ]
    if "malformed_extra" in reviews:
        rows.append("unexpected non-object review row")
    critique = {"passed": True, "issues": [], "stability_claim_reviews": rows}
    result = OutlineV3Executor._enforce_stability_claim_reviews(
        critique,
        comparisons={"candidate_1": pairs},
        candidate_hashes={"candidate_1": "candidate-hash"},
    )
    assert result["candidate_statuses"] == {"candidate_1": "blocked"}


def test_punctuation_only_typed_claim_change_does_not_require_semantic_review() -> None:
    baseline, variant = _typed_claim_pair(
        "Treatment improves the measured outcome.",
        "Treatment improves the measured outcome!",
    )
    assert _facts(baseline) == _facts(variant)
    _catalog, pairs = OutlineV3Executor._stability_claim_review_material(
        [baseline], [variant], candidate_id="candidate_1"
    )
    assert pairs == []


def test_numeric_punctuation_keeps_magnitude_changes_distinguishable() -> None:
    assert OutlineV3Executor._stability_claim_text_key("Effect increased 1.5 units.") != (
        OutlineV3Executor._stability_claim_text_key("Effect increased 15 units!")
    )
    assert OutlineV3Executor._stability_claim_text_key("Effect increased by 10%.") != (
        OutlineV3Executor._stability_claim_text_key("Effect increased by 10.")
    )
    assert OutlineV3Executor._stability_claim_text_key("Effect changed by -1.5 units.") != (
        OutlineV3Executor._stability_claim_text_key("Effect changed by 1.5 units.")
    )
    baseline, variant = _typed_claim_pair(
        "Effect changed by -1.5 units.",
        "Effect changed by 1.5 units.",
    )
    _catalog, pairs = OutlineV3Executor._stability_claim_review_material(
        [baseline], [variant], candidate_id="candidate_1"
    )
    assert len(pairs) == 1


def _typed_claim_provider(*, drift: str, review_decision: str, omit_review: bool = False):
    review_requests: list[dict] = []

    def provider(node_id: str, request: dict) -> dict:
        response = deepcopy(_configured_test_provider(node_id, request))
        authority = request.get("_prompt_authority") or {}
        actual_node_id = str(authority.get("node_id") or node_id)
        if "_provider_generation" in node_id:
            for section in response.get("content", {}).get("sections") or ():
                if not isinstance(section, dict):
                    continue
                claim = "Treatment improves the measured outcome."
                if actual_node_id.startswith("stability:") and drift == "paraphrase":
                    claim = "The measured outcome improves under treatment."
                elif actual_node_id.startswith("stability:") and drift == "directional":
                    claim = "Treatment worsens the measured outcome."
                section["claims"] = [claim]
                section["claim_support"] = [{
                    "claim": claim,
                    "paper_key": "paper-a",
                    "study_id": "study-1",
                    "source_claim_ids": ["source-claim-1"],
                    "evidence_ids": ["evidence-1"],
                    "source_field_ids": ["field-1"],
                    "condition_ids": ["condition-1"],
                    "claim_kind": "effect",
                    "effect_direction": "positive",
                }]
        comparisons = request.get("stability_claim_comparisons")
        if (
            isinstance(comparisons, dict)
            and any(isinstance(rows, list) and rows for rows in comparisons.values())
        ):
            review_requests.append(deepcopy(request))
            if not omit_review:
                response_content = response.setdefault("content", {})
                response_content["stability_claim_reviews"] = [
                    {
                        "candidate_id": str(candidate_id),
                        "pair_id": str(pair.get("pair_id") or ""),
                        "decision": review_decision,
                        "evidence_refs": list(pair.get("evidence_refs") or ()),
                        "rationale": "The bounded review compared the paired statements with their source evidence.",
                    }
                    for candidate_id, pairs in comparisons.items()
                    if isinstance(pairs, list)
                    for pair in pairs
                    if isinstance(pair, dict)
                ]
        return response

    return provider, review_requests


def test_variant_typed_paraphrase_uses_existing_evidence_critique_call(
    tmp_path,
) -> None:
    provider, review_requests = _typed_claim_provider(
        drift="paraphrase", review_decision="equivalent"
    )
    result = _executor(tmp_path, provider=provider, stability_mode="smoke").run()
    audit = _artifact_payload(result, "stability_audit")

    assert result.ok is True, result.diagnostics
    assert audit["status"] == "stable"
    assert len(review_requests) == 1
    request = review_requests[0]
    assert request["stability_claim_review_contract"]["version"] == "stability-claim-equivalence/v1"
    assert request["output_contract"]["must_include"][-1] == "stability_claim_reviews"
    assert audit["claim_equivalence_reviews"]["summary_order_reversed"]["pair_count"] == 2


def test_variant_typed_direction_reversal_is_blocked_before_adoption(tmp_path) -> None:
    provider, review_requests = _typed_claim_provider(
        drift="directional", review_decision="material_change"
    )
    result = _executor(tmp_path, provider=provider, stability_mode="smoke").run()

    assert result.ok is False
    assert len(review_requests) == 1
    audit = _artifact_payload(result, "stability_audit")
    review = audit["claim_equivalence_reviews"]["summary_order_reversed"]
    assert audit["status"] == "blocked"
    assert review["status"] == "candidate_blocks"
    assert set(review["candidate_statuses"].values()) == {"blocked"}


def test_variant_typed_paraphrase_without_review_result_fails_closed(tmp_path) -> None:
    provider, review_requests = _typed_claim_provider(
        drift="paraphrase", review_decision="equivalent", omit_review=True
    )
    result = _executor(tmp_path, provider=provider, stability_mode="smoke").run()

    assert result.ok is False
    assert len(review_requests) == 1
    audit = _artifact_payload(result, "stability_audit")
    review = audit["claim_equivalence_reviews"]["summary_order_reversed"]
    assert audit["status"] == "blocked"
    assert set(review["candidate_statuses"].values()) == {"blocked"}

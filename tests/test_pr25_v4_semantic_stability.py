"""Scientific stability compares typed evidence and direction, not prose titles."""

from __future__ import annotations

from copy import deepcopy

import pytest

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

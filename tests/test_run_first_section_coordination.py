"""Business regressions for explicit canonical section intent coordination."""

from copy import deepcopy

import pytest

from outline.v3_executor import OutlineV3ExecutionError
from tests.test_pr25_v3_regressions import (
    _coordination_merge_group,
    _coordination_sections,
    _executor,
)


def _canonical_merge():
    sections = _coordination_sections()[:2]
    sections[1]["title"] = "Context boundaries of the mechanism"
    sections[1]["goal"] = "Explain which context limits the mechanism."
    source_ids = [section["section_id"] for section in sections]
    group = _coordination_merge_group(sections, source_ids)
    group["title"] = "Mechanism and its context boundaries"
    group["goal"] = "Explain the mechanism together with the evidence limiting its scope."
    group["canonical_intent"] = {
        "title": group["title"],
        "goal": group["goal"],
        "source_intents": {
            section["section_id"]: {
                "title": section["title"],
                "goal": section["goal"],
                "contribution": section["rationale"],
            }
            for section in sections
        },
    }
    return sections, group


def test_explicit_coordination_merges_distinct_wording_without_losing_evidence(tmp_path):
    sections, group = _canonical_merge()
    original = deepcopy(sections)
    coordinated, audit = _executor(tmp_path)._apply_section_coordination(
        "candidate_1", sections,
        {"candidate_id": "candidate_1", "merge_groups": [group],
         "section_order": ["section:mechanism"]},
        parent_hash="parent",
    )
    merged = coordinated[0]
    assert merged["title"] == group["title"]
    assert merged["goal"] == group["goal"]
    assert merged["claims"] == [claim for section in original for claim in section["claims"]]
    assert merged["claim_support"] == [row for section in original for row in section["claim_support"]]
    assert merged["paper_keys"] == ["paper-a", "paper-b"]
    assert merged["coordination_source_intents"] == group["canonical_intent"]["source_intents"]
    assert audit["merge_groups"][0]["canonical_intent"] == group["canonical_intent"]
    assert sections == original


@pytest.mark.parametrize("corruption", ["missing_source", "foreign_goal", "empty_reason", "target_mismatch"])
def test_canonical_coordination_rejects_unaccounted_intent(tmp_path, corruption):
    sections, group = _canonical_merge()
    intents = group["canonical_intent"]["source_intents"]
    first = sections[0]["section_id"]
    if corruption == "missing_source":
        intents.pop(first)
    elif corruption == "foreign_goal":
        intents[first]["goal"] = "A goal never present in this candidate."
    elif corruption == "empty_reason":
        intents[first]["contribution"] = ""
    else:
        group["canonical_intent"]["goal"] = "A different target."
    with pytest.raises(OutlineV3ExecutionError, match="intent"):
        _executor(tmp_path)._apply_section_coordination(
            "candidate_1", sections,
            {"candidate_id": "candidate_1", "merge_groups": [group],
             "section_order": ["section:mechanism"]},
            parent_hash="parent",
        )

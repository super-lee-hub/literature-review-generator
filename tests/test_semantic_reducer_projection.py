from __future__ import annotations

from copy import deepcopy

import pytest

from outline.semantic_reducer_projection import split_nested_topic_for_reduction_v1


def _topic():
    fragments, refs = [], []
    for index in (1, 2):
        paper, fragment, result = f"paper-{index}", f"fragment-{index}", f"result-{index}"
        field_id = f"field-{index}"
        fragments.append({
            "fragment_id": fragment, "paper_ids": [paper],
            "supporting_evidence_ids": [f"E{index}"],
            "evidence_unit_indexes": {paper: [index]},
            "provider_results": [{
                "fragment_id": fragment, "paper_ids": [paper],
                "result_id": result, "batch_result_id": f"batch-result-{index}", "batch_id": f"batch-{index}",
                "provider_output": {"topic": {"conclusions": [f"Complete claim {index}."], "tail": "BOUNDARY_TAIL"}},
                "interpretation_context": {
                    "fields": [{"source_field_id": field_id, "paper_key": paper, "source_value": "条件文本 " * 200 + "BOUNDARY_TAIL"}],
                    "dependencies": [{"primary_claim_id": f"C{index}", "paper_key": paper, "required_source_field_ids": [field_id]}],
                },
            }],
        })
        refs.append({"fragment_id": fragment, "result_id": result, "batch_result_id": f"batch-result-{index}"})
    return {
        "topic_id": "logical-topic", "question": "Compare the evidence without losing conditions.",
        "paper_ids": ["paper-1", "paper-2"], "fragment_ids": ["fragment-1", "fragment-2"],
        "fragments": fragments, "provider_output_refs": refs,
        "supporting_evidence_ids": ["E1", "E2"], "bridge_paper_ids": [], "relation_ids": [],
    }


def test_split_conserves_scoped_complete_source_and_identity_without_mutation():
    topic = _topic()
    before = deepcopy(topic)
    rows = split_nested_topic_for_reduction_v1(topic)
    assert topic == before
    assert len(rows) == 2
    assert {row["topic_id"] for row in rows} == {"logical-topic"}
    assert {value for row in rows for value in row["fragment_ids"]} == set(topic["fragment_ids"])
    assert {value for row in rows for value in row["paper_ids"]} == set(topic["paper_ids"])
    for index, row in enumerate(rows, 1):
        assert row["paper_ids"] == [f"paper-{index}"]
        assert row["supporting_evidence_ids"] == [f"E{index}"]
        expected_fragment = deepcopy(topic["fragments"][index - 1])
        expected_fragment["provider_results"][0].pop("interpretation_context")
        assert row["fragments"] == [expected_fragment]
        assert row["question"] == topic["question"]
        field = row["interpretation_context"]["fields"][0]
        assert field["paper_key"] == f"paper-{index}"
        assert field["source_value"].endswith("BOUNDARY_TAIL")
        assert row["interpretation_context"]["dependencies"][0]["required_source_field_ids"] == [field["source_field_id"]]
    rows[0]["fragments"][0]["provider_results"][0]["provider_output"]["topic"]["tail"] = "changed"
    assert topic == before


@pytest.mark.parametrize("key,value", [
    ("conclusions", ["An inseparable comparison."]),
    ("interpretation_context", {"fields": [{"source_value": "Joint source context"}]}),
    ("bridge_paper_ids", ["paper-bridge"]),
    ("relation_ids", ["cross-paper-relation"]),
])
def test_unscoped_root_facts_or_relations_remain_indivisible(key, value):
    topic = _topic()
    topic[key] = value
    assert split_nested_topic_for_reduction_v1(topic) == [topic]


@pytest.mark.parametrize("corruption", ["duplicate_fragment", "wrong_paper", "wrong_result", "missing_field", "conflicting_field"])
def test_corrupt_fragment_or_interpretation_closure_is_rejected(corruption):
    topic = _topic()
    result = topic["fragments"][0]["provider_results"][0]
    if corruption == "duplicate_fragment":
        topic["fragments"][1]["fragment_id"] = "fragment-1"
    elif corruption == "wrong_paper":
        result["interpretation_context"]["fields"][0]["paper_key"] = "paper-2"
    elif corruption == "wrong_result":
        topic["provider_output_refs"][0]["result_id"] = "unknown-result"
    elif corruption == "missing_field":
        result["interpretation_context"]["fields"] = []
    else:
        field = deepcopy(result["interpretation_context"]["fields"][0])
        field["source_value"] = "Conflicting condition."
        result["interpretation_context"]["fields"].append(field)
    with pytest.raises(ValueError):
        split_nested_topic_for_reduction_v1(topic)


def test_one_fragment_is_not_split_or_changed():
    topic = _topic()
    topic["fragments"] = topic["fragments"][:1]
    assert split_nested_topic_for_reduction_v1(topic) == [topic]


@pytest.mark.parametrize("key,value", [
    ("paper_ids", "unowned-paper"),
    ("supporting_evidence_ids", "unowned-evidence"),
    ("provider_batch_ids", "unowned-batch"),
])
def test_detached_aggregate_reference_is_rejected_instead_of_dropped(key, value):
    topic = _topic()
    topic.setdefault(key, []).append(value)
    with pytest.raises(ValueError):
        split_nested_topic_for_reduction_v1(topic)


def test_actual_claim_evidence_is_conserved_and_context_is_carried_once():
    topic = _topic()
    result = topic["fragments"][0]["provider_results"][0]
    context = deepcopy(result["interpretation_context"])
    result["provider_output"]["claims"] = [{
        "claim_id": "actual-claim", "paper_key": "paper-1",
        "text": "The finding retains its recorded condition.", "evidence_ids": ["actual-evidence"],
    }]
    topic["supporting_evidence_ids"].append("actual-evidence")
    row = split_nested_topic_for_reduction_v1(topic)[0]
    assert "actual-evidence" in row["supporting_evidence_ids"]
    assert row["interpretation_context"] == context
    assert "interpretation_context" not in row["fragments"][0]["provider_results"][0]
    assert row["fragments"][0]["provider_results"][0]["provider_output"] == result["provider_output"]


@pytest.mark.parametrize("corruption", ["empty_text", "no_owner", "wrong_topic", "wrong_batch", "cross_fragment_conflict", "duplicate_result", "missing_result", "multiple_results"])
def test_context_or_result_owner_ambiguity_is_rejected(corruption):
    topic = _topic()
    first = topic["fragments"][0]["provider_results"][0]
    second = topic["fragments"][1]["provider_results"][0]
    if corruption == "empty_text":
        first["interpretation_context"]["fields"][0]["source_value"] = ""
    elif corruption == "no_owner":
        first["interpretation_context"]["fields"][0].pop("paper_key")
    elif corruption == "wrong_topic":
        first["topic_id"] = "wrong-topic"
    elif corruption == "wrong_batch":
        topic["provider_output_refs"][0]["batch_id"] = "wrong-batch"
    elif corruption == "duplicate_result":
        second["result_id"] = first["result_id"]
        topic["provider_output_refs"][1]["result_id"] = first["result_id"]
    elif corruption == "missing_result":
        topic["fragments"][0]["provider_results"] = []
    elif corruption == "multiple_results":
        topic["fragments"][0]["provider_results"].append(deepcopy(first))
    else:
        second["interpretation_context"]["fields"][0]["source_field_id"] = "field-1"
        second["interpretation_context"]["dependencies"][0]["required_source_field_ids"] = ["field-1"]
    with pytest.raises(ValueError):
        split_nested_topic_for_reduction_v1(topic)

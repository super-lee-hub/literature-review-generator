from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from tests.test_current_runtime_full_e2e import _outline_provider_response

_PROCESSED_ID_FIELDS = {
    "processed_topic_ids",
    "processed_fragment_ids",
    "processed_result_ids",
    "processed_relation_ids",
}


def _comparison_request(*, shared: bool) -> dict[str, Any]:
    topic_id = "topic:method:shared-coverage"
    rows = []
    for suffix, paper in (("a", "paper-a"), ("b", "paper-b")):
        field_id = f"field:{paper}:boundary"
        rows.append({
            "topic_id": topic_id,
            "fragment_id": f"fragment-{suffix}",
            "result_id": f"result-{suffix}",
            "paper_ids": [paper],
            "relation_ids": ["relation:a:b"],
            "claims": [{
                "claim_id": f"synthesis:topic_synthesis:{suffix}",
                "topic_ids": [topic_id],
                "paper_key": paper,
                "text": f"The source for {paper} reports its result within the recorded boundary.",
                "source_claim_ids": [f"claim:{paper}:finding"],
                "evidence_ids": [f"evidence:{paper}:finding"],
                "source_field_ids": [field_id],
            }],
            "interpretation_context": {
                "fields": [{"paper_key": paper, "source_field_id": field_id}],
                "dependencies": [],
            },
        })
    request: dict[str, Any] = {
        "topic_synthesis": rows,
        "relation_candidates": [{
            "relation_id": "relation:a:b",
            "paper_keys": ["paper-a", "paper-b"],
        }],
    }
    if shared:
        request["shared_synthesis_contract_version"] = "v1"
    return request


def test_shared_cross_group_fixture_uses_local_coverage_and_keeps_legacy_echoes() -> None:
    shared_request = _comparison_request(shared=True)
    shared = _outline_provider_response(
        "cross_group_comparison_provider:reduce:1:1",
        shared_request,
    )["content"]

    assert not (_PROCESSED_ID_FIELDS & set(shared))
    assert len(shared["comparisons"]) == 1
    assert len(shared["bridge_claims"]) == 1
    assert len(shared["topic_dispositions"]) == 1
    bridge = shared["bridge_claims"][0]
    assert set(bridge["source_claim_ids"]) == {
        "claim:paper-a:finding",
        "claim:paper-b:finding",
    }
    assert set(bridge["evidence_ids"]) == {
        "evidence:paper-a:finding",
        "evidence:paper-b:finding",
    }
    assert set(bridge["source_field_ids"]) == {
        "field:paper-a:boundary",
        "field:paper-b:boundary",
    }
    assert shared["topic_dispositions"][0]["status"] == "integrated"

    legacy_request = deepcopy(shared_request)
    legacy_request.pop("shared_synthesis_contract_version")
    legacy = _outline_provider_response(
        "cross_group_comparison_provider:reduce:1:1",
        legacy_request,
    )["content"]

    assert _PROCESSED_ID_FIELDS.issubset(legacy)
    assert legacy["processed_topic_ids"] == ["topic:method:shared-coverage"]
    assert set(legacy["processed_fragment_ids"]) == {"fragment-a", "fragment-b"}
    assert set(legacy["processed_result_ids"]) == {"result-a", "result-b"}
    assert shared["bridge_claims"] == legacy["bridge_claims"]
    assert shared["topic_dispositions"] == legacy["topic_dispositions"]

    shared_global = _outline_provider_response(
        "global_synthesis_provider",
        {
            "shared_synthesis_contract_version": "v1",
            "topic_synthesis": [],
            "cross_group_comparison": shared,
        },
    )["content"]
    legacy_global = _outline_provider_response(
        "global_synthesis_provider",
        {
            "topic_synthesis": [],
            "cross_group_comparison": legacy,
        },
    )["content"]
    assert not (_PROCESSED_ID_FIELDS & set(shared_global))
    assert _PROCESSED_ID_FIELDS.issubset(legacy_global)
    assert shared_global["synthesis_claims"] == legacy_global["synthesis_claims"]


def test_shared_reducer_deduplicates_only_identical_source_claim_text() -> None:
    topics = [f"topic:family:{index:02d}:source-bound" for index in range(18)]
    papers = [f"paper-{index}" for index in range(5)]
    claim_texts = {
        "claim:paper-0:finding": "The treatment increased the measured outcome in the observed sample.",
        "claim:paper-0:condition": "The finding applies only to the assigned reference-label condition and 30-day follow-up.",
        "claim:paper-0:condition-copy": "The finding applies only to the assigned reference-label condition and 30-day follow-up.",
        "claim:paper-1:qualifier": "The reported estimate remains attached to its own source condition and follow-up.",
        "claim:paper-2:qualifier": "The source reports its estimate only for the recorded sample and condition.",
        "claim:paper-3:finding": "The intervention reduced the primary outcome in the observed sample.",
        "claim:paper-3:condition": "The result is limited to the enrolled-member threshold offer at 30 days.",
        "claim:paper-3:condition-copy": "The result is limited to the enrolled-member threshold offer at 30 days.",
        "claim:paper-4:qualifier": "The source reports no pooled estimate across the separately described design.",
    }
    specs = [
        (0, 0, ["claim:paper-0:finding", "claim:paper-0:condition", "claim:paper-0:condition-copy"], "field:paper-0:boundary"),
        (1, 1, ["claim:paper-1:qualifier"], "field:paper-1:boundary"),
        (2, 1, ["claim:paper-2:qualifier"], "field:paper-2:boundary"),
        (3, 2, ["claim:paper-3:finding", "claim:paper-3:condition", "claim:paper-3:condition-copy"], "field:paper-3:boundary"),
        (4, 3, ["claim:paper-4:qualifier"], "field:paper-4:boundary"),
    ]
    topic_members: dict[str, set[str]] = {topic: set() for topic in topics}
    items: list[dict[str, Any]] = []
    claim_closures: list[dict[str, Any]] = []
    all_source_claim_ids: set[str] = set()
    all_evidence_ids: set[str] = set()
    all_field_ids: set[str] = set()
    field_texts: dict[str, str] = {}
    for index, (paper_index, integrated_topic_index, source_ids, field_id) in enumerate(specs):
        paper = papers[paper_index]
        topic = topics[integrated_topic_index]
        topic_members[topic].add(paper)
        evidence_ids = [f"evidence:{source_id}" for source_id in source_ids]
        all_source_claim_ids.update(source_ids)
        all_evidence_ids.update(evidence_ids)
        all_field_ids.add(field_id)
        field_texts[field_id] = claim_texts[source_ids[-1]]
        bridge = {
            "claim_id": f"synthesis:cross_group_comparison:prior-{index}",
            "topic_ids": [topic],
            "paper_keys": [paper],
            # The child reducer repeats the same source wording. The final
            # fixture fold may remove only duplicate exact source sentences.
            "text": "; ".join([claim_texts[source_id] for source_id in source_ids] * 2),
            "source_claim_ids": list(source_ids),
            "evidence_ids": evidence_ids,
            "source_field_ids": [field_id],
        }
        item_topics = [topics[value] for value in range(index, min(18, index + 4))]
        item_topics = sorted({*item_topics, topic})
        for item_topic in item_topics:
            topic_members[item_topic].add(paper)
        items.append({
            "reducer_id": f"cross_group_comparison_provider:reduce:1:{index + 1}",
            "topic_ids": item_topics,
            "paper_ids": [paper],
            "topic_members_by_id": {value: [paper] for value in item_topics},
            "fragment_ids": [f"fragment:{index}:{part}" for part in range(6)],
            "result_ids": [f"result:{index}:{part}" for part in range(6)],
            "interpretation_context": {
                "fields": [{
                    "paper_key": paper,
                    "source_field_id": field_id,
                    "source_value": claim_texts[source_ids[-1]],
                    "source_path": "limitations",
                    "canonical_field": "limitations",
                }],
                "dependencies": [],
            },
            "semantic_result": {"bridge_claims": [bridge], "comparisons": []},
        })
        for source_id in source_ids:
            claim_closures.append({
                "source_claim_records": [{
                    "claim_id": source_id,
                    "paper_key": paper,
                    "claim_type": "empirical_finding" if source_id.endswith("finding") else "author_interpretation",
                    "source_locator": source_id,
                    "text": claim_texts[source_id],
                }],
            })

    # Make the 18-topic membership explicit, with four covered topics and the
    # remaining topics retained as deferred dispositions.
    items[0]["topic_ids"] = topics
    items[0]["paper_ids"] = papers
    items[0]["topic_members_by_id"] = {
        topic: sorted(topic_members[topic]) for topic in topics
    }
    fields_by_paper = {
        papers[paper_index]: field_id
        for paper_index, _topic, _source_ids, field_id in specs
    }
    for item in items:
        item["interpretation_context"]["fields"] = [
            {
                "paper_key": paper,
                "source_field_id": fields_by_paper[paper],
                "source_value": claim_texts[
                    next(
                        source_ids[-1]
                        for paper_index, _topic, source_ids, _field in specs
                        if papers[paper_index] == paper
                    )
                ],
                "source_path": "limitations",
                "canonical_field": "limitations",
            }
            for paper in item["paper_ids"]
        ]
    request = {
        "shared_synthesis_contract_version": "v1",
        "topic_synthesis": items,
        "relation_candidates": [],
    }
    trace = [{"kind": "topic_source_bound", "claim_closures": claim_closures}]

    result = _outline_provider_response(
        "cross_group_comparison_provider:reduce:2:2",
        request,
        trace,
    )["content"]

    bridges = result["bridge_claims"]
    assert len(bridges) == 5
    assert {bridge["claim_id"] for bridge in bridges} == {
        f"synthesis:cross_group_comparison:prior-{index}"
        for index in range(5)
    }
    assert len(result["topic_dispositions"]) == 18
    assert sum(row["status"] == "integrated" for row in result["topic_dispositions"]) == 4
    assert sum(row["status"] == "deferred" for row in result["topic_dispositions"]) == 14
    assert {value for bridge in bridges for value in bridge["source_claim_ids"]} == all_source_claim_ids
    assert {value for bridge in bridges for value in bridge["evidence_ids"]} == all_evidence_ids
    assert {value for bridge in bridges for value in bridge["source_field_ids"]} == all_field_ids
    for bridge in bridges:
        assert all(
            claim_texts[value].rstrip(".!?。！？；; ").casefold() in bridge["text"].casefold()
            for value in bridge["source_claim_ids"]
        )
        field_id = bridge["source_field_ids"][0]
        assert field_id in all_field_ids
        assert field_texts[field_id].rstrip(".!?。！？；; ").casefold() in bridge["text"].casefold()
    assert _PROCESSED_ID_FIELDS.isdisjoint(result)
    encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
    assert (len(encoded) + 3) // 4 <= 4096

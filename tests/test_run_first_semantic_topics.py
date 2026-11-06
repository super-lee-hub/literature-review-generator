from __future__ import annotations

from outline.semantic_chunking import _build_topics, _topic_candidates, build_semantic_chunk_plan
from outline.v3_models import PaperContentLayers, PaperEvidenceDossier, PaperIndexCard


def _layers(count: int, *, include_method: bool = True, extra_unrouted_evidence: bool = True):
    cards = []
    dossiers = []
    for index in range(count):
        paper_id = f"paper-{index:03d}"
        method = ("survey", "questionnaire survey", "randomized experiment")[index % 3]
        theory = (
            "equity theory",
            "price fairness framework",
            "dual entitlement principle",
            "prospect theory",
            "loss aversion framework",
        )[index % 5]
        cards.append(PaperIndexCard(
            paper_id=paper_id,
            key_constructs=[] if index == 0 else ["consumer trust", "promotion fairness"],
            theories=[theory],
            mechanisms=[],
            method_category=method if include_method else "",
            key_boundaries=[],
        ))
        fields = {
            "research_questions": [f"{paper_id}:rq"],
            "concept_definitions": [f"{paper_id}:trust", f"{paper_id}:fairness"],
            "operationalizations": [f"{paper_id}:measure"],
            "theoretical_derivation": [f"{paper_id}:theory"],
            "findings": [f"{paper_id}:finding"],
            "mechanism_evidence": [f"{paper_id}:mechanism"],
            "moderators_boundaries": [f"{paper_id}:context"],
            "limitations": [f"{paper_id}:limitation"],
        }
        if extra_unrouted_evidence:
            fields["unmapped_source_evidence"] = [f"{paper_id}:other-1", f"{paper_id}:other-2"]
        evidence_ids = [value for values in fields.values() for value in values]
        dossiers.append(PaperEvidenceDossier(
            dossier_id=f"dossier:{paper_id}",
            paper_id=paper_id,
            overall_context=["online retail shoppers"],
            research_questions=[f"Does fairness change trust for {paper_id}?"],
            concept_definitions=["consumer trust", "promotion fairness"],
            operationalizations=["seven point trust scale"],
            theoretical_derivation=[theory],
            findings=[f"{paper_id} reports a result under an online retail condition."],
            mechanism_evidence=["reference-price comparison"],
            moderators_boundaries=["online retail shoppers"],
            evidence_ids_by_field=fields,
            evidence_text_by_id={evidence_id: evidence_id for evidence_id in evidence_ids},
        ))
    return PaperContentLayers(index_cards=cards, dossiers=dossiers)


def _membership_rows(content_layers: PaperContentLayers):
    groups, _paper_topics = _topic_candidates(content_layers)
    return {
        (str(group.get("dimension") or ""), str(group.get("label") or ""), tuple(group.get("paper_ids") or ()))
        for group in groups.values()
    }


def test_method_theory_family_routes_do_not_suppress_other_dimensions():
    layers = _layers(12)
    groups, paper_topics = _topic_candidates(layers)

    for dimension, label in (
        ("construct", "consumer trust"),
        ("construct", "promotion fairness"),
        ("mechanism", "reference-price comparison"),
        ("context", "online retail shoppers"),
    ):
        assert any(
            group.get("dimension") == dimension
            and group.get("label") == label
            and len(group.get("paper_ids") or ()) == 12
            for group in groups.values()
        )

    assert set(paper_topics) == {f"paper-{index:03d}" for index in range(12)}
    assert all(len(topic_ids) >= 4 for topic_ids in paper_topics.values())
    assert not any(group.get("paired_dimensions") for group in groups.values())


def test_added_method_membership_keeps_construct_mechanism_and_context_membership():
    without_method = _layers(6, include_method=False)
    with_method = _layers(6, include_method=True)
    before = {
        row for row in _membership_rows(without_method)
        if row[0] in {"construct", "mechanism", "context"}
    }
    after = {
        row for row in _membership_rows(with_method)
        if row[0] in {"construct", "mechanism", "context"}
    }

    assert after == before


def test_family_routing_is_order_invariant_and_preserves_shared_multidimension_routes():
    layers = _layers(63)
    groups, paper_topics = _topic_candidates(layers)
    reversed_layers = PaperContentLayers(
        index_cards=list(reversed(layers.index_cards)),
        dossiers=list(reversed(layers.dossiers)),
    )
    reversed_groups, reversed_paper_topics = _topic_candidates(reversed_layers)

    assert _membership_rows(layers) == _membership_rows(reversed_layers)
    assert paper_topics == reversed_paper_topics
    assert not any(group.get("paired_dimensions") for group in groups.values())
    assert len(groups) < 63
    assert all(paper_topics[f"paper-{index:03d}"] for index in range(63))

    topics, _cross_questions, coverage = _build_topics(layers)
    assert len(topics) < 24
    assert coverage["topic_routing"]["mode"] == "shared_family_and_exact_membership"
    assert coverage["topic_family_grouping"]["method_family_group_count"] > 0
    assert coverage["topic_family_grouping"]["theory_family_group_count"] > 0


def test_small_corpus_keeps_separate_shared_method_and_theory_questions():
    groups, _paper_topics = _topic_candidates(_layers(2))
    method = next(item for item in groups.values() if item.get("dimension") == "method")
    theory = next(item for item in groups.values() if item.get("dimension") == "theory")

    assert method["paper_ids"] == ["paper-000", "paper-001"]
    assert theory["paper_ids"] == ["paper-000", "paper-001"]
    assert not any(item.get("paired_dimensions") for item in groups.values())


def test_unselected_source_fields_remain_deferred_without_marking_every_paper_outlier():
    layers = _layers(12, extra_unrouted_evidence=True)
    topics, _cross_questions, coverage = _build_topics(layers)
    plan = build_semantic_chunk_plan(layers, candidate_count=1, physical_call_limit=100)

    assert not any(topic.status == "outlier_review" for topic in topics)
    assert coverage["outlier_paper_ids"] == []
    assert coverage["deferred_source_evidence_count"] == 24
    assert coverage["deferred_source_evidence_policy"] == "complete_dossiers_remain_locally_available_for_directed_retrieval"
    assert plan.budgets["provider_posts_emitted"] == 0

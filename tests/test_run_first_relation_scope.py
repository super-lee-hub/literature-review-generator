from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping
from typing import Any

import pytest

from outline.candidate_output_scope import (
    CandidateOutputScopeError,
    build_candidate_output_scope_v1,
)
from outline.v3_evidence import (
    build_global_corpus_ledger,
    build_multi_view_matrix,
    build_outline_evidence_views,
)
from outline.v3_relations import build_global_relation_map
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.writer_source_inventory import load_writer_source_inventory_v1
from tests.test_outline_v3_relations import _relation_summary
from tests.test_outline_v3_semantic_execution import _summary
from tests.writer_source_fixture import bind_production_writer_sources


def _relation_map(summaries: list[Mapping[str, Any]]):
    evidence = build_outline_evidence_views(summaries)
    matrix = build_multi_view_matrix(evidence)
    ledger = build_global_corpus_ledger(evidence)
    return build_global_relation_map(evidence, matrix, ledger)


def _focused_corpus() -> list[dict[str, Any]]:
    rows = [
        _relation_summary(
            "10.9000/a", "Paper A", "online retail", "survey",
            "The treatment supports trust in the tested context.",
        ),
        _relation_summary(
            "10.9000/b", "Paper B", "laboratory retail", "experiment",
            "The treatment contradicts earlier evidence about trust.",
        ),
        _relation_summary(
            "10.9000/c", "Paper C", "online retail", "field study",
            "The treatment improves trust only when the price cue is salient.",
        ),
        _relation_summary(
            "10.9000/d", "Paper D", "mobile retail", "survey",
            "The framework qualifies the treatment effect on trust.",
            paper_type="conceptual",
        ),
    ]
    rows[0]["ai_summary"]["core_analysis"]["theoretical_framework"] = "equity theory"
    rows[1]["ai_summary"]["core_analysis"]["theoretical_framework"] = "equity theory"
    rows[2]["ai_summary"]["core_analysis"]["theoretical_framework"] = "social exchange theory"
    rows[3]["ai_summary"]["core_analysis"]["theoretical_framework"] = "social exchange theory"
    rows[2]["ai_summary"]["core_analysis"]["limitations"] = (
        "The association is limited to the price-salience condition."
    )
    rows[0]["ai_summary"]["specialized_details"]["empirical"]["research_questions_or_hypotheses"].append(
        "Does the treatment change trust in an online retail context?"
    )
    return rows


def _semantic_selection(relation_map, *, title_by_key: Mapping[str, str]):
    return {
        (
            item["relation_type"],
            tuple(sorted(title_by_key[key] for key in item["paper_keys"])),
            tuple(item["reason_codes"]),
        )
        for item in relation_map.comparison_coverage["selected_comparisons"]
    }


def test_four_paper_relations_keep_conflict_qualifier_context_and_bridge_tasks():
    relation_map = _relation_map(_focused_corpus())
    title_by_key = {view.paper_key: view.title for view in build_outline_evidence_views(_focused_corpus()).views}
    selection = _semantic_selection(relation_map, title_by_key=title_by_key)

    assert any(kind == "contradicts" and "Paper A" in pair and "Paper B" in pair for kind, pair, _ in selection)
    assert any(kind == "qualifies" and "Paper A" in pair and "Paper C" in pair for kind, pair, _ in selection)
    assert any(
        kind == "different_context"
        and "Paper B" in pair
        and bool(set(pair) & {"Paper A", "Paper C"})
        for kind, pair, _ in selection
    )
    assert any(kind == "conceptual_integration" and "Paper C" in pair and "Paper D" in pair for kind, pair, _ in selection)
    assert any("cross_topic_bridge" in reasons for _kind, _pair, reasons in selection)
    assert relation_map.comparison_coverage["policy"] == "purposeful_sparse_cohorts_v1"
    assert not any(item.get("severity") == "blocking" for item in relation_map.blocking_diagnostics)


def test_four_paper_reported_magnitudes_select_adjacent_same_measure_strata():
    reported = (
        ("paper-a", "Paper A", "Treatment improved the measured outcome by four points."),
        ("paper-b", "Paper B", "Treatment improved the measured outcome by two points."),
        ("paper-c", "Paper C", "Treatment improved the measured outcome by one point."),
        ("paper-d", "Paper D", "Treatment did not change the measured outcome."),
    )
    summaries = []
    for key, title, finding in reported:
        summary = _relation_summary(f"10.9005/{key}", title, "digital retail", "survey", finding)
        summary["ai_summary"]["specialized_details"]["empirical"]["core_variables"]["dependent"] = [
            "measured outcome"
        ]
        summaries.append(summary)

    relation_map = _relation_map(summaries)
    title_by_key = {view.paper_key: view.title for view in build_outline_evidence_views(summaries).views}
    magnitude_rows = [
        row for row in relation_map.comparison_coverage["selected_comparisons"]
        if row["relation_type"] == "reported_magnitude_contrast"
    ]
    magnitude_pairs = {
        frozenset(title_by_key[key] for key in row["paper_keys"])
        for row in magnitude_rows
    }

    assert magnitude_pairs == {
        frozenset(("Paper A", "Paper B")),
        frozenset(("Paper B", "Paper C")),
        frozenset(("Paper C", "Paper D")),
    }
    assert all("reported_magnitude_contrast" in row["reason_codes"] for row in magnitude_rows)
    assert all(
        "measured outcome [point]" in relation.supporting_labels
        for relation in relation_map.relations
        if relation.relation_type == "reported_magnitude_contrast"
    )
    assert _relation_map(list(reversed(summaries))).content_hash == relation_map.content_hash

    renamed = []
    for index, summary in enumerate(summaries):
        clone = deepcopy(summary)
        clone["paper_info"]["doi"] = f"10.9998/magnitude-{index}"
        renamed.append(clone)
    renamed_map = _relation_map(renamed)
    renamed_titles = {
        view.paper_key: view.title
        for view in build_outline_evidence_views(renamed).views
    }
    assert _semantic_selection(relation_map, title_by_key=title_by_key) == _semantic_selection(
        renamed_map, title_by_key=renamed_titles,
    )

    unrelated = _relation_summary(
        "10.9006/unrelated", "Unrelated paper", "unique agricultural market", "ethnography",
        "The seasonal irrigation practice changes soil retention.",
    )
    unrelated["ai_summary"]["specialized_details"]["empirical"]["core_variables"] = {
        "independent": ["irrigation practice"], "dependent": ["soil retention"],
        "mediators": [], "moderators": [], "controls": [], "other_core_constructs": [],
    }
    unrelated["ai_summary"]["core_analysis"]["theoretical_framework"] = "irrigation systems theory"
    extended_rows = [*summaries, unrelated]
    extended_map = _relation_map(extended_rows)
    extended_titles = {
        view.paper_key: view.title
        for view in build_outline_evidence_views(extended_rows).views
    }
    assert _semantic_selection(relation_map, title_by_key=title_by_key).issubset(
        _semantic_selection(extended_map, title_by_key=extended_titles),
    )


def test_64_member_label_becomes_complete_cohort_without_pair_cap_block():
    summaries = [
        _relation_summary(
            f"10.9001/paper-{index:03d}", f"Paper {index:03d}", "digital retail", "survey",
            "The treatment significantly increases consumer trust in this setting."
            if index < 63 else
            "The treatment does not support increased consumer trust in this setting.",
        )
        for index in range(64)
    ]
    relation_map = _relation_map(summaries)
    cohort = next(
        item for item in relation_map.cohorts
        if item["dimension"] == "construct" and item["label"] == "consumer trust"
    )

    assert len(cohort["member_keys"]) == 64
    assert cohort["disposition"] == "selected"
    assert "minority_outcome" in cohort["reason_codes"]
    assert not any(item.get("code") == "relation_label_pair_cap" for item in relation_map.blocking_diagnostics)
    assert relation_map.comparison_coverage["selected_pair_count"] < 64
    assert relation_map.comparison_coverage["all_pairwise_comparisons_completed"] is False


def test_600_member_cohort_retains_conflict_qualifier_and_rare_topic_bridge():
    summaries = []
    for index in range(600):
        if index == 597:
            finding = "The treatment does not support increased consumer trust in this setting."
        elif index == 598:
            finding = "The treatment significantly increases consumer trust only under the rural price boundary."
        else:
            finding = "The treatment significantly increases consumer trust in this setting."
        summary = _relation_summary(
            f"10.9002/paper-{index:03d}", f"Paper {index:03d}",
            "rural retail" if index == 598 else "digital retail", "survey",
            finding,
        )
        if index == 599:
            summary["ai_summary"]["core_analysis"]["theoretical_framework"] = "rare exchange mechanism"
        summaries.append(summary)

    relation_map = _relation_map(summaries)
    title_by_key = {view.paper_key: view.title for view in build_outline_evidence_views(summaries).views}
    selected = relation_map.comparison_coverage["selected_comparisons"]
    title_pairs = {
        frozenset(title_by_key[key] for key in item["paper_keys"])
        for item in selected
    }
    reasons = {reason for item in selected for reason in item["reason_codes"]}
    consumer_trust = next(
        item for item in relation_map.cohorts
        if item["dimension"] == "construct" and item["label"] == "consumer trust"
    )

    assert len(consumer_trust["member_keys"]) == 600
    assert any("Paper 597" in pair for pair in title_pairs)
    assert any("Paper 598" in pair for pair in title_pairs)
    assert {"minority_outcome", "explicit_qualifier", "context_boundary", "cross_topic_bridge"}.issubset(reasons)
    assert len(title_pairs) < 600
    assert not any(item.get("code") == "relation_label_pair_cap" for item in relation_map.blocking_diagnostics)
    assert relation_map.comparison_coverage["cohort_member_references"] >= 600
    assert relation_map.comparison_coverage["all_pairwise_comparisons_completed"] is False


def test_600_member_repeated_conflict_selects_source_pattern_once_and_keeps_all_members():
    summaries = [
        _relation_summary(
            f"10.9004/paper-{index:03d}", f"Paper {index:03d}",
            "digital retail", "survey",
            "The treatment significantly increases consumer trust in this setting."
            if index < 300 else
            "The treatment does not support increased consumer trust in this setting.",
        )
        for index in range(600)
    ]
    relation_map = _relation_map(summaries)
    cohort = next(
        item for item in relation_map.cohorts
        if item["dimension"] == "construct" and item["label"] == "consumer trust"
    )

    assert len(cohort["member_keys"]) == 600
    assert relation_map.comparison_coverage["selected_pair_count"] == 1
    assert any(item.relation_type == "contradicts" for item in relation_map.relations)
    assert relation_map.comparison_coverage["cohort_pair_memberships_available"] >= 600 * 599 // 2
    assert relation_map.comparison_coverage["all_pairwise_comparisons_completed"] is False


def test_relation_selection_is_order_and_paper_key_invariant_and_local_on_unrelated_addition():
    original = _focused_corpus()
    original_map = _relation_map(original)
    reversed_map = _relation_map(list(reversed(original)))
    assert original_map.content_hash == reversed_map.content_hash

    renamed = []
    for index, row in enumerate(original):
        clone = deepcopy(row)
        clone["paper_info"]["doi"] = f"10.9999/renamed-{index:03d}"
        renamed.append(clone)
    renamed_map = _relation_map(renamed)

    def title_index(rows):
        return {
            view.paper_key: view.title
            for view in build_outline_evidence_views(rows).views
        }

    assert _semantic_selection(original_map, title_by_key=title_index(original)) == _semantic_selection(
        renamed_map, title_by_key=title_index(renamed),
    )

    unrelated = _relation_summary(
        "10.9003/unrelated", "Unrelated paper", "unique agricultural market", "ethnography",
        "The seasonal irrigation practice changes soil retention.",
    )
    unrelated["ai_summary"]["specialized_details"]["empirical"]["core_variables"] = {
        "independent": ["irrigation practice"], "dependent": ["soil retention"],
        "mediators": [], "moderators": [], "controls": [], "other_core_constructs": [],
    }
    unrelated["ai_summary"]["core_analysis"]["theoretical_framework"] = "irrigation systems theory"
    extended = _relation_map([*original, unrelated])
    extended_titles = title_index([*original, unrelated])
    original_selection = _semantic_selection(original_map, title_by_key=title_index(original))
    extended_selection = _semantic_selection(extended, title_by_key=extended_titles)
    assert original_selection.issubset(extended_selection)


def _relation_scope_fixture(tmp_path):
    workspace = JobWorkspace.create(str(tmp_path), "relation-output-scope", job_id="relation-scope-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    summaries = [
        _summary("paper-a", "Paper A", "The treatment improved trust under the first boundary."),
        _summary("paper-b", "Paper B", "The treatment changed trust under the second boundary."),
    ]
    fixture_service = type("FixtureService", (), {})()
    fixture_service.registry = registry
    fixture_service.workspace = workspace
    fixture_service.summaries = summaries
    bind_production_writer_sources(fixture_service, [])
    inventory = load_writer_source_inventory_v1(registry)
    papers = inventory.paper_by_key()

    claim_by_paper = {}
    finding_by_paper = {}
    required_evidence_ids = []
    for paper_key, paper in papers.items():
        finding = next(
            item for item in paper.claims
            if item.claim_type == "empirical_finding" and item.evidence_ids
        )
        boundary = next(
            item for item in paper.claims
            if item.claim_type == "author_interpretation"
            and "bounded" in item.text.casefold()
            and item.evidence_ids
        )
        claim_by_paper[paper_key] = [finding.claim_id, boundary.claim_id]
        finding_by_paper[paper_key] = finding.text
        required_evidence_ids.extend([*finding.evidence_ids, *boundary.evidence_ids])
    paper_keys = sorted(papers)
    relation_id = "relation:confirmed:paper-a-paper-b"
    bundle = {
        "relation_id": relation_id,
        "relation_type": "qualifies",
        "comparison_question": "Compare both findings while retaining each source boundary.",
        "paper_ids": paper_keys,
        "claim_ids_left": claim_by_paper[paper_keys[0]],
        "claim_ids_right": claim_by_paper[paper_keys[1]],
        "findings_left": [finding_by_paper[paper_keys[0]]],
        "findings_right": [finding_by_paper[paper_keys[1]]],
        "required_evidence_ids": sorted(set(required_evidence_ids)),
        "provided_evidence_ids": sorted(set(required_evidence_ids)),
        "missing_evidence_ids": [],
        "evidence_completeness": "complete",
        "decision": "confirmed",
    }
    routes = [
        {
            "topic_id": f"topic:{paper_key}",
            "logical_node_id": f"topic_synthesis:{paper_key}",
            "paper_ids": [paper_key],
            "fragments": [],
        }
        for paper_key in paper_keys
    ]
    scope = build_candidate_output_scope_v1(
        inventory,
        routes,
        selected_relation_ids=[relation_id],
        relation_bundles=[bundle],
    )
    return paper_keys, bundle, scope


def test_confirmed_complete_relation_bundle_creates_source_closed_two_sided_task(tmp_path):
    paper_keys, bundle, scope = _relation_scope_fixture(tmp_path)
    group = next(item for item in scope.claim_groups if item.task_id.startswith("relation_comparison:"))
    slots = [item for item in scope.claim_slots if item.claim_group_id == group.claim_group_id]

    assert group.task_id == f"relation_comparison:{bundle['relation_id']}"
    assert group.paper_keys == tuple(paper_keys)
    assert {item.paper_key for item in slots} == set(paper_keys)
    assert all(item.study_id == "" for item in slots)
    assert all(item.source_claim_ids and item.evidence_ids for item in slots)
    assert all(item.source_field_ids for item in slots)
    assert group.claim_kind == "model_authored_relation_comparison_slot"
    assert group.synthesis_claim_id.startswith("synthesis:relation_comparison:")
    assert group.provider_result_id == ""
    assert group.to_dict()["claim_seed"] is True
    assert group.to_dict()["task_contract"]["comparison_question"] == bundle["comparison_question"]
    assert group.to_dict()["task_contract"]["relation_id_is_not_a_conclusion"] is True


def test_for_groups_projects_exact_relation_group_and_rejects_partial_or_invalid_assignment(tmp_path):
    paper_keys, _bundle, scope = _relation_scope_fixture(tmp_path)
    group = next(item for item in scope.claim_groups if item.task_id.startswith("relation_comparison:"))

    assigned = scope.for_groups([group.claim_group_id], allowed_paper_keys=paper_keys)
    half_assignment = scope.for_papers([paper_keys[0]])

    assert assigned.claim_groups == (group,)
    assert {slot.claim_group_id for slot in assigned.claim_slots} == {group.claim_group_id}
    assert set(assigned.paper_keys) == set(paper_keys)
    assert assigned.task_ids == (group.task_id,)
    assert half_assignment.claim_groups == ()
    assert half_assignment.claim_slots == ()
    with pytest.raises(CandidateOutputScopeError, match="unknown claim group"):
        scope.for_groups(["unknown-group"])
    with pytest.raises(CandidateOutputScopeError, match="duplicate"):
        scope.for_groups([group.claim_group_id, group.claim_group_id])
    with pytest.raises(CandidateOutputScopeError, match="missing paper support"):
        scope.for_groups([group.claim_group_id], allowed_paper_keys=[paper_keys[0]])

    comparison_scope = scope.for_groups([group.claim_group_id], allowed_paper_keys=paper_keys)
    comparison_text = "The two source findings differ under their recorded boundaries."
    payload = {
        "sections": [{
            "section_id": "section:relation-comparison",
            "task_ids": [group.task_id],
            "title": "Comparing both source findings",
            "goal": "Describe the comparison with both source boundaries.",
            "paper_keys": paper_keys,
            "relation_ids": [group.relation_ids[0]],
            "claims": [comparison_text],
            "claim_support": [
                {
                    "claim_slot_id": slot.claim_slot_id,
                    "task_id": slot.task_id,
                    "claim": comparison_text,
                    "paper_key": slot.paper_key,
                    "primary_claim_id": slot.primary_claim_id,
                    "study_id": slot.study_id,
                    "source_claim_ids": list(slot.source_claim_ids),
                    "evidence_ids": list(slot.evidence_ids),
                    "source_field_ids": list(slot.source_field_ids),
                }
                for slot in comparison_scope.claim_slots
            ],
        }],
    }
    comparison_scope.validate(payload)
    one_sided = deepcopy(payload)
    one_sided["sections"][0]["claim_support"] = [
        row for row in one_sided["sections"][0]["claim_support"]
        if row["paper_key"] == paper_keys[1]
    ]
    one_sided["sections"][0]["paper_keys"] = [paper_keys[1]]
    with pytest.raises(CandidateOutputScopeError, match="missing one or more paper support closures"):
        comparison_scope.validate(one_sided)


def test_relation_output_scope_requires_confirmed_selected_complete_source_bundle(tmp_path):
    paper_keys, bundle, _scope = _relation_scope_fixture(tmp_path)
    workspace = JobWorkspace.create(str(tmp_path / "invalid"), "invalid-relation-scope", job_id="invalid-relation-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    fixture_service = type("FixtureService", (), {})()
    fixture_service.registry = registry
    fixture_service.workspace = workspace
    fixture_service.summaries = [
        _summary("paper-a", "Paper A", "The treatment improved trust under the first boundary."),
        _summary("paper-b", "Paper B", "The treatment changed trust under the second boundary."),
    ]
    bind_production_writer_sources(fixture_service, [])
    inventory = load_writer_source_inventory_v1(registry)
    routes = [
        {"topic_id": f"topic:{paper_key}", "logical_node_id": f"topic_synthesis:{paper_key}", "paper_ids": [paper_key], "fragments": []}
        for paper_key in paper_keys
    ]

    incomplete = {**bundle, "evidence_completeness": "incomplete", "missing_evidence_ids": ["missing-evidence"]}
    with pytest.raises(CandidateOutputScopeError, match="complete selected relation bundle"):
        build_candidate_output_scope_v1(
            inventory, routes, selected_relation_ids=[bundle["relation_id"]], relation_bundles=[incomplete],
        )

    with pytest.raises(CandidateOutputScopeError, match="selected confirmed relation"):
        build_candidate_output_scope_v1(
            inventory, routes, selected_relation_ids=[], relation_bundles=[bundle],
        )

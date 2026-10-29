"""Zero-POST producer projections for stable, complete selected topic sources."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import re
import socket

import pytest

from outline.semantic_chunking import TopicRoute, build_paper_content_layers
from outline.topic_source_scope import (
    TopicSourceScopeError,
    build_topic_source_selection_v2,
    ownership_compatible_v2,
    ownership_scope_v2,
    stable_topic_batch_identity_v2,
)
from outline.v3_evidence import build_outline_evidence_views
from outline.v3_models import TopicSynthesis
from runtime.provider_runtime import hash_json
from tests.test_outline_v3_semantic_execution import _executor, _summary


@pytest.fixture(autouse=True)
def _no_transport(monkeypatch):
    def denied(*_args, **_kwargs):
        pytest.fail("topic source-scope units must not dispatch providers or HTTP")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def source_case(tmp_path):
    summaries = [_summary(f"paper-{i}", f"Source {i}", f"The source result {i} is conditional.") for i in (0, 4, 5, 9)]
    changed = deepcopy(summaries)
    changed[-1]["core_analysis"]["limitations"] = "Paper 9 applies only in the revised measured subgroup."
    versions = []
    for index, sources in enumerate((summaries, changed)):
        def denied(*_args, **_kwargs):
            pytest.fail("builder fixture unexpectedly dispatched a provider")

        executor = _executor(tmp_path / str(index), summaries=sources, provider=denied, stability_mode="off")
        evidence = build_outline_evidence_views(sources, executor.job_id)
        layers = build_paper_content_layers(sources, evidence, job_id=executor.job_id)
        versions.append({"executor": executor, "evidence": evidence, "layers": layers})
    return versions


def _request(version, *, papers=("paper-4", "paper-5"), bridges=(), ordinal=1):
    topic = TopicSynthesis(
        topic_id="topic:incremental-U45", fragment_id="fragment:incremental-U45-v1",
        paper_ids=list(papers), bridge_paper_ids=list(bridges),
        supporting_evidence_ids=[value for paper in (*papers, *bridges)
                                 for value in version["layers"].dossier_by_paper[paper].evidence_ids],
    )
    route = TopicRoute(
        topic_id=topic.topic_id, question="Compare findings with all recorded source limitations.",
        paper_ids=list(papers), bridge_paper_ids=list(bridges), dimensions=["finding", "boundary"],
    )
    return version["executor"]._build_topic_provider_request(
        [topic], topic_routes={topic.topic_id: route}, evidence_model=version["evidence"],
        content_layers_model=version["layers"], batch_index=ordinal,
    )


def _selection(version, request):
    return build_topic_source_selection_v2(
        request, evidence_model=version["evidence"], content_layers_model=version["layers"],
    )


def _successor_wire(version, request):
    """Compose the exact proposed executor seams without changing shared code."""
    selection = _selection(version, request)
    identity = stable_topic_batch_identity_v2(request)
    wire = deepcopy(request)
    wire["provider_semantic_contract_version"] = "source-closed-provider/v2"
    wire["evidence_projection"].pop("content_layers_hash")
    wire["evidence_projection"]["selected_source_projection"] = {
        "schema_version": selection["schema_version"], "content_hash": selection["content_hash"],
        "paper_keys": selection["paper_keys"],
    }
    wire["hierarchy"]["batch_id"] = identity["batch_id"]
    return version["executor"]._attach_prompt_authority(identity["provider_node_id"], wire)


def test_real_builder_u45_wire_stays_exact_while_a09_qualifier_changes(source_case):
    before, after = source_case
    old, new = _request(before), _request(after, ordinal=9)
    assert old["evidence_units"] == new["evidence_units"]
    assert old["evidence_projection"]["content_layers_hash"] != new["evidence_projection"]["content_layers_hash"]
    assert _selection(before, old) == _selection(after, new)
    assert _successor_wire(before, old) == _successor_wire(after, new)
    a_old = _request(before, papers=("paper-0", "paper-9"))
    a_new = _request(after, papers=("paper-0", "paper-9"))
    assert _selection(before, a_old)["content_hash"] != _selection(after, a_new)["content_hash"]
    assert _successor_wire(before, a_old) != _successor_wire(after, a_new)


def test_bridge_source_and_full_selected_values_are_included_without_mutation(source_case):
    before, after = source_case
    request = _request(before, bridges=("paper-9",))
    original = deepcopy(request)
    selection = _selection(before, request)
    assert selection["paper_keys"] == ["paper-4", "paper-5", "paper-9"]
    for row in selection["papers"]:
        paper = row["paper_key"]
        assert row["evidence_view"] == next(view.to_dict() for view in before["evidence"].views if view.paper_key == paper)
        assert row["index_card"] == next(card.to_dict() for card in before["layers"].index_cards if card.paper_id == paper)
        assert row["dossier"] == before["layers"].dossier_by_paper[paper].to_dict()
    assert selection["content_hash"] == hash_json({key: value for key, value in selection.items() if key != "content_hash"})
    assert _selection(after, _request(after, bridges=("paper-9",)))["content_hash"] != selection["content_hash"]
    assert request == original
    selection["papers"][0]["dossier"]["limitations"].append("external mutation")
    assert _selection(before, request)["papers"][0]["dossier"] != selection["papers"][0]["dossier"]


def test_helper_does_not_rewrite_legacy_hashes_or_add_authority_flags(source_case):
    before = source_case[0]
    request = _request(before, ordinal=2)
    original = deepcopy(request)
    result = _selection(before, request)
    assert request == original
    assert request["hierarchy"]["batch_id"] == "topic_batch_2"
    assert request["evidence_projection"]["content_layers_hash"] == before["layers"].content_hash
    assert "provider_semantic_contract_version" not in request
    assert not {"verified", "authorized", "registry_job_id", "artifact_hash"}.intersection(result)


def test_stable_identity_is_order_independent_and_preserves_digit_consumers():
    topics = [TopicSynthesis(topic_id="t", fragment_id=f"fragment:{i}", paper_ids=["p5", "p4"], bridge_paper_ids=["p9"]) for i in (1, 2)]
    identity = stable_topic_batch_identity_v2(topics)
    reordered = [{"topic_id": topic.topic_id, "fragment_id": topic.fragment_id,
                  "paper_ids": list(reversed(topic.paper_ids)), "bridge_paper_ids": topic.bridge_paper_ids}
                 for topic in reversed(topics)]
    assert stable_topic_batch_identity_v2({"topics": reordered, "ignored_global_hash": "foreign"}) == identity
    assert 0 < identity["batch_number"] < 2**48
    assert re.fullmatch(r"topic_synthesis_provider:batch:[1-9][0-9]*", identity["provider_node_id"])
    assert identity["batch_id"] == f"topic_batch_{identity['batch_number']}"
    assert len(identity["identity_hash"]) == 64


@pytest.mark.parametrize("field", ["topic_id", "fragment_id", "paper_ids", "bridge_paper_ids"])
def test_complete_grouping_identity_changes_when_a_source_role_or_fragment_changes(field):
    topic = {"topic_id": "t", "fragment_id": "f", "paper_ids": ["p4"], "bridge_paper_ids": ["p5"]}
    changed = deepcopy(topic)
    changed[field] = ["p9"] if field.endswith("ids") else "different"
    assert stable_topic_batch_identity_v2([topic])["identity_hash"] != stable_topic_batch_identity_v2([changed])["identity_hash"]


def test_ownership_compatibility_prevents_disjoint_or_partly_overlapping_batches():
    u = TopicSynthesis(topic_id="u", paper_ids=["p4"], bridge_paper_ids=["p5"])
    same = TopicSynthesis(topic_id="same", paper_ids=["p5", "p4"])
    a = TopicSynthesis(topic_id="a", paper_ids=["p0", "p9"])
    overlap = TopicSynthesis(topic_id="overlap", paper_ids=["p4", "p9"])
    assert ownership_scope_v2([u]) == ("p4", "p5")
    assert ownership_compatible_v2([u], [same])
    assert not ownership_compatible_v2([u], [a])
    assert not ownership_compatible_v2([u], [overlap])
    assert not ownership_compatible_v2([u, a], [same])


@pytest.mark.parametrize("fault", ["empty", "duplicate_paper", "duplicate_fragment", "foreign_model"])
def test_identity_rejects_ambiguous_or_unsupported_members(fault):
    topic = TopicSynthesis(topic_id="t", fragment_id="f", paper_ids=["p4"])
    value = [] if fault == "empty" else [topic]
    if fault == "duplicate_paper":
        value = [replace(topic, paper_ids=["p4", "p4"])]
    elif fault == "duplicate_fragment":
        value.append(replace(topic, topic_id="another"))
    elif fault == "foreign_model":
        value = [object()]
    with pytest.raises(TopicSourceScopeError):
        stable_topic_batch_identity_v2(value)


@pytest.mark.parametrize("model", ["view", "card", "dossier"])
@pytest.mark.parametrize("fault", ["missing", "duplicate", "foreign_hash"])
def test_selection_rejects_missing_duplicate_or_foreign_source_rows(source_case, model, fault):
    version = dict(source_case[0])
    request = _request(source_case[0])
    if model == "view":
        rows, field, owner = version["evidence"].views, "views", "evidence"
        key = lambda row: row.paper_key
    else:
        field = "index_cards" if model == "card" else "dossiers"
        rows, owner = getattr(version["layers"], field), "layers"
        key = lambda row: row.paper_id
    selected = next(row for row in rows if key(row) == "paper-4")
    if fault == "missing":
        rows = [row for row in rows if key(row) != "paper-4"]
    elif fault == "duplicate":
        rows = [*rows, selected]
    else:
        rows = [replace(row, source_summary_hash="f" * 64) if key(row) == "paper-4" else row for row in rows]
    version[owner] = replace(version[owner], **{field: rows})
    with pytest.raises(TopicSourceScopeError):
        _selection(version, request)


@pytest.mark.parametrize("fault", ["unknown_paper", "foreign_unit", "duplicate_unit", "missing_unit", "planned_ids", "unsupported"])
def test_selection_rejects_foreign_incomplete_or_unsupported_request_scope(source_case, fault):
    version = source_case[0]
    request = deepcopy(_request(version))
    if fault == "unknown_paper":
        request["topics"][0]["paper_ids"].append("outside-corpus")
    elif fault == "foreign_unit":
        request["evidence_units"][0]["paper_key"] = "paper-9"
    elif fault == "duplicate_unit":
        request["evidence_units"].append(deepcopy(request["evidence_units"][0]))
    elif fault == "missing_unit":
        request["evidence_units"] = request["evidence_units"][1:]
    elif fault == "planned_ids":
        request["planned_evidence_unit_ids"].append("outside-unit")
    else:
        request["semantic_contract_version"] = "unknown/v1"
    with pytest.raises(TopicSourceScopeError):
        _selection(version, request)


@pytest.mark.parametrize("field", ["question", "dimensions", "output_contract"])
def test_complete_actual_wire_still_changes_for_question_dimensions_and_output_rules(source_case, field):
    version = source_case[0]
    original = _request(version)
    changed = deepcopy(original)
    if field == "question":
        changed["topics"][0]["question"] += " Preserve a distinct mechanism."
    elif field == "dimensions":
        changed["topics"][0]["dimensions"].append("mechanism")
    else:
        changed["output_contract"]["max_output_tokens"] += 128
    assert stable_topic_batch_identity_v2(original) == stable_topic_batch_identity_v2(changed)
    assert _selection(version, original) == _selection(version, changed)
    assert _successor_wire(version, original) != _successor_wire(version, changed)


@pytest.mark.parametrize("fault", ["model_version", "canonical_owner", "dossier_ref", "unit_hash", "blocked_dossier", "topic_unit"])
def test_selection_rejects_unsupported_or_foreign_binding_details(source_case, fault):
    version = dict(source_case[0])
    request = deepcopy(_request(version))
    if fault == "model_version":
        version["layers"] = replace(version["layers"], artifact_version="v99")
    elif fault == "canonical_owner":
        version["evidence"] = replace(version["evidence"], views=[
            replace(row, canonical_paper_key="paper-9") if row.paper_key == "paper-4" else row
            for row in version["evidence"].views
        ])
    elif fault == "dossier_ref":
        version["layers"] = replace(version["layers"], index_cards=[
            replace(row, evidence_package_id="dossier:paper-9") if row.paper_id == "paper-4" else row
            for row in version["layers"].index_cards
        ])
    elif fault == "unit_hash":
        request["evidence_units"][0]["evidence_unit_hash"] = "f" * 64
    elif fault == "blocked_dossier":
        version["layers"] = replace(version["layers"], dossiers=[
            replace(row, status="blocked") if row.paper_id == "paper-4" else row
            for row in version["layers"].dossiers
        ])
    else:
        request["topics"][0]["planned_evidence_unit_ids"] = ["outside-unit"]
    with pytest.raises(TopicSourceScopeError):
        _selection(version, request)

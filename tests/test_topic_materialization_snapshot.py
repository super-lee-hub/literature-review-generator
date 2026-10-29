"""Topic planning snapshots serialize standard evidence models once per operation."""
from __future__ import annotations

import pytest

from outline.semantic_chunking import TopicRoute, build_paper_content_layers
from outline.topic_materialization_snapshot import (
    TopicMaterializationSourceChanged,
    snapshot_topic_materialization,
)
from outline.v3_evidence import build_outline_evidence_views
from outline.v3_executor import OutlineV3ExecutionError, OutlineV3Executor
from outline.v3_models import (
    EvidenceClaim,
    OutlineEvidenceView,
    OutlineEvidenceViews,
    PaperContentLayers,
    PaperEvidenceDossier,
    PaperIndexCard,
    ResearchUnit,
    SourceFieldLedgerEntry,
    TopicSynthesis,
)
from runtime.source_revision import PROVIDER_SEMANTIC_CONTRACT_V2
from tests.test_outline_v3_semantic_execution import _summary
from tests.test_topic_source_scope import source_case


_BOUNDARY = "The effect was zero in the enrolled sample."
_EXTRA_SOURCE_FIELD = SourceFieldLedgerEntry(
    source_field_id="source-field:paper-a:findings:extra",
    source_path="core_analysis.findings",
    source_value=_BOUNDARY,
    disposition="exact",
    canonical_field="findings",
    scope="paper",
    source_summary_hash="source-summary:paper-a",
)


def _models():
    summaries = [_summary("paper-a", "Bounded source", _BOUNDARY)]
    evidence = build_outline_evidence_views(summaries, "topic-snapshot-test")
    layers = build_paper_content_layers(summaries, evidence, job_id="topic-snapshot-test")
    dossier = layers.dossier_by_paper["paper-a"]
    dossier.source_field_ledger.append(_EXTRA_SOURCE_FIELD)
    dossier.source_field_ledger.append(SourceFieldLedgerEntry(
        source_field_id="source-field:paper-a:study-a:findings",
        source_path="core_analysis.findings",
        source_value=_BOUNDARY,
        disposition="exact",
        canonical_field="findings",
        scope="explicit_study",
        study_id="study-a",
        source_summary_hash=dossier.source_summary_hash,
    ))
    dossier.evidence_ids_by_field.setdefault("findings", []).append("evidence:paper-a:findings")
    dossier.evidence_text_by_id["evidence:paper-a:findings"] = _BOUNDARY
    dossier.research_units.append(ResearchUnit(
        study_id="study-a",
        parent_paper_id="paper-a",
        research_questions=["Does the treatment affect the outcome?"],
        findings=[_BOUNDARY],
        claims=[EvidenceClaim(
            claim_id="claim:paper-a:study-a:findings",
            claim_type="empirical_finding",
            text=_BOUNDARY,
            study_id="study-a",
            evidence_ids=["evidence:paper-a:findings"],
            source_locator="p. 4",
            source_summary_hash=dossier.source_summary_hash,
        )],
        evidence_ids=["evidence:paper-a:findings"],
        source_summary_hash=dossier.source_summary_hash,
        source_study_id="study-a",
        source_field_ids=["source-field:paper-a:study-a:findings"],
    ))
    return evidence, layers


def _complete_unit(view, dossier):
    return OutlineV3Executor._complete_topic_evidence_unit(
        view, dossier, fields=("findings",),
    )


@pytest.fixture
def source_scope_case(tmp_path):
    # Reuse the exact source_case builder/controls from the producer-scope tests.
    return getattr(source_case, "__wrapped__")(tmp_path)


def test_snapshot_preserves_serialized_models_hashes_and_complete_source_units():
    evidence, layers = _models()
    expected_evidence = evidence.to_dict()
    expected_evidence_hash = evidence.content_hash
    expected_view_payloads = [view.to_dict() for view in evidence.views]
    expected_layers = layers.to_dict()
    expected_layers_hash = layers.content_hash
    expected_dossiers = {
        dossier.paper_id: (dossier.to_dict(), dossier.content_hash, dossier.evidence_ids)
        for dossier in layers.dossiers
    }
    expected_cards = [card.to_dict() for card in layers.index_cards]
    expected_unit = _complete_unit(evidence.views[0], layers.dossier_by_paper["paper-a"])

    with snapshot_topic_materialization(evidence, layers) as snapshot:
        assert snapshot.cached is True
        assert isinstance(snapshot.evidence_model, OutlineEvidenceViews)
        assert isinstance(snapshot.content_layers_model, PaperContentLayers)
        assert snapshot.evidence_model.to_dict() == expected_evidence
        assert snapshot.evidence_model.canonical_payload() == evidence.canonical_payload()
        assert snapshot.evidence_model.content_hash == expected_evidence_hash
        assert snapshot.evidence_model.artifact_hash == expected_evidence_hash
        assert [view.to_dict() for view in snapshot.evidence_model.views] == expected_view_payloads
        assert all(isinstance(view, OutlineEvidenceView) for view in snapshot.evidence_model.views)
        assert [view.view_hash for view in snapshot.evidence_model.evidence_views] == [
            view.view_hash for view in evidence.views
        ]

        snapshot_layers = snapshot.content_layers_model
        assert snapshot_layers.to_dict() == expected_layers
        assert snapshot_layers.canonical_payload() == layers.canonical_payload()
        assert snapshot_layers.content_hash == expected_layers_hash
        assert [card.to_dict() for card in snapshot_layers.index_cards] == expected_cards
        assert all(isinstance(card, PaperIndexCard) for card in snapshot_layers.index_cards)
        assert all(isinstance(dossier, PaperEvidenceDossier) for dossier in snapshot_layers.dossiers)
        assert {
            paper_id: (dossier.to_dict(), dossier.content_hash, dossier.evidence_ids)
            for paper_id, dossier in snapshot_layers.dossier_by_paper.items()
        } == expected_dossiers
        assert _complete_unit(
            snapshot.evidence_model.views[0],
            snapshot_layers.dossier_by_paper["paper-a"],
        ) == expected_unit
        assert any(
            row["source_field_id"] == _EXTRA_SOURCE_FIELD.source_field_id
            for row in expected_unit["source_field_ledger"]
        )


def test_explicit_v2_planning_wrapper_accepts_typed_snapshot_models(source_scope_case):
    version = source_scope_case[0]
    executor = version["executor"]
    executor.provider_semantic_contract_version = PROVIDER_SEMANTIC_CONTRACT_V2
    paper_ids = ["paper-4", "paper-5"]
    topic = TopicSynthesis(
        topic_id="topic:typed-snapshot-U45",
        fragment_id="fragment:typed-snapshot-U45-v1",
        paper_ids=paper_ids,
        supporting_evidence_ids=sorted({
            evidence_id
            for paper_id in paper_ids
            for evidence_id in version["layers"].dossier_by_paper[paper_id].evidence_ids
        }),
    )
    route = TopicRoute(
        topic_id=topic.topic_id,
        question="Compare the complete findings and boundaries for these papers.",
        paper_ids=paper_ids,
        dimensions=["finding", "boundary"],
    )
    profile = executor._role_route("topic_synthesis_provider:batch:1").profile

    expanded, batches, plans = executor._plan_topic_provider_batches(
        [topic],
        topic_routes={topic.topic_id: route},
        evidence_model=version["evidence"],
        content_layers_model=version["layers"],
        profile=profile,
    )

    assert expanded and batches and plans
    assert all(plan["planned_wire_ids_equal_materialized_ids"] for plan in plans)
    assert all(str(plan["batch_id"]).startswith("topic_batch_") for plan in plans)


def test_repeated_serialization_uses_cached_values_within_one_operation(monkeypatch):
    evidence, layers = _models()
    calls = {"view": 0, "dossier": 0, "card": 0}
    original_view = OutlineEvidenceView.to_dict
    original_dossier = PaperEvidenceDossier.to_dict
    original_card = PaperIndexCard.to_dict

    def count_view(self):
        calls["view"] += 1
        return original_view(self)

    def count_dossier(self):
        calls["dossier"] += 1
        return original_dossier(self)

    def count_card(self):
        calls["card"] += 1
        return original_card(self)

    monkeypatch.setattr(OutlineEvidenceView, "to_dict", count_view)
    monkeypatch.setattr(PaperEvidenceDossier, "to_dict", count_dossier)
    monkeypatch.setattr(PaperIndexCard, "to_dict", count_card)

    with snapshot_topic_materialization(evidence, layers) as snapshot:
        before = dict(calls)
        for _ in range(4):
            snapshot.evidence_model.to_dict()
            snapshot.evidence_model.content_hash
            snapshot.content_layers_model.to_dict()
            snapshot.content_layers_model.content_hash
            for view in snapshot.evidence_model.views:
                view.to_dict()
                view.view_hash
            for card in snapshot.content_layers_model.index_cards:
                card.to_dict()
            for dossier in snapshot.content_layers_model.dossiers:
                dossier.to_dict()
                dossier.content_hash
        assert calls == before
        assert before["view"] > 0 and before["dossier"] > 0 and before["card"] > 0


def test_payloads_and_delegated_fields_cannot_alias_caller_models_or_cache():
    evidence, layers = _models()
    expected_evidence = evidence.to_dict()
    expected_layers = layers.to_dict()
    original_view = evidence.views[0]
    original_dossier = layers.dossier_by_paper["paper-a"]

    with snapshot_topic_materialization(evidence, layers) as snapshot:
        view = snapshot.evidence_model.views[0]
        dossier = snapshot.content_layers_model.dossier_by_paper["paper-a"]
        assert view is not original_view
        assert dossier is not original_dossier

        view.findings.append("private delegated mutation")
        dossier.overall_context.append("private dossier mutation")
        poisoned_view = view.to_dict()
        poisoned_view["findings"].append("returned payload mutation")
        poisoned_layers = snapshot.content_layers_model.to_dict()
        poisoned_layers["dossiers"][0]["findings"].append("returned layers mutation")

        assert view.to_dict() == expected_evidence["views"][0]
        assert dossier.to_dict() == expected_layers["dossiers"][0]
        assert snapshot.content_layers_model.to_dict() == expected_layers
        assert "private delegated mutation" not in original_view.findings
        assert "private dossier mutation" not in original_dossier.overall_context


def test_mutating_delegated_source_units_and_ledger_cannot_split_cached_serialization():
    evidence, layers = _models()
    expected = layers.dossier_by_paper["paper-a"].to_dict()

    with snapshot_topic_materialization(evidence, layers) as snapshot:
        dossier = snapshot.content_layers_model.dossier_by_paper["paper-a"]
        delegated_units = dossier.research_units
        delegated_ledger = dossier.source_field_ledger
        assert delegated_units and delegated_ledger
        delegated_units[0].findings.append("detached unit mutation")
        delegated_units.clear()
        delegated_ledger.clear()

        assert [unit.to_dict() for unit in dossier.research_units] == expected["research_units"]
        assert [entry.to_dict() for entry in dossier.source_field_ledger] == expected["source_field_ledger"]
        assert dossier.to_dict() == expected
        assert dossier.content_hash == expected["content_hash"]


def test_a_new_operation_observes_source_changes_without_reusing_old_snapshot():
    evidence, layers = _models()
    with snapshot_topic_materialization(evidence, layers) as first:
        first_hash = first.content_layers_model.content_hash
        first_payload = first.content_layers_model.to_dict()
    layers.dossier_by_paper["paper-a"].findings.append("new measured boundary")
    with snapshot_topic_materialization(evidence, layers) as second:
        assert second.content_layers_model.content_hash != first_hash
        assert second.content_layers_model.to_dict() != first_payload
        assert "new measured boundary" in second.content_layers_model.dossier_by_paper["paper-a"].findings


@pytest.mark.parametrize("source", ("evidence", "content_layers"))
def test_original_model_drift_rejects_scope_exit(source):
    evidence, layers = _models()
    with pytest.raises(TopicMaterializationSourceChanged, match="changed during topic materialization"):
        with snapshot_topic_materialization(evidence, layers):
            if source == "evidence":
                evidence.views[0].findings.append("outside mutation")
            else:
                layers.dossier_by_paper["paper-a"].findings.append("outside mutation")


def test_copy_time_source_drift_is_rejected_before_snapshot_is_yielded(monkeypatch):
    import outline.topic_materialization_snapshot as module

    evidence, layers = _models()
    original_deepcopy = module.deepcopy
    yielded = []

    def mutate_during_model_copy(value):
        if isinstance(value, tuple) and len(value) == 2 and value[0] is evidence and value[1] is layers:
            evidence.views[0].findings.append("copy-time drift")
        return original_deepcopy(value)

    monkeypatch.setattr(module, "deepcopy", mutate_during_model_copy)
    with pytest.raises(TopicMaterializationSourceChanged, match="private model snapshot differs"):
        with snapshot_topic_materialization(evidence, layers) as snapshot:
            yielded.append(snapshot)
    assert yielded == []


def test_custom_dossier_subclass_uses_uncached_original_path():
    evidence, layers = _models()
    calls: list[bool] = []

    class CustomDossier(PaperEvidenceDossier):
        def to_dict(self):
            calls.append(True)
            return super().to_dict()

    custom = CustomDossier.from_dict(layers.dossier_by_paper["paper-a"].to_dict())
    layers.dossiers[0] = custom
    with snapshot_topic_materialization(evidence, layers) as snapshot:
        assert snapshot.cached is False
        assert snapshot.evidence_model is evidence
        assert snapshot.content_layers_model is layers
        before = len(calls)
        first = snapshot.content_layers_model.dossier_by_paper["paper-a"].to_dict()
        second = snapshot.content_layers_model.dossier_by_paper["paper-a"].to_dict()
        assert first == second
        assert len(calls) == before + 2


def test_uncached_custom_dossier_does_not_bypass_source_field_validation():
    evidence, layers = _models()

    class InvalidLedgerDossier(PaperEvidenceDossier):
        def to_dict(self):
            payload = super().to_dict()
            payload["source_field_ledger"][0]["disposition"] = "unsupported"
            return payload

    invalid = InvalidLedgerDossier.from_dict(layers.dossier_by_paper["paper-a"].to_dict())
    layers.dossiers[0] = invalid
    with snapshot_topic_materialization(evidence, layers) as snapshot:
        assert snapshot.cached is False
        with pytest.raises(OutlineV3ExecutionError, match="invalid source-field ledger entry"):
            _complete_unit(snapshot.evidence_model.views[0], invalid)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"source_field_id": ""}, "source_field_id is required"),
        ({"source_path": ""}, "source_path is required"),
        ({"source_value": ""}, "source_value is required"),
        ({"disposition": "invented"}, "unsupported source field disposition"),
        ({"scope": "invented"}, "unsupported source field scope"),
        ({"disposition": "rewritten", "derived_value": ""}, "rewritten source fields require"),
        ({"scope": "explicit_study", "study_id": ""}, "explicit study source fields require"),
        ({"scope": "paper", "study_id": "study-1"}, "paper or unresolved source fields cannot claim"),
    ],
)
def test_source_field_unit_constructor_still_validates_each_contract_field(changes, message):
    values = {
        "source_field_id": "source-field:paper-a:findings",
        "source_path": "core_analysis.findings",
        "source_value": _BOUNDARY,
        "disposition": "exact",
        "scope": "paper",
    }
    values.update(changes)
    with pytest.raises(ValueError, match=message):
        SourceFieldLedgerEntry(**values)


def test_evidence_claim_constructor_still_rejects_invalid_claim_modalities():
    with pytest.raises(ValueError, match="unsupported evidence claim type"):
        EvidenceClaim("claim-1", "unsupported", "A bounded claim.")

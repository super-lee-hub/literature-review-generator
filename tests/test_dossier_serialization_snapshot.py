"""Dossier serialization hashes its exact snapshot without duplicate expansion."""
from __future__ import annotations

from outline.semantic_chunking import build_paper_content_layers
from outline.v3_evidence import build_outline_evidence_views
from outline.v3_models import PaperEvidenceDossier, compute_v3_hash
from tests.test_outline_v3_semantic_execution import _summary


def _dossier():
    summaries = [_summary("paper-a", "Bounded source", "The effect was zero in the enrolled sample.")]
    evidence = build_outline_evidence_views(summaries, "serialization-snapshot")
    return build_paper_content_layers(summaries, evidence, job_id="serialization-snapshot").dossier_by_paper["paper-a"]


def test_serialized_payload_and_hash_match_previous_contract_with_one_materialization(monkeypatch):
    dossier = _dossier()
    expected = {**dossier.canonical_payload(), "evidence_ids": dossier.evidence_ids, "content_hash": dossier.content_hash}
    original = PaperEvidenceDossier.canonical_payload
    calls = []

    def canonical(self):
        calls.append(self.dossier_id)
        return original(self)

    monkeypatch.setattr(PaperEvidenceDossier, "canonical_payload", canonical)
    actual = dossier.to_dict()
    assert actual == expected
    assert compute_v3_hash(actual) == compute_v3_hash(expected)
    assert calls == [dossier.dossier_id]


def test_a_new_serialization_observes_changed_boundary_without_stale_hash():
    dossier = _dossier()
    first = dossier.to_dict()
    dossier.overall_context.append("The effect applies only at the measured follow-up.")
    second = dossier.to_dict()
    assert second["content_hash"] != first["content_hash"]
    assert second["content_hash"] == compute_v3_hash(dossier.canonical_payload())
    assert "The effect applies only at the measured follow-up." not in first["overall_context"]


def test_custom_subclass_hash_policy_is_preserved():
    class CustomDossier(PaperEvidenceDossier):
        @property
        def content_hash(self):
            return "c" * 64

    dossier = CustomDossier.from_dict(_dossier().to_dict())
    assert dossier.to_dict()["content_hash"] == "c" * 64

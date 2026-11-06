"""One topic-unit invocation reuses its detached source serialization."""
from __future__ import annotations

from outline.semantic_chunking import build_paper_content_layers
from outline.v3_evidence import build_outline_evidence_views
from outline.v3_models import OutlineEvidenceView, PaperEvidenceDossier, compute_v3_hash
from tests.test_outline_v3_semantic_execution import _dossier_with_claims, _executor


def test_splitter_serializes_each_source_once_with_identical_output(tmp_path, monkeypatch):
    executor = _executor(tmp_path, stability_mode="off")
    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    layers = build_paper_content_layers(executor.summaries, evidence, job_id=executor.job_id)
    dossier = _dossier_with_claims(layers.dossier_by_paper["paper-a"])
    view = next(row for row in evidence.views if row.paper_key == "paper-a")
    kwargs = {"fields": ["findings", "limitations"], "chunk_target_tokens": 700}
    expected = executor._complete_topic_evidence_units(view, dossier, **kwargs)
    calls = {"dossier": 0, "view": 0}
    original_dossier = PaperEvidenceDossier.to_dict
    original_view = OutlineEvidenceView.to_dict

    def dossier_dict(self):
        if self is dossier:
            calls["dossier"] += 1
        return original_dossier(self)

    def view_dict(self):
        if self is view:
            calls["view"] += 1
        return original_view(self)

    monkeypatch.setattr(PaperEvidenceDossier, "to_dict", dossier_dict)
    monkeypatch.setattr(OutlineEvidenceView, "to_dict", view_dict)
    actual = executor._complete_topic_evidence_units(view, dossier, **kwargs)
    assert actual == expected and compute_v3_hash(actual) == compute_v3_hash(expected)
    # Captured from frozen706's independent old splitter, SHA ca66b7ce...39edd9.
    assert compute_v3_hash(actual) == "ccb323281c6fe633702301a6f6eb40214e40203fa600abd06a84fd540a4b21ca"
    assert calls == {"dossier": 1, "view": 1}
    changed = next(unit for unit in actual if unit.get("evidence_text_by_id"))
    key = next(iter(changed["evidence_text_by_id"]))
    changed["evidence_text_by_id"][key] = "Caller-only mutation."
    assert compute_v3_hash(executor._complete_topic_evidence_units(view, dossier, **kwargs)) == (
        "ccb323281c6fe633702301a6f6eb40214e40203fa600abd06a84fd540a4b21ca"
    )

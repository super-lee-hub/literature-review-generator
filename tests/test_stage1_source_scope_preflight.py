"""Whole-source inspection must precede Stage 1 preprocessing side effects."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_current_stage1_generation import _service, _write_pdf


def test_invalid_later_pdf_blocks_before_any_prepare_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    first = tmp_path / "first.pdf"
    _write_pdf(first)
    service, bundle = _service(tmp_path, first, lambda **kwargs: {})
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"%PDF-1.4\ncorrupt source")
    second = replace(bundle.paper_work_items[0], canonical_paper_key="second-paper",
                     source_paper_id="second-paper", source_pdf=str(bad))
    bundle = replace(bundle, paper_work_items=[*bundle.paper_work_items, second])
    prepares: list[str] = []

    def prepare(item, previous, **kwargs):
        prepares.append(item.canonical_paper_key)
        raise AssertionError("preprocess side effect preceded whole-source preflight")

    monkeypatch.setattr(service, "_prepare_item", prepare)
    with pytest.raises(RuntimeError, match="source scope.*PDF"):
        service.run(bundle)
    assert prepares == []


def test_source_scope_is_ready_before_the_first_prepare_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pdf = tmp_path / "source.pdf"
    _write_pdf(pdf)
    service, bundle = _service(tmp_path, pdf, lambda **kwargs: {})

    def prepare(item, previous, **kwargs):
        record = service.registry.get("stage1_source_scope")
        assert record is not None
        service.registry.verify_ready_artifact_closure(record)
        import json
        scope = json.loads(Path(record.path).read_text(encoding="utf-8"))
        assert scope["papers"][0]["page_count"] == 1
        assert scope["papers"][0]["paper_key"] == item.canonical_paper_key
        raise RuntimeError("observed frozen scope before prepare")

    monkeypatch.setattr(service, "_prepare_item", prepare)
    with pytest.raises(RuntimeError, match="observed frozen scope"):
        service.run(bundle)


def test_source_scope_detects_pdf_drift_before_preprocessing(tmp_path: Path):
    pdf = tmp_path / "source.pdf"
    _write_pdf(pdf)
    service, bundle = _service(tmp_path, pdf, lambda **kwargs: {})
    service._freeze_source_scope(bundle)
    pdf.write_bytes(b"%PDF-1.4\nchanged source")
    with pytest.raises(RuntimeError, match="binding changed"):
        service._prepare_item(bundle.paper_work_items[0], None)


def test_source_scope_rejects_password_protected_pdf_before_prepare(tmp_path: Path, monkeypatch):
    import fitz

    pdf = tmp_path / "source.pdf"
    _write_pdf(pdf)
    service, bundle = _service(tmp_path, pdf, lambda **kwargs: {})
    protected = tmp_path / "protected.pdf"
    with fitz.open(pdf) as document:
        document.save(protected, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    item = replace(bundle.paper_work_items[0], source_pdf=str(protected))
    bundle = replace(bundle, paper_work_items=[item])
    monkeypatch.setattr(service, "_prepare_item", lambda *args: pytest.fail("must not preprocess"))
    with pytest.raises(RuntimeError, match="source scope.*PDF"):
        service.run(bundle)


def test_expected_call_graph_depends_on_the_frozen_source_scope(tmp_path: Path):
    import json

    pdf = tmp_path / "source.pdf"
    _write_pdf(pdf)
    service, bundle = _service(tmp_path, pdf, lambda **kwargs: {})
    scope = service._freeze_source_scope(bundle)
    service._predeclare_expected_calls(bundle, ())
    graph = service.registry.get("stage1:provider_expected_call_graph")
    assert graph is not None
    service.registry.verify_ready_artifact_closure(graph)
    payload = json.loads(Path(graph.path).read_text(encoding="utf-8"))
    assert payload["source_scope_artifact_id"] == scope.artifact_id
    assert payload["source_scope_artifact_hash"] == scope.content_hash
    assert any(ref.artifact_id == scope.artifact_id and ref.content_hash == scope.content_hash
               for ref in graph.depends_on)


def test_existing_source_bundle_must_match_the_preflight_input(tmp_path: Path, monkeypatch):
    pdf = tmp_path / "source.pdf"
    _write_pdf(pdf)
    service, bundle = _service(tmp_path, pdf, lambda **kwargs: {})
    service._ensure_durable_input_records(bundle)
    changed = replace(bundle, paper_work_items=[replace(bundle.paper_work_items[0],
                                                       canonical_paper_key="changed-paper")])
    monkeypatch.setattr(service, "_prepare_item", lambda *args: pytest.fail("must not preprocess"))
    with pytest.raises(RuntimeError, match="source bundle.*match"):
        service.run(changed)
    assert service.registry.get("stage1_source_scope") is None


def test_source_scope_schema_rejects_an_unrelated_paper_list(tmp_path: Path):
    import json
    from runtime.reconcile import ReconcileValidationError, _validate_stage1_source_scope

    pdf = tmp_path / "source.pdf"
    _write_pdf(pdf)
    service, bundle = _service(tmp_path, pdf, lambda **kwargs: {})
    record = service._freeze_source_scope(bundle)
    payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
    payload["papers"][0]["paper_key"] = "unrelated-paper"
    fake = tmp_path / "forged-scope.json"
    fake.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReconcileValidationError, match="source bundle"):
        _validate_stage1_source_scope(record, fake)

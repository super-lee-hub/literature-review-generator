from pathlib import Path
from types import SimpleNamespace
from typing import cast

import main
from config_loader import ConfigDict
from models import PaperInfo


class _DummyLogger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass

    def success(self, *_args, **_kwargs):
        pass


def test_main_zotero_parse_keeps_structured_partial_diagnostics(tmp_path: Path) -> None:
    report = tmp_path / "partial.txt"
    report.write_text(
        "*\nGood Paper\n作者\tAlice Smith\n*\n作者\tMissing Title",
        encoding="utf-8",
    )
    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=None)
    generator.logger = cast(main.CustomLogger, _DummyLogger())

    assert generator.parse_zotero_report(str(report)) is True
    assert generator.zotero_parse_result["status"] == "partial"
    assert generator.zotero_parse_result["diagnostics"][0]["code"] == "missing_title"
    assert [paper["title"] for paper in generator.papers] == ["Good Paper"]


def test_main_zotero_parse_preserves_missing_source_diagnostic(tmp_path: Path) -> None:
    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=None)
    generator.logger = cast(main.CustomLogger, _DummyLogger())

    assert generator.parse_zotero_report(str(tmp_path / "missing.txt")) is False
    assert generator.zotero_parse_result["status"] == "failed"
    assert generator.zotero_parse_result["diagnostics"][0]["code"] == "source_missing"


def _quality_ready_ai_summary() -> dict:
    return {
        "routing": {
            "paper_type": "empirical",
            "classification_status": "resolved",
            "route_confidence": "high",
            "secondary_candidates": [],
        },
        "core_analysis": {
            "summary": "A detailed summary with enough content to support downstream checks.",
            "key_points": ["Point A"],
            "methodology": "Mixed methods.",
            "findings": "Important findings.",
            "conclusions": "Meaningful conclusions.",
            "relevance": "Relevant to the review.",
            "limitations": "Limited by scope and sampling frame.",
            "theoretical_framework": None,
            "research_gap": None,
            "future_research_directions": [],
        },
        "paper_metadata": {
            "title": None,
            "authors": [],
            "year": None,
            "journal": None,
            "doi": None,
        },
        "specialized_details": {
            "empirical": {
                "research_questions_or_hypotheses": [],
                "data_source_and_size": None,
                "analysis_technique": None,
                "core_variables": {
                    "independent": [],
                    "dependent": [],
                    "mediators": [],
                    "moderators": [],
                    "controls": [],
                    "other_core_constructs": [],
                },
                "sample_characteristics_or_context": None,
            },
            "review": None,
            "conceptual": None,
        },
    }


def test_process_paper_uses_full_pdf_path_returned_by_find_pdf(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pdf_path = tmp_path / "resolved-paper.pdf"
    pdf_path.write_text("dummy pdf body", encoding="utf-8")

    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=None)
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.mode = "zotero"
    generator.config = ConfigDict(
        {
            "Paths": {"library_path": str(tmp_path)},
            "Primary_Reader_API": {
                "api_key": "primary",
                "model": "demo-model",
                "api_base": "https://example.com/v1",
            },
            "Backup_Reader_API": {
                "api_key": "",
                "model": "backup-model",
                "api_base": "https://example.com/v1",
            },
        }
    )
    generator.library_path = str(tmp_path)

    seen: dict[str, str] = {}

    def _prepare_stage1_input(resolved_pdf_path: str, strategy: str):
        seen["pdf_path"] = resolved_pdf_path
        seen["strategy"] = strategy
        return (
            "Resolved Paper\n" + "x" * 600,
            {
                "analysis_input_kind": "text",
                "extractor_used": "test",
                "preprocess_profile": strategy,
            },
        )

    monkeypatch.setattr(main, "create_file_index", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        main,
        "resolve_pdf_match",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="matched",
            selected_path=str(pdf_path),
            to_dict=lambda: {"status": "matched", "selected_path": str(pdf_path)},
        ),
    )
    monkeypatch.setattr(generator, "_check_cancelled", lambda: None)
    monkeypatch.setattr(generator, "_emit_progress", lambda **_kwargs: None)
    monkeypatch.setattr(
        generator,
        "_paper_progress_label",
        lambda paper: str(paper.get("title") or "unknown"),
    )
    monkeypatch.setattr(generator, "_prepare_stage1_input", _prepare_stage1_input)
    monkeypatch.setattr(generator, "_apply_stage1_text_metadata_backfill", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(generator, "_build_stage1_visual_bundle", lambda **_kwargs: {})
    monkeypatch.setattr(
        generator,
        "_build_stage1_model_input",
        lambda **_kwargs: {
            "prompt_text": "prompt",
            "user_message_content": None,
            "selected_visual_refs": [],
            "visual_manifest_path": "",
            "visual_bundle_path": "",
            "input_mode": "text_only",
            "fallback_reason": "",
        },
    )
    monkeypatch.setattr(main, "get_summary_from_ai_with_fallback", lambda *_args, **_kwargs: _quality_ready_ai_summary())
    monkeypatch.setattr(main, "validate_summary_quality", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(generator, "_apply_ai_metadata_backfill", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(generator, "_persist_paper_artifact", lambda *_args, **_kwargs: True)

    paper: PaperInfo = {
        "title": "Resolved Paper",
        "authors": ["Alice Smith"],
        "attachments": ["Resolved Paper"],
    }

    result = generator.process_paper(paper, paper_index=0, file_index=None, total_papers=1)

    assert result is not None
    assert result["status"] == "success"
    assert seen["pdf_path"] == str(pdf_path)
    assert result["paper_info"]["pdf_path"] == str(pdf_path)
    assert result["paper_info"]["source_pdf"] == str(pdf_path)
    assert result["paper_info"]["source_pdf_fingerprint"]


def test_process_paper_blocks_ambiguous_pdf_before_stage1_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=None)
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.mode = "zotero"
    generator.config = ConfigDict({"Paths": {"library_path": str(tmp_path)}})
    generator.library_path = str(tmp_path)
    stage1_called = False

    monkeypatch.setattr(generator, "_check_cancelled", lambda: None)
    monkeypatch.setattr(generator, "_emit_progress", lambda **_kwargs: None)
    monkeypatch.setattr(
        generator,
        "_paper_progress_label",
        lambda paper: str(paper.get("title") or "unknown"),
    )
    monkeypatch.setattr(
        main,
        "resolve_pdf_match",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="ambiguous",
            selected_path="",
            to_dict=lambda: {
                "status": "ambiguous",
                "selected_path": "",
                "candidates": [{"path": "a.pdf"}, {"path": "b.pdf"}],
                "diagnostics": ["top_candidates_within_margin"],
            },
        ),
    )

    def unexpected_stage1(*_args, **_kwargs):
        nonlocal stage1_called
        stage1_called = True
        raise AssertionError("Stage 1 must not run for ambiguous PDF identity")

    monkeypatch.setattr(generator, "_prepare_stage1_input", unexpected_stage1)
    monkeypatch.setattr(main, "get_summary_from_ai_with_fallback", unexpected_stage1)

    result = generator.process_paper(
        {"title": "Ambiguous Paper", "authors": [], "attachments": ["paper.pdf"]},
        paper_index=0,
        file_index=cast(main.FileIndex, object()),
        total_papers=1,
    )

    assert result is not None
    assert result["status"] == "failed"
    assert result["failure_reason"] == "ambiguous_pdf_match"
    assert result["pdf_match"]["status"] == "ambiguous"
    assert stage1_called is False


def test_process_paper_quarantines_doi_mismatch_before_stage1_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pdf_path = tmp_path / "wrong-source.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n" + b"x" * 2048)
    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=None)
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.mode = "zotero"
    generator.config = ConfigDict({"Paths": {"library_path": str(tmp_path)}})
    generator.library_path = str(tmp_path)
    provider_calls = 0

    monkeypatch.setattr(generator, "_check_cancelled", lambda: None)
    monkeypatch.setattr(generator, "_emit_progress", lambda **_kwargs: None)
    monkeypatch.setattr(
        generator,
        "_paper_progress_label",
        lambda paper: str(paper.get("title") or "unknown"),
    )
    monkeypatch.setattr(
        main,
        "resolve_pdf_match",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="matched",
            selected_path=str(pdf_path),
            to_dict=lambda: {"status": "matched", "selected_path": str(pdf_path)},
        ),
    )
    monkeypatch.setattr(
        generator,
        "_prepare_stage1_input",
        lambda *_args, **_kwargs: (
            "Wrong Source\nDOI 10.9999/wrong.2024\n" + "x" * 600,
            {
                "analysis_input_kind": "text",
                "extractor_used": "test",
                "preprocess_profile": "test",
            },
        ),
    )

    def unexpected_provider(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("Stage 1 provider must not run for a DOI mismatch")

    monkeypatch.setattr(generator, "_call_stage1_reader_with_scheduler", unexpected_provider)

    result = generator.process_paper(
        {
            "title": "Expected Source",
            "authors": ["Alice Smith"],
            "year": "2024",
            "doi": "10.1234/right.2024",
            "attachments": ["wrong-source.pdf"],
        },
        paper_index=0,
        file_index=cast(main.FileIndex, object()),
        total_papers=1,
    )

    assert result is not None
    assert result["status"] == "failed"
    assert result["failure_reason"] == "source_identity_mismatch"
    assert result["identity_verdict"] == "mismatch"
    assert result["artifact_status"] == "quarantined"
    assert provider_calls == 0


def test_process_all_papers_prefers_runtime_library_path(tmp_path: Path, monkeypatch) -> None:
    runtime_library = tmp_path / "runtime-library"
    runtime_library.mkdir()
    configured_library = tmp_path / "configured-library"
    configured_library.mkdir()

    generator = main.LiteratureReviewGenerator(
        project_name="demo",
        pdf_folder=None,
        library_path=str(runtime_library),
    )
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.mode = "zotero"
    generator.config = ConfigDict(
        {
            "Paths": {"library_path": str(configured_library)},
            "Performance": {"max_workers": "1"},
        }
    )
    generator.papers = [{"title": "Already processed", "authors": []}]
    paper_key = generator.get_paper_key(generator.papers[0])
    generator._checkpoint_processed_papers = {paper_key}
    generator._checkpoint_failed_papers = set()

    seen: dict[str, str] = {}

    class _Index:
        def __len__(self) -> int:
            return 1

    def _create_file_index(path: str) -> _Index:
        seen["library_path"] = path
        return _Index()

    monkeypatch.setattr(main, "create_file_index", _create_file_index)
    monkeypatch.setattr(generator, "_emit_stage1_progress", lambda **_kwargs: None)
    monkeypatch.setattr(generator, "save_summaries", lambda: True)
    monkeypatch.setattr(generator, "save_checkpoint", lambda: True)

    assert generator.process_all_papers() is True
    assert seen["library_path"] == str(runtime_library)

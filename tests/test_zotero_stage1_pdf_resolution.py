from pathlib import Path
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
            "x" * 600,
            {
                "analysis_input_kind": "text",
                "extractor_used": "test",
                "preprocess_profile": strategy,
            },
        )

    monkeypatch.setattr(main, "create_file_index", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(main, "find_pdf", lambda *_args, **_kwargs: str(pdf_path))
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

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


class _DummyTqdm:
    def __init__(self, iterable=None, total=None, desc=None):
        self.iterable = iterable
        self.total = total
        self.desc = desc

    def update(self, _value=1):
        return None

    def set_postfix_str(self, _value: str):
        return None

    def __iter__(self):
        return iter(self.iterable) if self.iterable is not None else iter(())


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


def test_process_paper_retries_checkpoint_failed_entry(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "retry-paper.pdf"
    pdf_path.write_text("dummy pdf body", encoding="utf-8")

    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.mode = "direct"
    generator.config = ConfigDict(
        {
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

    paper: PaperInfo = {
        "title": "Retry Paper",
        "authors": ["Alice Smith"],
        "pdf_path": str(pdf_path),
    }
    paper_key = main.LiteratureReviewGenerator.get_paper_key(paper)
    generator._checkpoint_failed_papers.add(paper_key)

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

    monkeypatch.setattr(generator, "_check_cancelled", lambda: None)
    monkeypatch.setattr(generator, "_emit_progress", lambda **_kwargs: None)
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

    result = generator.process_paper(paper, paper_index=0, file_index=None, total_papers=1)

    assert result is not None
    assert result["status"] == "success"
    assert seen["pdf_path"] == str(pdf_path)
    assert paper_key not in generator._checkpoint_failed_papers


def test_process_all_papers_does_not_skip_checkpoint_failed_entries(tmp_path: Path, monkeypatch) -> None:
    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.config = ConfigDict(
        {
            "Performance": {"max_workers": "2"},
            "Retry_Settings": {"max_retry_rounds": "0", "base_retry_delay": "0", "max_retry_delay": "0"},
        }
    )
    generator.papers = [
        {"title": "Retry Me", "pdf_path": str(tmp_path / "retry-me.pdf")},
    ]
    generator.summary_file = str(tmp_path / "demo_summaries.json")

    paper_key = main.LiteratureReviewGenerator.get_paper_key(generator.papers[0])
    generator._checkpoint_failed_papers.add(paper_key)

    attempts: list[str] = []

    monkeypatch.setattr(main, "tqdm", _DummyTqdm)
    monkeypatch.setattr(generator, "save_summaries", lambda: True)
    monkeypatch.setattr(generator, "save_checkpoint", lambda: True)
    monkeypatch.setattr(generator, "_check_cancelled", lambda: None)
    monkeypatch.setattr(generator, "_emit_stage1_progress", lambda **_kwargs: None)

    def _fake_process_paper(paper, paper_index, file_index, total_papers):
        attempts.append(paper["title"])
        return {
            "paper_info": paper,
            "status": "success",
        }

    monkeypatch.setattr(generator, "process_paper", _fake_process_paper)

    assert generator.process_all_papers() is True
    assert attempts == ["Retry Me"]

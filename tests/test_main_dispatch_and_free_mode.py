import argparse
import json
from types import SimpleNamespace
from typing import cast

from config_loader import ConfigDict
from context_manager import validate_summary_quality
import main
from models import PaperInfo


def _make_args(**overrides):
    base = {
        "config": "config.ini",
        "project_name": "demo",
        "pdf_folder": None,
        "run_all": False,
        "analyze_only": False,
        "generate_outline": False,
        "generate_review": False,
        "generate_section": None,
        "validate_review": False,
        "setup": False,
        "prime_with_folder": None,
        "concept": None,
        "retry_failed": False,
        "retry_review_failed": False,
        "merge": None,
        "free_mode_profile": None,
        "free_mode_idea": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class _DummyLogger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass

    def success(self, *_args, **_kwargs):
        pass


class _DummyGenerator:
    def __init__(self, config, project_name, pdf_folder):
        self.config = {}
        self.config_file = config
        self.project_name = project_name
        self.pdf_folder = pdf_folder
        self.output_dir = "out"
        self.logger = _DummyLogger()
        self.concept_mode = False
        self.concept_profile = None
        self.free_mode_profile_path = None
        self.free_mode_idea = None

    def load_configuration(self):
        return True

    def setup_output_directory(self):
        return True


def _quality_ready_ai_summary(
    *,
    paper_metadata: dict | None = None,
    limitations: str = "This study is limited by a single-country sample and short observation window.",
):
    return {
        "routing": {
            "paper_type": "empirical",
            "classification_status": "resolved",
            "route_confidence": "high",
            "secondary_candidates": [],
        },
        "core_analysis": {
            "summary": "This paper studies how AI recommendations shape consumer decisions across multiple scenarios with enough detail to satisfy quality checks.",
            "key_points": [
                "AI recommendations increase perceived efficiency when consumers have clear goals and sufficient product information."
            ],
            "methodology": "The study combines experiments and archival evidence to compare recommendation sources across contexts.",
            "findings": "The authors find that recommendation source effects depend on context, product framing, and user goals across several analyses.",
            "conclusions": "The paper concludes that firms should align recommendation source design with user expectations and decision contexts.",
            "relevance": "The study links recommender design to marketing and tourism decision support.",
            "limitations": limitations,
            "theoretical_framework": None,
            "research_gap": None,
            "future_research_directions": [],
        },
        "paper_metadata": paper_metadata
        or {
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


def test_build_stage1_prompt_injects_free_mode_profile(tmp_path) -> None:
    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=str(tmp_path))
    generator.project_name = "demo"
    generator.output_dir = str(tmp_path)

    profile_path = tmp_path / "demo_free_mode_profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "research_goal": "Compare A and B",
                "concept_relationship": "A influences B",
                "focus_points": ["mechanism"],
                "exclusions": [],
                "theory_or_variable_focus": [],
                "outline_preferences": [],
                "writing_constraints": [],
                "generated_prompt": "",
                "conversation_notes": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    generator.free_mode_profile_path = str(profile_path)
    generator._load_stage1_prompt_template = lambda: "HEAD{{FREE_MODE_CONTEXT}}BODY{{PAPER_FULL_TEXT}}"  # type: ignore[method-assign]

    prompt = generator._build_stage1_analysis_prompt("paper body")

    assert "Compare A and B" in prompt
    assert "paper body" in prompt
    assert "{{FREE_MODE_CONTEXT}}" not in prompt


def test_apply_ai_metadata_backfill_updates_direct_mode_placeholders(tmp_path) -> None:
    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=str(tmp_path))
    paper: PaperInfo = {
        "title": "raw_filename",
        "authors": [],
        "year": "未知年份",
        "journal": "未知期刊",
        "doi": "",
    }

    updated = generator._apply_ai_metadata_backfill(
        paper,
        {
            "paper_metadata": {
                "title": "Recovered Title",
                "authors": ["Alice Smith"],
                "year": "2024",
                "journal": "Journal of Tests",
                "doi": "10.1000/test",
            }
        },
    )

    assert updated == ["标题", "作者", "年份", "期刊", "DOI"]
    assert paper["title"] == "Recovered Title"
    assert paper["authors"] == ["Alice Smith"]
    assert paper["year"] == "2024"
    assert paper["journal"] == "Journal of Tests"
    assert paper["doi"] == "10.1000/test"


def test_validate_summary_quality_uses_ai_metadata_when_paper_info_has_placeholders() -> None:
    result = validate_summary_quality(
        {
            "paper_info": {
                "title": "raw_filename",
                "authors": [],
                "year": "未知年份",
                "journal": "未知期刊",
                "doi": "",
            },
            "status": "success",
            "ai_summary": _quality_ready_ai_summary(
                paper_metadata={
                    "title": "Recovered Title",
                    "authors": ["Alice Smith"],
                    "year": "2024",
                    "journal": "Journal of Tests",
                    "doi": "10.1000/test",
                }
            ),
        }
    )

    assert result[0] is True


def test_validate_summary_quality_relaxes_missing_metadata_for_direct_mode() -> None:
    result = validate_summary_quality(
        {
            "paper_info": {
                "title": "raw_filename",
                "authors": [],
                "year": "未知年份",
                "journal": "未知期刊",
                "doi": "",
            },
            "status": "success",
            "source_mode": "direct",
            "ai_summary": _quality_ready_ai_summary(),
        }
    )

    assert result[0] is True


def test_process_paper_backfills_metadata_before_quality_check(tmp_path, monkeypatch) -> None:
    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.config = ConfigDict({
        "Primary_Reader_API": {"api_key": "primary", "model": "m1", "api_base": "https://example.com/v1"},
        "Backup_Reader_API": {"api_key": "", "model": "m2", "api_base": "https://example.com/v1"},
    })

    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_text("dummy pdf placeholder", encoding="utf-8")

    monkeypatch.setattr(generator, "_prepare_stage1_input", lambda _path: ("x" * 1200, {"analysis_input_kind": "text", "extractor_used": "mock"}))
    monkeypatch.setattr(generator, "_load_stage1_prompt_template", lambda: "{{PAPER_FULL_TEXT}}")
    monkeypatch.setattr(generator, "_inject_free_mode_context", lambda prompt: prompt)
    monkeypatch.setattr(
        main,
        "get_summary_from_ai_with_fallback",
        lambda *args, **kwargs: _quality_ready_ai_summary(
            paper_metadata={
                "title": "Recovered Title",
                "authors": ["Alice Smith"],
                "year": "2024",
                "journal": "Journal of Tests",
                "doi": "10.1000/test",
            }
        ),
    )

    validation_snapshots = []

    def _fake_validate(summary_data):
        validation_snapshots.append(
            {
                "title": summary_data["paper_info"]["title"],
                "authors": list(summary_data["paper_info"]["authors"]),
                "year": summary_data["paper_info"]["year"],
                "journal": summary_data["paper_info"]["journal"],
                "source_mode": summary_data["source_mode"],
            }
        )
        return True, "ok"

    monkeypatch.setattr(main, "validate_summary_quality", _fake_validate)

    paper: PaperInfo = {
        "title": "raw_filename",
        "authors": [],
        "year": "未知年份",
        "journal": "未知期刊",
        "doi": "",
        "pdf_path": str(pdf_path),
    }

    result = generator.process_paper(paper, 0, None, 1)

    assert result is not None
    assert result["status"] == "success"
    assert validation_snapshots == [
        {
            "title": "Recovered Title",
            "authors": ["Alice Smith"],
            "year": "2024",
            "journal": "Journal of Tests",
            "source_mode": "direct",
        }
    ]


def test_dispatch_command_routes_generate_section(monkeypatch) -> None:
    called = {}

    monkeypatch.setattr(main, "detect_runtime_environment", lambda: SimpleNamespace(display_name="test", needs_isolation_recommendation=False))
    monkeypatch.setattr(main, "LiteratureReviewGenerator", _DummyGenerator)
    monkeypatch.setattr(main, "handle_generate_section_mode", lambda generator, args: called.setdefault("section", args.generate_section))

    main.dispatch_command(_make_args(generate_section=3))

    assert called["section"] == 3


def test_dispatch_command_routes_retry_review_failed(monkeypatch) -> None:
    called = {"retry": False}

    monkeypatch.setattr(main, "detect_runtime_environment", lambda: SimpleNamespace(display_name="test", needs_isolation_recommendation=False))
    monkeypatch.setattr(main, "LiteratureReviewGenerator", _DummyGenerator)
    monkeypatch.setattr(main, "handle_retry_review_failed_mode", lambda generator: called.__setitem__("retry", True))

    main.dispatch_command(_make_args(retry_review_failed=True))

    assert called["retry"] is True

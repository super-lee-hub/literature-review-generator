import argparse
import json
from types import SimpleNamespace

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

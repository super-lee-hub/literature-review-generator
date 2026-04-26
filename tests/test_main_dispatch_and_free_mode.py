import argparse
import inspect
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import cast

from config_loader import ConfigDict
from context_manager import validate_summary_quality
import main
from models import APIConfig, PaperInfo, ProcessingResult, SummariesList


REMOVED_QUEUE_FLAGS = [
    "--queue-add",
    "--queue-run",
    "--queue-file",
    "--queue-files",
    "--queue-list",
    "--queue-cancel",
    "--queue-retry",
    "--queue-clear",
]


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
        "summary_file": None,
        "reuse_stage1": False,
        "reuse_summary_files": None,
        "free_mode_profile": None,
        "free_mode_idea": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_cli_help_excludes_removed_queue_flags() -> None:
    result = subprocess.run(
        [sys.executable, str(Path(main.__file__).resolve()), "--help"],
        cwd=Path(main.__file__).resolve().parent,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert result.returncode == 0
    for flag in REMOVED_QUEUE_FLAGS:
        assert flag not in result.stdout


def test_removed_queue_flags_are_unknown_arguments() -> None:
    result = subprocess.run(
        [sys.executable, str(Path(main.__file__).resolve()), "--queue-list"],
        cwd=Path(main.__file__).resolve().parent,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert result.returncode != 0
    assert "unrecognized arguments" in result.stdout
    assert "--queue-list" in result.stdout


def test_dispatch_command_has_no_public_queue_route() -> None:
    dispatch_source = inspect.getsource(main.dispatch_command)

    for attr in [
        "handle_queue_list_mode",
        "handle_queue_cancel_mode",
        "handle_queue_retry_mode",
        "handle_queue_clear_mode",
        "handle_queue_add_mode",
        "handle_queue_run_mode",
    ]:
        assert not hasattr(main, attr)
        assert attr not in dispatch_source
    for token in ["queue_list", "queue_cancel", "queue_retry", "queue_clear", "queue_add", "queue_run"]:
        assert token not in dispatch_source


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
        self.progress_tracker = None

    def load_configuration(self):
        return True

    def setup_output_directory(self):
        return True


class _RecordingTracker:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, **kwargs):
        self.events.append(dict(kwargs))


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


def _legacy_mojibake_unknown_year() -> str:
    return "".join(chr(codepoint) for codepoint in (0x93C8, 0xE046, 0x7161, 0x9A9E, 0x7FE0, 0x5524))


def _legacy_mojibake_unknown_journal() -> str:
    return "".join(chr(codepoint) for codepoint in (0x93C8, 0xE046, 0x7161, 0x93C8, 0x71B7, 0x5794))


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

    assert len(updated) == 5
    assert updated[-1] == "DOI"
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


def test_placeholder_metadata_detection_handles_legacy_mojibake() -> None:
    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=".")

    assert generator._is_placeholder_metadata_value(_legacy_mojibake_unknown_year()) is True
    assert generator._is_placeholder_metadata_value(_legacy_mojibake_unknown_journal()) is True


def test_process_paper_backfills_metadata_before_quality_check(tmp_path, monkeypatch) -> None:
    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.config = ConfigDict({
        "Primary_Reader_API": {"api_key": "primary", "model": "m1", "api_base": "https://example.com/v1"},
        "Backup_Reader_API": {"api_key": "", "model": "m2", "api_base": "https://example.com/v1"},
    })

    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_text("dummy pdf placeholder", encoding="utf-8")

    monkeypatch.setattr(generator, "_prepare_stage1_input", lambda *_args, **_kwargs: ("x" * 1200, {"analysis_input_kind": "text", "extractor_used": "mock"}))
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


def test_process_paper_backfills_metadata_from_stage1_text_when_ai_metadata_is_empty(tmp_path, monkeypatch) -> None:
    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.config = ConfigDict({
        "Primary_Reader_API": {"api_key": "primary", "model": "m1", "api_base": "https://example.com/v1"},
        "Backup_Reader_API": {"api_key": "", "model": "m2", "api_base": "https://example.com/v1"},
    })

    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_text("dummy pdf placeholder", encoding="utf-8")

    stage1_text = """
    Cogent Business & Management
    ISSN: 2331-1975 (Online) Journal homepage: www.tandfonline.com/journals/oabm20
    Conversational AI in hospitality and tourism: a bibliometric-systematic review
    Muhammad Fikry Aransyah, Bambang Hermanto, Anang Muftiadi & Hera Oktadiana
    To cite this article: Muhammad Fikry Aransyah, Bambang Hermanto, Anang Muftiadi & Hera Oktadiana (2026) Conversational AI in hospitality and tourism: a bibliometric-systematic review, Cogent Business & Management, 13:1, 2613599, DOI: 10.1080/23311975.2026.2613599
    ABSTRACT
    """ + ("x" * 1200)

    monkeypatch.setattr(generator, "_prepare_stage1_input", lambda *_args, **_kwargs: (stage1_text, {"analysis_input_kind": "text", "extractor_used": "mock"}))
    monkeypatch.setattr(generator, "_load_stage1_prompt_template", lambda: "{{PAPER_FULL_TEXT}}")
    monkeypatch.setattr(generator, "_inject_free_mode_context", lambda prompt: prompt)
    monkeypatch.setattr(main, "get_summary_from_ai_with_fallback", lambda *args, **kwargs: _quality_ready_ai_summary())
    monkeypatch.setattr(main, "validate_summary_quality", lambda _summary_data: (True, "ok"))

    paper: PaperInfo = {
        "title": "Conversational AI in hospitality and tourism  a bibliometric systematic review",
        "authors": [],
        "year": _legacy_mojibake_unknown_year(),
        "journal": _legacy_mojibake_unknown_journal(),
        "doi": "",
        "pdf_path": str(pdf_path),
    }

    result = generator.process_paper(paper, 0, None, 1)

    assert result is not None
    assert result["status"] == "success"
    assert paper["authors"] == [
        "Muhammad Fikry Aransyah",
        "Bambang Hermanto",
        "Anang Muftiadi",
        "Hera Oktadiana",
    ]
    assert paper["year"] == "2026"
    assert paper["journal"] == "Cogent Business & Management"
    assert paper["doi"] == "10.1080/23311975.2026.2613599"


def test_process_paper_backfills_inverted_citation_authors_from_stage1_text(tmp_path, monkeypatch) -> None:
    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.config = ConfigDict({
        "Primary_Reader_API": {"api_key": "primary", "model": "m1", "api_base": "https://example.com/v1"},
        "Backup_Reader_API": {"api_key": "", "model": "m2", "api_base": "https://example.com/v1"},
    })

    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_text("dummy pdf placeholder", encoding="utf-8")

    stage1_text = """
    Journal of AI Studies
    Interesting Paper Title
    Smith, John & Doe, Jane
    To cite this article: Smith, John & Doe, Jane (2024) Interesting Paper Title, Journal of AI Studies, 1:1, 1-10, DOI: 10.1234/example
    ABSTRACT
    """ + ("x" * 1200)

    monkeypatch.setattr(generator, "_prepare_stage1_input", lambda *_args, **_kwargs: (stage1_text, {"analysis_input_kind": "text", "extractor_used": "mock"}))
    monkeypatch.setattr(generator, "_load_stage1_prompt_template", lambda: "{{PAPER_FULL_TEXT}}")
    monkeypatch.setattr(generator, "_inject_free_mode_context", lambda prompt: prompt)
    monkeypatch.setattr(main, "get_summary_from_ai_with_fallback", lambda *args, **kwargs: _quality_ready_ai_summary())
    monkeypatch.setattr(main, "validate_summary_quality", lambda _summary_data: (True, "ok"))

    paper: PaperInfo = {
        "title": "Interesting Paper Title",
        "authors": [],
        "year": _legacy_mojibake_unknown_year(),
        "journal": _legacy_mojibake_unknown_journal(),
        "doi": "",
        "pdf_path": str(pdf_path),
    }

    result = generator.process_paper(paper, 0, None, 1)

    assert result is not None
    assert result["status"] == "success"
    assert paper["authors"] == ["John Smith", "Jane Doe"]
    assert paper["year"] == "2024"
    assert paper["journal"] == "Journal of AI Studies"
    assert paper["doi"] == "10.1234/example"


def test_process_paper_returns_failed_when_all_stage1_strategies_fail(tmp_path, monkeypatch) -> None:
    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.config = ConfigDict({
        "Primary_Reader_API": {"api_key": "primary", "model": "m1", "api_base": "https://example.com/v1"},
        "Backup_Reader_API": {"api_key": "backup", "model": "m2", "api_base": "https://example.com/v1"},
    })

    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_text("dummy pdf placeholder", encoding="utf-8")
    monkeypatch.setenv("MINERU_API_TOKEN", "token")

    def _prepare_stage1_input(_path, preprocess_strategy="hybrid"):
        return (
            "A" * 1600,
            {
                "analysis_input_kind": "text",
                "extractor_used": f"mock-{preprocess_strategy}",
                "preprocess_profile": f"profile-{preprocess_strategy}",
            },
        )

    monkeypatch.setattr(generator, "_prepare_stage1_input", _prepare_stage1_input)
    monkeypatch.setattr(generator, "_build_stage1_visual_bundle", lambda **_kwargs: None)
    monkeypatch.setattr(
        generator,
        "_build_stage1_model_input",
        lambda **_kwargs: {
            "input_mode": "text_only",
            "prompt_text": "prompt",
            "user_message_content": None,
            "selected_visual_refs": [],
            "visual_manifest_path": "",
            "visual_bundle_path": "",
            "visual_selection_policy_snapshot": {},
            "multimodal_capability": {},
            "fallback_reason": "",
        },
    )
    monkeypatch.setattr(generator, "_apply_stage1_text_metadata_backfill", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(generator, "_apply_ai_metadata_backfill", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(main, "get_summary_from_ai_with_fallback", lambda *args, **kwargs: {"bad": "primary"})
    monkeypatch.setattr(main, "get_summary_from_ai", lambda *args, **kwargs: {"bad": "backup"})
    monkeypatch.setattr(main, "validate_summary_quality", lambda _summary_data: (False, "quality failed"))
    monkeypatch.setattr(
        generator,
        "_persist_paper_artifact",
        lambda _result: (_ for _ in ()).throw(AssertionError("paper artifact should not persist for failed stage one")),
    )

    paper: PaperInfo = {
        "title": "Demo Paper",
        "authors": ["Alice Example"],
        "year": "2024",
        "journal": "Journal of Tests",
        "doi": "",
        "pdf_path": str(pdf_path),
    }

    result = generator.process_paper(paper, 0, None, 1)

    assert result is not None
    assert result["status"] == "failed"
    assert "failure_reason" in result
    assert result["failure_reason"] is not None
    assert "所有预处理策略都失败" in result["failure_reason"]
    assert "Profile: profile-hybrid" in result["failure_reason"]
    assert "模型: backup" in result["failure_reason"]
    assert "attempt_history" in result
    assert [attempt["preprocess_strategy"] for attempt in result["attempt_history"]] == ["hybrid", "docling", "mineru", "legacy"]


def test_generate_review_outline_prefers_outline_api_config(tmp_path, monkeypatch) -> None:
    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.config = ConfigDict(
        {
            "Writer_API": {
                "api_key": "writer-key",
                "model": "writer-model",
                "api_base": "https://writer.example.com/v1",
            },
            "Outline_API": {
                "api_key": "outline-key",
                "model": "outline-model",
                "api_base": "https://outline.example.com/v1",
            },
        }
    )
    generator.summaries = cast(
        SummariesList,
        [
            cast(
                ProcessingResult,
                {
                    "paper_info": {"title": "Paper A", "authors": ["Alice"]},
                    "status": "success",
                    "ai_summary": cast(main.AISummary, _quality_ready_ai_summary()),
                },
            )
        ],
    )

    monkeypatch.setattr(main, "estimate_tokens", lambda _text: 42)
    monkeypatch.setattr(main, "optimize_context_for_outline", lambda *_args, **_kwargs: "[]")
    monkeypatch.setattr(generator, "_inject_free_mode_context", lambda prompt: prompt)

    captured_api_config: APIConfig | None = None

    def _fake_call_ai_api(*, api_config, **_kwargs):
        nonlocal captured_api_config
        captured_api_config = cast(APIConfig, api_config)
        return "# Demo Outline\n\n## 1. Section\n" + ("content\n" * 30)

    monkeypatch.setattr(main, "_call_ai_api", _fake_call_ai_api)

    outline = generator.generate_review_outline({})

    assert outline is not None
    assert captured_api_config is not None
    assert captured_api_config["model"] == "outline-model"
    assert captured_api_config["api_key"] == "outline-key"
    assert captured_api_config["api_base"] == "https://outline.example.com/v1"


def test_dispatch_command_routes_generate_section(monkeypatch) -> None:
    called = {}

    monkeypatch.setattr(main, "detect_runtime_environment", lambda: SimpleNamespace(display_name="test", needs_isolation_recommendation=False))
    monkeypatch.setattr(
        "services.workflow_facade.build_job_request",
        lambda args: SimpleNamespace(action="generate_section", generate_section=args.generate_section, progress_tracker=getattr(args, "_progress_tracker", None)),
    )

    class _Runner:
        def run(self, request, cancel_token=None):
            called["section"] = request.generate_section
            called["cancel_token"] = cancel_token
            return SimpleNamespace(success=True, exit_code=0)

    monkeypatch.setattr("services.job_runner.JobRunner", _Runner)

    main.dispatch_command(_make_args(generate_section=3))

    assert called["section"] == 3


def test_dispatch_command_routes_retry_review_failed(monkeypatch) -> None:
    called: dict[str, object] = {"retry": False, "cancel_token": None}

    monkeypatch.setattr(main, "detect_runtime_environment", lambda: SimpleNamespace(display_name="test", needs_isolation_recommendation=False))
    monkeypatch.setattr(
        "services.workflow_facade.build_job_request",
        lambda args: SimpleNamespace(action="retry_review_failed", retry_review_failed=args.retry_review_failed, progress_tracker=getattr(args, "_progress_tracker", None)),
    )

    class _Runner:
        def run(self, request, cancel_token=None):
            called["retry"] = bool(request.retry_review_failed)
            called["cancel_token"] = cancel_token
            return SimpleNamespace(success=True, exit_code=0)

    monkeypatch.setattr("services.job_runner.JobRunner", _Runner)

    main.dispatch_command(_make_args(retry_review_failed=True))

    assert called["retry"] is True


def test_dispatch_command_injects_progress_tracker(monkeypatch) -> None:
    captured = {}
    tracker = object()

    monkeypatch.setattr(main, "detect_runtime_environment", lambda: SimpleNamespace(display_name="test", needs_isolation_recommendation=False))
    monkeypatch.setattr(
        "services.workflow_facade.build_job_request",
        lambda args: SimpleNamespace(action="analyze", progress_tracker=getattr(args, "_progress_tracker", None)),
    )

    class _Runner:
        def run(self, request, cancel_token=None):
            captured["handled"] = True
            captured["tracker"] = request.progress_tracker
            captured["cancel_token"] = cancel_token
            return SimpleNamespace(success=True, exit_code=0)

    monkeypatch.setattr("services.job_runner.JobRunner", _Runner)

    main.dispatch_command(_make_args(_progress_tracker=tracker))

    assert captured["handled"] is True
    assert captured["tracker"] is tracker


def test_dispatch_command_rejects_summary_file_for_stage1_actions(monkeypatch) -> None:
    monkeypatch.setattr(main, "detect_runtime_environment", lambda: SimpleNamespace(display_name="test", needs_isolation_recommendation=False))
    monkeypatch.setattr(
        "services.workflow_facade.build_job_request",
        lambda _args: SimpleNamespace(
            action="analyze",
            summary_file="D:/subset.json",
            reuse_stage1=False,
            reuse_summary_files=(),
        ),
    )

    try:
        main.dispatch_command(_make_args(analyze_only=True, summary_file="D:/subset.json"))
    except SystemExit as exc:
        assert int(exc.code or 0) == 1
    else:  # pragma: no cover
        raise AssertionError("dispatch_command should reject --summary-file for stage1 actions")


def test_generator_validation_helpers_read_via_compat_layer(tmp_path) -> None:
    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=str(tmp_path))
    generator.config = ConfigDict(
        {
            "Validation": {
                "stage1_enabled": "true",
                "stage2_enabled": "false",
            },
            "Performance": {
                "enable_stage1_validation": "false",
                "enable_stage2_validation": "true",
            },
        }
    )

    assert generator._stage1_validation_enabled() is True
    assert generator._stage2_validation_enabled() is False


def test_process_all_papers_emits_stage1_progress_and_retry_updates(tmp_path, monkeypatch) -> None:
    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.config = ConfigDict(
        {
            "Performance": {"max_workers": "2"},
            "Retry_Settings": {"max_retry_rounds": "1", "base_retry_delay": "0", "max_retry_delay": "0"},
        }
    )
    generator.papers = [
        {"title": "Paper A", "pdf_path": str(tmp_path / "paper-a.pdf")},
        {"title": "Paper B", "pdf_path": str(tmp_path / "paper-b.pdf")},
    ]
    generator.summary_file = str(tmp_path / "demo_summaries.json")

    tracker = _RecordingTracker()
    generator.progress_tracker = tracker

    monkeypatch.setattr(main, "tqdm", _DummyTqdm)
    monkeypatch.setattr(generator, "save_summaries", lambda: True)
    monkeypatch.setattr(generator, "save_checkpoint", lambda: True)

    attempts: dict[str, int] = {}

    def _fake_process_paper(paper, paper_index, file_index, total_papers):
        title = paper["title"]
        attempts[title] = attempts.get(title, 0) + 1
        generator._emit_progress(stage="analyze", item_label=title, message=f"正在调用AI生成摘要: {title}")
        if title == "Paper B" and attempts[title] == 1:
            return {
                "paper_info": paper,
                "status": "failed",
                "failure_reason": "API timeout while generating summary",
            }
        return {
            "paper_info": paper,
            "status": "success",
        }

    monkeypatch.setattr(generator, "process_paper", _fake_process_paper)

    assert generator.process_all_papers() is True
    assert attempts == {"Paper A": 1, "Paper B": 2}

    events = tracker.events
    assert events[0]["stage"] == "analyze"
    assert events[0]["total"] == 2
    assert events[0]["current"] == 0
    assert events[0]["success_count"] == 0
    assert events[0]["failure_count"] == 0
    assert events[0]["remaining_count"] == 2
    assert events[0]["indeterminate"] is False
    assert "开始并发处理" in events[0]["message"]

    assert any(event.get("item_label") == "Paper A" and "正在调用AI生成摘要" in event.get("message", "") for event in events)
    assert any("处理失败" in event.get("message", "") and event.get("failure_count") == 1 for event in events)
    assert any(
        event.get("retry_round") == 1
        and event.get("retry_total_rounds") == 1
        and "开始第 1 轮自动重试" in event.get("message", "")
        for event in events
    )
    assert any(
        "重试成功" in event.get("message", "")
        and event.get("success_count") == 2
        and event.get("failure_count") == 0
        and event.get("remaining_count") == 0
        for event in events
    )

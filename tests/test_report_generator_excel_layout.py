import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pandas as pd

from report_generator import generate_excel_report, generate_failure_report


class _DummyLogger:
    def info(self, _message: str) -> None:
        pass

    def success(self, _message: str) -> None:
        pass

    def warning(self, _message: str) -> None:
        pass

    def error(self, _message: str) -> None:
        pass


def _build_summary(title: str, routing: dict, specialized_details: dict) -> dict:
    return {
        "paper_info": {
            "title": title,
            "authors": ["Alice", "Bob"],
            "year": "2024",
            "journal": "Journal of Testing",
            "doi": "10.1000/test",
        },
        "status": "success",
        "processing_time": 12.5,
        "text_length": 12345,
        "preprocess": {
            "extractor_used": "mineru",
        },
        "ai_summary": {
            "schema_version": "summary_v2_lite",
            "routing": routing,
            "core_analysis": {
                "summary": f"{title} summary",
                "key_points": ["point 1", "point 2"],
                "methodology": f"{title} methodology",
                "findings": f"{title} findings",
                "conclusions": f"{title} conclusions",
                "relevance": f"{title} relevance",
                "limitations": f"{title} limitations",
                "theoretical_framework": f"{title} framework",
                "research_gap": f"{title} gap",
                "future_research_directions": [f"{title} future"],
            },
            "specialized_details": specialized_details,
            "quality_audit": {
                "extraction_confidence": "medium",
                "completeness_score": 0.8,
                "needs_manual_review": routing["classification_status"] != "resolved",
                "missing_critical_fields": [],
                "conflict_flags": [],
                "inferred_fields": [],
            },
        },
    }


def test_generate_excel_report_uses_canonical_multisheet_layout(tmp_path: Path) -> None:
    summaries = [
        _build_summary(
            "Empirical Paper",
            {
                "paper_type": "empirical",
                "paper_subtype_raw": "survey study",
                "paper_subtype_normalized": "survey",
                "classification_status": "resolved",
                "route_confidence": "high",
                "classification_rationale": None,
                "secondary_candidates": [],
            },
            {
                "empirical": {
                    "research_questions_or_hypotheses": ["H1"],
                    "data_source_and_size": "survey, n=300",
                    "analysis_technique": "SEM",
                    "core_variables": {
                        "independent": ["trust"],
                        "dependent": ["adoption"],
                        "mediators": [],
                        "moderators": [],
                        "controls": [],
                        "other_core_constructs": [],
                    },
                    "sample_characteristics_or_context": "hotel customers",
                },
                "review": None,
                "conceptual": None,
            },
        ),
        _build_summary(
            "Review Paper",
            {
                "paper_type": "review",
                "paper_subtype_raw": "systematic review",
                "paper_subtype_normalized": "systematic_review",
                "classification_status": "hybrid",
                "route_confidence": "medium",
                "classification_rationale": "primary review framing but empirical elements also appear",
                "secondary_candidates": ["empirical"],
            },
            {
                "empirical": None,
                "review": {
                    "review_type": "systematic review",
                    "search_databases": ["Scopus", "Web of Science"],
                    "time_span": "2018-2024",
                    "included_studies_count": "75",
                    "inclusion_exclusion_criteria": "English journal articles",
                    "synthesis_approach": "thematic synthesis",
                    "main_themes": ["theme 1", "theme 2"],
                },
                "conceptual": None,
            },
        ),
        _build_summary(
            "Conceptual Paper",
            {
                "paper_type": "conceptual",
                "paper_subtype_raw": "framework development",
                "paper_subtype_normalized": "framework_development",
                "classification_status": "resolved",
                "route_confidence": "high",
                "classification_rationale": None,
                "secondary_candidates": [],
            },
            {
                "empirical": None,
                "review": None,
                "conceptual": {
                    "core_propositions": ["P1", "P2"],
                    "conceptual_relationships": "A influences B",
                    "theoretical_contributions": "extends prior theory",
                },
            },
        ),
        _build_summary(
            "Uncertain Paper",
            {
                "paper_type": None,
                "paper_subtype_raw": None,
                "paper_subtype_normalized": None,
                "classification_status": "uncertain",
                "route_confidence": "low",
                "classification_rationale": "insufficient evidence to assign a stable primary type",
                "secondary_candidates": [],
            },
            {
                "empirical": None,
                "review": None,
                "conceptual": None,
            },
        ),
    ]

    summary_file = tmp_path / "demo_summaries.json"
    summary_file.write_text(json.dumps(summaries, ensure_ascii=False), encoding="utf-8")

    generator = SimpleNamespace(
        summary_file=str(summary_file),
        output_dir=str(tmp_path),
        project_name="demo",
        logger=_DummyLogger(),
    )

    assert generate_excel_report(generator) is True

    workbook = pd.ExcelFile(tmp_path / "demo_analyzed_papers.xlsx")
    assert workbook.sheet_names == ["总表", "实证论文", "综述论文", "概念论文", "未确定论文"]

    total_df = cast(pd.DataFrame, workbook.parse("总表"))
    assert "primary_sheet_target" in total_df.columns
    assert "detailed_json" in total_df.columns
    assert total_df.loc[0, "primary_sheet_target"] == "实证论文"
    assert pd.isna(total_df.loc[3, "primary_sheet_target"])

    empirical_df = cast(pd.DataFrame, workbook.parse("实证论文"))
    assert empirical_df.shape[0] == 1
    assert "核心变量" in empirical_df.columns

    review_df = cast(pd.DataFrame, workbook.parse("综述论文"))
    assert review_df.shape[0] == 1
    assert "检索数据库" in review_df.columns

    conceptual_df = cast(pd.DataFrame, workbook.parse("概念论文"))
    assert conceptual_df.shape[0] == 1
    assert "核心命题" in conceptual_df.columns

    uncertain_df = cast(pd.DataFrame, workbook.parse("未确定论文"))
    assert uncertain_df.shape[0] == 2
    assert set(uncertain_df["论文标题"].tolist()) == {"Review Paper", "Uncertain Paper"}


def test_generate_excel_report_strips_illegal_excel_control_characters(tmp_path: Path) -> None:
    summary = _build_summary(
        "Illegal\x0bTitle",
        {
            "paper_type": "empirical",
            "paper_subtype_raw": "survey study",
            "paper_subtype_normalized": "survey",
            "classification_status": "resolved",
            "route_confidence": "high",
            "classification_rationale": None,
            "secondary_candidates": [],
        },
        {
            "empirical": {
                "research_questions_or_hypotheses": ["H1\x0c"],
                "data_source_and_size": "survey,\x0b n=300",
                "analysis_technique": "SEM",
                "core_variables": {
                    "independent": ["trust"],
                    "dependent": ["adoption"],
                    "mediators": [],
                    "moderators": [],
                    "controls": [],
                    "other_core_constructs": [],
                },
                "sample_characteristics_or_context": "hotel customers",
            },
            "review": None,
            "conceptual": None,
        },
    )
    summary["ai_summary"]["core_analysis"]["summary"] = "bad\x0btext\x0cfor excel"

    summary_file = tmp_path / "demo_summaries.json"
    summary_file.write_text(json.dumps([summary], ensure_ascii=False), encoding="utf-8")

    generator = SimpleNamespace(
        summary_file=str(summary_file),
        output_dir=str(tmp_path),
        project_name="demo",
        logger=_DummyLogger(),
    )

    assert generate_excel_report(generator) is True

    workbook = pd.ExcelFile(tmp_path / "demo_analyzed_papers.xlsx")
    total_df = cast(pd.DataFrame, workbook.parse(workbook.sheet_names[0]))

    assert total_df.iloc[0, 0] == "IllegalTitle"
    assert total_df.iloc[0, 5] == "badtextfor excel"


def test_generate_failure_report_includes_attempt_history_details(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    generator = SimpleNamespace(
        output_dir=str(output_dir),
        project_name="demo",
        logger=_DummyLogger(),
        failed_papers=[
            {
                "paper_info": {
                    "title": "Failed Paper",
                    "authors": ["Alice", "Bob"],
                    "year": "2024",
                    "journal": "Journal of Testing",
                    "doi": "10.1000/test",
                },
                "failure_reason": "All stage-one strategies failed",
                "attempt_history": [
                    {
                        "preprocess_strategy": "hybrid",
                        "preprocess_profile": "hybrid",
                        "extractor_used": "pymupdf4llm",
                        "model_used": "backup",
                        "quality_reason": "primary low quality; backup low quality",
                    }
                ],
            }
        ],
    )

    generator._get_report_file_path = lambda suffix: str(output_dir / f"demo{suffix}")

    assert generate_failure_report(generator) is True

    report_path = output_dir / "demo_failed_papers_report.txt"
    content = report_path.read_text(encoding="utf-8")
    assert "尝试明细" in content
    assert "策略=hybrid" in content
    assert "profile=hybrid" in content
    assert "提取器=pymupdf4llm" in content
    assert "模型=backup" in content
    assert "原因=primary low quality; backup low quality" in content

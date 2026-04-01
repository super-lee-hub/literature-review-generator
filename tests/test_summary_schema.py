from summary_schema import normalize_ai_summary, project_legacy_ai_summary


def test_normalize_ai_summary_caps_key_points_and_backfills_rationale() -> None:
    normalized = normalize_ai_summary(
        {
            "routing": {
                "paper_type": None,
                "paper_subtype_raw": None,
                "paper_subtype_normalized": None,
                "classification_status": "uncertain",
                "route_confidence": "low",
                "classification_rationale": None,
                "secondary_candidates": [],
            },
            "core_analysis": {
                "summary": "summary",
                "key_points": [f"point {i}" for i in range(1, 10)],
                "methodology": "method",
                "findings": "findings",
                "conclusions": "conclusions",
                "relevance": "relevance",
                "limitations": "limitations",
                "theoretical_framework": None,
                "research_gap": None,
                "future_research_directions": [],
            },
            "specialized_details": {
                "empirical": None,
                "review": None,
                "conceptual": None,
            },
        }
    )

    assert len(normalized["core_analysis"]["key_points"]) == 7
    assert normalized["routing"]["classification_rationale"] == "insufficient evidence to assign a stable primary type"
    assert "routing.classification_rationale" in normalized["quality_audit"]["inferred_fields"]


def test_normalize_ai_summary_maps_legacy_type_alias_to_subtype() -> None:
    normalized = normalize_ai_summary(
        {
            "common_core": {
                "summary": "summary",
                "key_points": ["point"],
                "methodology": "method",
                "findings": "findings",
                "conclusions": "conclusions",
                "relevance": "relevance",
                "limitations": "limitations",
            },
            "type_specific_details": {
                "paper_type": "systematic review",
                "review_details": {
                    "review_type": "systematic review",
                },
            },
        }
    )

    assert normalized["routing"]["paper_type"] == "review"
    assert normalized["routing"]["paper_subtype_raw"] == "systematic review"
    assert normalized["routing"]["paper_subtype_normalized"] == "systematic_review"


def test_project_legacy_ai_summary_projects_uncertain_type_for_null_primary_type() -> None:
    canonical = normalize_ai_summary(
        {
            "routing": {
                "paper_type": None,
                "paper_subtype_raw": None,
                "paper_subtype_normalized": None,
                "classification_status": "uncertain",
                "route_confidence": "low",
                "classification_rationale": None,
                "secondary_candidates": [],
            },
            "core_analysis": {
                "summary": "summary",
                "key_points": ["point"],
                "methodology": "method",
                "findings": "findings",
                "conclusions": "conclusions",
                "relevance": "relevance",
                "limitations": "limitations",
                "theoretical_framework": None,
                "research_gap": None,
                "future_research_directions": [],
            },
            "specialized_details": {
                "empirical": None,
                "review": None,
                "conceptual": None,
            },
        }
    )

    projected = project_legacy_ai_summary(canonical)
    assert projected["type_specific_details"]["paper_type"] == "uncertain"


def test_normalize_ai_summary_preserves_paper_metadata_and_projects_to_legacy() -> None:
    canonical = normalize_ai_summary(
        {
            "routing": {
                "paper_type": "review",
                "paper_subtype_raw": "systematic review",
                "paper_subtype_normalized": "systematic_review",
                "classification_status": "resolved",
                "route_confidence": "high",
                "classification_rationale": "clear review structure",
                "secondary_candidates": [],
            },
            "core_analysis": {
                "summary": "summary",
                "key_points": ["point"],
                "methodology": "method",
                "findings": "findings",
                "conclusions": "conclusions",
                "relevance": "relevance",
                "limitations": "limitations",
                "theoretical_framework": None,
                "research_gap": None,
                "future_research_directions": [],
            },
            "paper_metadata": {
                "title": "A Better Title",
                "authors": ["Alice Smith", "Bob Lee"],
                "year": "2024",
                "journal": "Journal of Tests",
                "doi": "10.1000/test",
            },
            "specialized_details": {
                "empirical": None,
                "review": {
                    "review_type": "systematic review",
                    "search_databases": [],
                    "time_span": None,
                    "included_studies_count": None,
                    "inclusion_exclusion_criteria": None,
                    "synthesis_approach": "systematic synthesis",
                    "main_themes": [],
                },
                "conceptual": None,
            },
        }
    )

    assert canonical["paper_metadata"]["title"] == "A Better Title"
    assert canonical["paper_metadata"]["authors"] == ["Alice Smith", "Bob Lee"]

    projected = project_legacy_ai_summary(
        canonical,
        paper_info={"title": "Fallback Title", "authors": [], "year": "", "journal": "", "doi": ""},
    )
    assert projected["common_core"]["title"] == "A Better Title"
    assert projected["common_core"]["authors"] == ["Alice Smith", "Bob Lee"]
    assert projected["common_core"]["year"] == "2024"

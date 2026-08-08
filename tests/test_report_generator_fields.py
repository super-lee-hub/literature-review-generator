from report_generator import _canonical_paper_type, _flatten_specialized_details


def _build_canonical_summary() -> dict:
    return {
        "schema_version": "summary_v2_lite",
        "routing": {
            "paper_type": "empirical",
            "paper_subtype_raw": "survey study",
            "paper_subtype_normalized": "survey",
            "classification_status": "resolved",
            "route_confidence": "high",
            "classification_rationale": "mentions survey sample and regression",
            "secondary_candidates": [],
        },
        "core_analysis": {
            "summary": "summary",
            "key_points": ["point 1", "point 2"],
            "methodology": "method",
            "findings": "finding",
            "conclusions": "conclusion",
            "relevance": "relevance",
            "limitations": "limits",
            "theoretical_framework": "TAM",
            "research_gap": "lack of longitudinal evidence",
            "future_research_directions": ["test cross-cultural differences"],
        },
        "specialized_details": {
            "empirical": {
                "research_questions_or_hypotheses": ["H1"],
                "data_source_and_size": "survey, n=300",
                "analysis_technique": "regression",
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
        "quality_audit": {
            "extraction_confidence": "medium",
            "completeness_score": 0.83,
            "needs_manual_review": False,
            "missing_critical_fields": [],
            "conflict_flags": [],
            "inferred_fields": [],
        },
    }


def test_flatten_specialized_details_exposes_router_summary_fields() -> None:
    flattened = _flatten_specialized_details(_build_canonical_summary())

    assert flattened["论文类型"] == "empirical"
    assert flattened["论文子类型"] == "survey"
    assert flattened["分类状态"] == "resolved"
    assert flattened["路由置信度"] == "high"
    assert flattened["分类依据"] == "mentions survey sample and regression"
    assert flattened["研究空白"] == "lack of longitudinal evidence"
    assert flattened["研究问题/假设"] == "H1"
    assert flattened["数据来源与样本"] == "survey, n=300"
    assert flattened["分析技术"] == "regression"
    assert flattened["样本/情境"] == "hotel customers"
    assert flattened["未来研究方向"] == "test cross-cultural differences"
    assert flattened["提取置信度"] == "high"


def test_canonical_paper_type_reads_routing_type() -> None:
    assert _canonical_paper_type(_build_canonical_summary()) == "empirical"

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = "summary_v2_lite"
PAPER_TYPES: Tuple[str, ...] = ("empirical", "review", "conceptual")
CLASSIFICATION_STATUSES: Tuple[str, ...] = ("resolved", "uncertain", "hybrid")
ROUTE_CONFIDENCE_VALUES: Tuple[str, ...] = ("high", "medium", "low")
SHEET_TARGETS: Dict[str, str] = {
    "empirical": "实证论文",
    "review": "综述论文",
    "conceptual": "概念论文",
}

SUBTYPE_VOCAB: Dict[str, Tuple[str, ...]] = {
    "empirical": (
        "quantitative",
        "qualitative",
        "mixed_method",
        "experiment",
        "survey",
        "archival",
        "case_study",
        "panel",
        "field_study",
    ),
    "review": (
        "systematic_review",
        "meta_analysis",
        "bibliometric_review",
        "scoping_review",
        "narrative_review",
        "integrative_review",
    ),
    "conceptual": (
        "theory_building",
        "framework_development",
        "perspective",
        "commentary",
        "model_proposition",
    ),
}

PAPER_TYPE_ALIASES: Dict[str, str] = {
    "empirical": "empirical",
    "experimental": "empirical",
    "experiment": "empirical",
    "quantitative": "empirical",
    "qualitative": "empirical",
    "mixed method": "empirical",
    "mixed methods": "empirical",
    "mixed-method": "empirical",
    "mixed-methods": "empirical",
    "survey": "empirical",
    "archival": "empirical",
    "case study": "empirical",
    "field study": "empirical",
    "panel": "empirical",
    "review": "review",
    "systematic review": "review",
    "meta analysis": "review",
    "meta-analysis": "review",
    "bibliometric": "review",
    "bibliometric review": "review",
    "scoping review": "review",
    "narrative review": "review",
    "integrative review": "review",
    "conceptual": "conceptual",
    "theoretical": "conceptual",
    "theory": "conceptual",
    "framework": "conceptual",
    "perspective": "conceptual",
    "commentary": "conceptual",
    "model proposition": "conceptual",
    "uncertain": "",
}

FIELD_OWNER_REGISTRY: Dict[str, Dict[str, Any]] = {
    "core_analysis.summary": {"critical_for": ("all",)},
    "core_analysis.key_points": {},
    "core_analysis.methodology": {"critical_for": ("all",)},
    "core_analysis.findings": {"critical_for": ("all",)},
    "core_analysis.conclusions": {"critical_for": ("all",)},
    "core_analysis.relevance": {},
    "core_analysis.limitations": {},
    "core_analysis.theoretical_framework": {
        "critical_for": ("conceptual",),
    },
    "core_analysis.research_gap": {},
    "core_analysis.future_research_directions": {},
    "specialized_details.empirical.research_questions_or_hypotheses": {},
    "specialized_details.empirical.data_source_and_size": {
        "critical_for": ("empirical",),
    },
    "specialized_details.empirical.analysis_technique": {
        "critical_for": ("empirical",),
    },
    "specialized_details.empirical.core_variables": {},
    "specialized_details.empirical.sample_characteristics_or_context": {},
    "specialized_details.review.review_type": {
        "critical_for": ("review",),
    },
    "specialized_details.review.search_databases": {},
    "specialized_details.review.time_span": {},
    "specialized_details.review.included_studies_count": {},
    "specialized_details.review.inclusion_exclusion_criteria": {},
    "specialized_details.review.synthesis_approach": {
        "critical_for": ("review",),
    },
    "specialized_details.review.main_themes": {},
    "specialized_details.conceptual.core_propositions": {},
    "specialized_details.conceptual.conceptual_relationships": {},
    "specialized_details.conceptual.theoretical_contributions": {
        "critical_for": ("conceptual",),
    },
}


def _normalize_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _unique_preserve_order(values: Iterable[str]) -> List[str]:
    seen = set()
    normalized: List[str] = []
    for value in values:
        text = value.strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


def _normalize_string_list(value: Any, max_items: Optional[int] = None) -> List[str]:
    items: Sequence[Any]
    if value is None:
        items = []
    elif isinstance(value, list):
        items = value
    else:
        items = [value]
    normalized = _unique_preserve_order(str(item).strip() for item in items if str(item).strip())
    if max_items is not None:
        return normalized[:max_items]
    return normalized


def _normalize_key_points(value: Any) -> List[str]:
    return _normalize_string_list(value, max_items=7)


def _normalize_route_confidence(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in ROUTE_CONFIDENCE_VALUES:
        return text
    return "low"


def _normalize_paper_type(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in PAPER_TYPES:
        return text
    if text in PAPER_TYPE_ALIASES:
        return PAPER_TYPE_ALIASES[text] or None
    if any(token in text for token in ("review", "meta", "bibliometric", "prisma", "scoping", "narrative")):
        return "review"
    if any(token in text for token in ("concept", "theor", "framework", "perspective", "commentary", "proposition")):
        return "conceptual"
    if any(token in text for token in ("empirical", "qualitative", "quantitative", "mixed", "survey", "experiment", "case", "archival", "panel")):
        return "empirical"
    return None


def _normalize_classification_status(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    if text in CLASSIFICATION_STATUSES:
        return text
    return None


def _normalize_core_variables(value: Any) -> Dict[str, List[str]]:
    base = {
        "independent": [],
        "dependent": [],
        "mediators": [],
        "moderators": [],
        "controls": [],
        "other_core_constructs": [],
    }
    if not isinstance(value, Mapping):
        return base
    for key in list(base.keys()):
        base[key] = _normalize_string_list(value.get(key))
    return base


def _empty_empirical_details() -> Dict[str, Any]:
    return {
        "research_questions_or_hypotheses": [],
        "data_source_and_size": None,
        "analysis_technique": None,
        "core_variables": _normalize_core_variables({}),
        "sample_characteristics_or_context": None,
    }


def _empty_review_details() -> Dict[str, Any]:
    return {
        "review_type": None,
        "search_databases": [],
        "time_span": None,
        "included_studies_count": None,
        "inclusion_exclusion_criteria": None,
        "synthesis_approach": None,
        "main_themes": [],
    }


def _empty_conceptual_details() -> Dict[str, Any]:
    return {
        "core_propositions": [],
        "conceptual_relationships": None,
        "theoretical_contributions": None,
    }


def default_routing() -> Dict[str, Any]:
    return {
        "paper_type": None,
        "paper_subtype_raw": None,
        "paper_subtype_normalized": None,
        "classification_status": "uncertain",
        "route_confidence": "low",
        "classification_rationale": None,
        "secondary_candidates": [],
    }


def default_core_analysis() -> Dict[str, Any]:
    return {
        "summary": None,
        "key_points": [],
        "methodology": None,
        "findings": None,
        "conclusions": None,
        "relevance": None,
        "limitations": None,
        "theoretical_framework": None,
        "research_gap": None,
        "future_research_directions": [],
    }


def default_paper_metadata() -> Dict[str, Any]:
    return {
        "title": None,
        "authors": [],
        "year": None,
        "journal": None,
        "doi": None,
    }


def default_specialized_details() -> Dict[str, Any]:
    return {
        "empirical": None,
        "review": None,
        "conceptual": None,
    }


def default_quality_audit() -> Dict[str, Any]:
    return {
        "extraction_confidence": "low",
        "completeness_score": 0.0,
        "needs_manual_review": True,
        "missing_critical_fields": [],
        "conflict_flags": [],
        "inferred_fields": [],
    }


def default_ai_summary() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "routing": default_routing(),
        "core_analysis": default_core_analysis(),
        "paper_metadata": default_paper_metadata(),
        "specialized_details": default_specialized_details(),
        "quality_audit": default_quality_audit(),
    }


def _normalize_empirical_details(value: Any) -> Dict[str, Any]:
    normalized = _empty_empirical_details()
    if not isinstance(value, Mapping):
        return normalized
    normalized["research_questions_or_hypotheses"] = _normalize_string_list(value.get("research_questions_or_hypotheses"))
    normalized["data_source_and_size"] = _normalize_text(value.get("data_source_and_size"))
    normalized["analysis_technique"] = _normalize_text(value.get("analysis_technique"))
    normalized["core_variables"] = _normalize_core_variables(value.get("core_variables"))
    normalized["sample_characteristics_or_context"] = _normalize_text(value.get("sample_characteristics_or_context"))
    return normalized


def _normalize_review_details(value: Any) -> Dict[str, Any]:
    normalized = _empty_review_details()
    if not isinstance(value, Mapping):
        return normalized
    normalized["review_type"] = _normalize_text(value.get("review_type"))
    normalized["search_databases"] = _normalize_string_list(value.get("search_databases"))
    normalized["time_span"] = _normalize_text(value.get("time_span"))
    normalized["included_studies_count"] = _normalize_text(value.get("included_studies_count"))
    normalized["inclusion_exclusion_criteria"] = _normalize_text(value.get("inclusion_exclusion_criteria"))
    normalized["synthesis_approach"] = _normalize_text(value.get("synthesis_approach"))
    normalized["main_themes"] = _normalize_string_list(value.get("main_themes"))
    return normalized


def _normalize_conceptual_details(value: Any) -> Dict[str, Any]:
    normalized = _empty_conceptual_details()
    if not isinstance(value, Mapping):
        return normalized
    normalized["core_propositions"] = _normalize_string_list(value.get("core_propositions"))
    normalized["conceptual_relationships"] = _normalize_text(value.get("conceptual_relationships"))
    normalized["theoretical_contributions"] = _normalize_text(value.get("theoretical_contributions"))
    return normalized


def _has_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_has_content(item) for item in value)
    if isinstance(value, Mapping):
        return any(_has_content(item) for item in value.values())
    return True


def _candidate_types_from_branches(branches: Mapping[str, Any]) -> List[str]:
    candidates: List[str] = []
    for paper_type in PAPER_TYPES:
        branch_value = branches.get(paper_type)
        if isinstance(branch_value, Mapping) and _has_content(branch_value):
            candidates.append(paper_type)
    return candidates


def _normalize_subtype_for_type(paper_type: Optional[str], raw_value: Any) -> Optional[str]:
    if not paper_type:
        return None
    raw_text = str(raw_value or "").strip()
    if not raw_text:
        return None
    text = raw_text.lower().replace("-", " ").replace("_", " ")
    if paper_type == "empirical":
        if "mixed" in text:
            return "mixed_method"
        if "field" in text:
            return "field_study"
        if "case" in text:
            return "case_study"
        if "panel" in text:
            return "panel"
        if "archiv" in text:
            return "archival"
        if "survey" in text:
            return "survey"
        if "experiment" in text:
            return "experiment"
        if "qualit" in text:
            return "qualitative"
        if "quantit" in text:
            return "quantitative"
        return None
    if paper_type == "review":
        if "systematic" in text:
            return "systematic_review"
        if "meta" in text:
            return "meta_analysis"
        if "bibliometric" in text:
            return "bibliometric_review"
        if "scoping" in text:
            return "scoping_review"
        if "narrative" in text:
            return "narrative_review"
        if "integrative" in text:
            return "integrative_review"
        return None
    if paper_type == "conceptual":
        if "theory" in text:
            return "theory_building"
        if "framework" in text:
            return "framework_development"
        if "perspective" in text:
            return "perspective"
        if "commentary" in text:
            return "commentary"
        if "proposition" in text or "model" in text:
            return "model_proposition"
        return None
    return None


def _detect_subtype_mismatch(paper_type: Optional[str], subtype_raw: Optional[str]) -> bool:
    if not paper_type or not subtype_raw:
        return False
    subtype_candidate_type = _normalize_paper_type(subtype_raw)
    return bool(subtype_candidate_type and subtype_candidate_type != paper_type)


def _normalize_secondary_candidates(
    value: Any,
    primary_type: Optional[str],
    fallback_candidates: Optional[Iterable[str]] = None,
) -> List[str]:
    raw_values = _normalize_string_list(value)
    normalized: List[str] = []
    seen = set()
    for raw_value in raw_values:
        candidate = _normalize_paper_type(raw_value)
        if not candidate or candidate == primary_type or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)

    if not normalized and fallback_candidates:
        for candidate in fallback_candidates:
            if candidate in PAPER_TYPES and candidate != primary_type and candidate not in seen:
                seen.add(candidate)
                normalized.append(candidate)

    return normalized[:2]


def _normalize_core_analysis_from_canonical(value: Any) -> Dict[str, Any]:
    normalized = default_core_analysis()
    if not isinstance(value, Mapping):
        return normalized
    normalized["summary"] = _normalize_text(value.get("summary"))
    normalized["key_points"] = _normalize_key_points(value.get("key_points"))
    normalized["methodology"] = _normalize_text(value.get("methodology"))
    normalized["findings"] = _normalize_text(value.get("findings"))
    normalized["conclusions"] = _normalize_text(value.get("conclusions"))
    normalized["relevance"] = _normalize_text(value.get("relevance"))
    normalized["limitations"] = _normalize_text(value.get("limitations"))
    normalized["theoretical_framework"] = _normalize_text(value.get("theoretical_framework"))
    normalized["research_gap"] = _normalize_text(value.get("research_gap"))
    normalized["future_research_directions"] = _normalize_string_list(value.get("future_research_directions"))
    return normalized


def _normalize_paper_metadata(value: Any) -> Dict[str, Any]:
    normalized = default_paper_metadata()
    if not isinstance(value, Mapping):
        return normalized
    normalized["title"] = _normalize_text(value.get("title"))
    normalized["authors"] = _normalize_string_list(value.get("authors"))
    normalized["year"] = _normalize_text(value.get("year"))
    normalized["journal"] = _normalize_text(value.get("journal"))
    normalized["doi"] = _normalize_text(value.get("doi"))
    return normalized


def _normalize_specialized_from_canonical(value: Any) -> Dict[str, Any]:
    normalized = default_specialized_details()
    if not isinstance(value, Mapping):
        return normalized

    empirical = _normalize_empirical_details(value.get("empirical"))
    review = _normalize_review_details(value.get("review"))
    conceptual = _normalize_conceptual_details(value.get("conceptual"))

    normalized["empirical"] = empirical if _has_content(empirical) else None
    normalized["review"] = review if _has_content(review) else None
    normalized["conceptual"] = conceptual if _has_content(conceptual) else None
    return normalized


def _fill_classification_rationale(routing: Dict[str, Any], inferred_fields: List[str]) -> None:
    needs_rationale = routing["classification_status"] != "resolved" or bool(routing["secondary_candidates"])
    rationale = _normalize_text(routing.get("classification_rationale"))
    if not needs_rationale:
        routing["classification_rationale"] = rationale
        return

    if rationale:
        routing["classification_rationale"] = rationale
        return

    if routing["classification_status"] == "uncertain":
        routing["classification_rationale"] = "insufficient evidence to assign a stable primary type"
    else:
        secondary = ", ".join(routing["secondary_candidates"]) if routing["secondary_candidates"] else "other candidate types"
        routing["classification_rationale"] = (
            f"primary type {routing.get('paper_type') or 'unknown'} selected, but competing evidence also supports {secondary}"
        )
    if "routing.classification_rationale" not in inferred_fields:
        inferred_fields.append("routing.classification_rationale")


def _canonical_value(ai_summary: Mapping[str, Any], path: str) -> Any:
    current: Any = ai_summary
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _compute_missing_critical_fields(ai_summary: Mapping[str, Any]) -> List[str]:
    routing = ai_summary.get("routing", {})
    paper_type = routing.get("paper_type")
    critical_paths = [
        "core_analysis.summary",
        "core_analysis.methodology",
        "core_analysis.findings",
        "core_analysis.conclusions",
    ]
    if paper_type == "empirical":
        critical_paths.extend(
            [
                "specialized_details.empirical.data_source_and_size",
                "specialized_details.empirical.analysis_technique",
            ]
        )
    elif paper_type == "review":
        critical_paths.extend(
            [
                "specialized_details.review.review_type",
                "specialized_details.review.synthesis_approach",
            ]
        )
    elif paper_type == "conceptual":
        critical_paths.extend(
            [
                "core_analysis.theoretical_framework",
                "specialized_details.conceptual.theoretical_contributions",
            ]
        )
    return [path for path in critical_paths if not _has_content(_canonical_value(ai_summary, path))]


def _compute_completeness_score(ai_summary: Mapping[str, Any], missing_fields: Sequence[str]) -> float:
    routing = ai_summary.get("routing", {})
    paper_type = routing.get("paper_type")
    denominator = 4
    if paper_type == "empirical":
        denominator += 2
    elif paper_type == "review":
        denominator += 2
    elif paper_type == "conceptual":
        denominator += 2
    numerator = max(denominator - len(missing_fields), 0)
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def _compute_extraction_confidence(
    route_confidence: str,
    completeness_score: float,
    conflict_flags: Sequence[str],
) -> str:
    if completeness_score >= 0.8 and not conflict_flags and route_confidence == "high":
        return "high"
    if completeness_score >= 0.6 and len(conflict_flags) <= 1:
        return "medium"
    return "low"


def _compute_conflict_flags(ai_summary: Mapping[str, Any]) -> List[str]:
    routing = ai_summary.get("routing", {})
    specialized = ai_summary.get("specialized_details", {})
    paper_type = routing.get("paper_type")
    status = routing.get("classification_status")
    subtype_raw = routing.get("paper_subtype_raw")
    active_branches = [name for name in PAPER_TYPES if isinstance(specialized.get(name), Mapping) and _has_content(specialized.get(name))]

    conflict_flags: List[str] = []
    if status == "resolved" and not paper_type:
        conflict_flags.append("resolved_without_primary_type")
    if status == "hybrid" and not paper_type:
        conflict_flags.append("status_type_inconsistency")
    if status == "uncertain" and paper_type is not None:
        conflict_flags.append("status_type_inconsistency")
    if len(active_branches) > 1:
        conflict_flags.append("multiple_specialized_branches_populated")
    if paper_type and not _has_content(specialized.get(paper_type)):
        conflict_flags.append("routed_branch_missing")
    if _detect_subtype_mismatch(paper_type, subtype_raw):
        conflict_flags.append("subtype_type_mismatch")
    return conflict_flags


def _ensure_specialized_activation(specialized: Dict[str, Any], paper_type: Optional[str]) -> Dict[str, Any]:
    normalized = default_specialized_details()
    if paper_type:
        if paper_type == "empirical":
            normalized["empirical"] = _normalize_empirical_details(specialized.get("empirical"))
        elif paper_type == "review":
            normalized["review"] = _normalize_review_details(specialized.get("review"))
        elif paper_type == "conceptual":
            normalized["conceptual"] = _normalize_conceptual_details(specialized.get("conceptual"))
        return normalized

    for branch_name in PAPER_TYPES:
        branch_value = specialized.get(branch_name)
        if isinstance(branch_value, Mapping) and _has_content(branch_value):
            if branch_name == "empirical":
                normalized[branch_name] = _normalize_empirical_details(branch_value)
            elif branch_name == "review":
                normalized[branch_name] = _normalize_review_details(branch_value)
            else:
                normalized[branch_name] = _normalize_conceptual_details(branch_value)
    return normalized


def _normalize_canonical_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    inferred_fields: List[str] = []

    routing_raw = payload.get("routing")
    routing_source: Mapping[str, Any] = routing_raw if isinstance(routing_raw, Mapping) else {}
    specialized_source = _normalize_specialized_from_canonical(payload.get("specialized_details"))
    branch_candidates = _candidate_types_from_branches(specialized_source)

    raw_paper_type_text = _normalize_text(routing_source.get("paper_type"))
    explicit_paper_type = _normalize_paper_type(routing_source.get("paper_type"))
    explicit_status = _normalize_classification_status(routing_source.get("classification_status"))
    route_confidence = _normalize_route_confidence(routing_source.get("route_confidence"))

    paper_type = explicit_paper_type
    if not paper_type and len(branch_candidates) == 1:
        paper_type = branch_candidates[0]
        inferred_fields.append("routing.paper_type")

    subtype_raw = _normalize_text(routing_source.get("paper_subtype_raw"))
    if not subtype_raw and raw_paper_type_text and raw_paper_type_text.lower() not in PAPER_TYPES:
        subtype_raw = raw_paper_type_text
    subtype_normalized = _normalize_subtype_for_type(paper_type, routing_source.get("paper_subtype_normalized") or subtype_raw)

    secondary_candidates = _normalize_secondary_candidates(
        routing_source.get("secondary_candidates"),
        paper_type,
        fallback_candidates=[candidate for candidate in branch_candidates if candidate != paper_type]
        if explicit_status == "hybrid"
        else None,
    )

    if explicit_status == "uncertain":
        if paper_type:
            secondary_candidates = _normalize_secondary_candidates(
                secondary_candidates or [paper_type],
                None,
                fallback_candidates=[paper_type],
            )
        paper_type = None
        subtype_normalized = None
        status = "uncertain"
    elif explicit_status == "hybrid" and paper_type and secondary_candidates:
        status = "hybrid"
    elif explicit_status == "resolved" and paper_type:
        status = "resolved"
    elif paper_type is None:
        status = "uncertain"
    elif secondary_candidates:
        status = "hybrid"
    else:
        status = "resolved"

    routing = default_routing()
    routing["paper_type"] = paper_type
    routing["paper_subtype_raw"] = subtype_raw
    routing["paper_subtype_normalized"] = subtype_normalized if paper_type else None
    routing["classification_status"] = status
    routing["route_confidence"] = route_confidence
    routing["classification_rationale"] = _normalize_text(routing_source.get("classification_rationale"))
    routing["secondary_candidates"] = secondary_candidates

    core_analysis = _normalize_core_analysis_from_canonical(payload.get("core_analysis"))
    paper_metadata = _normalize_paper_metadata(payload.get("paper_metadata"))
    specialized_details = _ensure_specialized_activation(specialized_source, paper_type)
    _fill_classification_rationale(routing, inferred_fields)

    ai_summary = {
        "schema_version": SCHEMA_VERSION,
        "routing": routing,
        "core_analysis": core_analysis,
        "paper_metadata": paper_metadata,
        "specialized_details": specialized_details,
        "quality_audit": default_quality_audit(),
    }

    missing_critical_fields = _compute_missing_critical_fields(ai_summary)
    conflict_flags = _compute_conflict_flags(ai_summary)
    completeness_score = _compute_completeness_score(ai_summary, missing_critical_fields)
    extraction_confidence = _compute_extraction_confidence(
        routing["route_confidence"],
        completeness_score,
        conflict_flags,
    )
    ai_summary["quality_audit"] = {
        "extraction_confidence": extraction_confidence,
        "completeness_score": completeness_score,
        "needs_manual_review": (
            routing["classification_status"] != "resolved"
            or routing["route_confidence"] == "low"
            or completeness_score < 0.6
            or bool(conflict_flags)
        ),
        "missing_critical_fields": missing_critical_fields,
        "conflict_flags": conflict_flags,
        "inferred_fields": _unique_preserve_order(inferred_fields),
    }
    return ai_summary


def is_canonical_ai_summary(payload: Any) -> bool:
    return isinstance(payload, Mapping) and (
        payload.get("schema_version") == SCHEMA_VERSION
        or any(key in payload for key in ("routing", "core_analysis", "paper_metadata", "specialized_details", "quality_audit"))
    )


def normalize_ai_summary(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("current summary payload must be a mapping")
    legacy_keys = {"common_core", "type_specific_details"}.intersection(payload)
    if legacy_keys:
        keys = ", ".join(sorted(str(key) for key in legacy_keys))
        raise ValueError(
            f"legacy summary shape is not accepted ({keys}); use summary_v2_lite canonical fields"
        )
    if is_canonical_ai_summary(payload):
        return _normalize_canonical_payload(payload)
    raise ValueError(
        "current summary payload must contain summary_v2_lite canonical fields"
    )


def get_ai_summary(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, Mapping) and isinstance(payload.get("ai_summary"), Mapping):
        return normalize_ai_summary(payload.get("ai_summary"))
    return normalize_ai_summary(payload)


def get_core_analysis(payload: Any) -> Dict[str, Any]:
    return get_ai_summary(payload)["core_analysis"]


def get_paper_metadata(payload: Any) -> Dict[str, Any]:
    return get_ai_summary(payload)["paper_metadata"]


def get_routing(payload: Any) -> Dict[str, Any]:
    return get_ai_summary(payload)["routing"]


def get_specialized_details(payload: Any) -> Dict[str, Any]:
    return get_ai_summary(payload)["specialized_details"]


def get_quality_audit(payload: Any) -> Dict[str, Any]:
    return get_ai_summary(payload)["quality_audit"]


def get_primary_sheet_target(payload: Any) -> str:
    paper_type = get_routing(payload).get("paper_type")
    return SHEET_TARGETS.get(str(paper_type or ""), "")


def canonical_ai_summary_json(payload: Any) -> str:
    return json.dumps(get_ai_summary(payload), ensure_ascii=False, indent=2)

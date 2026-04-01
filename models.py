# file: models.py

"""
Core typed models for the project.
"""

from typing import Any, Dict, List, Optional, TypedDict
from typing_extensions import NotRequired


class PaperInfo(TypedDict, total=False):
    title: str
    authors: List[str]
    year: str
    journal: str
    doi: str
    attachments: List[str]
    pdf_path: str
    file_index: int
    item_type: str
    abstract: str
    publication_title: str
    volume: str
    issue: str
    pages: str


class LegacyCommonCoreSummary(TypedDict, total=False):
    title: str
    authors: List[str]
    year: str
    journal: str
    doi: str
    summary: str
    key_points: List[str]
    methodology: str
    findings: str
    conclusions: str
    relevance: str
    limitations: str


class CoreVariables(TypedDict, total=False):
    independent: List[str]
    dependent: List[str]
    mediators: List[str]
    moderators: List[str]
    controls: List[str]
    other_core_constructs: List[str]


class EmpiricalDetails(TypedDict, total=False):
    research_questions_or_hypotheses: List[str]
    data_source_and_size: Optional[str]
    analysis_technique: Optional[str]
    core_variables: CoreVariables
    sample_characteristics_or_context: Optional[str]


class ReviewDetails(TypedDict, total=False):
    review_type: Optional[str]
    search_databases: List[str]
    time_span: Optional[str]
    included_studies_count: Optional[str]
    inclusion_exclusion_criteria: Optional[str]
    synthesis_approach: Optional[str]
    main_themes: List[str]


class ConceptualDetails(TypedDict, total=False):
    core_propositions: List[str]
    conceptual_relationships: Optional[str]
    theoretical_contributions: Optional[str]


class LegacyTypeSpecificDetails(TypedDict, total=False):
    paper_type: str
    paper_subtype: str
    route_confidence: str
    classification_rationale: str
    theoretical_framework: str
    research_gap: str
    research_questions_or_hypotheses: List[str]
    data_source_and_size: str
    analysis_technique: str
    core_variables: CoreVariables
    sample_characteristics_or_context: str
    future_research_directions: List[str]
    extraction_confidence: Any
    empirical_details: EmpiricalDetails
    review_details: Dict[str, Any]
    conceptual_details: Dict[str, Any]


class RoutingInfo(TypedDict):
    paper_type: Optional[str]
    paper_subtype_raw: Optional[str]
    paper_subtype_normalized: Optional[str]
    classification_status: str
    route_confidence: str
    classification_rationale: Optional[str]
    secondary_candidates: List[str]


class CoreAnalysis(TypedDict):
    summary: Optional[str]
    key_points: List[str]
    methodology: Optional[str]
    findings: Optional[str]
    conclusions: Optional[str]
    relevance: Optional[str]
    limitations: Optional[str]
    theoretical_framework: Optional[str]
    research_gap: Optional[str]
    future_research_directions: List[str]


class SpecializedDetails(TypedDict):
    empirical: Optional[EmpiricalDetails]
    review: Optional[ReviewDetails]
    conceptual: Optional[ConceptualDetails]


class QualityAudit(TypedDict):
    extraction_confidence: str
    completeness_score: float
    needs_manual_review: bool
    missing_critical_fields: List[str]
    conflict_flags: List[str]
    inferred_fields: List[str]


class ConceptAnalysis(TypedDict, total=False):
    contribution_to_concept: str
    position_in_development: str
    novelty_or_confirmation: str


class AISummary(TypedDict):
    schema_version: str
    routing: RoutingInfo
    core_analysis: CoreAnalysis
    specialized_details: SpecializedDetails
    quality_audit: QualityAudit
    concept_analysis: NotRequired[Optional[ConceptAnalysis]]


class ProcessingResult(TypedDict):
    paper_info: PaperInfo
    status: str
    ai_summary: NotRequired[Optional[AISummary]]
    processing_time: NotRequired[Optional[str]]
    text_length: NotRequired[Optional[int]]
    preprocess: NotRequired[Dict[str, Any]]
    failure_reason: NotRequired[Optional[str]]


class FailedPaper(TypedDict):
    paper_info: PaperInfo
    failure_reason: str


class APIConfig(TypedDict):
    api_key: Optional[str]
    model: Optional[str]
    api_base: Optional[str]


SummariesList = List[ProcessingResult]

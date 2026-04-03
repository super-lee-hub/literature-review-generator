from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Sequence, Set


WHITELISTED_FIELDS: Set[str] = {
    "core_analysis.abstract",
    "core_analysis.methods",
    "core_analysis.findings",
    "core_analysis.conclusions",
    "core_analysis.relevance",
    "core_analysis.limitations",
    "core_analysis.theoretical_framework",
    "core_analysis.research_gap",
    "paper_info.title",
    "paper_info.authors",
    "paper_info.year",
    "paper_info.journal",
}


@dataclass(frozen=True)
class SummaryCorrectionCandidate:
    field_path: str
    current_value: Any
    suggested_value: Any
    confidence: float
    evidence_source: str
    reason: str


@dataclass(frozen=True)
class SummaryRecheckReport:
    paper_key: str
    recheck_id: str
    created_at: str
    correction_candidates: List[SummaryCorrectionCandidate]
    fields_checked: List[str]
    fields_with_candidates: List[str]


class SummaryRechecker:
    def __init__(self, paper_artifact: Dict[str, Any]):
        self.paper_artifact = paper_artifact
        self.ai_summary = paper_artifact.get("analysis", {}).get("ai_summary", {})

    def recheck(self) -> SummaryRecheckReport:
        from datetime import datetime
        import uuid

        correction_candidates: List[SummaryCorrectionCandidate] = []
        fields_checked: List[str] = []

        for field_path in WHITELISTED_FIELDS:
            fields_checked.append(field_path)
            candidate = self._check_field(field_path)
            if candidate:
                correction_candidates.append(candidate)

        return SummaryRecheckReport(
            paper_key=self.paper_artifact.get("paper_identity", {}).get("canonical_paper_key", ""),
            recheck_id=str(uuid.uuid4()),
            created_at=datetime.now().isoformat(),
            correction_candidates=correction_candidates,
            fields_checked=fields_checked,
            fields_with_candidates=[c.field_path for c in correction_candidates],
        )

    def _check_field(self, field_path: str) -> Optional[SummaryCorrectionCandidate]:
        current_value = self._get_nested_value(self.ai_summary, field_path)
        if current_value is None:
            return None
        
        # Get original paper content if available
        original_content = self._get_nested_value(self.paper_artifact, "preprocess_artifacts.normalized_text") or ""
        
        # Check for empty or too short values
        if isinstance(current_value, str):
            # Trim whitespace
            trimmed_value = current_value.strip()
            
            # Check if value is empty or too short
            if not trimmed_value:
                return SummaryCorrectionCandidate(
                    field_path=field_path,
                    current_value=current_value,
                    suggested_value="",
                    confidence=0.9,
                    evidence_source="validation_logic",
                    reason="Field is empty or contains only whitespace"
                )
            
            # Check if value is too short for certain fields
            if field_path in ["core_analysis.abstract", "core_analysis.methods", "core_analysis.findings"]:
                if len(trimmed_value) < 50:
                    return SummaryCorrectionCandidate(
                        field_path=field_path,
                        current_value=current_value,
                        suggested_value=trimmed_value,
                        confidence=0.7,
                        evidence_source="validation_logic",
                        reason="Field value is unusually short"
                    )
        
        # Check for potential issues in paper info fields
        if field_path == "paper_info.year":
            if isinstance(current_value, str):
                if not current_value.isdigit() or len(current_value) != 4:
                    return SummaryCorrectionCandidate(
                        field_path=field_path,
                        current_value=current_value,
                        suggested_value=current_value,
                        confidence=0.8,
                        evidence_source="validation_logic",
                        reason="Year format appears to be invalid"
                    )
        
        # Check for authors field
        if field_path == "paper_info.authors":
            if isinstance(current_value, list):
                if len(current_value) == 0:
                    return SummaryCorrectionCandidate(
                        field_path=field_path,
                        current_value=current_value,
                        suggested_value=[],
                        confidence=0.9,
                        evidence_source="validation_logic",
                        reason="Authors list is empty"
                    )
            elif isinstance(current_value, str):
                if not current_value.strip():
                    return SummaryCorrectionCandidate(
                        field_path=field_path,
                        current_value=current_value,
                        suggested_value="",
                        confidence=0.9,
                        evidence_source="validation_logic",
                        reason="Authors field is empty"
                    )
        
        return None

    def _get_nested_value(self, data: Dict[str, Any], path: str) -> Any:
        keys = path.split(".")
        current = data
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return None
        return current


def run_summary_rechecks(
    paper_artifacts: Sequence[Dict[str, Any]],
) -> List[SummaryRecheckReport]:
    reports: List[SummaryRecheckReport] = []
    for artifact in paper_artifacts:
        rechecker = SummaryRechecker(artifact)
        reports.append(rechecker.recheck())
    return reports

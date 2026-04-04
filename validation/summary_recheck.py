from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Sequence, Set


# Field owner registry for canonical-only patching
FIELD_OWNER_REGISTRY: Dict[str, str] = {
    # Core analysis fields (canonical path)
    "core_analysis.summary": "canonical",
    "core_analysis.methodology": "canonical",
    "core_analysis.findings": "canonical",
    "core_analysis.conclusions": "canonical",
    "core_analysis.relevance": "canonical",
    "core_analysis.limitations": "canonical",
    "core_analysis.theoretical_framework": "canonical",
    "core_analysis.research_gap": "canonical",
    # Paper metadata fields (canonical path)
    "paper_metadata.title": "canonical",
    "paper_metadata.authors": "canonical",
    "paper_metadata.year": "canonical",
    "paper_metadata.journal": "canonical",
}

# Canonical fields only (derived from FIELD_OWNER_REGISTRY)
CANONICAL_FIELDS: Set[str] = {
    field for field, owner in FIELD_OWNER_REGISTRY.items() if owner == "canonical"
}

# Backward compatibility
WHITELISTED_FIELDS = CANONICAL_FIELDS


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
        # Get AI summary from canonical path
        self.ai_summary = paper_artifact.get("analysis", {}).get("ai_summary", {})
        # Also check for ai_summary at top level for compatibility
        if not self.ai_summary:
            self.ai_summary = paper_artifact.get("ai_summary", {})
        # Get preprocess artifacts for source-grounded checks
        self.preprocess_artifacts = paper_artifact.get("analysis", {}).get("preprocess", {})
        # Also check for preprocess artifacts at top level for compatibility
        if not self.preprocess_artifacts:
            self.preprocess_artifacts = paper_artifact.get("preprocess_artifacts", {})

    def recheck(self) -> SummaryRecheckReport:
        from datetime import datetime
        import uuid

        correction_candidates: List[SummaryCorrectionCandidate] = []
        fields_checked: List[str] = []

        # Only check canonical fields (canonical-only patching)
        for field_path in CANONICAL_FIELDS:
            fields_checked.append(field_path)
            # First try source-grounded check, then fall back to existing checks
            candidate = self._check_field_source_grounded(field_path)
            if not candidate:
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

    def _check_field_source_grounded(self, field_path: str) -> Optional[SummaryCorrectionCandidate]:
        """Source-grounded check using preprocess artifacts (canonical-only)."""
        current_value = self._get_nested_value(self.ai_summary, field_path)
        if current_value is None:
            return None
        
        # Only apply source-grounded checks for canonical fields
        # For this slice, we focus on core analysis fields
        source_check_canonical = [
            "core_analysis.summary",
            "core_analysis.methodology",
            "core_analysis.findings",
            "core_analysis.conclusions"
        ]
        
        if field_path not in source_check_canonical or field_path not in CANONICAL_FIELDS:
            return None
        
        # Get preprocessed text for source
        normalized_text = self.preprocess_artifacts.get("normalized_text", "")
        if not normalized_text:
            return None
        
        # Simple source-grounded check:
        # If current summary field is a string and normalized text is available,
        # check if any key content appears in source text to suggest confidence
        if isinstance(current_value, str):
            # For source-grounded check we only report if we have strong evidence
            # This is a conservative check that preserves whitelist discipline
            trimmed_current = current_value.strip()
            if not trimmed_current:
                return None
            
            # Extract first 50 chars as key content to check against source
            key_content = trimmed_current[:50].lower()
            if key_content not in normalized_text.lower():
                # Key content not found in normalized text, suggest verifying
                # Keep suggested value as original (we don't mutate, just flag)
                return SummaryCorrectionCandidate(
                    field_path=field_path,
                    current_value=current_value,
                    suggested_value=current_value,
                    confidence=0.6,
                    evidence_source="preprocess_artifacts.normalized_text",
                    reason="Key content not found in normalized paper text"
                )
        
        return None

    def _check_field(self, field_path: str) -> Optional[SummaryCorrectionCandidate]:
        current_value = self._get_nested_value(self.ai_summary, field_path)
        if current_value is None:
            return None
        
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
            if field_path in ["core_analysis.summary", "core_analysis.methodology", "core_analysis.findings"]:
                if len(trimmed_value) < 50:
                    return SummaryCorrectionCandidate(
                        field_path=field_path,
                        current_value=current_value,
                        suggested_value=trimmed_value,
                        confidence=0.7,
                        evidence_source="validation_logic",
                        reason="Field value is unusually short"
                    )
        
        # Check for potential issues in paper metadata fields
        if field_path == "paper_metadata.year":
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
        if field_path == "paper_metadata.authors":
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

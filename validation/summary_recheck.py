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

    def _extract_anchors(self, text: str) -> Dict[str, Any]:
        """Extract conservative anchors from text for conflict detection.

        Returns dict with sets/lists of:
        - years: 4-digit years (1900-2099)
        - numbers: numeric quantities (integers and decimals, not years)
        - percentages: percentage values
        - sample_sizes: numbers followed by n/N/participants/subjects
        - acronyms: continuous uppercase 3+ letter acronyms
        - direction_words: increase/decrease/higher/lower etc.
        """
        import re

        text_lower = (text or "").lower()
        anchors: Dict[str, Any] = {
            "years": set(re.findall(r"\b(19\d{2}|20\d{2})\b", text or "")),
            "percentages": set(re.findall(r"(\d+(?:\.\d+)?)\s*%", text or "")),
            "numbers": set(
                m.group(1)
                for m in re.finditer(r"(?<!\d)(\d+(?:\.\d+)?)(?!\s*(?:%|\d{3}))", text or "")
            ),
            "sample_sizes": set(
                re.findall(
                    r"(?i)(?:n\s*[=:]\s*|n\s+of\s+|sample\s+(?:size|of)\s+|"
                    r"included\s+)(\d+(?:,\d{3})*)",
                    text or "",
                )
            ),
            "acronyms": set(re.findall(r"\b([A-Z][A-Z0-9]{2,}(?:\s+[A-Z][A-Z0-9]{2,})?)\b", text or "")),
            "direction_words": set(),
        }

        # Direction words only when they appear near outcome/measurement context
        # to avoid false matches on common English phrases.
        outcome_pattern = r"(?:(?:show|found|reveal|demonstrat|indicat|observ|result|effect|outcome|perform|yield|produc|associat|correlat)\w*\s+.{0,60}?)"
        direction_pairs = [
            ("increase", "decrease"),
            ("higher", "lower"),
            ("improve", "worsen"),
            ("significant", "non-significant"),
            ("positive", "negative"),
        ]
        for pos, neg in direction_pairs:
            pos_in_context = re.search(outcome_pattern + pos, text_lower) if pos in text_lower else None
            neg_in_context = re.search(outcome_pattern + neg, text_lower) if neg in text_lower else None
            # Also match the opposite order: direction word near outcome word
            if not pos_in_context and pos in text_lower:
                pos_in_context = re.search(pos + r".{0,40}?(?:show|found|demonstrat|result|effect|outcome)", text_lower)
            if not neg_in_context and neg in text_lower:
                neg_in_context = re.search(neg + r".{0,40}?(?:show|found|demonstrat|result|effect|outcome)", text_lower)

            if pos_in_context:
                anchors["direction_words"].add(pos)
            if neg_in_context:
                anchors["direction_words"].add(neg)

        # Deduplicate: numbers that are years or percentages should not double-count
        pct_strings = {f"{p}%" for p in anchors["percentages"]}
        anchors["numbers"] = {
            n for n in anchors["numbers"]
            if n not in anchors["years"] and f"{n}%" not in pct_strings
        }

        return anchors

    def _check_field_source_grounded(self, field_path: str) -> Optional[SummaryCorrectionCandidate]:
        """Conservative anchor-conflict-based source-grounded check.

        Replaces the old first-50-char substring heuristic with discrete
        anchor extraction from both summary text and source text.  Only
        clear conflicts (summary anchor absent or contradictory in source)
        produce a correction candidate.  Paraphrase without anchor conflict
        produces no candidate.
        """
        current_value = self._get_nested_value(self.ai_summary, field_path)
        if current_value is None:
            return None

        source_check_canonical = [
            "core_analysis.summary",
            "core_analysis.methodology",
            "core_analysis.findings",
            "core_analysis.conclusions",
        ]

        if field_path not in source_check_canonical or field_path not in CANONICAL_FIELDS:
            return None

        normalized_text = self.preprocess_artifacts.get("normalized_text", "")
        if not normalized_text:
            return None

        if not isinstance(current_value, str) or not current_value.strip():
            return None

        # Extract anchors from both summary and source
        summary_anchors = self._extract_anchors(current_value)
        source_anchors = self._extract_anchors(normalized_text)

        conflicts: List[str] = []

        # Year conflicts: year in summary but not in source
        for year in summary_anchors["years"]:
            if year not in source_anchors["years"]:
                conflicts.append(f"Year {year} found in summary but not in source")

        # Percentage conflicts
        for pct in summary_anchors["percentages"]:
            if pct not in source_anchors["percentages"]:
                conflicts.append(f"Percentage {pct}% found in summary but not in source")

        # Sample size conflicts
        for n_val in summary_anchors["sample_sizes"]:
            if n_val not in source_anchors["sample_sizes"]:
                conflicts.append(f"Sample size {n_val} found in summary but not in source")

        # Acronym conflicts: acronym in summary but absent in source
        for acro in summary_anchors["acronyms"]:
            if acro not in source_anchors["acronyms"]:
                conflicts.append(f"Acronym {acro} in summary but not in source")

        # Direction word conflicts: opposite direction words
        for word in summary_anchors["direction_words"]:
            source_dirs = source_anchors["direction_words"]
            if word in ("increase", "improve", "higher", "significant", "positive", "large", "greater"):
                if "decrease" in source_dirs and word not in source_dirs:
                    conflicts.append(f"Direction conflict: summary says '{word}' but source contains 'decrease'")
            elif word in ("decrease", "worsen", "lower", "non-significant", "negative", "small", "less"):
                if "increase" in source_dirs and word not in source_dirs:
                    conflicts.append(f"Direction conflict: summary says '{word}' but source contains 'increase'")

        if conflicts:
            return SummaryCorrectionCandidate(
                field_path=field_path,
                current_value=current_value,
                suggested_value=current_value,
                confidence=0.6,
                evidence_source="preprocess_artifacts.normalized_text",
                reason="; ".join(conflicts),
            )

        # No anchor conflict — paraphrase is fine, no candidate
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

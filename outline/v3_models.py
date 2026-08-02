"""Outline Intelligence v3 artifact models.

The v3 layer is deliberately additive to the existing Outline v2 models.  It
stores deterministic, paper-level projections and the global artifacts that
are shared by every later outline candidate.  No model in this module makes a
semantic claim that is not present in the canonical Stage 1 summary.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


OUTLINE_V3_VERSION = "v3"
EVIDENCE_VIEWS_ARTIFACT_TYPE = "outline_evidence_views"
GLOBAL_CORPUS_LEDGER_ARTIFACT_TYPE = "global_corpus_ledger"
MULTI_VIEW_MATRIX_ARTIFACT_TYPE = "multi_view_matrix"
REVIEW_INTENT_ARTIFACT_TYPE = "review_intent"
COVERAGE_CONTRACT_ARTIFACT_TYPE = "coverage_contract"


def canonical_json(value: Any) -> str:
    """Serialize JSON in the one canonical form used by v3 hashes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def compute_v3_hash(value: Any) -> str:
    """Return a full SHA-256 hash for a JSON-compatible value."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _stable_unique(values: Iterable[Any]) -> List[str]:
    result: Dict[str, str] = {}
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        result.setdefault(text.casefold(), text)
    return [result[key] for key in sorted(result)]


def _stable_mapping(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): value[key] for key in sorted(value, key=lambda item: str(item))}


def _list_of_dicts(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


@dataclass(frozen=True)
class OutlineEvidenceView:
    """Deterministic projection of one canonical Stage 1 summary.

    The fields named in the v3 contract are intentionally explicit.  The
    additional identity, provenance, classification, and diagnostic fields
    make the projection auditable without introducing a second semantic truth
    source.
    """

    paper_key: str
    title: str = ""
    authors: List[str] = field(default_factory=list)
    year: Optional[int] = None
    paper_type: str = ""
    research_questions: List[str] = field(default_factory=list)
    theories: List[str] = field(default_factory=list)
    constructs: List[str] = field(default_factory=list)
    mechanisms: List[str] = field(default_factory=list)
    method: List[str] = field(default_factory=list)
    sample_or_context: List[str] = field(default_factory=list)
    findings: List[str] = field(default_factory=list)
    conclusions: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    research_gaps: List[str] = field(default_factory=list)
    future_directions: List[str] = field(default_factory=list)
    relevance: List[str] = field(default_factory=list)
    source_summary_hash: str = ""
    canonical_paper_key: str = ""
    doi: str = ""
    source_paper_id: str = ""
    aliases: List[str] = field(default_factory=list)
    identity_source: str = ""
    source_summary_hashes: List[str] = field(default_factory=list)
    source_fields: Dict[str, List[str]] = field(default_factory=dict)
    classification: str = "support"
    must_use: bool = False
    diagnostics: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "paper_key": self.paper_key,
            "canonical_paper_key": self.canonical_paper_key or self.paper_key,
            "title": self.title,
            "authors": list(self.authors),
            "year": self.year,
            "paper_type": self.paper_type,
            "research_questions": list(self.research_questions),
            "theories": list(self.theories),
            "constructs": list(self.constructs),
            "mechanisms": list(self.mechanisms),
            "method": list(self.method),
            "sample_or_context": list(self.sample_or_context),
            "findings": list(self.findings),
            "conclusions": list(self.conclusions),
            "limitations": list(self.limitations),
            "research_gaps": list(self.research_gaps),
            "future_directions": list(self.future_directions),
            "relevance": list(self.relevance),
            "source_summary_hash": self.source_summary_hash,
            "doi": self.doi,
            "source_paper_id": self.source_paper_id,
            "aliases": _stable_unique(self.aliases),
            "identity_source": self.identity_source,
            "source_summary_hashes": _stable_unique(self.source_summary_hashes),
            "source_fields": _stable_mapping({
                key: _stable_unique(values)
                for key, values in self.source_fields.items()
            }),
            "classification": self.classification,
            "must_use": bool(self.must_use),
            "diagnostics": _stable_unique(self.diagnostics),
        }

    @property
    def view_hash(self) -> str:
        return compute_v3_hash(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OutlineEvidenceView":
        source_fields = {
            str(key): _stable_unique(value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else [value])
            for key, value in _stable_mapping(data.get("source_fields")).items()
        }
        return cls(
            paper_key=str(data.get("paper_key") or data.get("canonical_paper_key") or ""),
            canonical_paper_key=str(data.get("canonical_paper_key") or data.get("paper_key") or ""),
            title=str(data.get("title") or ""),
            authors=_stable_unique(data.get("authors") or []),
            year=data.get("year"),
            paper_type=str(data.get("paper_type") or ""),
            research_questions=_stable_unique(data.get("research_questions") or []),
            theories=_stable_unique(data.get("theories") or []),
            constructs=_stable_unique(data.get("constructs") or []),
            mechanisms=_stable_unique(data.get("mechanisms") or []),
            method=_stable_unique(data.get("method") or []),
            sample_or_context=_stable_unique(data.get("sample_or_context") or []),
            findings=_stable_unique(data.get("findings") or []),
            conclusions=_stable_unique(data.get("conclusions") or []),
            limitations=_stable_unique(data.get("limitations") or []),
            research_gaps=_stable_unique(data.get("research_gaps") or []),
            future_directions=_stable_unique(data.get("future_directions") or []),
            relevance=_stable_unique(data.get("relevance") or []),
            source_summary_hash=str(data.get("source_summary_hash") or ""),
            doi=str(data.get("doi") or ""),
            source_paper_id=str(data.get("source_paper_id") or ""),
            aliases=_stable_unique(data.get("aliases") or []),
            identity_source=str(data.get("identity_source") or ""),
            source_summary_hashes=_stable_unique(data.get("source_summary_hashes") or []),
            source_fields=source_fields,
            classification=str(data.get("classification") or "support"),
            must_use=bool(data.get("must_use", False)),
            diagnostics=_stable_unique(data.get("diagnostics") or []),
        )


@dataclass(frozen=True)
class OutlineEvidenceViews:
    """Artifact containing the complete deterministic evidence-view set."""

    artifact_type: str = EVIDENCE_VIEWS_ARTIFACT_TYPE
    artifact_version: str = OUTLINE_V3_VERSION
    created_from_job_id: str = ""
    views: List[OutlineEvidenceView] = field(default_factory=list)
    source_summary_hashes: List[str] = field(default_factory=list)
    alias_crosswalk: Dict[str, str] = field(default_factory=dict)
    blocking_diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    shard_id: str = ""
    shard_count: int = 1

    @property
    def evidence_views(self) -> List[OutlineEvidenceView]:
        return self.views

    @property
    def status(self) -> str:
        return "blocked" if self.blocking_diagnostics else "ready"

    def canonical_payload(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "views": [view.to_dict() for view in sorted(self.views, key=lambda item: item.paper_key)],
            "source_summary_hashes": _stable_unique(self.source_summary_hashes),
            "alias_crosswalk": {
                str(key): str(value)
                for key, value in sorted(self.alias_crosswalk.items(), key=lambda item: str(item[0]))
            },
            "blocking_diagnostics": _list_of_dicts(sorted(
                self.blocking_diagnostics,
                key=lambda item: compute_v3_hash(item),
            )),
        }

    @property
    def content_hash(self) -> str:
        return compute_v3_hash(self.canonical_payload())

    @property
    def artifact_hash(self) -> str:
        return self.content_hash

    def to_dict(self) -> Dict[str, Any]:
        payload = self.canonical_payload()
        payload.update({
            "created_from_job_id": self.created_from_job_id,
            "shard_id": self.shard_id,
            "shard_count": self.shard_count,
            "status": self.status,
            "content_hash": self.content_hash,
        })
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OutlineEvidenceViews":
        return cls(
            artifact_type=str(data.get("artifact_type") or EVIDENCE_VIEWS_ARTIFACT_TYPE),
            artifact_version=str(data.get("artifact_version") or OUTLINE_V3_VERSION),
            created_from_job_id=str(data.get("created_from_job_id") or ""),
            views=[OutlineEvidenceView.from_dict(item) for item in data.get("views", []) if isinstance(item, Mapping)],
            source_summary_hashes=_stable_unique(data.get("source_summary_hashes") or []),
            alias_crosswalk={
                str(key): str(value)
                for key, value in _stable_mapping(data.get("alias_crosswalk")).items()
            },
            blocking_diagnostics=_list_of_dicts(data.get("blocking_diagnostics")),
            shard_id=str(data.get("shard_id") or ""),
            shard_count=int(data.get("shard_count") or 1),
        )


@dataclass(frozen=True)
class GlobalCorpusLedgerEntry:
    """One auditable, compact ledger entry for one canonical paper."""

    paper_key: str
    compact_record: str = ""
    classification: str = "support"
    classification_family: str = "support"
    must_use: bool = False
    assignment_status: str = "assigned"
    exclusion_reason: str = ""
    source_summary_hash: str = ""
    dimensions: Dict[str, List[str]] = field(default_factory=dict)
    diagnostic_candidate_topics: List[str] = field(default_factory=list)
    diagnostics: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "paper_key": self.paper_key,
            "compact_record": self.compact_record,
            "classification": self.classification,
            "classification_family": self.classification_family,
            "must_use": bool(self.must_use),
            "assignment_status": self.assignment_status,
            "exclusion_reason": self.exclusion_reason,
            "source_summary_hash": self.source_summary_hash,
            "dimensions": _stable_mapping({
                key: _stable_unique(values)
                for key, values in self.dimensions.items()
            }),
            "diagnostic_candidate_topics": _stable_unique(self.diagnostic_candidate_topics),
            "diagnostics": _stable_unique(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GlobalCorpusLedgerEntry":
        dimensions = {
            str(key): _stable_unique(value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else [value])
            for key, value in _stable_mapping(data.get("dimensions")).items()
        }
        return cls(
            paper_key=str(data.get("paper_key") or ""),
            compact_record=str(data.get("compact_record") or ""),
            classification=str(data.get("classification") or "support"),
            classification_family=str(data.get("classification_family") or data.get("classification") or "support"),
            must_use=bool(data.get("must_use", False)),
            assignment_status=str(data.get("assignment_status") or "assigned"),
            exclusion_reason=str(data.get("exclusion_reason") or ""),
            source_summary_hash=str(data.get("source_summary_hash") or ""),
            dimensions=dimensions,
            diagnostic_candidate_topics=_stable_unique(data.get("diagnostic_candidate_topics") or []),
            diagnostics=_stable_unique(data.get("diagnostics") or []),
        )


@dataclass(frozen=True)
class GlobalCorpusLedger:
    artifact_type: str = GLOBAL_CORPUS_LEDGER_ARTIFACT_TYPE
    artifact_version: str = OUTLINE_V3_VERSION
    entries: List[GlobalCorpusLedgerEntry] = field(default_factory=list)
    source_summary_hashes: List[str] = field(default_factory=list)
    blocking_diagnostics: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ledger(self) -> List[GlobalCorpusLedgerEntry]:
        return self.entries

    def canonical_payload(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "entries": [entry.to_dict() for entry in sorted(self.entries, key=lambda item: item.paper_key)],
            "source_summary_hashes": _stable_unique(self.source_summary_hashes),
            "blocking_diagnostics": _list_of_dicts(sorted(
                self.blocking_diagnostics,
                key=lambda item: compute_v3_hash(item),
            )),
        }

    @property
    def content_hash(self) -> str:
        return compute_v3_hash(self.canonical_payload())

    def to_dict(self) -> Dict[str, Any]:
        payload = self.canonical_payload()
        payload["content_hash"] = self.content_hash
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GlobalCorpusLedger":
        return cls(
            artifact_type=str(data.get("artifact_type") or GLOBAL_CORPUS_LEDGER_ARTIFACT_TYPE),
            artifact_version=str(data.get("artifact_version") or OUTLINE_V3_VERSION),
            entries=[GlobalCorpusLedgerEntry.from_dict(item) for item in data.get("entries", []) if isinstance(item, Mapping)],
            source_summary_hashes=_stable_unique(data.get("source_summary_hashes") or []),
            blocking_diagnostics=_list_of_dicts(data.get("blocking_diagnostics")),
        )


@dataclass(frozen=True)
class MultiViewMatrixRow:
    paper_key: str
    dimensions: Dict[str, List[str]] = field(default_factory=dict)
    source_summary_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "paper_key": self.paper_key,
            "dimensions": _stable_mapping({
                key: _stable_unique(values)
                for key, values in self.dimensions.items()
            }),
            "source_summary_hash": self.source_summary_hash,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MultiViewMatrixRow":
        dimensions = {
            str(key): _stable_unique(value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else [value])
            for key, value in _stable_mapping(data.get("dimensions")).items()
        }
        return cls(
            paper_key=str(data.get("paper_key") or ""),
            dimensions=dimensions,
            source_summary_hash=str(data.get("source_summary_hash") or ""),
        )


@dataclass(frozen=True)
class MultiViewMatrix:
    artifact_type: str = MULTI_VIEW_MATRIX_ARTIFACT_TYPE
    artifact_version: str = OUTLINE_V3_VERSION
    dimensions: List[str] = field(default_factory=lambda: [
        "theory",
        "construct",
        "mechanism",
        "context",
        "method",
        "finding",
        "limitation",
        "gap",
        "year",
        "development",
    ])
    rows: List[MultiViewMatrixRow] = field(default_factory=list)
    normalization_aliases: Dict[str, str] = field(default_factory=dict)
    source_summary_hashes: List[str] = field(default_factory=list)
    blocking_diagnostics: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def matrix(self) -> Dict[str, Dict[str, List[str]]]:
        return {
            row.paper_key: row.dimensions
            for row in sorted(self.rows, key=lambda item: item.paper_key)
        }

    def canonical_payload(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "dimensions": _stable_unique(self.dimensions),
            "rows": [row.to_dict() for row in sorted(self.rows, key=lambda item: item.paper_key)],
            "normalization_aliases": {
                str(key): str(value)
                for key, value in sorted(self.normalization_aliases.items(), key=lambda item: str(item[0]))
            },
            "source_summary_hashes": _stable_unique(self.source_summary_hashes),
            "blocking_diagnostics": _list_of_dicts(sorted(
                self.blocking_diagnostics,
                key=lambda item: compute_v3_hash(item),
            )),
        }

    @property
    def content_hash(self) -> str:
        return compute_v3_hash(self.canonical_payload())

    def to_dict(self) -> Dict[str, Any]:
        payload = self.canonical_payload()
        payload["content_hash"] = self.content_hash
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MultiViewMatrix":
        return cls(
            artifact_type=str(data.get("artifact_type") or MULTI_VIEW_MATRIX_ARTIFACT_TYPE),
            artifact_version=str(data.get("artifact_version") or OUTLINE_V3_VERSION),
            dimensions=_stable_unique(data.get("dimensions") or []),
            rows=[MultiViewMatrixRow.from_dict(item) for item in data.get("rows", []) if isinstance(item, Mapping)],
            normalization_aliases={
                str(key): str(value)
                for key, value in _stable_mapping(data.get("normalization_aliases")).items()
            },
            source_summary_hashes=_stable_unique(data.get("source_summary_hashes") or []),
            blocking_diagnostics=_list_of_dicts(data.get("blocking_diagnostics")),
        )


@dataclass(frozen=True)
class ReviewIntent:
    """Explicit review intent shared by every candidate outline."""

    review_question: str = ""
    scope: str = ""
    target_audience: str = ""
    desired_contribution: str = ""
    preferred_organizing_logic: str = ""
    must_cover: List[str] = field(default_factory=list)
    must_not_do: List[str] = field(default_factory=list)
    language: str = ""
    target_depth: str = ""
    target_length: str = ""
    artifact_type: str = REVIEW_INTENT_ARTIFACT_TYPE
    artifact_version: str = OUTLINE_V3_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "review_question": self.review_question,
            "scope": self.scope,
            "target_audience": self.target_audience,
            "desired_contribution": self.desired_contribution,
            "preferred_organizing_logic": self.preferred_organizing_logic,
            "must_cover": _stable_unique(self.must_cover),
            "must_not_do": _stable_unique(self.must_not_do),
            "language": self.language,
            "target_depth": self.target_depth,
            "target_length": self.target_length,
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
        }

    @property
    def content_hash(self) -> str:
        return compute_v3_hash(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReviewIntent":
        return cls(
            review_question=str(data.get("review_question") or ""),
            scope=str(data.get("scope") or ""),
            target_audience=str(data.get("target_audience") or ""),
            desired_contribution=str(data.get("desired_contribution") or ""),
            preferred_organizing_logic=str(data.get("preferred_organizing_logic") or ""),
            must_cover=_stable_unique(data.get("must_cover") or []),
            must_not_do=_stable_unique(data.get("must_not_do") or []),
            language=str(data.get("language") or ""),
            target_depth=str(data.get("target_depth") or ""),
            target_length=str(data.get("target_length") or ""),
            artifact_type=str(data.get("artifact_type") or REVIEW_INTENT_ARTIFACT_TYPE),
            artifact_version=str(data.get("artifact_version") or OUTLINE_V3_VERSION),
        )


@dataclass(frozen=True)
class CoverageContract:
    """Shared coverage obligations for all candidates and adoption."""

    corpus_paper_keys: List[str] = field(default_factory=list)
    must_use_paper_keys: List[str] = field(default_factory=list)
    required_dimensions: List[str] = field(default_factory=lambda: [
        "theory",
        "construct",
        "mechanism",
        "context",
        "method",
        "finding",
        "tension",
        "history",
        "bridge",
        "gap_support",
    ])
    assignment_statuses: Dict[str, str] = field(default_factory=dict)
    unassigned_reasons: Dict[str, str] = field(default_factory=dict)
    source_summary_hashes: List[str] = field(default_factory=list)
    artifact_type: str = COVERAGE_CONTRACT_ARTIFACT_TYPE
    artifact_version: str = OUTLINE_V3_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "corpus_paper_keys": _stable_unique(self.corpus_paper_keys),
            "must_use_paper_keys": _stable_unique(self.must_use_paper_keys),
            "required_dimensions": _stable_unique(self.required_dimensions),
            "assignment_statuses": {
                str(key): str(value)
                for key, value in sorted(self.assignment_statuses.items(), key=lambda item: str(item[0]))
            },
            "unassigned_reasons": {
                str(key): str(value)
                for key, value in sorted(self.unassigned_reasons.items(), key=lambda item: str(item[0]))
            },
            "source_summary_hashes": _stable_unique(self.source_summary_hashes),
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
        }

    @property
    def content_hash(self) -> str:
        return compute_v3_hash(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CoverageContract":
        return cls(
            corpus_paper_keys=_stable_unique(data.get("corpus_paper_keys") or []),
            must_use_paper_keys=_stable_unique(data.get("must_use_paper_keys") or []),
            required_dimensions=_stable_unique(data.get("required_dimensions") or []),
            assignment_statuses={
                str(key): str(value)
                for key, value in _stable_mapping(data.get("assignment_statuses")).items()
            },
            unassigned_reasons={
                str(key): str(value)
                for key, value in _stable_mapping(data.get("unassigned_reasons")).items()
            },
            source_summary_hashes=_stable_unique(data.get("source_summary_hashes") or []),
            artifact_type=str(data.get("artifact_type") or COVERAGE_CONTRACT_ARTIFACT_TYPE),
            artifact_version=str(data.get("artifact_version") or OUTLINE_V3_VERSION),
        )


# Names without the explicit version suffix are the public v3 vocabulary.
OutlineEvidenceViewV1 = OutlineEvidenceView
OutlineEvidenceViewsV1 = OutlineEvidenceViews
GlobalCorpusLedgerV1 = GlobalCorpusLedger
MultiViewMatrixV1 = MultiViewMatrix
ReviewIntentV1 = ReviewIntent
CoverageContractV1 = CoverageContract


__all__ = [
    "OUTLINE_V3_VERSION",
    "EVIDENCE_VIEWS_ARTIFACT_TYPE",
    "GLOBAL_CORPUS_LEDGER_ARTIFACT_TYPE",
    "MULTI_VIEW_MATRIX_ARTIFACT_TYPE",
    "REVIEW_INTENT_ARTIFACT_TYPE",
    "COVERAGE_CONTRACT_ARTIFACT_TYPE",
    "canonical_json",
    "compute_v3_hash",
    "OutlineEvidenceView",
    "OutlineEvidenceViews",
    "GlobalCorpusLedgerEntry",
    "GlobalCorpusLedger",
    "MultiViewMatrixRow",
    "MultiViewMatrix",
    "ReviewIntent",
    "CoverageContract",
    "OutlineEvidenceViewV1",
    "OutlineEvidenceViewsV1",
    "GlobalCorpusLedgerV1",
    "MultiViewMatrixV1",
    "ReviewIntentV1",
    "CoverageContractV1",
]

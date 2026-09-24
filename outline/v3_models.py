"""Outline Intelligence v3 artifact models.

The current Outline v3 model stores deterministic, paper-level projections and
the global artifacts shared by every later outline candidate.  No model in
this module makes a semantic claim that is not present in the canonical Stage
1 summary.
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
CONTENT_LAYERS_ARTIFACT_TYPE = "outline_content_layers"
RELATION_EVIDENCE_BUNDLES_ARTIFACT_TYPE = "relation_evidence_bundles"
TOPIC_SYNTHESIS_ARTIFACT_TYPE = "topic_synthesis"
GLOBAL_SYNTHESIS_ARTIFACT_TYPE = "global_synthesis"

EVIDENCE_CLAIM_TYPES = (
    "empirical_finding",
    "author_interpretation",
    "author_proposed_gap",
    "reviewer_inference",
)
RELATION_DECISIONS = (
    "supported",
    "contradicted",
    "insufficient_evidence",
    "not_comparable",
)


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


def _text_list(value: Any) -> List[str]:
    """Normalize one scalar-or-sequence field without splitting strings."""

    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return _stable_unique([value])
    if isinstance(value, Sequence):
        return _stable_unique(value)
    return _stable_unique([value])


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


@dataclass(frozen=True)
class OutlineQualityGate:
    """Typed adoption gate shared by coverage, stability, and health.

    The thresholds are persisted in every downstream binding.  Changing a
    gate therefore invalidates the audits and health decision instead of
    silently reusing a result calculated under another policy.
    """

    coverage_scope: str = "full"
    min_canonical_coverage_full: float = 1.0
    min_canonical_coverage_local: float = 1.0
    min_effective_sections: int = 1
    max_duplicate_assignments: int = 0
    block_placeholder_sections: bool = True
    block_empty_research_streams: bool = True

    def __post_init__(self) -> None:
        if self.coverage_scope not in {"full", "local"}:
            raise ValueError("coverage_scope must be 'full' or 'local'")
        for name in ("min_canonical_coverage_full", "min_canonical_coverage_local"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
            object.__setattr__(self, name, value)
        if int(self.min_effective_sections) < 1:
            raise ValueError("min_effective_sections must be positive")
        if int(self.max_duplicate_assignments) < 0:
            raise ValueError("max_duplicate_assignments must be non-negative")
        object.__setattr__(self, "min_effective_sections", int(self.min_effective_sections))
        object.__setattr__(self, "max_duplicate_assignments", int(self.max_duplicate_assignments))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "coverage_scope": self.coverage_scope,
            "min_canonical_coverage_full": self.min_canonical_coverage_full,
            "min_canonical_coverage_local": self.min_canonical_coverage_local,
            "min_effective_sections": self.min_effective_sections,
            "max_duplicate_assignments": self.max_duplicate_assignments,
            "block_placeholder_sections": self.block_placeholder_sections,
            "block_empty_research_streams": self.block_empty_research_streams,
        }

    @property
    def content_hash(self) -> str:
        return compute_v3_hash(self.to_dict())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "OutlineQualityGate":
        source = value or {}
        return cls(
            coverage_scope=str(source.get("coverage_scope") or "full"),
            min_canonical_coverage_full=float(source.get("min_canonical_coverage_full", 1.0)),
            min_canonical_coverage_local=float(source.get("min_canonical_coverage_local", 1.0)),
            min_effective_sections=int(source.get("min_effective_sections", 1)),
            max_duplicate_assignments=int(source.get("max_duplicate_assignments", 0)),
            block_placeholder_sections=bool(source.get("block_placeholder_sections", True)),
            block_empty_research_streams=bool(source.get("block_empty_research_streams", True)),
        )


@dataclass(frozen=True)
class RelationCandidate:
    """A deterministic, evidence-linked relation candidate between papers."""

    relation_id: str
    relation_type: str
    paper_keys: List[str] = field(default_factory=list)
    dimension: str = ""
    evidence_fields: Dict[str, List[str]] = field(default_factory=dict)
    confidence: str = "low"
    source_fields: Dict[str, List[str]] = field(default_factory=dict)
    supporting_labels: List[str] = field(default_factory=list)
    source_paper_key: str = ""
    target_paper_key: str = ""
    diagnostics: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        keys = _stable_unique(self.paper_keys)
        source_key = self.source_paper_key or (keys[0] if keys else "")
        target_key = self.target_paper_key or (keys[1] if len(keys) > 1 else "")
        return {
            "relation_id": self.relation_id,
            "relation_type": self.relation_type,
            "paper_keys": keys,
            "source_paper_key": source_key,
            "target_paper_key": target_key,
            "dimension": self.dimension,
            "evidence_fields": _stable_mapping({
                key: _stable_unique(value)
                for key, value in self.evidence_fields.items()
            }),
            "confidence": self.confidence,
            "source_fields": _stable_mapping({
                key: _stable_unique(value)
                for key, value in self.source_fields.items()
            }),
            "supporting_labels": _stable_unique(self.supporting_labels),
            "diagnostics": _stable_unique(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RelationCandidate":
        def field_map(value: Any) -> Dict[str, List[str]]:
            return {
                str(key): _stable_unique(
                    item if isinstance(item, Sequence) and not isinstance(item, (str, bytes)) else [item]
                )
                for key, item in _stable_mapping(value).items()
            }

        keys = _stable_unique(data.get("paper_keys") or [])
        return cls(
            relation_id=str(data.get("relation_id") or ""),
            relation_type=str(data.get("relation_type") or ""),
            paper_keys=keys,
            source_paper_key=str(data.get("source_paper_key") or (keys[0] if keys else "")),
            target_paper_key=str(data.get("target_paper_key") or (keys[1] if len(keys) > 1 else "")),
            dimension=str(data.get("dimension") or ""),
            evidence_fields=field_map(data.get("evidence_fields")),
            confidence=str(data.get("confidence") or "low"),
            source_fields=field_map(data.get("source_fields")),
            supporting_labels=_stable_unique(data.get("supporting_labels") or []),
            diagnostics=_stable_unique(data.get("diagnostics") or []),
        )


@dataclass(frozen=True)
class GlobalRelationMap:
    artifact_type: str = "global_relation_map"
    artifact_version: str = OUTLINE_V3_VERSION
    relations: List[RelationCandidate] = field(default_factory=list)
    paper_keys: List[str] = field(default_factory=list)
    source_artifact_hashes: Dict[str, str] = field(default_factory=dict)
    blocking_diagnostics: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def relation_candidates(self) -> List[RelationCandidate]:
        return self.relations

    def canonical_payload(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "relations": [
                relation.to_dict()
                for relation in sorted(self.relations, key=lambda item: item.relation_id)
            ],
            "paper_keys": _stable_unique(self.paper_keys),
            "source_artifact_hashes": {
                str(key): str(value)
                for key, value in sorted(self.source_artifact_hashes.items(), key=lambda item: str(item[0]))
            },
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
    def from_dict(cls, data: Mapping[str, Any]) -> "GlobalRelationMap":
        return cls(
            artifact_type=str(data.get("artifact_type") or "global_relation_map"),
            artifact_version=str(data.get("artifact_version") or OUTLINE_V3_VERSION),
            relations=[RelationCandidate.from_dict(item) for item in data.get("relations", []) if isinstance(item, Mapping)],
            paper_keys=_stable_unique(data.get("paper_keys") or []),
            source_artifact_hashes={
                str(key): str(value)
                for key, value in _stable_mapping(data.get("source_artifact_hashes")).items()
            },
            blocking_diagnostics=_list_of_dicts(data.get("blocking_diagnostics")),
        )


@dataclass(frozen=True)
class OrganizingAxis:
    axis_id: str
    organizing_logic: str
    label: str
    rationale: str = ""
    preferred_dimensions: List[str] = field(default_factory=list)
    preferred_relation_types: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "axis_id": self.axis_id,
            "organizing_logic": self.organizing_logic,
            "label": self.label,
            "rationale": self.rationale,
            "preferred_dimensions": _stable_unique(self.preferred_dimensions),
            "preferred_relation_types": _stable_unique(self.preferred_relation_types),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OrganizingAxis":
        return cls(
            axis_id=str(data.get("axis_id") or ""),
            organizing_logic=str(data.get("organizing_logic") or ""),
            label=str(data.get("label") or ""),
            rationale=str(data.get("rationale") or ""),
            preferred_dimensions=_stable_unique(data.get("preferred_dimensions") or []),
            preferred_relation_types=_stable_unique(data.get("preferred_relation_types") or []),
        )


@dataclass(frozen=True)
class OutlineCandidatePlan:
    candidate_id: str
    organizing_logic: str
    axis_id: str
    shared_artifact_hashes: Dict[str, str] = field(default_factory=dict)
    required_node_ids: List[str] = field(default_factory=list)
    provider_generation_node_id: str = ""
    status: str = "planned"
    diagnostics: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "organizing_logic": self.organizing_logic,
            "axis_id": self.axis_id,
            "shared_artifact_hashes": {
                str(key): str(value)
                for key, value in sorted(self.shared_artifact_hashes.items(), key=lambda item: str(item[0]))
            },
            "required_node_ids": _stable_unique(self.required_node_ids),
            "provider_generation_node_id": self.provider_generation_node_id,
            "provider_generation_is_separate": True,
            "status": self.status,
            "diagnostics": _stable_unique(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OutlineCandidatePlan":
        return cls(
            candidate_id=str(data.get("candidate_id") or ""),
            organizing_logic=str(data.get("organizing_logic") or ""),
            axis_id=str(data.get("axis_id") or ""),
            shared_artifact_hashes={
                str(key): str(value)
                for key, value in _stable_mapping(data.get("shared_artifact_hashes")).items()
            },
            required_node_ids=_stable_unique(data.get("required_node_ids") or []),
            provider_generation_node_id=str(data.get("provider_generation_node_id") or ""),
            status=str(data.get("status") or "planned"),
            diagnostics=_stable_unique(data.get("diagnostics") or []),
        )


@dataclass(frozen=True)
class OutlineCandidatePlans:
    artifact_type: str = "organizing_axes_and_candidate_plans"
    artifact_version: str = OUTLINE_V3_VERSION
    axes: List[OrganizingAxis] = field(default_factory=list)
    candidates: List[OutlineCandidatePlan] = field(default_factory=list)
    shared_artifact_hashes: Dict[str, str] = field(default_factory=dict)
    review_intent_hash: str = ""
    coverage_contract_hash: str = ""
    blocking_diagnostics: List[Dict[str, Any]] = field(default_factory=list)

    def canonical_payload(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "axes": [axis.to_dict() for axis in sorted(self.axes, key=lambda item: item.axis_id)],
            "candidates": [
                candidate.to_dict()
                for candidate in sorted(self.candidates, key=lambda item: item.candidate_id)
            ],
            "shared_artifact_hashes": {
                str(key): str(value)
                for key, value in sorted(self.shared_artifact_hashes.items(), key=lambda item: str(item[0]))
            },
            "review_intent_hash": self.review_intent_hash,
            "coverage_contract_hash": self.coverage_contract_hash,
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
    def from_dict(cls, data: Mapping[str, Any]) -> "OutlineCandidatePlans":
        return cls(
            artifact_type=str(data.get("artifact_type") or "organizing_axes_and_candidate_plans"),
            artifact_version=str(data.get("artifact_version") or OUTLINE_V3_VERSION),
            axes=[OrganizingAxis.from_dict(item) for item in data.get("axes", []) if isinstance(item, Mapping)],
            candidates=[OutlineCandidatePlan.from_dict(item) for item in data.get("candidates", []) if isinstance(item, Mapping)],
            shared_artifact_hashes={
                str(key): str(value)
                for key, value in _stable_mapping(data.get("shared_artifact_hashes")).items()
            },
            review_intent_hash=str(data.get("review_intent_hash") or ""),
            coverage_contract_hash=str(data.get("coverage_contract_hash") or ""),
            blocking_diagnostics=_list_of_dicts(data.get("blocking_diagnostics")),
        )


@dataclass(frozen=True)
class EvidenceClaim:
    """One source-linked claim in a paper evidence dossier.

    The claim type is deliberately modal.  A finding reported by the paper,
    the paper authors' interpretation, a gap proposed by the authors, and a
    later reviewer inference are not interchangeable evidence.
    """

    claim_id: str
    claim_type: str
    text: str
    study_id: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    source_locator: str = ""
    source_summary_hash: str = ""

    def __post_init__(self) -> None:
        if not self.claim_id.strip():
            raise ValueError("EvidenceClaim.claim_id is required")
        if self.claim_type not in EVIDENCE_CLAIM_TYPES:
            raise ValueError(f"unsupported evidence claim type: {self.claim_type!r}")
        if not self.text.strip():
            raise ValueError("EvidenceClaim.text is required")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "claim_type": self.claim_type,
            "text": self.text,
            "study_id": self.study_id,
            "evidence_ids": _stable_unique(self.evidence_ids),
            "source_locator": self.source_locator,
            "source_summary_hash": self.source_summary_hash,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceClaim":
        return cls(
            claim_id=str(data.get("claim_id") or ""),
            claim_type=str(data.get("claim_type") or ""),
            text=str(data.get("text") or ""),
            study_id=str(data.get("study_id") or ""),
            evidence_ids=_text_list(data.get("evidence_ids")),
            source_locator=str(data.get("source_locator") or ""),
            source_summary_hash=str(data.get("source_summary_hash") or ""),
        )


@dataclass(frozen=True)
class ResearchUnit:
    """A complete study/argument unit within one paper.

    A unit is the smallest legal split point for a long or multi-study paper;
    its findings, conditions, and claims travel together.
    """

    study_id: str
    parent_paper_id: str
    research_questions: List[str] = field(default_factory=list)
    definitions_and_operationalizations: Dict[str, List[str]] = field(default_factory=dict)
    theoretical_derivation: List[str] = field(default_factory=list)
    method: List[str] = field(default_factory=list)
    sample_or_context: List[str] = field(default_factory=list)
    findings: List[str] = field(default_factory=list)
    mechanisms: List[str] = field(default_factory=list)
    moderators_or_boundaries: List[str] = field(default_factory=list)
    zero_results: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    claims: List[EvidenceClaim] = field(default_factory=list)
    source_locators: Dict[str, List[str]] = field(default_factory=dict)
    evidence_ids: List[str] = field(default_factory=list)
    source_summary_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "study_id": self.study_id,
            "parent_paper_id": self.parent_paper_id,
            "research_questions": _stable_unique(self.research_questions),
            "definitions_and_operationalizations": _stable_mapping({
                key: _text_list(value)
                for key, value in self.definitions_and_operationalizations.items()
            }),
            "theoretical_derivation": _stable_unique(self.theoretical_derivation),
            "method": _stable_unique(self.method),
            "sample_or_context": _stable_unique(self.sample_or_context),
            "findings": _stable_unique(self.findings),
            "mechanisms": _stable_unique(self.mechanisms),
            "moderators_or_boundaries": _stable_unique(self.moderators_or_boundaries),
            "zero_results": _stable_unique(self.zero_results),
            "limitations": _stable_unique(self.limitations),
            "claims": [claim.to_dict() for claim in sorted(self.claims, key=lambda item: item.claim_id)],
            "source_locators": _stable_mapping({
                key: _text_list(value)
                for key, value in self.source_locators.items()
            }),
            "evidence_ids": _stable_unique(self.evidence_ids),
            "source_summary_hash": self.source_summary_hash,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResearchUnit":
        return cls(
            study_id=str(data.get("study_id") or ""),
            parent_paper_id=str(data.get("parent_paper_id") or ""),
            research_questions=_text_list(data.get("research_questions")),
            definitions_and_operationalizations={
                str(key): _text_list(value)
                for key, value in _stable_mapping(data.get("definitions_and_operationalizations")).items()
            },
            theoretical_derivation=_text_list(data.get("theoretical_derivation")),
            method=_text_list(data.get("method")),
            sample_or_context=_text_list(data.get("sample_or_context")),
            findings=_text_list(data.get("findings")),
            mechanisms=_text_list(data.get("mechanisms")),
            moderators_or_boundaries=_text_list(data.get("moderators_or_boundaries")),
            zero_results=_text_list(data.get("zero_results")),
            limitations=_text_list(data.get("limitations")),
            claims=[EvidenceClaim.from_dict(item) for item in data.get("claims", []) if isinstance(item, Mapping)],
            source_locators={
                str(key): _text_list(value)
                for key, value in _stable_mapping(data.get("source_locators")).items()
            },
            evidence_ids=_text_list(data.get("evidence_ids")),
            source_summary_hash=str(data.get("source_summary_hash") or ""),
        )


@dataclass(frozen=True)
class PaperIndexCard:
    """Compact whole-paper navigation record; not a substitute for a dossier."""

    paper_id: str
    research_questions: List[str] = field(default_factory=list)
    key_constructs: List[str] = field(default_factory=list)
    core_findings: List[str] = field(default_factory=list)
    key_boundaries: List[str] = field(default_factory=list)
    method_category: str = ""
    topic_tags: List[str] = field(default_factory=list)
    evidence_package_id: str = ""
    source_summary_hash: str = ""
    source_locators: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "research_questions": _stable_unique(self.research_questions),
            "key_constructs": _stable_unique(self.key_constructs),
            "core_findings": _stable_unique(self.core_findings),
            "key_boundaries": _stable_unique(self.key_boundaries),
            "method_category": self.method_category,
            "topic_tags": _stable_unique(self.topic_tags),
            "evidence_package_id": self.evidence_package_id,
            "source_summary_hash": self.source_summary_hash,
            "source_locators": _stable_unique(self.source_locators),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PaperIndexCard":
        return cls(
            paper_id=str(data.get("paper_id") or ""),
            research_questions=_text_list(data.get("research_questions")),
            key_constructs=_text_list(data.get("key_constructs")),
            core_findings=_text_list(data.get("core_findings")),
            key_boundaries=_text_list(data.get("key_boundaries")),
            method_category=str(data.get("method_category") or ""),
            topic_tags=_text_list(data.get("topic_tags")),
            evidence_package_id=str(data.get("evidence_package_id") or ""),
            source_summary_hash=str(data.get("source_summary_hash") or ""),
            source_locators=_text_list(data.get("source_locators")),
        )


@dataclass(frozen=True)
class PaperEvidenceDossier:
    """Logical-complete paper/study evidence package.

    Length is not a truncation contract.  When a source is incomplete, the
    dossier records the missing field instead of treating omitted material as
    a negative finding.
    """

    dossier_id: str
    paper_id: str
    source_summary_hash: str = ""
    overall_context: List[str] = field(default_factory=list)
    research_questions: List[str] = field(default_factory=list)
    concept_definitions: List[str] = field(default_factory=list)
    operationalizations: List[str] = field(default_factory=list)
    theoretical_derivation: List[str] = field(default_factory=list)
    findings: List[str] = field(default_factory=list)
    research_units: List[ResearchUnit] = field(default_factory=list)
    claims: List[EvidenceClaim] = field(default_factory=list)
    mechanism_evidence: List[str] = field(default_factory=list)
    moderators_boundaries: List[str] = field(default_factory=list)
    zero_results: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    source_locators: Dict[str, List[str]] = field(default_factory=dict)
    evidence_ids_by_field: Dict[str, List[str]] = field(default_factory=dict)
    evidence_text_by_id: Dict[str, str] = field(default_factory=dict)
    diagnostics: List[str] = field(default_factory=list)
    status: str = "ready"

    @property
    def evidence_ids(self) -> List[str]:
        return _stable_unique(
            value
            for values in self.evidence_ids_by_field.values()
            for value in values
        )

    def canonical_payload(self) -> Dict[str, Any]:
        return {
            "dossier_id": self.dossier_id,
            "paper_id": self.paper_id,
            "source_summary_hash": self.source_summary_hash,
            "overall_context": _stable_unique(self.overall_context),
            "research_questions": _stable_unique(self.research_questions),
            "concept_definitions": _stable_unique(self.concept_definitions),
            "operationalizations": _stable_unique(self.operationalizations),
            "theoretical_derivation": _stable_unique(self.theoretical_derivation),
            "findings": _stable_unique(self.findings),
            "research_units": [unit.to_dict() for unit in sorted(self.research_units, key=lambda item: item.study_id)],
            "claims": [claim.to_dict() for claim in sorted(self.claims, key=lambda item: item.claim_id)],
            "mechanism_evidence": _stable_unique(self.mechanism_evidence),
            "moderators_boundaries": _stable_unique(self.moderators_boundaries),
            "zero_results": _stable_unique(self.zero_results),
            "limitations": _stable_unique(self.limitations),
            "source_locators": _stable_mapping({
                key: _text_list(value)
                for key, value in self.source_locators.items()
            }),
            "evidence_ids_by_field": _stable_mapping({
                key: _stable_unique(value)
                for key, value in self.evidence_ids_by_field.items()
            }),
            "evidence_text_by_id": {
                str(key): str(value)
                for key, value in sorted(self.evidence_text_by_id.items(), key=lambda item: str(item[0]))
            },
            "diagnostics": _stable_unique(self.diagnostics),
            "status": self.status,
        }

    @property
    def content_hash(self) -> str:
        return compute_v3_hash(self.canonical_payload())

    def to_dict(self) -> Dict[str, Any]:
        payload = self.canonical_payload()
        payload["evidence_ids"] = self.evidence_ids
        payload["content_hash"] = self.content_hash
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PaperEvidenceDossier":
        return cls(
            dossier_id=str(data.get("dossier_id") or ""),
            paper_id=str(data.get("paper_id") or ""),
            source_summary_hash=str(data.get("source_summary_hash") or ""),
            overall_context=_text_list(data.get("overall_context")),
            research_questions=_text_list(data.get("research_questions")),
            concept_definitions=_text_list(data.get("concept_definitions")),
            operationalizations=_text_list(data.get("operationalizations")),
            theoretical_derivation=_text_list(data.get("theoretical_derivation")),
            findings=_text_list(data.get("findings")),
            research_units=[ResearchUnit.from_dict(item) for item in data.get("research_units", []) if isinstance(item, Mapping)],
            claims=[EvidenceClaim.from_dict(item) for item in data.get("claims", []) if isinstance(item, Mapping)],
            mechanism_evidence=_text_list(data.get("mechanism_evidence")),
            moderators_boundaries=_text_list(data.get("moderators_boundaries")),
            zero_results=_text_list(data.get("zero_results")),
            limitations=_text_list(data.get("limitations")),
            source_locators={
                str(key): _text_list(value)
                for key, value in _stable_mapping(data.get("source_locators")).items()
            },
            evidence_ids_by_field={
                str(key): _text_list(value)
                for key, value in _stable_mapping(data.get("evidence_ids_by_field")).items()
            },
            evidence_text_by_id={
                str(key): str(value)
                for key, value in _stable_mapping(data.get("evidence_text_by_id")).items()
            },
            diagnostics=_text_list(data.get("diagnostics")),
            status=str(data.get("status") or "ready"),
        )


@dataclass(frozen=True)
class PaperContentLayers:
    """Shared, content-addressed navigation cards and evidence dossiers."""

    artifact_type: str = CONTENT_LAYERS_ARTIFACT_TYPE
    artifact_version: str = OUTLINE_V3_VERSION
    index_cards: List[PaperIndexCard] = field(default_factory=list)
    dossiers: List[PaperEvidenceDossier] = field(default_factory=list)
    source_summary_hashes: List[str] = field(default_factory=list)
    blocking_diagnostics: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def dossier_by_paper(self) -> Dict[str, PaperEvidenceDossier]:
        return {item.paper_id: item for item in self.dossiers}

    def canonical_payload(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "index_cards": [item.to_dict() for item in sorted(self.index_cards, key=lambda item: item.paper_id)],
            "dossiers": [item.to_dict() for item in sorted(self.dossiers, key=lambda item: item.paper_id)],
            "source_summary_hashes": _stable_unique(self.source_summary_hashes),
            "blocking_diagnostics": _list_of_dicts(sorted(self.blocking_diagnostics, key=compute_v3_hash)),
        }

    @property
    def content_hash(self) -> str:
        return compute_v3_hash(self.canonical_payload())

    @property
    def status(self) -> str:
        return "blocked" if self.blocking_diagnostics else "ready"

    def to_dict(self) -> Dict[str, Any]:
        payload = self.canonical_payload()
        payload.update({"status": self.status, "content_hash": self.content_hash})
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PaperContentLayers":
        return cls(
            artifact_type=str(data.get("artifact_type") or CONTENT_LAYERS_ARTIFACT_TYPE),
            artifact_version=str(data.get("artifact_version") or OUTLINE_V3_VERSION),
            index_cards=[PaperIndexCard.from_dict(item) for item in data.get("index_cards", []) if isinstance(item, Mapping)],
            dossiers=[PaperEvidenceDossier.from_dict(item) for item in data.get("dossiers", []) if isinstance(item, Mapping)],
            source_summary_hashes=_text_list(data.get("source_summary_hashes")),
            blocking_diagnostics=_list_of_dicts(data.get("blocking_diagnostics")),
        )


@dataclass(frozen=True)
class RelationEvidenceBundle:
    """Evidence-complete input for one directional relation adjudication."""

    relation_id: str
    comparison_question: str = ""
    relation_type: str = ""
    paper_ids: List[str] = field(default_factory=list)
    study_ids: List[str] = field(default_factory=list)
    claim_ids_left: List[str] = field(default_factory=list)
    claim_ids_right: List[str] = field(default_factory=list)
    definitions_and_operationalizations: Dict[str, List[str]] = field(default_factory=dict)
    findings_left: List[str] = field(default_factory=list)
    findings_right: List[str] = field(default_factory=list)
    contexts_and_boundaries: Dict[str, List[str]] = field(default_factory=dict)
    source_locators: Dict[str, List[str]] = field(default_factory=dict)
    required_evidence_ids: List[str] = field(default_factory=list)
    provided_evidence_ids: List[str] = field(default_factory=list)
    missing_evidence_ids: List[str] = field(default_factory=list)
    evidence_completeness: str = "incomplete"
    decision: str = "insufficient_evidence"
    diagnostics: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.relation_id.strip():
            raise ValueError("RelationEvidenceBundle.relation_id is required")
        if self.decision not in RELATION_DECISIONS:
            raise ValueError(f"unsupported relation decision: {self.decision!r}")
        required = set(_stable_unique(self.required_evidence_ids))
        provided = set(_stable_unique(self.provided_evidence_ids))
        explicit_missing = set(_stable_unique(self.missing_evidence_ids))
        missing = sorted((required - provided) | explicit_missing)
        object.__setattr__(self, "required_evidence_ids", _stable_unique(required))
        object.__setattr__(self, "provided_evidence_ids", _stable_unique(provided))
        object.__setattr__(self, "missing_evidence_ids", _stable_unique(missing))
        if missing:
            object.__setattr__(self, "evidence_completeness", "incomplete")
            # A relation with missing finding/boundary evidence can never be
            # promoted to a substantive positive or negative judgment.
            if self.decision in {"supported", "contradicted"}:
                object.__setattr__(self, "decision", "insufficient_evidence")
        elif self.diagnostics and any(
            marker in " ".join(self.diagnostics).casefold()
            for marker in ("missing", "not_inspected", "dossier")
        ):
            object.__setattr__(self, "evidence_completeness", "incomplete")
        else:
            object.__setattr__(self, "evidence_completeness", "complete")

    @property
    def is_complete(self) -> bool:
        return self.evidence_completeness == "complete" and not self.missing_evidence_ids

    @property
    def completeness_ratio(self) -> float:
        required = set(self.required_evidence_ids)
        if not required:
            return 1.0
        return len(required & set(self.provided_evidence_ids)) / len(required)

    def with_decision(self, decision: str, *, diagnostics: Sequence[str] = ()) -> "RelationEvidenceBundle":
        next_decision = str(decision or "").strip()
        if next_decision not in RELATION_DECISIONS:
            raise ValueError(f"unsupported relation decision: {next_decision!r}")
        if not self.is_complete and next_decision in {"supported", "contradicted"}:
            next_decision = "insufficient_evidence"
        return replace_dataclass(self, decision=next_decision, diagnostics=[*self.diagnostics, *diagnostics])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "relation_id": self.relation_id,
            "comparison_question": self.comparison_question,
            "relation_type": self.relation_type,
            "paper_ids": _stable_unique(self.paper_ids),
            "study_ids": _stable_unique(self.study_ids),
            "claim_ids_left": _stable_unique(self.claim_ids_left),
            "claim_ids_right": _stable_unique(self.claim_ids_right),
            "definitions_and_operationalizations": _stable_mapping({
                key: _text_list(value)
                for key, value in self.definitions_and_operationalizations.items()
            }),
            "findings_left": _stable_unique(self.findings_left),
            "findings_right": _stable_unique(self.findings_right),
            "contexts_and_boundaries": _stable_mapping({
                key: _text_list(value)
                for key, value in self.contexts_and_boundaries.items()
            }),
            "source_locators": _stable_mapping({
                key: _text_list(value)
                for key, value in self.source_locators.items()
            }),
            "required_evidence_ids": _stable_unique(self.required_evidence_ids),
            "provided_evidence_ids": _stable_unique(self.provided_evidence_ids),
            "missing_evidence_ids": _stable_unique(self.missing_evidence_ids),
            "evidence_completeness": self.evidence_completeness,
            "evidence_complete": self.is_complete,
            "completeness_ratio": self.completeness_ratio,
            "decision": self.decision,
            "diagnostics": _stable_unique(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RelationEvidenceBundle":
        return cls(
            relation_id=str(data.get("relation_id") or ""),
            comparison_question=str(data.get("comparison_question") or ""),
            relation_type=str(data.get("relation_type") or ""),
            paper_ids=_text_list(data.get("paper_ids")),
            study_ids=_text_list(data.get("study_ids")),
            claim_ids_left=_text_list(data.get("claim_ids_left")),
            claim_ids_right=_text_list(data.get("claim_ids_right")),
            definitions_and_operationalizations={
                str(key): _text_list(value)
                for key, value in _stable_mapping(data.get("definitions_and_operationalizations")).items()
            },
            findings_left=_text_list(data.get("findings_left")),
            findings_right=_text_list(data.get("findings_right")),
            contexts_and_boundaries={
                str(key): _text_list(value)
                for key, value in _stable_mapping(data.get("contexts_and_boundaries")).items()
            },
            source_locators={
                str(key): _text_list(value)
                for key, value in _stable_mapping(data.get("source_locators")).items()
            },
            required_evidence_ids=_text_list(data.get("required_evidence_ids")),
            provided_evidence_ids=_text_list(data.get("provided_evidence_ids")),
            missing_evidence_ids=_text_list(data.get("missing_evidence_ids")),
            evidence_completeness=str(data.get("evidence_completeness") or "incomplete"),
            decision=str(data.get("decision") or "insufficient_evidence"),
            diagnostics=_text_list(data.get("diagnostics")),
        )


def replace_dataclass(value: Any, **changes: Any) -> Any:
    """Small local wrapper to keep model construction readable."""

    from dataclasses import replace

    return replace(value, **changes)


@dataclass(frozen=True)
class TopicSynthesis:
    """Shared group-level synthesis or its explicit not-yet-run plan."""

    topic_id: str
    question: str = ""
    paper_ids: List[str] = field(default_factory=list)
    bridge_paper_ids: List[str] = field(default_factory=list)
    relation_ids: List[str] = field(default_factory=list)
    conclusions: List[str] = field(default_factory=list)
    supporting_evidence_ids: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    comparability_notes: List[str] = field(default_factory=list)
    unresolved_questions: List[str] = field(default_factory=list)
    status: str = "planned"
    diagnostics: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "question": self.question,
            "paper_ids": _stable_unique(self.paper_ids),
            "bridge_paper_ids": _stable_unique(self.bridge_paper_ids),
            "relation_ids": _stable_unique(self.relation_ids),
            "conclusions": _stable_unique(self.conclusions),
            "supporting_evidence_ids": _stable_unique(self.supporting_evidence_ids),
            "conflicts": _stable_unique(self.conflicts),
            "comparability_notes": _stable_unique(self.comparability_notes),
            "unresolved_questions": _stable_unique(self.unresolved_questions),
            "status": self.status,
            "diagnostics": _stable_unique(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TopicSynthesis":
        return cls(
            topic_id=str(data.get("topic_id") or ""),
            question=str(data.get("question") or ""),
            paper_ids=_text_list(data.get("paper_ids")),
            bridge_paper_ids=_text_list(data.get("bridge_paper_ids")),
            relation_ids=_text_list(data.get("relation_ids")),
            conclusions=_text_list(data.get("conclusions")),
            supporting_evidence_ids=_text_list(data.get("supporting_evidence_ids")),
            conflicts=_text_list(data.get("conflicts")),
            comparability_notes=_text_list(data.get("comparability_notes")),
            unresolved_questions=_text_list(data.get("unresolved_questions")),
            status=str(data.get("status") or "planned"),
            diagnostics=_text_list(data.get("diagnostics")),
        )


@dataclass(frozen=True)
class GlobalSynthesis:
    """Cross-topic synthesis base shared by all outline candidates."""

    artifact_type: str = GLOBAL_SYNTHESIS_ARTIFACT_TYPE
    artifact_version: str = OUTLINE_V3_VERSION
    topic_ids: List[str] = field(default_factory=list)
    cross_group_comparison_questions: List[str] = field(default_factory=list)
    relation_ids: List[str] = field(default_factory=list)
    coverage_matrix: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    conclusions: List[str] = field(default_factory=list)
    alternative_explanations: List[str] = field(default_factory=list)
    unresolved_questions: List[str] = field(default_factory=list)
    supporting_evidence_ids: List[str] = field(default_factory=list)
    status: str = "planned"
    blocking_diagnostics: List[Dict[str, Any]] = field(default_factory=list)

    def canonical_payload(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "topic_ids": _stable_unique(self.topic_ids),
            "cross_group_comparison_questions": _stable_unique(self.cross_group_comparison_questions),
            "relation_ids": _stable_unique(self.relation_ids),
            "coverage_matrix": _stable_mapping({
                str(key): dict(value) if isinstance(value, Mapping) else {}
                for key, value in self.coverage_matrix.items()
            }),
            "conclusions": _stable_unique(self.conclusions),
            "alternative_explanations": _stable_unique(self.alternative_explanations),
            "unresolved_questions": _stable_unique(self.unresolved_questions),
            "supporting_evidence_ids": _stable_unique(self.supporting_evidence_ids),
            "status": self.status,
            "blocking_diagnostics": _list_of_dicts(self.blocking_diagnostics),
        }

    @property
    def content_hash(self) -> str:
        return compute_v3_hash(self.canonical_payload())

    def to_dict(self) -> Dict[str, Any]:
        payload = self.canonical_payload()
        payload["content_hash"] = self.content_hash
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GlobalSynthesis":
        return cls(
            artifact_type=str(data.get("artifact_type") or GLOBAL_SYNTHESIS_ARTIFACT_TYPE),
            artifact_version=str(data.get("artifact_version") or OUTLINE_V3_VERSION),
            topic_ids=_text_list(data.get("topic_ids")),
            cross_group_comparison_questions=_text_list(data.get("cross_group_comparison_questions")),
            relation_ids=_text_list(data.get("relation_ids")),
            coverage_matrix={
                str(key): dict(value) if isinstance(value, Mapping) else {}
                for key, value in _stable_mapping(data.get("coverage_matrix")).items()
            },
            conclusions=_text_list(data.get("conclusions")),
            alternative_explanations=_text_list(data.get("alternative_explanations")),
            unresolved_questions=_text_list(data.get("unresolved_questions")),
            supporting_evidence_ids=_text_list(data.get("supporting_evidence_ids")),
            status=str(data.get("status") or "planned"),
            blocking_diagnostics=_list_of_dicts(data.get("blocking_diagnostics")),
        )


# Names without the explicit version suffix are the public v3 vocabulary.
OutlineEvidenceViewV1 = OutlineEvidenceView
OutlineEvidenceViewsV1 = OutlineEvidenceViews
GlobalCorpusLedgerV1 = GlobalCorpusLedger
MultiViewMatrixV1 = MultiViewMatrix
ReviewIntentV1 = ReviewIntent
CoverageContractV1 = CoverageContract
OutlineQualityGateV1 = OutlineQualityGate
RelationCandidateV1 = RelationCandidate
GlobalRelationMapV1 = GlobalRelationMap
OrganizingAxisV1 = OrganizingAxis
OutlineCandidatePlanV1 = OutlineCandidatePlan
OutlineCandidatePlansV1 = OutlineCandidatePlans
EvidenceClaimV1 = EvidenceClaim
ResearchUnitV1 = ResearchUnit
PaperIndexCardV1 = PaperIndexCard
PaperEvidenceDossierV1 = PaperEvidenceDossier
PaperContentLayersV1 = PaperContentLayers
RelationEvidenceBundleV1 = RelationEvidenceBundle
TopicSynthesisV1 = TopicSynthesis
GlobalSynthesisV1 = GlobalSynthesis


__all__ = [
    "OUTLINE_V3_VERSION",
    "EVIDENCE_VIEWS_ARTIFACT_TYPE",
    "GLOBAL_CORPUS_LEDGER_ARTIFACT_TYPE",
    "MULTI_VIEW_MATRIX_ARTIFACT_TYPE",
    "REVIEW_INTENT_ARTIFACT_TYPE",
    "COVERAGE_CONTRACT_ARTIFACT_TYPE",
    "CONTENT_LAYERS_ARTIFACT_TYPE",
    "RELATION_EVIDENCE_BUNDLES_ARTIFACT_TYPE",
    "TOPIC_SYNTHESIS_ARTIFACT_TYPE",
    "GLOBAL_SYNTHESIS_ARTIFACT_TYPE",
    "EVIDENCE_CLAIM_TYPES",
    "RELATION_DECISIONS",
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
    "OutlineQualityGate",
    "OutlineEvidenceViewV1",
    "OutlineEvidenceViewsV1",
    "GlobalCorpusLedgerV1",
    "MultiViewMatrixV1",
    "ReviewIntentV1",
    "CoverageContractV1",
    "OutlineQualityGateV1",
    "RelationCandidate",
    "GlobalRelationMap",
    "OrganizingAxis",
    "OutlineCandidatePlan",
    "OutlineCandidatePlans",
    "RelationCandidateV1",
    "GlobalRelationMapV1",
    "OrganizingAxisV1",
    "OutlineCandidatePlanV1",
    "OutlineCandidatePlansV1",
    "EvidenceClaim",
    "ResearchUnit",
    "PaperIndexCard",
    "PaperEvidenceDossier",
    "PaperContentLayers",
    "RelationEvidenceBundle",
    "TopicSynthesis",
    "GlobalSynthesis",
    "EvidenceClaimV1",
    "ResearchUnitV1",
    "PaperIndexCardV1",
    "PaperEvidenceDossierV1",
    "PaperContentLayersV1",
    "RelationEvidenceBundleV1",
    "TopicSynthesisV1",
    "GlobalSynthesisV1",
]

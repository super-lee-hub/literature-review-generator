from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from services.citation_catalog import (
    augment_citation_catalog_from_literature_map,
    build_citation_catalog,
    extract_doi_aliases,
    extract_citation_key,
    format_reference_entry,
)
from services.citation_ref_catalog import extract_ref_ids_from_token, resolve_ref_id
from services.job_workspace import utc_now_iso
from services.sentence_segmenter import segment_sentences


@dataclass(frozen=True)
class CitationSpan:
    span_id: str
    start_offset: int
    end_offset: int
    text: str
    anchor_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CitationSpan":
        return cls(**data)


@dataclass(frozen=True)
class CitationOccurrence:
    occurrence_id: str
    citation_token: str
    paper_id: str
    paper_key: str
    section_number: int
    section_title: str
    block_id: str
    block_order: int
    ref_id: str = ""
    canonical_paper_key: str = ""
    source_type: str = "structured_ref"
    spans: List[CitationSpan] = field(default_factory=list)
    context_before: str = ""
    context_after: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "occurrence_id": self.occurrence_id,
            "citation_token": self.citation_token,
            "paper_id": self.paper_id,
            "paper_key": self.paper_key,
            "section_number": self.section_number,
            "section_title": self.section_title,
            "block_id": self.block_id,
            "block_order": self.block_order,
            "ref_id": self.ref_id,
            "canonical_paper_key": self.canonical_paper_key,
            "source_type": self.source_type,
            "spans": [span.to_dict() for span in self.spans],
            "context_before": self.context_before,
            "context_after": self.context_after,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CitationOccurrence":
        return cls(
            occurrence_id=data["occurrence_id"],
            citation_token=data["citation_token"],
            paper_id=data["paper_id"],
            paper_key=data.get("paper_key", data["paper_id"]),
            section_number=data["section_number"],
            section_title=data["section_title"],
            block_id=data["block_id"],
            block_order=data["block_order"],
            ref_id=str(data.get("ref_id") or ""),
            canonical_paper_key=str(data.get("canonical_paper_key") or data.get("paper_key") or ""),
            source_type=str(data.get("source_type") or "structured_ref"),
            spans=[CitationSpan.from_dict(s) for s in data.get("spans", [])],
            context_before=data.get("context_before", ""),
            context_after=data.get("context_after", ""),
        )


@dataclass(frozen=True)
class CitationCluster:
    cluster_id: str
    paper_id: str
    paper_key: str
    occurrence_ids: List[str] = field(default_factory=list)
    first_occurrence_section: int = 0
    total_occurrences: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CitationCluster":
        return cls(**data)


@dataclass(frozen=True)
class CitationSetBundle:
    bundle_id: str
    citation_set_key: str
    paper_ids: List[str] = field(default_factory=list)
    paper_keys: List[str] = field(default_factory=list)
    occurrence_ids: List[str] = field(default_factory=list)
    block_ids: List[str] = field(default_factory=list)
    section_numbers: List[int] = field(default_factory=list)
    section_titles: List[str] = field(default_factory=list)
    claim_texts: List[str] = field(default_factory=list)
    claim_units: List[Dict[str, Any]] = field(default_factory=list)
    citation_tokens: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CitationSetBundle":
        return cls(**data)


@dataclass(frozen=True)
class BibliographyEntry:
    entry_id: str
    paper_id: str
    paper_key: str
    citation_text: str
    is_cited: bool = True
    cluster_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BibliographyEntry":
        return cls(**data)


@dataclass(frozen=True)
class CitationPaperEntry:
    entry_id: str
    paper_id: str
    paper_key: str
    title: str
    authors: List[str] = field(default_factory=list)
    year: str = ""
    journal: str = ""
    doi: str = ""
    aliases: List[str] = field(default_factory=list)
    status: str = "clean_canonical"
    reasons: List[str] = field(default_factory=list)
    confidence_score: float = 1.0
    decision_threshold: float = 0.85
    decision_source: str = "rule"
    source_fields: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CitationMigrationReport:
    contract_version: str
    load_source: str
    fallback_counters: Dict[str, int] = field(default_factory=dict)
    paper_statuses: List[Dict[str, Any]] = field(default_factory=list)
    legacy_citation_policy: str = "report_only"
    legacy_warnings: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CitationManifestV1:
    artifact_type: str
    artifact_version: str
    created_from_job_id: str
    created_at: str
    manifest_identity: Dict[str, Any]
    review_reference: Dict[str, Any]
    citations: Sequence[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CitationManifestV2:
    artifact_type: str
    artifact_version: str
    created_from_job_id: str
    created_at: str
    manifest_identity: Dict[str, Any]
    review_reference: Dict[str, Any]
    occurrences: List[CitationOccurrence] = field(default_factory=list)
    clusters: List[CitationCluster] = field(default_factory=list)
    citation_sets: List[CitationSetBundle] = field(default_factory=list)
    bibliography: List[BibliographyEntry] = field(default_factory=list)
    review_draft_version: str = "v2"
    legacy_citation_policy: str = "report_only"
    legacy_warnings: List[Dict[str, Any]] = field(default_factory=list)
    fallback_counters: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "created_from_job_id": self.created_from_job_id,
            "created_at": self.created_at,
            "manifest_identity": self.manifest_identity,
            "review_reference": self.review_reference,
            "occurrences": [occ.to_dict() for occ in self.occurrences],
            "clusters": [cluster.to_dict() for cluster in self.clusters],
            "citation_sets": [bundle.to_dict() for bundle in self.citation_sets],
            "bibliography": [entry.to_dict() for entry in self.bibliography],
            "review_draft_version": self.review_draft_version,
            "legacy_citation_policy": self.legacy_citation_policy,
            "legacy_warnings": list(self.legacy_warnings),
            "fallback_counters": dict(self.fallback_counters),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CitationManifestV2":
        return cls(
            artifact_type=data["artifact_type"],
            artifact_version=data["artifact_version"],
            created_from_job_id=data["created_from_job_id"],
            created_at=data["created_at"],
            manifest_identity=data["manifest_identity"],
            review_reference=data["review_reference"],
            occurrences=[CitationOccurrence.from_dict(o) for o in data.get("occurrences", [])],
            clusters=[CitationCluster.from_dict(c) for c in data.get("clusters", [])],
            citation_sets=[CitationSetBundle.from_dict(c) for c in data.get("citation_sets", [])],
            bibliography=[BibliographyEntry.from_dict(b) for b in data.get("bibliography", [])],
            review_draft_version=data.get("review_draft_version", "v2"),
            legacy_citation_policy=str(data.get("legacy_citation_policy") or "report_only"),
            legacy_warnings=list(data.get("legacy_warnings") or []),
            fallback_counters=dict(data.get("fallback_counters") or {}),
        )

    def get_cited_bibliography(self) -> List[BibliographyEntry]:
        return [entry for entry in self.bibliography if entry.is_cited]
    
    def get_occurrences_for_paper(self, paper_identifier: str) -> List[CitationOccurrence]:
        """根据 paper_id 或 paper_key 获取所有相关的引用出现"""
        return [
            occ for occ in self.occurrences 
            if occ.paper_id == paper_identifier or occ.paper_key == paper_identifier
        ]
    
    def get_cluster_for_paper(self, paper_identifier: str) -> Optional[CitationCluster]:
        """根据 paper_id 或 paper_key 获取相关的引用集群"""
        for cluster in self.clusters:
            if cluster.paper_id == paper_identifier or cluster.paper_key == paper_identifier:
                return cluster
        return None


@dataclass(frozen=True)
class CitationManifestV3:
    artifact_type: str
    artifact_version: str
    created_from_job_id: str
    created_at: str
    manifest_identity: Dict[str, Any]
    review_reference: Dict[str, Any]
    paper_entries: List[CitationPaperEntry] = field(default_factory=list)
    occurrences: List[CitationOccurrence] = field(default_factory=list)
    clusters: List[CitationCluster] = field(default_factory=list)
    citation_sets: List[CitationSetBundle] = field(default_factory=list)
    bibliography: List[BibliographyEntry] = field(default_factory=list)
    migration_report: CitationMigrationReport = field(
        default_factory=lambda: CitationMigrationReport(contract_version="v3", load_source="v3")
    )
    review_draft_version: str = "v2"
    dependencies: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "created_from_job_id": self.created_from_job_id,
            "created_at": self.created_at,
            "manifest_identity": self.manifest_identity,
            "review_reference": self.review_reference,
            "paper_entries": [entry.to_dict() for entry in self.paper_entries],
            "occurrences": [occ.to_dict() for occ in self.occurrences],
            "clusters": [cluster.to_dict() for cluster in self.clusters],
            "citation_sets": [bundle.to_dict() for bundle in self.citation_sets],
            "bibliography": [entry.to_dict() for entry in self.bibliography],
            "migration_report": self.migration_report.to_dict(),
            "review_draft_version": self.review_draft_version,
            "dependencies": self.dependencies,
        }


class LegacyCitationPolicy(str, Enum):
    REPORT_ONLY = "report_only"
    WARN_AND_RESOLVE = "warn_and_resolve"
    FATAL = "fatal"


def parse_legacy_citation_policy(value: Any = None) -> LegacyCitationPolicy:
    normalized = str(value or LegacyCitationPolicy.REPORT_ONLY.value).strip().lower()
    try:
        return LegacyCitationPolicy(normalized)
    except ValueError as exc:
        allowed = ", ".join(policy.value for policy in LegacyCitationPolicy)
        raise ValueError(f"Invalid [Validation].legacy_citation_policy={value!r}; expected one of: {allowed}") from exc


def build_citation_manifest_v1(
    *,
    job_id: str,
    project_name: str,
    manifest_id: str,
    review_draft_path: str,
    review_word_path: str,
    citations: Sequence[Dict[str, Any]],
) -> CitationManifestV1:
    normalized_citations = [
        {
            "citation_id": str(citation.get("citation_id") or ""),
            "paper_id": str(citation.get("paper_id") or ""),
            "text": str(citation.get("text") or "").strip(),
            "context": str(citation.get("context") or "").strip(),
            "section_number": int(citation.get("section_number") or 0),
            "section_title": str(citation.get("section_title") or "").strip(),
            "block_id": str(citation.get("block_id") or ""),
            "block_order": int(citation.get("block_order") or 0),
            "review_draft_version": str(citation.get("review_draft_version") or "v2"),
        }
        for citation in citations
    ]

    return CitationManifestV1(
        artifact_type="citation_manifest",
        artifact_version="v1",
        created_from_job_id=job_id,
        created_at=utc_now_iso(),
        manifest_identity={
            "manifest_id": manifest_id,
            "project_name": project_name,
            "scope": "review_citations",
        },
        review_reference={
            "review_draft_path": review_draft_path,
            "review_word_path": review_word_path,
        },
        citations=normalized_citations,
    )


def build_citation_manifest_v2(
    *,
    job_id: str,
    project_name: str,
    manifest_id: str,
    review_draft_path: str,
    review_word_path: str,
    review_draft_version: str = "v2",
    occurrences: Optional[List[CitationOccurrence]] = None,
    clusters: Optional[List[CitationCluster]] = None,
    bibliography: Optional[List[BibliographyEntry]] = None,
    citation_sets: Optional[List[CitationSetBundle]] = None,
) -> CitationManifestV2:
    return CitationManifestV2(
        artifact_type="citation_manifest",
        artifact_version="v2",
        created_from_job_id=job_id,
        created_at=utc_now_iso(),
        manifest_identity={
            "manifest_id": manifest_id,
            "project_name": project_name,
            "scope": "review_citations_truth_source",
        },
        review_reference={
            "review_draft_path": review_draft_path,
            "review_word_path": review_word_path,
        },
        occurrences=occurrences or [],
        clusters=clusters or [],
        citation_sets=citation_sets or [],
        bibliography=bibliography or [],
        review_draft_version=review_draft_version,
    )


def migrate_v1_to_v2(v1_manifest: CitationManifestV1) -> CitationManifestV2:
    occurrences: List[CitationOccurrence] = []
    bibliography: List[BibliographyEntry] = []
    clusters: List[CitationCluster] = []
    paper_occurrence_map: Dict[str, List[str]] = {}

    for idx, citation in enumerate(v1_manifest.citations):
        paper_id = str(citation.get("paper_id", f"paper_{idx}"))
        occurrence_id = f"occ_{idx}_{paper_id}"
        occurrences.append(
            CitationOccurrence(
                occurrence_id=occurrence_id,
                citation_token=str(citation.get("text", "")),
                paper_id=paper_id,
                paper_key=paper_id,
                section_number=int(citation.get("section_number", 0)),
                section_title=str(citation.get("section_title", "")),
                block_id=str(citation.get("block_id", f"block_{idx}")),
                block_order=int(citation.get("block_order", idx + 1)),
                spans=[],
                context_before=str(citation.get("context", "")),
                context_after="",
            )
        )
        paper_occurrence_map.setdefault(paper_id, []).append(occurrence_id)
        if not any(entry.paper_id == paper_id for entry in bibliography):
            bibliography.append(
                BibliographyEntry(
                    entry_id=f"bib_{paper_id}",
                    paper_id=paper_id,
                    paper_key=paper_id,
                    citation_text=str(citation.get("text", "")),
                    is_cited=True,
                )
            )

    for paper_id, occ_ids in paper_occurrence_map.items():
        first_section = min((occ.section_number for occ in occurrences if occ.paper_id == paper_id), default=0)
        clusters.append(
            CitationCluster(
                cluster_id=f"cluster_{paper_id}",
                paper_id=paper_id,
                paper_key=paper_id,
                occurrence_ids=occ_ids,
                first_occurrence_section=first_section,
                total_occurrences=len(occ_ids),
            )
        )

    return CitationManifestV2(
        artifact_type="citation_manifest",
        artifact_version="v2",
        created_from_job_id=v1_manifest.created_from_job_id,
        created_at=v1_manifest.created_at,
        manifest_identity={**v1_manifest.manifest_identity, "migrated_from": "v1"},
        review_reference=v1_manifest.review_reference,
        occurrences=occurrences,
        clusters=clusters,
        citation_sets=[],
        bibliography=bibliography,
        review_draft_version="v2",
    )


def unresolved_occurrences(citation_manifest: Dict[str, Any] | CitationManifestV2) -> List[Dict[str, Any]]:
    manifest_dict = citation_manifest.to_dict() if isinstance(citation_manifest, CitationManifestV2) else citation_manifest
    unresolved: List[Dict[str, Any]] = []
    for occurrence in manifest_dict.get("occurrences", []):
        paper_id = str(occurrence.get("paper_id") or "").strip()
        if not paper_id or paper_id == "unknown":
            unresolved.append(occurrence)
    return unresolved


def _build_occurrence(
    *,
    occurrence_id: str,
    citation_token: str,
    paper_id: Optional[str],
    paper_key: Optional[str],
    ref_id: str = "",
    canonical_paper_key: Optional[str] = None,
    source_type: str = "structured_ref",
    section_number: int,
    section_title: str,
    block_id: str,
    block_order: int,
    block_text: str,
    span_start: Optional[int] = None,
    span_end: Optional[int] = None,
) -> CitationOccurrence:
    safe_paper_id = str(paper_id or "unknown").strip() or "unknown"
    safe_paper_key = str(paper_key or safe_paper_id).strip() or "unknown"
    safe_canonical_key = str(canonical_paper_key or safe_paper_key).strip() or safe_paper_key
    spans: List[CitationSpan] = []
    if isinstance(span_start, int) and isinstance(span_end, int) and 0 <= span_start < span_end <= len(block_text):
        spans.append(
            CitationSpan(
                span_id=f"span_{occurrence_id}",
                start_offset=span_start,
                end_offset=span_end,
                text=block_text[span_start:span_end],
            )
        )
    context_before = block_text[max(0, (span_start or 0) - 160):(span_start or 0)].strip() if spans else block_text[:200].strip()
    context_after = block_text[span_end:(span_end + 160)].strip() if spans and span_end is not None else ""
    return CitationOccurrence(
        occurrence_id=occurrence_id,
        citation_token=citation_token,
        paper_id=safe_paper_id,
        paper_key=safe_paper_key,
        section_number=section_number,
        section_title=section_title,
        block_id=block_id,
        block_order=block_order,
        ref_id=str(ref_id or ""),
        canonical_paper_key=safe_canonical_key,
        source_type=str(source_type or "structured_ref"),
        spans=spans,
        context_before=context_before,
        context_after=context_after,
    )


def normalize_citation_set_key(paper_ids: Sequence[str], paper_keys: Sequence[str] | None = None) -> str:
    normalized = [str(item).strip() for item in paper_ids if str(item).strip() and str(item).strip() != "unknown"]
    if not normalized and paper_keys is not None:
        normalized = [str(item).strip() for item in paper_keys if str(item).strip() and str(item).strip() != "unknown"]
    normalized = sorted(dict.fromkeys(normalized))
    return "+".join(normalized)


def _strip_citation_tokens(text: str) -> str:
    cleaned = re.sub(r"\[\[cite_ref:[^\]]+\]\]", "", text or "")
    cleaned = re.sub(r"\[\[cite:[^\]]+\]\]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _unique_non_empty(values: Iterable[Any]) -> List[str]:
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _occurrence_bounds(occurrence: CitationOccurrence) -> tuple[Optional[int], Optional[int]]:
    starts = [span.start_offset for span in occurrence.spans if isinstance(span.start_offset, int)]
    ends = [span.end_offset for span in occurrence.spans if isinstance(span.end_offset, int)]
    if not starts or not ends:
        return None, None
    return min(starts), max(ends)


def _citation_tail_remainder(
    sentence_text: str,
    sentence_occurrences: Sequence[CitationOccurrence],
    *,
    sentence_start: int,
) -> str:
    starts: List[int] = []
    for occurrence in sentence_occurrences:
        start, _end = _occurrence_bounds(occurrence)
        if start is not None:
            starts.append(max(start - sentence_start, 0))
    if not starts:
        return sentence_text

    tail = sentence_text[min(starts):]
    for occurrence in sentence_occurrences:
        if occurrence.citation_token:
            tail = tail.replace(occurrence.citation_token, "")
    tail = re.sub(r"\[\[cite_ref:[^\]]+\]\]", "", tail)
    tail = re.sub(r"\[\[cite:[^\]]+\]\]", "", tail)
    tail = re.sub(r"[\s,，;；:：.。!?！？、()\[\]（）]+", "", tail)
    return tail


def _semantic_claim_count(cleaned_sentence: str) -> int:
    text = re.sub(r"\s+", " ", cleaned_sentence or "").strip()
    if not text:
        return 0
    parts = [part.strip() for part in re.split(r"[;；]+", text) if part.strip()]
    return len(parts) if len(parts) > 1 else 1


def _semantic_block_claim_count(text: str) -> int:
    cleaned = _strip_citation_tokens(text)
    parts = [part.strip() for part in re.split(r"[.!?;；。！？]+", cleaned) if part.strip()]
    return len(parts) if parts else 0


def _prior_uncited_semantic_claim_exists(text: str) -> bool:
    """Return whether prior prose contains a claim with no local citation token."""
    sentence_with_trailing_citations = re.compile(
        r"[^.!?;；。！？]*(?:[.!?;；。！？]+|$)"
        r"(?:\s*\[\[(?:cite_ref|cite):[^\]]+\]\])*"
    )
    for match in sentence_with_trailing_citations.finditer(text or ""):
        segment = match.group(0)
        cleaned = _strip_citation_tokens(segment)
        if not cleaned or not re.search(r"\w", cleaned, flags=re.UNICODE):
            continue
        has_citation = bool(re.search(r"\[\[(?:cite_ref|cite):[^\]]+\]\]", segment))
        if not has_citation and _semantic_claim_count(cleaned) > 0:
            return True
    return False


def _alignment_for_sentence(
    *,
    sentence_text: str,
    sentence_start: int,
    block_text_before_sentence: str,
    sentence_occurrences: Sequence[CitationOccurrence],
) -> tuple[str, float]:
    if not sentence_occurrences:
        return "legacy_fallback", 0.0
    if any(_occurrence_bounds(occurrence)[0] is None for occurrence in sentence_occurrences):
        return "legacy_fallback", 0.0

    citation_tail_empty = _citation_tail_remainder(
        sentence_text,
        sentence_occurrences,
        sentence_start=sentence_start,
    ) == ""
    citation_tokens = _unique_non_empty(
        occurrence.citation_token for occurrence in sentence_occurrences
    )
    if (
        len(sentence_occurrences) > 1
        and citation_tail_empty
        and len(citation_tokens) == 1
        and citation_tokens[0].startswith("[[cite_ref:")
    ):
        return "inferred", 0.82
    if (
        len(sentence_occurrences) > 1
        and citation_tail_empty
        and citation_tokens
        and all(token.startswith("[[cite_ref:") for token in citation_tokens)
    ):
        return "inferred", 0.78
    prior_uncited_claim_exists = _prior_uncited_semantic_claim_exists(block_text_before_sentence)

    if len(sentence_occurrences) > 1 and citation_tail_empty and prior_uncited_claim_exists:
        return "ambiguous", 0.35
    if len(sentence_occurrences) == 1 and citation_tail_empty:
        return "inferred", 0.86
    if len(sentence_occurrences) > 1 and citation_tail_empty:
        return "inferred", 0.74
    return "explicit", 0.92


def _build_claim_unit(
    *,
    claim_marker: str,
    citation_set_key: str,
    block_id: str,
    sentence_index: int,
    span_start: int,
    span_end: int,
    claim_text: str,
    sentence_occurrences: Sequence[CitationOccurrence],
    block_text: str,
) -> Dict[str, Any]:
    raw_sentence = block_text[span_start:span_end]
    alignment_status, alignment_confidence = _alignment_for_sentence(
        sentence_text=raw_sentence,
        sentence_start=span_start,
        block_text_before_sentence=block_text[:span_start],
        sentence_occurrences=sentence_occurrences,
    )
    supporting_paper_ids = _unique_non_empty(occ.paper_id for occ in sentence_occurrences if occ.paper_id != "unknown")
    supporting_paper_keys = _unique_non_empty(occ.paper_key for occ in sentence_occurrences if occ.paper_key != "unknown")
    supporting_occurrence_ids = _unique_non_empty(occ.occurrence_id for occ in sentence_occurrences)
    claim_unit = {
        "claim_unit_id": hashlib.sha256(claim_marker.encode("utf-8")).hexdigest()[:16],
        "citation_set_key": citation_set_key,
        "block_id": block_id,
        "sentence_index": sentence_index,
        "span_start": span_start,
        "span_end": span_end,
        "raw_text": raw_sentence,
        "display_text": raw_sentence.strip(),
        "claim_text": claim_text,
        "citation_tokens": sorted(
            dict.fromkeys(occ.citation_token for occ in sentence_occurrences if occ.citation_token)
        ),
        "block_anchor_hash": hashlib.sha256(block_text.encode("utf-8")).hexdigest()[:8],
        "supporting_paper_ids": supporting_paper_ids if alignment_status in {"explicit", "inferred"} else [],
        "supporting_paper_keys": supporting_paper_keys if alignment_status in {"explicit", "inferred"} else [],
        "supporting_occurrence_ids": supporting_occurrence_ids if alignment_status in {"explicit", "inferred"} else [],
        "alignment_status": alignment_status,
        "alignment_confidence": alignment_confidence,
    }
    if alignment_status == "ambiguous":
        claim_unit["pooled_paper_ids"] = supporting_paper_ids
        claim_unit["pooled_occurrence_ids"] = supporting_occurrence_ids
    return claim_unit


def _build_citation_set_bundles(
    *,
    occurrences: Sequence[CitationOccurrence],
    review_draft_v2: Dict[str, Any],
) -> List[CitationSetBundle]:
    sections = review_draft_v2.get("content", {}).get("sections", [])
    occurrences_by_block: Dict[str, List[CitationOccurrence]] = {}
    for occurrence in occurrences:
        occurrences_by_block.setdefault(occurrence.block_id, []).append(occurrence)

    bundles_by_key: Dict[str, Dict[str, Any]] = {}
    for section in sections:
        section_number = int(section.get("section_number") or 0)
        section_title = str(section.get("section_title") or "")
        for block in section.get("blocks", []):
            block_id = str(block.get("block_id") or "")
            block_text = str(block.get("text") or "")
            block_occurrences = occurrences_by_block.get(block_id, [])
            if not block_occurrences:
                continue

            sentence_spans = segment_sentences(block_text)
            for sentence_index, sentence_span in enumerate(sentence_spans, start=1):
                sent_start = sentence_span.span_start
                sent_end = sentence_span.span_end
                sentence_text = sentence_span.raw_text
                sentence_occurrences = [
                    occurrence
                    for occurrence in block_occurrences
                    if any(
                        max(sent_start, span.start_offset) < min(sent_end, span.end_offset)
                        for span in occurrence.spans
                    ) or not occurrence.spans
                ]
                if not sentence_occurrences:
                    continue

                paper_ids = [occ.paper_id for occ in sentence_occurrences]
                paper_keys = [occ.paper_key for occ in sentence_occurrences]
                citation_set_key = normalize_citation_set_key(paper_ids, paper_keys)
                if not citation_set_key:
                    continue

                aggregate = bundles_by_key.setdefault(
                    citation_set_key,
                    {
                        "bundle_id": f"bundle_{len(bundles_by_key) + 1}",
                        "citation_set_key": citation_set_key,
                        "paper_ids": sorted(dict.fromkeys(paper_ids)),
                        "paper_keys": sorted(dict.fromkeys(paper_keys)),
                        "occurrence_ids": [],
                        "block_ids": [],
                        "section_numbers": [],
                        "section_titles": [],
                        "claim_texts": [],
                        "claim_units": [],
                        "citation_tokens": [],
                    },
                )

                aggregate["occurrence_ids"].extend(occ.occurrence_id for occ in sentence_occurrences)
                if block_id not in aggregate["block_ids"]:
                    aggregate["block_ids"].append(block_id)
                if section_number not in aggregate["section_numbers"]:
                    aggregate["section_numbers"].append(section_number)
                if section_title and section_title not in aggregate["section_titles"]:
                    aggregate["section_titles"].append(section_title)

                cleaned_sentence = _strip_citation_tokens(sentence_text)
                if cleaned_sentence:
                    claim_marker = f"{block_id}:{sentence_index}:{cleaned_sentence}"
                    if claim_marker not in aggregate.setdefault("_claim_markers", []):
                        aggregate["_claim_markers"].append(claim_marker)
                        aggregate["claim_texts"].append(cleaned_sentence)
                        aggregate["claim_units"].append(
                            _build_claim_unit(
                                claim_marker=claim_marker,
                                citation_set_key=citation_set_key,
                                block_id=block_id,
                                sentence_index=sentence_index,
                                span_start=sent_start,
                                span_end=sent_end,
                                claim_text=cleaned_sentence,
                                sentence_occurrences=sentence_occurrences,
                                block_text=block_text,
                            )
                        )

                for occurrence in sentence_occurrences:
                    if occurrence.citation_token not in aggregate["citation_tokens"]:
                        aggregate["citation_tokens"].append(occurrence.citation_token)

    bundles: List[CitationSetBundle] = []
    for aggregate in bundles_by_key.values():
        aggregate.pop("_claim_markers", None)
        aggregate["occurrence_ids"] = list(dict.fromkeys(aggregate["occurrence_ids"]))
        bundles.append(CitationSetBundle(**aggregate))
    bundles.sort(key=lambda bundle: (len(bundle.paper_ids), bundle.citation_set_key))
    return bundles


def _build_exact_entry_lookup(
    entries: Sequence[Any],
    paper_summaries: Sequence[Mapping[str, Any]],
) -> Dict[str, List[Any]]:
    exact: Dict[str, List[Any]] = {}

    def _add(value: Any, entry: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        keys = {text.casefold()}
        normalized_doi = extract_doi_aliases(text)
        keys.update(item.casefold() for item in normalized_doi)
        for key in keys:
            exact.setdefault(key, [])
            if entry not in exact[key]:
                exact[key].append(entry)

    for index, entry in enumerate(entries):
        summary = paper_summaries[index] if index < len(paper_summaries) else {}
        paper_info_raw = summary.get("paper_info", {}) if isinstance(summary, Mapping) else {}
        paper_info = paper_info_raw if isinstance(paper_info_raw, Mapping) else {}
        for value in (
            getattr(entry, "paper_id", ""),
            getattr(entry, "paper_key", ""),
            getattr(entry, "doi", ""),
            paper_info.get("canonical_paper_key"),
            paper_info.get("source_paper_id"),
            paper_info.get("doi"),
        ):
            _add(value, entry)
    return exact


def _lookup_exact_unique(value: Any, exact_lookup: Mapping[str, List[Any]]) -> tuple[Optional[Any], str]:
    candidates: List[str] = []
    raw = str(value or "").strip()
    if raw:
        candidates.append(raw.casefold())
    for doi in extract_doi_aliases(raw):
        candidates.append(doi.casefold())
    for key in dict.fromkeys(candidates):
        matches = exact_lookup.get(key, [])
        if len(matches) == 1:
            return matches[0], "resolved"
        if len(matches) > 1:
            return None, "ambiguous"
    return None, "missing"


def _warning(
    *,
    policy: LegacyCitationPolicy,
    token: str,
    source_type: str,
    disposition: str,
    reason: str,
    section_number: int,
    section_title: str,
    block_id: str,
) -> Dict[str, Any]:
    return {
        "policy": policy.value,
        "citation_token": token,
        "source_type": source_type,
        "disposition": disposition,
        "reason": reason,
        "section_number": section_number,
        "section_title": section_title,
        "block_id": block_id,
    }


def build_citation_manifest_v2_from_review_draft(
    *,
    job_id: str,
    project_name: str,
    manifest_id: str,
    review_draft_path: str,
    review_word_path: str,
    review_draft_v2: Dict[str, Any],
    paper_summaries: List[Dict[str, Any]],
    allow_legacy_regex: bool = True,
    literature_map: Optional[Dict[str, Any]] = None,
    citation_ref_catalog: Optional[Mapping[str, Any]] = None,
    legacy_citation_policy: str | LegacyCitationPolicy = LegacyCitationPolicy.REPORT_ONLY,
) -> CitationManifestV2:
    occurrences: List[CitationOccurrence] = []
    clusters: List[CitationCluster] = []
    citation_sets: List[CitationSetBundle] = []
    bibliography: List[BibliographyEntry] = []

    entries, alias_map = build_citation_catalog(paper_summaries)
    entries, alias_map = augment_citation_catalog_from_literature_map(entries, alias_map, literature_map)
    exact_lookup = _build_exact_entry_lookup(entries, paper_summaries)
    paper_occurrence_map: Dict[str, List[str]] = {}
    sections = review_draft_v2.get("content", {}).get("sections", [])

    occurrence_counter = 0
    policy = legacy_citation_policy if isinstance(legacy_citation_policy, LegacyCitationPolicy) else parse_legacy_citation_policy(legacy_citation_policy)
    legacy_warnings: List[Dict[str, Any]] = []
    fallback_cite_pattern = re.compile(r"\[\[cite:[^|\]]+(?:\|[^\]]+)*\]\]")
    fallback_apa_pattern = re.compile(r"\([^)]+,\s*\d{4}[^)]*\)")

    for section in sections:
        section_number = int(section.get("section_number") or 0)
        section_title = str(section.get("section_title") or "")
        for block in section.get("blocks", []):
            block_id = str(block.get("block_id") or f"s{section_number}_b0")
            block_order = int(block.get("block_order") or 0)
            block_text = str(block.get("text") or "")
            block_citations = block.get("citations", [])

            extracted_citations = list(block_citations)
            if not extracted_citations and allow_legacy_regex:
                for match in fallback_cite_pattern.finditer(block_text):
                    extracted_citations.append(
                        {
                            "citation_token": match.group(0),
                            "paper_key": extract_citation_key(match.group(0)),
                            "span_start": match.start(),
                            "span_end": match.end(),
                            "source_type": "legacy_token",
                        }
                    )
                for match in fallback_apa_pattern.finditer(block_text):
                    extracted_citations.append(
                        {
                            "citation_token": match.group(0),
                            "span_start": match.start(),
                            "span_end": match.end(),
                            "source_type": "legacy_apa",
                        }
                    )

            for citation in extracted_citations:
                citation_token = str(
                    citation.get("citation_token")
                    or citation.get("raw_text")
                    or citation.get("text")
                    or ""
                ).strip()
                source_type = str(citation.get("source_type") or "").strip()
                ref_id = str(citation.get("ref_id") or "").strip()
                explicit_paper_key = str(citation.get("paper_key") or "").strip() or None
                explicit_paper_id = str(citation.get("paper_id") or "").strip() or None
                canonical_paper_key = str(citation.get("canonical_paper_key") or explicit_paper_key or "").strip() or None
                if not source_type and (explicit_paper_id or explicit_paper_key or canonical_paper_key):
                    source_type = "structured_token"
                entry = None
                resolved_paper_id: Optional[str] = None
                resolved_paper_key: Optional[str] = None
                resolved_canonical_key: Optional[str] = None
                truth_source_type = source_type

                structured_ref_ids = [ref_id] if ref_id else []
                if source_type == "structured_ref":
                    structured_ref_ids = [str(item).strip() for item in structured_ref_ids if str(item).strip()]
                    if not structured_ref_ids:
                        structured_ref_ids = extract_ref_ids_from_token(citation_token)
                    if not structured_ref_ids:
                        legacy_warnings.append(
                            _warning(
                                policy=policy,
                                token=citation_token,
                                source_type="structured_ref",
                                disposition="NEEDS_REVIEW",
                                reason="malformed structured citation ref token",
                                section_number=section_number,
                                section_title=section_title,
                                block_id=block_id,
                            )
                        )
                        continue

                    for structured_ref_id in structured_ref_ids:
                        catalog_entry = resolve_ref_id(citation_ref_catalog, structured_ref_id)
                        if catalog_entry:
                            resolved_paper_id = str(catalog_entry.get("paper_id") or "").strip() or None
                            resolved_canonical_key = str(catalog_entry.get("canonical_paper_key") or resolved_paper_id or "").strip() or None
                            resolved_paper_key = resolved_canonical_key
                            entry, _status = _lookup_exact_unique(
                                resolved_canonical_key or resolved_paper_id,
                                exact_lookup,
                            )
                        else:
                            legacy_warnings.append(
                                _warning(
                                    policy=policy,
                                    token=citation_token,
                                    source_type="structured_ref",
                                    disposition="NEEDS_REVIEW",
                                    reason=f"unresolved citation ref id: {structured_ref_id}",
                                    section_number=section_number,
                                    section_title=section_title,
                                    block_id=block_id,
                                )
                            )
                            continue

                        occurrence_counter += 1
                        occurrence = _build_occurrence(
                            occurrence_id=f"occ_{occurrence_counter}",
                            citation_token=citation_token or structured_ref_id or "(Unknown, n.d.)",
                            paper_id=resolved_paper_id or (entry.paper_id if entry else explicit_paper_id),
                            paper_key=resolved_paper_key or (entry.paper_key if entry else canonical_paper_key),
                            ref_id=structured_ref_id,
                            canonical_paper_key=resolved_canonical_key or (entry.paper_key if entry else canonical_paper_key),
                            source_type=truth_source_type,
                            section_number=section_number,
                            section_title=section_title,
                            block_id=block_id,
                            block_order=block_order,
                            block_text=block_text,
                            span_start=citation.get("span_start"),
                            span_end=citation.get("span_end"),
                        )
                        occurrences.append(occurrence)
                        if occurrence.paper_id != "unknown":
                            paper_occurrence_map.setdefault(occurrence.paper_id, []).append(occurrence.occurrence_id)
                    continue
                elif source_type == "exact_id":
                    for candidate in (explicit_paper_id, canonical_paper_key, explicit_paper_key):
                        entry, status = _lookup_exact_unique(candidate, exact_lookup)
                        if entry:
                            break
                        if status == "ambiguous":
                            legacy_warnings.append(
                                _warning(
                                    policy=policy,
                                    token=citation_token,
                                    source_type="exact_id",
                                    disposition="NEEDS_REVIEW",
                                    reason=f"ambiguous exact citation id: {candidate}",
                                    section_number=section_number,
                                    section_title=section_title,
                                    block_id=block_id,
                                )
                            )
                            break
                    if not entry:
                        continue
                    resolved_paper_id = entry.paper_id
                    resolved_paper_key = entry.paper_key
                    resolved_canonical_key = entry.paper_key
                elif source_type == "structured_token":
                    for candidate in (explicit_paper_id, canonical_paper_key, explicit_paper_key, extract_citation_key(citation_token)):
                        entry, status = _lookup_exact_unique(candidate, exact_lookup)
                        if entry:
                            break
                        if status == "ambiguous":
                            legacy_warnings.append(
                                _warning(
                                    policy=policy,
                                    token=citation_token,
                                    source_type="structured_token",
                                    disposition="NEEDS_REVIEW",
                                    reason=f"ambiguous legacy structured citation id: {candidate}",
                                    section_number=section_number,
                                    section_title=section_title,
                                    block_id=block_id,
                                )
                            )
                            break
                    if not entry:
                        legacy_warnings.append(
                            _warning(
                                policy=policy,
                                token=citation_token,
                                source_type="structured_token",
                                disposition="NEEDS_REVIEW",
                                reason="legacy structured citation is unresolved",
                                section_number=section_number,
                                section_title=section_title,
                                block_id=block_id,
                            )
                        )
                        continue
                    resolved_paper_id = entry.paper_id
                    resolved_paper_key = entry.paper_key
                    resolved_canonical_key = entry.paper_key
                    truth_source_type = "exact_id"
                    legacy_warnings.append(
                        _warning(
                            policy=policy,
                            token=citation_token,
                            source_type="structured_token",
                            disposition="migrated_exact_id",
                            reason="legacy structured citation resolved by explicit exact id",
                            section_number=section_number,
                            section_title=section_title,
                            block_id=block_id,
                        )
                    )
                elif source_type == "legacy_token" or citation_token.startswith("[[cite:"):
                    legacy_key = extract_citation_key(citation_token)
                    if policy == LegacyCitationPolicy.FATAL:
                        raise ValueError(f"Legacy citation token is forbidden by legacy_citation_policy=fatal: {citation_token}")
                    if policy == LegacyCitationPolicy.WARN_AND_RESOLVE:
                        entry, status = _lookup_exact_unique(legacy_key, exact_lookup)
                        if entry:
                            resolved_paper_id = entry.paper_id
                            resolved_paper_key = entry.paper_key
                            resolved_canonical_key = entry.paper_key
                            truth_source_type = "exact_id"
                            legacy_warnings.append(
                                _warning(
                                    policy=policy,
                                    token=citation_token,
                                    source_type="legacy_token",
                                    disposition="warn_and_resolved",
                                    reason="legacy [[cite:...]] resolved by exact unique id",
                                    section_number=section_number,
                                    section_title=section_title,
                                    block_id=block_id,
                                )
                            )
                        else:
                            legacy_warnings.append(
                                _warning(
                                    policy=policy,
                                    token=citation_token,
                                    source_type="legacy_token",
                                    disposition="NEEDS_REVIEW",
                                    reason="ambiguous legacy citation id" if status == "ambiguous" else "legacy citation is unresolved",
                                    section_number=section_number,
                                    section_title=section_title,
                                    block_id=block_id,
                                )
                            )
                            continue
                    else:
                        legacy_warnings.append(
                            _warning(
                                policy=policy,
                                token=citation_token,
                                source_type="legacy_token",
                                disposition="report_only",
                                reason="legacy [[cite:...]] token is excluded from citation truth",
                                section_number=section_number,
                                section_title=section_title,
                                block_id=block_id,
                            )
                        )
                        continue
                elif source_type == "legacy_apa" or re.fullmatch(r"\([^)]+,\s*\d{4}[^)]*\)", citation_token):
                    if policy == LegacyCitationPolicy.FATAL:
                        raise ValueError(f"APA-style citation text is forbidden by legacy_citation_policy=fatal: {citation_token}")
                    legacy_warnings.append(
                        _warning(
                            policy=policy,
                            token=citation_token,
                            source_type="legacy_apa",
                            disposition="report_only",
                            reason="APA-style citation text is excluded from citation truth and never auto-resolved",
                            section_number=section_number,
                            section_title=section_title,
                            block_id=block_id,
                        )
                    )
                    continue
                else:
                    legacy_warnings.append(
                        _warning(
                            policy=policy,
                            token=citation_token,
                            source_type=source_type or "unknown",
                            disposition="NEEDS_REVIEW",
                            reason="citation source_type is not a citation truth source",
                            section_number=section_number,
                            section_title=section_title,
                            block_id=block_id,
                        )
                    )
                    continue

                occurrence_counter += 1
                occurrence = _build_occurrence(
                    occurrence_id=f"occ_{occurrence_counter}",
                    citation_token=citation_token or ref_id or "(Unknown, n.d.)",
                    paper_id=resolved_paper_id or (entry.paper_id if entry else explicit_paper_id),
                    paper_key=resolved_paper_key or (entry.paper_key if entry else canonical_paper_key),
                    ref_id=ref_id,
                    canonical_paper_key=resolved_canonical_key or (entry.paper_key if entry else canonical_paper_key),
                    source_type=truth_source_type,
                    section_number=section_number,
                    section_title=section_title,
                    block_id=block_id,
                    block_order=block_order,
                    block_text=block_text,
                    span_start=citation.get("span_start"),
                    span_end=citation.get("span_end"),
                )
                occurrences.append(occurrence)
                if occurrence.paper_id != "unknown":
                    paper_occurrence_map.setdefault(occurrence.paper_id, []).append(occurrence.occurrence_id)

    for paper_id, occurrence_ids in paper_occurrence_map.items():
        first_section = min((occ.section_number for occ in occurrences if occ.paper_id == paper_id), default=0)
        clusters.append(
            CitationCluster(
                cluster_id=f"cluster_{paper_id}",
                paper_id=paper_id,
                paper_key=paper_id,
                occurrence_ids=occurrence_ids,
                first_occurrence_section=first_section,
                total_occurrences=len(occurrence_ids),
            )
        )

    entries_by_exact_paper_id = {entry.paper_id: entry for entry in entries}
    entries_by_exact_paper_key = {entry.paper_key: entry for entry in entries}
    for paper_id in paper_occurrence_map:
        entry = entries_by_exact_paper_id.get(paper_id) or entries_by_exact_paper_key.get(paper_id)
        if entry is None:
            related_occurrence = next((occ for occ in occurrences if occ.paper_id == paper_id), None)
            if related_occurrence is not None:
                entry = entries_by_exact_paper_key.get(related_occurrence.canonical_paper_key)
        if entry is None:
            continue
        bibliography.append(
            BibliographyEntry(
                entry_id=f"bib_{entry.index:03d}",
                paper_id=paper_id,
                paper_key=entry.paper_key,
                citation_text=format_reference_entry(entry),
                is_cited=True,
                cluster_id=f"cluster_{paper_id}",
            )
        )

    citation_sets = _build_citation_set_bundles(
        occurrences=occurrences,
        review_draft_v2=review_draft_v2,
    )
    warning_source_counts: Dict[str, int] = {}
    for warning in legacy_warnings:
        source = str(warning.get("source_type") or "unknown")
        warning_source_counts[source] = warning_source_counts.get(source, 0) + 1

    return CitationManifestV2(
        artifact_type="citation_manifest",
        artifact_version="v2",
        created_from_job_id=job_id,
        created_at=utc_now_iso(),
        manifest_identity={
            "manifest_id": manifest_id,
            "project_name": project_name,
            "scope": "review_citations_truth_source",
        },
        review_reference={
            "review_draft_path": review_draft_path,
            "review_word_path": review_word_path,
        },
        occurrences=occurrences,
        clusters=clusters,
        citation_sets=citation_sets,
        bibliography=bibliography,
        review_draft_version="v2",
        legacy_citation_policy=policy.value,
        legacy_warnings=legacy_warnings,
        fallback_counters={
            "legacy_warnings": len(legacy_warnings),
            "legacy_tokens": warning_source_counts.get("legacy_token", 0),
            "legacy_apa": warning_source_counts.get("legacy_apa", 0),
            "structured_ref_unresolved": warning_source_counts.get("structured_ref", 0),
            "unresolved_occurrences": len(unresolved_occurrences({"occurrences": [occ.to_dict() for occ in occurrences]})),
        },
    )


def build_citation_manifest_v3_from_review_draft(
    *,
    job_id: str,
    project_name: str,
    manifest_id: str,
    review_draft_path: str,
    review_word_path: str,
    review_draft_v2: Dict[str, Any],
    paper_summaries: List[Dict[str, Any]],
    literature_map: Optional[Dict[str, Any]] = None,
    load_source: str = "v3",
    citation_ref_catalog: Optional[Mapping[str, Any]] = None,
    citation_ref_catalog_path: str = "",
    citation_ref_catalog_hash: str = "",
    legacy_citation_policy: str | LegacyCitationPolicy = LegacyCitationPolicy.REPORT_ONLY,
) -> CitationManifestV3:
    legacy_manifest = build_citation_manifest_v2_from_review_draft(
        job_id=job_id,
        project_name=project_name,
        manifest_id=manifest_id,
        review_draft_path=review_draft_path,
        review_word_path=review_word_path,
        review_draft_v2=review_draft_v2,
        paper_summaries=paper_summaries,
        allow_legacy_regex=False,
        literature_map=literature_map,
        citation_ref_catalog=citation_ref_catalog,
        legacy_citation_policy=legacy_citation_policy,
    )

    entries, _alias_map = build_citation_catalog(paper_summaries)
    entries, _alias_map = augment_citation_catalog_from_literature_map(entries, _alias_map, literature_map)
    cited_paper_ids = {cluster.paper_id for cluster in legacy_manifest.clusters}
    paper_entries: List[CitationPaperEntry] = []
    paper_statuses: List[Dict[str, Any]] = []

    for entry in entries:
        if entry.paper_id not in cited_paper_ids:
            continue
        paper_entry = CitationPaperEntry(
            entry_id=f"paper_{entry.index:03d}",
            paper_id=entry.paper_id,
            paper_key=entry.paper_key,
            title=entry.title,
            authors=list(entry.authors),
            year=entry.year,
            journal=entry.journal,
            doi=entry.doi,
            aliases=list(entry.aliases),
            status=entry.migration_status,
            reasons=list(entry.migration_reasons or []),
            confidence_score=entry.confidence_score,
            decision_threshold=entry.decision_threshold,
            decision_source=entry.decision_source,
            source_fields=dict(entry.source_fields or {}),
        )
        paper_entries.append(paper_entry)
        paper_statuses.append(
            {
                "paper_id": paper_entry.paper_id,
                "paper_key": paper_entry.paper_key,
                "status": paper_entry.status,
                "reason_codes": list(paper_entry.reasons),
                "confidence_score": paper_entry.confidence_score,
                "decision_threshold": paper_entry.decision_threshold,
                "decision_source": paper_entry.decision_source,
                "source_fields": dict(paper_entry.source_fields),
                "rerun_required": paper_entry.status == "rerun_required",
            }
        )

    return CitationManifestV3(
        artifact_type="citation_manifest",
        artifact_version="v3",
        created_from_job_id=legacy_manifest.created_from_job_id,
        created_at=legacy_manifest.created_at,
        manifest_identity={
            "manifest_id": manifest_id,
            "project_name": project_name,
            "scope": "review_citations_truth_source",
            "contract_version": "v3",
        },
        review_reference={
            **legacy_manifest.review_reference,
            "citation_ref_catalog_path": citation_ref_catalog_path,
            "citation_ref_catalog_hash": citation_ref_catalog_hash,
        },
        paper_entries=paper_entries,
        occurrences=legacy_manifest.occurrences,
        clusters=legacy_manifest.clusters,
        citation_sets=legacy_manifest.citation_sets,
        bibliography=legacy_manifest.bibliography,
        migration_report=CitationMigrationReport(
            contract_version="v3",
            load_source=load_source,
            fallback_counters={
                "summary_bibliography_fallback": 0,
                "legacy_regex_extraction": 0,
                "legacy_manifest_load": 0,
                "synthetic_citation_sets": 0,
                "summary_generated_reference": 0,
                "unresolved_occurrences": len(unresolved_occurrences(legacy_manifest.to_dict())),
                **dict(legacy_manifest.fallback_counters),
            },
            paper_statuses=paper_statuses,
            legacy_citation_policy=legacy_manifest.legacy_citation_policy,
            legacy_warnings=list(legacy_manifest.legacy_warnings),
        ),
        review_draft_version=legacy_manifest.review_draft_version,
        dependencies={
            "citation_ref_catalog_path": citation_ref_catalog_path,
            "citation_ref_catalog_hash": citation_ref_catalog_hash,
        },
    )

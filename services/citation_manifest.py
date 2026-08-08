from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from services.citation_catalog import (
    augment_citation_catalog_from_literature_map,
    build_citation_catalog,
    extract_doi_aliases,
    format_reference_entry,
)
from services.citation_ref_catalog import extract_ref_ids_from_token, resolve_ref_id
from services.job_workspace import utc_now_iso
from services.sentence_segmenter import segment_sentences


DEFAULT_RENDER_POLICY: Dict[str, str] = {
    "citation_style": "APA7",
    "citation_locale": "en-US",
    "citation_render_mode": "structured_refs",
    "style_engine_version": "auto-generate-render-v1",
    "bibliography_sort_policy": "manifest_order",
    "narrative_parenthetical_policy": "preserve_source_refs",
}

_STRUCTURED_TOKEN_PATTERN = re.compile(r"\[\[cite(?:_ref)?:[^\]]+\]\]")
_REF_ID_PATTERN = re.compile(r"R\d{3,}")


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
    def from_dict(cls, data: Mapping[str, Any]) -> "CitationSpan":
        return cls(
            span_id=str(data["span_id"]),
            start_offset=int(data["start_offset"]),
            end_offset=int(data["end_offset"]),
            text=str(data["text"]),
            anchor_hash=str(data.get("anchor_hash") or ""),
        )


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
    def from_dict(cls, data: Mapping[str, Any]) -> "CitationOccurrence":
        return cls(
            occurrence_id=str(data["occurrence_id"]),
            citation_token=str(data["citation_token"]),
            paper_id=str(data["paper_id"]),
            paper_key=str(data.get("paper_key") or data["paper_id"]),
            section_number=int(data["section_number"]),
            section_title=str(data["section_title"]),
            block_id=str(data["block_id"]),
            block_order=int(data["block_order"]),
            ref_id=str(data.get("ref_id") or ""),
            canonical_paper_key=str(
                data.get("canonical_paper_key") or data.get("paper_key") or ""
            ),
            source_type=str(data.get("source_type") or "structured_ref"),
            spans=[CitationSpan.from_dict(span) for span in data.get("spans", [])],
            context_before=str(data.get("context_before") or ""),
            context_after=str(data.get("context_after") or ""),
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
    def from_dict(cls, data: Mapping[str, Any]) -> "CitationCluster":
        return cls(
            cluster_id=str(data["cluster_id"]),
            paper_id=str(data["paper_id"]),
            paper_key=str(data.get("paper_key") or data["paper_id"]),
            occurrence_ids=[str(item) for item in data.get("occurrence_ids", [])],
            first_occurrence_section=int(data.get("first_occurrence_section") or 0),
            total_occurrences=int(data.get("total_occurrences") or 0),
        )


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
    def from_dict(cls, data: Mapping[str, Any]) -> "CitationSetBundle":
        return cls(
            bundle_id=str(data["bundle_id"]),
            citation_set_key=str(data["citation_set_key"]),
            paper_ids=[str(item) for item in data.get("paper_ids", [])],
            paper_keys=[str(item) for item in data.get("paper_keys", [])],
            occurrence_ids=[str(item) for item in data.get("occurrence_ids", [])],
            block_ids=[str(item) for item in data.get("block_ids", [])],
            section_numbers=[int(item) for item in data.get("section_numbers", [])],
            section_titles=[str(item) for item in data.get("section_titles", [])],
            claim_texts=[str(item) for item in data.get("claim_texts", [])],
            claim_units=[dict(item) for item in data.get("claim_units", [])],
            citation_tokens=[str(item) for item in data.get("citation_tokens", [])],
        )


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
    def from_dict(cls, data: Mapping[str, Any]) -> "BibliographyEntry":
        return cls(
            entry_id=str(data["entry_id"]),
            paper_id=str(data["paper_id"]),
            paper_key=str(data.get("paper_key") or data["paper_id"]),
            citation_text=str(data.get("citation_text") or ""),
            is_cited=bool(data.get("is_cited", True)),
            cluster_id=(str(data["cluster_id"]) if data.get("cluster_id") else None),
        )


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

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CitationPaperEntry":
        return cls(
            entry_id=str(data["entry_id"]),
            paper_id=str(data["paper_id"]),
            paper_key=str(data.get("paper_key") or data["paper_id"]),
            title=str(data.get("title") or ""),
            authors=[str(item) for item in data.get("authors", [])],
            year=str(data.get("year") or ""),
            journal=str(data.get("journal") or ""),
            doi=str(data.get("doi") or ""),
            aliases=[str(item) for item in data.get("aliases", [])],
            status=str(data.get("status") or "clean_canonical"),
            reasons=[str(item) for item in data.get("reasons", [])],
            confidence_score=float(data.get("confidence_score") or 0.0),
            decision_threshold=float(data.get("decision_threshold") or 0.0),
            decision_source=str(data.get("decision_source") or "rule"),
            source_fields={str(k): str(v) for k, v in dict(data.get("source_fields") or {}).items()},
        )


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
    review_draft_version: str = "v3"
    dependencies: Dict[str, Any] = field(default_factory=dict)
    render_policy: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_RENDER_POLICY))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "created_from_job_id": self.created_from_job_id,
            "created_at": self.created_at,
            "manifest_identity": self.manifest_identity,
            "review_reference": self.review_reference,
            "paper_entries": [entry.to_dict() for entry in self.paper_entries],
            "occurrences": [occurrence.to_dict() for occurrence in self.occurrences],
            "clusters": [cluster.to_dict() for cluster in self.clusters],
            "citation_sets": [bundle.to_dict() for bundle in self.citation_sets],
            "bibliography": [entry.to_dict() for entry in self.bibliography],
            "review_draft_version": self.review_draft_version,
            "dependencies": self.dependencies,
            "render_policy": self.render_policy,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CitationManifestV3":
        return cls(
            artifact_type=str(data["artifact_type"]),
            artifact_version=str(data["artifact_version"]),
            created_from_job_id=str(data["created_from_job_id"]),
            created_at=str(data["created_at"]),
            manifest_identity=dict(data["manifest_identity"]),
            review_reference=dict(data["review_reference"]),
            paper_entries=[CitationPaperEntry.from_dict(item) for item in data.get("paper_entries", [])],
            occurrences=[CitationOccurrence.from_dict(item) for item in data.get("occurrences", [])],
            clusters=[CitationCluster.from_dict(item) for item in data.get("clusters", [])],
            citation_sets=[CitationSetBundle.from_dict(item) for item in data.get("citation_sets", [])],
            bibliography=[BibliographyEntry.from_dict(item) for item in data.get("bibliography", [])],
            review_draft_version=str(data.get("review_draft_version") or "v3"),
            dependencies=dict(data.get("dependencies") or {}),
            render_policy={**DEFAULT_RENDER_POLICY, **dict(data.get("render_policy") or {})},
        )

    def get_cited_bibliography(self) -> List[BibliographyEntry]:
        return [entry for entry in self.bibliography if entry.is_cited]

    def get_occurrences_for_paper(self, paper_identifier: str) -> List[CitationOccurrence]:
        return [
            occurrence
            for occurrence in self.occurrences
            if occurrence.paper_id == paper_identifier or occurrence.paper_key == paper_identifier
        ]

    def get_cluster_for_paper(self, paper_identifier: str) -> Optional[CitationCluster]:
        for cluster in self.clusters:
            if cluster.paper_id == paper_identifier or cluster.paper_key == paper_identifier:
                return cluster
        return None


def unresolved_occurrences(
    citation_manifest: Mapping[str, Any] | CitationManifestV3,
) -> List[Dict[str, Any]]:
    manifest_dict = (
        citation_manifest.to_dict()
        if isinstance(citation_manifest, CitationManifestV3)
        else citation_manifest
    )
    unresolved: List[Dict[str, Any]] = []
    for raw_occurrence in manifest_dict.get("occurrences", []):
        if not isinstance(raw_occurrence, Mapping):
            unresolved.append(dict(raw_occurrence))
            continue
        occurrence = dict(raw_occurrence)
        paper_id = str(occurrence.get("paper_id") or "").strip()
        ref_id = str(occurrence.get("ref_id") or "").strip()
        if not ref_id or not paper_id or paper_id == "unknown":
            unresolved.append(occurrence)
    return unresolved


def _build_occurrence(
    *,
    occurrence_id: str,
    citation_token: str,
    paper_id: Optional[str],
    paper_key: Optional[str],
    ref_id: str,
    canonical_paper_key: Optional[str],
    source_type: str,
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
    if (
        isinstance(span_start, int)
        and isinstance(span_end, int)
        and 0 <= span_start < span_end <= len(block_text)
    ):
        spans.append(
            CitationSpan(
                span_id=f"span_{occurrence_id}",
                start_offset=span_start,
                end_offset=span_end,
                text=block_text[span_start:span_end],
            )
        )
    context_before = (
        block_text[max(0, (span_start or 0) - 160) : (span_start or 0)].strip()
        if spans
        else block_text[:200].strip()
    )
    context_after = (
        block_text[span_end : span_end + 160].strip()
        if spans and span_end is not None
        else ""
    )
    return CitationOccurrence(
        occurrence_id=occurrence_id,
        citation_token=citation_token,
        paper_id=safe_paper_id,
        paper_key=safe_paper_key,
        section_number=section_number,
        section_title=section_title,
        block_id=block_id,
        block_order=block_order,
        ref_id=ref_id,
        canonical_paper_key=safe_canonical_key,
        source_type=source_type,
        spans=spans,
        context_before=context_before,
        context_after=context_after,
    )


def normalize_citation_set_key(
    paper_ids: Sequence[str], paper_keys: Sequence[str] | None = None
) -> str:
    normalized = [
        str(item).strip()
        for item in paper_ids
        if str(item).strip() and str(item).strip() != "unknown"
    ]
    if not normalized and paper_keys is not None:
        normalized = [
            str(item).strip()
            for item in paper_keys
            if str(item).strip() and str(item).strip() != "unknown"
        ]
    return "+".join(sorted(dict.fromkeys(normalized)))


def _strip_citation_tokens(text: str) -> str:
    cleaned = re.sub(r"\[\[cite_ref:R\d{3,}(?:,\s*R\d{3,})*\]\]", "", text or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _unique_non_empty(values: Iterable[Any]) -> List[str]:
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _occurrence_bounds(occurrence: CitationOccurrence) -> tuple[Optional[int], Optional[int]]:
    starts = [span.start_offset for span in occurrence.spans]
    ends = [span.end_offset for span in occurrence.spans]
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
    tail = sentence_text[min(starts) :]
    for occurrence in sentence_occurrences:
        if occurrence.citation_token:
            tail = tail.replace(occurrence.citation_token, "")
    return re.sub(r"[\s,;:.!?()\[\]]+", "", tail)


def _semantic_claim_count(cleaned_sentence: str) -> int:
    text = re.sub(r"\s+", " ", cleaned_sentence or "").strip()
    if not text:
        return 0
    parts = [part.strip() for part in re.split(r"[;；]+", text) if part.strip()]
    return len(parts) if len(parts) > 1 else 1


def _semantic_block_claim_count(text: str) -> int:
    cleaned = _strip_citation_tokens(text)
    parts = [part.strip() for part in re.split(r"[.!?;；。！？]+", cleaned) if part.strip()]
    return len(parts)


def _alignment_for_sentence(
    *,
    sentence_text: str,
    sentence_start: int,
    block_text_before_sentence: str,
    sentence_occurrences: Sequence[CitationOccurrence],
) -> tuple[str, float]:
    if not sentence_occurrences:
        return "unresolved", 0.0
    if any(_occurrence_bounds(occurrence)[0] is None for occurrence in sentence_occurrences):
        return "unresolved", 0.0

    citation_tail_empty = (
        _citation_tail_remainder(
            sentence_text,
            sentence_occurrences,
            sentence_start=sentence_start,
        )
        == ""
    )
    semantic_claim_count = _semantic_claim_count(_strip_citation_tokens(sentence_text))
    block_claim_count = _semantic_block_claim_count(block_text_before_sentence) + semantic_claim_count
    if len(sentence_occurrences) > 1 and citation_tail_empty and block_claim_count > 1:
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
    supporting_paper_ids = _unique_non_empty(
        occurrence.paper_id
        for occurrence in sentence_occurrences
        if occurrence.paper_id != "unknown"
    )
    supporting_paper_keys = _unique_non_empty(
        occurrence.paper_key
        for occurrence in sentence_occurrences
        if occurrence.paper_key != "unknown"
    )
    supporting_occurrence_ids = _unique_non_empty(
        occurrence.occurrence_id for occurrence in sentence_occurrences
    )
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
            dict.fromkeys(
                occurrence.citation_token
                for occurrence in sentence_occurrences
                if occurrence.citation_token
            )
        ),
        "block_anchor_hash": hashlib.sha256(block_text.encode("utf-8")).hexdigest()[:8],
        "supporting_paper_ids": (
            supporting_paper_ids if alignment_status in {"explicit", "inferred"} else []
        ),
        "supporting_paper_keys": (
            supporting_paper_keys if alignment_status in {"explicit", "inferred"} else []
        ),
        "supporting_occurrence_ids": (
            supporting_occurrence_ids
            if alignment_status in {"explicit", "inferred"}
            else []
        ),
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
    review_draft: Mapping[str, Any],
) -> List[CitationSetBundle]:
    sections = review_draft.get("content", {}).get("sections", [])
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

            for sentence_index, sentence_span in enumerate(segment_sentences(block_text), start=1):
                sent_start = sentence_span.span_start
                sent_end = sentence_span.span_end
                sentence_text = sentence_span.raw_text
                sentence_occurrences = [
                    occurrence
                    for occurrence in block_occurrences
                    if any(
                        max(sent_start, span.start_offset) < min(sent_end, span.end_offset)
                        for span in occurrence.spans
                    )
                    or not occurrence.spans
                ]
                if not sentence_occurrences:
                    continue

                paper_ids = [occurrence.paper_id for occurrence in sentence_occurrences]
                paper_keys = [occurrence.paper_key for occurrence in sentence_occurrences]
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
                        "_claim_markers": [],
                    },
                )
                aggregate["occurrence_ids"].extend(
                    occurrence.occurrence_id for occurrence in sentence_occurrences
                )
                if block_id not in aggregate["block_ids"]:
                    aggregate["block_ids"].append(block_id)
                if section_number not in aggregate["section_numbers"]:
                    aggregate["section_numbers"].append(section_number)
                if section_title and section_title not in aggregate["section_titles"]:
                    aggregate["section_titles"].append(section_title)

                cleaned_sentence = _strip_citation_tokens(sentence_text)
                if cleaned_sentence:
                    claim_marker = f"{block_id}:{sentence_index}:{cleaned_sentence}"
                    if claim_marker not in aggregate["_claim_markers"]:
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
    entries: Sequence[Any], paper_summaries: Sequence[Mapping[str, Any]]
) -> Dict[str, List[Any]]:
    exact: Dict[str, List[Any]] = {}

    def add(value: Any, entry: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        keys = {text.casefold()}
        keys.update(item.casefold() for item in extract_doi_aliases(text))
        for key in keys:
            exact.setdefault(key, [])
            if entry not in exact[key]:
                exact[key].append(entry)

    for index, entry in enumerate(entries):
        summary = paper_summaries[index] if index < len(paper_summaries) else {}
        paper_info = summary.get("paper_info", {}) if isinstance(summary, Mapping) else {}
        paper_info = paper_info if isinstance(paper_info, Mapping) else {}
        for value in (
            getattr(entry, "paper_id", ""),
            getattr(entry, "paper_key", ""),
            getattr(entry, "doi", ""),
            paper_info.get("canonical_paper_key"),
            paper_info.get("source_paper_id"),
            paper_info.get("doi"),
        ):
            add(value, entry)
    return exact


def _lookup_exact_unique(
    value: Any, exact_lookup: Mapping[str, List[Any]]
) -> tuple[Optional[Any], str]:
    candidates: List[str] = []
    raw = str(value or "").strip()
    if raw:
        candidates.append(raw.casefold())
    candidates.extend(doi.casefold() for doi in extract_doi_aliases(raw))
    for key in dict.fromkeys(candidates):
        matches = exact_lookup.get(key, [])
        if len(matches) == 1:
            return matches[0], "resolved"
        if len(matches) > 1:
            return None, "ambiguous"
    return None, "missing"


def _validate_block_tokens(block_id: str, block_text: str) -> None:
    for match in _STRUCTURED_TOKEN_PATTERN.finditer(block_text):
        if not extract_ref_ids_from_token(match.group(0)):
            raise ValueError(
                f"Citation block {block_id} contains a non-structured citation token: {match.group(0)}"
            )


def _citation_ref_ids(citation: Mapping[str, Any], block_id: str) -> tuple[str, List[str]]:
    citation_token = str(
        citation.get("citation_token")
        or citation.get("raw_text")
        or ""
    ).strip()
    token_ref_ids = extract_ref_ids_from_token(citation_token)
    explicit_ref_id = str(citation.get("ref_id") or "").strip()
    if citation_token and not token_ref_ids:
        raise ValueError(
            f"Citation block {block_id} contains a non-structured citation token: {citation_token}"
        )
    if explicit_ref_id and not _REF_ID_PATTERN.fullmatch(explicit_ref_id):
        raise ValueError(f"Citation block {block_id} contains an invalid ref_id: {explicit_ref_id}")
    if explicit_ref_id and token_ref_ids and explicit_ref_id not in token_ref_ids:
        raise ValueError(
            f"Citation block {block_id} ref_id does not match its citation token: {explicit_ref_id}"
        )
    ref_ids = [explicit_ref_id] if explicit_ref_id else token_ref_ids
    if not ref_ids:
        raise ValueError(f"Citation block {block_id} is missing a structured ref_id")
    if not citation_token:
        citation_token = f"[[cite_ref:{', '.join(ref_ids)}]]"
    return citation_token, ref_ids


def build_citation_manifest_from_review_draft(
    *,
    job_id: str,
    project_name: str,
    manifest_id: str,
    review_draft_path: str,
    review_word_path: str,
    review_draft: Dict[str, Any],
    paper_summaries: List[Dict[str, Any]],
    literature_map: Optional[Dict[str, Any]] = None,
    citation_ref_catalog: Optional[Mapping[str, Any]] = None,
    citation_ref_catalog_path: str = "",
    citation_ref_catalog_hash: str = "",
    render_policy: Optional[Mapping[str, Any]] = None,
) -> CitationManifestV3:
    occurrences: List[CitationOccurrence] = []
    clusters: List[CitationCluster] = []
    bibliography: List[BibliographyEntry] = []

    entries, alias_map = build_citation_catalog(paper_summaries)
    entries, alias_map = augment_citation_catalog_from_literature_map(
        entries, alias_map, literature_map
    )
    del alias_map
    exact_lookup = _build_exact_entry_lookup(entries, paper_summaries)
    paper_occurrence_map: Dict[str, List[str]] = {}
    paper_key_by_id: Dict[str, str] = {}
    sections = review_draft.get("content", {}).get("sections", [])
    occurrence_counter = 0

    for section in sections:
        section_number = int(section.get("section_number") or 0)
        section_title = str(section.get("section_title") or "")
        for block in section.get("blocks", []):
            block_id = str(block.get("block_id") or f"s{section_number}_b0")
            block_order = int(block.get("block_order") or 0)
            block_text = str(block.get("text") or "")
            _validate_block_tokens(block_id, block_text)
            block_citations = block.get("citations", [])
            if not isinstance(block_citations, list):
                raise ValueError(f"Citation block {block_id} citations must be an array")

            for citation in block_citations:
                if not isinstance(citation, Mapping):
                    raise ValueError(f"Citation block {block_id} citation must be an object")
                source_type = str(citation.get("source_type") or "").strip()
                if source_type and source_type not in {"structured_ref", "unresolved_ref"}:
                    raise ValueError(
                        f"Citation block {block_id} has unsupported source_type: {source_type}"
                    )
                citation_token, ref_ids = _citation_ref_ids(citation, block_id)

                for ref_id in ref_ids:
                    catalog_entry = resolve_ref_id(citation_ref_catalog, ref_id)
                    resolved_paper_id: Optional[str] = None
                    resolved_paper_key: Optional[str] = None
                    resolved_canonical_key: Optional[str] = None
                    catalog_match = None
                    occurrence_source_type = "unresolved_ref"
                    if catalog_entry:
                        resolved_paper_id = str(catalog_entry.get("paper_id") or "").strip() or None
                        resolved_canonical_key = (
                            str(
                                catalog_entry.get("canonical_paper_key")
                                or resolved_paper_id
                                or ""
                            ).strip()
                            or None
                        )
                        resolved_paper_key = resolved_canonical_key
                        catalog_match, _status = _lookup_exact_unique(
                            resolved_canonical_key or resolved_paper_id,
                            exact_lookup,
                        )
                        occurrence_source_type = "structured_ref"
                        if catalog_match:
                            resolved_paper_id = catalog_match.paper_id
                            resolved_paper_key = catalog_match.paper_key
                            resolved_canonical_key = catalog_match.paper_key

                    occurrence_counter += 1
                    occurrence = _build_occurrence(
                        occurrence_id=f"occ_{occurrence_counter}",
                        citation_token=citation_token,
                        paper_id=resolved_paper_id,
                        paper_key=resolved_paper_key,
                        ref_id=ref_id,
                        canonical_paper_key=resolved_canonical_key,
                        source_type=occurrence_source_type,
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
                        paper_occurrence_map.setdefault(occurrence.paper_id, []).append(
                            occurrence.occurrence_id
                        )
                        paper_key_by_id.setdefault(occurrence.paper_id, occurrence.paper_key)

    for paper_id, occurrence_ids in paper_occurrence_map.items():
        first_section = min(
            (occurrence.section_number for occurrence in occurrences if occurrence.paper_id == paper_id),
            default=0,
        )
        clusters.append(
            CitationCluster(
                cluster_id=f"cluster_{paper_id}",
                paper_id=paper_id,
                paper_key=paper_key_by_id.get(paper_id, paper_id),
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
            related_occurrence = next(
                (occurrence for occurrence in occurrences if occurrence.paper_id == paper_id),
                None,
            )
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
        review_draft=review_draft,
    )
    cited_paper_ids = set(paper_occurrence_map)
    paper_entries: List[CitationPaperEntry] = []
    for entry in entries:
        if entry.paper_id not in cited_paper_ids:
            continue
        reasons = list(entry.migration_reasons or [])
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
            reasons=reasons,
            confidence_score=entry.confidence_score,
            decision_threshold=entry.decision_threshold,
            decision_source=entry.decision_source,
            source_fields=dict(entry.source_fields or {}),
        )
        paper_entries.append(paper_entry)
    return CitationManifestV3(
        artifact_type="citation_manifest",
        artifact_version="v3",
        created_from_job_id=job_id,
        created_at=utc_now_iso(),
        manifest_identity={
            "manifest_id": manifest_id,
            "project_name": project_name,
            "scope": "review_citations_truth_source",
            "contract_version": "v3",
        },
        review_reference={
            "review_draft_path": review_draft_path,
            "review_word_path": review_word_path,
            "citation_ref_catalog_path": citation_ref_catalog_path,
            "citation_ref_catalog_hash": citation_ref_catalog_hash,
        },
        paper_entries=paper_entries,
        occurrences=occurrences,
        clusters=clusters,
        citation_sets=citation_sets,
        bibliography=bibliography,
        review_draft_version="v3",
        dependencies={
            "citation_ref_catalog_path": citation_ref_catalog_path,
            "citation_ref_catalog_hash": citation_ref_catalog_hash,
        },
        render_policy={
            **DEFAULT_RENDER_POLICY,
            **dict(render_policy or {}),
        },
    )

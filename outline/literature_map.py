"""Literature map builder for Outline Intelligence v2.

Derives canonical paper nodes from Stage 1 summaries and paper artifacts.
Conservative: unknowns become diagnostics, not fabricated claims.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from services.paper_identity import (
    build_canonical_paper_key,
    build_paper_key,
    normalize_doi,
    normalize_paper_identity,
    normalized_author_surnames,
    normalized_title_key,
)
from summary_schema import get_ai_summary

from outline.quality_rules import is_method_only_stream_label, is_noise_stream_label, stream_promotion_tier
from outline.v2_models import LiteratureMap, PaperNode, compute_content_hash


_CONFIDENCE_RANK = {"": 0, "low": 1, "medium": 2, "high": 3}
_CLASSIFICATION_ORDER = {"unknown": 0, "peripheral": 1, "background_only": 1, "support": 2, "core": 3}
_STOP_TERMS = {
    "paper",
    "study",
    "research",
    "article",
    "analysis",
    "literature",
    "review",
    "empirical",
    "conceptual",
    "findings",
    "results",
    "conclusion",
    "conclusions",
    "methodology",
    "method",
    "methods",
    "gap",
    "gaps",
    "limitation",
    "limitations",
}
_TERM_SPLIT_RE = re.compile(r"[;,/]|(?:\band\b)|(?:\n+)", re.IGNORECASE)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _hash_summary(summary: Mapping[str, Any]) -> str:
    return compute_content_hash(summary)[:16]


def _safe_str(value: Any, default: str = "") -> str:
    return str(value).strip() if value not in (None, "") else default


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _safe_year(value: Any) -> Optional[int]:
    if value is None:
        return None
    match = re.search(r"\b(19|20)\d{2}\b", str(value))
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def _normalize_authors(value: Any) -> List[str]:
    if isinstance(value, list):
        items = value
    elif value in (None, ""):
        items = []
    else:
        items = re.split(r";|\band\b", str(value), flags=re.IGNORECASE)
    authors: List[str] = []
    seen: set[str] = set()
    for item in items:
        text = _safe_str(item)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        authors.append(text)
    return authors


def _normalize_term(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    text = text.strip(" -–—:;,.()[]{}")
    if not text:
        return ""
    lowered = text.casefold()
    lowered = re.sub(r"[_\-]+", " ", lowered)
    lowered = re.sub(r"[^\w\s]", "", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    if not lowered or lowered in _STOP_TERMS:
        return ""
    if len(lowered) < 3 and lowered not in {"ai", "ml"}:
        return ""
    return lowered


def _flatten_values(value: Any) -> Iterable[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        flattened: List[Any] = []
        for nested in value.values():
            flattened.extend(_flatten_values(nested))
        return flattened
    if isinstance(value, (list, tuple, set)):
        flattened = []
        for item in value:
            flattened.extend(_flatten_values(item))
        return flattened
    if isinstance(value, str):
        parts = [part.strip() for part in _TERM_SPLIT_RE.split(value) if part.strip()]
        return parts or [value]
    return [value]


def _term_list(*values: Any, max_items: int = 12) -> List[str]:
    terms: List[str] = []
    seen: set[str] = set()
    for value in values:
        for item in _flatten_values(value):
            term = _normalize_term(item)
            if not term or term in seen:
                continue
            seen.add(term)
            terms.append(term)
            if len(terms) >= max_items:
                return terms
    return terms


def _text_list(*values: Any, max_items: int = 12) -> List[str]:
    items: List[str] = []
    seen: set[str] = set()
    for value in values:
        for item in _flatten_values(value):
            text = _safe_str(item)
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            items.append(text)
            if len(items) >= max_items:
                return items
    return items


def _first_text(*values: Any) -> str:
    for value in values:
        text = _safe_str(value)
        if text:
            return text
    return ""


def _metadata_confidence_from_summary(summary: Mapping[str, Any]) -> str:
    raw_ai_summary = _as_mapping(summary.get("ai_summary"))
    ai_summary = get_ai_summary(summary)
    quality = _as_mapping(raw_ai_summary.get("quality_audit")) or _as_mapping(ai_summary.get("quality_audit"))
    confidence = str(
        summary.get("metadata_confidence")
        or _as_mapping(summary.get("paper_info")).get("metadata_confidence")
        or quality.get("extraction_confidence")
        or ""
    ).strip().lower()
    return confidence if confidence in {"high", "medium", "low"} else "low"


def _metadata_confidence_from_artifact(artifact: Mapping[str, Any]) -> str:
    source = _as_mapping(artifact.get("source"))
    paper_info = _as_mapping(artifact.get("paper_info"))
    confidence = str(
        artifact.get("metadata_confidence")
        or source.get("metadata_confidence")
        or paper_info.get("metadata_confidence")
        or ""
    ).strip().lower()
    return confidence if confidence in {"high", "medium", "low"} else "medium"


def _extract_metadata_from_summary(summary: Mapping[str, Any]) -> Dict[str, Any]:
    paper_info = _as_mapping(summary.get("paper_info"))
    ai_summary = get_ai_summary(summary)
    ai_metadata = _as_mapping(ai_summary.get("paper_metadata"))
    return {
        "title": _first_text(
            paper_info.get("title"),
            ai_metadata.get("title"),
            summary.get("title"),
        ),
        "authors": _normalize_authors(
            paper_info.get("authors")
            or ai_metadata.get("authors")
            or summary.get("authors")
        ),
        "year": _safe_year(paper_info.get("year") or ai_metadata.get("year") or summary.get("year")),
        "doi": paper_info.get("doi") or ai_metadata.get("doi") or summary.get("doi"),
        "canonical_paper_key": _first_text(
            paper_info.get("canonical_paper_key"),
            summary.get("canonical_paper_key"),
        ),
        "source_paper_id": _first_text(paper_info.get("source_paper_id"), summary.get("source_paper_id")),
        "paper_key_aliases": list(paper_info.get("paper_key_aliases") or summary.get("paper_key_aliases") or []),
        "metadata_confidence": _metadata_confidence_from_summary(summary),
    }


def _extract_metadata_from_artifact(artifact: Mapping[str, Any]) -> Dict[str, Any]:
    identity = _as_mapping(artifact.get("paper_identity"))
    paper_info = _as_mapping(artifact.get("paper_info"))
    analysis = _as_mapping(artifact.get("analysis"))
    ai_summary = get_ai_summary(analysis.get("ai_summary") or artifact.get("ai_summary") or {})
    ai_metadata = _as_mapping(ai_summary.get("paper_metadata"))
    return {
        "title": _first_text(paper_info.get("title"), ai_metadata.get("title"), artifact.get("title")),
        "authors": _normalize_authors(paper_info.get("authors") or ai_metadata.get("authors") or artifact.get("authors")),
        "year": _safe_year(paper_info.get("year") or ai_metadata.get("year") or artifact.get("year")),
        "doi": paper_info.get("doi") or ai_metadata.get("doi") or artifact.get("doi"),
        "canonical_paper_key": _first_text(identity.get("canonical_paper_key"), paper_info.get("canonical_paper_key")),
        "source_paper_id": _first_text(identity.get("source_paper_id"), paper_info.get("source_paper_id")),
        "paper_key_aliases": list(identity.get("paper_key_aliases") or paper_info.get("paper_key_aliases") or []),
        "metadata_confidence": _metadata_confidence_from_artifact(artifact),
    }


def _identity_for_metadata(
    metadata: Mapping[str, Any],
    *,
    source_hash: str = "",
    source_index: int | None = None,
    source_type: str = "",
) -> Tuple[str, str]:
    identity = normalize_paper_identity(
        metadata,
        source_hash=f"{source_hash or 'nohash'}:{source_type or 'record'}:{source_index if source_index is not None else 'unknown'}",
        source_index=source_index,
        allow_title_fallback=False,
    )
    return str(identity["canonical_key"]), str(identity["canonical_key_source"])


def _identity_blocking_diagnostic(record: Mapping[str, Any]) -> Dict[str, str] | None:
    if record.get("identity_source") != "source_hash":
        return None
    metadata = _as_mapping(record.get("metadata"))
    return {
        "type": "missing_stable_paper_identity",
        "paper_key": _safe_str(record.get("canonical_paper_key")),
        "index": str(record.get("source_index", "")),
        "source_type": _safe_str(record.get("source_type")),
        "message": (
            "Paper identity is missing DOI, explicit canonical key, or title-author-year; "
            f"using unique source key for {_safe_str(metadata.get('title'), 'untitled source')}"
        ),
    }


def _alias_values(metadata: Mapping[str, Any], source_hash: str) -> List[str]:
    identity = normalize_paper_identity(metadata, source_hash=source_hash, allow_title_fallback=True)
    aliases = [
        _safe_str(metadata.get("canonical_paper_key")),
        normalize_doi(metadata.get("doi")),
        build_canonical_paper_key(metadata),
        build_paper_key(metadata),
        *[str(alias) for alias in identity.get("aliases", [])],
        normalized_title_key(metadata.get("title")),
        source_hash,
        _safe_str(metadata.get("source_paper_id")),
    ]
    aliases.extend(_safe_str(item) for item in (metadata.get("paper_key_aliases") or []))
    normalized_aliases = []
    for alias in aliases:
        if not alias or alias == "unknown_title":
            continue
        normalized_aliases.append(alias)
        doi_alias = normalize_doi(alias)
        if doi_alias and doi_alias != alias:
            normalized_aliases.append(doi_alias)
    return list(dict.fromkeys(normalized_aliases))


def _core_source(summary_like: Mapping[str, Any]) -> Tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    ai_summary = get_ai_summary(summary_like)
    core = _as_mapping(ai_summary.get("core_analysis"))
    routing = _as_mapping(ai_summary.get("routing"))
    specialized = _as_mapping(ai_summary.get("specialized_details"))
    empirical = _as_mapping(specialized.get("empirical"))
    review = _as_mapping(specialized.get("review"))
    conceptual = _as_mapping(specialized.get("conceptual"))
    return core, routing, empirical, review, conceptual


def _extract_signals_from_summary(summary: Mapping[str, Any]) -> Dict[str, List[str]]:
    paper_info = _as_mapping(summary.get("paper_info"))
    core, routing, empirical, review, conceptual = _core_source(summary)
    core_variables = _as_mapping(empirical.get("core_variables"))
    return {
        "themes": _term_list(
            summary.get("themes"),
            paper_info.get("themes"),
            core.get("key_points"),
            core.get("relevance"),
            routing.get("paper_type"),
            routing.get("paper_subtype_normalized"),
            routing.get("paper_subtype_raw"),
            review.get("main_themes"),
            conceptual.get("core_propositions"),
        ),
        "methods": _term_list(
            summary.get("methods"),
            paper_info.get("methods"),
            core.get("methodology"),
            empirical.get("analysis_technique"),
            empirical.get("data_source_and_size"),
            review.get("review_type"),
            review.get("synthesis_approach"),
        ),
        "theories": _term_list(
            summary.get("theories"),
            paper_info.get("theories"),
            core.get("theoretical_framework"),
            conceptual.get("theoretical_contributions"),
            conceptual.get("conceptual_relationships"),
        ),
        "variables": _term_list(
            summary.get("variables"),
            core_variables,
            empirical.get("research_questions_or_hypotheses"),
        ),
        "gaps": _text_list(
            summary.get("gaps"),
            core.get("research_gap"),
            core.get("future_research_directions"),
        ),
        "limitations": _text_list(
            summary.get("limitations"),
            paper_info.get("limitations"),
            core.get("limitations"),
        ),
        "findings": _text_list(
            summary.get("findings"),
            core.get("findings"),
            core.get("conclusions"),
        ),
    }


def _extract_signals_from_artifact(artifact: Mapping[str, Any]) -> Dict[str, List[str]]:
    analysis = _as_mapping(artifact.get("analysis"))
    paper_info = _as_mapping(artifact.get("paper_info"))
    merged = {
        "paper_info": paper_info,
        "ai_summary": analysis.get("ai_summary") or artifact.get("ai_summary") or {},
        "themes": artifact.get("themes") or paper_info.get("themes"),
        "methods": artifact.get("methods") or paper_info.get("methods"),
        "theories": artifact.get("theories") or paper_info.get("theories"),
        "limitations": artifact.get("limitations") or paper_info.get("limitations"),
    }
    return _extract_signals_from_summary(merged)


def _extract_abstract_from_summary(summary: Mapping[str, Any]) -> str:
    paper_info = _as_mapping(summary.get("paper_info"))
    core, _routing, _empirical, _review, _conceptual = _core_source(summary)
    return _first_text(summary.get("abstract"), paper_info.get("abstract"), core.get("summary"))


def _extract_abstract_from_artifact(artifact: Mapping[str, Any]) -> str:
    analysis = _as_mapping(artifact.get("analysis"))
    return _extract_abstract_from_summary({
        "paper_info": artifact.get("paper_info") or {},
        "ai_summary": analysis.get("ai_summary") or artifact.get("ai_summary") or {},
        "abstract": artifact.get("abstract"),
    })


def _classification_from_record(source: Mapping[str, Any], metadata: Mapping[str, Any]) -> Tuple[str, bool]:
    paper_info = _as_mapping(source.get("paper_info"))
    classification = _safe_str(
        paper_info.get("classification")
        or source.get("classification")
        or metadata.get("classification")
        or "support",
        "support",
    )
    if classification not in {"core", "background_only", "peripheral", "support", "unknown"}:
        classification = "support"
    must_use = str(
        paper_info.get("must_use")
        or source.get("must_use")
        or metadata.get("must_use")
        or ""
    ).strip().lower() in {"true", "1", "yes", "on"}
    if classification == "core":
        must_use = True
    return classification, must_use


def _source_record(
    *,
    source_type: str,
    source_index: int,
    source_hash: str,
    paper_key_seen: str,
    canonical_paper_key: str,
    metadata: Mapping[str, Any],
) -> Dict[str, Any]:
    identity = normalize_paper_identity(
        metadata,
        source_hash=source_hash,
        source_index=source_index,
        allow_title_fallback=False,
    )
    return {
        "source_type": source_type,
        "source_index": source_index,
        "source_hash": source_hash,
        "paper_key_seen": paper_key_seen,
        "canonical_paper_key": canonical_paper_key,
        "title": _safe_str(metadata.get("title")),
        "doi": normalize_doi(metadata.get("doi")),
        "raw_doi": _safe_str(metadata.get("doi")),
        "canonical_key": canonical_paper_key,
        "canonical_key_source": identity.get("canonical_key_source", ""),
        "aliases": identity.get("aliases", []),
        "rejected_identity_values": identity.get("rejected_identity_values", []),
        "identity_diagnostics": identity.get("diagnostics", []),
        "metadata_confidence": _safe_str(metadata.get("metadata_confidence"), "low"),
    }


def _record_from_summary(summary: Mapping[str, Any], index: int) -> Dict[str, Any]:
    source_hash = _hash_summary(summary)
    metadata = _extract_metadata_from_summary(summary)
    canonical_key, identity_source = _identity_for_metadata(
        metadata,
        source_hash=source_hash,
        source_index=index,
        source_type="summary",
    )
    classification, must_use = _classification_from_record(summary, metadata)
    paper_key_seen = _safe_str(metadata.get("canonical_paper_key")) or build_paper_key(metadata)
    identity = normalize_paper_identity(
        metadata,
        source_hash=source_hash,
        source_index=index,
        allow_title_fallback=False,
    )
    return {
        "source_type": "summary",
        "source_index": index,
        "source_hash": source_hash,
        "paper_key_seen": paper_key_seen,
        "canonical_paper_key": canonical_key,
        "identity_source": identity_source,
        "metadata": metadata,
        "signals": _extract_signals_from_summary(summary),
        "abstract": _extract_abstract_from_summary(summary),
        "classification": classification,
        "must_use": must_use,
        "identity": identity,
        "diagnostics": list(identity.get("diagnostics", [])),
        "source_record": _source_record(
            source_type="summary",
            source_index=index,
            source_hash=source_hash,
            paper_key_seen=paper_key_seen,
            canonical_paper_key=canonical_key,
            metadata=metadata,
        ),
    }


def _record_from_artifact(artifact: Mapping[str, Any], index: int) -> Dict[str, Any]:
    source_hash = _hash_summary(artifact)
    metadata = _extract_metadata_from_artifact(artifact)
    canonical_key, identity_source = _identity_for_metadata(
        metadata,
        source_hash=source_hash,
        source_index=index,
        source_type="paper_artifact",
    )
    classification, must_use = _classification_from_record(_as_mapping(artifact.get("paper_info")), metadata)
    paper_key_seen = _safe_str(metadata.get("canonical_paper_key")) or build_paper_key(metadata)
    identity = normalize_paper_identity(
        metadata,
        source_hash=source_hash,
        source_index=index,
        allow_title_fallback=False,
    )
    return {
        "source_type": "paper_artifact",
        "source_index": index,
        "source_hash": source_hash,
        "paper_key_seen": paper_key_seen,
        "canonical_paper_key": canonical_key,
        "identity_source": identity_source,
        "metadata": metadata,
        "signals": _extract_signals_from_artifact(artifact),
        "abstract": _extract_abstract_from_artifact(artifact),
        "classification": classification,
        "must_use": must_use,
        "identity": identity,
        "diagnostics": list(identity.get("diagnostics", [])),
        "source_record": _source_record(
            source_type="paper_artifact",
            source_index=index,
            source_hash=source_hash,
            paper_key_seen=paper_key_seen,
            canonical_paper_key=canonical_key,
            metadata=metadata,
        ),
    }


def _merge_unique(*lists: Iterable[str]) -> List[str]:
    merged: List[str] = []
    seen: set[str] = set()
    for values in lists:
        for value in values:
            text = _safe_str(value)
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            merged.append(text)
    return merged


def _choose_metadata(records: Sequence[Dict[str, Any]], field: str) -> Any:
    ranked = sorted(
        records,
        key=lambda r: (
            _CONFIDENCE_RANK.get(str(r["metadata"].get("metadata_confidence") or "").lower(), 0),
            1 if r["source_type"] == "paper_artifact" else 0,
        ),
        reverse=True,
    )
    for record in ranked:
        value = record["metadata"].get(field)
        if value not in (None, "", []):
            return value
    return [] if field == "authors" else None


def _detect_suspicious_merge(records: Sequence[Dict[str, Any]], canonical_key: str) -> List[Dict[str, str]]:
    diagnostics: List[Dict[str, str]] = []
    titles_by_doi: Dict[str, set[str]] = defaultdict(set)
    doi_by_title: Dict[str, set[str]] = defaultdict(set)
    for record in records:
        metadata = record["metadata"]
        doi = normalize_doi(metadata.get("doi"))
        title_key = normalized_title_key(metadata.get("title"))
        if doi and title_key and title_key != "unknown_title":
            titles_by_doi[doi].add(title_key)
            doi_by_title[title_key].add(doi)

    for doi, titles in titles_by_doi.items():
        if len(titles) > 1:
            diagnostics.append({
                "type": "suspicious_merge_same_doi_different_title",
                "paper_key": canonical_key,
                "message": f"Canonical paper {canonical_key} has DOI {doi} with multiple normalized titles",
            })
    for title, dois in doi_by_title.items():
        if len(dois) > 1:
            diagnostics.append({
                "type": "suspicious_merge_same_title_different_doi",
                "paper_key": canonical_key,
                "message": f"Canonical paper {canonical_key} has title {title} with multiple DOI values",
            })
    return diagnostics


def _diagnostics_for_node(node: PaperNode) -> List[str]:
    diagnostics: List[str] = []
    label = node.title or node.paper_key
    if not node.title:
        diagnostics.append(f"missing_title for {label}")
    if not node.authors:
        diagnostics.append(f"missing_authors for {label}")
    if node.year is None:
        diagnostics.append(f"missing_year for {label}")
    if not node.themes:
        diagnostics.append(f"missing_themes for {label}")
    if not node.abstract_snippet:
        diagnostics.append(f"missing_abstract for {label}")
    return diagnostics


def _node_from_records(canonical_key: str, records: Sequence[Dict[str, Any]]) -> Tuple[PaperNode, List[Dict[str, str]]]:
    identity_source = records[0]["identity_source"] if records else ""
    title = _safe_str(_choose_metadata(records, "title"))
    authors = list(_choose_metadata(records, "authors") or [])
    year = _safe_year(_choose_metadata(records, "year"))
    doi = normalize_doi(_choose_metadata(records, "doi"))
    abstract = _first_text(*[record.get("abstract") for record in records])

    themes = _merge_unique(*[record["signals"]["themes"] for record in records])
    methods = _merge_unique(*[record["signals"]["methods"] for record in records])
    theories = _merge_unique(*[record["signals"]["theories"] for record in records])
    variables = _merge_unique(*[record["signals"]["variables"] for record in records])
    gaps = _merge_unique(*[record["signals"]["gaps"] for record in records])
    limitations = _merge_unique(*[record["signals"]["limitations"] for record in records])
    findings = _merge_unique(*[record["signals"]["findings"] for record in records])

    classification = max(
        (record["classification"] for record in records),
        key=lambda value: _CLASSIFICATION_ORDER.get(value, 0),
        default="support",
    )
    must_use = any(record["must_use"] for record in records) or classification == "core"
    source_hashes = [record["source_hash"] for record in records if record.get("source_hash")]
    metadata = {
        "canonical_paper_key": canonical_key,
        "doi": doi,
        "title": title,
        "authors": authors,
        "year": year,
    }
    aliases = _merge_unique(
        _alias_values(metadata, source_hashes[0] if source_hashes else ""),
        *[_alias_values(record["metadata"], record["source_hash"]) for record in records],
    )
    source_records = [dict(record["source_record"]) for record in records]
    diagnostics = _merge_unique(*[record.get("diagnostics", []) for record in records])

    node = PaperNode(
        paper_key=canonical_key,
        canonical_paper_key=canonical_key,
        identity_source=identity_source,
        aliases=aliases,
        source_records=source_records,
        source_summary_hash=source_hashes[0] if source_hashes else "",
        title=title,
        authors=authors,
        year=year,
        abstract_snippet=abstract[:500] if abstract else "",
        themes=themes,
        methods=methods,
        theories=theories,
        variables=variables,
        gaps=gaps,
        findings=findings,
        limitations=limitations,
        classification=classification,
        must_use=must_use,
        diagnostics=diagnostics,
    )
    node = PaperNode(
        **{**node.to_dict(), "diagnostics": _merge_unique(node.diagnostics, _diagnostics_for_node(node))}
    )
    return node, _detect_suspicious_merge(records, canonical_key)


def _extract_paper_node(summary: Dict[str, Any], index: int) -> PaperNode:
    """Extract a paper node from a single summary dict.

    Kept for tests/backward compatibility. Full builds should use
    build_literature_map() so duplicate sources can be merged.
    """
    record = _record_from_summary(summary, index)
    node, _diagnostics = _node_from_records(record["canonical_paper_key"], [record])
    return node


def _stream_confidence(paper_count: int, source_field_count: int) -> str:
    if paper_count >= 3 and source_field_count >= 2:
        return "high"
    if paper_count >= 2:
        return "medium"
    return "low"


def _build_research_streams(paper_nodes: Sequence[PaperNode]) -> List[Dict[str, Any]]:
    streams: Dict[str, Dict[str, Any]] = {}
    for node in paper_nodes:
        field_terms = {
            "themes": node.themes,
            "theories": node.theories,
            "variables": node.variables,
            "methods": node.methods,
            "gaps": [_normalize_term(item) for item in node.gaps],
            "findings": [_normalize_term(item) for item in node.findings],
        }
        for field, terms in field_terms.items():
            for term in terms:
                normalized = _normalize_term(term)
                if not normalized:
                    continue
                if is_noise_stream_label(normalized):
                    continue
                if field == "methods" and is_method_only_stream_label(normalized):
                    continue
                stream = streams.setdefault(
                    normalized,
                    {
                        "stream_name": normalized,
                        "normalized_stream_key": normalized,
                        "evidence_terms": [],
                        "paper_keys": [],
                        "source_fields": [],
                        "confidence": "low",
                        "thin_stream": True,
                    },
                )
                if term not in stream["evidence_terms"]:
                    stream["evidence_terms"].append(term)
                if node.paper_key not in stream["paper_keys"]:
                    stream["paper_keys"].append(node.paper_key)
                if field not in stream["source_fields"]:
                    stream["source_fields"].append(field)

    sorted_streams = sorted(
        streams.values(),
        key=lambda s: (
            stream_promotion_tier(s["stream_name"], s["source_fields"], len(set(s["paper_keys"]))),
            len(s["paper_keys"]),
            len(s["source_fields"]),
            s["stream_name"],
        ),
        reverse=True,
    )
    for stream in sorted_streams:
        paper_count = len(set(stream["paper_keys"]))
        stream["promotion_tier"] = stream_promotion_tier(
            stream["stream_name"],
            stream["source_fields"],
            paper_count,
        )
        stream["thin_stream"] = paper_count < 2 or stream["promotion_tier"] <= 0
        stream["confidence"] = _stream_confidence(paper_count, len(stream["source_fields"]))
    return sorted_streams


def build_literature_map(
    summaries: Sequence[Dict[str, Any]],
    job_id: str,
    paper_artifacts: Sequence[Dict[str, Any]] | None = None,
) -> LiteratureMap:
    """Build a literature_map from summaries and optional paper artifacts.

    Source records are losslessly preserved while paper_nodes are canonicalized
    to one node per canonical paper.
    """
    grouped_records: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    source_hashes: List[str] = []
    blocking_diagnostics: List[Dict[str, str]] = []

    for i, summary in enumerate(summaries):
        if not isinstance(summary, Mapping):
            blocking_diagnostics.append({
                "type": "invalid_summary_format",
                "index": str(i),
                "message": f"Summary at index {i} is not a dict",
            })
            continue
        source_hash = _hash_summary(summary)
        source_hashes.append(source_hash)
        try:
            record = _record_from_summary(summary, i)
            grouped_records[record["canonical_paper_key"]].append(record)
        except Exception as exc:
            blocking_diagnostics.append({
                "type": "extraction_failure",
                "index": str(i),
                "message": str(exc),
            })

    for i, artifact in enumerate(paper_artifacts or []):
        if not isinstance(artifact, Mapping):
            blocking_diagnostics.append({
                "type": "invalid_paper_artifact_format",
                "index": str(i),
                "message": f"Paper artifact at index {i} is not a dict",
            })
            continue
        source_hash = _hash_summary(artifact)
        source_hashes.append(source_hash)
        try:
            record = _record_from_artifact(artifact, i)
            grouped_records[record["canonical_paper_key"]].append(record)
        except Exception as exc:
            blocking_diagnostics.append({
                "type": "paper_artifact_extraction_failure",
                "index": str(i),
                "message": str(exc),
            })

    paper_nodes: List[PaperNode] = []
    for canonical_key in sorted(grouped_records):
        node, suspicious = _node_from_records(canonical_key, grouped_records[canonical_key])
        paper_nodes.append(node)
        for record in grouped_records[canonical_key]:
            identity_diagnostic = _identity_blocking_diagnostic(record)
            if identity_diagnostic:
                blocking_diagnostics.append(identity_diagnostic)
        blocking_diagnostics.extend(suspicious)

    paper_classification: Dict[str, List[str]] = {
        "core": [],
        "background_only": [],
        "peripheral": [],
        "support": [],
        "unknown": [],
    }
    for node in paper_nodes:
        cls = node.classification if node.classification in paper_classification else "support"
        paper_classification[cls].append(node.paper_key)

    return LiteratureMap(
        created_from_job_id=job_id,
        created_at=_utc_now_iso(),
        source_summary_hashes=source_hashes,
        paper_nodes=paper_nodes,
        research_streams=_build_research_streams(paper_nodes),
        theoretical_dimensions=[],
        method_clusters=[],
        empirical_contexts=[],
        key_tensions=[],
        candidate_gaps=[
            {"paper_key": node.paper_key, "gap": gap}
            for node in paper_nodes
            for gap in node.gaps
        ],
        paper_classification=paper_classification,
        blocking_diagnostics=blocking_diagnostics,
    )

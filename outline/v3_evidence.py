"""Deterministic Outline Intelligence v3 evidence projections.

This module is intentionally provider-free.  It reads Stage 1 summaries,
resolves paper identity through explicit stable keys, and emits the shared
evidence artifacts consumed by every later candidate.  A missing identity or
failed source summary is recorded as a blocking diagnostic; it is never
silently converted into a paper node or an exclusion.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from services.paper_identity import (
    normalize_doi,
    normalized_title_key,
    normalize_paper_identity,
    title_author_year_key_from_paper,
)
from summary_schema import get_ai_summary

from outline.v3_models import (
    CoverageContract,
    GlobalCorpusLedger,
    GlobalCorpusLedgerEntry,
    MultiViewMatrix,
    MultiViewMatrixRow,
    OutlineEvidenceView,
    OutlineEvidenceViews,
    ReviewIntent,
    compute_v3_hash,
)


MATRIX_DIMENSIONS: Tuple[str, ...] = (
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
)

_CLASSIFICATION_PRIORITY = {
    "unknown": 0,
    "peripheral": 1,
    "background_only": 1,
    "background": 1,
    "support": 2,
    "core": 3,
}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _stable_unique(values: Iterable[Any]) -> List[str]:
    by_key: Dict[str, str] = {}
    for value in values:
        text = _safe_text(value)
        if not text:
            continue
        by_key.setdefault(text.casefold(), text)
    return [by_key[key] for key in sorted(by_key)]


def _text_values(value: Any) -> List[str]:
    """Project literal strings without inventing semantic labels."""

    if value is None:
        return []
    if isinstance(value, Mapping):
        items: List[Any] = []
        for key in sorted(value, key=lambda item: str(item)):
            items.extend(_text_values(value[key]))
        return _stable_unique(items)
    if isinstance(value, (list, tuple, set)):
        items: List[Any] = []
        for item in value:
            items.extend(_text_values(item))
        return _stable_unique(items)
    text = _safe_text(value)
    return [text] if text else []


def _safe_year(value: Any) -> Optional[int]:
    if value is None:
        return None
    match = re.search(r"\b(19|20)\d{2}\b", _safe_text(value))
    if not match:
        return None
    return int(match.group(0))


def _normalize_alias_token(value: Any) -> str:
    text = _safe_text(value)
    if not text:
        return ""
    doi = normalize_doi(text)
    if doi:
        return doi
    return re.sub(r"\s+", " ", text).strip().casefold()


def _canonical_target(value: Any) -> str:
    text = _safe_text(value)
    doi = normalize_doi(text)
    return doi or re.sub(r"\s+", " ", text).strip()


def _normalise_crosswalk(
    alias_crosswalk: Optional[Any],
) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    """Normalize mapping/list crosswalk forms and report collisions."""

    pairs: List[Tuple[Any, Any]] = []
    if isinstance(alias_crosswalk, Mapping):
        for alias, target in alias_crosswalk.items():
            if isinstance(target, Mapping):
                target_value = target.get("canonical_paper_key") or target.get("canonical") or target.get("target")
            else:
                target_value = target
            pairs.append((alias, target_value))
    elif isinstance(alias_crosswalk, Sequence) and not isinstance(alias_crosswalk, (str, bytes)):
        for item in alias_crosswalk:
            if not isinstance(item, Mapping):
                continue
            alias = item.get("alias") or item.get("from") or item.get("paper_key")
            target = item.get("canonical_paper_key") or item.get("canonical") or item.get("to") or item.get("target")
            pairs.append((alias, target))

    normalized: Dict[str, str] = {}
    diagnostics: List[Dict[str, Any]] = []
    for alias, target in pairs:
        alias_key = _normalize_alias_token(alias)
        target_key = _canonical_target(target)
        if not alias_key or not target_key:
            diagnostics.append({
                "code": "invalid_alias_crosswalk_entry",
                "severity": "blocking",
                "message": "Alias crosswalk entries require a non-empty alias and canonical target.",
            })
            continue
        previous = normalized.get(alias_key)
        if previous and previous != target_key:
            diagnostics.append({
                "code": "conflicting_alias_crosswalk",
                "severity": "blocking",
                "alias": alias_key,
                "targets": sorted({previous, target_key}),
                "message": f"Alias {alias_key!r} maps to more than one canonical paper key.",
            })
            continue
        normalized[alias_key] = target_key

    return normalized, diagnostics


def _resolve_crosswalk(value: str, crosswalk: Mapping[str, str]) -> Tuple[str, bool, str]:
    current = _canonical_target(value)
    if not current:
        return "", False, ""
    seen: set[str] = set()
    changed = False
    while _normalize_alias_token(current) in crosswalk:
        token = _normalize_alias_token(current)
        if token in seen:
            return "", True, "alias_crosswalk_cycle"
        seen.add(token)
        next_value = _canonical_target(crosswalk[token])
        if not next_value:
            return "", True, "alias_crosswalk_empty_target"
        current = next_value
        changed = True
    return current, changed, ""


def _summary_metadata(summary: Mapping[str, Any], ai_summary: Mapping[str, Any]) -> Dict[str, Any]:
    paper_info = _as_mapping(summary.get("paper_info"))
    ai_metadata = _as_mapping(ai_summary.get("paper_metadata"))
    return {
        "title": _safe_text(paper_info.get("title") or ai_metadata.get("title") or summary.get("title")),
        "authors": _text_values(paper_info.get("authors") or ai_metadata.get("authors") or summary.get("authors")),
        "year": _safe_year(paper_info.get("year") or ai_metadata.get("year") or summary.get("year")),
        "doi": _safe_text(paper_info.get("doi") or ai_metadata.get("doi") or summary.get("doi")),
        "canonical_paper_key": _safe_text(
            paper_info.get("canonical_paper_key") or summary.get("canonical_paper_key")
        ),
        "source_paper_id": _safe_text(
            paper_info.get("source_paper_id") or summary.get("source_paper_id")
        ),
        "paper_key_aliases": _text_values(
            paper_info.get("paper_key_aliases")
            or paper_info.get("aliases")
            or summary.get("paper_key_aliases")
            or summary.get("aliases")
        ),
        "classification": _safe_text(
            paper_info.get("classification")
            or summary.get("classification")
            or "support"
        ).casefold(),
        "must_use": str(
            paper_info.get("must_use")
            or summary.get("must_use")
            or ""
        ).strip().casefold() in {"true", "1", "yes", "on"},
    }


def _identity_candidates(
    metadata: Mapping[str, Any],
    crosswalk: Optional[Mapping[str, str]] = None,
) -> List[Tuple[str, str]]:
    candidates: List[Tuple[str, str]] = []
    explicit = _safe_text(metadata.get("canonical_paper_key"))
    if explicit:
        candidates.append(("canonical_paper_key", explicit))
    doi = normalize_doi(metadata.get("doi"))
    if doi:
        candidates.append(("normalized_doi", doi))
    source_id = _safe_text(metadata.get("source_paper_id"))
    if source_id:
        candidates.append(("source_paper_id", source_id))
    title_author_year = title_author_year_key_from_paper(metadata)
    if title_author_year:
        candidates.append(("title_author_year", title_author_year))
    # An explicit crosswalk is allowed to make an otherwise unstable alias
    # usable, but an un-mapped title-only value must remain blocking.
    crosswalk_values = crosswalk or {}
    for alias in [
        *list(metadata.get("paper_key_aliases") or []),
        normalized_title_key(metadata.get("title")),
    ]:
        alias_token = _normalize_alias_token(alias)
        if alias_token and alias_token in crosswalk_values:
            candidates.append(("alias_crosswalk", alias))
    return candidates


def _resolve_identity(
    metadata: Mapping[str, Any],
    crosswalk: Mapping[str, str],
) -> Tuple[str, str, List[str], List[Dict[str, Any]]]:
    candidates = _identity_candidates(metadata, crosswalk)
    aliases = _stable_unique([
        metadata.get("canonical_paper_key"),
        metadata.get("doi"),
        metadata.get("source_paper_id"),
        normalized_title_key(metadata.get("title")),
        title_author_year_key_from_paper(metadata),
        *list(metadata.get("paper_key_aliases") or []),
    ])

    diagnostics: List[Dict[str, Any]] = []
    resolved_candidates: List[Tuple[str, str, bool]] = []
    crosswalk_used = False
    for source, value in candidates:
        resolved, used, error = _resolve_crosswalk(value, crosswalk)
        if error:
            diagnostics.append({
                "code": error,
                "severity": "blocking",
                "value": _safe_text(value),
                "message": f"Unable to resolve identity value {value!r} through the alias crosswalk.",
            })
            continue
        if resolved:
            resolved_candidates.append((source, resolved, used))
            crosswalk_used = crosswalk_used or used

    if not resolved_candidates:
        # A title-only or otherwise unstable record must not be assigned a
        # source-hash key.  That would make list position part of identity.
        diagnostics.append({
            "code": "missing_stable_paper_identity",
            "severity": "blocking",
            "title": _safe_text(metadata.get("title")),
            "source_paper_id": _safe_text(metadata.get("source_paper_id")),
            "message": "Paper requires a canonical key, DOI, source paper ID, or title-author-year identity.",
        })
        return "", "", aliases, diagnostics

    chosen_source, chosen_key, _chosen_used = resolved_candidates[0]

    identity_source = "alias_crosswalk" if crosswalk_used else chosen_source
    # Include all stable identity values as aliases so the join is inspectable.
    aliases = _stable_unique([*aliases, *[value for _source, value, _used in resolved_candidates]])
    return chosen_key, identity_source, aliases, diagnostics


def _field_sources(source_fields: Dict[str, List[str]], field: str, *paths: str) -> None:
    if paths:
        source_fields[field] = _stable_unique(paths)


def _extract_view(
    summary: Mapping[str, Any],
    source_summary_hash: str,
    crosswalk: Mapping[str, str],
) -> Tuple[Optional[OutlineEvidenceView], List[Dict[str, Any]]]:
    ai_summary = get_ai_summary(summary)
    metadata = _summary_metadata(summary, ai_summary)
    paper_key, identity_source, aliases, identity_diagnostics = _resolve_identity(metadata, crosswalk)
    diagnostics: List[str] = []
    blocking: List[Dict[str, Any]] = []
    if identity_diagnostics:
        for diagnostic in identity_diagnostics:
            diagnostics.append(str(diagnostic.get("code") or "identity_diagnostic"))
            diagnostic = dict(diagnostic)
            diagnostic["source_summary_hash"] = source_summary_hash
            if paper_key:
                diagnostic["paper_key"] = paper_key
            blocking.append(diagnostic)
    if not paper_key:
        return None, blocking

    raw_status = summary.get("status")
    if raw_status not in (None, "", "success", "completed"):
        blocking.append({
            "code": "source_summary_not_success",
            "severity": "blocking",
            "paper_key": paper_key,
            "source_summary_hash": source_summary_hash,
            "status": _safe_text(raw_status),
            "message": "Only successful Stage 1 summaries may enter the v3 evidence layer.",
        })
        # Preserve the failure as evidence, but do not let failed semantic
        # output participate in the corpus ledger or matrix.
        return None, blocking

    routing = _as_mapping(ai_summary.get("routing"))
    core = _as_mapping(ai_summary.get("core_analysis"))
    specialized = _as_mapping(ai_summary.get("specialized_details"))
    empirical = _as_mapping(specialized.get("empirical"))
    review = _as_mapping(specialized.get("review"))
    conceptual = _as_mapping(specialized.get("conceptual"))
    quality = _as_mapping(ai_summary.get("quality_audit"))
    variables = _as_mapping(empirical.get("core_variables"))

    source_fields: Dict[str, List[str]] = {}

    research_questions = _text_values(empirical.get("research_questions_or_hypotheses"))
    if research_questions:
        _field_sources(source_fields, "research_questions", "ai_summary.specialized_details.empirical.research_questions_or_hypotheses")

    theories = _text_values(core.get("theoretical_framework"))
    if theories:
        _field_sources(source_fields, "theories", "ai_summary.core_analysis.theoretical_framework")
    theoretical_contributions = _text_values(conceptual.get("theoretical_contributions"))
    if theoretical_contributions:
        theories = _stable_unique([*theories, *theoretical_contributions])
        _field_sources(source_fields, "theories", "ai_summary.specialized_details.conceptual.theoretical_contributions")

    constructs: List[str] = []
    construct_paths: List[str] = []
    for role in sorted(variables):
        values = _text_values(variables[role])
        if values:
            constructs.extend(values)
            construct_paths.append(f"ai_summary.specialized_details.empirical.core_variables.{role}")
    constructs = _stable_unique(constructs)
    if constructs:
        _field_sources(source_fields, "constructs", *construct_paths)

    mechanisms: List[str] = []
    mechanism_paths: List[str] = []
    for role in ("mediators", "moderators"):
        values = _text_values(variables.get(role))
        if values:
            mechanisms.extend(values)
            mechanism_paths.append(f"ai_summary.specialized_details.empirical.core_variables.{role}")
    # Some Stage 1 producers preserve an explicit mechanisms field while
    # remaining schema-compatible.  It is accepted only when present as a
    # literal canonical empirical field; no mechanism is inferred from prose.
    explicit_mechanisms = _text_values(empirical.get("mechanisms"))
    if explicit_mechanisms:
        mechanisms.extend(explicit_mechanisms)
        mechanism_paths.append("ai_summary.specialized_details.empirical.mechanisms")
    mechanisms = _stable_unique(mechanisms)
    if mechanisms:
        _field_sources(source_fields, "mechanisms", *mechanism_paths)

    method = _text_values(core.get("methodology"))
    if method:
        _field_sources(source_fields, "method", "ai_summary.core_analysis.methodology")
    analysis_technique = _text_values(empirical.get("analysis_technique"))
    if analysis_technique:
        method = _stable_unique([*method, *analysis_technique])
        _field_sources(source_fields, "method", "ai_summary.specialized_details.empirical.analysis_technique")
    if review.get("review_type"):
        method = _stable_unique([*method, *_text_values(review.get("review_type"))])
        _field_sources(source_fields, "method", "ai_summary.specialized_details.review.review_type")
    if review.get("synthesis_approach"):
        method = _stable_unique([*method, *_text_values(review.get("synthesis_approach"))])
        _field_sources(source_fields, "method", "ai_summary.specialized_details.review.synthesis_approach")

    sample_or_context = _text_values(empirical.get("sample_characteristics_or_context"))
    if sample_or_context:
        _field_sources(source_fields, "sample_or_context", "ai_summary.specialized_details.empirical.sample_characteristics_or_context")
    data_source = _text_values(empirical.get("data_source_and_size"))
    if data_source:
        sample_or_context = _stable_unique([*sample_or_context, *data_source])
        _field_sources(source_fields, "sample_or_context", "ai_summary.specialized_details.empirical.data_source_and_size")

    findings = _text_values(core.get("findings"))
    if findings:
        _field_sources(source_fields, "findings", "ai_summary.core_analysis.findings")
    conclusions = _text_values(core.get("conclusions"))
    if conclusions:
        _field_sources(source_fields, "conclusions", "ai_summary.core_analysis.conclusions")
    limitations = _text_values(core.get("limitations"))
    if limitations:
        _field_sources(source_fields, "limitations", "ai_summary.core_analysis.limitations")
    research_gaps = _text_values(core.get("research_gap"))
    if research_gaps:
        _field_sources(source_fields, "research_gaps", "ai_summary.core_analysis.research_gap")
    future_directions = _text_values(core.get("future_research_directions"))
    if future_directions:
        _field_sources(source_fields, "future_directions", "ai_summary.core_analysis.future_research_directions")
    relevance = _text_values(core.get("relevance"))
    if relevance:
        _field_sources(source_fields, "relevance", "ai_summary.core_analysis.relevance")

    if bool(quality.get("needs_manual_review")):
        diagnostics.append("source_quality_manual_review")
    for missing in _text_values(quality.get("missing_critical_fields")):
        diagnostics.append(f"missing_source_field:{missing}")
    for conflict in _text_values(quality.get("conflict_flags")):
        diagnostics.append(f"source_quality_conflict:{conflict}")

    classification = metadata["classification"] or "support"
    if classification not in _CLASSIFICATION_PRIORITY:
        classification = "support"
    paper_type = _safe_text(routing.get("paper_type"))

    view = OutlineEvidenceView(
        paper_key=paper_key,
        canonical_paper_key=paper_key,
        title=_safe_text(metadata.get("title")),
        authors=_stable_unique(metadata.get("authors") or []),
        year=metadata.get("year"),
        paper_type=paper_type,
        research_questions=research_questions,
        theories=theories,
        constructs=constructs,
        mechanisms=mechanisms,
        method=method,
        sample_or_context=sample_or_context,
        findings=findings,
        conclusions=conclusions,
        limitations=limitations,
        research_gaps=research_gaps,
        future_directions=future_directions,
        relevance=relevance,
        source_summary_hash=source_summary_hash,
        source_summary_hashes=[source_summary_hash],
        doi=normalize_doi(metadata.get("doi")),
        source_paper_id=_safe_text(metadata.get("source_paper_id")),
        aliases=aliases,
        identity_source=identity_source,
        source_fields=source_fields,
        classification=classification,
        must_use=bool(metadata.get("must_use")) or classification == "core",
        diagnostics=_stable_unique(diagnostics),
    )
    return view, blocking


def _merge_views(left: OutlineEvidenceView, right: OutlineEvidenceView) -> Tuple[OutlineEvidenceView, List[Dict[str, Any]]]:
    """Merge duplicate source records without depending on input order."""

    diagnostics: List[Dict[str, Any]] = []

    def choose_text(field: str) -> str:
        values = _stable_unique([getattr(left, field), getattr(right, field)])
        if len(values) > 1:
            diagnostics.append({
                "code": f"conflicting_{field}_across_sources",
                "severity": "blocking",
                "paper_key": left.paper_key,
                "values": values,
                "message": f"Duplicate source records disagree on {field}; deterministic minimum retained.",
            })
        return values[0] if values else ""

    def choose_year() -> Optional[int]:
        values = sorted({value for value in (left.year, right.year) if value is not None})
        if len(values) > 1:
            diagnostics.append({
                "code": "conflicting_year_across_sources",
                "severity": "blocking",
                "paper_key": left.paper_key,
                "values": values,
                "message": "Duplicate source records disagree on year; deterministic minimum retained.",
            })
        return values[0] if values else None

    classification = max(
        (left.classification, right.classification),
        key=lambda value: (_CLASSIFICATION_PRIORITY.get(value, 0), value),
    )
    hashes = _stable_unique([*left.source_summary_hashes, *right.source_summary_hashes, left.source_summary_hash, right.source_summary_hash])
    hashes = [value for value in hashes if value]
    source_hash = hashes[0] if len(hashes) == 1 else compute_v3_hash({"source_summary_hashes": hashes})
    merged_fields: Dict[str, List[str]] = {}
    for field in (
        "research_questions",
        "theories",
        "constructs",
        "mechanisms",
        "method",
        "sample_or_context",
        "findings",
        "conclusions",
        "limitations",
        "research_gaps",
        "future_directions",
        "relevance",
        "authors",
        "aliases",
        "diagnostics",
    ):
        merged_fields[field] = _stable_unique([*getattr(left, field), *getattr(right, field)])
    source_fields: Dict[str, List[str]] = defaultdict(list)
    for source in (left.source_fields, right.source_fields):
        for field, paths in source.items():
            source_fields[field].extend(paths)

    merged = OutlineEvidenceView(
        paper_key=left.paper_key,
        canonical_paper_key=left.paper_key,
        title=choose_text("title"),
        authors=merged_fields["authors"],
        year=choose_year(),
        paper_type=choose_text("paper_type"),
        research_questions=merged_fields["research_questions"],
        theories=merged_fields["theories"],
        constructs=merged_fields["constructs"],
        mechanisms=merged_fields["mechanisms"],
        method=merged_fields["method"],
        sample_or_context=merged_fields["sample_or_context"],
        findings=merged_fields["findings"],
        conclusions=merged_fields["conclusions"],
        limitations=merged_fields["limitations"],
        research_gaps=merged_fields["research_gaps"],
        future_directions=merged_fields["future_directions"],
        relevance=merged_fields["relevance"],
        source_summary_hash=source_hash,
        source_summary_hashes=hashes,
        doi=choose_text("doi"),
        source_paper_id=choose_text("source_paper_id"),
        aliases=merged_fields["aliases"],
        identity_source=choose_text("identity_source"),
        source_fields={field: _stable_unique(paths) for field, paths in source_fields.items()},
        classification=classification,
        must_use=left.must_use or right.must_use or classification == "core",
        diagnostics=merged_fields["diagnostics"],
    )
    return merged, diagnostics


def build_outline_evidence_views(
    summaries: Iterable[Mapping[str, Any]],
    job_id: str = "",
    alias_crosswalk: Optional[Any] = None,
    *,
    strict_status: bool = False,
) -> OutlineEvidenceViews:
    """Build deterministic evidence views from canonical Stage 1 summaries.

    ``job_id`` is retained only as provenance metadata.  It is deliberately
    excluded from the content hash so the same corpus produces the same
    artifact hash in different workspaces.  ``strict_status`` treats a missing
    status as blocking; the default accepts older in-memory test fixtures while
    still blocking an explicitly failed status.
    """

    # Permit the natural shorthand build_outline_evidence_views(summaries,
    # alias_crosswalk) without making the compatibility job-id parameter
    # ambiguous for callers that use a mapping as the second positional arg.
    if isinstance(job_id, Mapping) and alias_crosswalk is None:
        alias_crosswalk = job_id
        job_id = ""

    crosswalk, crosswalk_diagnostics = _normalise_crosswalk(alias_crosswalk)
    groups: Dict[str, List[OutlineEvidenceView]] = defaultdict(list)
    source_hashes: List[str] = []
    blocking: List[Dict[str, Any]] = list(crosswalk_diagnostics)

    for summary in summaries:
        if not isinstance(summary, Mapping):
            blocking.append({
                "code": "invalid_summary_type",
                "severity": "blocking",
                "message": "Stage 1 summary must be a mapping.",
            })
            continue
        source_hash = compute_v3_hash(summary)
        source_hashes.append(source_hash)
        if strict_status and summary.get("status") in (None, ""):
            blocking.append({
                "code": "missing_summary_status",
                "severity": "blocking",
                "source_summary_hash": source_hash,
                "message": "Strict v3 projection requires an explicit successful Stage 1 status.",
            })
            continue
        view, diagnostics = _extract_view(summary, source_hash, crosswalk)
        blocking.extend(diagnostics)
        if view is not None:
            groups[view.paper_key].append(view)

    views: List[OutlineEvidenceView] = []
    for paper_key in sorted(groups):
        records = sorted(groups[paper_key], key=lambda item: item.view_hash)
        merged = records[0]
        for record in records[1:]:
            merged, merge_diagnostics = _merge_views(merged, record)
            blocking.extend(merge_diagnostics)
        views.append(merged)

    return OutlineEvidenceViews(
        created_from_job_id=str(job_id or ""),
        views=views,
        source_summary_hashes=_stable_unique(source_hashes),
        alias_crosswalk=crosswalk,
        blocking_diagnostics=blocking,
    )


def _view_dimensions(view: OutlineEvidenceView) -> Dict[str, List[str]]:
    return {
        "theory": list(view.theories),
        "construct": list(view.constructs),
        "mechanism": list(view.mechanisms),
        "context": list(view.sample_or_context),
        "method": list(view.method),
        "finding": list(view.findings),
        "limitation": list(view.limitations),
        "gap": list(view.research_gaps),
        "year": [str(view.year)] if view.year is not None else [],
        "development": list(view.future_directions),
    }


def _normalize_matrix_label(value: Any, dimension: str, aliases: Mapping[str, str]) -> str:
    text = re.sub(r"\s+", " ", _safe_text(value)).strip().casefold()
    if not text:
        return ""
    text = re.sub(r"[\u2010-\u2015\-]+", "-", text)
    text = re.sub(r"\s+", " ", text)
    dimension_key = f"{dimension}:{text}"
    return _normalize_alias_token(aliases.get(dimension_key, aliases.get(text, text)))


def build_multi_view_matrix(
    evidence: OutlineEvidenceViews | GlobalCorpusLedger | Sequence[OutlineEvidenceView],
    aliases: Optional[Mapping[str, Any]] = None,
) -> MultiViewMatrix:
    """Build the non-mutually-exclusive paper × dimension matrix."""

    if isinstance(evidence, OutlineEvidenceViews):
        views = list(evidence.views)
        source_hashes = list(evidence.source_summary_hashes)
        blocking = list(evidence.blocking_diagnostics)
    elif isinstance(evidence, GlobalCorpusLedger):
        rows = []
        for entry in evidence.entries:
            rows.append(MultiViewMatrixRow(
                paper_key=entry.paper_key,
                dimensions={key: list(value) for key, value in entry.dimensions.items()},
                source_summary_hash=entry.source_summary_hash,
            ))
        return MultiViewMatrix(
            rows=sorted(rows, key=lambda item: item.paper_key),
            source_summary_hashes=list(evidence.source_summary_hashes),
            blocking_diagnostics=list(evidence.blocking_diagnostics),
        )
    else:
        views = [item for item in evidence if isinstance(item, OutlineEvidenceView)]
        source_hashes = [view.source_summary_hash for view in views]
        blocking = []

    alias_map: Dict[str, str] = {}
    if isinstance(aliases, Mapping):
        for key, value in aliases.items():
            if isinstance(value, Mapping):
                dimension = _safe_text(key).casefold()
                for alias, target in value.items():
                    alias_map[f"{dimension}:{_normalize_alias_token(alias)}"] = _normalize_alias_token(target)
            else:
                alias_map[_normalize_alias_token(key)] = _normalize_alias_token(value)

    rows: List[MultiViewMatrixRow] = []
    for view in sorted(views, key=lambda item: item.paper_key):
        raw_dimensions = _view_dimensions(view)
        dimensions: Dict[str, List[str]] = {}
        for dimension in MATRIX_DIMENSIONS:
            dimensions[dimension] = _stable_unique(
                _normalize_matrix_label(value, dimension, alias_map)
                for value in raw_dimensions.get(dimension, [])
            )
        rows.append(MultiViewMatrixRow(
            paper_key=view.paper_key,
            dimensions=dimensions,
            source_summary_hash=view.source_summary_hash,
        ))

    return MultiViewMatrix(
        dimensions=list(MATRIX_DIMENSIONS),
        rows=rows,
        normalization_aliases=alias_map,
        source_summary_hashes=_stable_unique(source_hashes),
        blocking_diagnostics=blocking,
    )


def _classification_family(classification: str) -> str:
    value = _safe_text(classification).casefold()
    if value == "background_only":
        return "background"
    if value in {"core", "support", "background", "peripheral"}:
        return value
    return "unknown"


def _compact_record(view: OutlineEvidenceView) -> str:
    parts: List[str] = []

    def append(label: str, values: Sequence[Any], limit: int = 280) -> None:
        text = "; ".join(_stable_unique(values))
        if text:
            parts.append(f"{label}: {text[:limit]}")

    append("title", [view.title])
    append("year", [view.year] if view.year is not None else [])
    append("type", [view.paper_type])
    append("theory", view.theories)
    append("construct", view.constructs)
    append("mechanism", view.mechanisms)
    append("method", view.method)
    append("context", view.sample_or_context)
    append("finding", view.findings)
    append("gap", view.research_gaps)
    append("development", view.future_directions)
    return " | ".join(parts)[:1400]


def build_global_corpus_ledger(
    evidence: OutlineEvidenceViews | Sequence[OutlineEvidenceView],
    *,
    classification_overrides: Optional[Mapping[str, Any]] = None,
    excluded_with_reasons: Optional[Mapping[str, str]] = None,
) -> GlobalCorpusLedger:
    """Build one compact, complete ledger without creating outline chapters."""

    if isinstance(evidence, OutlineEvidenceViews):
        views = list(evidence.views)
        source_hashes = list(evidence.source_summary_hashes)
        blocking = list(evidence.blocking_diagnostics)
    else:
        views = [item for item in evidence if isinstance(item, OutlineEvidenceView)]
        source_hashes = [view.source_summary_hash for view in views]
        blocking = []

    overrides = classification_overrides or {}
    exclusions = excluded_with_reasons or {}
    entries: List[GlobalCorpusLedgerEntry] = []
    for view in sorted(views, key=lambda item: item.paper_key):
        override = overrides.get(view.paper_key)
        if isinstance(override, Mapping):
            classification = _safe_text(override.get("classification")) or view.classification
            must_use = bool(override.get("must_use", view.must_use))
        elif override not in (None, ""):
            classification = _safe_text(override)
            must_use = view.must_use
        else:
            classification = view.classification
            must_use = view.must_use
        classification = classification.casefold() or "unknown"
        if classification not in {"core", "support", "background", "background_only", "peripheral", "unknown", "excluded"}:
            classification = "unknown"
        family = _classification_family(classification)

        exclusion_reason = _safe_text(exclusions.get(view.paper_key))
        if classification == "excluded" and not exclusion_reason:
            exclusion_reason = "explicitly_excluded_by_input_contract"
        if exclusion_reason:
            assignment_status = "excluded_with_reason"
        elif classification in {"background", "background_only", "peripheral"}:
            assignment_status = "background_only"
        elif view.diagnostics:
            assignment_status = "manual_review_required"
        else:
            assignment_status = "assigned"

        raw_dimensions = _view_dimensions(view)
        topics = _stable_unique([
            *view.theories,
            *view.constructs,
            *view.mechanisms,
            *view.research_gaps,
        ])[:12]
        entries.append(GlobalCorpusLedgerEntry(
            paper_key=view.paper_key,
            compact_record=_compact_record(view),
            classification=classification,
            classification_family=family,
            must_use=must_use or classification == "core",
            assignment_status=assignment_status,
            exclusion_reason=exclusion_reason,
            source_summary_hash=view.source_summary_hash,
            dimensions={key: _stable_unique(value) for key, value in raw_dimensions.items()},
            diagnostic_candidate_topics=topics,
            diagnostics=list(view.diagnostics),
        ))

    return GlobalCorpusLedger(
        entries=entries,
        source_summary_hashes=_stable_unique(source_hashes),
        blocking_diagnostics=blocking,
    )


def build_review_intent(value: Optional[Any] = None, **overrides: Any) -> ReviewIntent:
    """Create an explicit intent object; absent values remain empty."""

    if isinstance(value, ReviewIntent):
        payload = value.to_dict()
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        payload = {}
    payload.update(overrides)
    return ReviewIntent.from_dict(payload)


def build_coverage_contract(
    ledger: GlobalCorpusLedger,
    intent: Optional[ReviewIntent] = None,
) -> CoverageContract:
    """Build the shared paper-assignment contract used by all candidates."""

    entries = sorted(ledger.entries, key=lambda item: item.paper_key)
    assignment_statuses = {entry.paper_key: entry.assignment_status for entry in entries}
    reasons = {
        entry.paper_key: entry.exclusion_reason
        for entry in entries
        if entry.exclusion_reason
    }
    must_use = [entry.paper_key for entry in entries if entry.must_use]
    if intent is not None:
        required = _stable_unique(["must_cover", *intent.must_cover])
    else:
        required = [
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
        ]
    return CoverageContract(
        corpus_paper_keys=[entry.paper_key for entry in entries],
        must_use_paper_keys=must_use,
        required_dimensions=required,
        assignment_statuses=assignment_statuses,
        unassigned_reasons=reasons,
        source_summary_hashes=list(ledger.source_summary_hashes),
    )


def shard_outline_evidence_views(
    evidence: OutlineEvidenceViews,
    shard_size: int,
) -> List[OutlineEvidenceViews]:
    """Partition by stable paper key while preserving the same artifact schema."""

    if shard_size <= 0:
        raise ValueError("shard_size must be positive")
    views = sorted(evidence.views, key=lambda item: item.paper_key)
    if not views:
        return [OutlineEvidenceViews(
            created_from_job_id=evidence.created_from_job_id,
            views=[],
            source_summary_hashes=[],
            alias_crosswalk=evidence.alias_crosswalk,
            blocking_diagnostics=evidence.blocking_diagnostics,
            shard_id="1/1",
            shard_count=1,
        )]
    shard_count = (len(views) + shard_size - 1) // shard_size
    shards: List[OutlineEvidenceViews] = []
    for index in range(shard_count):
        subset = views[index * shard_size:(index + 1) * shard_size]
        subset_hashes = _stable_unique(
            source_hash
            for view in subset
            for source_hash in (view.source_summary_hashes or [view.source_summary_hash])
        )
        shards.append(OutlineEvidenceViews(
            created_from_job_id=evidence.created_from_job_id,
            views=subset,
            source_summary_hashes=subset_hashes,
            alias_crosswalk=evidence.alias_crosswalk,
            blocking_diagnostics=evidence.blocking_diagnostics if index == 0 else [],
            shard_id=f"{index + 1}/{shard_count}",
            shard_count=shard_count,
        ))
    return shards


def merge_outline_evidence_shards(shards: Sequence[OutlineEvidenceViews]) -> OutlineEvidenceViews:
    """Merge technical shards and verify duplicate keys are identical."""

    if not shards:
        return OutlineEvidenceViews()
    first = shards[0]
    by_key: Dict[str, OutlineEvidenceView] = {}
    blocking: List[Dict[str, Any]] = []
    source_hashes: List[str] = []
    crosswalk: Dict[str, str] = {}
    for shard in shards:
        if shard.artifact_type != first.artifact_type or shard.artifact_version != first.artifact_version:
            raise ValueError("Cannot merge Outline v3 shards with different schemas")
        for key, value in shard.alias_crosswalk.items():
            if key in crosswalk and crosswalk[key] != value:
                raise ValueError(f"Conflicting alias crosswalk value for {key!r}")
            crosswalk[key] = value
        blocking.extend(shard.blocking_diagnostics)
        source_hashes.extend(shard.source_summary_hashes)
        for view in shard.views:
            previous = by_key.get(view.paper_key)
            if previous is not None and previous.view_hash != view.view_hash:
                raise ValueError(f"Conflicting evidence view for duplicated paper key {view.paper_key!r}")
            by_key[view.paper_key] = view
    return OutlineEvidenceViews(
        created_from_job_id=first.created_from_job_id,
        views=[by_key[key] for key in sorted(by_key)],
        source_summary_hashes=_stable_unique(source_hashes),
        alias_crosswalk=crosswalk,
        blocking_diagnostics=blocking,
    )


__all__ = [
    "MATRIX_DIMENSIONS",
    "build_outline_evidence_views",
    "build_global_corpus_ledger",
    "build_multi_view_matrix",
    "build_review_intent",
    "build_coverage_contract",
    "shard_outline_evidence_views",
    "merge_outline_evidence_shards",
]

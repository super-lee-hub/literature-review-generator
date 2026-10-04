"""Provider-free semantic chunk planning for Outline Intelligence v3.

This module is deliberately a local projection and planning boundary.  It
turns canonical Stage 1 summaries into two reusable content layers, builds
evidence-complete relation bundles, and emits a bounded call plan.  It never
calls a provider and it never treats a paper id, a token shard, or a topic
label as proof of a substantive relation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, cast

from outline.v3_evidence import build_outline_evidence_views, find_source_study_records
from outline.v3_models import (
    EVIDENCE_CLAIM_TYPES,
    EvidenceClaim,
    GlobalRelationMap,
    InterpretationDependency,
    PaperContentLayers,
    PaperEvidenceDossier,
    PaperIndexCard,
    RelationCandidate,
    RelationEvidenceBundle,
    ResearchUnit,
    SourceFieldLedgerEntry,
    TopicSynthesis,
    compute_v3_hash,
)
from runtime.provider_runtime import (
    DEFAULT_PROVIDER_CALL_BUDGET,
    authorized_provider_call_limit,
)
from summary_schema import get_ai_summary

SEMANTIC_CHUNK_PLAN_ARTIFACT_TYPE = "semantic_chunk_plan"
SEMANTIC_CHUNK_PLAN_ARTIFACT_VERSION = "v1"
SEMANTIC_CHUNK_PLAN_SCHEMA_VERSION = "semantic-chunk-plan-v1"
_TOPIC_FAMILY_GROUPING_VERSION = "method-theory-paper-pairs-v3"

_METHOD_TOPIC_FAMILIES: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("mixed_methods", "mixed-method designs", ("mixed method", "mixed-method")),
    (
        "review_synthesis",
        "review and synthesis designs",
        ("meta-analysis", "meta analysis", "systematic review", "review article"),
    ),
    (
        "conceptual_or_analytical",
        "conceptual and analytical designs",
        ("conceptual", "game theoretic", "game-theoretic", "analytical model", "no primary data"),
    ),
    (
        "experimental_design",
        "experimental designs",
        ("experiment", "randomized", "randomised"),
    ),
    (
        "qualitative_study",
        "qualitative designs",
        ("qualitative", "interview", "focus group", "case study"),
    ),
    (
        "survey_study",
        "survey designs",
        ("survey", "questionnaire", "telephone survey"),
    ),
    (
        "observational_data",
        "observational and archival designs",
        ("web crawling", "web crawl", "scraping", "scanner data", "archival", "secondary data", "panel data"),
    ),
)

_THEORY_TOPIC_FAMILIES: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    (
        "price_fairness_justice",
        "price-fairness and justice frameworks",
        (
            "dual entitlement",
            "price fairness",
            "equity theory",
            "distributive justice",
            "procedural justice",
            "fairness heuristic",
            "justice theory",
            "fairness as",
            "fairnessjustice",
        ),
    ),
    (
        "attribution_responsibility",
        "attribution and responsibility frameworks",
        ("attribution", "blame", "locus of control"),
    ),
    (
        "prospect_reference",
        "prospect and reference-point frameworks",
        ("prospect theory", "loss aversion", "reference point", "range frequency"),
    ),
    (
        "relationship_norms",
        "relationship and social-norm frameworks",
        ("social exchange", "communal", "exchange relationship", "relationship norm", "social norm"),
    ),
    (
        "economic_utility",
        "economic utility frameworks",
        (
            "transaction utility",
            "customer surplus",
            "consumer surplus",
            "price discrimination",
            "behavioral economics",
            "inequity aversion",
            "yield management",
        ),
    ),
)

_FIELD_LOCATORS: Dict[str, Tuple[str, ...]] = {
    "research_questions": ("specialized_details.*.research_questions_or_hypotheses",),
    "concept_definitions": ("core_analysis.core_variables", "core_analysis.key_constructs"),
    "operationalizations": ("specialized_details.*.analysis_technique", "specialized_details.*.data_source_and_size"),
    "theoretical_derivation": ("core_analysis.theoretical_framework", "core_analysis.theoretical_derivation"),
    "findings": ("core_analysis.findings", "core_analysis.key_points"),
    "mechanism_evidence": ("core_analysis.mechanisms", "specialized_details.*.core_variables.mediators"),
    "moderators_boundaries": ("core_analysis.limitations", "specialized_details.*.sample_characteristics_or_context"),
    "zero_results": ("core_analysis.zero_results", "core_analysis.null_results", "core_analysis.non_significant_results"),
    "limitations": ("core_analysis.limitations",),
}

_TOPIC_DIMENSIONS: Tuple[Tuple[str, str], ...] = (
    ("theory", "theories"),
    ("construct", "constructs"),
    ("mechanism", "mechanisms"),
    ("method", "method"),
    ("context", "sample_or_context"),
)

_MULTI_STUDY_SIGNAL = re.compile(
    r"\b(?:three|four|3|4)\s+stud(?:y|ies)\b"
    r"|\b(?:study|experiment)\s*(?:no\.?\s*)?(?:2|3|4|ii|iii|iv)\b"
    r"|\bstudies\s+(?:1\s*(?:-|–|to|and|,|&)\s*)?(?:2|3|4)\b"
    r"|(?:研究|实验)\s*(?:二|三|四|2|3|4)\b",
    re.IGNORECASE,
)


def _safe_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _stable_unique(values: Iterable[Any]) -> List[str]:
    result: Dict[str, str] = {}
    for value in values:
        text = _safe_text(value)
        if text:
            result.setdefault(text.casefold(), text)
    return [result[key] for key in sorted(result)]


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text_values(value: Any) -> List[str]:
    """Flatten literal source values without truncating conditional text."""

    if value is None:
        return []
    if isinstance(value, Mapping):
        result: List[str] = []
        for key in sorted(value, key=lambda item: str(item)):
            result.extend(_text_values(value[key]))
        return _stable_unique(result)
    if isinstance(value, (list, tuple, set)):
        result = []
        for item in value:
            result.extend(_text_values(item))
        return _stable_unique(result)
    text = _safe_text(value)
    return [text] if text else []


def _normalise_label(value: Any) -> str:
    text = re.sub(r"\s+", " ", _safe_text(value)).strip().casefold()
    return re.sub(r"[^\w\- ]+", "", text, flags=re.UNICODE).strip()


def _method_theory_topic_family(
    dimension: str,
    label: str,
) -> Tuple[str, str] | None:
    """Return a conservative navigation family for a method/theory label.

    Family membership organizes requests only. Exact source wording and its
    paper/study evidence remain in the typed content layers and provider input.
    Unknown labels keep their exact route so this helper never guesses a topic
    equivalence from weak token overlap.
    """

    normalized_dimension = str(dimension or "").strip().casefold()
    normalized_label = _normalise_label(label)
    rules = (
        _METHOD_TOPIC_FAMILIES
        if normalized_dimension == "method"
        else _THEORY_TOPIC_FAMILIES
        if normalized_dimension == "theory"
        else ()
    )
    for family_id, family_label, terms in rules:
        if any(term in normalized_label for term in terms):
            return family_id, family_label
    return None


def _topic_label_identity_rows(
    topic_data: Mapping[str, Mapping[str, Any]],
) -> List[Tuple[str, str, str]]:
    rows = [
        (str(dimension), str(paper_id), str(label))
        for item in topic_data.values()
        for dimension in [item.get("dimension")]
        for paper_id, labels in (item.get("source_labels_by_paper") or {}).items()
        for label in labels
        if str(dimension) in {"method", "theory"} and str(label)
    ]
    return sorted(set(rows))


def _path_value(value: Any, path: Sequence[str]) -> Any:
    current = value
    for segment in path:
        if segment == "*":
            return current
        if not isinstance(current, Mapping):
            return None
        current = current.get(segment)
    return current


def _collect_paths(ai_summary: Mapping[str, Any], paths: Sequence[str]) -> List[str]:
    """Read canonical Stage 1 paths, including wildcard detail sections."""

    values: List[str] = []
    for raw_path in paths:
        parts = tuple(part for part in raw_path.split(".") if part)
        if "*" not in parts:
            values.extend(_text_values(_path_value(ai_summary, parts)))
            continue
        star_index = parts.index("*")
        prefix = _path_value(ai_summary, parts[:star_index])
        if isinstance(prefix, Mapping):
            for key in sorted(prefix, key=lambda item: str(item)):
                values.extend(_text_values(_path_value(prefix[key], parts[star_index + 1 :])))
    return _stable_unique(values)


def _find_study_records(value: Any) -> List[Tuple[str, Mapping[str, Any], str]]:
    """Find only records whose source explicitly identifies the study."""

    return [
        (source_study_id, record, f"ai_summary.{source_path}")
        for source_study_id, record, source_path, explicit in find_source_study_records(value)
        if explicit
    ]


def _record_path_matches(source_path: str, record_path: str) -> bool:
    return (
        source_path == record_path
        or source_path.startswith(record_path + ".")
        or source_path.startswith(record_path + "[")
    )


def _has_multi_study_signal(entries: Sequence[SourceFieldLedgerEntry]) -> bool:
    for entry in entries:
        path = entry.source_path.casefold().replace("-", "_")
        if path.endswith("included_studies_count"):
            try:
                if int(entry.source_value.strip()) > 1:
                    return True
            except ValueError:
                pass
        if _MULTI_STUDY_SIGNAL.search(entry.source_value):
            return True
    return False


def _summary_hash(summary: Mapping[str, Any]) -> str:
    return compute_v3_hash(summary)


def _evidence_id(paper_id: str, field_name: str, index: int, text: str) -> str:
    digest = hashlib.sha256(f"{paper_id}|{field_name}|{index}|{text}".encode("utf-8")).hexdigest()[:16]
    return f"evidence:{paper_id}:{field_name}:{digest}"


def _claim(
    *,
    paper_id: str,
    source_hash: str,
    claim_type: str,
    text: str,
    field_name: str,
    index: int,
    study_id: str = "",
    locator: str = "",
) -> Tuple[EvidenceClaim, str]:
    evidence_id = _evidence_id(paper_id, field_name, index, text)
    claim_id = f"claim:{paper_id}:{field_name}:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"
    return EvidenceClaim(
        claim_id=claim_id,
        claim_type=claim_type,
        text=text,
        study_id=study_id,
        evidence_ids=[evidence_id],
        source_locator=locator or f"stage1:ai_summary.{field_name}",
        source_summary_hash=source_hash,
    ), evidence_id


def _unit_field(record: Mapping[str, Any], *names: str) -> List[str]:
    values: List[str] = []
    for name in names:
        values.extend(_text_values(record.get(name)))
    return _stable_unique(values)


def _qualifier_reason(value: Any) -> str:
    text = _safe_text(value).casefold().replace("-", "_")
    if any(token in text for token in ("zero_result", "null_result", "null_finding", "non_significant")):
        return "zero_result"
    if any(token in text for token in ("mechanism", "mediator", "mediation")):
        return "mechanism"
    if any(token in text for token in ("boundary", "moderator", "condition", "limitation", "limit")):
        return "boundary"
    return ""


def _typed_unit_scope(payload: Mapping[str, Any]) -> Tuple[str, str, str]:
    source_study_id = _safe_text(payload.get("source_study_id"))
    unit_study_id = _safe_text(payload.get("study_id"))
    locators = payload.get("source_locators")
    explicit_locator = isinstance(locators, Mapping) and bool(locators.get("study"))
    if source_study_id:
        return "explicit_study", unit_study_id or source_study_id, source_study_id
    if explicit_locator and unit_study_id and not unit_study_id.endswith(":paper_level"):
        # Typed post-builder units may carry an explicit source locator and ID
        # without the newer source_study_id field.
        return "explicit_study", unit_study_id, unit_study_id
    if unit_study_id.endswith(":paper_level"):
        return "paper", unit_study_id, ""
    return "unresolved", unit_study_id, ""


def _source_field_id(
    *,
    source_summary_hash: str,
    source_path: str,
    source_value: str,
    canonical_field: str,
    scope: str,
    study_id: str,
) -> str:
    return "source-field:" + compute_v3_hash({
        "source_summary_hash": source_summary_hash,
        "source_path": source_path,
        "source_value": source_value,
        "canonical_field": canonical_field,
        "scope": scope,
        "study_id": study_id,
    })


def _coerce_ledger_entries(value: Iterable[Any]) -> List[SourceFieldLedgerEntry]:
    entries: Dict[str, SourceFieldLedgerEntry] = {}
    for item in value:
        try:
            entry = (
                item
                if isinstance(item, SourceFieldLedgerEntry)
                else SourceFieldLedgerEntry.from_dict(item)
                if isinstance(item, Mapping)
                else None
            )
        except (TypeError, ValueError):
            entry = None
        if entry is not None:
            entries[entry.source_field_id] = entry
    return [entries[key] for key in sorted(entries)]


def _research_unit_payload(
    research_unit: ResearchUnit | Mapping[str, Any],
) -> Dict[str, Any]:
    if isinstance(research_unit, ResearchUnit):
        return research_unit.to_dict()
    return dict(research_unit)


def _claim_evidence_values(claim: Mapping[str, Any]) -> List[Any]:
    value = claim.get("evidence_ids")
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return [value] if value is not None else []


def derive_unit_source_field_ledger(
    research_unit: ResearchUnit | Mapping[str, Any],
    source_field_ledger: Iterable[Any] = (),
) -> List[SourceFieldLedgerEntry]:
    """Return ledger text plus conservative field entries from a typed unit.

    The fallback covers typed units assembled after raw Stage 1 projection.
    It uses the supplied unit scope and never turns a paper/unresolved source
    into a study-scoped field.
    """

    payload = _research_unit_payload(research_unit)
    entries = _coerce_ledger_entries(source_field_ledger)
    scope, _unit_study_id, scoped_study_id = _typed_unit_scope(payload)
    source_hash = _safe_text(payload.get("source_summary_hash"))
    field_values = (
        ("moderators_or_boundaries", "moderators_boundaries", "boundary"),
        ("zero_results", "zero_results", "zero_result"),
        ("mechanisms", "mechanism_evidence", "mechanism"),
        ("limitations", "limitations", "boundary"),
    )
    by_signature = {
        (
            entry.source_value.strip().casefold(),
            _qualifier_reason(entry.canonical_field or entry.source_path),
            entry.scope,
            entry.study_id,
        )
        for entry in entries
        if entry.interpretation_required
    }
    if scope == "paper":
        by_signature.update(
            (
                entry.source_value.strip().casefold(),
                _qualifier_reason(entry.canonical_field or entry.source_path),
                "paper",
                "",
            )
            for entry in entries
            if entry.interpretation_required and entry.scope == "unresolved"
        )
    for field_name, canonical_field, _reason in field_values:
        for index, text in enumerate(_text_values(payload.get(field_name))):
            signature = (text.strip().casefold(), _reason, scope, scoped_study_id)
            if signature in by_signature:
                continue
            path = f"typed_research_unit.{field_name}[{index}]"
            entry = SourceFieldLedgerEntry(
                source_field_id=_source_field_id(
                    source_summary_hash=source_hash,
                    source_path=path,
                    source_value=text,
                    canonical_field=canonical_field,
                    scope=scope,
                    study_id=scoped_study_id,
                ),
                source_path=path,
                source_value=text,
                disposition="context",
                canonical_field=canonical_field,
                scope=scope,
                study_id=scoped_study_id,
                interpretation_required=True,
                source_summary_hash=source_hash,
            )
            entries.append(entry)
            by_signature.add(signature)
    return sorted(
        {entry.source_field_id: entry for entry in entries}.values(),
        key=lambda item: item.source_field_id,
    )


def derive_interpretation_dependencies(
    research_unit: ResearchUnit | Mapping[str, Any],
    source_field_ledger: Iterable[Any] = (),
) -> List[InterpretationDependency]:
    """Link a finding to qualifying claims and context from its same source scope."""

    payload = _research_unit_payload(research_unit)
    entries = derive_unit_source_field_ledger(research_unit, source_field_ledger)
    unit_scope, unit_study_id, source_study_id = _typed_unit_scope(payload)
    raw_claims = payload.get("claims") or ()
    claims: List[Dict[str, Any]] = []
    for item in raw_claims:
        if isinstance(item, Mapping):
            claims.append(dict(item))
        elif hasattr(item, "to_dict"):
            claims.append(dict(cast(Any, item).to_dict()))

    def claim_reason(claim: Mapping[str, Any]) -> str:
        reason = _qualifier_reason(claim.get("source_locator"))
        if reason:
            return reason
        claim_text = _safe_text(claim.get("text")).casefold()
        for field_name, _canonical, field_reason in (
            ("moderators_or_boundaries", "moderators_boundaries", "boundary"),
            ("zero_results", "zero_results", "zero_result"),
            ("mechanisms", "mechanism_evidence", "mechanism"),
            ("limitations", "limitations", "boundary"),
        ):
            if claim_text and any(
                claim_text == value.casefold()
                for value in _text_values(payload.get(field_name))
            ):
                return field_reason
        return ""

    qualifier_claims = [
        (claim, claim_reason(claim))
        for claim in claims
        if claim_reason(claim)
    ]
    qualifier_fields: List[Tuple[SourceFieldLedgerEntry, str]] = []
    for entry in entries:
        if not entry.interpretation_required:
            continue
        reason = _qualifier_reason(entry.canonical_field) or _qualifier_reason(entry.source_path)
        if not reason:
            continue
        if unit_scope == "explicit_study":
            if entry.scope == "explicit_study" and entry.study_id == source_study_id:
                qualifier_fields.append((entry, reason))
        elif entry.scope in {"paper", "unresolved"}:
            qualifier_fields.append((entry, reason))

    primary_claims = [
        claim
        for claim in claims
        if str(claim.get("claim_type") or "") == "empirical_finding"
        and not claim_reason(claim)
    ]
    dependencies: List[InterpretationDependency] = []
    for primary in primary_claims:
        primary_id = _safe_text(primary.get("claim_id"))
        primary_study_id = _safe_text(primary.get("study_id"))
        if unit_scope == "explicit_study" and primary_study_id == unit_study_id:
            dependency_scope, dependency_study_id = "explicit_study", unit_study_id
            scoped_claims = [
                (claim, reason)
                for claim, reason in qualifier_claims
                if _safe_text(claim.get("study_id")) == unit_study_id
            ]
            scoped_fields = [
                (entry, reason)
                for entry, reason in qualifier_fields
                if entry.scope == "explicit_study" and entry.study_id == source_study_id
            ]
        elif not primary_study_id and unit_scope in {"paper", "unresolved"}:
            scoped_claims = [
                (claim, reason)
                for claim, reason in qualifier_claims
                if not _safe_text(claim.get("study_id"))
            ]
            scoped_fields = [
                (entry, reason)
                for entry, reason in qualifier_fields
                if entry.scope in {"paper", "unresolved"}
            ]
            dependency_scope = (
                "unresolved"
                if unit_scope == "unresolved" or any(entry.scope == "unresolved" for entry, _ in scoped_fields)
                else "paper"
            )
            dependency_study_id = ""
        else:
            continue
        if not scoped_claims and not scoped_fields:
            continue
        reasons = _stable_unique([reason for _claim, reason in scoped_claims] + [reason for _entry, reason in scoped_fields])
        dependencies.append(InterpretationDependency(
            primary_claim_id=primary_id,
            required_source_claim_ids=_stable_unique(
                claim.get("claim_id")
                for claim, _reason in scoped_claims
                if _safe_text(claim.get("claim_id")) != primary_id
            ),
            required_evidence_ids=_stable_unique(
                evidence_id
                for claim, _reason in scoped_claims
                if _safe_text(claim.get("claim_id")) != primary_id
                for evidence_id in _claim_evidence_values(claim)
            ),
            required_source_field_ids=[entry.source_field_id for entry, _reason in scoped_fields],
            scope=dependency_scope,
            study_id=dependency_study_id,
            reason=",".join(reasons),
        ))
    return sorted(
        {compute_v3_hash(item.to_dict()): item for item in dependencies}.values(),
        key=lambda item: (item.primary_claim_id, item.scope, item.reason),
    )


def _make_dossier(view: Any, summary: Mapping[str, Any]) -> PaperEvidenceDossier:
    paper_id = str(view.paper_key)
    source_hash = str(view.source_summary_hash or _summary_hash(summary))
    try:
        normalized_ai_summary = get_ai_summary(summary)
    except (TypeError, ValueError, KeyError):
        normalized_ai_summary = _as_mapping(summary.get("ai_summary"))
    raw_ai_summary = summary.get("ai_summary")
    ai_summary = (
        raw_ai_summary
        if isinstance(raw_ai_summary, Mapping)
        else summary
        if isinstance(summary, Mapping) and "core_analysis" in summary
        else normalized_ai_summary
    )

    evidence_text_by_id: Dict[str, str] = {}
    evidence_ids_by_field: Dict[str, List[str]] = {}
    source_locators: Dict[str, List[str]] = {}
    claims: List[EvidenceClaim] = []

    def add_field(field_name: str, values: Iterable[Any], locator: str) -> List[str]:
        clean = _stable_unique(values)
        if not clean:
            return []
        ids: List[str] = []
        for index, text in enumerate(clean):
            evidence_id = _evidence_id(paper_id, field_name, index, text)
            ids.append(evidence_id)
            evidence_text_by_id[evidence_id] = text
        evidence_ids_by_field[field_name] = _stable_unique([*evidence_ids_by_field.get(field_name, []), *ids])
        source_locators[field_name] = _stable_unique([*source_locators.get(field_name, []), locator])
        return ids

    field_values: Dict[str, List[str]] = {
        "research_questions": _collect_paths(ai_summary, _FIELD_LOCATORS["research_questions"]),
        "concept_definitions": [*view.constructs, *_collect_paths(ai_summary, ("core_analysis.core_variables",))],
        "operationalizations": _collect_paths(ai_summary, _FIELD_LOCATORS["operationalizations"]),
        "theoretical_derivation": [*view.theories, *_collect_paths(ai_summary, _FIELD_LOCATORS["theoretical_derivation"])],
        "findings": list(view.findings),
        "mechanism_evidence": list(view.mechanisms),
        "moderators_boundaries": [*view.limitations, *view.sample_or_context],
        "zero_results": _collect_paths(ai_summary, _FIELD_LOCATORS["zero_results"]),
        "limitations": list(view.limitations),
    }
    # Preserve any explicitly named canonical fields that the evidence view
    # does not currently project, without inventing page numbers or labels.
    for field_name, paths in _FIELD_LOCATORS.items():
        field_values[field_name] = _stable_unique([*field_values.get(field_name, []), *_collect_paths(ai_summary, paths)])

    field_claim_types = {
        "findings": "empirical_finding",
        "zero_results": "empirical_finding",
        "limitations": "author_interpretation",
        "moderators_boundaries": "author_interpretation",
        "theoretical_derivation": "author_interpretation",
        "research_questions": "author_interpretation",
        "concept_definitions": "author_interpretation",
        "operationalizations": "author_interpretation",
        "mechanism_evidence": "empirical_finding",
    }
    for field_name, values in field_values.items():
        ids = add_field(field_name, values, f"stage1:ai_summary.{field_name}")
        claim_type = field_claim_types.get(field_name)
        if claim_type not in EVIDENCE_CLAIM_TYPES:
            continue
        for index, text in enumerate(_stable_unique(values)):
            claim, _evidence_id_value = _claim(
                paper_id=paper_id,
                source_hash=source_hash,
                claim_type=claim_type,
                text=text,
                field_name=field_name,
                index=index,
                locator=f"stage1:ai_summary.{field_name}",
            )
            # Keep the same source id generated by add_field; it is content
            # addressed and therefore remains stable across input ordering.
            if ids:
                claims.append(claim)

    evidence_id_diagnostics: List[str] = []
    for field_name, claim_type, source_key in (
        ("author_proposed_gap", "author_proposed_gap", "research_gaps"),
        ("author_future_direction", "author_proposed_gap", "future_directions"),
    ):
        values = list(view.research_gaps if source_key == "research_gaps" else view.future_directions)
        ids = add_field(source_key, values, f"stage1:ai_summary.{source_key}")
        for index, text in enumerate(_stable_unique(values)):
            claim, evidence_id = _claim(
                paper_id=paper_id,
                source_hash=source_hash,
                claim_type=claim_type,
                text=text,
                field_name=source_key,
                index=index,
                locator=f"stage1:ai_summary.{source_key}",
            )
            if index >= len(ids) or ids[index] != evidence_id:
                evidence_id_diagnostics.append(
                    f"evidence_id_mismatch:{paper_id}:{source_key}:{index}"
                )
            claims.append(claim)

    explicit_reviewer_inferences = _collect_paths(ai_summary, ("reviewer_inference", "reviewer_inferences"))
    for index, text in enumerate(explicit_reviewer_inferences):
        claim, _ = _claim(
            paper_id=paper_id,
            source_hash=source_hash,
            claim_type="reviewer_inference",
            text=text,
            field_name="reviewer_inference",
            index=index,
            locator="stage1:ai_summary.reviewer_inference",
        )
        claims.append(claim)
        add_field("reviewer_inference", [text], "stage1:ai_summary.reviewer_inference")

    overall_context = _stable_unique([view.paper_type, *view.sample_or_context])
    units: List[ResearchUnit] = []
    study_records = _find_study_records(summary)
    source_field_ledger = _coerce_ledger_entries(getattr(view, "source_field_ledger", ()))
    raw_study_records = find_source_study_records(summary)
    unresolved_study_records = [item for item in raw_study_records if not item[3]]
    multi_study_signal = bool(unresolved_study_records) or _has_multi_study_signal(source_field_ledger)
    if not study_records and multi_study_signal:
        source_field_ledger = [
            replace(entry, scope="unresolved", study_id="")
            if entry.scope == "paper"
            and _qualifier_reason(entry.canonical_field or entry.source_path)
            else entry
            for entry in source_field_ledger
        ]
    if study_records:
        for index, (marker, record, locator) in enumerate(study_records):
            study_id = f"{paper_id}:study:{_normalise_label(marker)}"
            record_path = locator[len("ai_summary."):] if locator.startswith("ai_summary.") else locator
            unit_findings = _unit_field(record, "findings", "finding", "results", "result", "conclusions", "conclusion")
            unit_questions = _unit_field(record, "research_questions", "research_question", "questions", "hypotheses", "hypothesis")
            unit_method = _unit_field(record, "method", "methodology", "design", "analysis")
            unit_context = _unit_field(record, "sample", "context", "sample_or_context", "participants", "data_source")
            unit_mechanisms = _unit_field(record, "mechanisms", "mechanism", "mediators", "mediation")
            unit_boundaries = _unit_field(record, "moderators", "boundaries", "boundary_conditions", "limitations", "conditions")
            unit_zero = _unit_field(record, "zero_results", "null_results", "non_significant_results", "null_findings")
            unit_claims: List[EvidenceClaim] = []
            for field_name, values, claim_type in (
                ("findings", unit_findings, "empirical_finding"),
                ("zero_results", unit_zero, "empirical_finding"),
                ("boundaries", unit_boundaries, "author_interpretation"),
            ):
                for claim_index, text in enumerate(values):
                    field_key = f"{study_id}:{field_name}"
                    claim, evidence_id = _claim(
                        paper_id=paper_id,
                        source_hash=source_hash,
                        claim_type=claim_type,
                        text=text,
                        field_name=field_key,
                        index=claim_index,
                        study_id=study_id,
                        locator=locator,
                    )
                    unit_claims.append(claim)
                    evidence_text_by_id[evidence_id] = text
                    evidence_ids_by_field[field_key] = _stable_unique([
                        *evidence_ids_by_field.get(field_key, []),
                        evidence_id,
                    ])
                    source_locators[field_key] = _stable_unique([
                        *source_locators.get(field_key, []),
                        locator,
                    ])
            unit = ResearchUnit(
                study_id=study_id,
                parent_paper_id=paper_id,
                research_questions=unit_questions,
                definitions_and_operationalizations={
                    "definitions": _unit_field(record, "definitions", "constructs", "variables"),
                    "operationalizations": _unit_field(record, "operationalizations", "measures", "measurement"),
                },
                theoretical_derivation=_unit_field(record, "theory", "theoretical_derivation", "rationale"),
                method=unit_method,
                sample_or_context=unit_context,
                findings=unit_findings,
                mechanisms=unit_mechanisms,
                moderators_or_boundaries=unit_boundaries,
                zero_results=unit_zero,
                limitations=_unit_field(record, "limitations"),
                claims=unit_claims,
                source_locators={"study": [locator]},
                evidence_ids=_stable_unique([item for claim in unit_claims for item in claim.evidence_ids]),
                source_summary_hash=source_hash,
                source_study_id=marker,
                source_field_ids=[
                    entry.source_field_id
                    for entry in source_field_ledger
                    if entry.scope == "explicit_study"
                    and entry.study_id == marker
                    and _record_path_matches(entry.source_path, record_path)
                ],
            )
            source_field_ledger = derive_unit_source_field_ledger(unit, source_field_ledger)
            unit_field_ids = _stable_unique([
                *unit.source_field_ids,
                *[
                    entry.source_field_id
                    for entry in source_field_ledger
                    if entry.scope == "explicit_study"
                    and entry.study_id == marker
                    and (
                        _record_path_matches(entry.source_path, record_path)
                        or entry.source_path.startswith("typed_research_unit.")
                    )
                ],
            ])
            unit = replace(
                unit,
                source_field_ids=unit_field_ids,
                interpretation_dependencies=derive_interpretation_dependencies(unit, source_field_ledger),
            )
            units.append(unit)
    else:
        # A paper without explicit study records still receives one complete
        # paper-level unit.  A possible multi-study source remains unresolved.
        unit = ResearchUnit(
            study_id=f"{paper_id}:study:paper_level",
            parent_paper_id=paper_id,
            research_questions=field_values["research_questions"],
            definitions_and_operationalizations={
                "definitions": field_values["concept_definitions"],
                "operationalizations": field_values["operationalizations"],
            },
            theoretical_derivation=field_values["theoretical_derivation"],
            method=list(view.method),
            sample_or_context=list(view.sample_or_context),
            findings=field_values["findings"],
            mechanisms=field_values["mechanism_evidence"],
            moderators_or_boundaries=field_values["moderators_boundaries"],
            zero_results=field_values["zero_results"],
            limitations=field_values["limitations"],
            claims=[claim for claim in claims if claim.study_id in {"", f"{paper_id}:study:paper_level"}],
            source_locators={field: list(values) for field, values in source_locators.items()},
            evidence_ids=[item for values in evidence_ids_by_field.values() for item in values],
            source_summary_hash=source_hash,
        )
        source_field_ledger = derive_unit_source_field_ledger(unit, source_field_ledger)
        unit = replace(
            unit,
            interpretation_dependencies=derive_interpretation_dependencies(unit, source_field_ledger),
        )
        units.append(unit)

    diagnostics: List[str] = [str(item) for item in (getattr(view, "diagnostics", ()) or ())]
    diagnostics.extend(evidence_id_diagnostics)
    if unresolved_study_records:
        diagnostics.append("unidentified_study_record_scope_unresolved")
    elif not study_records and multi_study_signal:
        diagnostics.append("multi_study_mapping_unresolved")
    if not claims:
        diagnostics.append("no_structured_claims_in_stage1_summary")
    status = "ready" if not diagnostics else "partial"
    return PaperEvidenceDossier(
        dossier_id=f"dossier:{paper_id}",
        paper_id=paper_id,
        source_summary_hash=source_hash,
        overall_context=overall_context,
        research_questions=field_values["research_questions"],
        concept_definitions=field_values["concept_definitions"],
        operationalizations=field_values["operationalizations"],
        theoretical_derivation=field_values["theoretical_derivation"],
        findings=field_values["findings"],
        research_units=units,
        claims=claims,
        mechanism_evidence=field_values["mechanism_evidence"],
        moderators_boundaries=field_values["moderators_boundaries"],
        zero_results=field_values["zero_results"],
        limitations=field_values["limitations"],
        source_locators=source_locators,
        evidence_ids_by_field=evidence_ids_by_field,
        evidence_text_by_id=evidence_text_by_id,
        diagnostics=_stable_unique(diagnostics),
        status=status,
        source_field_ledger=source_field_ledger,
    )


def _compact_navigation_values(values: Iterable[Any], *, max_items: int = 3, max_chars: int = 220) -> List[str]:
    """Make a navigation-only projection; the dossier keeps the full value."""

    compact: List[str] = []
    for value in _stable_unique(values):
        text = re.sub(r"\s+", " ", value).strip()
        if len(text) > max_chars:
            text = text[: max_chars - 24].rstrip() + " ...[see dossier]"
        compact.append(text)
        if len(compact) >= max_items:
            break
    return compact


def build_paper_content_layers(
    summaries: Iterable[Mapping[str, Any]],
    evidence_views: Any | None = None,
    *,
    job_id: str = "",
    strict_status: bool = False,
) -> PaperContentLayers:
    """Build order-invariant navigation cards and logical evidence dossiers."""

    raw_summaries = [dict(item) for item in summaries if isinstance(item, Mapping)]
    evidence = evidence_views or build_outline_evidence_views(raw_summaries, job_id, strict_status=strict_status)
    by_hash: Dict[str, Mapping[str, Any]] = {
        _summary_hash(summary): summary
        for summary in raw_summaries
    }
    cards: List[PaperIndexCard] = []
    dossiers: List[PaperEvidenceDossier] = []
    for view in sorted(evidence.views, key=lambda item: item.paper_key):
        summary = by_hash.get(str(view.source_summary_hash))
        if summary is None:
            # Merged Stage 1 views can carry more than one source hash.  Use
            # the first matching raw source only for local field projection;
            # the merged hash lineage remains on the view and dossier.
            summary = next((item for item in raw_summaries if _summary_hash(item) in set(view.source_summary_hashes)), None)
        if summary is None:
            continue
        dossier = _make_dossier(view, summary)
        card = PaperIndexCard(
            paper_id=view.paper_key,
            research_questions=_compact_navigation_values(view.research_questions, max_items=2),
            key_constructs=_compact_navigation_values(view.constructs, max_items=8, max_chars=100),
            core_findings=_compact_navigation_values([*view.findings, *view.conclusions], max_items=2),
            key_boundaries=_compact_navigation_values([*view.limitations, *view.sample_or_context], max_items=2),
            method_category="; ".join(_compact_navigation_values(view.method, max_items=3, max_chars=120)),
            topic_tags=_compact_navigation_values([*view.theories, *view.constructs, *view.mechanisms], max_items=12, max_chars=100),
            theories=_compact_navigation_values(view.theories, max_items=8, max_chars=100),
            mechanisms=_compact_navigation_values(view.mechanisms, max_items=8, max_chars=100),
            evidence_package_id=dossier.dossier_id,
            source_summary_hash=dossier.source_summary_hash,
            source_locators=["stage1:paper_info", "stage1:ai_summary"],
        )
        cards.append(card)
        dossiers.append(dossier)
    return PaperContentLayers(
        index_cards=cards,
        dossiers=dossiers,
        source_summary_hashes=_stable_unique(
            [*getattr(evidence, "source_summary_hashes", ()), *[item.source_summary_hash for item in dossiers]]
        ),
        blocking_diagnostics=list(getattr(evidence, "blocking_diagnostics", ()) or ()),
    )


def _required_relation_fields(candidate: RelationCandidate) -> Dict[str, Tuple[str, ...]]:
    """Return conservative field requirements for one candidate relation."""

    result: Dict[str, Tuple[str, ...]] = {}
    for paper_id in candidate.paper_keys:
        # A relation may be proposed from shared constructs or methods, but a
        # substantive adjudication still needs a finding and a condition/boundary
        # for every participating paper.
        result[str(paper_id)] = ("findings", "moderators_boundaries")
    return result


def build_relation_evidence_bundle(
    candidate: RelationCandidate | Mapping[str, Any],
    content_layers: PaperContentLayers,
) -> RelationEvidenceBundle:
    """Materialize an evidence-complete bundle without making a judgment."""

    item = candidate if isinstance(candidate, RelationCandidate) else RelationCandidate.from_dict(candidate)
    dossier_by_paper = content_layers.dossier_by_paper
    required: List[str] = []
    provided: List[str] = []
    missing: List[str] = []
    claim_left: List[str] = []
    claim_right: List[str] = []
    findings_left: List[str] = []
    findings_right: List[str] = []
    source_locators: Dict[str, List[str]] = {}
    definitions: Dict[str, List[str]] = {}
    contexts: Dict[str, List[str]] = {}
    paper_order = list(item.paper_keys)
    for index, paper_id in enumerate(paper_order):
        dossier = dossier_by_paper.get(paper_id)
        side_claims = claim_left if index == 0 else claim_right
        side_findings = findings_left if index == 0 else findings_right
        if dossier is None:
            for field_name in ("findings", "moderators_boundaries"):
                missing.append(f"evidence:{paper_id}:{field_name}:missing_dossier")
            continue
        for field_name in _required_relation_fields(item).get(paper_id, ()):
            ids = list(dossier.evidence_ids_by_field.get(field_name, ()))
            required.extend(ids or [f"evidence:{paper_id}:{field_name}:missing"])
            provided.extend(ids)
            if not ids:
                missing.append(f"evidence:{paper_id}:{field_name}:missing")
        for claim in dossier.claims:
            if claim.study_id and claim.study_id.startswith(f"{paper_id}:study:"):
                side_claims.extend(claim.claim_id for _ in [0])
        side_findings.extend(dossier.findings)
        definitions[paper_id] = _stable_unique([*dossier.concept_definitions, *dossier.operationalizations])
        contexts[paper_id] = _stable_unique([*dossier.overall_context, *dossier.moderators_boundaries])
        source_locators[paper_id] = _stable_unique(
            locator
            for values in dossier.source_locators.values()
            for locator in values
        )
    missing.extend(sorted(set(required) - set(provided)))
    missing = _stable_unique(missing)
    return RelationEvidenceBundle(
        relation_id=item.relation_id,
        comparison_question=(
            f"Can the recorded {item.relation_type} relation be compared for "
            f"{', '.join(item.paper_keys)} without ignoring findings or boundaries?"
        ),
        relation_type=item.relation_type,
        paper_ids=list(item.paper_keys),
        claim_ids_left=_stable_unique(claim_left),
        claim_ids_right=_stable_unique(claim_right),
        definitions_and_operationalizations=definitions,
        findings_left=_stable_unique(findings_left),
        findings_right=_stable_unique(findings_right),
        contexts_and_boundaries=contexts,
        source_locators=source_locators,
        required_evidence_ids=_stable_unique(required),
        provided_evidence_ids=_stable_unique(provided),
        missing_evidence_ids=missing,
        evidence_completeness="complete" if not missing else "incomplete",
        decision="not_comparable" if not missing else "insufficient_evidence",
        diagnostics=(
            ["required finding/boundary evidence is incomplete"] if missing else []
        ),
    )


def _token_estimate(value: Any) -> int:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return max(1, math.ceil(len(serialized) / 4))


@dataclass(frozen=True)
class SemanticCallPlan:
    logical_node_id: str
    role: str
    input_hashes: List[str] = field(default_factory=list)
    complete_input_tokens: int = 0
    output_reserve_tokens: int = 0
    expected_physical_calls: Optional[int] = None
    existing_cache_hits: int = 0
    retry_fallback_reserve: int = 0
    estimated_cost: Optional[float] = None
    pricing_source: str = "unknown"
    cost_status: str = "unknown"
    status: str = "planned"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SemanticCallPlan":
        return cls(
            logical_node_id=str(data.get("logical_node_id") or ""),
            role=str(data.get("role") or ""),
            input_hashes=_stable_unique(data.get("input_hashes") or []),
            complete_input_tokens=int(data.get("complete_input_tokens") or 0),
            output_reserve_tokens=int(data.get("output_reserve_tokens") or 0),
            expected_physical_calls=(
                int(data["expected_physical_calls"])
                if data.get("expected_physical_calls") is not None
                else None
            ),
            existing_cache_hits=int(data.get("existing_cache_hits") or 0),
            retry_fallback_reserve=int(data.get("retry_fallback_reserve") or 0),
            estimated_cost=(float(data["estimated_cost"]) if data.get("estimated_cost") is not None else None),
            pricing_source=str(data.get("pricing_source") or "unknown"),
            cost_status=str(data.get("cost_status") or "unknown"),
            status=str(data.get("status") or "planned"),
        )


@dataclass(frozen=True)
class TopicRoute:
    topic_id: str
    question: str
    paper_ids: List[str] = field(default_factory=list)
    bridge_paper_ids: List[str] = field(default_factory=list)
    dimensions: List[str] = field(default_factory=list)
    comparison_questions: List[str] = field(default_factory=list)
    required_evidence_ids: List[str] = field(default_factory=list)
    logical_node_id: str = ""
    estimated_input_tokens: int = 0
    status: str = "planned"
    include_unbound_claims: bool = False

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        for key in ("paper_ids", "bridge_paper_ids", "dimensions", "comparison_questions", "required_evidence_ids"):
            payload[key] = _stable_unique(payload[key])
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TopicRoute":
        return cls(
            topic_id=str(data.get("topic_id") or ""),
            question=str(data.get("question") or ""),
            paper_ids=_stable_unique(data.get("paper_ids") or []),
            bridge_paper_ids=_stable_unique(data.get("bridge_paper_ids") or []),
            dimensions=_stable_unique(data.get("dimensions") or []),
            comparison_questions=_stable_unique(data.get("comparison_questions") or []),
            required_evidence_ids=_stable_unique(data.get("required_evidence_ids") or []),
            logical_node_id=str(data.get("logical_node_id") or ""),
            estimated_input_tokens=int(data.get("estimated_input_tokens") or 0),
            status=str(data.get("status") or "planned"),
            include_unbound_claims=bool(data.get("include_unbound_claims", False)),
        )


@dataclass(frozen=True)
class ReuseInventoryItem:
    source_path: str
    content_hash: str
    source_node: str
    receipt_ids: List[str] = field(default_factory=list)
    content_status: str = "unknown"
    evidence_completeness: str = "unknown"
    new_node_usage: str = ""
    disposition: str = "audit_only"
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["receipt_ids"] = _stable_unique(self.receipt_ids)
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReuseInventoryItem":
        return cls(
            source_path=str(data.get("source_path") or ""),
            content_hash=str(data.get("content_hash") or ""),
            source_node=str(data.get("source_node") or ""),
            receipt_ids=_stable_unique(data.get("receipt_ids") or []),
            content_status=str(data.get("content_status") or "unknown"),
            evidence_completeness=str(data.get("evidence_completeness") or "unknown"),
            new_node_usage=str(data.get("new_node_usage") or ""),
            disposition=str(data.get("disposition") or "audit_only"),
            reason=str(data.get("reason") or ""),
        )


@dataclass(frozen=True)
class SemanticChunkPlan:
    artifact_type: str = SEMANTIC_CHUNK_PLAN_ARTIFACT_TYPE
    artifact_version: str = SEMANTIC_CHUNK_PLAN_ARTIFACT_VERSION
    schema_version: str = SEMANTIC_CHUNK_PLAN_SCHEMA_VERSION
    content_layers_hash: str = ""
    source_summary_hashes: List[str] = field(default_factory=list)
    topics: List[TopicRoute] = field(default_factory=list)
    relation_bundles: List[RelationEvidenceBundle] = field(default_factory=list)
    cross_group_questions: List[str] = field(default_factory=list)
    call_plan: List[SemanticCallPlan] = field(default_factory=list)
    reuse_inventory: List[ReuseInventoryItem] = field(default_factory=list)
    budgets: Dict[str, Any] = field(default_factory=dict)
    coverage: Dict[str, Any] = field(default_factory=dict)
    blocking_diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    candidate_count: int = 3

    @property
    def status(self) -> str:
        return "blocked" if self.blocking_diagnostics else "ready"

    @property
    def estimated_physical_calls(self) -> Optional[int]:
        if any(item.expected_physical_calls is None for item in self.call_plan):
            return None
        return sum(max(0, int(item.expected_physical_calls or 0)) for item in self.call_plan)

    @property
    def candidate_generation_physical_calls(self) -> int:
        return max(0, int(self.candidate_count))

    @property
    def shared_content_hash(self) -> str:
        return compute_v3_hash(self.canonical_payload())

    def canonical_payload(self) -> Dict[str, Any]:
        # candidate_count intentionally does not participate in this hash.  It
        # changes organization decisions only; topic/relation understanding is
        # shared and must not be recomputed when the user asks for another
        # number of candidates.
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "schema_version": self.schema_version,
            "content_layers_hash": self.content_layers_hash,
            "source_summary_hashes": _stable_unique(self.source_summary_hashes),
            "topics": [item.to_dict() for item in sorted(self.topics, key=lambda item: item.topic_id)],
            "relation_bundles": [item.to_dict() for item in sorted(self.relation_bundles, key=lambda item: item.relation_id)],
            "cross_group_questions": _stable_unique(self.cross_group_questions),
            "call_plan": [item.to_dict() for item in sorted(self.call_plan, key=lambda item: item.logical_node_id)],
            "reuse_inventory": [item.to_dict() for item in sorted(self.reuse_inventory, key=lambda item: (item.source_node, item.source_path))],
            "budgets": self.budgets,
            "coverage": self.coverage,
            "blocking_diagnostics": sorted(self.blocking_diagnostics, key=lambda item: compute_v3_hash(item)),
        }

    @property
    def content_hash(self) -> str:
        return self.shared_content_hash

    def to_dict(self) -> Dict[str, Any]:
        payload = self.canonical_payload()
        payload.update({
            "content_hash": self.content_hash,
            "status": self.status,
            "candidate_count": self.candidate_count,
            "estimated_physical_calls": self.estimated_physical_calls,
            "candidate_generation_physical_calls": self.candidate_generation_physical_calls,
            "estimated_total_with_candidate_generation": (
                self.estimated_physical_calls + self.candidate_generation_physical_calls
                if self.estimated_physical_calls is not None
                else None
            ),
            "provider_posts_emitted": 0,
        })
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SemanticChunkPlan":
        return cls(
            artifact_type=str(data.get("artifact_type") or SEMANTIC_CHUNK_PLAN_ARTIFACT_TYPE),
            artifact_version=str(data.get("artifact_version") or SEMANTIC_CHUNK_PLAN_ARTIFACT_VERSION),
            schema_version=str(data.get("schema_version") or SEMANTIC_CHUNK_PLAN_SCHEMA_VERSION),
            content_layers_hash=str(data.get("content_layers_hash") or ""),
            source_summary_hashes=_stable_unique(data.get("source_summary_hashes") or []),
            topics=[TopicRoute.from_dict(item) for item in data.get("topics", []) if isinstance(item, Mapping)],
            relation_bundles=[RelationEvidenceBundle.from_dict(item) for item in data.get("relation_bundles", []) if isinstance(item, Mapping)],
            cross_group_questions=_stable_unique(data.get("cross_group_questions") or []),
            call_plan=[SemanticCallPlan.from_dict(item) for item in data.get("call_plan", []) if isinstance(item, Mapping)],
            reuse_inventory=[ReuseInventoryItem.from_dict(item) for item in data.get("reuse_inventory", []) if isinstance(item, Mapping)],
            budgets=dict(data.get("budgets") or {}),
            coverage=dict(data.get("coverage") or {}),
            blocking_diagnostics=[dict(item) for item in data.get("blocking_diagnostics", []) if isinstance(item, Mapping)],
            candidate_count=int(data.get("candidate_count") or 3),
        )


def _topic_candidates(content_layers: PaperContentLayers) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[str]]]:
    cards = {card.paper_id: card for card in content_layers.index_cards}
    occurrences: Dict[Tuple[str, str], set[str]] = {}
    generic_labels = {
        "about", "across", "based", "case", "data", "effect", "effects",
        "evidence", "finding", "findings", "from", "into", "paper", "papers",
        "research", "result", "results", "study", "studies", "using", "with",
        "dossier", "author", "authors", "stated", "limitations", "between",
        "experiments", "experiment", "theory", "method", "methods", "token",
        "that", "state", "were", "only", "sampled", "the", "and", "for",
    }
    for card in cards.values():
        for dimension, field_name in _TOPIC_DIMENSIONS:
            if field_name == "theories":
                values = card.theories
            elif field_name == "constructs":
                values = card.key_constructs
            elif field_name == "mechanisms":
                values = card.mechanisms
            elif field_name == "method":
                values = [card.method_category]
            elif field_name == "sample_or_context":
                # A long limitation sentence is evidence, not a reusable topic
                # label. Only concise, explicit context terms may route papers.
                values = [
                    item for item in card.key_boundaries
                    if len(str(item).strip()) <= 80 and len(str(item).split()) <= 5
                ]
            else:
                values = []
            for value in values:
                label = _normalise_label(value)
                if not label or label in generic_labels:
                    continue
                occurrences.setdefault((dimension, label), set()).add(card.paper_id)

    family_labels: Dict[Tuple[str, str], Dict[str, set[str]]] = {}
    family_display: Dict[Tuple[str, str], str] = {}
    for (dimension, label), paper_ids in occurrences.items():
        family = _method_theory_topic_family(dimension, label)
        if family is None:
            continue
        family_id, display = family
        key = (dimension, family_id)
        family_display[key] = display
        family_labels.setdefault(key, {})[label] = set(paper_ids)

    # Family labels are retained as cross-paper navigation metadata. They do
    # not become broad provider tasks: method and theory records are paired
    # within each paper so one read can answer both dimensions independently.
    family_metadata: Dict[Tuple[str, str], Dict[str, Any]] = {}
    family_keys_by_paper: Dict[str, set[Tuple[str, str]]] = {}
    family_keys_by_label: Dict[Tuple[str, str], set[Tuple[str, str]]] = {}
    for key, labels in family_labels.items():
        dimension, family_id = key
        labels_by_paper: Dict[str, List[str]] = {}
        for label, paper_ids in labels.items():
            for paper_id in paper_ids:
                labels_by_paper.setdefault(paper_id, []).append(label)
                family_keys_by_label.setdefault((dimension, label), set()).add(key)
        labels_by_paper = {
            paper_id: sorted(set(values))
            for paper_id, values in sorted(labels_by_paper.items())
        }
        paper_ids = sorted(labels_by_paper)
        if len(paper_ids) < 2:
            continue
        label_rows = sorted(
            (dimension, paper_id, label)
            for paper_id, values in labels_by_paper.items()
            for label in values
        )
        metadata = {
            "topic_id": f"topic-family:{dimension}:{hashlib.sha256(family_id.encode('utf-8')).hexdigest()[:12]}",
            "dimension": dimension,
            "label": family_display[key],
            "paper_ids": paper_ids,
            "label_frequency": "family_metadata",
            "family_grouped": True,
            "family_id": family_id,
            "source_labels_by_paper": labels_by_paper,
            "source_label_count": len(labels),
            "source_label_occurrence_count": len(label_rows),
            "source_label_identity_set_hash": compute_v3_hash(label_rows),
        }
        family_metadata[key] = metadata
        for paper_id in paper_ids:
            family_keys_by_paper.setdefault(paper_id, set()).add(key)

    labels_by_paper_dimension: Dict[str, Dict[str, set[str]]] = {}
    for (dimension, label), paper_ids in occurrences.items():
        if dimension not in {"method", "theory"}:
            continue
        for paper_id in paper_ids:
            labels_by_paper_dimension.setdefault(paper_id, {}).setdefault(dimension, set()).add(label)
    paired_papers = {
        paper_id
        for paper_id, dimensions in labels_by_paper_dimension.items()
        if dimensions.get("method") and dimensions.get("theory")
    }

    residual_occurrences: Dict[Tuple[str, str], set[str]] = {}
    for (dimension, label), paper_ids in occurrences.items():
        # Pairing method and theory creates one paper-local task for those two
        # dimensions. It must not remove the same papers from independent
        # construct, mechanism, or context tasks.
        residual_papers = paper_ids - paired_papers if dimension in {"method", "theory"} else paper_ids
        if residual_papers:
            residual_occurrences[(dimension, label)] = residual_papers
    residual_family_labels: Dict[Tuple[str, str], Dict[str, set[str]]] = {}
    for (dimension, label), paper_ids in residual_occurrences.items():
        family = _method_theory_topic_family(dimension, label)
        if family is None:
            continue
        family_id, _display = family
        residual_family_labels.setdefault((dimension, family_id), {})[label] = set(paper_ids)

    folded_labels = {
        (dimension, label)
        for (dimension, _family_id), labels in residual_family_labels.items()
        if len(labels) > 1
        for label in labels
    }
    shared_exact = sorted(
        (
            (dimension, label, sorted(papers))
            for (dimension, label), papers in residual_occurrences.items()
            if len(papers) >= 2 and (dimension, label) not in folded_labels
        ),
        key=lambda item: (-len(item[2]), item[0], item[1]),
    )
    family_groups = []
    for key, labels in residual_family_labels.items():
        if len(labels) < 2:
            continue
        dimension, family_id = key
        labels_by_paper: Dict[str, List[str]] = {}
        for label, paper_ids in labels.items():
            for paper_id in paper_ids:
                labels_by_paper.setdefault(paper_id, []).append(label)
        labels_by_paper = {
            paper_id: sorted(set(values))
            for paper_id, values in sorted(labels_by_paper.items())
        }
        paper_ids = sorted(labels_by_paper)
        label_rows = sorted(
            (dimension, paper_id, label)
            for paper_id, values in labels_by_paper.items()
            for label in values
        )
        topic_id = f"topic:{dimension}:family:{hashlib.sha256(family_id.encode('utf-8')).hexdigest()[:12]}"
        family_groups.append((
            topic_id,
            {
                "dimension": dimension,
                "label": family_display[key],
                "paper_ids": paper_ids,
                "label_frequency": "family",
                "family_grouped": True,
                "family_id": family_id,
                "source_labels_by_paper": labels_by_paper,
                "source_label_count": len(labels),
                "source_label_occurrence_count": len(label_rows),
                "source_label_identity_set_hash": compute_v3_hash(label_rows),
            },
        ))
    family_groups.sort(
        key=lambda item: (
            -len(item[1]["paper_ids"]),
            str(item[1]["dimension"]),
            str(item[1]["family_id"]),
        )
    )
    singleton_exact = sorted(
        (
            (dimension, label, sorted(papers))
            for (dimension, label), papers in residual_occurrences.items()
            if len(papers) == 1 and (dimension, label) not in folded_labels
        ),
        key=lambda item: (item[0], item[1]),
    )
    exact_rows = [*shared_exact, *singleton_exact]
    topic_data: Dict[str, Dict[str, Any]] = {}
    paper_topics: Dict[str, List[str]] = {paper_id: [] for paper_id in cards}

    for paper_id in sorted(paired_papers):
        dimensions = labels_by_paper_dimension[paper_id]
        method_labels = sorted(dimensions["method"])
        theory_labels = sorted(dimensions["theory"])
        source_rows = sorted(
            [*(('method', paper_id, label) for label in method_labels),
             *(('theory', paper_id, label) for label in theory_labels)]
        )
        topic_id = f"topic:method_theory:paper:{hashlib.sha256(paper_id.encode('utf-8')).hexdigest()[:12]}"
        memberships = [
            family_metadata[key]
            for key in sorted(family_keys_by_paper.get(paper_id, set()))
            if key in family_metadata
        ]
        topic_data[topic_id] = {
            "dimension": "method_theory",
            "dimensions": ["method", "theory"],
            "label": "paper-specific method and theory",
            "paper_ids": [paper_id],
            "label_frequency": "paper_pair",
            "family_grouped": False,
            "paired_dimensions": True,
            "method_labels": method_labels,
            "theory_labels": theory_labels,
            "source_labels_by_paper": {paper_id: [*method_labels, *theory_labels]},
            "source_label_count": len(set([*method_labels, *theory_labels])),
            "source_label_occurrence_count": len(source_rows),
            "source_label_identity_set_hash": compute_v3_hash(source_rows),
            "family_memberships": memberships,
        }
        paper_topics.setdefault(paper_id, []).append(topic_id)

    for topic_id, data in family_groups:
        dimension = str(data["dimension"])
        family_id = str(data.get("family_id") or "")
        metadata = family_metadata.get((dimension, family_id))
        data["family_memberships"] = [metadata] if metadata else []
        topic_data[topic_id] = data
        for paper_id in data["paper_ids"]:
            paper_topics.setdefault(paper_id, []).append(topic_id)

    for dimension, label, paper_ids in exact_rows:
        topic_id = f"topic:{dimension}:{hashlib.sha256(label.encode('utf-8')).hexdigest()[:12]}"
        memberships = [
            family_metadata[key]
            for key in sorted(family_keys_by_label.get((dimension, label), set()))
            if key in family_metadata
        ]
        topic_data[topic_id] = {
            "dimension": dimension,
            "label": label,
            "paper_ids": paper_ids,
            "label_frequency": "shared" if len(paper_ids) >= 2 else "singleton",
            "family_grouped": False,
            "source_labels_by_paper": {paper_id: [label] for paper_id in paper_ids},
            "source_label_count": 1,
            "source_label_occurrence_count": len(paper_ids),
            "source_label_identity_set_hash": compute_v3_hash(
                sorted((dimension, paper_id, label) for paper_id in paper_ids)
            ),
            "family_memberships": memberships,
        }
        for paper_id in paper_ids:
            paper_topics.setdefault(paper_id, []).append(topic_id)
    return topic_data, paper_topics


def _build_topics(content_layers: PaperContentLayers) -> Tuple[List[TopicRoute], List[str], Dict[str, Any]]:
    topic_data, paper_topics = _topic_candidates(content_layers)
    dossiers = content_layers.dossier_by_paper
    cards = {card.paper_id: card for card in content_layers.index_cards}
    topics: List[TopicRoute] = []
    family_group_rows_by_id: Dict[str, Dict[str, Any]] = {}
    for topic_id, data in sorted(topic_data.items()):
        for group in data.get("family_memberships") or []:
            if isinstance(group, Mapping):
                family_group_rows_by_id[str(group.get("topic_id") or "")] = {
                    "topic_id": str(group.get("topic_id") or ""),
                    "dimension": str(group.get("dimension") or ""),
                    "label": str(group.get("label") or ""),
                    "family_id": str(group.get("family_id") or ""),
                    "source_label_count": int(group.get("source_label_count") or 0),
                    "source_label_occurrence_count": int(
                        group.get("source_label_occurrence_count") or 0
                    ),
                    "source_label_identity_set_hash": str(
                        group.get("source_label_identity_set_hash") or ""
                    ),
                    "paper_count": len(group.get("paper_ids") or []),
                    "paper_ids": list(group.get("paper_ids") or []),
                    "source_labels_by_paper": dict(group.get("source_labels_by_paper") or {}),
                }
        paper_ids = list(data["paper_ids"])
        route_dimensions = [
            str(value)
            for value in (data.get("dimensions") or [data.get("dimension")])
            if str(value)
        ]
        field_names_by_dimension = {
            "theory": ("research_questions", "theoretical_derivation", "concept_definitions"),
            "construct": ("research_questions", "concept_definitions", "operationalizations"),
            "mechanism": ("research_questions", "mechanism_evidence", "findings", "zero_results"),
            "method": ("research_questions", "operationalizations", "findings", "zero_results"),
            "context": (
                "research_questions",
                "moderators_boundaries",
                "findings",
                "zero_results",
                "limitations",
            ),
        }
        required = [
            evidence_id
            for dimension in route_dimensions
            for field_name in field_names_by_dimension.get(dimension, ())
            for paper_id in paper_ids
            for evidence_id in dossiers.get(
                paper_id, PaperEvidenceDossier("", paper_id)
            ).evidence_ids_by_field.get(field_name, [])
        ]
        if not required:
            required = [
                evidence_id
                for paper_id in paper_ids
                for values in dossiers.get(
                    paper_id, PaperEvidenceDossier("", paper_id)
                ).evidence_ids_by_field.values()
                for evidence_id in values
            ]
        topic_label = str(data["label"])
        if data.get("paired_dimensions"):
            paper_id = paper_ids[0]
            topic_question = (
                f"Read the method and theory evidence for {paper_id} in one paper-specific pass, "
                "but answer the two dimensions separately. Do not transfer a method, framework, "
                "condition, or finding between papers or treat family membership as equivalence."
            )
            topic_comparison_questions = [
                f"METHOD QUESTION for {paper_id}: What design, sample, data source, conditions, and limits are explicitly reported, and how do they constrain this paper's findings?",
                f"THEORY QUESTION for {paper_id}: Which named framework and theoretical derivation support this paper's claims, and what mechanisms, conditions, and evidence limits remain explicit?",
            ]
        elif data.get("family_grouped") and data["dimension"] == "method":
            topic_question = (
                f"Compare the recorded {topic_label} for these papers as a navigation group only. "
                "Assess each paper and explicit study separately, retaining its method wording, "
                "sample, data source, conditions, and limits; do not infer shared findings from "
                "shared family membership."
            )
            topic_comparison_questions = [
                "Which design, sample, or data-source differences change what can be inferred for each paper?",
                "Which conditions, null results, and source-field boundaries must remain attached to each method record?",
            ]
        elif data.get("family_grouped") and data["dimension"] == "theory":
            topic_question = (
                f"Compare the named frameworks in the {topic_label} as a navigation group only. "
                "Assess each paper and explicit study separately, retaining its source wording, "
                "claim owner, evidence links, conditions, and limits; do not treat family membership "
                "as theoretical equivalence."
            )
            topic_comparison_questions = [
                "Which named framework supports each paper's claims, and where do the source records differ?",
                "Which mechanisms, conditions, unresolved fields, and evidence limits must remain separate?",
            ]
        else:
            topic_question = (
                f"Preserve and assess the unique {data['dimension']} label {topic_label!r} in its full context."
                if data.get("label_frequency") == "singleton"
                else f"How do the included studies sharing the {data['dimension']} label {topic_label!r} differ in findings, conditions, and limits?"
            )
            topic_comparison_questions = [
                "Are apparent conflicts explained by operationalization, sample, method, or context?",
                "Which mechanism and boundary claims have direct evidence rather than reviewer inference?",
            ]
        topics.append(TopicRoute(
            topic_id=topic_id,
            question=topic_question,
            paper_ids=paper_ids,
            dimensions=route_dimensions,
            comparison_questions=topic_comparison_questions,
            required_evidence_ids=_stable_unique(required),
            logical_node_id=f"topic_synthesis:{topic_id.removeprefix('topic:')}",
            # Topic synthesis starts from the compact navigation layer.  The
            # full dossier remains addressable by evidence id and is retrieved
            # only for a directed comparison or a high-risk claim.
            estimated_input_tokens=_token_estimate({
                "topic": topic_label,
                "index_cards": [cards[paper_id].to_dict() for paper_id in paper_ids if paper_id in cards],
                "evidence_package_ids": [dossiers[paper_id].dossier_id for paper_id in paper_ids if paper_id in dossiers],
                "output_schema": "TopicSynthesis",
            }),
        ))
    covered_evidence_by_paper: Dict[str, set[str]] = {paper_id: set() for paper_id in dossiers}
    for topic in topics:
        for paper_id in topic.paper_ids:
            dossier = dossiers.get(paper_id)
            if dossier is not None:
                dossier_ids = {str(value) for value in dossier.evidence_ids if str(value)}
                covered_evidence_by_paper.setdefault(paper_id, set()).update(
                    str(value)
                    for value in topic.required_evidence_ids
                    if str(value) in dossier_ids
                )
    uncovered_evidence_by_paper = {
        paper_id: sorted(
            {str(value) for value in dossier.evidence_ids if str(value)}
            - covered_evidence_by_paper.get(paper_id, set())
        )
        for paper_id, dossier in dossiers.items()
    }
    papers_with_unbound_claims: set[str] = set()
    for paper_id, dossier in dossiers.items():
        nested_claim_evidence = {
            str(claim.claim_id): bool(claim.evidence_ids)
            for unit in dossier.research_units
            for claim in unit.claims
        }
        claims = [
            *list(dossier.claims),
            *[claim for unit in dossier.research_units for claim in unit.claims],
        ]
        if any(
            not claim.evidence_ids
            and not nested_claim_evidence.get(str(claim.claim_id), False)
            for claim in claims
        ):
            papers_with_unbound_claims.add(paper_id)
    outliers = sorted(
        paper_id
        for paper_id in cards
        if not paper_topics.get(paper_id)
        or uncovered_evidence_by_paper.get(paper_id)
        or paper_id in papers_with_unbound_claims
    )
    if outliers:
        topics.append(TopicRoute(
            topic_id="topic:outlier_pool",
            question="Which explicitly uncovered evidence units or outlying findings still require preservation?",
            paper_ids=outliers,
            dimensions=["outlier"],
            comparison_questions=["Which remaining evidence is unrepresented in the typed topic routes, and what remains unresolved?"],
            required_evidence_ids=_stable_unique(
                evidence_id
                for paper_id in outliers
                for evidence_id in uncovered_evidence_by_paper.get(paper_id, [])
            ),
            logical_node_id="topic_synthesis:outlier_pool",
            estimated_input_tokens=_token_estimate({
                "topic": "outlier_pool",
                "index_cards": [cards[paper_id].to_dict() for paper_id in outliers if paper_id in cards],
                "evidence_package_ids": [dossiers[paper_id].dossier_id for paper_id in outliers if paper_id in dossiers],
                "output_schema": "TopicSynthesis",
            }),
            status="outlier_review",
            include_unbound_claims=True,
        ))
    if not topics and content_layers.index_cards:
        paper_ids = sorted(card.paper_id for card in content_layers.index_cards)
        topics.append(TopicRoute(
            topic_id="topic:corpus_review",
            question="What evidence, boundaries, and unresolved differences are present across the complete included corpus?",
            paper_ids=paper_ids,
            dimensions=["corpus"],
            comparison_questions=["Which papers require a later directed comparison?"],
            required_evidence_ids=_stable_unique(
                evidence_id
                for dossier in dossiers.values()
                for values in dossier.evidence_ids_by_field.values()
                for evidence_id in values
            ),
            logical_node_id="topic_synthesis:corpus_review",
            estimated_input_tokens=_token_estimate({
                "index_cards": [card.to_dict() for card in content_layers.index_cards],
                "evidence_package_ids": [dossier.dossier_id for dossier in dossiers.values()],
            }),
        ))
    bridge_count = {paper_id: len(topic_ids) for paper_id, topic_ids in paper_topics.items()}
    bridge_papers = sorted(paper_id for paper_id, count in bridge_count.items() if count > 1)
    for topic in topics:
        object.__setattr__(topic, "bridge_paper_ids", [paper_id for paper_id in bridge_papers if paper_id in topic.paper_ids])
    family_group_rows = sorted(
        family_group_rows_by_id.values(), key=lambda item: item["topic_id"]
    )
    cross_group_questions: List[str] = []
    for dimension, dimension_label in (("method", "method"), ("theory", "theory")):
        groups = [
            item for item in family_group_rows
            if item["dimension"] == dimension
        ]
        if not groups:
            continue
        group_text = "; ".join(
            f"{item['family_id']} ({item['label']}; "
            + ", ".join(
                f"{paper_id}:{','.join(labels)}"
                for paper_id, labels in sorted(item["source_labels_by_paper"].items())
            )
            + ")"
            for item in groups
        )
        cross_group_questions.append(
            f"Cross-paper {dimension_label} family relationships (navigation hypotheses, not evidence of equivalence): "
            f"{group_text}. Compare the listed paper-specific {dimension_label} questions using their source records; "
            "preserve exact paper/study ownership, qualifier conditions, findings, and limits, and state where labels "
            "do not support a substantive comparison."
        )
    substantive = [topic for topic in topics if topic.topic_id != "topic:outlier_pool"]
    for left, right in zip(substantive, substantive[1:]):
        cross_group_questions.append(
            f"Compare {left.topic_id} with {right.topic_id}: do their findings conflict, qualify one another, or differ only by method/context?"
        )
    if len(substantive) >= 2:
        cross_group_questions.append("Which bridge papers or research units connect the strongest cross-topic mechanism and boundary evidence?")
        cross_group_questions.append("Which low-frequency, zero-result, or unresolved findings remain visible across topic routes?")
    ordered_questions: List[str] = []
    seen_questions: set[str] = set()
    for question in cross_group_questions:
        normalized = _safe_text(question).casefold()
        if normalized and normalized not in seen_questions:
            seen_questions.add(normalized)
            ordered_questions.append(_safe_text(question))
    cross_group_questions = ordered_questions[:5]
    coverage = {
        "input_paper_count": len(content_layers.index_cards),
        "routed_paper_count": len({paper_id for topic in topics for paper_id in topic.paper_ids}),
        "topic_count": len(topics),
        "outlier_paper_ids": outliers,
        "bridge_paper_ids": bridge_papers,
        "unrouted_paper_ids": sorted(set(paper_topics) - {paper_id for topic in topics for paper_id in topic.paper_ids}),
        "topic_family_grouping": {
            "version": _TOPIC_FAMILY_GROUPING_VERSION,
            "family_group_count": len(family_group_rows),
            "method_family_group_count": sum(
                item["dimension"] == "method" for item in family_group_rows
            ),
            "theory_family_group_count": sum(
                item["dimension"] == "theory" for item in family_group_rows
            ),
            "source_label_occurrence_count": sum(
                int(item["source_label_occurrence_count"]) for item in family_group_rows
            ),
            "groups": family_group_rows,
        },
    }
    return topics, cross_group_questions, coverage


def _call_plan(
    *,
    content_layers: PaperContentLayers,
    topics: Sequence[TopicRoute],
    cross_group_questions: Sequence[str],
    relation_bundles: Sequence[RelationEvidenceBundle],
    physical_call_limit: int,
    retry_fallback_reserve: int,
) -> Tuple[List[SemanticCallPlan], Dict[str, Any]]:
    shared_hash = content_layers.content_hash
    topic_navigation = [
        {
            "topic_id": topic.topic_id,
            "question": topic.question,
            "paper_count": len(topic.paper_ids),
            "paper_ids": topic.paper_ids,
            "bridge_paper_ids": topic.bridge_paper_ids,
            "dimensions": topic.dimensions,
            "required_evidence_count": len(topic.required_evidence_ids),
        }
        for topic in topics
    ]
    relation_navigation = [
        {
            "relation_id": item.relation_id,
            "relation_type": item.relation_type,
            "paper_ids": item.paper_ids,
            "evidence_completeness": item.evidence_completeness,
            "decision": item.decision,
            "missing_evidence_count": len(item.missing_evidence_ids),
            "claim_ids_left": item.claim_ids_left,
            "claim_ids_right": item.claim_ids_right,
        }
        for item in relation_bundles
    ]
    calls: List[SemanticCallPlan] = [
        SemanticCallPlan(
            logical_node_id="global_navigation",
            role="navigation",
            input_hashes=[shared_hash],
            complete_input_tokens=_token_estimate({"index_cards": [item.to_dict() for item in content_layers.index_cards], "schema": "navigation"}) + 2100,
            output_reserve_tokens=2500,
            expected_physical_calls=None,
            retry_fallback_reserve=0,
        )
    ]
    calls.extend(
        SemanticCallPlan(
            logical_node_id=topic.logical_node_id,
            role="topic_synthesis",
            input_hashes=[shared_hash, compute_v3_hash(topic.to_dict())],
            complete_input_tokens=topic.estimated_input_tokens + 2100,
            output_reserve_tokens=3500,
            expected_physical_calls=None,
            retry_fallback_reserve=0,
        )
        for topic in topics
    )
    calls.extend(
        SemanticCallPlan(
            logical_node_id=f"cross_group_comparison:{index + 1}",
            role="cross_group_comparison",
            input_hashes=[shared_hash, compute_v3_hash(question)],
            complete_input_tokens=_token_estimate({"question": question, "topic_navigation": topic_navigation}) + 2100,
            output_reserve_tokens=3500,
            expected_physical_calls=None,
            retry_fallback_reserve=0,
        )
        for index, question in enumerate(cross_group_questions)
    )
    calls.append(SemanticCallPlan(
        logical_node_id="relation_adjudication",
        role="directed_relation_adjudication",
        input_hashes=[shared_hash, compute_v3_hash(relation_navigation)],
        complete_input_tokens=_token_estimate({
            "relation_navigation": relation_navigation,
            "required_evidence_policy": "findings and boundaries must be complete before substantive judgment",
        }) + 2200,
        output_reserve_tokens=3500,
        expected_physical_calls=None,
        retry_fallback_reserve=min(1, max(0, retry_fallback_reserve)),
    ))
    calls.append(SemanticCallPlan(
        logical_node_id="global_synthesis",
        role="global_synthesis",
        input_hashes=[shared_hash, *[compute_v3_hash(topic.to_dict()) for topic in topics]],
        complete_input_tokens=_token_estimate({
            "topics": [
                {
                    "topic_id": topic["topic_id"],
                    "question": topic["question"],
                    "paper_count": topic["paper_count"],
                    "bridge_paper_ids": topic["bridge_paper_ids"],
                    "dimensions": topic["dimensions"],
                }
                for topic in topic_navigation
            ],
            "relations": [
                {
                    "relation_id": item["relation_id"],
                    "relation_type": item["relation_type"],
                    "paper_count": len(item["paper_ids"]),
                    "evidence_completeness": item["evidence_completeness"],
                    "missing_evidence_count": item["missing_evidence_count"],
                }
                for item in relation_navigation
            ],
        }) + 2500,
        output_reserve_tokens=5000,
        expected_physical_calls=None,
        retry_fallback_reserve=min(2, max(0, retry_fallback_reserve - 1)),
    ))
    planned = (
        sum(int(item.expected_physical_calls or 0) for item in calls)
        if all(item.expected_physical_calls is not None for item in calls)
        else None
    )
    retry_reserve = (
        sum(item.retry_fallback_reserve for item in calls)
        if planned is not None
        else None
    )
    budgets = {
        "single_input_target_tokens": "12000-24000",
        "single_input_hard_limit_tokens": 32000,
        "call_plan_input_tokens_include": ["system", "schema", "tools", "navigation", "evidence", "serialization_wrapper"],
        "logical_node_count": len(calls),
        "estimated_physical_calls": planned,
        "retry_fallback_reserve": retry_reserve,
        "physical_call_limit": physical_call_limit,
        "within_physical_call_limit": (
            planned + int(retry_reserve or 0) <= physical_call_limit
            if planned is not None
            else None
        ),
        "provider_request_plan_status": "not_planned_navigation_only",
        "provider_request_plan_scope": "navigation_and_relation_candidates_only",
        "provider_posts_emitted": 0,
        "estimated_cost": None,
        "cost_status": "unknown",
        "pricing_source": "unknown",
        "relation_bundle_count": len(relation_bundles),
        "relation_navigation_count": len(relation_navigation),
        "topic_navigation_only": True,
        "input_hard_limit_tokens": 32000,
        "input_estimate_scope": "navigation_projection_not_serialized_provider_request",
        "navigation_oversized_logical_nodes": [
            item.logical_node_id for item in calls if item.complete_input_tokens > 32000
        ],
        "navigation_estimate_within_32000": all(
            item.complete_input_tokens <= 32000 for item in calls
        ),
        "within_input_hard_limit": None,
    }
    return calls, budgets


def _select_relation_bundles(
    bundles: Sequence[RelationEvidenceBundle],
    *,
    target_tokens: int = 24000,
) -> Tuple[List[RelationEvidenceBundle], List[str]]:
    """Select a bounded, high-risk relation set for the next provider node."""

    priority = {
        "contradicts": 0,
        "explains_discrepancy": 1,
        "qualifies": 2,
        "bridge_between_topics": 3,
        "replicates": 4,
        "extends": 5,
        "supports": 6,
    }
    ordered = sorted(
        bundles,
        key=lambda item: (
            priority.get(item.relation_type, 9),
            0 if item.is_complete else 1,
            -len(item.missing_evidence_ids),
            item.relation_id,
        ),
    )
    selected: List[RelationEvidenceBundle] = []
    selected_tokens = 0
    for bundle in ordered:
        estimate = _token_estimate(bundle.to_dict())
        if estimate > target_tokens:
            continue
        if selected and selected_tokens + estimate > target_tokens:
            continue
        selected.append(bundle)
        selected_tokens += estimate
    if not selected and ordered:
        # Retain one oversized relation as an explicit blocked item rather than
        # dropping it or slicing its evidence text.
        selected.append(ordered[0])
    selected_ids = {item.relation_id for item in selected}
    return selected, sorted(selected_ids)


def build_semantic_chunk_plan(
    content_layers: PaperContentLayers,
    relation_map: GlobalRelationMap | None = None,
    *,
    candidate_count: int = 3,
    physical_call_limit: int = DEFAULT_PROVIDER_CALL_BUDGET,
    retry_fallback_reserve: int = 3,
    reuse_inventory: Sequence[ReuseInventoryItem | Mapping[str, Any]] = (),
) -> SemanticChunkPlan:
    """Build the shared semantic plan used by all later candidate outlines."""

    if candidate_count <= 0:
        raise ValueError("candidate_count must be positive")
    if physical_call_limit < 0:
        raise ValueError("physical_call_limit cannot be negative")
    physical_call_limit = authorized_provider_call_limit(physical_call_limit)
    topics, cross_group_questions, coverage = _build_topics(content_layers)
    relation_bundles = [
        build_relation_evidence_bundle(candidate, content_layers)
        for candidate in (relation_map.relations if relation_map is not None else [])
    ]
    selected_relation_bundles, selected_relation_ids = _select_relation_bundles(relation_bundles)
    calls, budgets = _call_plan(
        content_layers=content_layers,
        topics=topics,
        cross_group_questions=cross_group_questions,
        relation_bundles=selected_relation_bundles,
        physical_call_limit=physical_call_limit,
        retry_fallback_reserve=retry_fallback_reserve,
    )
    diagnostics = list(content_layers.blocking_diagnostics)
    incomplete_selected = [
        item for item in selected_relation_bundles if not item.is_complete
    ]
    for bundle in incomplete_selected:
        diagnostics.append({
            "code": "relation_evidence_incomplete",
            "severity": "blocking",
            "relation_id": bundle.relation_id,
            "missing_evidence_ids": list(bundle.missing_evidence_ids),
            "message": "A selected relation has no complete dossier evidence; it must remain insufficient_evidence until the missing unit is materialized.",
        })
    if budgets["within_physical_call_limit"] is False:
        diagnostics.append({
            "code": "physical_call_budget_exceeded",
            "severity": "blocking",
            "estimated_physical_calls": budgets["estimated_physical_calls"],
            "retry_fallback_reserve": budgets["retry_fallback_reserve"],
            "physical_call_limit": physical_call_limit,
            "message": "The plan is retained but cannot start provider execution under the configured physical-call gate.",
        })
    if budgets["within_input_hard_limit"] is False:
        diagnostics.append({
            "code": "logical_node_input_exceeds_hard_limit",
            "severity": "blocking",
            "logical_node_ids": list(budgets["navigation_oversized_logical_nodes"]),
            "input_hard_limit_tokens": budgets["input_hard_limit_tokens"],
            "message": "A logical request must be split by semantic unit or use directed evidence retrieval; no evidence may be silently truncated.",
        })
    if coverage["unrouted_paper_ids"]:
        diagnostics.append({
            "code": "papers_not_routed",
            "severity": "blocking",
            "paper_ids": list(coverage["unrouted_paper_ids"]),
            "message": "Every ready paper must have a topic or explicit outlier route.",
        })
    coverage.update({
        "relation_candidate_count": len(relation_bundles),
        "selected_relation_count": len(selected_relation_bundles),
        "selected_relation_ids": selected_relation_ids,
        "unselected_relation_count": max(0, len(relation_bundles) - len(selected_relation_bundles)),
        "unselected_relation_policy": "retained_locally_for_directed_evidence_retrieval",
    })
    budgets["relation_candidate_count"] = len(relation_bundles)
    budgets["selected_relation_count"] = len(selected_relation_bundles)
    normalized_reuse = [
        item if isinstance(item, ReuseInventoryItem) else ReuseInventoryItem.from_dict(item)
        for item in reuse_inventory
    ]
    return SemanticChunkPlan(
        content_layers_hash=content_layers.content_hash,
        source_summary_hashes=list(content_layers.source_summary_hashes),
        topics=topics,
        relation_bundles=relation_bundles,
        cross_group_questions=cross_group_questions,
        call_plan=calls,
        reuse_inventory=normalized_reuse,
        budgets=budgets,
        coverage=coverage,
        blocking_diagnostics=diagnostics,
        candidate_count=candidate_count,
    )


def build_topic_synthesis_plan(plan: SemanticChunkPlan) -> List[TopicSynthesis]:
    """Return local planned TopicSynthesis records without provider calls."""

    relation_by_paper: Dict[str, List[str]] = {}
    for relation in plan.relation_bundles:
        for paper_id in relation.paper_ids:
            relation_by_paper.setdefault(paper_id, []).append(relation.relation_id)
    return [
        TopicSynthesis(
            topic_id=topic.topic_id,
            fragment_id=topic.topic_id,
            question=topic.question,
            paper_ids=topic.paper_ids,
            bridge_paper_ids=topic.bridge_paper_ids,
            relation_ids=_stable_unique(
                relation_id
                for paper_id in topic.paper_ids
                for relation_id in relation_by_paper.get(paper_id, [])
            ),
            supporting_evidence_ids=topic.required_evidence_ids,
            unresolved_questions=topic.comparison_questions,
            status="planned",
        )
        for topic in plan.topics
    ]


__all__ = [
    "SEMANTIC_CHUNK_PLAN_ARTIFACT_TYPE",
    "SEMANTIC_CHUNK_PLAN_ARTIFACT_VERSION",
    "SEMANTIC_CHUNK_PLAN_SCHEMA_VERSION",
    "ReuseInventoryItem",
    "SemanticCallPlan",
    "SemanticChunkPlan",
    "TopicRoute",
    "build_paper_content_layers",
    "build_relation_evidence_bundle",
    "build_semantic_chunk_plan",
    "build_topic_synthesis_plan",
    "derive_interpretation_dependencies",
    "derive_unit_source_field_ledger",
]

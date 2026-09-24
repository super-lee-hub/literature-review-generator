"""Provider-free semantic chunk planning for Outline Intelligence v3.

This module is deliberately a local projection and planning boundary.  It
turns canonical Stage 1 summaries into two reusable content layers, builds
evidence-complete relation bundles, and emits a bounded call plan.  It never
calls a provider and it never treats a paper id, a token shard, or a topic
label as proof of a substantive relation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from outline.v3_evidence import build_outline_evidence_views
from outline.v3_models import (
    EVIDENCE_CLAIM_TYPES,
    GlobalRelationMap,
    PaperContentLayers,
    PaperEvidenceDossier,
    PaperIndexCard,
    RelationCandidate,
    RelationEvidenceBundle,
    ResearchUnit,
    EvidenceClaim,
    TopicSynthesis,
    compute_v3_hash,
)
from summary_schema import get_ai_summary


SEMANTIC_CHUNK_PLAN_ARTIFACT_TYPE = "semantic_chunk_plan"
SEMANTIC_CHUNK_PLAN_ARTIFACT_VERSION = "v1"
SEMANTIC_CHUNK_PLAN_SCHEMA_VERSION = "semantic-chunk-plan-v1"

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


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


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
    """Find explicit study-level records without mistaking variable maps for studies."""

    candidates: List[Tuple[str, Mapping[str, Any], str]] = []
    study_keys = {
        "studies",
        "research_units",
        "researchunits",
        "experiments",
        "study_results",
        "individual_studies",
        "study_level_results",
    }

    def walk(node: Any, locator: str) -> None:
        if isinstance(node, Mapping):
            for key in sorted(node, key=lambda item: str(item)):
                child = node[key]
                child_locator = f"{locator}.{key}" if locator else str(key)
                if str(key).casefold().replace("-", "_") in study_keys and isinstance(child, Sequence) and not isinstance(child, (str, bytes)):
                    for index, item in enumerate(child):
                        if not isinstance(item, Mapping):
                            continue
                        marker = (
                            item.get("study_id")
                            or item.get("id")
                            or item.get("study")
                            or item.get("experiment_id")
                            or f"study_{index + 1}"
                        )
                        # A study record should contain at least one study-like
                        # field.  This avoids treating arbitrary lists of maps as
                        # independent studies.
                        keys = {str(k).casefold() for k in item}
                        if keys.intersection({"findings", "results", "method", "sample", "hypotheses", "research_question", "research_questions", "claims", "conclusions"}):
                            candidates.append((str(marker), item, f"{child_locator}[{index}]"))
                walk(child, child_locator)
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            for index, item in enumerate(node):
                walk(item, f"{locator}[{index}]")

    walk(value, "ai_summary")
    seen: set[str] = set()
    result: List[Tuple[str, Mapping[str, Any], str]] = []
    for marker, record, locator in candidates:
        key = f"{marker.casefold()}|{locator}"
        if key in seen:
            continue
        seen.add(key)
        result.append((marker, record, locator))
    return result


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


def _make_dossier(view: Any, summary: Mapping[str, Any]) -> PaperEvidenceDossier:
    paper_id = str(view.paper_key)
    source_hash = str(view.source_summary_hash or _summary_hash(summary))
    try:
        ai_summary = get_ai_summary(summary)
    except (TypeError, ValueError, KeyError):
        ai_summary = _as_mapping(summary.get("ai_summary"))

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
    if study_records:
        for index, (marker, record, locator) in enumerate(study_records):
            study_id = f"{paper_id}:study:{_normalise_label(marker) or index + 1}"
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
            units.append(ResearchUnit(
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
            ))
    else:
        # A paper without explicit study records still receives one complete
        # paper-level unit.  It is not labelled as a multi-study result.
        units.append(ResearchUnit(
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
        ))

    diagnostics: List[str] = [str(item) for item in (getattr(view, "diagnostics", ()) or ())]
    diagnostics.extend(evidence_id_diagnostics)
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
    expected_physical_calls: int = 0
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
            expected_physical_calls=int(data.get("expected_physical_calls") or 0),
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
    def estimated_physical_calls(self) -> int:
        return sum(max(0, int(item.expected_physical_calls)) for item in self.call_plan)

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
    stop_tokens = {
        "about", "across", "based", "case", "data", "effect", "effects",
        "evidence", "finding", "findings", "from", "into", "paper", "papers",
        "research", "result", "results", "study", "studies", "using", "with",
        "dossier", "author", "authors", "stated", "limitations", "between",
        "experiments", "experiment", "theory", "method", "methods", "token",
    }
    blocked_labels = {
        "dossier", "author", "authors", "stated", "limitations", "between",
        "experiments", "experiment", "theory", "method", "methods",
    }
    for card in cards.values():
        for dimension, field_name in _TOPIC_DIMENSIONS:
            if field_name == "theories":
                values = [
                    tag for tag in card.topic_tags
                    if tag in card.topic_tags and tag not in card.key_constructs
                ]
            elif field_name == "constructs":
                values = card.key_constructs
            elif field_name == "mechanisms":
                values = [tag for tag in card.topic_tags if tag not in card.key_constructs]
            elif field_name == "method":
                values = [card.method_category]
            elif field_name == "sample_or_context":
                values = card.key_boundaries
            else:
                values = []
            for value in values:
                label = _normalise_label(value)
                if not label or label in blocked_labels:
                    continue
                occurrences.setdefault((dimension, label), set()).add(card.paper_id)
                # Exact labels are often too brittle for Stage 1 projections
                # (one paper says "perceived value" and another says
                # "value perceptions").  Token groups are only routing
                # hints; they never establish a relation or a conclusion.
                for token in re.findall(r"[\w\u4e00-\u9fff]{4,}", label, flags=re.UNICODE):
                    if token in stop_tokens:
                        continue
                    occurrences.setdefault((dimension, f"token:{token}"), set()).add(card.paper_id)
    shared = {
        (dimension, label): sorted(papers)
        for (dimension, label), papers in occurrences.items()
        if len(papers) >= 2
    }
    # Keep the highest-coverage labels and retain the remainder in the outlier
    # pool rather than silently dropping rare theories or methods.
    ranked = sorted(
        shared.items(),
        key=lambda item: (
            -len(item[1]),
            0 if str(item[0][1]).startswith("token:") else 1,
            item[0][0],
            item[0][1],
        ),
    )
    selected = ranked[:12]
    topic_data: Dict[str, Dict[str, Any]] = {}
    paper_topics: Dict[str, List[str]] = {paper_id: [] for paper_id in cards}
    for (dimension, label), paper_ids in selected:
        topic_id = f"topic:{dimension}:{hashlib.sha256(label.encode('utf-8')).hexdigest()[:12]}"
        topic_data[topic_id] = {"dimension": dimension, "label": label, "paper_ids": paper_ids}
        for paper_id in paper_ids:
            paper_topics.setdefault(paper_id, []).append(topic_id)
    return topic_data, paper_topics


def _build_topics(content_layers: PaperContentLayers) -> Tuple[List[TopicRoute], List[str], Dict[str, Any]]:
    topic_data, paper_topics = _topic_candidates(content_layers)
    dossiers = content_layers.dossier_by_paper
    cards = {card.paper_id: card for card in content_layers.index_cards}
    topics: List[TopicRoute] = []
    for topic_id, data in sorted(topic_data.items()):
        paper_ids = list(data["paper_ids"])
        required = [
            evidence_id
            for paper_id in paper_ids
            for values in dossiers.get(paper_id, PaperEvidenceDossier("", paper_id)).evidence_ids_by_field.values()
            for evidence_id in values
        ]
        topic_label = str(data["label"])
        topics.append(TopicRoute(
            topic_id=topic_id,
            question=f"How do the included studies sharing {topic_label!r} differ in findings, mechanisms, methods, and boundaries?",
            paper_ids=paper_ids,
            dimensions=[str(data["dimension"])],
            comparison_questions=[
                "Are apparent conflicts explained by operationalization, sample, method, or context?",
                "Which mechanism and boundary claims have direct evidence rather than reviewer inference?",
            ],
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
    outliers = sorted(paper_id for paper_id, topic_ids in paper_topics.items() if not topic_ids)
    if outliers:
        topics.append(TopicRoute(
            topic_id="topic:outlier_pool",
            question="Which less frequent or cross-cutting findings require explicit preservation outside the dominant topic routes?",
            paper_ids=outliers,
            dimensions=["outlier"],
            comparison_questions=["What evidence would be lost if these papers were forced into a dominant topic?"],
            required_evidence_ids=_stable_unique(
                evidence_id
                for paper_id in outliers
                for values in dossiers.get(paper_id, PaperEvidenceDossier("", paper_id)).evidence_ids_by_field.values()
                for evidence_id in values
            ),
            logical_node_id="topic_synthesis:outlier_pool",
            estimated_input_tokens=_token_estimate({
                "topic": "outlier_pool",
                "index_cards": [cards[paper_id].to_dict() for paper_id in outliers if paper_id in cards],
                "evidence_package_ids": [dossiers[paper_id].dossier_id for paper_id in outliers if paper_id in dossiers],
                "output_schema": "TopicSynthesis",
            }),
            status="outlier_review",
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
    cross_group_questions: List[str] = []
    substantive = [topic for topic in topics if topic.topic_id != "topic:outlier_pool"]
    for left, right in zip(substantive, substantive[1:]):
        cross_group_questions.append(
            f"Compare {left.topic_id} with {right.topic_id}: do their findings conflict, qualify one another, or differ only by method/context?"
        )
    if len(substantive) >= 2:
        cross_group_questions.append("Which bridge papers or research units connect the strongest cross-topic mechanism and boundary evidence?")
        cross_group_questions.append("Which low-frequency, zero-result, or unresolved findings remain visible across topic routes?")
    cross_group_questions = _stable_unique(cross_group_questions)[:5]
    coverage = {
        "input_paper_count": len(content_layers.index_cards),
        "routed_paper_count": len({paper_id for topic in topics for paper_id in topic.paper_ids}),
        "topic_count": len(topics),
        "outlier_paper_ids": outliers,
        "bridge_paper_ids": bridge_papers,
        "unrouted_paper_ids": sorted(set(paper_topics) - {paper_id for topic in topics for paper_id in topic.paper_ids}),
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
            expected_physical_calls=0,
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
            expected_physical_calls=0,
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
            expected_physical_calls=0,
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
        expected_physical_calls=1,
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
        expected_physical_calls=0,
        retry_fallback_reserve=min(2, max(0, retry_fallback_reserve - 1)),
    ))
    planned = sum(item.expected_physical_calls for item in calls)
    retry_reserve = sum(item.retry_fallback_reserve for item in calls)
    budgets = {
        "single_input_target_tokens": "12000-24000",
        "single_input_hard_limit_tokens": 32000,
        "call_plan_input_tokens_include": ["system", "schema", "tools", "navigation", "evidence", "serialization_wrapper"],
        "logical_node_count": len(calls),
        "estimated_physical_calls": planned,
        "retry_fallback_reserve": retry_reserve,
        "physical_call_limit": physical_call_limit,
        "within_physical_call_limit": planned + retry_reserve <= physical_call_limit,
        "provider_posts_emitted": 0,
        "estimated_cost": None,
        "cost_status": "unknown",
        "pricing_source": "unknown",
        "relation_bundle_count": len(relation_bundles),
        "relation_navigation_count": len(relation_navigation),
        "topic_navigation_only": True,
        "input_hard_limit_tokens": 32000,
        "oversized_logical_nodes": [
            item.logical_node_id for item in calls if item.complete_input_tokens > 32000
        ],
        "within_input_hard_limit": all(item.complete_input_tokens <= 32000 for item in calls),
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
    physical_call_limit: int = 24,
    retry_fallback_reserve: int = 3,
    reuse_inventory: Sequence[ReuseInventoryItem | Mapping[str, Any]] = (),
) -> SemanticChunkPlan:
    """Build the shared semantic plan used by all later candidate outlines."""

    if candidate_count <= 0:
        raise ValueError("candidate_count must be positive")
    if physical_call_limit < 0:
        raise ValueError("physical_call_limit cannot be negative")
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
    if not budgets["within_physical_call_limit"]:
        diagnostics.append({
            "code": "physical_call_budget_exceeded",
            "severity": "blocking",
            "estimated_physical_calls": budgets["estimated_physical_calls"],
            "retry_fallback_reserve": budgets["retry_fallback_reserve"],
            "physical_call_limit": physical_call_limit,
            "message": "The plan is retained but cannot start provider execution under the configured physical-call gate.",
        })
    if not budgets["within_input_hard_limit"]:
        diagnostics.append({
            "code": "logical_node_input_exceeds_hard_limit",
            "severity": "blocking",
            "logical_node_ids": list(budgets["oversized_logical_nodes"]),
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
    "SemanticCallPlan",
    "TopicRoute",
    "ReuseInventoryItem",
    "SemanticChunkPlan",
    "build_paper_content_layers",
    "build_relation_evidence_bundle",
    "build_semantic_chunk_plan",
    "build_topic_synthesis_plan",
]

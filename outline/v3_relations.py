"""Provider-free relation candidates and organizing-axis planning for v3."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from outline.v3_evidence import MATRIX_DIMENSIONS, build_multi_view_matrix
from outline.v3_models import (
    CoverageContract,
    GlobalCorpusLedger,
    GlobalRelationMap,
    MultiViewMatrix,
    OrganizingAxis,
    OutlineCandidatePlan,
    OutlineCandidatePlans,
    OutlineEvidenceView,
    OutlineEvidenceViews,
    RelationCandidate,
    ReviewIntent,
    compute_v3_hash,
)


RELATION_TYPES: Tuple[str, ...] = (
    "supports",
    "extends",
    "contradicts",
    "qualifies",
    "replicates",
    "uses_same_theory",
    "studies_same_construct",
    "studies_same_mechanism",
    "different_context",
    "different_method",
    "explains_discrepancy",
    "bridge_between_topics",
    "historical_predecessor",
    "conceptual_integration",
)

_RELATION_DIMENSION_TYPES = {
    "theory": "uses_same_theory",
    "construct": "studies_same_construct",
    "mechanism": "studies_same_mechanism",
}

_TEXT_SIGNAL_PATTERNS = {
    "support": ("support", "positive", "increase", "enhance", "significant", "consistent", "confirmed"),
    "contradict": ("contradict", "inconsistent", "oppos", "negative", "fail", "not support", "mixed result"),
    "qualify": ("qualif", "boundary", "condition", "moderate", "contingent", "limitation", "depends"),
    "replicate": ("replicat", "reproduc", "robustness check", "same result"),
    "extend": ("extend", "advance", "build on", "integrat", "new mechanism", "novel"),
}

_AXIS_SPECS = (
    (
        "theory_evolution",
        "Theory evolution",
        "Trace how theoretical explanations develop, qualify, and integrate across the corpus.",
        ["theory", "year", "development"],
        ["uses_same_theory", "historical_predecessor", "extends", "qualifies"],
    ),
    (
        "mechanism_integration",
        "Mechanism integration",
        "Connect constructs and explicitly recorded mechanisms across contexts and methods.",
        ["mechanism", "construct", "context"],
        ["studies_same_mechanism", "studies_same_construct", "bridge_between_topics", "conceptual_integration"],
    ),
    (
        "controversy",
        "Controversy and boundary conditions",
        "Surface supporting, contrary, and qualifying evidence without collapsing disagreement.",
        ["finding", "limitation", "context", "method"],
        ["supports", "contradicts", "qualifies", "explains_discrepancy"],
    ),
    (
        "problem_evidence_synthesis",
        "Problem, evidence, and synthesis",
        "Organize from research problems through methods and findings to supported gaps.",
        ["gap", "method", "finding", "development"],
        ["supports", "extends", "bridge_between_topics", "explains_discrepancy"],
    ),
    (
        "context_boundaries",
        "Context boundaries",
        "Compare where the evidence travels, where methods differ, and where context limits claims.",
        ["context", "method", "construct", "limitation"],
        ["different_context", "different_method", "qualifies", "replicates"],
    ),
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


def _evidence_views(value: OutlineEvidenceViews | Sequence[OutlineEvidenceView]) -> Tuple[List[OutlineEvidenceView], List[Dict[str, Any]], Dict[str, str]]:
    if isinstance(value, OutlineEvidenceViews):
        return list(value.views), list(value.blocking_diagnostics), {"outline_evidence_views": value.content_hash}
    views = [item for item in value if isinstance(item, OutlineEvidenceView)]
    return views, [], {}


def _view_field_values(view: OutlineEvidenceView, dimension: str) -> List[str]:
    fields = {
        "theory": view.theories,
        "construct": view.constructs,
        "mechanism": view.mechanisms,
        "context": view.sample_or_context,
        "method": view.method,
        "finding": view.findings,
        "limitation": view.limitations,
        "gap": view.research_gaps,
        "year": [str(view.year)] if view.year is not None else [],
        "development": view.future_directions,
    }
    return _stable_unique(fields.get(dimension, []))


def _source_fields(view: OutlineEvidenceView, dimension: str) -> List[str]:
    mapping = {
        "theory": "theories",
        "construct": "constructs",
        "mechanism": "mechanisms",
        "context": "sample_or_context",
        "method": "method",
        "finding": "findings",
        "limitation": "limitations",
        "gap": "research_gaps",
        "year": "year",
        "development": "future_directions",
    }
    field = mapping.get(dimension, dimension)
    return list(view.source_fields.get(field, [field]))


def _text_for_relation(view: OutlineEvidenceView) -> str:
    return " ".join([
        *view.findings,
        *view.conclusions,
        *view.limitations,
        *view.research_gaps,
        *view.future_directions,
    ]).casefold()


def _has_signal(view: OutlineEvidenceView, signal: str) -> bool:
    text = _text_for_relation(view)
    return any(pattern in text for pattern in _TEXT_SIGNAL_PATTERNS[signal])


def _relation(
    relation_type: str,
    source: OutlineEvidenceView,
    target: OutlineEvidenceView,
    *,
    dimension: str,
    labels: Sequence[str],
    confidence: str = "low",
    evidence_fields: Optional[Mapping[str, Sequence[str]]] = None,
) -> RelationCandidate:
    paper_keys = [source.paper_key, target.paper_key]
    normalized_keys = sorted(set(paper_keys))
    source_fields = {
        source.paper_key: _stable_unique(_source_fields(source, dimension)),
        target.paper_key: _stable_unique(_source_fields(target, dimension)),
    }
    if evidence_fields is None:
        evidence_fields = {
            source.paper_key: [dimension],
            target.paper_key: [dimension],
        }
    relation_payload = {
        "relation_type": relation_type,
        "paper_keys": normalized_keys,
        "dimension": dimension,
        "labels": _stable_unique(labels),
        "evidence_fields": {
            key: _stable_unique(values)
            for key, values in sorted(evidence_fields.items())
        },
        "source_fields": source_fields,
    }
    relation_id = f"relation-{compute_v3_hash(relation_payload)[:24]}"
    return RelationCandidate(
        relation_id=relation_id,
        relation_type=relation_type,
        paper_keys=normalized_keys,
        source_paper_key=source.paper_key,
        target_paper_key=target.paper_key,
        dimension=dimension,
        evidence_fields={key: _stable_unique(values) for key, values in evidence_fields.items()},
        confidence=confidence,
        source_fields=source_fields,
        supporting_labels=_stable_unique(labels),
    )


def _add_relation(relations: Dict[str, RelationCandidate], relation: RelationCandidate) -> None:
    relations.setdefault(relation.relation_id, relation)


def build_global_relation_map(
    evidence: OutlineEvidenceViews | Sequence[OutlineEvidenceView],
    matrix: Optional[MultiViewMatrix] = None,
    ledger: Optional[GlobalCorpusLedger] = None,
    *,
    max_pairs_per_label: int = 2000,
) -> GlobalRelationMap:
    """Build sparse relation candidates from shared evidence dimensions.

    The inverted index is the important boundary: papers are paired only when
    they share a recorded dimension value.  A cap on pathological labels keeps
    the relation map from silently becoming a full corpus cross-product; the
    cap is recorded as a blocking diagnostic for adoption.
    """

    views, blocking, source_hashes = _evidence_views(evidence)
    views_by_key = {view.paper_key: view for view in views}
    if matrix is None and isinstance(evidence, OutlineEvidenceViews):
        matrix = build_multi_view_matrix(evidence)
    if matrix is None:
        matrix = build_multi_view_matrix(views)
    source_hashes["multi_view_matrix"] = matrix.content_hash
    blocking.extend(matrix.blocking_diagnostics)
    if ledger is not None:
        source_hashes["global_corpus_ledger"] = ledger.content_hash
        blocking.extend(ledger.blocking_diagnostics)

    index: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for row in sorted(matrix.rows, key=lambda item: item.paper_key):
        for dimension in MATRIX_DIMENSIONS:
            for label in _stable_unique(row.dimensions.get(dimension, [])):
                index[(dimension, label)].append(row.paper_key)

    pair_dimensions: Dict[Tuple[str, str], Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
    for (dimension, label), paper_keys in sorted(index.items()):
        keys = sorted(set(paper_keys))
        if len(keys) > max_pairs_per_label:
            blocking.append({
                "code": "relation_label_pair_cap",
                "severity": "blocking",
                "dimension": dimension,
                "label": label,
                "paper_count": len(keys),
                "max_pairs_per_label": max_pairs_per_label,
                "message": "Relation candidates were capped for a high-frequency label.",
            })
            keys = keys[:max_pairs_per_label]
        for left_index, left_key in enumerate(keys):
            for right_key in keys[left_index + 1:]:
                pair_dimensions[(left_key, right_key)][dimension].add(label)

    relations: Dict[str, RelationCandidate] = {}
    for (left_key, right_key), dimension_map in sorted(pair_dimensions.items()):
        left = views_by_key.get(left_key)
        right = views_by_key.get(right_key)
        if left is None or right is None:
            continue
        shared_dimensions = sorted(dimension_map)

        for dimension in shared_dimensions:
            relation_type = _RELATION_DIMENSION_TYPES.get(dimension)
            if relation_type:
                _add_relation(relations, _relation(
                    relation_type,
                    left,
                    right,
                    dimension=dimension,
                    labels=sorted(dimension_map[dimension]),
                    confidence="medium",
                ))

        left_context = set(_view_field_values(left, "context"))
        right_context = set(_view_field_values(right, "context"))
        if left_context and right_context and left_context != right_context:
            _add_relation(relations, _relation(
                "different_context", left, right, dimension="context",
                labels=sorted(left_context | right_context),
                confidence="medium",
            ))

        left_method = set(_view_field_values(left, "method"))
        right_method = set(_view_field_values(right, "method"))
        if left_method and right_method and left_method != right_method:
            _add_relation(relations, _relation(
                "different_method", left, right, dimension="method",
                labels=sorted(left_method | right_method),
                confidence="medium",
            ))

        # A bridge candidate requires overlap plus non-identical topical
        # coverage.  It is a planning signal, not a claim that the papers are
        # conceptually integrated.
        left_labels = {label for labels in (_view_field_values(left, dimension) for dimension in MATRIX_DIMENSIONS) for label in labels}
        right_labels = {label for labels in (_view_field_values(right, dimension) for dimension in MATRIX_DIMENSIONS) for label in labels}
        if len(shared_dimensions) >= 2 or (left_labels & right_labels and left_labels != right_labels):
            _add_relation(relations, _relation(
                "bridge_between_topics", left, right, dimension=shared_dimensions[0],
                labels=sorted(left_labels & right_labels), confidence="low",
            ))

        left_year = left.year
        right_year = right.year
        substantive_overlap = any(dimension in shared_dimensions for dimension in ("theory", "construct", "mechanism", "finding"))
        if substantive_overlap and left_year is not None and right_year is not None and left_year != right_year:
            older, newer = (left, right) if left_year < right_year else (right, left)
            _add_relation(relations, _relation(
                "historical_predecessor", older, newer, dimension="year",
                labels=[str(older.year), str(newer.year)], confidence="low",
            ))

        if (left.paper_type == "conceptual" or right.paper_type == "conceptual") and any(
            dimension in shared_dimensions for dimension in ("theory", "construct", "mechanism")
        ):
            _add_relation(relations, _relation(
                "conceptual_integration", left, right,
                dimension=next(d for d in shared_dimensions if d in {"theory", "construct", "mechanism"}),
                labels=sorted(left_labels & right_labels), confidence="low",
            ))

        left_support = _has_signal(left, "support")
        right_support = _has_signal(right, "support")
        left_contradict = _has_signal(left, "contradict")
        right_contradict = _has_signal(right, "contradict")
        relation_dimension = next((d for d in shared_dimensions if d in {"theory", "construct", "mechanism", "context"}), shared_dimensions[0])
        shared_labels = sorted(dimension_map[relation_dimension])
        evidence_fields = {
            left.paper_key: ["findings", "conclusions"],
            right.paper_key: ["findings", "conclusions"],
        }
        if (left_support and right_support) and not (left_contradict or right_contradict):
            _add_relation(relations, _relation(
                "supports", left, right, dimension=relation_dimension,
                labels=shared_labels, confidence="low", evidence_fields=evidence_fields,
            ))
        if left_contradict or right_contradict:
            _add_relation(relations, _relation(
                "contradicts", left, right, dimension=relation_dimension,
                labels=shared_labels, confidence="low", evidence_fields=evidence_fields,
            ))
            if (left_context and right_context and left_context != right_context) or (left_method and right_method and left_method != right_method):
                _add_relation(relations, _relation(
                    "explains_discrepancy", left, right, dimension=relation_dimension,
                    labels=shared_labels, confidence="low", evidence_fields=evidence_fields,
                ))
        if _has_signal(left, "qualify") or _has_signal(right, "qualify"):
            _add_relation(relations, _relation(
                "qualifies", left, right, dimension=relation_dimension,
                labels=shared_labels, confidence="low", evidence_fields=evidence_fields,
            ))
        if _has_signal(left, "replicate") or _has_signal(right, "replicate"):
            _add_relation(relations, _relation(
                "replicates", left, right, dimension=relation_dimension,
                labels=shared_labels, confidence="low", evidence_fields=evidence_fields,
            ))
        if _has_signal(left, "extend") or _has_signal(right, "extend"):
            _add_relation(relations, _relation(
                "extends", left, right, dimension=relation_dimension,
                labels=shared_labels, confidence="low", evidence_fields=evidence_fields,
            ))

    return GlobalRelationMap(
        relations=[relations[key] for key in sorted(relations)],
        paper_keys=sorted(views_by_key),
        source_artifact_hashes=source_hashes,
        blocking_diagnostics=blocking,
    )


def _preferred_axis_order(intent: Optional[ReviewIntent]) -> List[str]:
    axis_ids = [spec[0] for spec in _AXIS_SPECS]
    preferred = _safe_text(intent.preferred_organizing_logic if intent else "").casefold().replace("-", "_").replace(" ", "_")
    if preferred not in axis_ids:
        return axis_ids
    return [preferred, *[axis_id for axis_id in axis_ids if axis_id != preferred]]


def build_organizing_axes(intent: Optional[ReviewIntent] = None) -> List[OrganizingAxis]:
    """Return all fixed organizing axes; intent changes preference, not scope."""

    specs_by_id = {spec[0]: spec for spec in _AXIS_SPECS}
    axes: List[OrganizingAxis] = []
    for axis_id in _preferred_axis_order(intent):
        _id, label, rationale, dimensions, relation_types = specs_by_id[axis_id]
        axes.append(OrganizingAxis(
            axis_id=axis_id,
            organizing_logic=axis_id,
            label=label,
            rationale=rationale,
            preferred_dimensions=list(dimensions),
            preferred_relation_types=list(relation_types),
        ))
    return axes


def build_outline_candidate_plans(
    ledger: GlobalCorpusLedger,
    matrix: MultiViewMatrix,
    relation_map: GlobalRelationMap,
    intent: ReviewIntent,
    coverage_contract: CoverageContract,
    *,
    candidate_count: int = 5,
) -> OutlineCandidatePlans:
    """Create deterministic candidate plans with provider generation isolated."""

    if candidate_count <= 0:
        raise ValueError("candidate_count must be positive")
    axes = build_organizing_axes(intent)
    selected_axes = axes[:min(candidate_count, len(axes))]
    shared = {
        "global_corpus_ledger": ledger.content_hash,
        "multi_view_matrix": matrix.content_hash,
        "global_relation_map": relation_map.content_hash,
        "review_intent": intent.content_hash,
        "coverage_contract": coverage_contract.content_hash,
    }
    inherited_blocking = [
        *ledger.blocking_diagnostics,
        *matrix.blocking_diagnostics,
        *relation_map.blocking_diagnostics,
    ]
    candidates: List[OutlineCandidatePlan] = []
    for index, axis in enumerate(selected_axes, start=1):
        candidate_id = f"candidate_{index}"
        diagnostics = ["shared_input_blocked"] if inherited_blocking else []
        candidates.append(OutlineCandidatePlan(
            candidate_id=candidate_id,
            organizing_logic=axis.organizing_logic,
            axis_id=axis.axis_id,
            shared_artifact_hashes=dict(shared),
            required_node_ids=[
                "outline_evidence_views",
                "global_corpus_ledger",
                "multi_view_matrix",
                "relation_candidates",
                "global_relation_map",
                "review_intent",
                "coverage_contract",
                "organizing_axes",
                candidate_id,
            ],
            provider_generation_node_id=f"{candidate_id}_provider_generation",
            status="blocked" if inherited_blocking else "planned",
            diagnostics=diagnostics,
        ))

    return OutlineCandidatePlans(
        axes=axes,
        candidates=candidates,
        shared_artifact_hashes=shared,
        review_intent_hash=intent.content_hash,
        coverage_contract_hash=coverage_contract.content_hash,
        blocking_diagnostics=inherited_blocking,
    )


__all__ = [
    "RELATION_TYPES",
    "build_global_relation_map",
    "build_organizing_axes",
    "build_outline_candidate_plans",
]

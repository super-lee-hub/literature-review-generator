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
    "reported_magnitude_contrast",
    "required_comparison",
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
    return "" if value is None else str(value).strip()


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
    diagnostics: Sequence[str] = (),
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
        diagnostics=_stable_unique(diagnostics),
    )


def _add_relation(relations: Dict[str, RelationCandidate], relation: RelationCandidate) -> None:
    relations.setdefault(relation.relation_id, relation)


_SUBSTANTIVE_DIMENSIONS = ("theory", "construct", "mechanism", "finding")
_OUTCOME_BUCKET_PRIORITY = {
    "support": 0,
    "contradiction": 1,
    "qualifier": 2,
    "zero_effect": 3,
    "mixed": 4,
    "unclassified": 5,
}

_NUMBER_WORDS = {
    "zero": 0.0,
    "one": 1.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
    "six": 6.0,
    "seven": 7.0,
    "eight": 8.0,
    "nine": 9.0,
    "ten": 10.0,
    "eleven": 11.0,
    "twelve": 12.0,
    "fifteen": 15.0,
    "twenty": 20.0,
    "thirty": 30.0,
    "forty": 40.0,
    "fifty": 50.0,
    "sixty": 60.0,
    "seventy": 70.0,
    "eighty": 80.0,
    "ninety": 90.0,
    "hundred": 100.0,
}
_NUMBER_WORD_PATTERN = "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True))
_NUMBER_PATTERN = rf"(?:\d+(?:\.\d+)?|{_NUMBER_WORD_PATTERN})"
_UNIT_PATTERN = r"(?:percentage\s+points?|points?|percent(?:age)?)"
_REPORTED_CHANGE = re.compile(
    rf"\b(?P<verb>improv\w*|increas\w*|rais\w*|rose|ris\w*|grew|decreas\w*|reduc\w*|fell|declin\w*)\s+"
    rf"(?:the\s+)?(?P<metric>[\w][\w -]*?)\s+by\s+"
    rf"(?P<number>{_NUMBER_PATTERN})\s+(?P<unit>{_UNIT_PATTERN})\b",
    re.IGNORECASE,
)
_NO_CHANGE_PATTERNS = (
    re.compile(r"\b(?:did not|didn't|does not|doesn't)\s+change\s+(?:the\s+|a\s+|an\s+)?(?P<metric>[^,.;:]+)", re.IGNORECASE),
    re.compile(r"\bno change in\s+(?:the\s+|a\s+|an\s+)?(?P<metric>[^,.;:]+)", re.IGNORECASE),
    re.compile(r"\b(?:no effect|zero effect|null effect) on\s+(?:the\s+|a\s+|an\s+)?(?P<metric>[^,.;:]+)", re.IGNORECASE),
)


def _semantic_fingerprint(view: OutlineEvidenceView) -> str:
    """Content identity used for cohort/representative selection.

    Paper keys and source hashes are excluded so a key rename or unrelated
    corpus addition cannot choose a different semantic anchor.
    """

    return compute_v3_hash({
        "title": _safe_text(view.title).casefold(),
        "authors": _stable_unique(view.authors),
        "year": view.year,
        "paper_type": _safe_text(view.paper_type).casefold(),
        "research_questions": _stable_unique(view.research_questions),
        "theories": _stable_unique(view.theories),
        "constructs": _stable_unique(view.constructs),
        "mechanisms": _stable_unique(view.mechanisms),
        "method": _stable_unique(view.method),
        "sample_or_context": _stable_unique(view.sample_or_context),
        "findings": _stable_unique(view.findings),
        "conclusions": _stable_unique(view.conclusions),
        "limitations": _stable_unique(view.limitations),
        "research_gaps": _stable_unique(view.research_gaps),
        "future_directions": _stable_unique(view.future_directions),
    })


def _representative_order(view: OutlineEvidenceView) -> Tuple[Any, ...]:
    fields = (
        view.research_questions,
        view.theories,
        view.constructs,
        view.mechanisms,
        view.method,
        view.sample_or_context,
        view.findings,
        view.limitations,
        view.research_gaps,
        view.future_directions,
    )
    richness = sum(len(_stable_unique(values)) for values in fields)
    return (
        -richness,
        _semantic_fingerprint(view),
        _safe_text(view.title).casefold(),
        tuple(_stable_unique(view.authors)),
        str(view.year or ""),
        view.paper_key,
    )


def _finding_bucket(view: OutlineEvidenceView) -> str:
    """Classify recorded finding wording for comparison task routing only."""

    text = " ".join(view.findings).casefold()
    support = any(pattern in text for pattern in _TEXT_SIGNAL_PATTERNS["support"]) or "improv" in text
    contradict = any(pattern in text for pattern in _TEXT_SIGNAL_PATTERNS["contradict"])
    qualify = any(pattern in text for pattern in _TEXT_SIGNAL_PATTERNS["qualify"])
    qualify = qualify or any(pattern in text for pattern in (
        "only", "unless", "except", "conditional", "depends", "contingent",
        "moderator", "moderated", "boundary", "condition", "limited to",
        "restricted to", "when ", "under which",
    ))
    if support and contradict:
        return "mixed"
    if contradict:
        return "contradiction"
    if _reported_zero_effect(view):
        return "zero_effect"
    if qualify:
        return "qualifier"
    if support:
        return "support"
    return "unclassified"


def _has_explicit_qualifier(view: OutlineEvidenceView) -> bool:
    text = " ".join((
        *view.findings,
        *view.limitations,
        *view.research_gaps,
        *view.future_directions,
    )).casefold()
    return any(pattern in text for pattern in (
        "qualif", "only", "unless", "except", "conditional", "depends",
        "contingent", "moderator", "moderated", "boundary", "condition",
        "limited to", "restricted to", "when ", "under which",
    ))


def _normalized_metric(value: str) -> str:
    text = " ".join(value.casefold().split()).strip(" -_.,;:()[]{}")
    text = re.sub(r"^(?:the|a|an)\s+", "", text)
    text = re.sub(r"\b(?:significantly|substantially|slightly|marginally)\b", " ", text)
    return " ".join(text.split()).strip(" -_.,;:()[]{}")


def _number_value(value: str) -> float | None:
    text = value.casefold().strip()
    if text in _NUMBER_WORDS:
        return _NUMBER_WORDS[text]
    try:
        return float(text)
    except ValueError:
        return None


def _unit_key(value: str) -> str:
    text = " ".join(value.casefold().split())
    if text.startswith("percentage point"):
        return "percentage_point"
    if text in {"percent", "percentage"}:
        return "percent"
    if text.startswith("point"):
        return "point"
    return text


def _reported_zero_effect(view: OutlineEvidenceView) -> bool:
    return any(
        pattern.search(text)
        for text in (" ".join(view.findings),)
        for pattern in _NO_CHANGE_PATTERNS
    )


def _reported_magnitudes(view: OutlineEvidenceView) -> Tuple[Tuple[str, str, float, str], ...]:
    """Extract only explicit finding magnitudes; never calculate new effects."""

    observations: Set[Tuple[str, str, float, str]] = set()
    for finding in view.findings:
        for match in _REPORTED_CHANGE.finditer(finding):
            metric = _normalized_metric(match.group("metric"))
            magnitude = _number_value(match.group("number"))
            if not metric or magnitude is None:
                continue
            verb = match.group("verb").casefold()
            direction = "increase" if verb.startswith(("improv", "increas", "rais", "ris", "grew")) else "decrease"
            signed_value = magnitude if direction == "increase" else -magnitude
            observations.add((metric, _unit_key(match.group("unit")), signed_value, direction))
        for pattern in _NO_CHANGE_PATTERNS:
            match = pattern.search(finding)
            if match:
                metric = _normalized_metric(match.group("metric"))
                if metric:
                    observations.add((metric, "", 0.0, "zero"))
    return tuple(sorted(observations))


def _evidence_pattern_signature(view: OutlineEvidenceView) -> Tuple[Tuple[str, ...], ...]:
    """Group repeated source patterns while keeping qualifier variants distinct."""

    def normalize(values: Sequence[Any]) -> Tuple[str, ...]:
        return tuple(sorted({" ".join(value.casefold().split()) for value in _stable_unique(values)}))

    return (
        normalize(view.findings),
        normalize(view.limitations),
        normalize(view.research_gaps),
        normalize(view.future_directions),
    )


def _candidate_pair_key(left: str, right: str) -> Tuple[str, str]:
    first, second = sorted((left, right))
    return first, second


def _normalize_required_comparisons(
    required_comparisons: Sequence[Mapping[str, Any]],
    *,
    views_by_key: Mapping[str, OutlineEvidenceView],
) -> List[Dict[str, Any]]:
    """Validate explicit user-required two-paper comparison tasks.

    These records select a source-organization task. They do not assert a
    relation outcome and are never reconstructed from paper IDs or magnitude
    representatives.
    """

    if not isinstance(required_comparisons, Sequence) or isinstance(
        required_comparisons, (str, bytes)
    ):
        raise ValueError("required_comparisons must be an array")
    normalized: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for index, raw in enumerate(required_comparisons):
        if not isinstance(raw, Mapping):
            raise ValueError(f"required_comparisons[{index}] must be an object")
        raw_keys = raw.get("paper_keys")
        if (
            not isinstance(raw_keys, Sequence)
            or isinstance(raw_keys, (str, bytes))
            or len(raw_keys) != 2
            or any(not isinstance(key, str) or not key.strip() for key in raw_keys)
            or len(set(raw_keys)) != 2
        ):
            raise ValueError(
                f"required_comparisons[{index}] needs exactly two distinct canonical paper_keys"
            )
        paper_keys = sorted(str(key).strip() for key in raw_keys)
        if len(set(paper_keys)) != 2:
            raise ValueError(
                f"required_comparisons[{index}] needs exactly two distinct canonical paper_keys"
            )
        missing_keys = [key for key in paper_keys if key not in views_by_key]
        if missing_keys:
            raise ValueError(
                f"required_comparisons[{index}] references an unknown source paper: {missing_keys[0]}"
            )
        question = raw.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"required_comparisons[{index}] needs a non-empty question")
        question = question.strip()
        relation_type_value = raw.get("relation_type")
        if relation_type_value is not None and not isinstance(relation_type_value, str):
            raise ValueError(f"required_comparisons[{index}].relation_type must be text")
        requested_relation_type = str(relation_type_value or "").strip()
        task_relation_type = requested_relation_type or "required_comparison"
        requirement_id = "required-comparison-" + compute_v3_hash({
            "paper_keys": paper_keys,
            "question": question,
            "relation_type": requested_relation_type,
        })[:24]
        if requirement_id in seen:
            raise ValueError(f"required_comparisons[{index}] duplicates an existing required task")
        seen.add(requirement_id)
        normalized.append({
            "requirement_id": requirement_id,
            "paper_keys": paper_keys,
            "question": question,
            "requested_relation_type": requested_relation_type,
            "task_relation_type": task_relation_type,
        })
    return sorted(normalized, key=lambda item: item["requirement_id"])


def _required_comparison_source_fields(
    view: OutlineEvidenceView,
) -> Dict[str, List[str]]:
    """Return real Stage 1 source paths for findings and boundary support."""

    return {
        "findings": _stable_unique(view.source_fields.get("findings", ())),
        "moderators_boundaries": _stable_unique([
            *view.source_fields.get("limitations", ()),
            *view.source_fields.get("sample_or_context", ()),
        ]),
    }


def build_global_relation_map(
    evidence: OutlineEvidenceViews | Sequence[OutlineEvidenceView],
    matrix: Optional[MultiViewMatrix] = None,
    ledger: Optional[GlobalCorpusLedger] = None,
    *,
    max_pairs_per_label: int = 2000,
    required_comparisons: Sequence[Mapping[str, Any]] = (),
) -> GlobalRelationMap:
    """Build complete cohorts and purposeful sparse relation candidates.

    Shared labels preserve full cohort membership. They do not imply that all
    members need separate scientific comparisons. Pair tasks are proposed only
    for recorded outcome conflict, explicit qualifier, context/method strata
    under a shared substantive axis, or distinct topical signatures. The
    unenumerated pair space remains explicit and available for directed
    retrieval. Explicit ``required_comparisons`` are added as exact, user-owned
    two-paper tasks with both source sides and their finding/boundary fields;
    their question is task metadata, never a relation conclusion.
    ``max_pairs_per_label`` is retained for call compatibility but no longer
    truncates or blocks cohort membership.
    """

    views, blocking, source_hashes = _evidence_views(evidence)
    views_by_key = {view.paper_key: view for view in views}
    normalized_required_comparisons = _normalize_required_comparisons(
        required_comparisons,
        views_by_key=views_by_key,
    )
    if matrix is None and isinstance(evidence, OutlineEvidenceViews):
        matrix = build_multi_view_matrix(evidence)
    if matrix is None:
        matrix = build_multi_view_matrix(views)
    source_hashes["multi_view_matrix"] = matrix.content_hash
    blocking.extend(matrix.blocking_diagnostics)
    if ledger is not None:
        source_hashes["global_corpus_ledger"] = ledger.content_hash
        blocking.extend(ledger.blocking_diagnostics)

    del max_pairs_per_label  # Compatibility only; never use a pair-count cutoff.
    index: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    dimensions_by_key: Dict[str, Dict[str, List[str]]] = {}
    for row in sorted(matrix.rows, key=lambda item: item.paper_key):
        if row.paper_key not in views_by_key:
            continue
        row_dimensions = {
            dimension: _stable_unique(row.dimensions.get(dimension, []))
            for dimension in MATRIX_DIMENSIONS
        }
        dimensions_by_key[row.paper_key] = row_dimensions
        for dimension in MATRIX_DIMENSIONS:
            for label in row_dimensions[dimension]:
                index[(dimension, label)].append(row.paper_key)

    semantic_fingerprints = {
        paper_key: _semantic_fingerprint(view)
        for paper_key, view in views_by_key.items()
    }
    cohorts: List[Dict[str, Any]] = []
    cohort_keys: Dict[str, Tuple[str, ...]] = {}
    singleton_label_group_count = 0
    for (dimension, label), paper_keys in sorted(index.items()):
        keys = tuple(sorted(set(paper_keys)))
        if len(keys) <= 1:
            singleton_label_group_count += 1
            continue
        cohort_fingerprint = compute_v3_hash({
            'dimension': dimension,
            'label': label.casefold(),
            'member_fingerprints': sorted(semantic_fingerprints[key] for key in keys),
        })
        cohort_id = f"cohort-{cohort_fingerprint[:24]}"
        cohort_keys[cohort_id] = keys
        cohorts.append({
            "cohort_id": cohort_id,
            "dimension": dimension,
            "label": label,
            "member_keys": list(keys),
            "member_count": len(keys),
            "possible_pair_count": len(keys) * (len(keys) - 1) // 2,
            "source_fields": {
                key: _stable_unique(_source_fields(views_by_key[key], dimension))
                for key in keys
            },
            "disposition": "deferred",
            "reason": "Shared label membership is preserved; pairwise comparison requires a source-backed contrast.",
            "reason_codes": ["shared_label_only_no_source_contrast_signal"],
            "selected_relation_ids": [],
            "pairwise_exploration_deferred": True,
        })

    selected_pair_reasons: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    selected_pair_cohorts: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    selected_pair_magnitude_labels: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    cohort_selected_reasons: Dict[str, Set[str]] = defaultdict(set)

    def select_pair(
        left_key: str,
        right_key: str,
        reason: str,
        cohort_id: str,
        *,
        magnitude_label: str = "",
    ) -> None:
        if left_key == right_key:
            return
        pair = _candidate_pair_key(left_key, right_key)
        selected_pair_reasons[pair].add(reason)
        selected_pair_cohorts[pair].add(cohort_id)
        if magnitude_label:
            selected_pair_magnitude_labels[pair].add(magnitude_label)
        cohort_selected_reasons[cohort_id].add(reason)

    def representative(keys: Iterable[str]) -> str:
        return min(keys, key=lambda key: _representative_order(views_by_key[key]))

    def pattern_representatives(keys: Iterable[str]) -> List[str]:
        by_signature: Dict[Tuple[Tuple[str, ...], ...], List[str]] = defaultdict(list)
        for key in keys:
            by_signature[_evidence_pattern_signature(views_by_key[key])].append(key)
        return [
            representative(by_signature[signature])
            for signature in sorted(by_signature)
        ]

    def different_representatives(left_keys: Iterable[str], right_keys: Iterable[str]) -> Tuple[str, str] | None:
        left_ordered = sorted(set(left_keys), key=lambda key: _representative_order(views_by_key[key]))
        right_ordered = sorted(set(right_keys), key=lambda key: _representative_order(views_by_key[key]))
        for left_key in left_ordered[:2]:
            for right_key in right_ordered[:2]:
                if left_key != right_key:
                    return left_key, right_key
        return None

    for cohort in cohorts:
        dimension = str(cohort["dimension"])
        if dimension not in _SUBSTANTIVE_DIMENSIONS:
            continue
        cohort_id = str(cohort["cohort_id"])
        keys = cohort_keys[cohort_id]
        cohort_views = {key: views_by_key[key] for key in keys}

        buckets: Dict[str, List[str]] = defaultdict(list)
        for key in keys:
            buckets[_finding_bucket(cohort_views[key])].append(key)

        # Pair every member of the rarer support/contradiction class with a
        # content-selected representative from the other class. This retains
        # counterexamples without emitting the full cross-product.
        support_keys = buckets.get("support", [])
        contradiction_keys = [*buckets.get("contradiction", []), *buckets.get("mixed", [])]
        if support_keys and contradiction_keys:
            if len(support_keys) >= len(contradiction_keys):
                anchor = representative(support_keys)
                contrast_members = contradiction_keys
            else:
                anchor = representative(contradiction_keys)
                contrast_members = support_keys
            for key in pattern_representatives(contrast_members):
                select_pair(key, anchor, "minority_outcome", cohort_id)
                select_pair(key, anchor, "outcome_conflict", cohort_id)

        # Explicit numeric or word-number reports are compared only within a
        # shared metric/unit/signature, then by adjacent distinct magnitude
        # strata. A source-reported zero is paired with its nearest nonzero
        # stratum for the same metric. This preserves useful contrasts without
        # constructing every pair in a broad cohort.
        magnitude_values: Dict[Tuple[str, str, str], Dict[float, Set[str]]] = defaultdict(lambda: defaultdict(set))
        zero_values: Dict[str, Set[str]] = defaultdict(set)
        magnitude_pair_found = False
        for key in keys:
            for metric, unit, value, direction in _reported_magnitudes(cohort_views[key]):
                if direction == "zero":
                    zero_values[metric].add(key)
                else:
                    magnitude_values[(metric, unit, direction)][abs(value)].add(key)
        for (metric, unit, _direction), value_strata in sorted(magnitude_values.items()):
            magnitudes = sorted(value_strata)
            magnitude_label = f"{metric} [{unit}]"
            for lower, upper in zip(magnitudes, magnitudes[1:]):
                pair = different_representatives(value_strata[lower], value_strata[upper])
                if pair is not None:
                    select_pair(
                        *pair,
                        "reported_magnitude_contrast",
                        cohort_id,
                        magnitude_label=magnitude_label,
                    )
                    magnitude_pair_found = True
            if magnitudes and metric in zero_values:
                nearest = magnitudes[0]
                nonzero_representative = representative(value_strata[nearest])
                for zero_key in pattern_representatives(sorted(zero_values[metric])):
                    pair = different_representatives([zero_key], [nonzero_representative])
                    if pair is not None:
                        select_pair(
                            *pair,
                            "reported_magnitude_contrast",
                            cohort_id,
                            magnitude_label=magnitude_label,
                        )
                        select_pair(*pair, "reported_zero_vs_nonzero", cohort_id)
                        magnitude_pair_found = True
        zero_effect_keys = buckets.get("zero_effect", [])
        if support_keys and zero_effect_keys and not magnitude_pair_found:
            anchor = representative(support_keys)
            for key in pattern_representatives(zero_effect_keys):
                select_pair(key, anchor, "zero_effect_contrast", cohort_id)

        # Explicit qualifier members remain visible even when their finding
        # polarity resembles the majority. Generic label membership alone does
        # not create a comparison task.
        qualifier_keys = [key for key in keys if _has_explicit_qualifier(cohort_views[key])]
        comparator_keys = [key for key in keys if key not in set(qualifier_keys)]
        if qualifier_keys and comparator_keys:
            anchor = representative(comparator_keys)
            for key in pattern_representatives(qualifier_keys):
                select_pair(key, anchor, "explicit_qualifier", cohort_id)

        # Select one representative per non-modal context/method stratum so a
        # rare boundary or design remains available regardless of paper ID.
        for difference_dimension in ("context", "method"):
            strata: Dict[Tuple[str, ...], List[str]] = defaultdict(list)
            for key in keys:
                values = tuple(sorted(
                    value.casefold()
                    for value in _view_field_values(cohort_views[key], difference_dimension)
                    if value
                ))
                if values:
                    strata[values].append(key)
            if len(strata) <= 1:
                continue
            ordered_strata = sorted(
                strata,
                key=lambda signature: (
                    -len(strata[signature]),
                    signature,
                    min(semantic_fingerprints[key] for key in strata[signature]),
                ),
            )
            anchor_key = representative(strata[ordered_strata[0]])
            for signature in ordered_strata[1:]:
                select_pair(
                    anchor_key,
                    representative(strata[signature]),
                    f"{difference_dimension}_boundary",
                    cohort_id,
                )

        # A shared substantive axis with distinct remaining topic signatures
        # creates one bridge task per non-modal signature, rather than merging
        # every connected paper into one transitive comparison.
        topic_strata: Dict[Tuple[Tuple[str, Tuple[str, ...]], ...], List[str]] = defaultdict(list)
        for key in keys:
            signature = tuple(
                (
                    other_dimension,
                    tuple(sorted(
                        value.casefold()
                        for value in dimensions_by_key.get(key, {}).get(other_dimension, [])
                    )),
                )
                for other_dimension in ("theory", "construct", "mechanism")
                if other_dimension != dimension
                and dimensions_by_key.get(key, {}).get(other_dimension)
            )
            if signature:
                topic_strata[signature].append(key)
        if len(topic_strata) > 1:
            ordered_signatures = sorted(
                topic_strata,
                key=lambda signature: (
                    -len(topic_strata[signature]),
                    signature,
                    min(semantic_fingerprints[key] for key in topic_strata[signature]),
                ),
            )
            anchor_key = representative(topic_strata[ordered_signatures[0]])
            for signature in ordered_signatures[1:]:
                select_pair(
                    anchor_key,
                    representative(topic_strata[signature]),
                    "cross_topic_bridge",
                    cohort_id,
                )

    # Materialize only selected task pairs. The full cohort records retain
    # membership and the unenumerated exploratory pair space.
    pair_dimensions: Dict[Tuple[str, str], Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
    for left_key, right_key in selected_pair_reasons:
        for dimension in MATRIX_DIMENSIONS:
            shared_labels = (
                set(dimensions_by_key.get(left_key, {}).get(dimension, ()))
                & set(dimensions_by_key.get(right_key, {}).get(dimension, ()))
            )
            if shared_labels:
                pair_dimensions[(left_key, right_key)][dimension].update(shared_labels)

    relations: Dict[str, RelationCandidate] = {}
    relation_cohorts: Dict[str, Set[str]] = defaultdict(set)
    for (left_key, right_key), dimension_map in sorted(pair_dimensions.items()):
        left = views_by_key.get(left_key)
        right = views_by_key.get(right_key)
        if left is None or right is None:
            continue
        shared_dimensions = sorted(dimension_map)
        reason_codes = sorted(selected_pair_reasons[(left_key, right_key)])
        pair_cohort_ids = selected_pair_cohorts[(left_key, right_key)]

        def add_relation(relation: RelationCandidate) -> None:
            _add_relation(relations, relation)
            relation_cohorts[relation.relation_id].update(pair_cohort_ids)

        for dimension in shared_dimensions:
            relation_type = _RELATION_DIMENSION_TYPES.get(dimension)
            if relation_type:
                add_relation(_relation(
                    relation_type,
                    left,
                    right,
                    dimension=dimension,
                    labels=sorted(dimension_map[dimension]),
                    confidence="medium",
                    diagnostics=reason_codes,
                ))

        left_context = set(_view_field_values(left, "context"))
        right_context = set(_view_field_values(right, "context"))
        if left_context and right_context and left_context != right_context:
            add_relation(_relation(
                "different_context", left, right, dimension="context",
                labels=sorted(left_context | right_context),
                confidence="medium",
                diagnostics=reason_codes,
            ))

        left_method = set(_view_field_values(left, "method"))
        right_method = set(_view_field_values(right, "method"))
        if left_method and right_method and left_method != right_method:
            add_relation(_relation(
                "different_method", left, right, dimension="method",
                labels=sorted(left_method | right_method),
                confidence="medium",
                diagnostics=reason_codes,
            ))

        # A bridge candidate requires overlap plus non-identical topical
        # coverage.  It is a planning signal, not a claim that the papers are
        # conceptually integrated.
        left_labels = {label for labels in (_view_field_values(left, dimension) for dimension in MATRIX_DIMENSIONS) for label in labels}
        right_labels = {label for labels in (_view_field_values(right, dimension) for dimension in MATRIX_DIMENSIONS) for label in labels}
        if len(shared_dimensions) >= 2 or (left_labels & right_labels and left_labels != right_labels):
            add_relation(_relation(
                "bridge_between_topics", left, right, dimension=shared_dimensions[0],
                labels=sorted(left_labels & right_labels), confidence="low",
                diagnostics=reason_codes,
            ))

        left_year = left.year
        right_year = right.year
        substantive_overlap = any(dimension in shared_dimensions for dimension in ("theory", "construct", "mechanism", "finding"))
        if substantive_overlap and left_year is not None and right_year is not None and left_year != right_year:
            older, newer = (left, right) if left_year < right_year else (right, left)
            add_relation(_relation(
                "historical_predecessor", older, newer, dimension="year",
                labels=[str(older.year), str(newer.year)], confidence="low",
                diagnostics=reason_codes,
            ))

        if (left.paper_type == "conceptual" or right.paper_type == "conceptual") and any(
            dimension in shared_dimensions for dimension in ("theory", "construct", "mechanism")
        ):
            add_relation(_relation(
                "conceptual_integration", left, right,
                dimension=next(d for d in shared_dimensions if d in {"theory", "construct", "mechanism"}),
                labels=sorted(left_labels & right_labels), confidence="low",
                diagnostics=reason_codes,
            ))

        left_finding_text = " ".join(left.findings).casefold()
        right_finding_text = " ".join(right.findings).casefold()
        left_support = any(pattern in left_finding_text for pattern in _TEXT_SIGNAL_PATTERNS["support"])
        right_support = any(pattern in right_finding_text for pattern in _TEXT_SIGNAL_PATTERNS["support"])
        left_contradict = any(pattern in left_finding_text for pattern in _TEXT_SIGNAL_PATTERNS["contradict"])
        right_contradict = any(pattern in right_finding_text for pattern in _TEXT_SIGNAL_PATTERNS["contradict"])
        # Positive/negative words are only a retrieval signal.  A substantive
        # relation requires a shared theory, construct, mechanism, or finding
        # dimension; year, author, template labels, and context alone cannot
        # establish support or contradiction.
        relation_dimension = next(
            (d for d in shared_dimensions if d in {"theory", "construct", "mechanism", "finding"}),
            "",
        )
        shared_labels = sorted(dimension_map[relation_dimension]) if relation_dimension else []
        evidence_fields = {
            left.paper_key: ["findings", "conclusions"],
            right.paper_key: ["findings", "conclusions"],
        }
        if relation_dimension and (left_support and right_support) and not (left_contradict or right_contradict):
            add_relation(_relation(
                "supports", left, right, dimension=relation_dimension,
                labels=shared_labels, confidence="low", evidence_fields=evidence_fields,
                diagnostics=reason_codes,
            ))
        if relation_dimension and (left_contradict or right_contradict):
            add_relation(_relation(
                "contradicts", left, right, dimension=relation_dimension,
                labels=shared_labels, confidence="low", evidence_fields=evidence_fields,
                diagnostics=reason_codes,
            ))
            if (left_context and right_context and left_context != right_context) or (left_method and right_method and left_method != right_method):
                add_relation(_relation(
                    "explains_discrepancy", left, right, dimension=relation_dimension,
                    labels=shared_labels, confidence="low", evidence_fields=evidence_fields,
                    diagnostics=reason_codes,
                ))
        if _has_explicit_qualifier(left) or _has_explicit_qualifier(right):
            add_relation(_relation(
                "qualifies", left, right, dimension=relation_dimension,
                labels=shared_labels, confidence="low", evidence_fields=evidence_fields,
                diagnostics=reason_codes,
            ))
        if any(pattern in left_finding_text for pattern in _TEXT_SIGNAL_PATTERNS["replicate"]) or any(
            pattern in right_finding_text for pattern in _TEXT_SIGNAL_PATTERNS["replicate"]
        ):
            add_relation(_relation(
                "replicates", left, right, dimension=relation_dimension,
                labels=shared_labels, confidence="low", evidence_fields=evidence_fields,
                diagnostics=reason_codes,
            ))
        if any(pattern in left_finding_text for pattern in _TEXT_SIGNAL_PATTERNS["extend"]) or any(
            pattern in right_finding_text for pattern in _TEXT_SIGNAL_PATTERNS["extend"]
        ):
            add_relation(_relation(
                "extends", left, right, dimension=relation_dimension,
                labels=shared_labels, confidence="low", evidence_fields=evidence_fields,
                diagnostics=reason_codes,
            ))
        if "reported_magnitude_contrast" in reason_codes:
            magnitude_dimension = next(
                (dimension for dimension in shared_dimensions if dimension in _SUBSTANTIVE_DIMENSIONS),
                relation_dimension,
            )
            magnitude_labels = sorted(selected_pair_magnitude_labels[(left_key, right_key)])
            if magnitude_dimension and magnitude_labels:
                magnitude_evidence_fields = {
                    left.paper_key: ["findings"],
                    right.paper_key: ["findings"],
                }
                add_relation(_relation(
                    "reported_magnitude_contrast",
                    left,
                    right,
                    dimension=magnitude_dimension,
                    labels=magnitude_labels,
                    confidence="medium",
                    evidence_fields=magnitude_evidence_fields,
                    diagnostics=reason_codes,
                ))

    required_comparison_rows: List[Dict[str, Any]] = []
    required_relation_ids: List[str] = []
    used_required_relation_ids: Set[str] = set()
    for required in normalized_required_comparisons:
        paper_keys = list(required["paper_keys"])
        pair = (paper_keys[0], paper_keys[1])
        left, right = (views_by_key[paper_keys[0]], views_by_key[paper_keys[1]])
        task_relation_type = str(required["task_relation_type"])
        source_fields_by_paper = {
            key: _required_comparison_source_fields(views_by_key[key])
            for key in paper_keys
        }
        flattened_source_fields = {
            key: _stable_unique(
                path
                for paths in source_fields_by_paper[key].values()
                for path in paths
            )
            for key in paper_keys
        }
        required_field_names = {
            key: ["findings", "moderators_boundaries"]
            for key in paper_keys
        }
        missing_source_fields_by_paper = {
            key: [
                field_name
                for field_name in required_field_names[key]
                if not source_fields_by_paper[key].get(field_name)
                or (
                    field_name == "findings" and not views_by_key[key].findings
                )
                or (
                    field_name == "moderators_boundaries"
                    and not (views_by_key[key].limitations or views_by_key[key].sample_or_context)
                )
            ]
            for key in paper_keys
        }
        pair_cohort_ids = sorted(
            cohort_id
            for cohort_id, cohort_members in cohort_keys.items()
            if set(paper_keys).issubset(cohort_members)
        )
        selected_pair_reasons[pair].add("required_user_comparison")
        selected_pair_cohorts[pair].update(pair_cohort_ids)
        for cohort_id in pair_cohort_ids:
            cohort_selected_reasons[cohort_id].add("required_user_comparison")

        # Reuse one existing same-pair/same-task relation when possible. A
        # separate requirement identity is created for distinct user questions
        # over that same pair/type so no question is silently collapsed.
        existing = next((
            relation for relation in sorted(relations.values(), key=lambda item: item.relation_id)
            if relation.relation_id not in used_required_relation_ids
            and relation.relation_type == task_relation_type
            and set(relation.paper_keys) == set(paper_keys)
        ), None)
        if existing is not None:
            relation_id = existing.relation_id
            merged_evidence_fields = {
                key: _stable_unique([
                    *existing.evidence_fields.get(key, ()),
                    *required_field_names[key],
                ])
                for key in paper_keys
            }
            merged_source_fields = {
                key: _stable_unique([
                    *existing.source_fields.get(key, ()),
                    *flattened_source_fields[key],
                ])
                for key in paper_keys
            }
            relations[relation_id] = RelationCandidate(
                relation_id=relation_id,
                relation_type=existing.relation_type,
                paper_keys=list(existing.paper_keys),
                dimension=existing.dimension,
                evidence_fields=merged_evidence_fields,
                confidence=existing.confidence,
                source_fields=merged_source_fields,
                supporting_labels=list(existing.supporting_labels),
                source_paper_key=existing.source_paper_key or paper_keys[0],
                target_paper_key=existing.target_paper_key or paper_keys[1],
                diagnostics=_stable_unique([
                    *existing.diagnostics,
                    "required_user_comparison",
                ]),
            )
        else:
            relation_id = "relation-" + compute_v3_hash({
                "required_comparison_id": required["requirement_id"],
                "source_fields_by_paper": flattened_source_fields,
                "required_field_names_by_paper": required_field_names,
            })[:24]
            shared_finding_labels = sorted(
                set(dimensions_by_key.get(paper_keys[0], {}).get("finding", ()))
                & set(dimensions_by_key.get(paper_keys[1], {}).get("finding", ()))
            )
            relation_dimension = "finding"
            relations[relation_id] = RelationCandidate(
                relation_id=relation_id,
                relation_type=task_relation_type,
                paper_keys=paper_keys,
                source_paper_key=paper_keys[0],
                target_paper_key=paper_keys[1],
                dimension=relation_dimension,
                evidence_fields=required_field_names,
                confidence="low",
                source_fields=flattened_source_fields,
                supporting_labels=shared_finding_labels,
                diagnostics=["required_user_comparison"],
            )
        used_required_relation_ids.add(relation_id)
        relation_cohorts[relation_id].update(pair_cohort_ids)
        required_relation_ids.append(relation_id)
        missing_fields = {
            key: list(values)
            for key, values in missing_source_fields_by_paper.items()
            if values
        }
        required_comparison_rows.append({
            "requirement_id": required["requirement_id"],
            "relation_id": relation_id,
            "paper_keys": paper_keys,
            "question": required["question"],
            "requested_relation_type": required["requested_relation_type"],
            "task_relation_type": task_relation_type,
            "required_field_names_by_paper": required_field_names,
            "source_fields_by_paper": source_fields_by_paper,
            "missing_source_fields_by_paper": missing_fields,
            "cohort_ids": pair_cohort_ids,
            "status": "selected_source_incomplete" if missing_fields else "selected_source_complete",
            "scientific_result_status": "pending_adjudication",
            "relation_id_is_not_a_conclusion": True,
        })

    selected_comparisons = [
        {
            "relation_id": relation.relation_id,
            "relation_type": relation.relation_type,
            "paper_keys": list(relation.paper_keys),
            "reason_codes": sorted(selected_pair_reasons[pair]),
            "cohort_ids": sorted(relation_cohorts.get(relation.relation_id, ())),
        }
        for relation in sorted(relations.values(), key=lambda item: item.relation_id)
        for pair in [_candidate_pair_key(*relation.paper_keys[:2])]
    ]
    selected_relation_ids = {item["relation_id"] for item in selected_comparisons}
    for cohort in cohorts:
        cohort_id = str(cohort["cohort_id"])
        cohort_relation_ids = sorted({
            str(item["relation_id"])
            for item in selected_comparisons
            if cohort_id in item["cohort_ids"]
        })
        if cohort_relation_ids:
            cohort["disposition"] = "selected"
            cohort["reason_codes"] = sorted(cohort_selected_reasons.get(cohort_id, ()))
            cohort["selected_relation_ids"] = cohort_relation_ids
            cohort["reason"] = (
                "Source-recorded contrasts were selected as finite relation tasks; "
                "the remaining cohort pair space stays available for directed retrieval."
            )

    deferred_cohort_ids = sorted(
        str(item["cohort_id"]) for item in cohorts
        if item["disposition"] == "deferred"
    )
    comparison_coverage = {
        "schema_version": "relation-comparison-coverage/v1",
        "policy": "purposeful_sparse_cohorts_v1",
        "cohort_count": len(cohorts),
        "cohort_member_references": sum(int(item["member_count"]) for item in cohorts),
        "cohort_pair_memberships_available": sum(int(item["possible_pair_count"]) for item in cohorts),
        "selected_pair_count": len(selected_pair_reasons),
        "selected_relation_count": len(selected_relation_ids),
        "selected_relation_ids": sorted(selected_relation_ids),
        "selected_comparisons": selected_comparisons,
        "deferred_cohort_ids": deferred_cohort_ids,
        "not_applicable": {
            "singleton_label_group_count": singleton_label_group_count,
            "reason": "A single-member source group has no cross-paper comparison task.",
        },
        "all_pairwise_comparisons_completed": False,
        "pairwise_exploration_status": "deferred_outside_selected_tasks",
        "pairwise_exploration_reason": (
            "Complete cohort membership is preserved; shared labels alone do not require every pair to be compared."
        ),
    }
    if required_comparison_rows:
        comparison_coverage["required_comparisons"] = required_comparison_rows
        comparison_coverage["required_relation_ids"] = sorted(set(required_relation_ids))

    return GlobalRelationMap(
        relations=[relations[key] for key in sorted(relations)],
        paper_keys=sorted(views_by_key),
        source_artifact_hashes=source_hashes,
        blocking_diagnostics=blocking,
        cohorts=cohorts,
        comparison_coverage=comparison_coverage,
    )


def _preferred_axis_order(intent: Optional[ReviewIntent]) -> List[str]:
    axis_ids = [spec[0] for spec in _AXIS_SPECS]
    preferred = _safe_text(intent.preferred_organizing_logic if intent else "").casefold().replace("-", "_").replace(" ", "_")
    if preferred not in axis_ids:
        return axis_ids
    return [preferred, *[axis_id for axis_id in axis_ids if axis_id != preferred]]


def build_organizing_axes(
    intent: Optional[ReviewIntent] = None, *, candidate_count: int = 5,
) -> List[OrganizingAxis]:
    """Keep the fixed axes first, then compose distinct hierarchical axes."""

    if not 1 <= candidate_count <= 12:
        raise ValueError("candidate_count must be between 1 and 12")

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
    base_axes = list(axes)
    for primary in base_axes:
        for secondary in base_axes:
            if len(axes) >= candidate_count:
                return axes
            if primary.axis_id == secondary.axis_id:
                continue
            axis_id = f"{primary.axis_id}_then_{secondary.axis_id}"
            axes.append(OrganizingAxis(
                axis_id=axis_id,
                organizing_logic=axis_id,
                label=f"{primary.label} / {secondary.label}",
                rationale=(
                    f"Use {primary.axis_id} as the primary organization of sections; "
                    f"use {secondary.axis_id} to organize comparisons within those sections. "
                    f"Primary rationale: {primary.rationale} "
                    f"Secondary rationale: {secondary.rationale}"
                ),
                preferred_dimensions=list(dict.fromkeys([
                    *primary.preferred_dimensions, *secondary.preferred_dimensions,
                ])),
                preferred_relation_types=list(dict.fromkeys([
                    *primary.preferred_relation_types, *secondary.preferred_relation_types,
                ])),
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
    semantic_chunk_plan_hash: str = "",
) -> OutlineCandidatePlans:
    """Create deterministic candidate plans with provider generation isolated."""

    axes = build_organizing_axes(intent, candidate_count=candidate_count)
    selected_axes = axes[:candidate_count]
    shared = {
        "global_corpus_ledger": ledger.content_hash,
        "multi_view_matrix": matrix.content_hash,
        "global_relation_map": relation_map.content_hash,
        "review_intent": intent.content_hash,
        "coverage_contract": coverage_contract.content_hash,
    }
    if str(semantic_chunk_plan_hash or "").strip():
        shared["semantic_chunk_plan"] = str(semantic_chunk_plan_hash).strip()
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
                *(["semantic_chunk_plan"] if str(semantic_chunk_plan_hash or "").strip() else []),
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

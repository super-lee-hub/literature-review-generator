"""Shared quality rules for Outline Intelligence v2."""

from __future__ import annotations

import re
from typing import Any, Sequence


_PLACEHOLDER_PATTERNS = [
    r"^\s*$",
    r"^section\s*\d+\s*$",
    r"^unnamed[_\s-]*stream$",
    r"^research problem framing\b",
    r"^identified gaps\s*$",
    r"^synthesis of research stream\b",
    r"^synthesis of methodological approaches\b",
    r"^no papers available\b",
    r"^diagnostic\b",
    r"^placeholder\b",
    r"^untitled\b",
    r"^overview$",
    r"^introduction$",
    r"^background$",
    r"^discussion$",
    r"^trust$",
    r"^survey$",
    r"^finding$",
    r"^shared theme$",
    r"^second shared theme$",
]

_GENERIC_TITLES = {
    "gap",
    "gaps",
    "theme",
    "themes",
    "method",
    "methods",
    "findings",
    "literature",
    "review",
    "value",
    "trust",
    "anger",
    "positive",
    "negative",
}

_NOISE_SINGLETONS = {
    "effect",
    "effects",
    "result",
    "results",
    "finding",
    "findings",
    "model",
    "models",
    "high",
    "low",
    "medium",
}

_METHOD_ONLY_TERMS = {
    "analysis",
    "analyses",
    "experiment",
    "experiments",
    "method",
    "methods",
    "survey",
    "surveys",
}

_CORE_STREAM_TERMS = {
    "price fairness",
    "perceived price fairness",
    "price fairness perceptions",
    "perceived fairness",
    "价格公平感知",
}

_MECHANISM_STREAM_TERMS = {
    "equity theory adams",
    "dual entitlement principle kahneman et al",
    "归因理论weiner",
}

_OUTCOME_STREAM_TERMS = {
    "purchase intention",
    "购买意愿",
    "perceived value",
    "trust",
    "negative emotions",
    "disappointment",
    "complaint",
}

_REQUIRED_FLOW_ROLES = {
    "establish_problem_space",
    "synthesize_stream",
    "connect_mechanism",
    "compare_contexts",
    "identify_gaps",
}

_OPTIONAL_FLOW_ROLES = {"methodological_synthesis"}
_NON_BLOCKING_FLOW_ROLES = {"supporting_context", "diagnostic"}
_FORBIDDEN_PROVIDER_FLOW_ROLES = {"diagnostic", "supporting_context", "placeholder_flow"}


def normalize_label(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text


def is_placeholder_title(value: Any) -> bool:
    """Return True for generic diagnostic/placeholder section labels."""
    text = normalize_label(value)
    lowered = text.casefold()
    if any(re.search(pattern, lowered) for pattern in _PLACEHOLDER_PATTERNS):
        return True
    compact = re.sub(r"[^\w\s]", "", lowered).strip()
    if compact in _GENERIC_TITLES:
        return True
    has_non_ascii = any(ord(ch) > 127 for ch in compact)
    # Very short ASCII labels such as "gap" or "trust" are usually diagnostics.
    # Non-Latin labels can be meaningful at fewer code points, so only block
    # single-character non-ASCII labels as too-short placeholders.
    if not has_non_ascii and len(compact) < 8 and compact not in {"ai ethics", "pricing"}:
        return True
    if has_non_ascii and len(compact) < 2:
        return True
    return False


def is_generic_scholarship_title(value: Any) -> bool:
    text = normalize_label(value).casefold().strip(" .:")
    if not text.endswith(" scholarship"):
        return False
    stem = text[: -len(" scholarship")].strip()
    return bool(stem) and (
        stem in _GENERIC_TITLES
        or stem in _NOISE_SINGLETONS
    )


def is_noise_stream_label(value: Any) -> bool:
    """Return True for isolated statistical/metadata tokens, not multi-word topics."""
    text = normalize_label(value)
    lowered = text.casefold().strip(" .:;,_-()[]{}")
    compact = re.sub(r"[^\w\s]", "", lowered).strip()
    if not compact:
        return True
    if re.fullmatch(r"(19|20)\d{2}", compact):
        return True
    if re.fullmatch(r"p\s*[<=>]?\s*0?\.\d+", lowered):
        return True
    if re.fullmatch(r"p\d{2,}", compact):
        return True
    if re.fullmatch(r"\d+(\.\d+)?", compact):
        return True
    if len(compact) < 3 and compact not in {"ai", "ml"}:
        return True
    words = compact.split()
    if len(words) == 1 and compact in (_NOISE_SINGLETONS | _METHOD_ONLY_TERMS):
        return True
    return False


def is_method_only_stream_label(value: Any) -> bool:
    """Return True for isolated method metadata labels that should not force a chapter."""
    compact = re.sub(r"[^\w\s]", "", normalize_label(value).casefold()).strip()
    return len(compact.split()) == 1 and compact in _METHOD_ONLY_TERMS


def is_long_method_title(value: Any) -> bool:
    text = normalize_label(value)
    lowered = text.casefold()
    return (
        lowered.startswith("methodological patterns across ")
        and (len(text) > 90 or any(ord(ch) > 127 for ch in text))
    )


def is_sentence_like_stream_label(value: Any) -> bool:
    """Return True for long extracted sentences that should stay evidence, not chapters."""
    text = normalize_label(value)
    compact = re.sub(r"[^\w\s]", "", text.casefold()).strip()
    if len(compact) > 90:
        return True
    has_non_ascii = any(ord(ch) > 127 for ch in compact)
    if has_non_ascii and len(compact) > 28:
        return True
    words = compact.split()
    return len(words) > 7


def is_low_quality_title(value: Any) -> bool:
    return (
        is_placeholder_title(value)
        or is_generic_scholarship_title(value)
        or is_long_method_title(value)
    )


def stream_promotion_tier(stream_name: Any, source_fields: Sequence[Any] | None = None, paper_count: int = 0) -> int:
    """Rank whether a literature-map stream should become a main synthesis step.

    Stage 1 can contain long findings or translated sentences in theme/theory
    fields. Those are useful evidence, but forcing every repeated sentence to
    become a required outline flow step makes the quality gate noisy.
    """
    name = normalize_label(stream_name).casefold()
    fields = {str(field) for field in (source_fields or [])}
    if paper_count < 2 or is_noise_stream_label(name) or is_method_only_stream_label(name):
        return 0
    if is_sentence_like_stream_label(name):
        return 0
    if name in _CORE_STREAM_TERMS:
        return 4
    if name in _MECHANISM_STREAM_TERMS:
        return 3
    if name in _OUTCOME_STREAM_TERMS:
        return 2
    if paper_count >= 3 and fields.intersection({"themes", "theories", "variables"}):
        return 1
    return 0


def required_flow_roles() -> set[str]:
    return set(_REQUIRED_FLOW_ROLES)


def optional_flow_roles() -> set[str]:
    return set(_OPTIONAL_FLOW_ROLES)


def non_blocking_flow_roles() -> set[str]:
    return set(_NON_BLOCKING_FLOW_ROLES)


def allowed_provider_flow_roles() -> set[str]:
    return set(_REQUIRED_FLOW_ROLES | _OPTIONAL_FLOW_ROLES)


def forbidden_provider_flow_roles() -> set[str]:
    return set(_FORBIDDEN_PROVIDER_FLOW_ROLES)


def is_required_flow_role(role: Any) -> bool:
    return str(role or "") in _REQUIRED_FLOW_ROLES


def is_required_capable_flow_role(role: Any) -> bool:
    return str(role or "") in (_REQUIRED_FLOW_ROLES | _OPTIONAL_FLOW_ROLES)


def synthesis_title_for_stream(stream_name: str) -> str:
    label = normalize_label(stream_name).strip(" .:")
    if not label or is_placeholder_title(label) or is_noise_stream_label(label):
        return ""
    if len(label) > 90:
        return ""
    lowered = label.casefold()
    if lowered in _GENERIC_TITLES or lowered in _NOISE_SINGLETONS:
        return ""
    return f"Synthesis of {label[:1].upper()}{label[1:]}"

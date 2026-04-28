from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional


PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_ASCII_RE = re.compile(r"[A-Za-z]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WORD_RE = re.compile(r"[A-Za-z]{3,}|\d+(?:\.\d+)?|[\u4e00-\u9fff]")
_TEXTISH_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]")


@dataclass(frozen=True)
class TextQualityResult:
    decision: str
    reasons: List[str]
    metrics: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def score_text_quality(
    text: str,
    reference_text: Optional[str] = None,
    expected_language: Optional[str] = None,
    title: Optional[str] = None,
) -> TextQualityResult:
    """Score whether a candidate text is safe to use as stage-one model input."""

    candidate = str(text or "")
    reference = str(reference_text or "")
    language = _normalize_language(expected_language) or _infer_language(reference or candidate)
    metrics = _basic_metrics(candidate)
    reference_metrics = _basic_metrics(reference) if reference else {}
    if reference:
        metrics["reference_cjk_ratio"] = reference_metrics.get("cjk_ratio", 0.0)
        metrics["reference_ascii_ratio"] = reference_metrics.get("ascii_ratio", 0.0)
        metrics["length_ratio_to_reference"] = _safe_ratio(
            metrics["nonspace_length"],
            int(reference_metrics.get("nonspace_length", 0)),
        )
        metrics["token_overlap"] = _token_overlap(reference, candidate)
        metrics["char_ngram_overlap"] = _char_ngram_overlap(reference, candidate)
    else:
        metrics["reference_cjk_ratio"] = 0.0
        metrics["reference_ascii_ratio"] = 0.0
        metrics["length_ratio_to_reference"] = None
        metrics["token_overlap"] = None
        metrics["char_ngram_overlap"] = None
    metrics["expected_language"] = language or ""
    metrics["title_overlap"] = _title_overlap(title, candidate) if title else None

    reasons: List[str] = []
    if metrics["nonspace_length"] < 80:
        reasons.append("too_short")
    if metrics["control_char_rate"] > 0.01:
        reasons.append("control_noise")
    if metrics["symbol_noise_rate"] > 0.42:
        reasons.append("symbol_noise")
    if metrics["line_gibberish_rate"] > 0.35:
        reasons.append("line_gibberish")
    if metrics["repetition_score"] > 0.28:
        reasons.append("repetition")

    reference_cjk_ratio = float(metrics.get("reference_cjk_ratio") or 0.0)
    candidate_cjk_ratio = float(metrics.get("cjk_ratio") or 0.0)
    candidate_ascii_ratio = float(metrics.get("ascii_ratio") or 0.0)
    reference_says_chinese = language == "zh" or reference_cjk_ratio >= 0.30
    if reference and reference_says_chinese and reference_cjk_ratio >= 0.30:
        if candidate_cjk_ratio < reference_cjk_ratio * 0.55:
            reasons.append("cjk_collapse")
        if candidate_ascii_ratio > 0.35 and candidate_cjk_ratio < reference_cjk_ratio * 0.55:
            reasons.append("suspected_garbled_markdown")

    overlap = metrics.get("char_ngram_overlap")
    if (
        reference
        and isinstance(overlap, float)
        and overlap < 0.55
        and metrics["nonspace_length"] >= 80
        and int(reference_metrics.get("nonspace_length", 0)) >= 80
    ):
        reasons.append("low_overlap")

    fail_reasons = {
        "too_short",
        "control_noise",
        "cjk_collapse",
        "suspected_garbled_markdown",
        "low_overlap",
    }
    if any(reason in fail_reasons for reason in reasons):
        decision = FAIL
    elif reasons:
        decision = WARN
    else:
        decision = PASS
    return TextQualityResult(decision=decision, reasons=sorted(set(reasons)), metrics=metrics)


def _normalize_language(value: Optional[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"zh", "cn", "chinese", "\u4e2d\u6587", "chi", "zho"}:
        return "zh"
    if normalized in {"en", "eng", "english"}:
        return "en"
    return ""


def _infer_language(text: str) -> str:
    metrics = _basic_metrics(text)
    if metrics["cjk_ratio"] >= 0.30:
        return "zh"
    if metrics["ascii_ratio"] >= 0.30:
        return "en"
    return ""


def _basic_metrics(text: str) -> Dict[str, Any]:
    total = max(len(text), 1)
    nonspace = [char for char in text if not char.isspace()]
    nonspace_total = max(len(nonspace), 1)
    cjk = len(_CJK_RE.findall(text))
    ascii_letters = len(_ASCII_RE.findall(text))
    controls = len(_CONTROL_RE.findall(text))
    textish = len(_TEXTISH_RE.findall(text))
    symbol_noise = sum(1 for char in nonspace if not _is_expected_text_char(char))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    gibberish_lines = sum(1 for line in lines if _line_gibberish(line))
    return {
        "length": len(text),
        "nonspace_length": len(nonspace),
        "cjk_ratio": cjk / nonspace_total,
        "ascii_ratio": ascii_letters / nonspace_total,
        "control_char_rate": controls / total,
        "symbol_noise_rate": symbol_noise / nonspace_total,
        "textish_ratio": textish / nonspace_total,
        "line_count": len(lines),
        "line_gibberish_rate": gibberish_lines / max(len(lines), 1),
        "repetition_score": _repetition_score(text),
        "whitespace_fragmentation": _whitespace_fragmentation(text),
    }


def _is_expected_text_char(char: str) -> bool:
    if char.isalnum() or _CJK_RE.match(char):
        return True
    if char in ".,;:!?()[]{}<>/\\|+-_=*#%&$@'\"`~^":
        return True
    if char in "\uff0c\u3002\uff1b\uff1a\uff01\uff1f\uff08\uff09\u3010\u3011\u300a\u300b\u3001\u00b7\u2014\u2013":
        return True
    return False


def _line_gibberish(line: str) -> bool:
    stripped = "".join(char for char in line if not char.isspace())
    if len(stripped) < 20:
        return False
    textish = len(_TEXTISH_RE.findall(stripped))
    uppercase = sum(1 for char in stripped if "A" <= char <= "Z")
    cjk = len(_CJK_RE.findall(stripped))
    vowels = sum(1 for char in stripped.lower() if char in "aeiou")
    letters = len(_ASCII_RE.findall(stripped))
    if textish / max(len(stripped), 1) < 0.45:
        return True
    if letters >= 40 and uppercase / max(letters, 1) > 0.75 and vowels / max(letters, 1) > 0.45 and cjk == 0:
        return True
    return False


def _repetition_score(text: str) -> float:
    tokens = _WORD_RE.findall(text.lower())
    if len(tokens) < 20:
        return 0.0
    counts: Dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    return max(counts.values()) / max(len(tokens), 1)


def _whitespace_fragmentation(text: str) -> float:
    if not text:
        return 0.0
    separated_single_chars = len(re.findall(r"(?<!\S)\S(?!\S)", text))
    return separated_single_chars / max(len(_WORD_RE.findall(text)), 1)


def _token_overlap(reference: str, candidate: str) -> Optional[float]:
    ref_tokens = set(_WORD_RE.findall(reference.lower()))
    cand_tokens = set(_WORD_RE.findall(candidate.lower()))
    if not ref_tokens or not cand_tokens:
        return None
    return len(ref_tokens & cand_tokens) / len(ref_tokens)


def _char_ngram_overlap(reference: str, candidate: str, n: int = 5) -> Optional[float]:
    ref = _normalized_chars(reference)
    cand = _normalized_chars(candidate)
    if len(ref) < n or len(cand) < n:
        return None
    ref_grams = {ref[index : index + n] for index in range(len(ref) - n + 1)}
    cand_grams = {cand[index : index + n] for index in range(len(cand) - n + 1)}
    if not ref_grams:
        return None
    return len(ref_grams & cand_grams) / len(ref_grams)


def _normalized_chars(text: str) -> str:
    return "".join(char.lower() for char in text if char.isalnum() or _CJK_RE.match(char))


def _title_overlap(title: Optional[str], candidate: str) -> Optional[float]:
    title_tokens = set(_WORD_RE.findall(str(title or "").lower()))
    if not title_tokens:
        return None
    candidate_tokens = set(_WORD_RE.findall(candidate.lower()))
    if not candidate_tokens:
        return 0.0
    return len(title_tokens & candidate_tokens) / len(title_tokens)


def _safe_ratio(left: int, right: int) -> Optional[float]:
    if right <= 0:
        return None
    return left / right

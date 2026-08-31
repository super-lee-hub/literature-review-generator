"""Deterministic canonical PDF selection for Zotero parent attachments.

The selector is deliberately local and side-effect free.  It never mutates
Zotero; it only ranks parent-linked PDF candidates and records why every
candidate was selected, retained as auxiliary material, or rejected.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import math
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    import pymupdf as fitz  # type: ignore
except ImportError:  # pragma: no cover - compatibility with older PyMuPDF releases.
    import fitz  # type: ignore


CANONICAL_ATTACHMENT_POLICY_VERSION = "canonical-attachment-selector-v1"

PRIMARY_FULLTEXT = "PRIMARY_FULLTEXT"
SCANNED_PRIMARY = "SCANNED_PRIMARY"
TRANSLATION_DERIVATIVE = "TRANSLATION_DERIVATIVE"
SUPPLEMENT = "SUPPLEMENT"
WRONG_ATTACHMENT = "WRONG_ATTACHMENT"
DUPLICATE_IDENTICAL = "DUPLICATE_IDENTICAL"
UNKNOWN_VERSION = "UNKNOWN_VERSION"

VERSION_OF_RECORD = "VERSION_OF_RECORD"
ACCEPTED_MANUSCRIPT = "ACCEPTED_MANUSCRIPT"
POSTPRINT = "POSTPRINT"
PREPRINT = "PREPRINT"

_VERSION_RANK = {
    VERSION_OF_RECORD: 5,
    ACCEPTED_MANUSCRIPT: 4,
    POSTPRINT: 3,
    PREPRINT: 2,
    UNKNOWN_VERSION: 1,
}

_TRANSLATION_MARKERS = (
    re.compile(r"\bzh[-_ ]?cn\b", re.IGNORECASE),
    re.compile(r"\btb[-_ ]?dual\b", re.IGNORECASE),
    re.compile(r"translation(?:_generated_by_ai)?", re.IGNORECASE),
    re.compile(r"\btranslated\b", re.IGNORECASE),
    re.compile(r"\bbilingual\b", re.IGNORECASE),
    re.compile(r"babeldoc", re.IGNORECASE),
    re.compile(r"中文翻译|翻译版|双语|译文"),
)
_SUPPLEMENT_PATH_MARKERS = (
    re.compile(r"\bweb[\s_-]+appendi(?:x|ces)\b", re.IGNORECASE),
    re.compile(r"\bonline[\s_-]+appendi(?:x|ces)\b", re.IGNORECASE),
    re.compile(r"\b(?:supplement(?:ary|al)?|supporting)[\s_-]+(?:appendi(?:x|ces)|information|materials?|data|file)\b", re.IGNORECASE),
    re.compile(r"\bappendi(?:x|ces)\b", re.IGNORECASE),
    re.compile(r"附录|补充材料|附加材料"),
)
_SUPPLEMENT_HEADER_MARKERS = (
    re.compile(r"\b(?:web|online)[\s_-]+appendi(?:x|ces)[\s_-]+for\b", re.IGNORECASE),
    re.compile(r"\b(?:supplementary|supplemental)[\s_-]+appendi(?:x|ces)\b", re.IGNORECASE),
    re.compile(r"(?<!view )(?<!see )\b(?:supplementary|supplemental)[\s_-]+materials?\b", re.IGNORECASE),
    re.compile(r"\bsupporting[\s_-]+information\b", re.IGNORECASE),
    re.compile(r"\bappendi(?:x|ces)[\s_-]+for\b", re.IGNORECASE),
    re.compile(r"附录|补充材料|附加材料"),
)
_VOR_MARKERS = (
    re.compile(r"version\s+of\s+record", re.IGNORECASE),
    re.compile(r"corrected\s+proof", re.IGNORECASE),
    re.compile(r"published\s+(?:version|by)", re.IGNORECASE),
    re.compile(r"final\s+published", re.IGNORECASE),
)
_ACCEPTED_MARKERS = (
    re.compile(r"author\s+accepted\s+manuscript", re.IGNORECASE),
    re.compile(r"accepted\s+manuscript", re.IGNORECASE),
    re.compile(r"accepted\s+version", re.IGNORECASE),
)
_POSTPRINT_MARKERS = (
    re.compile(r"post[- ]print", re.IGNORECASE),
    re.compile(r"author\s+manuscript", re.IGNORECASE),
)
_PREPRINT_MARKERS = (
    re.compile(r"\bpreprint\b", re.IGNORECASE),
    re.compile(r"working\s+paper", re.IGNORECASE),
    re.compile(r"submitted\s+manuscript", re.IGNORECASE),
    re.compile(r"this\s+draft", re.IGNORECASE),
    re.compile(r"ssrn\.com", re.IGNORECASE),
    re.compile(r"electronic\s+copy\s+available", re.IGNORECASE),
)


def _text(value: Any) -> str:
    return str(value or "")


def _contains_any(text: str, patterns: Sequence[re.Pattern[str]]) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _date_sort_key(value: Any) -> str:
    text = _text(value).strip()
    return text if text else "9999-99-99 99:99:99"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _attachment_sort_key(candidate: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        _date_sort_key(candidate.get("date_added")),
        _text(candidate.get("attachment_key")).casefold(),
        _text(candidate.get("path")).casefold(),
    )


def _nonblank_ratio(pixmap: Any) -> float:
    width = int(getattr(pixmap, "width", 0) or 0)
    height = int(getattr(pixmap, "height", 0) or 0)
    channel_count = max(int(getattr(pixmap, "n", 3) or 3), 3)
    samples = bytes(getattr(pixmap, "samples", b"") or b"")
    total = width * height
    if total <= 0 or not samples:
        return 0.0
    stride = channel_count
    dark = 0
    for offset in range(0, len(samples) - 2, stride):
        if min(samples[offset], samples[offset + 1], samples[offset + 2]) < 245:
            dark += 1
    return dark / total


def inspect_pdf_quality(path: str | Path) -> dict[str, Any]:
    """Inspect renderability and text/image characteristics without writing files."""

    target = Path(path).expanduser().resolve()
    payload: dict[str, Any] = {
        "path": str(target),
        "exists": target.is_file(),
        "file_size_bytes": target.stat().st_size if target.is_file() else 0,
        "page_count": 0,
        "text_chars": 0,
        "text_nonzero_pages": 0,
        "cjk_chars": 0,
        "latin_chars": 0,
        "image_pages": 0,
        "image_count": 0,
        "renderable_pages": 0,
        "nonblank_pages": 0,
        "render_failures": [],
        "max_page_pixels": 0.0,
        "min_nonblank_ratio": 0.0,
        "median_nonblank_ratio": 0.0,
        "render_complete": False,
        "scanned_like": False,
        "metadata": {},
        "first_page_excerpt": "",
        "error": "",
    }
    if not target.is_file() or target.suffix.casefold() != ".pdf":
        payload["error"] = "pdf_file_missing_or_invalid_suffix"
        return payload

    nonblank_ratios: list[float] = []
    try:
        with fitz.open(str(target)) as document:
            payload["page_count"] = int(document.page_count)
            payload["metadata"] = dict(document.metadata or {})
            for page_index in range(document.page_count):
                page = document.load_page(page_index)
                raw_text = str(page.get_text("text") or "")
                stripped_text = raw_text.strip()
                payload["text_chars"] += len(stripped_text)
                if stripped_text:
                    payload["text_nonzero_pages"] += 1
                payload["cjk_chars"] += len(re.findall(r"[\u3400-\u9fff]", raw_text))
                payload["latin_chars"] += len(re.findall(r"[A-Za-z]", raw_text))
                images = len(page.get_images(full=True))
                payload["image_count"] += images
                if images:
                    payload["image_pages"] += 1
                payload["max_page_pixels"] = max(
                    float(payload["max_page_pixels"]),
                    float(page.rect.width * page.rect.height),
                )
                if page_index == 0:
                    payload["first_page_excerpt"] = raw_text[:12000]
                try:
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(0.35, 0.35), alpha=False)
                    payload["renderable_pages"] += 1
                    ratio = _nonblank_ratio(pixmap)
                    nonblank_ratios.append(ratio)
                    if ratio >= 0.01:
                        payload["nonblank_pages"] += 1
                except Exception as exc:  # pragma: no cover - depends on malformed PDFs.
                    payload["render_failures"].append(
                        {"page_no": page_index + 1, "error": f"{type(exc).__name__}:{exc}"}
                    )
    except Exception as exc:
        payload["error"] = f"pdf_open_error:{type(exc).__name__}:{exc}"

    page_count = int(payload["page_count"] or 0)
    payload["render_complete"] = bool(
        page_count > 0
        and int(payload["renderable_pages"] or 0) == page_count
        and int(payload["nonblank_pages"] or 0)
        + int(payload["text_nonzero_pages"] or 0)
        >= page_count
        and not payload["render_failures"]
    )
    if nonblank_ratios:
        ordered = sorted(nonblank_ratios)
        payload["min_nonblank_ratio"] = min(ordered)
        middle = len(ordered) // 2
        payload["median_nonblank_ratio"] = (
            ordered[middle]
            if len(ordered) % 2
            else (ordered[middle - 1] + ordered[middle]) / 2
        )
    text_chars = int(payload["text_chars"] or 0)
    payload["scanned_like"] = bool(
        page_count > 0
        and int(payload["image_pages"] or 0) >= max(1, math.ceil(page_count * 0.5))
        and text_chars <= max(600, page_count * 120)
    )
    return payload


def _candidate_evidence_text(candidate: Mapping[str, Any], quality: Mapping[str, Any]) -> str:
    metadata = quality.get("metadata")
    metadata_mapping = metadata if isinstance(metadata, Mapping) else {}
    pieces = [
        _text(candidate.get("path")),
        _text(candidate.get("attachment_key")),
        _text(candidate.get("attachment_title")),
        _text(candidate.get("raw_path")),
        _text(metadata_mapping.get("title")),
        _text(metadata_mapping.get("subject")),
        _text(metadata_mapping.get("keywords")),
        _text(metadata_mapping.get("creator")),
        _text(metadata_mapping.get("producer")),
        _text(quality.get("first_page_excerpt")),
    ]
    return " ".join(item for item in pieces if item).casefold()


def _role_marker_evidence(candidate: Mapping[str, Any], quality: Mapping[str, Any]) -> str:
    """Return metadata/path plus the document header, excluding body text."""

    metadata = quality.get("metadata")
    metadata_mapping = metadata if isinstance(metadata, Mapping) else {}
    header = _text(quality.get("first_page_excerpt"))[:1200]
    return " ".join(
        item
        for item in (
            Path(_text(candidate.get("path"))).name,
            _text(candidate.get("attachment_title")),
            _text(candidate.get("raw_path")),
            _text(metadata_mapping.get("title")),
            _text(metadata_mapping.get("subject")),
            _text(metadata_mapping.get("producer")),
            header,
        )
        if item
    ).casefold()


def _has_supplement_marker(candidate: Mapping[str, Any], quality: Mapping[str, Any]) -> bool:
    """Detect a supplement from its container metadata or a strong header.

    A regular article often says ``View supplementary material`` on its first
    page.  That navigation link is not evidence that the PDF itself is a
    supplement, so header matching is intentionally narrower than filename
    matching.
    """

    metadata = quality.get("metadata")
    metadata_mapping = metadata if isinstance(metadata, Mapping) else {}
    container_evidence = " ".join(
        item
        for item in (
            Path(_text(candidate.get("path"))).name,
            _text(candidate.get("attachment_title")),
            _text(candidate.get("raw_path")),
            _text(metadata_mapping.get("title")),
            _text(metadata_mapping.get("subject")),
        )
        if item
    ).casefold()
    if _contains_any(container_evidence, _SUPPLEMENT_PATH_MARKERS):
        return True
    header = _text(quality.get("first_page_excerpt"))[:1200].casefold()
    return _contains_any(header, _SUPPLEMENT_HEADER_MARKERS)


def _version_class(evidence_text: str) -> str:
    if _contains_any(evidence_text, _VOR_MARKERS):
        return VERSION_OF_RECORD
    if _contains_any(evidence_text, _ACCEPTED_MARKERS):
        return ACCEPTED_MANUSCRIPT
    if _contains_any(evidence_text, _POSTPRINT_MARKERS):
        return POSTPRINT
    if _contains_any(evidence_text, _PREPRINT_MARKERS):
        return PREPRINT
    return UNKNOWN_VERSION


def _base_role(
    candidate: Mapping[str, Any],
    quality: Mapping[str, Any],
    *,
    parent_identity_trusted: bool,
) -> tuple[str, list[str], bool]:
    reasons: list[str] = []
    evidence_text = _candidate_evidence_text(candidate, quality)
    identity_verdict = _text(candidate.get("identity_verdict")).casefold()
    identity_reasons = {
        _text(item).casefold() for item in (candidate.get("identity_reasons") or [])
    }
    if identity_verdict == "mismatch" or {
        "nonempty_doi_mismatch",
        "multiple_distinct_doi_candidates",
    } & identity_reasons:
        return WRONG_ATTACHMENT, ["positive_identity_conflict"], False
    if _text(quality.get("error")) or not bool(quality.get("render_complete")):
        return WRONG_ATTACHMENT, ["pdf_not_fully_renderable"], False
    if _has_supplement_marker(candidate, quality):
        return SUPPLEMENT, ["supplement_or_appendix_marker"], False
    if _contains_any(evidence_text, _TRANSLATION_MARKERS):
        return TRANSLATION_DERIVATIVE, ["translation_or_bilingual_marker"], False
    # Deterministic evidence for bilingual scan bundles that contain an
    # original-language copy followed by a translated copy is added by the
    # group-level classifier below.  A normal identity match is a usable
    # primary; an identity-ambiguous scan is usable only with a trusted parent
    # relation and complete page rendering.
    if bool(candidate.get("canonical_ready")):
        return PRIMARY_FULLTEXT, ["identity_match"], True
    if (
        parent_identity_trusted
        and bool(quality.get("scanned_like"))
        and bool(quality.get("render_complete"))
    ):
        return SCANNED_PRIMARY, ["trusted_parent_relation_and_complete_render"], True
    return UNKNOWN_VERSION, ["identity_not_confirmed"], False


def _is_paired_bilingual_scan(
    candidate: Mapping[str, Any],
    peer: Mapping[str, Any],
) -> bool:
    quality = candidate.get("quality")
    peer_quality = peer.get("quality")
    if not isinstance(quality, Mapping) or not isinstance(peer_quality, Mapping):
        return False
    pages = int(quality.get("page_count") or 0)
    peer_pages = int(peer_quality.get("page_count") or 0)
    if pages != peer_pages * 2 or not bool(quality.get("render_complete")):
        return False
    if not bool(peer_quality.get("render_complete")):
        return False
    return int(quality.get("image_count") or 0) >= max(20, pages * 10)


def _primary_quality_key(candidate: Mapping[str, Any]) -> tuple[int, int, int, int, float]:
    quality = candidate.get("quality")
    quality_mapping = quality if isinstance(quality, Mapping) else {}
    if str(candidate.get("role") or "") == SCANNED_PRIMARY:
        return (
            int(bool(quality_mapping.get("render_complete"))),
            int(quality_mapping.get("nonblank_pages") or 0),
            int(quality_mapping.get("renderable_pages") or 0),
            int(quality_mapping.get("file_size_bytes") or 0),
            float(quality_mapping.get("max_page_pixels") or 0.0),
        )
    return (
        int(bool(quality_mapping.get("render_complete"))),
        int(quality_mapping.get("text_chars") or 0),
        int(quality_mapping.get("text_nonzero_pages") or 0),
        int(quality_mapping.get("page_count") or 0),
        float(quality_mapping.get("max_page_pixels") or 0.0),
    )


def canonicalize_attachment_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    parent_match_method: str,
    parent_count: int,
) -> dict[str, Any]:
    """Return a deterministic canonical selection and full candidate ledger."""

    working: list[dict[str, Any]] = []
    quality_cache: dict[str, dict[str, Any]] = {}
    parent_identity_trusted = bool(
        int(parent_count or 0) == 1
        and _text(parent_match_method).casefold() in {"doi", "title_exact"}
    )
    for raw_candidate in candidates:
        candidate = dict(raw_candidate)
        path = str(candidate.get("path") or "")
        cache_key = str(Path(path).expanduser().resolve()).casefold()
        if cache_key not in quality_cache:
            quality_cache[cache_key] = inspect_pdf_quality(path)
        quality = quality_cache[cache_key]
        if not _text(candidate.get("sha256")) and bool(quality.get("exists")):
            try:
                candidate["sha256"] = _sha256_file(Path(path).expanduser().resolve())
            except OSError:
                candidate["sha256"] = ""
        role, reasons, eligible = _base_role(
            candidate,
            quality,
            parent_identity_trusted=parent_identity_trusted,
        )
        candidate["quality"] = quality
        candidate["role"] = role
        candidate["base_role"] = role
        candidate["version_class"] = _version_class(_candidate_evidence_text(candidate, quality))
        candidate["role_reasons"] = list(reasons)
        candidate["primary_eligible"] = eligible
        working.append(candidate)

    # A pair of fully rendered scan bundles with an exact 2x page-count
    # relationship is treated as a deterministic bilingual derivative.  This
    # is intentionally narrow and is logged in the candidate evidence.
    for candidate in working:
        if str(candidate.get("role") or "") != SCANNED_PRIMARY:
            continue
        for peer in working:
            if candidate is peer or str(peer.get("role") or "") != SCANNED_PRIMARY:
                continue
            if _is_paired_bilingual_scan(candidate, peer):
                candidate["role"] = TRANSLATION_DERIVATIVE
                candidate["primary_eligible"] = False
                candidate["role_reasons"] = [
                    *list(candidate.get("role_reasons") or []),
                    "paired_bilingual_scan_page_count_2x",
                ]
                break

    # Collapse identical content before version ranking.  The representative
    # is stable and uses dateAdded only inside this exact-hash group.
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for candidate in working:
        digest = _text(candidate.get("sha256")).casefold()
        if digest:
            by_hash.setdefault(digest, []).append(candidate)
    duplicate_count = 0
    representatives: list[dict[str, Any]] = []
    for candidate in working:
        digest = _text(candidate.get("sha256")).casefold()
        group = by_hash.get(digest, []) if digest else []
        if len(group) <= 1:
            representatives.append(candidate)
            continue
        representative = sorted(group, key=_attachment_sort_key)[0]
        if candidate is representative:
            representatives.append(candidate)
            continue
        candidate["role"] = DUPLICATE_IDENTICAL
        candidate["primary_eligible"] = False
        candidate["role_reasons"] = ["same_sha256_as_representative"]
        duplicate_count += 1

    eligible = [
        candidate
        for candidate in representatives
        if bool(candidate.get("primary_eligible"))
        and str(candidate.get("role") or "") in {PRIMARY_FULLTEXT, SCANNED_PRIMARY}
    ]
    ordered = sorted(
        eligible,
        key=lambda candidate: (
            -_VERSION_RANK.get(str(candidate.get("version_class") or UNKNOWN_VERSION), 1),
            tuple(-value for value in _primary_quality_key(candidate)[:-1]),
            -_primary_quality_key(candidate)[-1],
            _attachment_sort_key(candidate),
        ),
    )
    selected = ordered[0] if ordered else None

    events: Counter[str] = Counter()
    auxiliary: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in working:
        role = str(candidate.get("role") or UNKNOWN_VERSION)
        if role == DUPLICATE_IDENTICAL:
            auxiliary.append(candidate)
        elif role in {TRANSLATION_DERIVATIVE, SUPPLEMENT}:
            auxiliary.append(candidate)
        elif role == WRONG_ATTACHMENT:
            rejected.append(candidate)
    if duplicate_count:
        events["DUPLICATE_COLLAPSED"] += duplicate_count
    events["TRANSLATION_EXCLUDED"] += sum(
        1
        for candidate in working
        if candidate.get("role") == TRANSLATION_DERIVATIVE
        or candidate.get("base_role") == TRANSLATION_DERIVATIVE
    )
    events["SUPPLEMENT_EXCLUDED"] += sum(
        1
        for candidate in working
        if candidate.get("role") == SUPPLEMENT
        or candidate.get("base_role") == SUPPLEMENT
    )
    events["IDENTITY_MISMATCH"] += sum(
        1
        for candidate in working
        if candidate.get("role") == WRONG_ATTACHMENT
        or candidate.get("base_role") == WRONG_ATTACHMENT
    )

    selection_reason: list[str] = []
    if selected is not None:
        selected_role = str(selected.get("role") or PRIMARY_FULLTEXT)
        events["SCANNED_PRIMARY" if selected_role == SCANNED_PRIMARY else "PRIMARY_SELECTED"] += 1
        if selected_role == SCANNED_PRIMARY:
            selection_reason.append("selected_complete_rendered_scanned_primary")
        if str(selected.get("version_class") or UNKNOWN_VERSION) == VERSION_OF_RECORD:
            events["VERSION_OF_RECORD_SELECTED"] += 1
            selection_reason.append("version_of_record_rank")
        same_rank = [
            candidate
            for candidate in eligible
            if str(candidate.get("version_class") or UNKNOWN_VERSION)
            == str(selected.get("version_class") or UNKNOWN_VERSION)
        ]
        if len(same_rank) > 1:
            quality_keys = {_primary_quality_key(candidate) for candidate in same_rank}
            if len(quality_keys) == 1:
                events["TIEBREAK_BY_DATE_ADDED"] += 1
                selection_reason.append("date_added_then_attachment_key_tiebreak")
            else:
                selection_reason.append("quality_then_attachment_version_rank")
    else:
        events["STILL_UNRESOLVED"] += 1

    selected_path = str(selected.get("path") or "") if selected else ""
    status = "selected" if selected is not None else "unresolved"
    unresolved_reason = ""
    if selected is None:
        if rejected and not eligible:
            unresolved_reason = "all_parent_attachments_have_positive_identity_conflict_or_are_invalid"
        elif not working:
            unresolved_reason = "no_parent_pdf_candidates"
        else:
            unresolved_reason = "no_safe_primary_after_role_and_quality_filtering"
    return {
        "policy_version": CANONICAL_ATTACHMENT_POLICY_VERSION,
        "status": status,
        "selected": selected,
        "selected_path": selected_path,
        "selected_attachment_key": str(selected.get("attachment_key") or "") if selected else "",
        "selected_role": str(selected.get("role") or "") if selected else "",
        "selected_version_class": str(selected.get("version_class") or "") if selected else "",
        "selection_reason": selection_reason,
        "unresolved_reason": unresolved_reason,
        "candidates": working,
        "auxiliary_attachments": auxiliary,
        "rejected_attachments": rejected,
        "events": dict(events),
    }


__all__ = [
    "ACCEPTED_MANUSCRIPT",
    "CANONICAL_ATTACHMENT_POLICY_VERSION",
    "DUPLICATE_IDENTICAL",
    "POSTPRINT",
    "PREPRINT",
    "PRIMARY_FULLTEXT",
    "SCANNED_PRIMARY",
    "SUPPLEMENT",
    "TRANSLATION_DERIVATIVE",
    "UNKNOWN_VERSION",
    "VERSION_OF_RECORD",
    "WRONG_ATTACHMENT",
    "canonicalize_attachment_candidates",
    "inspect_pdf_quality",
]

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping


_UNKNOWN_DOI_VALUES = {"", "unknown", "n/a", "na", "none", "null"}
_DOI_PATTERN = re.compile(r"^10\.\d{4,9}/[-._;()/:A-Z0-9]+$", re.IGNORECASE)


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _strip_doi_prefix(value: str) -> str:
    text = re.sub(r"^doi:\s*", "", value.strip(), flags=re.IGNORECASE)
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)


def looks_like_doi_value(value: Any) -> bool:
    """Return True when a value appears intended to be a DOI/DOI URL."""
    text = _safe_str(value).casefold()
    return bool(text) and ("10." in text or "doi.org/" in text or text.startswith("doi:"))


def normalize_doi(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    lowered = text.casefold()
    if lowered in _UNKNOWN_DOI_VALUES:
        return ""

    candidate = _strip_doi_prefix(text).strip().rstrip(" .;,")
    decorated_match = re.fullmatch(
        r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)\s*[<\[]https?://(?:dx\.)?doi\.org/.*[>\]]?",
        candidate,
        flags=re.IGNORECASE,
    )
    if decorated_match:
        candidate = decorated_match.group(1).rstrip(" .;,")
    # Do not salvage DOI-looking values embedded in full text, wrapped across
    # lines, or polluted by copied PDF prose.  Those values remain diagnostics
    # for callers that use normalize_paper_identity().
    if re.search(r"\s", candidate):
        return ""
    if not _DOI_PATTERN.fullmatch(candidate):
        return ""
    return candidate.casefold()


def has_normalized_doi(value: Any) -> bool:
    return bool(normalize_doi(value))


def rejected_doi_diagnostic(value: Any, field: str = "doi") -> Dict[str, str] | None:
    """Return a diagnostic for DOI-like values rejected by normalize_doi()."""
    raw = _safe_str(value)
    if not raw or not looks_like_doi_value(raw) or normalize_doi(raw):
        return None
    reason = "invalid_doi_format"
    stripped = _strip_doi_prefix(raw).strip()
    if re.search(r"\s", stripped):
        reason = "polluted_doi_value"
    elif raw.casefold().startswith(("http://", "https://")) and not stripped:
        reason = "truncated_doi_url"
    return {"field": field, "value": raw, "reason": reason}


def normalized_title_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "unknown_title"
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", " ", text).strip() or "unknown_title"


def normalized_author_surnames(authors: Any) -> List[str]:
    if isinstance(authors, list):
        source_authors = authors
    elif authors in (None, ""):
        source_authors = []
    else:
        source_authors = [authors]

    surnames: List[str] = []
    for author in source_authors[:3]:
        text = str(author or "").strip()
        if not text:
            continue
        if "," in text:
            surname = text.split(",", 1)[0]
        else:
            parts = text.split()
            surname = parts[-1] if parts else ""
        surname = re.sub(r"[^\w]+", "", surname).lower()
        if surname:
            surnames.append(surname)
    if len(source_authors) > 3:
        surnames.append("et_al")
    return surnames


def title_author_year_key_from_paper(paper: Mapping[str, Any]) -> str:
    """Return a title + first-author + year identity key when all parts exist."""
    title_key = normalized_title_key(paper.get("title"))
    author_surnames = normalized_author_surnames(paper.get("authors"))
    year = str(paper.get("year") or "").strip().casefold()
    if not title_key or title_key == "unknown_title" or not author_surnames or not year:
        return ""
    return f"{title_key}|{author_surnames[0]}|{year}"


def normalize_paper_identity(
    paper: Mapping[str, Any],
    *,
    source_hash: str = "",
    source_index: int | None = None,
    allow_title_fallback: bool = False,
) -> Dict[str, Any]:
    """Build a structured canonical identity with DOI hygiene diagnostics.

    The strict path used by Outline v2 does not merge title-only records.  If a
    DOI-looking value is polluted by copied PDF text/newlines/truncated URLs, it
    is rejected as a canonical DOI but retained in rejected_identity_values.
    """
    rejected: List[Dict[str, str]] = []
    diagnostics: List[str] = []

    def _reject(value: Any, field: str) -> None:
        diagnostic = rejected_doi_diagnostic(value, field)
        if diagnostic:
            rejected.append(diagnostic)
            diagnostics.append(f"{diagnostic['reason']} in {field}: {diagnostic['value']}")

    canonical_key = ""
    canonical_key_source = ""

    explicit_key = _safe_str(paper.get("canonical_paper_key"))
    if explicit_key:
        explicit_doi = normalize_doi(explicit_key)
        if explicit_doi:
            canonical_key = explicit_doi
            canonical_key_source = "canonical_paper_key.normalized_doi"
        elif looks_like_doi_value(explicit_key):
            _reject(explicit_key, "canonical_paper_key")
        else:
            canonical_key = explicit_key
            canonical_key_source = "canonical_paper_key"

    raw_doi = paper.get("raw_doi", paper.get("doi"))
    doi = normalize_doi(raw_doi)
    if not canonical_key and doi:
        canonical_key = doi
        canonical_key_source = "normalized_doi"
    elif not doi:
        _reject(raw_doi, "doi")

    title_author_year = title_author_year_key_from_paper(paper)
    if not canonical_key and title_author_year:
        canonical_key = title_author_year
        canonical_key_source = "normalized_title_first_author_year"

    title_key = normalized_title_key(paper.get("title"))
    if not canonical_key and allow_title_fallback and title_key != "unknown_title":
        canonical_key = title_key
        canonical_key_source = "normalized_title"

    if not canonical_key:
        suffix = source_hash or str(source_index if source_index is not None else "unknown")
        canonical_key = f"source:{suffix}"
        canonical_key_source = "source_hash"
        diagnostics.append("missing_stable_paper_identity")

    aliases: List[str] = []
    for value in [
        explicit_key,
        doi,
        title_author_year,
        title_key if title_key != "unknown_title" else "",
        source_hash,
        _safe_str(paper.get("source_paper_id")),
        *[_safe_str(item) for item in (paper.get("paper_key_aliases") or [])],
    ]:
        if value and value not in aliases:
            aliases.append(value)

    return {
        "canonical_key": canonical_key,
        "canonical_key_source": canonical_key_source,
        "aliases": aliases,
        "rejected_identity_values": rejected,
        "diagnostics": diagnostics,
    }


def build_canonical_paper_key(paper: Mapping[str, Any]) -> str:
    """Build the canonical Outline/Stage identity key.

    Priority:
    1. explicit canonical_paper_key
    2. normalized DOI
    3. normalized title + first author + year
    4. normalized title fallback

    This keeps the legacy build_paper_key() fallback stable for older callers
    while giving newer artifact loops the stricter title-author-year identity.
    """
    return normalize_paper_identity(paper, allow_title_fallback=True)["canonical_key"]


def build_paper_key(paper: Mapping[str, Any]) -> str:
    doi = normalize_doi(paper.get("doi"))
    if doi:
        return doi

    title_key = normalized_title_key(paper.get("title"))
    author_surnames = normalized_author_surnames(paper.get("authors"))
    authors_key = "_".join(author_surnames) if author_surnames else "unknown_author"
    return f"{title_key}_{authors_key}"

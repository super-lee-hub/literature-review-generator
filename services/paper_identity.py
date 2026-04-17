from __future__ import annotations

import re
from typing import Any, List, Mapping


_UNKNOWN_DOI_VALUES = {"", "unknown", "n/a", "na", "none", "null"}


def normalize_doi(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    lowered = text.casefold()
    if lowered in _UNKNOWN_DOI_VALUES:
        return ""

    text = re.sub(r"^doi:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)
    match = re.search(r"(10\.\d+/.+)", text, flags=re.IGNORECASE)
    if match:
        text = match.group(1)

    return text.strip().rstrip(" .;,)").casefold()


def has_normalized_doi(value: Any) -> bool:
    return bool(normalize_doi(value))


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
        parts = str(author or "").strip().split()
        if parts:
            surnames.append(parts[-1].lower())
    if len(source_authors) > 3:
        surnames.append("et_al")
    return surnames


def build_paper_key(paper: Mapping[str, Any]) -> str:
    doi = normalize_doi(paper.get("doi"))
    if doi:
        return doi

    title_key = normalized_title_key(paper.get("title"))
    author_surnames = normalized_author_surnames(paper.get("authors"))
    authors_key = "_".join(author_surnames) if author_surnames else "unknown_author"
    return f"{title_key}_{authors_key}"

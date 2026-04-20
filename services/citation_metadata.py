from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping

from services.paper_identity import normalize_doi
from summary_schema import get_paper_metadata


_NOISE_PHRASES = [
    "abstract",
    "摘要",
    "keywords",
    "关键词",
    "references",
    "参考文献",
    "published by",
    "to cite this article",
    "to cite this document",
    "article information",
    "contents lists available at",
    "science direct",
    "sciencedirect",
    "e-mail",
    "email",
    "issn",
    "vol.",
    "volume",
    "issue",
    "page ",
    "page 1",
    "journal of management science september",
]

_AUTHOR_BLOCKLIST = {
    "published by",
    "article",
    "article information",
    "to cite this article",
    "to cite this document",
    "quick commerce",
    "commerce",
    "document",
    "by",
    "page 1",
}

_REASON_POLLUTED_AUTHOR = "polluted_metadata_author"
_REASON_POLLUTED_JOURNAL = "polluted_metadata_journal"
_REASON_POLLUTED_DOI = "polluted_metadata_doi"
_REASON_AMBIGUOUS_IDENTITY = "ambiguous_identity"
_REASON_MISSING_STRUCTURED = "missing_structured_citations"
_DEFAULT_THRESHOLD = 0.85


@dataclass(frozen=True)
class NormalizedPaperMetadata:
    title: str
    authors: List[str]
    year: str
    journal: str
    doi: str
    status: str
    reasons: List[str]
    confidence_score: float
    decision_threshold: float
    decision_source: str
    source_fields: Dict[str, str]


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def _looks_like_noise_blob(text: str, *, max_len: int = 160) -> bool:
    candidate = _normalize_space(text)
    if not candidate:
        return True
    lowered = candidate.casefold()
    if len(candidate) > max_len:
        return True
    if "@" in candidate or "http://" in lowered or "https://" in lowered:
        return True
    if sum(1 for phrase in _NOISE_PHRASES if phrase in lowered) >= 1:
        return True
    if candidate.count("。") > 1 or candidate.count(".") > 6:
        return True
    if len(re.findall(r"\d", candidate)) > 16:
        return True
    return False


def normalize_title(value: Any) -> str:
    candidate = _normalize_space(value)
    if not candidate or _looks_like_noise_blob(candidate, max_len=220):
        return ""
    return candidate


def _split_authors(value: Any) -> List[str]:
    if isinstance(value, list):
        raw_items = [str(item or "").strip() for item in value]
    elif value in (None, ""):
        raw_items = []
    else:
        raw_items = [segment.strip() for segment in re.split(r";|,|&| and ", str(value))]
    return [item for item in raw_items if item]


def _clean_author(author: str) -> str:
    candidate = _normalize_space(author)
    candidate = re.sub(r"\[\d+\]", "", candidate).strip()
    candidate = re.sub(r"\s+", " ", candidate)
    return candidate


def _is_valid_author(author: str) -> bool:
    candidate = _clean_author(author)
    if not candidate:
        return False
    lowered = candidate.casefold()
    if lowered in _AUTHOR_BLOCKLIST:
        return False
    if any(phrase in lowered for phrase in _NOISE_PHRASES):
        return False
    if "@" in candidate or "doi" in lowered:
        return False
    if len(candidate) > 60:
        return False
    if len(re.findall(r"\d", candidate)) > 2:
        return False
    if _contains_cjk(candidate):
        return 2 <= len(candidate.replace(" ", "")) <= 12
    parts = candidate.replace(".", " ").split()
    if not parts or len(parts) > 6:
        return False
    if any(len(part) > 24 for part in parts):
        return False
    lowercase_parts = sum(1 for part in parts if part and part[0].islower())
    if lowercase_parts > 1:
        return False
    return True


def normalize_authors(value: Any) -> List[str]:
    authors: List[str] = []
    for raw_author in _split_authors(value):
        cleaned = _clean_author(raw_author)
        if cleaned and _is_valid_author(cleaned) and cleaned not in authors:
            authors.append(cleaned)
    return authors


def normalize_year(value: Any) -> str:
    candidate = _normalize_space(value)
    if not candidate:
        return ""
    match = re.search(r"\b(19|20)\d{2}\b", candidate)
    return match.group(0) if match else ""


def normalize_journal(value: Any) -> str:
    candidate = _normalize_space(value)
    if not candidate:
        return ""
    if _looks_like_noise_blob(candidate, max_len=120):
        return ""
    return candidate


def sanitize_metadata_fields(metadata: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "title": normalize_title(metadata.get("title")),
        "authors": normalize_authors(metadata.get("authors")),
        "year": normalize_year(metadata.get("year") or metadata.get("date")),
        "journal": normalize_journal(
            metadata.get("journal")
            or metadata.get("出版物")
            or metadata.get("刊名简称")
        ),
        "doi": normalize_doi(metadata.get("doi")),
    }


def _first_nonempty(candidates: Iterable[tuple[str, str]]) -> tuple[str, str]:
    for source, value in candidates:
        if value:
            return source, value
    return "", ""


def normalize_summary_paper_metadata(summary: Mapping[str, Any]) -> NormalizedPaperMetadata:
    paper_info_raw = summary.get("paper_info", {})
    paper_info = paper_info_raw if isinstance(paper_info_raw, Mapping) else {}
    ai_summary = summary.get("ai_summary", {})
    ai_metadata_raw = get_paper_metadata(ai_summary) if ai_summary else {}
    ai_metadata = ai_metadata_raw if isinstance(ai_metadata_raw, Mapping) else {}

    paper_fields = sanitize_metadata_fields(paper_info)
    ai_fields = sanitize_metadata_fields(ai_metadata)

    reasons: List[str] = []

    raw_paper_authors = paper_info.get("authors")
    if raw_paper_authors and not paper_fields["authors"]:
        reasons.append(_REASON_POLLUTED_AUTHOR)

    raw_paper_journal = (
        paper_info.get("journal")
        or paper_info.get("出版物")
        or paper_info.get("刊名简称")
    )
    if raw_paper_journal and not paper_fields["journal"]:
        reasons.append(_REASON_POLLUTED_JOURNAL)

    raw_paper_doi = paper_info.get("doi")
    if raw_paper_doi and not paper_fields["doi"]:
        reasons.append(_REASON_POLLUTED_DOI)

    title_source, title = _first_nonempty(
        [
            ("paper_info.title", paper_fields["title"]),
            ("ai_summary.paper_metadata.title", ai_fields["title"]),
        ]
    )
    authors_source, authors_value = _first_nonempty(
        [
            ("paper_info.authors", "__AUTHORS__" if paper_fields["authors"] else ""),
            ("ai_summary.paper_metadata.authors", "__AUTHORS__" if ai_fields["authors"] else ""),
        ]
    )
    authors = paper_fields["authors"] if authors_source == "paper_info.authors" else ai_fields["authors"]
    year_source, year = _first_nonempty(
        [
            ("paper_info.year", paper_fields["year"]),
            ("ai_summary.paper_metadata.year", ai_fields["year"]),
        ]
    )
    journal_source, journal = _first_nonempty(
        [
            ("paper_info.journal", paper_fields["journal"]),
            ("paper_info.出版物", normalize_journal(paper_info.get("出版物"))),
            ("paper_info.刊名简称", normalize_journal(paper_info.get("刊名简称"))),
            ("ai_summary.paper_metadata.journal", ai_fields["journal"]),
        ]
    )
    doi_source, doi = _first_nonempty(
        [
            ("paper_info.doi", paper_fields["doi"]),
            ("ai_summary.paper_metadata.doi", ai_fields["doi"]),
        ]
    )

    confidence = 1.0
    if not title:
        confidence -= 0.30
        reasons.append(_REASON_AMBIGUOUS_IDENTITY)
    if not authors:
        confidence -= 0.25
        reasons.append(_REASON_POLLUTED_AUTHOR)
    if not year:
        confidence -= 0.15
    if not journal:
        confidence -= 0.10
    if not doi:
        confidence -= 0.05
    if title_source and title_source != "paper_info.title":
        confidence -= 0.05
    if authors_source and authors_source != "paper_info.authors":
        confidence -= 0.05
    if journal_source and not journal_source.startswith("paper_info.journal"):
        confidence -= 0.03
    if doi_source and doi_source != "paper_info.doi":
        confidence -= 0.02

    confidence = max(0.0, min(1.0, round(confidence, 2)))
    deduped_reasons = list(dict.fromkeys(reason for reason in reasons if reason))

    if confidence < 0.60 or _REASON_AMBIGUOUS_IDENTITY in deduped_reasons:
        status = "rerun_required"
    elif confidence < _DEFAULT_THRESHOLD or deduped_reasons:
        status = "rebuilt_with_warnings"
    else:
        status = "clean_canonical"

    return NormalizedPaperMetadata(
        title=title,
        authors=authors,
        year=year,
        journal=journal,
        doi=doi,
        status=status,
        reasons=deduped_reasons,
        confidence_score=confidence,
        decision_threshold=_DEFAULT_THRESHOLD,
        decision_source="rule",
        source_fields={
            "title": title_source,
            "authors": authors_source,
            "year": year_source,
            "journal": journal_source,
            "doi": doi_source,
        },
    )

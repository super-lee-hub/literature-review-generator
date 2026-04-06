from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


def build_paper_key(paper: Mapping[str, Any]) -> str:
    doi = str(paper.get("doi") or "").strip()
    if doi and doi.lower() not in {"unknown", "n/a"}:
        match = re.search(r"(10\.\d+/.+)", doi)
        if match:
            return match.group(1)
        return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)

    title = str(paper.get("title") or "").strip()
    title_clean = re.sub(r"[^\w\s]", "", title.lower())
    title_clean = re.sub(r"\s+", " ", title_clean).strip() or "unknown_title"

    authors = paper.get("authors") or []
    if not isinstance(authors, list):
        authors = [authors]
    surnames: List[str] = []
    for author in authors[:3]:
        parts = str(author or "").strip().split()
        if parts:
            surnames.append(parts[-1].lower())
    if len(authors) > 3:
        surnames.append("et_al")
    authors_clean = "_".join(surnames) if surnames else "unknown_author"
    return f"{title_clean}_{authors_clean}"


def normalize_alias(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.sub(r"\s+", "", text).casefold()


def extract_citation_key(citation_token: str) -> str:
    match = re.match(r"\[\[cite:([^|\]]+)(?:\|[^\]]+)*\]\]", str(citation_token or "").strip())
    if match:
        return match.group(1).strip()
    return str(citation_token or "").strip()


def extract_review_body(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    if "=== 正文 ===" in raw:
        raw = raw.split("=== 正文 ===", 1)[1].strip()

    cleanup_markers = [
        "=== 逻辑规划 ===",
        "文献引用矩阵",
        "写作思路",
        "数据核查处理计划",
        "数据核查计划",
    ]
    lines = [line.rstrip() for line in raw.splitlines()]
    cleaned_lines: List[str] = []
    skipping_planning = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append("")
            continue
        if stripped == "=== 逻辑规划 ===":
            skipping_planning = True
            continue
        if stripped == "=== 正文 ===":
            skipping_planning = False
            continue
        if skipping_planning:
            continue
        if any(marker in stripped for marker in cleanup_markers):
            continue
        cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


@dataclass(frozen=True)
class CitationCatalogEntry:
    index: int
    paper_id: str
    paper_key: str
    title: str
    authors: List[str]
    year: str
    journal: str
    doi: str
    aliases: List[str]


def _author_surname(author: str) -> str:
    parts = str(author or "").strip().split()
    return parts[-1] if parts else "Anonymous"


def format_in_text_citation(
    entry: CitationCatalogEntry,
    *,
    mode: str = "parenthetical",
    locator: Optional[str] = None,
) -> str:
    authors = entry.authors or ["Anonymous"]
    surnames = [_author_surname(author) for author in authors]
    if len(surnames) == 1:
        author_text = surnames[0]
    elif len(surnames) == 2:
        author_text = f"{surnames[0]} & {surnames[1]}"
    else:
        author_text = f"{surnames[0]} et al."

    year = entry.year or "n.d."
    locator_text = f", {locator}" if locator else ""
    mode_normalized = str(mode or "parenthetical").strip().lower()
    if mode_normalized == "narrative":
        return f"{author_text} ({year}{locator_text})"
    return f"({author_text}, {year}{locator_text})"


def format_reference_entry(entry: CitationCatalogEntry) -> str:
    if entry.authors:
        if len(entry.authors) <= 7:
            author_text = ", ".join(entry.authors)
        else:
            author_text = ", ".join(entry.authors[:6]) + ", ..., " + entry.authors[-1]
    else:
        author_text = "Anonymous"

    parts = [author_text, f"({entry.year or 'n.d.'}).", f"{entry.title or 'Untitled.'}"]
    if entry.journal:
        parts.append(f"*{entry.journal}*")
    if entry.doi:
        parts.append(f"https://doi.org/{entry.doi}")
    return " ".join(part for part in parts if part).strip()


def build_citation_catalog(
    paper_summaries: Sequence[Mapping[str, Any]],
) -> Tuple[List[CitationCatalogEntry], Dict[str, CitationCatalogEntry]]:
    entries: List[CitationCatalogEntry] = []
    alias_map: Dict[str, CitationCatalogEntry] = {}

    for index, summary in enumerate(paper_summaries, start=1):
        paper_info_raw = summary.get("paper_info", {})
        paper_info = paper_info_raw if isinstance(paper_info_raw, Mapping) else {}
        authors = paper_info.get("authors") or []
        if not isinstance(authors, list):
            authors = [authors]

        paper_key = str(
            paper_info.get("canonical_paper_key")
            or paper_info.get("source_paper_id")
            or build_paper_key(paper_info)
        ).strip()
        aliases = {
            paper_key,
            f"ref_{index:03d}",
            f"paper_{index}",
            f"paper{index}",
            f"文献{index}",
            f"文献 {index}",
        }
        for item in paper_info.get("paper_key_aliases") or []:
            aliases.add(str(item).strip())
        title = str(paper_info.get("title") or "").strip()
        if title:
            aliases.add(title)

        entry = CitationCatalogEntry(
            index=index,
            paper_id=paper_key,
            paper_key=paper_key,
            title=title,
            authors=[str(author).strip() for author in authors if str(author).strip()],
            year=str(paper_info.get("year") or "").strip(),
            journal=str(paper_info.get("journal") or "").strip(),
            doi=str(paper_info.get("doi") or "").strip(),
            aliases=sorted(alias for alias in aliases if alias),
        )
        entries.append(entry)
        for alias in entry.aliases:
            alias_map[normalize_alias(alias)] = entry

    return entries, alias_map


def resolve_citation_entry(
    citation_key_or_token: str,
    alias_map: Mapping[str, CitationCatalogEntry],
) -> Optional[CitationCatalogEntry]:
    raw_key = extract_citation_key(citation_key_or_token)
    normalized = normalize_alias(raw_key)
    if not normalized:
        return None
    return alias_map.get(normalized)

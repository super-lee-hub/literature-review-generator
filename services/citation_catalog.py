from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from services.citation_metadata import normalize_summary_paper_metadata
from services.paper_identity import (
    build_paper_key as build_legacy_paper_key,
    normalize_doi,
    normalized_title_key,
)


def build_paper_key(paper: Mapping[str, Any]) -> str:
    return build_legacy_paper_key(paper)


def normalize_alias(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.sub(r"\s+", "", text).casefold()


_DOI_IN_TEXT_RE = re.compile(r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", re.IGNORECASE)


def extract_doi_aliases(value: Any) -> List[str]:
    """Return clean DOI aliases found in a possibly decorated DOI field."""
    aliases: List[str] = []
    direct = normalize_doi(value)
    if direct:
        aliases.append(direct)
    for match in _DOI_IN_TEXT_RE.finditer(str(value or "")):
        candidate = normalize_doi(match.group(1))
        if candidate and candidate not in aliases:
            aliases.append(candidate)
    return aliases


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
    migration_status: str = "clean_canonical"
    migration_reasons: List[str] | None = None
    confidence_score: float = 1.0
    decision_threshold: float = 0.85
    decision_source: str = "rule"
    source_fields: Dict[str, str] | None = None


def _author_surname(author: str) -> str:
    parts = str(author or "").strip().split()
    return parts[-1] if parts else "Anonymous"


_CJK_PINYIN = {
    "邱": "qiu",
    "凌": "ling",
    "云": "yun",
    "庞": "pang",
    "隽": "jun",
    "苏": "su",
    "源": "yuan",
    "宋": "song",
    "晓": "xiao",
    "兵": "bing",
    "何": "he",
    "夏": "xia",
    "楠": "nan",
    "熊": "xiong",
    "玉": "yu",
    "娟": "juan",
    "吕": "lv",
    "巍": "wei",
    "金": "jin",
    "振": "zhen",
    "宇": "yu",
    "杨": "yang",
    "锐": "rui",
    "花": "hua",
    "海": "hai",
    "燕": "yan",
    "洋": "yang",
    "伍": "wu",
    "勇": "yong",
    "强": "qiang",
    "罗": "luo",
    "津": "jin",
    "窦": "dou",
    "文": "wen",
    "静": "jing",
}


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in str(text or ""))


def _romanized_cjk_variants(text: Any) -> List[str]:
    raw = str(text or "").strip()
    if not raw or not _contains_cjk(raw):
        return []
    parts: List[str] = []
    for char in raw:
        if char.isspace():
            continue
        if char in _CJK_PINYIN:
            parts.append(_CJK_PINYIN[char])
        elif "\u4e00" <= char <= "\u9fff":
            return []
    romanized = "".join(parts)
    if not romanized:
        return []
    variants = [romanized]
    ng_simplified = romanized.replace("ng", "n")
    if ng_simplified != romanized:
        variants.append(ng_simplified)
    return list(dict.fromkeys(variants))


def _yearless_alias_variants(value: Any) -> List[str]:
    text = str(value or "").strip()
    if not text:
        return []
    variants = [text]
    stripped = re.sub(r"[_-]?(?:19|20)\d{2}$", "", text, flags=re.IGNORECASE)
    if stripped and stripped != text:
        variants.append(stripped)
    return list(dict.fromkeys(variants))


def alias_lookup_keys(value: Any) -> List[str]:
    keys: List[str] = []
    for variant in _yearless_alias_variants(value):
        normalized = normalize_alias(variant)
        if normalized and normalized not in keys:
            keys.append(normalized)
    return keys


def _add_alias_with_variants(aliases: set[str], value: Any) -> None:
    text = str(value or "").strip()
    if not text:
        return
    aliases.update(_yearless_alias_variants(text))
    title_key = normalized_title_key(text)
    if title_key != "unknown_title":
        aliases.add(title_key)
    for doi_alias in extract_doi_aliases(text):
        aliases.add(doi_alias)
        aliases.add(f"https://doi.org/{doi_alias}")


def _add_author_year_aliases(aliases: set[str], authors: Sequence[str], year: str) -> None:
    author_list = [str(author).strip() for author in authors if str(author).strip()]
    if not author_list:
        return

    surnames = [_author_surname(author) for author in author_list]
    romanized_full_names = [_romanized_cjk_variants(author) for author in author_list]
    romanized_surnames = [
        _romanized_cjk_variants(str(author).strip()[0]) if _contains_cjk(author) else []
        for author in author_list
    ]
    for author, surname in zip(author_list, surnames):
        aliases.add(author)
        aliases.add(surname)
        if year:
            aliases.add(f"{surname}{year}")
            aliases.add(f"{surname}, {year}")
            aliases.add(f"{surname}_{year}")
    for full_name_variants, surname_variants in zip(romanized_full_names, romanized_surnames):
        for alias in [*full_name_variants, *surname_variants]:
            aliases.add(alias)
            if year:
                aliases.add(f"{alias}{year}")
                aliases.add(f"{alias}_{year}")

    if len(surnames) > 1:
        first_surname = surnames[0]
        second_surname = surnames[1]
        aliases.add(f"{first_surname} et al.")
        aliases.add(f"{first_surname}_et_al")
        aliases.add(f"{first_surname} and {second_surname}")
        aliases.add(f"{first_surname}_and_{second_surname}")
        aliases.add(f"{first_surname}_{second_surname}")
        if year:
            aliases.add(f"{first_surname} et al.{year}")
            aliases.add(f"{first_surname} et al., {year}")
            aliases.add(f"{first_surname}_et_al_{year}")
            aliases.add(f"{first_surname} and {second_surname}, {year}")
            aliases.add(f"{first_surname}_and_{second_surname}_{year}")
            aliases.add(f"{first_surname}_{second_surname}_{year}")

    romanized_first = romanized_surnames[0] if romanized_surnames else []
    romanized_second = romanized_surnames[1] if len(romanized_surnames) > 1 else []
    for first in romanized_first:
        aliases.add(f"{first}_et_al")
        if year:
            aliases.add(f"{first}_et_al_{year}")
        for second in romanized_second:
            for combo in (
                f"{first}{second}",
                f"{first}_{second}",
                f"{first}_and_{second}",
            ):
                aliases.add(combo)
                if year:
                    aliases.add(f"{combo}{year}")
                    aliases.add(f"{combo}_{year}")


def _title_alias_variants(title: str) -> List[str]:
    normalized = normalized_title_key(title).replace(" ", "")
    if not normalized or normalized == "unknown_title":
        return []
    variants = [normalized]
    for separator in ("对", "的影响", "研究", "作用"):
        index = normalized.find(separator)
        if index > 3:
            variants.append(normalized[:index])
    for length in (4, 6, 8, 10, 12):
        if len(normalized) > length:
            variants.append(normalized[:length])
    return list(dict.fromkeys(variant for variant in variants if len(variant) > 3))


def _add_title_author_aliases(
    aliases: set[str],
    title: str,
    authors: Sequence[str],
    year: str,
) -> None:
    author_list = [str(author).strip() for author in authors if str(author).strip()]
    if not title or not author_list:
        return
    author_combo = "_".join(author_list[:3])
    if len(author_list) > 3:
        author_combo = f"{author_combo}_et_al"
    romanized_surnames: List[str] = []
    for author in author_list[:3]:
        if _contains_cjk(author):
            romanized_surnames.extend(_romanized_cjk_variants(author[0])[:1])
        else:
            romanized_surnames.append(_author_surname(author).casefold())
    romanized_combo = "_".join(item for item in romanized_surnames if item)
    if len(author_list) > 3 and romanized_combo:
        romanized_combo = f"{romanized_combo}_et_al"
    for title_variant in _title_alias_variants(title):
        for combo in (author_combo, romanized_combo):
            if not combo:
                continue
            alias = f"{title_variant}_{combo}"
            aliases.add(alias)
            if year:
                aliases.add(f"{alias}_{year}")


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
    # Clean metadata fields
    def clean_field(value: Any) -> str:
        if not value:
            return ""
        text = str(value).strip()
        # Remove placeholder values
        placeholders = ['未知年份', '未知期刊', '无标题', 'n.d.']
        for placeholder in placeholders:
            if text == placeholder:
                return ""
        # Remove common noise patterns
        noise_patterns = [
            "Contents lists available at ScienceDirect",
            "RESEARCH ARTICLE",
            "Article",
            "Abstract",
            "摘要",
            "Introduction",
            "引言",
            "Keywords",
            "关键词",
            "References",
            "参考文献",
            "Copyright",
            "版权",
            "©",
            "Published by",
            "Elsevier",
            "Springer",
            "Taylor & Francis",
            "Wiley",
            "Oxford University Press",
            "Cambridge University Press",
            "American Psychological Association",
            "APA",
            "IEEE",
            "ACM",
            "SpringerLink",
            "ScienceDirect",
            "PubMed",
            "Google Scholar",
            "DOI:",
            "doi:",
        ]
        for pattern in noise_patterns:
            text = text.replace(pattern, "").strip()
        # Remove excessive whitespace
        text = ' '.join(text.split())
        return text

    def clean_doi(value: Any) -> str:
        if not value:
            return ""
        text = str(value).strip()
        # Extract only the DOI part (10.xxxx/xxxx)
        import re
        doi_match = re.search(r'10\.\d{4,}/[^\s]+', text)
        if doi_match:
            return doi_match.group(0)
        # Remove any non-DOI content
        text = text.replace("https://doi.org/", "").strip()
        # Keep only alphanumeric, dots, slashes, hyphens, and underscores
        text = re.sub(r'[^a-zA-Z0-9./\-_]', '', text)
        return text

    # Clean authors
    cleaned_authors = [clean_field(author) for author in (entry.authors or []) if clean_field(author)]
    if cleaned_authors:
        if len(cleaned_authors) <= 7:
            author_text = ", ".join(cleaned_authors)
        else:
            author_text = ", ".join(cleaned_authors[:6]) + ", ..., " + cleaned_authors[-1]
    else:
        author_text = "Anonymous"

    # Clean other fields
    cleaned_year = clean_field(entry.year)
    cleaned_title = clean_field(entry.title)
    cleaned_journal = clean_field(entry.journal)
    cleaned_doi = clean_doi(entry.doi)

    parts = [author_text, f"({cleaned_year or 'n.d.'}).", f"{cleaned_title or 'Untitled.'}"]
    if cleaned_journal:
        parts.append(f"*{cleaned_journal}*")
    if cleaned_doi:
        parts.append(f"https://doi.org/{cleaned_doi}")
    
    reference = " ".join(part for part in parts if part).strip()
    # Skip references that are just placeholders
    if reference and not reference.strip() == "Anonymous (n.d.). Untitled.":
        return reference
    return ""


def build_citation_catalog(
    paper_summaries: Sequence[Mapping[str, Any]],
) -> Tuple[List[CitationCatalogEntry], Dict[str, CitationCatalogEntry]]:
    entries: List[CitationCatalogEntry] = []
    alias_map: Dict[str, CitationCatalogEntry] = {}

    for index, summary in enumerate(paper_summaries, start=1):
        paper_info_raw = summary.get("paper_info", {})
        paper_info = paper_info_raw if isinstance(paper_info_raw, Mapping) else {}
        normalized_metadata = normalize_summary_paper_metadata(summary)
        authors = normalized_metadata.authors

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
            str(index),
            f"文献{index}",
            f"文献 {index}",
        }
        for item in paper_info.get("paper_key_aliases") or []:
            _add_alias_with_variants(aliases, item)
        title = normalized_metadata.title or str(paper_info.get("title") or "").strip()
        if title:
            _add_alias_with_variants(aliases, title)
        for item in (
            paper_key,
            paper_info.get("source_paper_id"),
            normalized_metadata.doi,
            paper_info.get("doi"),
        ):
            _add_alias_with_variants(aliases, item)
        
        # Add author-based aliases
        author_list = [str(author).strip() for author in authors if str(author).strip()]
        year = normalized_metadata.year or str(paper_info.get("year") or "").strip()
        _add_author_year_aliases(aliases, author_list, year)
        _add_title_author_aliases(aliases, title, author_list, year)

        entry = CitationCatalogEntry(
            index=index,
            paper_id=paper_key,
            paper_key=paper_key,
            title=title,
            authors=author_list,
            year=year,
            journal=normalized_metadata.journal,
            doi=normalized_metadata.doi,
            aliases=sorted(alias for alias in aliases if alias),
            migration_status=normalized_metadata.status,
            migration_reasons=list(normalized_metadata.reasons),
            confidence_score=normalized_metadata.confidence_score,
            decision_threshold=normalized_metadata.decision_threshold,
            decision_source=normalized_metadata.decision_source,
            source_fields=dict(normalized_metadata.source_fields),
        )
        entries.append(entry)
        for alias in entry.aliases:
            alias_map[normalize_alias(alias)] = entry

    return entries, alias_map


def build_citation_catalog_from_manifest(
    citation_manifest: Mapping[str, Any],
) -> Dict[str, CitationCatalogEntry]:
    alias_map: Dict[str, CitationCatalogEntry] = {}
    for index, entry_data in enumerate(citation_manifest.get("paper_entries", []), start=1):
        alias_values: set[str] = set()
        for item in (entry_data.get("aliases") or []):
            _add_alias_with_variants(alias_values, item)
        for item in (
            entry_data.get("entry_id"),
            entry_data.get("paper_id"),
            entry_data.get("paper_key"),
            entry_data.get("title"),
            entry_data.get("doi"),
        ):
            _add_alias_with_variants(alias_values, item)
        authors = [str(item).strip() for item in (entry_data.get("authors") or []) if str(item).strip()]
        year = str(entry_data.get("year") or "")
        _add_author_year_aliases(alias_values, authors, year)
        _add_title_author_aliases(alias_values, str(entry_data.get("title") or ""), authors, year)
        aliases = sorted(alias for alias in alias_values if alias)
        entry = CitationCatalogEntry(
            index=index,
            paper_id=str(entry_data.get("paper_id") or entry_data.get("paper_key") or ""),
            paper_key=str(entry_data.get("paper_key") or entry_data.get("paper_id") or ""),
            title=str(entry_data.get("title") or ""),
            authors=authors,
            year=year,
            journal=str(entry_data.get("journal") or ""),
            doi=str(entry_data.get("doi") or ""),
            aliases=aliases,
            migration_status=str(entry_data.get("status") or "clean_canonical"),
            migration_reasons=list(entry_data.get("reasons") or []),
            confidence_score=float(entry_data.get("confidence_score") or 1.0),
            decision_threshold=float(entry_data.get("decision_threshold") or 0.85),
            decision_source=str(entry_data.get("decision_source") or "rule"),
            source_fields=dict(entry_data.get("source_fields") or {}),
        )
        for alias in entry.aliases:
            alias_map[normalize_alias(alias)] = entry
    return alias_map


def _literature_map_node_aliases(node: Mapping[str, Any]) -> List[str]:
    aliases: List[str] = []
    for item in (
        node.get("paper_key"),
        node.get("canonical_paper_key"),
        node.get("source_summary_hash"),
        node.get("title"),
    ):
        if item:
            aliases.append(str(item))
    aliases.extend(str(item) for item in (node.get("aliases") or []) if str(item).strip())
    for record in node.get("source_records") or []:
        if not isinstance(record, Mapping):
            continue
        for item in (
            record.get("paper_key_seen"),
            record.get("canonical_paper_key"),
            record.get("canonical_key"),
            record.get("source_hash"),
            record.get("title"),
            record.get("doi"),
            record.get("raw_doi"),
        ):
            if item:
                aliases.append(str(item))
        aliases.extend(str(item) for item in (record.get("aliases") or []) if str(item).strip())
    return list(dict.fromkeys(aliases))


def augment_citation_catalog_from_literature_map(
    entries: Sequence[CitationCatalogEntry],
    alias_map: Mapping[str, CitationCatalogEntry],
    literature_map: Optional[Mapping[str, Any]],
) -> Tuple[List[CitationCatalogEntry], Dict[str, CitationCatalogEntry]]:
    if not literature_map:
        return list(entries), dict(alias_map)

    entries_by_id = {entry.paper_id: entry for entry in entries}
    title_to_id = {
        normalize_alias(entry.title): entry.paper_id
        for entry in entries
        if normalize_alias(entry.title)
    }
    augmented_aliases: Dict[str, set[str]] = {
        entry.paper_id: set(entry.aliases)
        for entry in entries
    }

    def _match_aliases(aliases: Sequence[str]) -> Optional[CitationCatalogEntry]:
        for alias in aliases:
            matched = alias_map.get(normalize_alias(alias))
            if matched is not None:
                return matched
            for doi_alias in extract_doi_aliases(alias):
                matched = alias_map.get(normalize_alias(doi_alias))
                if matched is not None:
                    return matched
        for alias in aliases:
            matched_id = title_to_id.get(normalize_alias(alias))
            if matched_id:
                return entries_by_id[matched_id]
        return None

    for node in literature_map.get("paper_nodes", []):
        if not isinstance(node, Mapping):
            continue
        node_aliases = _literature_map_node_aliases(node)
        matched_entry = _match_aliases(node_aliases)
        if matched_entry is None:
            continue
        for alias in node_aliases:
            _add_alias_with_variants(augmented_aliases[matched_entry.paper_id], alias)

    updated_entries = [
        replace(entry, aliases=sorted(alias for alias in augmented_aliases[entry.paper_id] if alias))
        for entry in entries
    ]
    updated_alias_map: Dict[str, CitationCatalogEntry] = {}
    for entry in updated_entries:
        for alias in entry.aliases:
            normalized = normalize_alias(alias)
            if normalized:
                updated_alias_map[normalized] = entry
    return updated_entries, updated_alias_map


def resolve_citation_entry(
    citation_key_or_token: str,
    alias_map: Mapping[str, CitationCatalogEntry],
) -> Optional[CitationCatalogEntry]:
    raw_key = extract_citation_key(citation_key_or_token)
    for normalized in alias_lookup_keys(raw_key):
        matched = alias_map.get(normalized)
        if matched is not None:
            return matched
    return None

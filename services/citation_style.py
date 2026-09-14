"""Small, deterministic CSL-like citation style boundary.

The project keeps citation metadata in JSON artifacts rather than depending on
an installed word processor or a network-backed CSL service.  This module is
the single formatting boundary for the shipped APA 7 backend.  It accepts
both legacy author strings and canonical CSL/Zotero-style creator mappings so
callers can migrate metadata incrementally without guessing surnames at each
rendering site.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from services.paper_identity import normalize_doi


@dataclass(frozen=True)
class ReferenceSegment:
    """A text fragment in a reference with its document formatting intent."""

    text: str
    italic: bool = False


@dataclass(frozen=True)
class FormattedReference:
    """Rendered reference text plus the segments needed by DOCX writers."""

    segments: tuple[ReferenceSegment, ...]

    @property
    def text(self) -> str:
        return "".join(segment.text for segment in self.segments)


def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    text = text.replace("*", "")
    return re.sub(r"\s+", " ", text)


def _contains_cjk(value: str) -> bool:
    return any("\u3400" <= char <= "\u9fff" for char in value)


def _mapping_value(value: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        candidate = _clean_text(value.get(key))
        if candidate:
            return candidate
    return ""


def _split_legacy_creator(value: str) -> dict[str, str] | None:
    text = _clean_text(value)
    if not text:
        return None
    if _contains_cjk(text) and "," not in text and not re.search(r"\s", text):
        # A bare CJK name is already the safest citation form.  Treating the
        # last character as a surname would silently corrupt the identity.
        return {"literal": text}
    if "," in text:
        family, given = (part.strip() for part in text.split(",", 1))
        if family:
            return {"family": family, **({"given": given} if given else {})}
    parts = text.split()
    if len(parts) == 1:
        return {"family": parts[0]}
    return {"family": parts[-1], "given": " ".join(parts[:-1])}


def normalize_creator(value: Any) -> dict[str, str] | None:
    """Normalize one Zotero/CSL/legacy creator into a small CSL-like object."""

    if isinstance(value, Mapping):
        literal = _mapping_value(value, "literal", "name")
        family = _mapping_value(value, "family", "lastName", "last_name")
        given = _mapping_value(value, "given", "firstName", "first_name")
        if literal and not family:
            return {"literal": literal}
        if family:
            result = {"family": family}
            if given:
                result["given"] = given
            return result
        if given:
            return {"literal": given}
        return None
    return _split_legacy_creator(_clean_text(value))


def normalize_creators(value: Any) -> list[dict[str, str]]:
    """Return canonical creators without splitting ``Last, First`` names."""

    if isinstance(value, Mapping):
        raw_values: list[Any] = [value]
    elif isinstance(value, (list, tuple)):
        raw_values = list(value)
    elif value in (None, ""):
        raw_values = []
    else:
        # Commas are meaningful inside a Last, First creator.  Report exports
        # use semicolons/newlines for multiple creators, so only those are
        # safe separators for a scalar value.
        raw_values = [part for part in re.split(r";|\r?\n", str(value)) if part.strip()]

    creators: list[dict[str, str]] = []
    for raw in raw_values:
        creator = normalize_creator(raw)
        if creator and creator not in creators:
            creators.append(creator)
    return creators


def creator_family(creator: Any) -> str:
    normalized = normalize_creator(creator)
    if not normalized:
        return "Anonymous"
    return normalized.get("literal") or normalized.get("family") or "Anonymous"


def creator_display_name(creator: Any) -> str:
    normalized = normalize_creator(creator)
    if not normalized:
        return ""
    literal = normalized.get("literal")
    if literal:
        return literal
    family = normalized.get("family", "")
    given = normalized.get("given", "")
    return f"{family}, {given}" if given else family


def _initials(given: str) -> str:
    given = _clean_text(given)
    if not given:
        return ""
    if _contains_cjk(given):
        return given
    parts = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", given)
    initials: list[str] = []
    for part in parts:
        initials.append(part[0].upper() + ".")
    return " ".join(initials)


def _citation_creator_name(
    creator: Mapping[str, str],
    *,
    include_initial: bool = False,
    include_full_name: bool = False,
) -> str:
    family = creator_family(creator)
    if include_full_name and not creator.get("literal"):
        given = _clean_text(creator.get("given", ""))
        if given:
            return f"{family}, {given}"
    if include_initial and not creator.get("literal"):
        initials = _initials(creator.get("given", ""))
        if initials:
            return f"{family}, {initials}"
    return family


def _reference_creator(creator: Mapping[str, str]) -> str:
    literal = _clean_text(creator.get("literal"))
    if literal:
        return literal
    family = _clean_text(creator.get("family"))
    given = _initials(creator.get("given", ""))
    if not family:
        return given
    return f"{family}, {given}" if given else family


def _record_value(record: Any, *keys: str) -> str:
    if isinstance(record, Mapping):
        for key in keys:
            value = _clean_text(record.get(key))
            if value:
                return value
        return ""
    for key in keys:
        value = _clean_text(getattr(record, key, ""))
        if value:
            return value
    return ""


def record_creators(record: Any) -> list[dict[str, str]]:
    raw_creators: Any
    if isinstance(record, Mapping):
        raw_creators = record.get("creators") or record.get("authors")
    else:
        raw_creators = getattr(record, "creators", None) or getattr(record, "authors", None)
    return normalize_creators(raw_creators)


def record_identifier(record: Any) -> str:
    return _record_value(record, "paper_id", "paper_key", "entry_id", "title")


def _year(record: Any, suffix: str = "") -> str:
    value = _record_value(record, "year", "date")
    match = re.search(r"\b(?:19|20)\d{2}\b", value)
    year = match.group(0) if match else "n.d."
    return year + (suffix if suffix and year != "n.d." else "")


def _title_key(record: Any) -> str:
    return _record_value(record, "title") or "untitled"


def _record_int(record: Any, key: str, default: int = 0) -> int:
    raw: Any
    if isinstance(record, Mapping):
        raw = record.get(key, default)
    else:
        raw = getattr(record, key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _record_bool(record: Any, key: str, default: bool = False) -> bool:
    raw: Any
    if isinstance(record, Mapping):
        raw = record.get(key, default)
    else:
        raw = getattr(record, key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().casefold() in {"1", "true", "yes", "on"}


class CitationStyleEngine:
    """Deterministic APA 7 renderer over CSL-like records."""

    name = "APA7"
    version = "apa7-csl-engine-v1"

    def __init__(self, style: str = "APA7") -> None:
        normalized = str(style or self.name).strip().upper().replace(" ", "")
        if normalized not in {"APA7", "APA7TH", "APA"}:
            raise ValueError(f"unsupported citation style: {style}")

    @staticmethod
    def _author_text(
        record: Any,
        *,
        narrative: bool = False,
        author_count: int | None = None,
        author_initials: bool | None = None,
    ) -> str:
        creators = record_creators(record)
        if not creators:
            return "Anonymous"
        include_initials = (
            _record_bool(record, "in_text_include_initials")
            if author_initials is None
            else bool(author_initials)
        )
        include_full_names = bool(
            record.get("in_text_include_full_names", False)
            if isinstance(record, Mapping)
            else getattr(record, "in_text_include_full_names", False)
        )
        families = [
            _citation_creator_name(
                creator,
                include_initial=include_initials,
                include_full_name=include_full_names,
            )
            for creator in creators
        ]
        requested_count = (
            _record_int(record, "in_text_author_count")
            if author_count is None
            else max(0, int(author_count))
        )
        if len(families) > 2 and requested_count > 0:
            visible = families[: min(requested_count, len(families))]
            if len(visible) < len(families):
                return ", ".join(visible) + ", et al."
            if len(visible) == 1:
                return visible[0]
            if len(visible) == 2:
                return f"{visible[0]} {'and' if narrative else '&'} {visible[1]}"
            return ", ".join(visible[:-1]) + f", {'and' if narrative else '&'} {visible[-1]}"
        if len(families) == 1:
            return families[0]
        if len(families) == 2:
            return f"{families[0]} {'and' if narrative else '&'} {families[1]}"
        return f"{families[0]} et al."

    def format_in_text(
        self,
        record: Any,
        *,
        mode: str = "parenthetical",
        locator: str | None = None,
        year_suffix: str = "",
        author_count: int | None = None,
        author_initials: bool | None = None,
    ) -> str:
        normalized_mode = str(mode or "parenthetical").strip().lower()
        if normalized_mode not in {"parenthetical", "narrative"}:
            raise ValueError(f"unsupported citation mode: {mode}")
        author = self._author_text(
            record,
            narrative=normalized_mode == "narrative",
            author_count=author_count,
            author_initials=author_initials,
        )
        year = _year(record, year_suffix)
        locator_text = f", {_clean_text(locator)}" if _clean_text(locator) else ""
        if normalized_mode == "narrative":
            return f"{author} ({year}{locator_text})"
        return f"({author}, {year}{locator_text})"

    @staticmethod
    def _reference_authors(record: Any) -> str:
        creators = record_creators(record)
        if not creators:
            return "Anonymous"
        names = [_reference_creator(creator) for creator in creators]
        if len(names) <= 20:
            if len(names) == 1:
                return names[0]
            return ", ".join(names[:-1]) + ", & " + names[-1]
        # APA 7 retains the first 19 names and the final name for 21+ authors.
        return ", ".join(names[:19]) + ", … " + names[-1]

    def format_reference(self, record: Any, *, year_suffix: str = "") -> FormattedReference:
        author = self._reference_authors(record)
        if not author.endswith("."):
            author += "."
        year = _year(record, year_suffix)
        title = _record_value(record, "title") or "Untitled"
        title = title.rstrip(" .") + "."
        container = _record_value(record, "container_title", "journal", "publication_title")
        volume = _record_value(record, "volume")
        issue = _record_value(record, "issue")
        pages = _record_value(record, "pages", "page")
        article_number = _record_value(record, "article_number", "article-number")
        publisher = _record_value(record, "publisher")
        doi = normalize_doi(_record_value(record, "doi"))
        url = _record_value(record, "url")

        segments: list[ReferenceSegment] = [
            ReferenceSegment(author),
            ReferenceSegment(f" ({year})."),
            ReferenceSegment(f" {title}"),
        ]
        if container:
            segments.append(ReferenceSegment(" "))
            segments.append(ReferenceSegment(container, italic=True))
            if volume:
                segments.append(ReferenceSegment(", "))
                segments.append(ReferenceSegment(volume, italic=True))
            if issue:
                segments.append(ReferenceSegment(f"({issue})"))
            page_value = pages or (f"Article {article_number}" if article_number else "")
            if page_value:
                segments.append(ReferenceSegment(f", {page_value}"))
            segments.append(ReferenceSegment("."))
        elif publisher:
            segments.append(ReferenceSegment(f" {publisher}."))

        locator = f"https://doi.org/{doi}" if doi else url
        if locator:
            segments.append(ReferenceSegment(f" {locator}"))
        return FormattedReference(tuple(segments))

    def disambiguation_suffixes(self, records: Iterable[Any]) -> dict[str, str]:
        groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for record in records:
            year = _year(record)
            if year == "n.d.":
                continue
            author_key = "|".join(
                creator_display_name(creator).casefold() for creator in record_creators(record)
            ) or "anonymous"
            groups[(author_key, year)].append(record)

        suffixes: dict[str, str] = {}
        for records_in_group in groups.values():
            if len(records_in_group) < 2:
                continue
            ordered = sorted(records_in_group, key=lambda item: (_title_key(item).casefold(), record_identifier(item)))
            for index, record in enumerate(ordered):
                suffixes[record_identifier(record)] = chr(ord("a") + index)
        return suffixes

    def disambiguation_author_counts(self, records: Iterable[Any]) -> dict[str, int]:
        """Return the smallest author prefix that makes short citations unique.

        APA-style ``First et al.`` labels collide when different multi-author
        papers share a first author and year. The document renderer stores this
        count on each entry so body citations remain resolvable after structured
        tokens are removed, while identical full author lists continue to use
        deterministic year suffixes.
        """

        materialized = list(records)
        groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for record in materialized:
            creators = record_creators(record)
            year = _year(record)
            if len(creators) <= 2 or year == "n.d.":
                continue
            first_family = creator_family(creators[0]).casefold()
            groups[(first_family, year)].append(record)

        counts: dict[str, int] = {}
        for group_records in groups.values():
            signatures = {
                record_identifier(item): tuple(
                    creator_display_name(creator).casefold()
                    for creator in record_creators(item)
                )
                for item in group_records
            }
            if len(set(signatures.values())) <= 1:
                continue
            for record in group_records:
                identifier = record_identifier(record)
                signature = signatures[identifier]
                required = 1
                for other_identifier, other_signature in signatures.items():
                    if other_identifier == identifier or other_signature == signature:
                        continue
                    common = 0
                    for left, right in zip(signature, other_signature):
                        if left != right:
                            break
                        common += 1
                    required = max(required, common + 1)
                counts[identifier] = min(max(2, required), len(signature))
        return counts

    def disambiguation_author_initials(self, records: Iterable[Any]) -> set[str]:
        """Return records whose initials are needed to distinguish body labels."""

        materialized = list(records)
        groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for record in materialized:
            creators = record_creators(record)
            year = _year(record)
            if len(creators) <= 1 or year == "n.d.":
                continue
            groups[(creator_family(creators[0]).casefold(), year)].append(record)

        result: set[str] = set()
        for group_records in groups.values():
            counts = self.disambiguation_author_counts(group_records)
            labels: dict[str, str] = {}
            for item in group_records:
                identifier = record_identifier(item)
                creators = record_creators(item)
                count = counts.get(identifier, 0)
                visible = creators[: min(count, len(creators))] if count > 0 else creators[:1]
                labels[identifier] = "|".join(
                    creator_family(value).casefold() for value in visible
                )
            by_label: dict[str, list[str]] = defaultdict(list)
            for identifier, label in labels.items():
                by_label[label].append(identifier)
            for identifiers in by_label.values():
                if len(identifiers) > 1:
                    result.update(identifiers)
        return result

    def disambiguation_author_full_names(self, records: Iterable[Any]) -> set[str]:
        """Return entries whose visible given names must be rendered in full.

        Initials are useful for the common ``Smith, J.`` collision, but they
        are not unique for names such as John and Jane.  When the selected
        family prefix still collides after initials, retain full given names
        in the in-text label so the citation remains resolvable.
        """

        materialized = list(records)
        groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for record in materialized:
            creators = record_creators(record)
            year = _year(record)
            if not creators or year == "n.d.":
                continue
            groups[(creator_family(creators[0]).casefold(), year)].append(record)

        full_names: set[str] = set()
        for group_records in groups.values():
            signatures = {
                record_identifier(item): tuple(
                    creator_display_name(creator).casefold()
                    for creator in record_creators(item)
                )
                for item in group_records
            }
            if len(set(signatures.values())) <= 1:
                continue
            counts = self.disambiguation_author_counts(group_records)
            labels: dict[str, str] = {}
            for item in group_records:
                identifier = record_identifier(item)
                creators = record_creators(item)
                count = counts.get(identifier, 0)
                visible = creators[: min(count, len(creators))] if count > 0 else creators[:1]
                labels[identifier] = "|".join(creator_family(value).casefold() for value in visible)
            by_label: dict[str, list[str]] = defaultdict(list)
            for identifier, label in labels.items():
                by_label[label].append(identifier)
            for identifiers in by_label.values():
                if len(identifiers) > 1:
                    full_names.update(identifiers)
        return full_names

    @staticmethod
    def sort_key(record: Any) -> tuple[str, str, str]:
        creators = record_creators(record)
        author_key = "|".join(creator_family(creator).casefold() for creator in creators) or "zzzz-anonymous"
        year_value = _year(record)
        return author_key, year_value.casefold(), _title_key(record).casefold()


__all__ = [
    "CitationStyleEngine",
    "FormattedReference",
    "ReferenceSegment",
    "creator_display_name",
    "creator_family",
    "normalize_creator",
    "normalize_creators",
    "record_creators",
]

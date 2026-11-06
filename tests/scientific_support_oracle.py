"""Small, controlled-PDF oracle for the Run First scientific fixture.

This is test-audit support, not a general literature validator.  It parses a
narrow fact-row grammar from actual PDF pages and checks authored per-paper
clauses against those rows.  Unsupported prose is reported as unjudgable;
source identity strings alone never count as evidence.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import fitz  # type: ignore[import-not-found]

EffectDirection = Literal["increase", "decrease", "null", "unreported"]
Judgment = Literal["supported", "unsupported", "unjudgable"]

_FACT_ROW = re.compile(
    r"FACT\s*\|\s*study\s*=\s*(?P<study>Study\s+[A-Z0-9]+)\s*"
    r"\|\s*n\s*=\s*(?P<sample>\d+)\s*"
    r"\|\s*direction\s*=\s*(?P<direction>increase|decrease|null)\s*"
    r"\|\s*magnitude\s*=\s*(?P<magnitude>none|\d+(?:\.\d+)?\s+points?)\s*"
    r"\|\s*condition\s*=\s*(?P<condition>.*?)\s*"
    r"\|\s*follow-up\s*=\s*(?P<days>\d+)\s+days\s*"
    r"\|\s*qualifiers\s*=\s*(?P<qualifiers>.*?)"
    r"(?=\s+FACT\s*\||$)",
    re.IGNORECASE | re.DOTALL,
)
_TITLE_LINE = re.compile(r"^\s*title\s*:\s*(.*?)\s*$", re.IGNORECASE)
_IDENTIFIER_LINE = re.compile(r"^\s*identifier\s*:\s*(.*?)\s*$", re.IGNORECASE)
_STUDY = re.compile(r"\bStudy\s+([A-Z0-9]+)\b", re.IGNORECASE)
_STUDY_SAMPLE = re.compile(r"\b(Study\s+[A-Z0-9]+)\s*\(\s*n\s*=\s*(\d+)\s*\)", re.IGNORECASE)
_SAMPLE = re.compile(r"\bn\s*=\s*(\d+)\b", re.IGNORECASE)
_MAGNITUDE = re.compile(r"(?<!\w)(\d+(?:\.\d+)?)\s*(?:points?|percentage\s+points?|个点)\b", re.IGNORECASE)
_FOLLOW_UP = re.compile(r"\b(\d+)\s*[- ]?days?\b", re.IGNORECASE)
_RESULT_EFFECT = re.compile(
    r"the treatment\s+(?P<verb>increased|reduced|produced no measurable change)\s+"
    r"(?:in\s+)?the primary outcome(?:\s+by\s+(?P<magnitude>\d+(?:\.\d+)?)\s+points?)?\s+"
    r"in the observed controlled sample\s*\(\s*n\s*=\s*(?P<sample>\d+)\s*\)",
    re.IGNORECASE,
)
_TABLE_STUDY_ESTIMATE = re.compile(
    r"\b(Study\s+[A-Z0-9]+)\s*\(\s*n\s*=\s*(\d+)\s*\)\s+source estimate\b",
    re.IGNORECASE,
)

_DIRECTION_PATTERNS: Mapping[EffectDirection, tuple[re.Pattern[str], ...]] = {
    "increase": (
        re.compile(r"\b(?:increased?|rose|risen|grew|higher)\b", re.IGNORECASE),
        re.compile(r"(?:提高|增加|上升)"),
    ),
    "decrease": (
        re.compile(r"\b(?:decreased?|reduced?|fell|declined?|lower)\b", re.IGNORECASE),
        re.compile(r"(?:下降|减少|降低)"),
    ),
    "null": (
        re.compile(
            r"\b(?:no measurable change|no measurable effect|no significant change|"
            r"no significant effect|no detectable difference|no change in the primary outcome|"
            r"did not change the primary outcome|unchanged)\b",
            re.IGNORECASE,
        ),
        re.compile(r"(?:没有可测量变化|未观察到变化|没有显著变化)"),
    ),
}


def _normalized(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    value = re.sub(r"[\W_]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _phrase_present(text: str, phrase: str) -> bool:
    wanted = _normalized(phrase)
    observed = f" {_normalized(text)} "
    return bool(wanted) and f" {wanted} " in observed


def _title_pattern(title: str) -> re.Pattern[str]:
    tokens = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", title), flags=re.UNICODE)
    if not tokens:
        return re.compile(r"(?!)")
    return re.compile(
        r"(?<!\w)" + r"[\W_]+".join(re.escape(token) for token in tokens) + r"(?!\w)",
        re.IGNORECASE,
    )


def _line_value(page_text: str, label: str) -> str:
    pattern = re.compile(rf"^\s*{re.escape(label)}\s*:\s*(.*?)\s*$", re.IGNORECASE)
    field_start = re.compile(
        r"^\s*(?:(?:Title|Identifier|Method|Results|Sample note|Limitations|Condition detail|"
        r"Table 2 \(page 2\)|Figure 3|Source discussion|DOI|中文原文|条件说明)\s*[:：]|FACT\s*\|)", re.IGNORECASE,
    )
    lines = page_text.splitlines()
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if match:
            parts = [match.group(1)]
            for continuation in lines[index + 1:]:
                if not continuation.strip() or field_start.match(continuation):
                    break
                parts.append(continuation)
            return " ".join(" ".join(parts).split())
    return ""


def _locator(
    *,
    paper_key: str,
    page_number: int,
    pdf_sha256: str,
    page_sha256s: Sequence[str],
    fact_sha256: str,
) -> SourceLocator:
    return SourceLocator(
        paper_key=paper_key,
        page_number=page_number,
        pdf_sha256=pdf_sha256,
        page_sha256=page_sha256s[page_number - 1],
        fact_sha256=fact_sha256,
    )


def _parse_natural_source(
    *,
    paper_key: str,
    pdf_sha256: str,
    page_texts: Sequence[str],
    page_sha256s: Sequence[str],
) -> tuple[str, str, tuple[StudySample, ...], tuple[SourceFact, ...], tuple[str, ...]]:
    """Parse the current PDF fixture's printed Title/Results/Table 2 grammar."""

    title = ""
    identifier = ""
    for page_text in page_texts:
        if not title:
            title = _line_value(page_text, "Title") or _line_value(page_text, "TITLE")
        if not identifier:
            identifier = _line_value(page_text, "Identifier")
    joined = "\n".join(page_texts)
    all_normalized = " ".join(joined.split())
    sample_note = next((_line_value(page, "Sample note") for page in page_texts if _line_value(page, "Sample note")), "")
    limitations = next((_line_value(page, "Limitations") for page in page_texts if _line_value(page, "Limitations")), "")
    method = next((_line_value(page, "Method") for page in page_texts if _line_value(page, "Method")), "")
    result_page_number = next(
        (index + 1 for index, page in enumerate(page_texts) if _line_value(page, "Results")),
        0,
    )
    results = _line_value(page_texts[result_page_number - 1], "Results") if result_page_number else ""
    table_page_number = next(
        (index + 1 for index, page in enumerate(page_texts) if _line_value(page, "Table 2 (page 2)")),
        0,
    )
    table_line = _line_value(page_texts[table_page_number - 1], "Table 2 (page 2)") if table_page_number else ""
    condition_detail = next(
        (_line_value(page, "Condition detail") for page in page_texts if _line_value(page, "Condition detail")),
        "",
    )
    study_conditions: dict[str, str] = {
        f"Study {match.group(1).upper()}": re.sub(
            r"^(?:a|an|the)\s+", "", " ".join(match.group(2).split()).strip(" .;,"), flags=re.IGNORECASE,
        )
        for match in re.finditer(r"\bStudy\s+([A-Z])\s+tested\s+([^.;]+)", joined, re.IGNORECASE)
    }
    sample_rows: dict[str | None, tuple[int, str]] = {}
    for match in _STUDY_SAMPLE.finditer(all_normalized):
        study = " ".join(match.group(1).split()).title()
        sample_rows[study] = (int(match.group(2)), study_conditions.get(study, ""))
    if not sample_rows:
        raw_sample = re.search(r"\b(?:one|a single) observed controlled sample\s*\(\s*n\s*=\s*(\d+)\s*\)", sample_note, re.IGNORECASE)
        if raw_sample:
            sample_rows[None] = (int(raw_sample.group(1)), "")
    if condition_detail and len(sample_rows) == 1:
        natural_condition = " ".join(condition_detail.split()).strip(" .;,")
        if "reference-price label" in natural_condition.casefold() and "no-promotion shelf label" in natural_condition.casefold():
            natural_condition = "reference-price label and no-promotion shelf label"
        only_key = next(iter(sample_rows))
        sample_rows[only_key] = (sample_rows[only_key][0], natural_condition)
    samples = tuple(
        StudySample(paper_key, study, count, condition)
        for study, (count, condition) in sorted(sample_rows.items(), key=lambda item: str(item[0]))
    )

    effect_match = _RESULT_EFFECT.search(results)
    if effect_match is None:
        return title, identifier, samples, (), ("natural_result_grammar_unresolved",)
    raw_verb = " ".join(effect_match.group("verb").casefold().split())
    direction: EffectDirection = (
        "increase" if raw_verb == "increased"
        else "decrease" if raw_verb == "reduced"
        else "null"
    )
    raw_magnitude = effect_match.group("magnitude")
    magnitude: int | float | None = None
    if raw_magnitude:
        amount = float(raw_magnitude)
        magnitude = int(amount) if amount.is_integer() else amount
    sample_size = int(effect_match.group("sample"))
    table_match = _TABLE_STUDY_ESTIMATE.search(table_line)
    study_label: str | None = None
    if table_match:
        table_study = " ".join(table_match.group(1).split()).title()
        if int(table_match.group(2)) == sample_size and table_study in sample_rows:
            study_label = table_study
    if len(sample_rows) > 1 and study_label is None:
        return title, identifier, samples, (), ("natural_effect_study_link_unresolved",)
    if study_label is None and sample_rows and None not in sample_rows:
        return title, identifier, samples, (), ("natural_effect_sample_link_unresolved",)
    sample_condition = next(
        (sample.condition for sample in samples if sample.study_label == study_label),
        condition_detail if len(samples) == 1 else "",
    )
    diagnostics: list[str] = []
    if not sample_condition:
        diagnostics.append("natural_condition_unresolved")
    days_match = _FOLLOW_UP.search(method or limitations or all_normalized)
    if days_match is None:
        return title, identifier, samples, (), ("natural_follow_up_unresolved",)
    follow_up_days = int(days_match.group(1))

    qualifiers: list[str] = []
    if "observed controlled sample" in _normalized(results + " " + limitations):
        qualifiers.append("@sample_scope")
    if ("longer term" in _normalized(limitations) or "longer term" in _normalized(joined)) and follow_up_days:
        qualifiers.append("@follow_up_boundary")
    separation_text = _normalized(sample_note + " " + joined)
    if len(samples) > 1 and any(token in separation_text for token in ("separately labeled", "separately conducted", "does not combine", "no common pooled")):
        qualifiers.append("@study_separation")
    if len(samples) > 1 and "pool" in separation_text:
        qualifiers.append("@no_pooling")
    if not qualifiers:
        diagnostics.append("natural_qualifier_grammar_unresolved")

    excerpt = " | ".join(value for value in (results, sample_note, limitations, condition_detail, table_line) if value)
    fact_hash = _sha256(excerpt.encode("utf-8"))
    source_pages = tuple(dict.fromkeys(value for value in (result_page_number, table_page_number) if value))
    locators = tuple(
        _locator(
            paper_key=paper_key,
            page_number=page_number,
            pdf_sha256=pdf_sha256,
            page_sha256s=page_sha256s,
            fact_sha256=fact_hash,
        )
        for page_number in source_pages
    )
    fact = SourceFact(
        paper_key=paper_key,
        study_label=study_label,
        sample_size=sample_size,
        direction=direction,
        magnitude_points=magnitude,
        condition=sample_condition,
        follow_up_days=follow_up_days,
        required_qualifiers=tuple(qualifiers),
        locator=next((row for row in locators if row.page_number == table_page_number), locators[0]),
        supporting_locators=locators,
        source_excerpt=excerpt,
    )
    if not sample_rows:
        diagnostics.append("natural_sample_roster_unresolved")
    if len(samples) > 1 and "@study_separation" not in qualifiers:
        diagnostics.append("natural_study_separation_unresolved")
    return title, identifier, samples, (fact,), tuple(diagnostics)


def _qualifier_present(text: str, qualifier: str, source: SourceDocument) -> bool:
    normalized = _normalized(text)
    if qualifier == "@sample_scope":
        return any(value in normalized for value in ("observed controlled sample", "observed sample only", "sample specific"))
    if qualifier == "@follow_up_boundary":
        return ("longer term" in normalized or "beyond 30 days" in normalized or "ends at day 30" in normalized)
    if qualifier == "@study_separation":
        study_labels = [sample.study_label for sample in source.study_samples if sample.study_label]
        sample_pairs = _study_sample_pairs(text)
        all_samples_stated = all(
            (sample.study_label, sample.sample_size) in sample_pairs
            for sample in source.study_samples if sample.study_label
        )
        separation = any(value in normalized for value in ("separate", "distinct", "not pooled", "no pooled", "no common pooled", "does not combine"))
        return len(study_labels) > 1 and all_samples_stated and separation
    if qualifier == "@no_pooling":
        return any(value in normalized for value in ("not pooled", "no pooled", "no common pooled", "does not combine"))
    return _phrase_present(text, qualifier)


def _condition_present(text: str, condition: str) -> bool:
    parts = [value.strip() for value in condition.split(" and ") if value.strip()]
    return bool(parts) and all(_phrase_present(text, value) for value in parts)


def _sample_statement_matches(
    source: SourceDocument,
    text: str,
    fact: SourceFact,
    sample_values: set[int | float],
    stated_pairs: set[tuple[str, int]],
) -> bool:
    if fact.sample_size not in sample_values:
        return False
    known_pairs = {
        (sample.study_label, sample.sample_size)
        for sample in source.study_samples
        if sample.study_label
    }
    for label, size in stated_pairs:
        if (label, size) not in known_pairs:
            return False
    other_values = sample_values - {fact.sample_size}
    paired_other_values = {
        size for label, size in stated_pairs
        if label != fact.study_label and (label, size) in known_pairs
    }
    if other_values != paired_other_values:
        return False
    if fact.study_label is not None:
        incorrect_primary_pairs = {
            size for label, size in stated_pairs if label == fact.study_label and size != fact.sample_size
        }
        if incorrect_primary_pairs:
            return False
    if len(source.study_samples) > 1 and len(sample_values) > 1:
        return all(
            (sample.study_label, sample.sample_size) in stated_pairs
            for sample in source.study_samples
            if sample.study_label
        )
    return True


@dataclass(frozen=True)
class SourceLocator:
    paper_key: str
    page_number: int
    pdf_sha256: str
    page_sha256: str
    fact_sha256: str


@dataclass(frozen=True)
class SourceFact:
    paper_key: str
    study_label: str | None
    sample_size: int
    direction: EffectDirection
    magnitude_points: int | float | None
    condition: str
    follow_up_days: int
    required_qualifiers: tuple[str, ...]
    locator: SourceLocator
    supporting_locators: tuple[SourceLocator, ...]
    source_excerpt: str


@dataclass(frozen=True)
class StudySample:
    paper_key: str
    study_label: str | None
    sample_size: int
    condition: str


@dataclass(frozen=True)
class SourceDocument:
    paper_key: str
    title: str
    title_aliases: tuple[str, ...]
    identifier: str
    pdf_path: str
    pdf_sha256: str
    page_sha256s: tuple[str, ...]
    page_texts: tuple[str, ...]
    study_samples: tuple[StudySample, ...]
    facts: tuple[SourceFact, ...]
    parse_diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class PaperClause:
    """The part of one authored assertion attributed to one source."""

    paper_key: str
    text: str
    page_number: int | None = None


@dataclass(frozen=True)
class AuthoredAssertion:
    text: str
    paper_clauses: tuple[PaperClause, ...]
    required_paper_keys: tuple[str, ...]
    adapter_issues: tuple[str, ...] = ()
    whole_assertion_shared: bool = False


@dataclass(frozen=True)
class SupportMetrics:
    support_sides_supported: int
    support_sides_submitted: int
    required_paper_sides_supported: int
    required_paper_sides: int
    locator_pages_verified: int
    locator_pages_required: int
    fact_obligations_met: int
    fact_obligations_required: int
    qualifier_obligations_met: int
    qualifier_obligations_required: int
    unjudgable_source_sides: int

    def to_dict(self) -> dict[str, int]:
        return {
            "support_sides_supported": self.support_sides_supported,
            "support_sides_submitted": self.support_sides_submitted,
            "required_paper_sides_supported": self.required_paper_sides_supported,
            "required_paper_sides": self.required_paper_sides,
            "locator_pages_verified": self.locator_pages_verified,
            "locator_pages_required": self.locator_pages_required,
            "fact_obligations_met": self.fact_obligations_met,
            "fact_obligations_required": self.fact_obligations_required,
            "qualifier_obligations_met": self.qualifier_obligations_met,
            "qualifier_obligations_required": self.qualifier_obligations_required,
            "unjudgable_source_sides": self.unjudgable_source_sides,
        }


@dataclass(frozen=True)
class SupportProof:
    paper_key: str
    study_label: str
    page_number: int
    pdf_sha256: str
    page_sha256: str
    fact_sha256: str
    source_excerpt: str
    supporting_page_numbers: tuple[int, ...] = ()


@dataclass(frozen=True)
class SupportVerdict:
    judgment: Judgment
    issue_codes: tuple[str, ...]
    metrics: SupportMetrics
    proofs: tuple[SupportProof, ...]


@dataclass(frozen=True)
class ControlledPdfCorpus:
    documents: Mapping[str, SourceDocument]

    @classmethod
    def from_pdfs(cls, pdfs: Mapping[str, str | Path]) -> ControlledPdfCorpus:
        """Extract source facts from the named PDF files, never from fixture metadata."""

        if not pdfs:
            raise ValueError("controlled PDF corpus requires at least one source")
        documents: dict[str, SourceDocument] = {}
        for raw_key, raw_path in sorted(pdfs.items(), key=lambda item: str(item[0])):
            paper_key = str(raw_key).strip()
            path = Path(raw_path).expanduser().resolve()
            if not paper_key or not path.is_file():
                raise ValueError(f"controlled PDF source is missing: {paper_key or path}")
            raw_bytes = path.read_bytes()
            pdf_hash = _sha256(raw_bytes)
            page_texts: list[str] = []
            page_hashes: list[str] = []
            parsed: list[tuple[int, str, re.Match[str]]] = []
            try:
                with fitz.open(path) as document:
                    for page_index, page in enumerate(document):
                        page_text = page.get_text("text")
                        if not page_text.strip():
                            raise ValueError(f"controlled PDF has an unreadable text page: {paper_key}:{page_index + 1}")
                        page_texts.append(page_text)
                        normalized_page = " ".join(page_text.split())
                        page_hashes.append(_sha256(normalized_page.encode("utf-8")))
                        for fact_match in _FACT_ROW.finditer(normalized_page):
                            parsed.append((page_index + 1, normalized_page, fact_match))
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError(f"controlled PDF source could not be parsed: {paper_key}: {exc}") from exc
            facts: list[SourceFact] = []
            parse_diagnostics: tuple[str, ...] = ()
            title = next((_line_value(page, "Title") or _line_value(page, "TITLE") for page in page_texts
                          if _line_value(page, "Title") or _line_value(page, "TITLE")), "")
            identifier = next((_line_value(page, "Identifier") for page in page_texts
                               if _line_value(page, "Identifier")), "")
            study_samples: tuple[StudySample, ...]
            if parsed:
                seen_studies: set[str] = set()
                for page_number, _page_text, match in parsed:
                    study = " ".join(match.group("study").split()).title()
                    if study in seen_studies:
                        raise ValueError(f"duplicate controlled study label in {paper_key}: {study}")
                    seen_studies.add(study)
                    raw_direction = match.group("direction").casefold()
                    direction: EffectDirection = raw_direction  # type: ignore[assignment]
                    raw_magnitude = match.group("magnitude").strip().casefold()
                    if direction == "null":
                        if raw_magnitude != "none":
                            raise ValueError(f"null source fact has a numeric magnitude: {paper_key}:{study}")
                        magnitude: int | float | None = None
                    else:
                        if raw_magnitude == "none":
                            raise ValueError(f"directional source fact lacks magnitude: {paper_key}:{study}")
                        amount_match = re.match(r"\d+(?:\.\d+)?", raw_magnitude)
                        if amount_match is None:
                            raise ValueError(f"source magnitude is invalid: {paper_key}:{study}")
                        amount = float(amount_match.group(0))
                        magnitude = int(amount) if amount.is_integer() else amount
                    excerpt = " ".join(match.group(0).split())
                    fact_hash = _sha256(excerpt.encode("utf-8"))
                    locator = _locator(
                        paper_key=paper_key,
                        page_number=page_number,
                        pdf_sha256=pdf_hash,
                        page_sha256s=page_hashes,
                        fact_sha256=fact_hash,
                    )
                    qualifiers = tuple(
                        " ".join(value.strip().split()).rstrip(" .;,")
                        for value in match.group("qualifiers").split(";")
                        if value.strip()
                    )
                    facts.append(SourceFact(
                        paper_key=paper_key,
                        study_label=study,
                        sample_size=int(match.group("sample")),
                        direction=direction,
                        magnitude_points=magnitude,
                        condition=" ".join(match.group("condition").split()).strip(" .;"),
                        follow_up_days=int(match.group("days")),
                        required_qualifiers=qualifiers,
                        locator=locator,
                        supporting_locators=(locator,),
                        source_excerpt=excerpt,
                    ))
                study_samples = tuple(
                    StudySample(paper_key, fact.study_label, fact.sample_size, fact.condition)
                    for fact in facts
                )
            else:
                title, identifier, study_samples, natural_facts, parse_diagnostics = _parse_natural_source(
                    paper_key=paper_key,
                    pdf_sha256=pdf_hash,
                    page_texts=page_texts,
                    page_sha256s=page_hashes,
                )
                facts.extend(natural_facts)
            if not title and identifier:
                title = identifier
            if not title:
                parse_diagnostics = tuple(dict.fromkeys((*parse_diagnostics, "source_title_unresolved")))
            aliases = tuple(dict.fromkeys(value for value in (title, identifier) if value))
            documents[paper_key] = SourceDocument(
                paper_key=paper_key,
                title=title,
                title_aliases=aliases,
                identifier=identifier,
                pdf_path=str(path),
                pdf_sha256=pdf_hash,
                page_sha256s=tuple(page_hashes),
                page_texts=tuple(page_texts),
                study_samples=study_samples,
                facts=tuple(facts),
                parse_diagnostics=parse_diagnostics,
            )
        return cls(documents=documents)

    def authored_assertion_from_source_titles(
        self,
        text: str,
        *,
        required_paper_keys: Sequence[str],
    ) -> AuthoredAssertion:
        """Split a comparison at titles extracted from the source PDFs.

        If a title is absent or repeated, the adapter refuses to infer paper
        sides from canonical IDs and records an unjudgable mapping issue.
        """

        required = tuple(str(value) for value in required_paper_keys if str(value))
        spans: list[tuple[int, int, str]] = []
        issues: list[str] = []
        for paper_key in required:
            source = self.documents.get(paper_key)
            if source is None:
                issues.append("required_source_not_in_pdf_corpus")
                continue
            candidate_matches = [
                match
                for alias in source.title_aliases
                for match in _title_pattern(alias).finditer(text)
            ]
            by_start: dict[int, re.Match[str]] = {}
            for match in candidate_matches:
                existing = by_start.get(match.start())
                if existing is None or (match.end() - match.start()) > (existing.end() - existing.start()):
                    by_start[match.start()] = match
            matches = sorted(by_start.values(), key=lambda match: match.start())
            non_overlapping: list[re.Match[str]] = []
            for match in matches:
                if non_overlapping and match.start() < non_overlapping[-1].end():
                    continue
                non_overlapping.append(match)
            matches = non_overlapping
            if len(matches) != 1:
                issues.append("unjudgable_source_title_anchor")
                continue
            spans.append((matches[0].start(), matches[0].end(), paper_key))
        spans.sort()
        # Explicit joint attribution gives the same whole assertion to every
        # named source. Each side is still evaluated independently against its
        # actual PDF; matching identities or equal wording never proves support.
        shared_marker = re.search(r"\beach report\s+", text, flags=re.IGNORECASE)
        if (not issues and len(spans) > 1 and shared_marker is not None
                and all(end <= shared_marker.start() for _start, end, _paper in spans)
                and not text[:spans[0][0]].strip()
                and not text[spans[-1][1]:shared_marker.start()].strip()
                and all(re.fullmatch(r"\s+and\s+", text[left[1]:right[0]], flags=re.IGNORECASE)
                        for left, right in zip(spans, spans[1:]))):
            shared_text = text[shared_marker.end():].strip()
            return AuthoredAssertion(
                text=str(text),
                paper_clauses=tuple(PaperClause(paper_key=paper, text=shared_text) for _start, _end, paper in spans),
                required_paper_keys=required,
                whole_assertion_shared=True,
            )
        clauses: list[PaperClause] = []
        for index, (start, _end, paper_key) in enumerate(spans):
            finish = spans[index + 1][0] if index + 1 < len(spans) else len(text)
            clause = text[start:finish].strip(" \t\r\n,;:.")
            clause = re.sub(r"\b(?:whereas|while|and then)\s*$", "", clause, flags=re.IGNORECASE).strip()
            if clause:
                clauses.append(PaperClause(paper_key=paper_key, text=clause))
            else:
                issues.append("unjudgable_empty_source_clause")
        return AuthoredAssertion(
            text=str(text),
            paper_clauses=tuple(clauses),
            required_paper_keys=required,
            adapter_issues=tuple(dict.fromkeys(issues)),
        )


def _direction_mentions(text: str) -> set[EffectDirection]:
    return {
        direction
        for direction, patterns in _DIRECTION_PATTERNS.items()
        if any(pattern.search(text) for pattern in patterns)
    }


def _number_values(pattern: re.Pattern[str], text: str) -> set[int | float]:
    result: set[int | float] = set()
    for match in pattern.finditer(text):
        value = float(match.group(1))
        result.add(int(value) if value.is_integer() else value)
    return result


def _study_sample_pairs(text: str) -> set[tuple[str, int]]:
    """Bind each printed sample count to the nearest named Study clause."""

    rows: set[tuple[str, int]] = set()
    study_mentions = list(_STUDY.finditer(text))
    for index, mention in enumerate(study_mentions):
        end = study_mentions[index + 1].start() if index + 1 < len(study_mentions) else len(text)
        segment = text[mention.end():end]
        sample = _SAMPLE.search(segment)
        if sample:
            rows.add((f"Study {mention.group(1).upper()}", int(sample.group(1))))
    return rows


def evaluate_assertion(corpus: ControlledPdfCorpus, assertion: AuthoredAssertion) -> SupportVerdict:
    """Audit per-paper clauses against parsed PDF facts and emit count metrics."""

    shared_keys = tuple(clause.paper_key for clause in assertion.paper_clauses)
    if (assertion.whole_assertion_shared and not assertion.adapter_issues
            and len(shared_keys) > 1 and len(set(shared_keys)) == len(shared_keys)
            and set(shared_keys) == set(assertion.required_paper_keys)
            and len(assertion.required_paper_keys) == len(shared_keys)
            and len({clause.text for clause in assertion.paper_clauses}) == 1):
        # Joint attribution maps the same complete prose to several sources;
        # ordinary per-source evaluation independently checks every obligation.
        sides = [evaluate_assertion(corpus, AuthoredAssertion(
            text=assertion.text, paper_clauses=(clause,), required_paper_keys=(clause.paper_key,),
        )) for clause in assertion.paper_clauses]
        counts = sides[0].metrics.to_dict()
        metrics = SupportMetrics(**{
            key: sum(side.metrics.to_dict()[key] for side in sides) for key in counts
        })
        judgment: Judgment = (
            "unsupported" if any(side.judgment == "unsupported" for side in sides)
            else "unjudgable" if any(side.judgment == "unjudgable" for side in sides)
            else "supported"
        )
        return SupportVerdict(
            judgment,
            tuple(dict.fromkeys(code for side in sides for code in side.issue_codes)),
            metrics, tuple(proof for side in sides for proof in side.proofs),
        )
    issues = list(assertion.adapter_issues)
    required = tuple(str(value) for value in assertion.required_paper_keys if str(value))
    required_set = set(required)
    support_keys = [str(row.paper_key) for row in assertion.paper_clauses]
    if len(required) != len(required_set):
        issues.append("duplicate_required_paper_key")
    if len(support_keys) != len(set(support_keys)):
        issues.append("duplicate_paper_clause")
    provided_set = set(support_keys)
    missing = required_set - provided_set
    extra = provided_set - required_set
    if missing:
        issues.append("missing_required_paper_side")
    if extra:
        issues.append("unexpected_paper_side")

    supported_sides: set[str] = set()
    proofs: list[SupportProof] = []
    locator_required = 0
    locator_verified = 0
    fact_total = fact_met = qualifier_total = qualifier_met = 0
    unjudgable_sides = 0
    has_unjudgable = "unjudgable_source_title_anchor" in issues or "unjudgable_empty_source_clause" in issues

    for clause in assertion.paper_clauses:
        paper_key = str(clause.paper_key)
        source = corpus.documents.get(paper_key)
        side_issues: list[str] = []
        if source is None:
            issues.append("support_source_not_in_pdf_corpus")
            locator_required += 1
            continue
        locator_required += 1
        if not _phrase_present(assertion.text, clause.text):
            side_issues.append("paper_clause_not_in_authored_assertion")
        for other_key, other_source in corpus.documents.items():
            if other_key != paper_key and any(
                _title_pattern(alias).search(clause.text) for alias in other_source.title_aliases
            ):
                side_issues.append("cross_paper_attribution")
        if len(required_set) > 1 and not any(
            _title_pattern(alias).search(clause.text) for alias in source.title_aliases
        ):
            side_issues.append("source_title_missing_from_paper_clause")

        study_text = clause.text
        for source_document in corpus.documents.values():
            for alias in source_document.title_aliases:
                study_text = _title_pattern(alias).sub(" ", study_text)
        study_mentions = [f"Study {match.group(1).upper()}" for match in _STUDY.finditer(study_text)]
        unique_studies = list(dict.fromkeys(study_mentions))
        known_studies = {sample.study_label for sample in source.study_samples if sample.study_label}
        unknown_studies = set(unique_studies) - known_studies
        if unknown_studies:
            side_issues.append("unsupported_study_label")
        if not unique_studies and any(fact.study_label for fact in source.facts):
            side_issues.append("study_label_omitted")
        if source.parse_diagnostics:
            side_issues.append("unjudgable_source_pdf_facts")
            has_unjudgable = True

        directions = _direction_mentions(clause.text)
        if not directions:
            side_issues.append("unjudgable_effect_grammar")
            has_unjudgable = True
        elif len(directions) > 1:
            side_issues.append("ambiguous_effect_direction")
            has_unjudgable = True
        sample_values = _number_values(_SAMPLE, clause.text)
        magnitude_values = _number_values(_MAGNITUDE, clause.text)
        follow_up_values = _number_values(_FOLLOW_UP, clause.text)
        study_sample_pairs = _study_sample_pairs(study_text)
        if unknown_studies:
            matching: list[SourceFact] = []
        elif unique_studies:
            matching = [fact for fact in source.facts if fact.study_label in set(unique_studies)]
        else:
            matching = [fact for fact in source.facts if fact.study_label is None]
        if len(matching) > 1 and len(directions) == 1:
            narrowed = [fact for fact in matching if fact.direction in directions]
            if magnitude_values:
                narrowed = [fact for fact in narrowed if fact.magnitude_points in magnitude_values]
            if len(narrowed) == 1:
                matching = narrowed
        if clause.page_number is not None:
            matching_on_page = [
                fact for fact in matching
                if clause.page_number in {row.page_number for row in (fact.supporting_locators or (fact.locator,))}
            ]
            if matching and not matching_on_page:
                side_issues.append("source_locator_page_mismatch")
            matching = matching_on_page
        fact = matching[0] if len(matching) == 1 else None
        if fact is None and not unknown_studies and not source.parse_diagnostics:
            if len(source.facts) > 1 and not unique_studies:
                side_issues.append("unjudgable_study_scope")
                has_unjudgable = True
            else:
                side_issues.append("source_fact_not_unique_at_locator")
        if fact is None:
            if not unique_studies or source.parse_diagnostics:
                has_unjudgable = True
            if has_unjudgable:
                unjudgable_sides += 1
            if unknown_studies:
                side_issues.append("unsupported_study_label")
            issues.extend(side_issues)
            continue

        fact_locators = fact.supporting_locators or (fact.locator,)
        locator_required += max(0, len(fact_locators) - 1)
        locator_verified += len(fact_locators)
        proofs.append(SupportProof(
            paper_key=paper_key,
            study_label=fact.study_label or "unlabeled_sample",
            page_number=fact.locator.page_number,
            pdf_sha256=fact.locator.pdf_sha256,
            page_sha256=fact.locator.page_sha256,
            fact_sha256=fact.locator.fact_sha256,
            source_excerpt=fact.source_excerpt,
            supporting_page_numbers=tuple(row.page_number for row in fact_locators),
        ))

        obligations: list[tuple[str, bool]] = [
            ("study", (not unique_studies and fact.study_label is None)
             or (fact.study_label is not None and fact.study_label in unique_studies)),
            ("direction", len(directions) == 1 and next(iter(directions)) == fact.direction),
            ("sample_size", _sample_statement_matches(source, clause.text, fact, sample_values, study_sample_pairs)),
            ("follow_up", follow_up_values == {fact.follow_up_days}),
        ]
        if fact.condition:
            obligations.append(("condition", _condition_present(clause.text, fact.condition)))
        else:
            side_issues.append("unjudgable_source_condition")
        if fact.magnitude_points is None:
            obligations.append(("magnitude", not magnitude_values))
            if magnitude_values:
                side_issues.append("magnitude_asserted_for_null_finding")
        else:
            obligations.append(("magnitude", magnitude_values == {fact.magnitude_points}))
        fact_total += len(obligations)
        fact_met += sum(1 for _name, passed in obligations if passed)
        if len(directions) == 1 and next(iter(directions)) != fact.direction:
            side_issues.append("effect_direction_mismatch")
        if fact.magnitude_points is not None and magnitude_values != {fact.magnitude_points}:
            side_issues.append("effect_magnitude_mismatch")
        if not _sample_statement_matches(source, clause.text, fact, sample_values, study_sample_pairs):
            side_issues.append("sample_size_mismatch")
        if fact.condition and not _condition_present(clause.text, fact.condition):
            side_issues.append("condition_omitted_or_mismatched")
        if follow_up_values != {fact.follow_up_days}:
            side_issues.append("follow_up_omitted_or_mismatched")

        qualifier_total += len(fact.required_qualifiers)
        present_qualifiers = sum(
            1 for qualifier in fact.required_qualifiers
            if _qualifier_present(clause.text, qualifier, source)
        )
        qualifier_met += present_qualifiers
        if present_qualifiers != len(fact.required_qualifiers):
            side_issues.append("required_qualifier_omitted")
        normalized_clause = _normalized(clause.text)
        if "pooled" in normalized_clause and not any(
            "pooled" in _normalized(qualifier) for qualifier in fact.required_qualifiers
        ) and "@no_pooling" not in fact.required_qualifiers:
            side_issues.append("unsupported_pooling_claim")
        if not side_issues:
            supported_sides.add(paper_key)
        if any(code.startswith("unjudgable_") for code in side_issues):
            unjudgable_sides += 1
        issues.extend(side_issues)

    submitted = len(assertion.paper_clauses)
    metrics = SupportMetrics(
        support_sides_supported=len(supported_sides),
        support_sides_submitted=submitted,
        required_paper_sides_supported=len(supported_sides & required_set),
        required_paper_sides=len(required_set),
        locator_pages_verified=locator_verified,
        locator_pages_required=locator_required,
        fact_obligations_met=fact_met,
        fact_obligations_required=fact_total,
        qualifier_obligations_met=qualifier_met,
        qualifier_obligations_required=qualifier_total,
        unjudgable_source_sides=unjudgable_sides,
    )
    issue_codes = tuple(dict.fromkeys(issues))
    if issue_codes and any(not code.startswith("unjudgable_") for code in issue_codes):
        judgment: Judgment = "unsupported"
    elif issue_codes or has_unjudgable:
        judgment = "unjudgable"
    else:
        judgment = "supported"
    if not issue_codes and missing:
        judgment = "unsupported"
    return SupportVerdict(judgment, issue_codes, metrics, tuple(proofs))


def _verdict_snapshot(verdict: SupportVerdict) -> dict[str, Any]:
    return {
        "judgment": verdict.judgment,
        "issue_codes": list(verdict.issue_codes),
        "metrics": verdict.metrics.to_dict(),
        "pdf_proofs": [
            {
                "paper_key": proof.paper_key,
                "study_label": proof.study_label,
                "page_number": proof.page_number,
                "supporting_page_numbers": list(proof.supporting_page_numbers),
                "pdf_sha256": proof.pdf_sha256,
                "page_sha256": proof.page_sha256,
                "fact_sha256": proof.fact_sha256,
            }
            for proof in verdict.proofs
        ],
    }


def audit_candidate_support(
    corpus: ControlledPdfCorpus,
    sections: Sequence[Mapping[str, Any]],
    *,
    required_paper_keys: Sequence[str],
) -> dict[str, Any]:
    """Audit source-bound candidate assertions for the required comparison."""

    required = {str(value) for value in required_paper_keys if str(value)}
    grouped: dict[str, set[str]] = {}
    for section in sections:
        for support in section.get("claim_support") or ():
            if not isinstance(support, Mapping):
                continue
            claim_text = str(support.get("claim") or "").strip()
            paper_key = str(support.get("paper_key") or "")
            if claim_text and paper_key:
                grouped.setdefault(claim_text, set()).add(paper_key)
    rows: list[dict[str, Any]] = []
    issues: list[str] = []
    for claim_text, paper_keys in sorted(grouped.items()):
        if paper_keys != required:
            continue
        assertion = corpus.authored_assertion_from_source_titles(
            claim_text,
            required_paper_keys=required_paper_keys,
        )
        verdict = evaluate_assertion(corpus, assertion)
        rows.append({
            "text_sha256": _sha256(claim_text.encode("utf-8")),
            "paper_keys": sorted(paper_keys),
            **_verdict_snapshot(verdict),
        })
        issues.extend(verdict.issue_codes)
        if verdict.judgment != "supported":
            issues.append("candidate_source_fact_support_failed")
    if not rows:
        issues.append("required_candidate_comparison_assertion_missing")
    unique_issues = list(dict.fromkeys(issues))
    return {
        "status": "supported" if rows and not unique_issues else "unsupported",
        "issue_codes": unique_issues,
        "required_paper_keys": list(required_paper_keys),
        "assertions": rows,
        "sides_supported": sum(int(row["metrics"]["required_paper_sides_supported"]) for row in rows),
        "sides_required": sum(int(row["metrics"]["required_paper_sides"]) for row in rows),
        "unjudgable_assertion_count": sum(row["judgment"] == "unjudgable" for row in rows),
    }


def audit_canonical_citation_manifest(
    corpus: ControlledPdfCorpus,
    manifest: Mapping[str, Any],
    *,
    required_paper_keys: Sequence[str],
) -> dict[str, Any]:
    """Audit Writer assertions against a caller-verified canonical manifest.

    The caller must first verify that ``manifest`` came from the READY current
    citation-manifest Registry record and matches the CurrentArtifactSet hash.
    """

    required = {str(value) for value in required_paper_keys if str(value)}
    clusters = [row for row in manifest.get("clusters") or () if isinstance(row, Mapping)]
    rows: list[dict[str, Any]] = []
    issues: list[str] = []
    for bundle in manifest.get("citation_sets") or ():
        if not isinstance(bundle, Mapping):
            continue
        bundle_papers = {str(value) for value in bundle.get("paper_keys") or () if str(value)}
        occurrence_ids = {str(value) for value in bundle.get("occurrence_ids") or () if str(value)}
        cluster_papers = {
            str(cluster.get("paper_key") or cluster.get("canonical_paper_key") or cluster.get("paper_id") or "")
            for cluster in clusters
            if occurrence_ids.intersection(
                str(value) for value in cluster.get("occurrence_ids") or () if str(value)
            )
        }
        cluster_papers.discard("")
        for raw_text in bundle.get("claim_texts") or ():
            text = str(raw_text or "").strip()
            if not text:
                continue
            assertion = corpus.authored_assertion_from_source_titles(
                text,
                required_paper_keys=required_paper_keys,
            )
            if assertion.adapter_issues:
                continue
            verdict = evaluate_assertion(corpus, assertion)
            set_matches = bundle_papers == required
            clusters_match = cluster_papers == required
            row_issues = list(verdict.issue_codes)
            if not set_matches:
                row_issues.append("writer_citation_set_does_not_cover_required_papers")
            if not clusters_match:
                row_issues.append("canonical_citation_clusters_do_not_cover_required_papers")
            if verdict.judgment != "supported":
                row_issues.append("writer_source_fact_support_failed")
            row_issues = list(dict.fromkeys(row_issues))
            issues.extend(row_issues)
            rows.append({
                "text_sha256": _sha256(text.encode("utf-8")),
                "citation_set_id": str(bundle.get("bundle_id") or ""),
                "citation_set_paper_keys": sorted(bundle_papers),
                "canonical_cluster_paper_keys": sorted(cluster_papers),
                "citation_set_matches_required_pair": set_matches,
                "clusters_match_required_pair": clusters_match,
                **_verdict_snapshot(verdict),
                "issue_codes": row_issues,
            })
    if not rows:
        issues.append("required_writer_comparison_assertion_missing")
    unique_issues = list(dict.fromkeys(issues))
    return {
        "status": "supported" if rows and not unique_issues else "unsupported",
        "issue_codes": unique_issues,
        "required_paper_keys": list(required_paper_keys),
        "assertions": rows,
        "sides_supported": sum(int(row["metrics"]["required_paper_sides_supported"]) for row in rows),
        "sides_required": sum(int(row["metrics"]["required_paper_sides"]) for row in rows),
        "unjudgable_assertion_count": sum(row["judgment"] == "unjudgable" for row in rows),
    }


def _candidate_claim_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("text") or value.get("claim") or "").strip()
    return str(value or "").strip()


def _required_comparison_audit(
    requirements: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    *,
    row_id_field: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, raw in enumerate(requirements):
        question = str(raw.get("question") or "").strip()
        raw_keys = raw.get("paper_keys")
        paper_keys = (
            [str(value).strip() for value in raw_keys if str(value).strip()]
            if isinstance(raw_keys, Sequence) and not isinstance(raw_keys, (str, bytes))
            else []
        )
        normalized = sorted(set(paper_keys))
        issues: list[str] = []
        if len(paper_keys) != 2 or len(normalized) != 2:
            issues.append("required_comparison_requires_two_distinct_paper_keys")
        if not question:
            issues.append("required_comparison_question_missing")
        matching = [
            row for row in rows
            if normalized and sorted({str(value) for value in row.get("paper_keys") or () if str(value)}) == normalized
        ]
        supported_ids = [
            str(row.get(row_id_field) or "") for row in matching
            if row.get("judgment") == "supported" and str(row.get(row_id_field) or "")
        ]
        if issues:
            status = "unsupported"
        elif supported_ids:
            status = "supported"
        elif any(row.get("judgment") == "unjudgable" for row in matching):
            status = "unjudgable"
            issues.append("required_comparison_source_proof_unjudgable")
        else:
            status = "unsupported"
            issues.append(
                "required_comparison_assertion_missing"
                if not matching
                else "required_comparison_assertion_not_supported"
            )
        result = {
            "comparison_index": index,
            "paper_keys": normalized,
            "question": question,
            "status": status,
            "matching_assertion_ids": [
                str(row.get(row_id_field) or "") for row in matching
                if str(row.get(row_id_field) or "")
            ],
            "supported_assertion_ids": supported_ids,
            "issue_codes": list(dict.fromkeys(issues)),
        }
        if raw.get("relation_type"):
            result["relation_type"] = str(raw["relation_type"])
        results.append(result)
    return results


def _judgment_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        judgment: sum(row.get("judgment") == judgment for row in rows)
        for judgment in ("supported", "unsupported", "unjudgable")
    }


def _aggregate_judgment(rows: Sequence[Mapping[str, Any]], *, has_structural_issues: bool = False) -> str:
    counts = _judgment_counts(rows)
    if counts["unsupported"] or has_structural_issues:
        return "unsupported"
    if counts["unjudgable"] or not rows:
        return "unjudgable"
    return "supported"


def audit_all_selected_candidate_support(
    corpus: ControlledPdfCorpus,
    sections: Sequence[Mapping[str, Any]],
    *,
    required_comparisons: Sequence[Mapping[str, Any]] = (),
    single_source_bindings: Any = None,
) -> dict[str, Any]:
    """Audit every authored candidate claim occurrence against its support rows.

    Claim text is never globally deduplicated. The exact section and claim
    occurrence are retained, and every same-section support row for that exact
    text is copied into the result. Source titles are read from the PDFs via
    ``corpus``; support keys and relation IDs are not title anchors. A separately
    verified READY Registry proof may bind an entire single-source claim to its
    source; that path still evaluates the exact text against the actual PDF.
    """

    if single_source_bindings is not None:
        from tests.run_first_candidate_oracle_binding import VerifiedSingleSourceCandidateBindings

        if (not isinstance(single_source_bindings, VerifiedSingleSourceCandidateBindings)
                or not single_source_bindings.is_verified):
            raise ValueError("single-source candidate audit requires a verified READY Registry binding")

    rows: list[dict[str, Any]] = []
    issues: list[str] = []
    orphan_rows: list[dict[str, Any]] = []
    malformed_sections: list[dict[str, Any]] = []
    for section_index, section in enumerate(sections):
        if not isinstance(section, Mapping):
            malformed_sections.append({"section_index": section_index, "issue": "candidate_section_not_object"})
            issues.append("candidate_section_not_object")
            continue
        section_id = str(section.get("section_id") or f"section-index-{section_index}")
        raw_claims = section.get("claims") or ()
        raw_support = section.get("claim_support") or ()
        if not isinstance(raw_claims, Sequence) or isinstance(raw_claims, (str, bytes)):
            malformed_sections.append({"section_index": section_index, "section_id": section_id,
                                       "issue": "candidate_claims_not_array"})
            issues.append("candidate_claims_not_array")
            raw_claims = ()
        if not isinstance(raw_support, Sequence) or isinstance(raw_support, (str, bytes)):
            malformed_sections.append({"section_index": section_index, "section_id": section_id,
                                       "issue": "candidate_claim_support_not_array"})
            issues.append("candidate_claim_support_not_array")
            raw_support = ()

        support_by_claim: dict[str, list[Mapping[str, Any]]] = {}
        malformed_support: list[dict[str, Any]] = []
        for support_index, support in enumerate(raw_support):
            if not isinstance(support, Mapping):
                malformed_support.append({"support_index": support_index, "issue": "candidate_support_row_not_object"})
                issues.append("candidate_support_row_not_object")
                continue
            support_text = str(support.get("claim") or "").strip()
            if not support_text:
                malformed_support.append({"support_index": support_index, "issue": "candidate_support_claim_missing"})
                issues.append("candidate_support_claim_missing")
                continue
            support_by_claim.setdefault(support_text, []).append(support)

        claim_texts = [_candidate_claim_text(item) for item in raw_claims]
        declared_texts = {text for text in claim_texts if text}
        for support_text, support_rows in support_by_claim.items():
            if support_text not in declared_texts:
                for support in support_rows:
                    orphan_rows.append({
                        "section_index": section_index,
                        "section_id": section_id,
                        "claim_text_sha256": _sha256(support_text.encode("utf-8")),
                        "support_row": dict(support),
                    })
                issues.append("orphan_candidate_support_row")

        for claim_index, raw_claim in enumerate(raw_claims):
            claim_text = _candidate_claim_text(raw_claim)
            authored_claim_id = (
                str(raw_claim.get("claim_id") or raw_claim.get("claim_group_id") or "").strip()
                if isinstance(raw_claim, Mapping)
                else ""
            )
            occurrence_id = f"section-index-{section_index}:claim:{claim_index}"
            claim_support_rows = list(support_by_claim.get(claim_text, ())) if claim_text else []
            row_issues: list[str] = []
            mapping_basis = "unresolved"
            paper_keys = sorted({
                str(item.get("paper_key") or "").strip()
                for item in claim_support_rows
                if str(item.get("paper_key") or "").strip()
            })
            if not claim_text:
                row_issues.append("candidate_claim_text_missing")
                judgment: Judgment = "unjudgable"
                verdict: SupportVerdict | None = None
            elif not claim_support_rows:
                row_issues.append("candidate_claim_support_missing")
                judgment = "unsupported"
                verdict = None
            elif not paper_keys:
                row_issues.append("candidate_support_paper_key_missing")
                judgment = "unjudgable"
                verdict = None
            else:
                missing_closure = False
                for support in claim_support_rows:
                    for field_name in ("source_claim_ids", "evidence_ids", "source_field_ids"):
                        values = support.get(field_name)
                        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or not values:
                            missing_closure = True
                            row_issues.append(f"candidate_support_{field_name}_missing")
                bound_source = (
                    single_source_bindings.matches(section_index, claim_index, claim_text, claim_support_rows)
                    if single_source_bindings is not None and len(paper_keys) == 1 and not missing_closure
                    else None
                )
                shared_sources = (
                    single_source_bindings.matches_shared(section_index, claim_index, claim_text, claim_support_rows)
                    if single_source_bindings is not None and len(paper_keys) > 1 and not missing_closure
                    else None
                )
                if bound_source == paper_keys[0] and len(paper_keys) == 1:
                    # The exact adopted scope, bank text and complete support
                    # rows establish one source. The scientific evaluator still
                    # reads that source's actual PDF facts/conditions/locators.
                    assertion = AuthoredAssertion(
                        text=claim_text, paper_clauses=(PaperClause(paper_key=bound_source, text=claim_text),),
                        required_paper_keys=tuple(paper_keys),
                    )
                    mapping_basis = "ready_registry_single_source_scope"
                elif shared_sources is not None and set(shared_sources) == set(paper_keys):
                    # Independent complete single-paper bank groups bind the
                    # same exact assertion. Check that entire assertion against
                    # each actual PDF, retaining all adverse/uncertain verdicts.
                    assertion = AuthoredAssertion(
                        text=claim_text,
                        paper_clauses=tuple(PaperClause(paper_key=paper, text=claim_text) for paper in shared_sources),
                        required_paper_keys=tuple(paper_keys),
                        whole_assertion_shared=True,
                    )
                    mapping_basis = "ready_registry_shared_whole_assertion_scope"
                else:
                    assertion = corpus.authored_assertion_from_source_titles(
                        claim_text, required_paper_keys=paper_keys,
                    )
                    mapping_basis = "pdf_title_anchors"
                verdict = evaluate_assertion(corpus, assertion)
                row_issues.extend(verdict.issue_codes)
                if missing_closure:
                    judgment = "unsupported"
                    row_issues.append("candidate_support_closure_incomplete")
                elif assertion.adapter_issues:
                    judgment = "unjudgable"
                else:
                    judgment = verdict.judgment

            row_id = occurrence_id
            result_row = {
                **(_verdict_snapshot(verdict) if verdict is not None else {}),
                "assertion_id": row_id,
                "section_id": section_id,
                "section_index": section_index,
                "claim_index": claim_index,
                "claim_occurrence_id": occurrence_id,
                "authored_claim_id": authored_claim_id,
                "claim_text_sha256": _sha256(claim_text.encode("utf-8")),
                "source_mapping_basis": mapping_basis,
                "paper_keys": paper_keys,
                "support_row_count": len(claim_support_rows),
                "support_rows": [dict(item) for item in claim_support_rows],
                "judgment": judgment,
                "issue_codes": list(dict.fromkeys(row_issues)),
            }
            rows.append(result_row)

        issues.extend(
            str(row.get("issue") or "candidate_support_structure_invalid")
            for row in malformed_support
        )

    issue_codes = list(dict.fromkeys(issues))
    counts = _judgment_counts(rows)
    status = _aggregate_judgment(rows, has_structural_issues=bool(issue_codes))
    required_rows = _required_comparison_audit(
        required_comparisons,
        rows,
        row_id_field="assertion_id",
    )
    return {
        "schema_version": "controlled-pdf-all-selected-candidate-support/v1",
        "status": status,
        "issue_codes": issue_codes,
        "assertion_counts": counts,
        "assertions": rows,
        "orphan_support_rows": orphan_rows,
        "malformed_sections": malformed_sections,
        "required_comparisons": required_rows,
        "required_comparisons_status": (
            "supported"
            if required_rows and all(item["status"] == "supported" for item in required_rows)
            else "unsupported"
        ),
    }


_CITATION_REF_TOKEN = re.compile(r"\bR\d{3,}\b")
_CITATION_TOKEN = re.compile(r"\[\[cite_ref:R\d{3,}(?:,\s*R\d{3,})*\]\]")


def _source_key_aliases(row: Mapping[str, Any]) -> tuple[str, ...]:
    canonical = str(row.get("canonical_paper_key") or "").strip()
    paper_key = str(row.get("paper_key") or "").strip()
    if canonical and paper_key and canonical != paper_key:
        return tuple(sorted({canonical, paper_key}))
    if canonical or paper_key:
        return (canonical or paper_key,)
    paper_id = str(row.get("paper_id") or "").strip()
    return (paper_id,) if paper_id else ()


def _ids_from_manifest_field(value: Any) -> tuple[list[str], str | None]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return [], "manifest_id_array_invalid"
    values = [str(item).strip() for item in value if str(item).strip()]
    if len(values) != len(value):
        return values, "manifest_id_array_contains_empty_value"
    if len(values) != len(set(values)):
        return values, "manifest_id_array_contains_duplicate"
    return values, None


def _record_index(rows: Any, identity_key: str) -> tuple[dict[str, list[Mapping[str, Any]]], list[str]]:
    index: dict[str, list[Mapping[str, Any]]] = {}
    issues: list[str] = []
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return index, [f"manifest_{identity_key}_records_invalid"]
    for row in rows:
        if not isinstance(row, Mapping):
            issues.append(f"manifest_{identity_key}_record_not_object")
            continue
        identity = str(row.get(identity_key) or "").strip()
        if not identity:
            issues.append(f"manifest_{identity_key}_record_identity_missing")
            continue
        index.setdefault(identity, []).append(row)
    if any(len(values) != 1 for values in index.values()):
        issues.append(f"manifest_{identity_key}_record_identity_duplicated")
    return index, issues


def _source_title_spans(
    corpus: ControlledPdfCorpus,
    text: str,
    paper_keys: Sequence[str],
) -> tuple[dict[str, tuple[int, int]], list[str]]:
    spans: dict[str, tuple[int, int]] = {}
    issues: list[str] = []
    for paper_key in paper_keys:
        source = corpus.documents.get(paper_key)
        if source is None:
            issues.append("support_source_not_in_pdf_corpus")
            continue
        candidate_matches = [
            match
            for alias in source.title_aliases
            for match in _title_pattern(alias).finditer(text)
        ]
        by_start: dict[int, re.Match[str]] = {}
        for match in candidate_matches:
            previous = by_start.get(match.start())
            if previous is None or match.end() - match.start() > previous.end() - previous.start():
                by_start[match.start()] = match
        matches: list[re.Match[str]] = []
        for match in sorted(by_start.values(), key=lambda item: item.start()):
            if matches and match.start() < matches[-1].end():
                continue
            matches.append(match)
        if len(matches) != 1:
            issues.append("unjudgable_source_title_anchor")
            continue
        spans[paper_key] = (matches[0].start(), matches[0].end())
    return spans, list(dict.fromkeys(issues))


def _normalized_manifest_claim_text(value: str) -> str:
    without_tokens = _CITATION_TOKEN.sub("", value)
    collapsed = re.sub(r"\s+", " ", without_tokens).strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", collapsed)


def _writer_unit_mapping_issues(
    unit: Mapping[str, Any],
    bundle: Mapping[str, Any],
    *,
    occurrence_index: Mapping[str, Sequence[Mapping[str, Any]]],
    clusters_by_occurrence: Mapping[str, Sequence[tuple[Mapping[str, Any], str]]],
    corpus: ControlledPdfCorpus,
) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    raw_paper_keys, paper_issue = _ids_from_manifest_field(unit.get("supporting_paper_keys"))
    raw_occurrence_ids, occurrence_issue = _ids_from_manifest_field(unit.get("supporting_occurrence_ids"))
    if paper_issue:
        issues.append(
            "writer_unit_supporting_paper_keys_missing"
            if paper_issue == "manifest_id_array_invalid" or not raw_paper_keys
            else "writer_unit_supporting_paper_keys_invalid"
        )
    if not raw_paper_keys:
        issues.append("writer_unit_supporting_paper_keys_missing")
    if occurrence_issue:
        issues.append("writer_unit_supporting_occurrence_ids_invalid")
    if not raw_occurrence_ids:
        issues.append("writer_unit_supporting_occurrence_ids_missing")
    declared_keys = sorted(set(raw_paper_keys))
    declared_occurrences = sorted(set(raw_occurrence_ids))
    alignment_status = str(unit.get("alignment_status") or "").strip().casefold()
    if alignment_status == "ambiguous":
        issues.append("writer_unit_alignment_ambiguous")
    elif alignment_status not in {"explicit", "inferred"}:
        issues.append("writer_unit_alignment_status_unverified")

    bundle_keys, bundle_key_issue = _ids_from_manifest_field(bundle.get("paper_keys"))
    bundle_occurrences, bundle_occ_issue = _ids_from_manifest_field(bundle.get("occurrence_ids"))
    if bundle_key_issue or not bundle_keys:
        issues.append("writer_citation_set_paper_keys_invalid")
    if bundle_occ_issue or not bundle_occurrences:
        issues.append("writer_citation_set_occurrence_ids_invalid")
    if not set(declared_keys).issubset(set(bundle_keys)):
        issues.append("writer_unit_paper_scope_outside_citation_set")
    if not set(declared_occurrences).issubset(set(bundle_occurrences)):
        issues.append("writer_unit_occurrence_not_in_citation_set")

    resolved_occurrence_keys: set[str] = set()
    resolved_cluster_keys: set[str] = set()
    unit_block_id = str(unit.get("block_id") or "").strip()
    raw_text = str(unit.get("raw_text") or "")
    claim_text = str(unit.get("claim_text") or "").strip()
    span_start, span_end = unit.get("span_start"), unit.get("span_end")
    if not unit_block_id or type(span_start) is not int or type(span_end) is not int or span_start < 0 or span_end <= span_start:
        issues.append("writer_unit_span_binding_missing_or_invalid")
    if not raw_text or not claim_text:
        issues.append("writer_unit_claim_text_missing")
    elif _normalized_manifest_claim_text(raw_text) != _normalized_manifest_claim_text(claim_text):
        issues.append("writer_unit_claim_text_stale_vs_raw_text")
    elif type(span_start) is int and type(span_end) is int and len(raw_text) != span_end - span_start:
        issues.append("writer_unit_raw_text_span_length_mismatch")

    citation_tokens, token_issue = _ids_from_manifest_field(unit.get("citation_tokens"))
    if token_issue or not citation_tokens:
        issues.append("writer_unit_citation_tokens_missing_or_invalid")
    token_refs: dict[str, set[str]] = {
        token: set(_CITATION_REF_TOKEN.findall(token)) for token in citation_tokens
    }
    if len(declared_keys) > 1 and any(len(refs) > 1 for refs in token_refs.values()):
        issues.append("joint_multisource_citation_token")

    occurrence_rows: list[Mapping[str, Any]] = []
    for occurrence_id in declared_occurrences:
        matches = occurrence_index.get(occurrence_id, ())
        if len(matches) != 1:
            issues.append("writer_unit_occurrence_id_unresolved")
            continue
        occurrence = matches[0]
        occurrence_rows.append(occurrence)
        aliases = _source_key_aliases(occurrence)
        if len(aliases) != 1:
            issues.append("writer_occurrence_source_key_missing_or_conflicted")
            continue
        occurrence_key = aliases[0]
        resolved_occurrence_keys.add(occurrence_key)
        if occurrence_key not in corpus.documents:
            issues.append("writer_occurrence_source_not_in_pdf_corpus")
        if occurrence_key not in declared_keys:
            issues.append("writer_unit_occurrence_source_mismatch")
        if unit_block_id and str(occurrence.get("block_id") or "").strip() != unit_block_id:
            issues.append("writer_unit_occurrence_block_mismatch")
        occurrence_token = str(occurrence.get("citation_token") or "").strip()
        occurrence_ref = str(occurrence.get("ref_id") or "").strip()
        if not occurrence_token or occurrence_token not in citation_tokens:
            issues.append("writer_unit_occurrence_citation_token_mismatch")
        all_unit_refs = set().union(*token_refs.values()) if token_refs else set()
        if not occurrence_ref or occurrence_ref not in all_unit_refs:
            issues.append("writer_unit_occurrence_ref_id_mismatch")
        occurrence_clusters = clusters_by_occurrence.get(occurrence_id, ())
        if not occurrence_clusters:
            issues.append("writer_unit_occurrence_cluster_missing")
        for cluster, cluster_key in occurrence_clusters:
            if cluster_key:
                resolved_cluster_keys.add(cluster_key)
                if cluster_key != occurrence_key:
                    issues.append("writer_unit_cluster_source_mismatch")
            if len(occurrence_clusters) != 1:
                issues.append("writer_unit_occurrence_cluster_ambiguous")
    if resolved_occurrence_keys != set(declared_keys):
        issues.append("writer_unit_paper_keys_do_not_equal_occurrence_sources")
    if resolved_cluster_keys != set(declared_keys):
        issues.append("writer_unit_paper_keys_do_not_equal_cluster_sources")

    if raw_text and type(span_start) is int and type(span_end) is int:
        title_spans, title_issues = _source_title_spans(corpus, raw_text, declared_keys)
        issues.extend(title_issues)
        ordered_titles = sorted(
            ((paper_key, span[0], span[1]) for paper_key, span in title_spans.items()),
            key=lambda item: item[1],
        )
        for occurrence_id, occurrence in zip(declared_occurrences, occurrence_rows):
            aliases = _source_key_aliases(occurrence)
            if len(aliases) != 1 or aliases[0] not in title_spans:
                continue
            paper_key = aliases[0]
            title_start, _title_end = title_spans[paper_key]
            later_titles = [start for key, start, _end in ordered_titles if start > title_start]
            clause_end = min(later_titles) if later_titles else len(raw_text)
            spans = occurrence.get("spans")
            if not isinstance(spans, Sequence) or isinstance(spans, (str, bytes)) or not spans:
                issues.append("writer_unit_occurrence_spans_missing")
                continue
            for span in spans:
                if not isinstance(span, Mapping):
                    issues.append("writer_unit_occurrence_span_invalid")
                    continue
                start, end = span.get("start_offset"), span.get("end_offset")
                if type(start) is not int or type(end) is not int or not (span_start <= start < end <= span_end):
                    issues.append("writer_unit_occurrence_span_outside_claim")
                    continue
                relative_start, relative_end = start - span_start, end - span_start
                if relative_start < title_start or relative_end > clause_end:
                    issues.append("writer_unit_citation_not_local_to_source_clause")

    table_bound = any(str(unit.get(field) or "").strip() for field in ("table_id", "row_id", "cell_id"))
    if table_bound:
        issues.append("writer_table_unit_outside_paragraph_source_oracle")
    return list(dict.fromkeys(issues)), declared_keys


def audit_all_canonical_writer_claim_units(
    corpus: ControlledPdfCorpus,
    manifest: Mapping[str, Any],
    *,
    required_comparisons: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Audit every canonical Writer claim unit using only unit-level source links.

    The caller must pass the payload read from the READY citation-manifest
    Registry record that matches CurrentArtifactSet. Bundle paper unions never
    fill missing unit support keys or occurrence IDs. Table-bound units are
    retained and reported as unjudgable by this paragraph-scoped PDF oracle.
    """

    occurrence_index, occurrence_index_issues = _record_index(manifest.get("occurrences"), "occurrence_id")
    cluster_rows = manifest.get("clusters")
    clusters_by_occurrence: dict[str, list[tuple[Mapping[str, Any], str]]] = {}
    structure_issues = list(occurrence_index_issues)
    if not isinstance(cluster_rows, Sequence) or isinstance(cluster_rows, (str, bytes)):
        cluster_rows = ()
        structure_issues.append("manifest_cluster_records_invalid")
    for cluster in cluster_rows:
        if not isinstance(cluster, Mapping):
            structure_issues.append("manifest_cluster_record_not_object")
            continue
        cluster_key_aliases = _source_key_aliases(cluster)
        cluster_key = cluster_key_aliases[0] if len(cluster_key_aliases) == 1 else ""
        if len(cluster_key_aliases) != 1:
            structure_issues.append("manifest_cluster_source_key_missing_or_conflicted")
        occurrence_ids, occurrence_issue = _ids_from_manifest_field(cluster.get("occurrence_ids"))
        if occurrence_issue:
            structure_issues.append("manifest_cluster_occurrence_ids_invalid")
        for occurrence_id in occurrence_ids:
            clusters_by_occurrence.setdefault(occurrence_id, []).append((cluster, cluster_key))

    raw_bundles = manifest.get("citation_sets")
    if not isinstance(raw_bundles, Sequence) or isinstance(raw_bundles, (str, bytes)):
        raw_bundles = ()
        structure_issues.append("manifest_citation_sets_invalid")
    rows: list[dict[str, Any]] = []
    unit_ids: set[str] = set()
    for bundle_index, bundle in enumerate(raw_bundles):
        if not isinstance(bundle, Mapping):
            structure_issues.append("manifest_citation_set_not_object")
            continue
        bundle_id = str(bundle.get("bundle_id") or f"bundle-index-{bundle_index}")
        raw_units = bundle.get("claim_units")
        if not isinstance(raw_units, Sequence) or isinstance(raw_units, (str, bytes)):
            raw_units = ()
            structure_issues.append("manifest_claim_units_invalid")
        if not raw_units and bundle.get("claim_texts"):
            for legacy_index, _claim_text in enumerate(bundle.get("claim_texts") or ()):
                rows.append({
                    "claim_unit_id": f"{bundle_id}:missing-unit:{legacy_index}",
                    "citation_set_id": bundle_id,
                    "paper_keys": [],
                    "judgment": "unjudgable",
                    "issue_codes": ["writer_claim_text_without_claim_unit"],
                    "source_row_count": 0,
                })
        for unit_index, unit in enumerate(raw_units):
            if not isinstance(unit, Mapping):
                rows.append({
                    "claim_unit_id": f"{bundle_id}:malformed-unit:{unit_index}",
                    "citation_set_id": bundle_id,
                    "paper_keys": [],
                    "judgment": "unjudgable",
                    "issue_codes": ["writer_claim_unit_not_object"],
                    "source_row_count": 0,
                })
                continue
            unit_id = str(unit.get("claim_unit_id") or f"{bundle_id}:unit:{unit_index}").strip()
            issues: list[str] = []
            if unit_id in unit_ids:
                issues.append("writer_claim_unit_id_duplicated")
            unit_ids.add(unit_id)
            issues.extend(structure_issues)
            mapping_issues, declared_keys = _writer_unit_mapping_issues(
                unit,
                bundle,
                occurrence_index=occurrence_index,
                clusters_by_occurrence=clusters_by_occurrence,
                corpus=corpus,
            )
            issues.extend(mapping_issues)
            claim_text = str(unit.get("claim_text") or "").strip()
            table_bound = any(str(unit.get(field) or "").strip() for field in ("table_id", "row_id", "cell_id"))
            verdict: SupportVerdict | None = None
            if table_bound:
                judgment: Judgment = "unjudgable"
            elif not claim_text:
                judgment = "unjudgable"
                issues.append("writer_unit_claim_text_missing")
            elif declared_keys:
                assertion = corpus.authored_assertion_from_source_titles(
                    claim_text,
                    required_paper_keys=declared_keys,
                )
                verdict = evaluate_assertion(corpus, assertion)
                issues.extend(verdict.issue_codes)
                judgment = "unjudgable" if assertion.adapter_issues else verdict.judgment
            else:
                judgment = "unjudgable"
            if mapping_issues or unit_id in unit_ids and "writer_claim_unit_id_duplicated" in issues:
                judgment = "unjudgable"
            row = {
                **(_verdict_snapshot(verdict) if verdict is not None else {}),
                "claim_unit_id": unit_id,
                "citation_set_id": bundle_id,
                "section_numbers": [int(value) for value in bundle.get("section_numbers") or () if type(value) is int],
                "paper_keys": declared_keys,
                "supporting_occurrence_ids": [str(value) for value in unit.get("supporting_occurrence_ids") or () if str(value)]
                    if isinstance(unit.get("supporting_occurrence_ids"), Sequence)
                    and not isinstance(unit.get("supporting_occurrence_ids"), (str, bytes)) else [],
                "alignment_status": str(unit.get("alignment_status") or ""),
                "citation_tokens": [str(value) for value in unit.get("citation_tokens") or () if str(value)]
                    if isinstance(unit.get("citation_tokens"), Sequence)
                    and not isinstance(unit.get("citation_tokens"), (str, bytes)) else [],
                "claim_text_sha256": _sha256(claim_text.encode("utf-8")),
                "judgment": judgment,
                "issue_codes": list(dict.fromkeys(issues)),
                "source_row_count": len(unit.get("supporting_occurrence_ids") or ())
                    if isinstance(unit.get("supporting_occurrence_ids"), Sequence)
                    and not isinstance(unit.get("supporting_occurrence_ids"), (str, bytes)) else 0,
            }
            rows.append(row)
    issue_codes = list(dict.fromkeys(structure_issues + [
        code for row in rows for code in row.get("issue_codes") or ()
    ]))
    counts = _judgment_counts(rows)
    required_rows = _required_comparison_audit(
        required_comparisons,
        rows,
        row_id_field="claim_unit_id",
    )
    return {
        "schema_version": "controlled-pdf-all-writer-claim-units/v1",
        "status": _aggregate_judgment(rows, has_structural_issues=bool(structure_issues)),
        "issue_codes": issue_codes,
        "claim_unit_counts": counts,
        "claim_units": rows,
        "required_comparisons": required_rows,
        "required_comparisons_status": (
            "supported"
            if required_rows and all(item["status"] == "supported" for item in required_rows)
            else "unsupported"
        ),
    }


__all__ = [
    "AuthoredAssertion",
    "ControlledPdfCorpus",
    "PaperClause",
    "SourceDocument",
    "SourceFact",
    "SourceLocator",
    "SupportMetrics",
    "SupportProof",
    "SupportVerdict",
    "audit_all_canonical_writer_claim_units",
    "audit_all_selected_candidate_support",
    "audit_candidate_support",
    "audit_canonical_citation_manifest",
    "evaluate_assertion",
]

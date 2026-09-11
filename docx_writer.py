"""Canonical DOCX renderer for the current review artifact contract."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from docx import Document
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
from docx.shared import Cm, Inches, Pt
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from services.citation_catalog import CitationCatalogEntry, format_in_text_citation, format_reference_entry
from services.citation_ref_catalog import LEGAL_CITE_REF_TOKEN_PATTERN, extract_ref_ids_from_token
from services.citation_style import CitationStyleEngine, ReferenceSegment, normalize_creators


def _log(logger: Any, level: str, message: str) -> None:
    method = getattr(logger, level, None) or getattr(logger, "info", None)
    if callable(method):
        method(message)


def _entry_lookup(manifest: Mapping[str, Any]) -> dict[str, CitationCatalogEntry]:
    entries: dict[str, CitationCatalogEntry] = {}
    for raw in manifest.get("paper_entries", []):
        if not isinstance(raw, Mapping):
            continue
        paper_id = str(raw.get("paper_id") or raw.get("paper_key") or "").strip()
        paper_key = str(raw.get("paper_key") or paper_id).strip()
        if not paper_id:
            continue
        entry = CitationCatalogEntry(
            index=len(entries) + 1,
            paper_id=paper_id,
            paper_key=paper_key,
            title=str(raw.get("title") or ""),
            authors=[str(item).strip() for item in raw.get("authors", []) if str(item).strip()],
            year=str(raw.get("year") or ""),
            journal=str(raw.get("journal") or ""),
            doi=str(raw.get("doi") or ""),
            aliases=[str(item) for item in raw.get("aliases", [])],
            creators=normalize_creators(raw.get("creators") or raw.get("authors")),
            container_title=str(
                raw.get("container_title")
                or raw.get("journal")
                or raw.get("publication_title")
                or ""
            ),
            volume=str(raw.get("volume") or ""),
            issue=str(raw.get("issue") or ""),
            pages=str(raw.get("pages") or ""),
            article_number=str(raw.get("article_number") or ""),
            publisher=str(raw.get("publisher") or ""),
            url=str(raw.get("url") or ""),
            year_suffix=str(raw.get("year_suffix") or ""),
        )
        entries[paper_id] = entry
        entries[paper_key] = entry
    by_ref: dict[str, CitationCatalogEntry] = {}
    for occurrence in manifest.get("occurrences", []):
        if not isinstance(occurrence, Mapping):
            continue
        ref_id = str(occurrence.get("ref_id") or "").strip()
        paper_id = str(occurrence.get("paper_id") or "").strip()
        if ref_id and paper_id in entries:
            by_ref[ref_id] = entries[paper_id]
    return by_ref


def _paper_entry_lookup(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    lookup: dict[str, Mapping[str, Any]] = {}
    for raw in manifest.get("paper_entries", []):
        if not isinstance(raw, Mapping):
            continue
        for key in ("paper_id", "paper_key"):
            value = str(raw.get(key) or "").strip()
            if value:
                lookup[value] = raw
    return lookup


def _citation_token_options(token: str) -> dict[str, str]:
    inner = str(token or "").strip()[2:-2]
    parts = inner.split("|")
    options: dict[str, str] = {}
    for part in parts[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            options[key.strip().casefold()] = value.strip()
    return options


def render_structured_citations(
    text: str,
    generator_instance: Any,
    citation_manifest: Mapping[str, Any],
) -> tuple[str, List[str]]:
    del generator_instance
    lookup = _entry_lookup(citation_manifest)
    style_engine = CitationStyleEngine()
    unresolved: list[str] = []
    raw = str(text or "")
    occurrence_modes: dict[str, list[tuple[str, str | None]]] = {}
    for occurrence in citation_manifest.get("occurrences", []):
        if not isinstance(occurrence, Mapping):
            continue
        ref_id = str(occurrence.get("ref_id") or "").strip()
        if ref_id:
            occurrence_modes.setdefault(ref_id, []).append(
                (
                    str(occurrence.get("mode") or "parenthetical"),
                    str(occurrence.get("locator")) if occurrence.get("locator") else None,
                )
            )
    occurrence_positions: dict[str, int] = {}

    # --- Group adjacent citation tokens into one multi-id group -----------
    # Writer emission often produces consecutive single-ref tokens, e.g.
    #   [[cite_ref:R006]][[cite_ref:R009]]
    # which would otherwise render as "(A)(B)".  Merge a maximal run of
    # adjacent tokens into a single token with comma-separated ref ids:
    #   [[cite_ref:R006, R009]] -> "(A; B)"
    def _group_adjacent(run_match: re.Match[str]) -> str:
        run = run_match.group(0)
        ids: list[str] = []
        for token in re.findall(r"\[\[cite_ref:[^\]]+\]\]", run):
            for ref_id in extract_ref_ids_from_token(token):
                if ref_id not in ids:
                    ids.append(ref_id)
        if len(ids) <= 1:
            return run
        return f"[[cite_ref:{', '.join(ids)}]]"

    rendered_text = re.sub(r"(?:\[\[cite_ref:[^\]]+\]\])+", _group_adjacent, raw)

    # --- Normalize missing space before a citation group ------------------
    # "text[[cite_ref:R008]]" -> "text [[cite_ref:R008]]"; the renderer never
    # depends on the model emitting the space itself.
    def _ensure_space_before(match: re.Match[str]) -> str:
        return f"{match.group(1)} {match.group(2)}"

    rendered_text = re.sub(
        r"([^\s(,，.。])(\[\[cite_ref:)", _ensure_space_before, rendered_text
    )

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        ref_ids = extract_ref_ids_from_token(token)
        if not ref_ids:
            unresolved.append(token)
            return token
        options = _citation_token_options(token)
        rendered: list[str] = []
        modes: list[str] = []
        for ref_id in ref_ids:
            entry = lookup.get(ref_id)
            if entry is None:
                unresolved.append(ref_id)
                continue
            position = occurrence_positions.get(ref_id, 0)
            occurrence_positions[ref_id] = position + 1
            occurrence_mode, occurrence_locator = (
                occurrence_modes.get(ref_id, [("parenthetical", None)])[position]
                if position < len(occurrence_modes.get(ref_id, []))
                else ("parenthetical", None)
            )
            mode = options.get("mode") or occurrence_mode
            locator = options.get("locator") or occurrence_locator
            value = style_engine.format_in_text(
                entry,
                mode=mode,
                locator=locator,
                year_suffix=entry.year_suffix,
            )
            modes.append(mode)
            rendered.append(value)
        if len(rendered) != len(ref_ids):
            return token
        if len(rendered) == 1 and modes == ["narrative"]:
            return rendered[0]
        if all(mode == "parenthetical" for mode in modes):
            return f"({'; '.join(value.strip('()') for value in rendered)})"
        return "; ".join(rendered)

    def record_legacy(match: re.Match[str]) -> str:
        token = match.group(0)
        unresolved.append(token)
        return token

    rendered = re.sub(r"\[\[cite:(?!ref:)[^\]]+\]\]", record_legacy, rendered_text)
    rendered = re.sub(r"\[\[cite_ref:[^\]]+\]\]", replace, rendered)
    return rendered.replace("`", ""), unresolved


def set_advanced_document_styles(
    doc: Any,
    font_name: str = "Times New Roman",
    font_size_body: int = 12,
    font_size_heading1: int = 16,
    font_size_heading2: int = 14,
) -> None:
    section = doc.sections[0]
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)
    section.left_margin = Cm(3.17)
    section.right_margin = Cm(3.17)
    normal = doc.styles["Normal"]
    normal.font.name = font_name
    normal.font.size = Pt(font_size_body)
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), font_name)
    for name, size in (("Heading 1", font_size_heading1), ("Heading 2", font_size_heading2)):
        style = doc.styles[name]
        style.font.name = font_name
        style.font.size = Pt(size)
        style.font.bold = True
        style._element.rPr.rFonts.set(qn("w:eastAsia"), font_name)


def add_header_and_footer(doc: Any, title: str = "Literature Review") -> None:
    section = doc.sections[0]
    header = section.header.paragraphs[0]
    header.text = title
    header.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
    run = footer.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.text = "PAGE"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._element.extend((begin, instruction, end))


def append_section_to_word_document(
    generator_instance: Any,
    section_number: int,
    section_title: str,
    section_text: str,
    word_file: str,
    *,
    citation_manifest: Mapping[str, Any],
) -> bool:
    try:
        output = Path(word_file)
        doc = Document(str(output)) if output.is_file() else Document()
        if not output.is_file():
            set_advanced_document_styles(doc)
            add_header_and_footer(doc)
        rendered, unresolved = render_structured_citations(
            section_text,
            generator_instance,
            citation_manifest,
        )
        if unresolved:
            raise ValueError("unresolved citation references: " + ", ".join(sorted(set(unresolved))))
        doc.add_heading(f"{section_number}. {section_title}", level=2)
        for paragraph in rendered.split("\n\n"):
            if paragraph.strip():
                doc.add_paragraph(paragraph.strip())
        output.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(output))
        return True
    except Exception as exc:
        _log(getattr(generator_instance, "logger", None), "error", str(exc))
        return False


def generate_apa_references_from_manifest(
    citation_manifest: Mapping[str, Any],
    generator_instance: Any,
) -> List[str]:
    del generator_instance
    references: list[str] = []
    for entry in citation_manifest.get("bibliography", []):
        if isinstance(entry, Mapping) and entry.get("is_cited", True):
            text = str(entry.get("citation_text") or "").strip()
            if text:
                references.append(_strip_reference_markup(text))
    # ``citation_manifest.bibliography`` is the sole bibliography authority.
    # An empty cited bibliography must remain empty; reconstructing entries
    # from ``paper_entries`` would emit uncited corpus papers.
    return list(dict.fromkeys(references))


def _strip_reference_markup(text: str) -> str:
    """Keep legacy manifest text readable while removing Markdown emphasis."""

    return re.sub(r"\*([^*\n]+)\*", r"\1", str(text or "")).replace("*", "").strip()


def _legacy_reference_segments(text: str) -> tuple[ReferenceSegment, ...]:
    segments: list[ReferenceSegment] = []
    cursor = 0
    for match in re.finditer(r"\*([^*\n]+)\*", str(text or "")):
        if match.start() > cursor:
            segments.append(ReferenceSegment(str(text)[cursor : match.start()]))
        segments.append(ReferenceSegment(match.group(1), italic=True))
        cursor = match.end()
    if cursor < len(str(text or "")):
        segments.append(ReferenceSegment(str(text)[cursor:]))
    if not segments:
        segments.append(ReferenceSegment(_strip_reference_markup(text)))
    return tuple(segments)


def _manifest_reference_segments(
    manifest: Mapping[str, Any],
    bibliography_entry: Mapping[str, Any],
) -> tuple[ReferenceSegment, ...]:
    paper_lookup = _paper_entry_lookup(manifest)
    paper_id = str(bibliography_entry.get("paper_id") or "").strip()
    paper_key = str(bibliography_entry.get("paper_key") or "").strip()
    raw = paper_lookup.get(paper_id) or paper_lookup.get(paper_key)
    fallback = _strip_reference_markup(str(bibliography_entry.get("citation_text") or "").strip())
    if not raw:
        return _legacy_reference_segments(str(bibliography_entry.get("citation_text") or ""))

    has_structured_metadata = any(
        str(raw.get(key) or "").strip()
        for key in (
            "creators",
            "container_title",
            "journal",
            "publication_title",
            "volume",
            "issue",
            "pages",
            "article_number",
            "publisher",
            "doi",
            "url",
        )
    )
    if not has_structured_metadata:
        return _legacy_reference_segments(str(bibliography_entry.get("citation_text") or ""))

    def build_entry(raw_entry: Mapping[str, Any], index: int) -> CitationCatalogEntry:
        raw_id = str(raw_entry.get("paper_id") or raw_entry.get("paper_key") or "").strip()
        raw_key = str(raw_entry.get("paper_key") or raw_id).strip()
        return CitationCatalogEntry(
            index=index,
            paper_id=raw_id,
            paper_key=raw_key,
            title=str(raw_entry.get("title") or ""),
            authors=[str(item) for item in raw_entry.get("authors", []) if str(item).strip()],
            year=str(raw_entry.get("year") or ""),
            journal=str(raw_entry.get("journal") or ""),
            doi=str(raw_entry.get("doi") or ""),
            aliases=[str(item) for item in raw_entry.get("aliases", []) if str(item).strip()],
            creators=normalize_creators(raw_entry.get("creators") or raw_entry.get("authors")),
            container_title=str(
                raw_entry.get("container_title")
                or raw_entry.get("journal")
                or raw_entry.get("publication_title")
                or ""
            ),
            volume=str(raw_entry.get("volume") or ""),
            issue=str(raw_entry.get("issue") or ""),
            pages=str(raw_entry.get("pages") or ""),
            article_number=str(raw_entry.get("article_number") or ""),
            publisher=str(raw_entry.get("publisher") or ""),
            url=str(raw_entry.get("url") or ""),
            year_suffix=str(raw_entry.get("year_suffix") or ""),
        )

    raw_papers = [
        item for item in manifest.get("paper_entries", []) if isinstance(item, Mapping)
    ]
    all_entries = [build_entry(item, index) for index, item in enumerate(raw_papers, start=1)]
    engine = CitationStyleEngine()
    suffixes = engine.disambiguation_suffixes(all_entries)
    entry = next(
        (
            item
            for item in all_entries
            if item.paper_id == paper_id or item.paper_key == paper_key
        ),
        build_entry(raw, 1),
    )
    entry = replace(entry, year_suffix=entry.year_suffix or suffixes.get(entry.paper_id, ""))
    formatted = engine.format_reference(entry, year_suffix=entry.year_suffix)
    if not formatted.text:
        return _legacy_reference_segments(fallback)
    if not fallback or fallback == formatted.text:
        return formatted.segments

    # Manifest citation_text is the canonical JSON/DOCX text contract. Keep
    # that exact text when an older manifest omitted rich fields such as the
    # same-author/year suffix, while still applying real Word italics.
    container = entry.container_title
    if not container or container not in fallback:
        return (ReferenceSegment(fallback),)
    container_start = fallback.find(container)
    segments: list[ReferenceSegment] = [ReferenceSegment(fallback[:container_start])]
    segments.append(ReferenceSegment(container, italic=True))
    cursor = container_start + len(container)
    if entry.volume:
        volume_start = fallback.find(entry.volume, cursor)
        if volume_start >= 0:
            segments.append(ReferenceSegment(fallback[cursor:volume_start]))
            segments.append(ReferenceSegment(entry.volume, italic=True))
            cursor = volume_start + len(entry.volume)
    segments.append(ReferenceSegment(fallback[cursor:]))
    return tuple(segment for segment in segments if segment.text)


def _append_reference_paragraph(
    doc: Any,
    segments: Iterable[ReferenceSegment],
) -> None:
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.left_indent = Cm(1.27)
    paragraph.paragraph_format.first_line_indent = Cm(-1.27)
    paragraph.paragraph_format.space_after = Pt(8)
    paragraph.paragraph_format.line_spacing = 1.0
    for segment in segments:
        run = paragraph.add_run(segment.text)
        run.italic = segment.italic


def scan_docx_for_unresolved_citation_tokens(
    docx_path: str,
    citation_manifest: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    doc = Document(docx_path)
    text_parts = [paragraph.text for paragraph in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                text_parts.extend(paragraph.text for paragraph in cell.paragraphs)
    for section in doc.sections:
        text_parts.extend(paragraph.text for paragraph in section.header.paragraphs)
        text_parts.extend(paragraph.text for paragraph in section.footer.paragraphs)
    text = "\n".join(text_parts)
    raw_tokens = re.findall(r"\[\[(?:cite_ref|cite):[^\]]+\]\]", text)
    legal_tokens = LEGAL_CITE_REF_TOKEN_PATTERN.findall(text)
    paper_entries = _paper_entry_lookup(citation_manifest or {})
    valid_ref_to_paper: dict[str, str] = {}
    for item in (citation_manifest or {}).get("occurrences", []):
        if not isinstance(item, Mapping):
            continue
        ref_id = str(item.get("ref_id") or "").strip()
        paper_id = str(item.get("paper_id") or "").strip()
        paper_key = str(item.get("paper_key") or "").strip()
        if ref_id and paper_id and paper_id != "unknown" and (
            paper_id in paper_entries or paper_key in paper_entries
        ):
            valid_ref_to_paper[ref_id] = paper_id
    unresolved = [
        token
        for token in raw_tokens
        if (
            not token.startswith("[[cite_ref:")
            or not extract_ref_ids_from_token(token)
            or not set(extract_ref_ids_from_token(token)).issubset(valid_ref_to_paper)
        )
    ]
    unresolved_ids = sorted(
        {
            ref_id
            for token in raw_tokens
            if token.startswith("[[cite_ref:")
            for ref_id in extract_ref_ids_from_token(token)
            if ref_id not in valid_ref_to_paper
        }
    )
    unresolved.extend(unresolved_ids)
    text_without_tokens = re.sub(r"\[\[(?:cite_ref|cite):[^\]]+\]\]", "", text)
    bare_ref_ids = sorted(
        {
            ref_id
            for ref_id in valid_ref_to_paper
            if re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(ref_id)}(?![A-Za-z0-9_])",
                text_without_tokens,
            )
        }
    )
    references_index = next(
        (
            index
            for index, paragraph in enumerate(doc.paragraphs)
            if paragraph.text.strip().casefold() in {"references", "参考文献"}
        ),
        None,
    )
    reference_paragraphs = (
        [paragraph.text.strip() for paragraph in doc.paragraphs[references_index + 1 :] if paragraph.text.strip()]
        if references_index is not None
        else []
    )
    uncited_entries: list[str] = []
    markdown_reference_tokens: list[str] = []
    for raw_entry in (citation_manifest or {}).get("bibliography", []):
        if not isinstance(raw_entry, Mapping) or raw_entry.get("is_cited", True):
            continue
        expected = _strip_reference_markup(str(raw_entry.get("citation_text") or "").strip())
        if expected and any(
            re.sub(r"\s+", " ", paragraph).strip() == expected
            for paragraph in reference_paragraphs
        ):
            uncited_entries.append(expected)
    cited_paper_ids = set(valid_ref_to_paper.values())
    seen_paper_ids: set[str] = set()
    for raw_paper in (citation_manifest or {}).get("paper_entries", []):
        if not isinstance(raw_paper, Mapping):
            continue
        paper_id = str(raw_paper.get("paper_id") or raw_paper.get("paper_key") or "").strip()
        if not paper_id or paper_id in seen_paper_ids or paper_id in cited_paper_ids:
            continue
        seen_paper_ids.add(paper_id)
        title = str(raw_paper.get("title") or "").strip()
        if title and any(title.casefold() in paragraph.casefold() for paragraph in reference_paragraphs):
            uncited_entries.append(title)
    for paragraph in reference_paragraphs:
        markdown_reference_tokens.extend(re.findall(r"\*[^*\n]+\*", paragraph))
    return {
        "docx_path": docx_path,
        "paragraph_count": len(doc.paragraphs),
        "table_count": len(doc.tables),
        "legal_tokens": legal_tokens,
        "unresolved_tokens": sorted(set(unresolved)),
        "bare_ref_ids": bare_ref_ids,
        "uncited_bibliography_entries": sorted(set(uncited_entries)),
        "markdown_reference_tokens": sorted(set(markdown_reference_tokens)),
        "references_seen": "References" in text,
        "passed": not (
            unresolved
            or bare_ref_ids
            or uncited_entries
            or markdown_reference_tokens
        ),
    }


def rebuild_review_docx_from_structured_artifacts(
    generator_instance: Any,
    review_draft: Mapping[str, Any],
    citation_manifest: Mapping[str, Any],
    output_path: str,
) -> None:
    output = Path(output_path)
    if output.exists():
        output.unlink()
    for section in review_draft.get("content", {}).get("sections", []):
        text = "\n\n".join(
            str(block.get("text") or "").strip()
            for block in section.get("blocks", [])
            if isinstance(block, Mapping) and str(block.get("text") or "").strip()
        )
        if not append_section_to_word_document(
            generator_instance,
            int(section.get("section_number") or 0),
            str(section.get("section_title") or ""),
            text,
            str(output),
            citation_manifest=citation_manifest,
        ):
            raise ValueError("section DOCX rendering failed")
    doc = Document(str(output))
    doc.add_heading("References", level=1)
    seen_references: set[str] = set()
    bibliography = citation_manifest.get("bibliography", [])
    if isinstance(bibliography, list):
        for raw_entry in bibliography:
            if not isinstance(raw_entry, Mapping) or not raw_entry.get("is_cited", True):
                continue
            reference_text = _strip_reference_markup(
                str(raw_entry.get("citation_text") or "").strip()
            )
            if not reference_text or reference_text in seen_references:
                continue
            _append_reference_paragraph(
                doc,
                _manifest_reference_segments(citation_manifest, raw_entry),
            )
            seen_references.add(reference_text)
    if not seen_references:
        for reference in generate_apa_references_from_manifest(citation_manifest, generator_instance):
            if reference and reference not in seen_references:
                _append_reference_paragraph(doc, (ReferenceSegment(reference),))
                seen_references.add(reference)
    doc.save(str(output))
    report = scan_docx_for_unresolved_citation_tokens(str(output), citation_manifest)
    if not report["passed"]:
        raise ValueError("DOCX contains unresolved citation tokens")


def rebuild_final_docx_from_manifest(
    generator_instance: Any,
    review_draft: Mapping[str, Any],
    citation_manifest: Mapping[str, Any],
    output_path: str,
    *,
    scan_report_path: str = "",
) -> Dict[str, Any]:
    rebuild_review_docx_from_structured_artifacts(
        generator_instance,
        review_draft,
        citation_manifest,
        output_path,
    )
    report = scan_docx_for_unresolved_citation_tokens(output_path, citation_manifest)
    if scan_report_path:
        target = Path(scan_report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def create_word_document(generator_instance: Any, markdown_text: str, output_path: str) -> bool:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    set_advanced_document_styles(doc)
    add_header_and_footer(doc)
    for line in str(markdown_text or "").splitlines():
        if line.startswith("### "):
            doc.add_heading(line[4:], level=3)
        elif line.startswith("## "):
            doc.add_heading(line[3:], level=2)
        elif line.startswith("# "):
            doc.add_heading(line[2:], level=1)
        elif line.strip():
            doc.add_paragraph(line.strip())
    doc.save(str(output))
    _log(getattr(generator_instance, "logger", None), "info", f"DOCX written: {output}")
    return output.is_file()

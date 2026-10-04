"""Canonical DOCX renderer for the current review artifact contract."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
from docx.shared import Cm, Pt
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from services.citation_catalog import CitationCatalogEntry
from services.citation_ref_catalog import LEGAL_CITE_REF_TOKEN_PATTERN, extract_ref_ids_from_token
from services.citation_style import CitationStyleEngine, ReferenceSegment, normalize_creators


_WRITER_TABLE_SCHEMA_VERSION = "writer_table_layout/v1"
_WRITER_TABLE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_WRITER_TABLE_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WRITER_TABLE_STATIC_LABEL_FORBIDDEN = re.compile(r"[0-9０-９.!?。！？\r\n]")
_WRITER_TABLE_CITATION_TOKEN = re.compile(r"\[\[cite_ref:[^\]]+\]\]")
_WRITER_TABLE_SOURCE_CONTEXT_FIELDS = (
    "source_claim_ids",
    "evidence_ids",
    "source_field_ids",
    "qualifier_source_claim_ids",
    "qualifier_evidence_ids",
    "qualifier_source_field_ids",
)


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
            in_text_author_count=int(raw.get("in_text_author_count") or 0),
            in_text_include_initials=bool(raw.get("in_text_include_initials", False)),
            in_text_include_full_names=bool(raw.get("in_text_include_full_names", False)),
        )
        entries[paper_id] = entry
        entries[paper_key] = entry
    unique_entries = {entry.paper_id: entry for entry in entries.values()}
    style_engine = CitationStyleEngine()
    suffixes = style_engine.disambiguation_suffixes(unique_entries.values())
    author_counts = style_engine.disambiguation_author_counts(unique_entries.values())
    author_initials = style_engine.disambiguation_author_initials(unique_entries.values())
    author_full_names = style_engine.disambiguation_author_full_names(unique_entries.values())
    entries = {
        key: replace(
            entry,
            year_suffix=entry.year_suffix or suffixes.get(entry.paper_id, ""),
            in_text_author_count=author_counts.get(entry.paper_id, 0),
            in_text_include_initials=(
                entry.in_text_include_initials or entry.paper_id in author_initials
            ),
            in_text_include_full_names=(
                entry.in_text_include_full_names or entry.paper_id in author_full_names
            ),
        )
        for key, entry in entries.items()
    }
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
    *,
    section_number: int | None = None,
    block_id: str | None = None,
    occurrence_positions: dict[str, int] | None = None,
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
            in_section = (
                section_number is None
                or int(occurrence.get("section_number") or 0) == section_number
            )
            in_block = (
                block_id is None
                or str(occurrence.get("block_id") or "") == block_id
            )
            if in_section and in_block:
                occurrence_modes.setdefault(ref_id, []).append(
                    (
                        str(occurrence.get("mode") or "parenthetical"),
                        str(occurrence.get("locator")) if occurrence.get("locator") else None,
                    )
                )
    used_occurrences = occurrence_positions if occurrence_positions is not None else {}

    rendered_text = raw

    # --- Normalize missing space before a citation group ------------------
    # "text[[cite_ref:R008]]" -> "text [[cite_ref:R008]]"; the renderer never
    # depends on the model emitting the space itself.
    def _ensure_space_before(match: re.Match[str]) -> str:
        return f"{match.group(1)} {match.group(2)}"

    rendered_text = re.sub(
        r"([^\s(,，.。\]])(\[\[cite_ref:)", _ensure_space_before, rendered_text
    )

    def replace(match: re.Match[str]) -> str:
        rendered: list[str] = []
        modes: list[str] = []
        expected = 0
        for token in re.findall(r"\[\[cite_ref:[^\]]+\]\]", match.group(0)):
            ref_ids = extract_ref_ids_from_token(token)
            if not ref_ids:
                unresolved.append(token)
                return match.group(0)
            expected += len(ref_ids)
            options = _citation_token_options(token)
            for ref_id in ref_ids:
                entry = lookup.get(ref_id)
                if entry is None:
                    unresolved.append(ref_id)
                    continue
                position = used_occurrences.get(ref_id, 0)
                used_occurrences[ref_id] = position + 1
                occurrence_mode, occurrence_locator = (
                    occurrence_modes.get(ref_id, [("parenthetical", None)])[position]
                    if position < len(occurrence_modes.get(ref_id, []))
                    else ("parenthetical", None)
                )
                mode = options.get("mode") or occurrence_mode
                locator = options.get("locator") or occurrence_locator
                rendered.append(style_engine.format_in_text(entry, mode=mode, locator=locator, year_suffix=entry.year_suffix))
                modes.append(mode)
        if len(rendered) != expected:
            return match.group(0)
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
    rendered = re.sub(r"(?:\[\[cite_ref:[^\]]+\]\])+", replace, rendered)
    return rendered.replace("`", ""), unresolved


def _split_pipe_table_row(line: str) -> list[str]:
    """Split one Markdown pipe row, honoring escaped literal pipes."""

    raw = str(line or "").strip()
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in raw:
        if escaped:
            if character == "|":
                current.append("|")
            else:
                current.extend(("\\", character))
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            cells.append("".join(current).strip())
            current.clear()
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    if raw.startswith("|") and cells and not cells[0]:
        cells.pop(0)
    if raw.endswith("|") and cells and not cells[-1]:
        cells.pop()
    return cells


def _parse_pipe_table(text: str) -> tuple[list[str], list[str], list[list[str]]] | None:
    """Parse a complete GitHub-style pipe table, not prose containing pipes.

    The current Review v3 Writer schema carries block text and normalizes
    ``block_kind`` to ``paragraph``.  An unambiguous Markdown table is therefore
    the compatible table encoding until that upstream schema carries typed
    rows.  Requiring the whole block to match prevents ordinary prose with a
    pipe character from changing its DOCX representation.
    """

    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if len(lines) < 3:
        return None
    header = _split_pipe_table_row(lines[0])
    separator = _split_pipe_table_row(lines[1])
    if not header or len(header) != len(separator) or any(not cell for cell in header):
        return None
    alignments: list[str] = []
    for cell in separator:
        if not re.fullmatch(r":?-{3,}:?", cell):
            return None
        alignments.append(
            "center" if cell.startswith(":") and cell.endswith(":")
            else "right" if cell.endswith(":")
            else "left"
        )
    body: list[list[str]] = []
    for line in lines[2:]:
        row = _split_pipe_table_row(line)
        if len(row) != len(header):
            return None
        body.append(row)
    return (header, alignments, body) if body else None


def _has_native_writer_table_fields(block: Mapping[str, Any]) -> bool:
    return "table_layout_schema_version" in block or any(
        field in block
        for field in ("table_id", "headers", "rows")
    )


def _validate_native_writer_table_block(
    block: Mapping[str, Any],
) -> tuple[list[str], list[list[Mapping[str, Any]]]]:
    """Validate the projected Writer table contract before creating DOCX XML."""

    if str(block.get("block_kind") or "").casefold() != "table":
        raise ValueError("native Writer table must have block_kind='table'")
    if block.get("table_layout_schema_version") != _WRITER_TABLE_SCHEMA_VERSION:
        raise ValueError("native Writer table has an unsupported layout schema version")
    if "text" in block and block["text"] != "":
        raise ValueError("native Writer table must not carry parent text")

    for field in ("block_id", "table_id"):
        value = block.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"native Writer table is missing {field}")
    table_block_id = str(block["block_id"])
    table_id = str(block["table_id"])
    if not _WRITER_TABLE_IDENTIFIER.fullmatch(table_id):
        raise ValueError("native Writer table has an invalid table_id")
    basis_hash = block.get("writer_task_basis_hash")
    if not isinstance(basis_hash, str) or not _WRITER_TABLE_SHA256.fullmatch(basis_hash):
        raise ValueError("native Writer table has an invalid writer_task_basis_hash")

    headers = block.get("headers")
    if (
        not isinstance(headers, list)
        or not headers
        or len(headers) > 16
        or any(
            not isinstance(header, str)
            or not header.strip()
            or header != header.strip()
            or len(header) > 80
            or _WRITER_TABLE_STATIC_LABEL_FORBIDDEN.search(header)
            or _WRITER_TABLE_CITATION_TOKEN.search(header)
            for header in headers
        )
        or len(headers) != len(set(headers))
    ):
        raise ValueError("native Writer table has malformed or duplicate headers")

    rows = block.get("rows")
    if not isinstance(rows, list) or not rows or len(rows) > 512:
        raise ValueError("native Writer table has malformed or unbounded rows")

    row_ids: set[str] = set()
    cell_ids: set[str] = set()
    cell_block_ids: set[str] = set()
    factual_unit_ids: set[tuple[str, str]] = set()
    validated_rows: list[list[Mapping[str, Any]]] = []
    total_cells = len(headers)
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("native Writer table contains a malformed row")
        row_id = row.get("row_id")
        cells = row.get("cells")
        if (
            not isinstance(row_id, str)
            or not _WRITER_TABLE_IDENTIFIER.fullmatch(row_id)
            or row_id in row_ids
            or not isinstance(cells, list)
            or len(cells) != len(headers)
        ):
            raise ValueError("native Writer table row has an invalid identity or dimensions")
        row_ids.add(row_id)
        total_cells += len(cells)
        if total_cells > 4096:
            raise ValueError("native Writer table exceeds the fixed cell limit")

        validated_cells: list[Mapping[str, Any]] = []
        for cell in cells:
            if not isinstance(cell, Mapping):
                raise ValueError("native Writer table contains a malformed cell")
            cell_id = cell.get("cell_id")
            cell_block_id = cell.get("block_id")
            text = cell.get("text")
            cell_kind = cell.get("cell_kind")
            if (
                not isinstance(cell_id, str)
                or not cell_id.strip()
                or cell_id in cell_ids
                or not isinstance(cell_block_id, str)
                or not cell_block_id.strip()
                or cell_block_id == table_block_id
                or cell_block_id in cell_block_ids
                or not isinstance(text, str)
                or not text.strip()
            ):
                raise ValueError("native Writer table cell is missing a unique identity or text")
            cell_ids.add(cell_id)
            cell_block_ids.add(cell_block_id)

            if cell_kind == "static_label":
                if (
                    cell.get("source_validation_status")
                    != "caller_allowlisted_nonfactual_label"
                    or len(text) > 80
                    or text != text.strip()
                    or _WRITER_TABLE_STATIC_LABEL_FORBIDDEN.search(text)
                    or _WRITER_TABLE_CITATION_TOKEN.search(text)
                    or any(
                        field in cell
                        for field in (
                            "writer_task_id",
                            "writer_output_unit_id",
                            "writer_task_basis_hash",
                            "allowed_ref_ids",
                            "required_source_context",
                        )
                    )
                ):
                    raise ValueError("native Writer table static label is not a valid nonfactual label")
            elif cell_kind == "factual_output_unit":
                task_id = cell.get("writer_task_id")
                unit_id = cell.get("writer_output_unit_id")
                cell_basis_hash = cell.get("writer_task_basis_hash")
                allowed_ref_ids = cell.get("allowed_ref_ids")
                source_context = cell.get("required_source_context")
                if (
                    not isinstance(task_id, str)
                    or not _WRITER_TABLE_IDENTIFIER.fullmatch(task_id)
                    or not isinstance(unit_id, str)
                    or not _WRITER_TABLE_IDENTIFIER.fullmatch(unit_id)
                    or cell_basis_hash != basis_hash
                    or cell.get("source_validation_status")
                    != "canonical_source_inventory_verified"
                    or not isinstance(allowed_ref_ids, list)
                    or not allowed_ref_ids
                    or any(not isinstance(ref_id, str) or not ref_id.strip() for ref_id in allowed_ref_ids)
                    or len(allowed_ref_ids) != len(set(allowed_ref_ids))
                    or not isinstance(source_context, Mapping)
                    or any(
                        not isinstance(source_context.get(field), list)
                        or any(
                            not isinstance(value, str) or not value.strip()
                            for value in source_context[field]
                        )
                        for field in _WRITER_TABLE_SOURCE_CONTEXT_FIELDS
                    )
                ):
                    raise ValueError("native Writer table factual cell has incomplete source bindings")
                unit_key = (task_id, unit_id)
                if unit_key in factual_unit_ids:
                    raise ValueError("native Writer table repeats a factual output unit")
                factual_unit_ids.add(unit_key)
                used_ref_ids: set[str] = set()
                for token in _WRITER_TABLE_CITATION_TOKEN.findall(text):
                    ref_ids = extract_ref_ids_from_token(token)
                    if not ref_ids:
                        raise ValueError("native Writer table factual cell has a malformed citation")
                    used_ref_ids.update(ref_ids)
                if not used_ref_ids or used_ref_ids - set(allowed_ref_ids):
                    raise ValueError("native Writer table factual cell has missing or foreign citations")
                if len(text) > 4096 or _parse_pipe_table(text) is not None:
                    raise ValueError("native Writer table factual cell exceeds its text contract")
            else:
                raise ValueError("native Writer table cell has an unsupported cell_kind")
            validated_cells.append(cell)
        validated_rows.append(validated_cells)

    return headers, validated_rows


def _append_native_writer_table(
    doc: Any,
    headers: list[str],
    rows: list[list[Mapping[str, Any]]],
    *,
    generator_instance: Any,
    citation_manifest: Mapping[str, Any],
    section_number: int,
) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True
    header_row = table.rows[0]
    header_row_properties = header_row._tr.get_or_add_trPr()
    repeat_header = OxmlElement("w:tblHeader")
    repeat_header.set(qn("w:val"), "true")
    header_row_properties.append(repeat_header)

    for cell, text in zip(header_row.cells, headers, strict=True):
        paragraph = cell.paragraphs[0]
        paragraph.paragraph_format.space_after = Pt(0)
        paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
        paragraph.add_run(text).bold = True

    for row in rows:
        word_cells = table.add_row().cells
        for word_cell, cell in zip(word_cells, row, strict=True):
            text = str(cell["text"])
            if cell["cell_kind"] == "factual_output_unit":
                text, unresolved = render_structured_citations(
                    text,
                    generator_instance,
                    citation_manifest,
                    section_number=section_number,
                    block_id=str(cell["block_id"]),
                )
                if unresolved:
                    raise ValueError(
                        "unresolved native table citation references: "
                        + ", ".join(sorted(set(unresolved)))
                    )
            paragraph = word_cell.paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(0)
            paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
            paragraph.add_run(text)


def _append_pipe_table(
    doc: Any,
    table_data: tuple[list[str], list[str], list[list[str]]],
    *,
    generator_instance: Any,
    citation_manifest: Mapping[str, Any],
    section_number: int,
    block_id: str,
) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    header, alignments, body = table_data
    table = doc.add_table(rows=1, cols=len(header))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True

    header_row = table.rows[0]
    header_row_properties = header_row._tr.get_or_add_trPr()
    repeat_header = OxmlElement("w:tblHeader")
    repeat_header.set(qn("w:val"), "true")
    header_row_properties.append(repeat_header)

    shared_occurrence_positions: dict[str, int] = {}
    rows = [(header, True), *((row, False) for row in body)]
    for values, is_header in rows:
        cells = header_row.cells if is_header else table.add_row().cells
        for index, (cell, value) in enumerate(zip(cells, values, strict=True)):
            rendered, unresolved = render_structured_citations(
                value,
                generator_instance,
                citation_manifest,
                section_number=section_number,
                block_id=block_id,
                occurrence_positions=shared_occurrence_positions,
            )
            if unresolved:
                raise ValueError(
                    "unresolved table citation references: "
                    + ", ".join(sorted(set(unresolved)))
                )
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(0)
            paragraph.alignment = {
                "left": WD_ALIGN_PARAGRAPH.LEFT,
                "center": WD_ALIGN_PARAGRAPH.CENTER,
                "right": WD_ALIGN_PARAGRAPH.RIGHT,
            }[alignments[index]]
            run = paragraph.add_run(rendered)
            run.bold = is_header


def append_review_section_blocks_to_word_document(
    generator_instance: Any,
    section_number: int,
    section_title: str,
    blocks: Iterable[Mapping[str, Any]],
    word_file: str,
    *,
    citation_manifest: Mapping[str, Any],
) -> bool:
    """Append structured paragraphs and native Markdown pipe tables."""

    try:
        output = Path(word_file)
        doc = Document(str(output)) if output.is_file() else Document()
        if not output.is_file():
            set_advanced_document_styles(doc)
            add_header_and_footer(doc)
        doc.add_heading(f"{section_number}. {section_title}", level=2)
        for block in blocks:
            if not isinstance(block, Mapping):
                raise ValueError("review block must be an object")
            native_table_data = None
            if _has_native_writer_table_fields(block):
                native_table_data = _validate_native_writer_table_block(block)
            text = str(block.get("text") or "").strip()
            block_id = str(block.get("block_id") or "")
            block_kind = str(block.get("block_kind") or "paragraph").casefold()
            if native_table_data is not None:
                _append_native_writer_table(
                    doc,
                    *native_table_data,
                    generator_instance=generator_instance,
                    citation_manifest=citation_manifest,
                    section_number=section_number,
                )
                continue
            if not text:
                if block_kind in {"table", "markdown_table"}:
                    raise ValueError(
                        f"review table block {block_id or '<unknown>'} is empty"
                    )
                continue
            table_data = _parse_pipe_table(text)
            if block_kind in {"table", "markdown_table"} and table_data is None:
                raise ValueError(
                    f"review table block {block_id or '<unknown>'} is not a complete pipe table"
                )
            if table_data is not None:
                _append_pipe_table(
                    doc,
                    table_data,
                    generator_instance=generator_instance,
                    citation_manifest=citation_manifest,
                    section_number=section_number,
                    block_id=block_id,
                )
                continue

            rendered, unresolved = render_structured_citations(
                text,
                generator_instance,
                citation_manifest,
                section_number=section_number,
                block_id=block_id or None,
            )
            if unresolved:
                raise ValueError(
                    "unresolved citation references: "
                    + ", ".join(sorted(set(unresolved)))
                )
            for paragraph_text in rendered.split("\n\n"):
                if paragraph_text.strip():
                    doc.add_paragraph(paragraph_text.strip())
        output.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(output))
        return True
    except Exception as exc:
        _log(getattr(generator_instance, "logger", None), "error", str(exc))
        return False


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
            section_number=section_number,
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

    # A legacy manifest may contain an older reference string without the
    # document-level author-year suffix. Once the structured catalog proves a
    # suffix is required, the bibliography must use the same text as body
    # citations instead of preserving the ambiguous legacy string.
    if entry.year_suffix:
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
    sections = review_draft.get("content", {}).get("sections", [])
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        for block in section.get("blocks") or []:
            if not isinstance(block, Mapping):
                continue
            block_kind = str(block.get("block_kind") or "paragraph").casefold()
            if _has_native_writer_table_fields(block) or (
                block_kind in {"table", "markdown_table"}
                and not str(block.get("text") or "").strip()
            ):
                _validate_native_writer_table_block(block)
    if output.exists():
        output.unlink()
    for section in sections:
        blocks = section.get("blocks") or []
        has_table_block = any(
            isinstance(block, Mapping)
            and (
                str(block.get("block_kind") or "").casefold()
                in {"table", "markdown_table"}
                or _parse_pipe_table(str(block.get("text") or "")) is not None
            )
            for block in blocks
        )
        if has_table_block:
            appended = append_review_section_blocks_to_word_document(
                generator_instance,
                int(section.get("section_number") or 0),
                str(section.get("section_title") or ""),
                blocks,
                str(output),
                citation_manifest=citation_manifest,
            )
        else:
            text = "\n\n".join(
                str(block.get("text") or "").strip()
                for block in blocks
                if isinstance(block, Mapping) and str(block.get("text") or "").strip()
            )
            # Sections without tables keep the established renderer path and
            # its section-wide citation occurrence ordering exactly.
            if not blocks:
                text = str(section.get("content") or "").strip()
            appended = append_section_to_word_document(
                generator_instance,
                int(section.get("section_number") or 0),
                str(section.get("section_title") or ""),
                text,
                str(output),
                citation_manifest=citation_manifest,
            )
        if not appended:
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
    draft_identity = review_draft.get("draft_identity")
    draft_title = (
        str(draft_identity.get("title") or "").strip()
        if isinstance(draft_identity, Mapping)
        else ""
    )
    header = doc.sections[0].header.paragraphs[0]
    header.text = draft_title or "Literature Review"
    header.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
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

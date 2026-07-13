"""Versioned, diagnostic Zotero report parsing with a legacy list projection."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import logging
import os
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional, Sequence, Tuple, cast

from models import PaperInfo
from services.text_io import read_text_file_with_fallbacks


logger = logging.getLogger(__name__)
PARSER_VERSION = "zotero-parser-v1"


@dataclass(frozen=True)
class ZoteroFieldSourceV1:
    field: str
    source_key: str
    line_start: int
    line_end: int
    parser_route: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ZoteroFieldSourceV1":
        return cls(
            field=str(payload.get("field") or ""),
            source_key=str(payload.get("source_key") or ""),
            line_start=int(payload.get("line_start") or 0),
            line_end=int(payload.get("line_end") or 0),
            parser_route=str(payload.get("parser_route") or ""),
        )


@dataclass(frozen=True)
class ZoteroParseDiagnosticV1:
    code: str
    severity: Literal["info", "warning", "error"]
    message: str
    entry_index: Optional[int] = None
    field: str = ""
    line_start: Optional[int] = None
    line_end: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ZoteroParseDiagnosticV1":
        severity = str(payload.get("severity") or "error")
        if severity not in {"info", "warning", "error"}:
            severity = "error"
        return cls(
            code=str(payload.get("code") or "unknown"),
            severity=cast(Literal["info", "warning", "error"], severity),
            message=str(payload.get("message") or ""),
            entry_index=int(payload["entry_index"]) if payload.get("entry_index") is not None else None,
            field=str(payload.get("field") or ""),
            line_start=int(payload["line_start"]) if payload.get("line_start") is not None else None,
            line_end=int(payload["line_end"]) if payload.get("line_end") is not None else None,
        )


@dataclass(frozen=True)
class ZoteroParseStatsV1:
    detected_entries: int = 0
    parsed_entries: int = 0
    skipped_entries: int = 0
    wrapped_fields_joined: int = 0

    def to_dict(self) -> Dict[str, int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ZoteroParseStatsV1":
        return cls(
            detected_entries=int(payload.get("detected_entries") or 0),
            parsed_entries=int(payload.get("parsed_entries") or 0),
            skipped_entries=int(payload.get("skipped_entries") or 0),
            wrapped_fields_joined=int(payload.get("wrapped_fields_joined") or 0),
        )


@dataclass(frozen=True)
class ZoteroRecordV1:
    paper: PaperInfo
    field_sources: Mapping[str, Tuple[ZoteroFieldSourceV1, ...]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "paper": dict(self.paper),
            "field_sources": {
                field_name: [source.to_dict() for source in sources]
                for field_name, sources in sorted(self.field_sources.items())
            },
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ZoteroRecordV1":
        raw_sources = payload.get("field_sources") or {}
        field_sources: Dict[str, Tuple[ZoteroFieldSourceV1, ...]] = {}
        if isinstance(raw_sources, Mapping):
            for field_name, values in raw_sources.items():
                if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                    field_sources[str(field_name)] = tuple(
                        ZoteroFieldSourceV1.from_dict(value)
                        for value in values
                        if isinstance(value, Mapping)
                    )
        return cls(
            paper=cast(PaperInfo, dict(payload.get("paper") or {})),
            field_sources=field_sources,
        )


@dataclass(frozen=True)
class ZoteroParseResultV1:
    source_path: str
    report_hash: str
    status: Literal["ok", "partial", "failed"]
    parser_route: Literal["standard", "retry_key_value", "regex_fallback", "none"]
    routes_attempted: Tuple[str, ...]
    records: Tuple[ZoteroRecordV1, ...]
    diagnostics: Tuple[ZoteroParseDiagnosticV1, ...]
    stats: ZoteroParseStatsV1
    parse_confidence: float
    artifact_type: Literal["zotero_parse_result"] = "zotero_parse_result"
    artifact_version: Literal["v1"] = "v1"
    parser_version: str = PARSER_VERSION

    @property
    def papers(self) -> List[PaperInfo]:
        return [cast(PaperInfo, dict(record.paper)) for record in self.records]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "parser_version": self.parser_version,
            "source_path": self.source_path,
            "report_hash": self.report_hash,
            "status": self.status,
            "parser_route": self.parser_route,
            "routes_attempted": list(self.routes_attempted),
            "records": [record.to_dict() for record in self.records],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "stats": self.stats.to_dict(),
            "parse_confidence": self.parse_confidence,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ZoteroParseResultV1":
        status = str(payload.get("status") or "failed")
        if status not in {"ok", "partial", "failed"}:
            status = "failed"
        parser_route = str(payload.get("parser_route") or "none")
        if parser_route not in {"standard", "retry_key_value", "regex_fallback", "none"}:
            parser_route = "none"
        return cls(
            source_path=str(payload.get("source_path") or ""),
            report_hash=str(payload.get("report_hash") or ""),
            status=cast(Literal["ok", "partial", "failed"], status),
            parser_route=cast(
                Literal["standard", "retry_key_value", "regex_fallback", "none"],
                parser_route,
            ),
            routes_attempted=tuple(str(item) for item in (payload.get("routes_attempted") or [])),
            records=tuple(
                ZoteroRecordV1.from_dict(item)
                for item in (payload.get("records") or [])
                if isinstance(item, Mapping)
            ),
            diagnostics=tuple(
                ZoteroParseDiagnosticV1.from_dict(item)
                for item in (payload.get("diagnostics") or [])
                if isinstance(item, Mapping)
            ),
            stats=ZoteroParseStatsV1.from_dict(
                cast(Mapping[str, Any], payload.get("stats"))
                if isinstance(payload.get("stats"), Mapping)
                else {}
            ),
            parse_confidence=float(payload.get("parse_confidence") or 0.0),
            parser_version=str(payload.get("parser_version") or PARSER_VERSION),
        )


_FIELD_ALIASES: Dict[str, str] = {
    "item type": "item_type",
    "条目类型": "item_type",
    "author": "authors",
    "authors": "authors",
    "作者": "authors",
    "editor": "editors",
    "editors": "editors",
    "编辑": "editors",
    "title": "title",
    "标题": "title",
    "abstract": "abstract",
    "abstract note": "abstract",
    "摘要": "abstract",
    "language": "language",
    "语言": "language",
    "library catalog": "library_catalog",
    "文库编目": "library_catalog",
    "other": "other",
    "其他": "other",
    "date added": "date_added",
    "添加日期": "date_added",
    "date modified": "date_modified",
    "修改日期": "date_modified",
    "date": "date",
    "year": "year",
    "年份": "year",
    "日期": "date",
    "short title": "short_title",
    "短标题": "short_title",
    "url": "url",
    "网址": "url",
    "accessed": "access_date",
    "access date": "access_date",
    "访问时间": "access_date",
    "rights": "rights",
    "版权": "rights",
    "volume": "volume",
    "卷次": "volume",
    "pages": "pages",
    "page": "pages",
    "页码": "pages",
    "publication": "publication_title",
    "publication title": "publication_title",
    "journal": "journal",
    "期刊": "journal",
    "刊名": "publication_title",
    "doi": "doi",
    "issue": "issue",
    "期号": "issue",
    "issn": "issn",
    "attachment": "attachments",
    "attachments": "attachments",
    "附件": "attachments",
    "tag": "tags",
    "tags": "tags",
    "标签": "tags",
    "失败原因": "failure_reason",
    "failure reason": "failure_reason",
}

_REPEATED_FIELDS = {"authors", "editors", "attachments", "tags"}
_HEADER_LINES = {
    "zotero report",
    "zotero 报告",
    "report",
    "报告",
}


def _normalized_key(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _split_authors(value: str) -> List[str]:
    normalized = str(value or "").strip()
    if not normalized:
        return []
    primary_parts = [
        item.strip()
        for item in re.split(r"\s*(?:;|；|、|\band\b|&)\s*", normalized, flags=re.IGNORECASE)
        if item.strip()
    ]
    if len(primary_parts) > 1:
        return primary_parts
    comma_parts = [item.strip() for item in re.split(r"\s*,\s*", normalized) if item.strip()]
    if len(comma_parts) <= 1:
        return comma_parts
    if len(comma_parts) % 2 == 0:
        paired = [
            f"{comma_parts[index]}, {comma_parts[index + 1]}"
            for index in range(0, len(comma_parts), 2)
        ]
        if all(paired):
            return paired
    return comma_parts


def _normalize_field_value(field_name: str, parts: Sequence[str]) -> str:
    value = " ".join(part.strip() for part in parts if part.strip()).strip()
    if field_name in {"doi", "url"}:
        return re.sub(r"\s+", "", value)
    return re.sub(r"\s+", " ", value)


def _parse_known_field(line: str) -> Optional[Tuple[str, str, str]]:
    if "\t" in line:
        key, value = line.split("\t", 1)
        mapped = _FIELD_ALIASES.get(_normalized_key(key))
        if mapped:
            return mapped, key.strip(), value.strip()
    match = re.match(r"^\s*([^:：\t]{1,80})\s*[:：]\s*(.*)$", line)
    if match:
        key = match.group(1).strip()
        mapped = _FIELD_ALIASES.get(_normalized_key(key))
        if mapped:
            return mapped, key, match.group(2).strip()
    return None


def _split_star_entries(content: str) -> List[List[Tuple[int, str]]]:
    numbered = list(enumerate(content.splitlines(), start=1))
    has_star = any(line.strip() == "*" for _, line in numbered)
    if not has_star:
        return [numbered] if any("\t" in line for _, line in numbered) else []

    entries: List[List[Tuple[int, str]]] = []
    current: List[Tuple[int, str]] = []
    seen_boundary = False
    for line_number, line in numbered:
        if line.strip() == "*":
            if seen_boundary and any(value.strip() for _, value in current):
                entries.append(current)
            current = []
            seen_boundary = True
            continue
        if seen_boundary:
            current.append((line_number, line))
    if seen_boundary and any(value.strip() for _, value in current):
        entries.append(current)
    return entries


def _parse_entry_lines(
    lines: Sequence[Tuple[int, str]],
    *,
    entry_index: int,
    parser_route: str,
) -> Tuple[Optional[ZoteroRecordV1], List[ZoteroParseDiagnosticV1], int]:
    paper: Dict[str, Any] = {"authors": [], "editors": [], "tags": [], "attachments": []}
    sources: Dict[str, List[ZoteroFieldSourceV1]] = {}
    diagnostics: List[ZoteroParseDiagnosticV1] = []
    wrapped_fields_joined = 0
    current_field = ""
    current_key = ""
    current_start = 0
    current_end = 0
    current_parts: List[str] = []
    section = ""
    title_parts: List[str] = []
    title_start = 0
    title_end = 0
    saw_field = False

    def add_source(field_name: str, source_key: str, start: int, end: int) -> None:
        sources.setdefault(field_name, []).append(
            ZoteroFieldSourceV1(
                field=field_name,
                source_key=source_key,
                line_start=start,
                line_end=end,
                parser_route=parser_route,
            )
        )

    def assign(field_name: str, source_key: str, parts: Sequence[str], start: int, end: int) -> None:
        value = _normalize_field_value(field_name, parts)
        if not value:
            return
        if field_name in {"authors", "editors"}:
            values = _split_authors(value) if field_name == "authors" else [value]
            paper.setdefault(field_name, []).extend(values)
        elif field_name in {"attachments", "tags"}:
            paper.setdefault(field_name, []).append(value)
        else:
            if paper.get(field_name):
                diagnostics.append(
                    ZoteroParseDiagnosticV1(
                        code="duplicate_scalar_field",
                        severity="warning",
                        message=f"Duplicate scalar field retained from the first occurrence: {field_name}",
                        entry_index=entry_index,
                        field=field_name,
                        line_start=start,
                        line_end=end,
                    )
                )
                return
            paper[field_name] = value
            if field_name == "publication_title" and not paper.get("journal"):
                paper["journal"] = value
        add_source(field_name, source_key, start, end)

    def flush_current() -> None:
        nonlocal current_field, current_key, current_start, current_end, current_parts
        if current_field:
            assign(current_field, current_key, current_parts, current_start, current_end)
        current_field = ""
        current_key = ""
        current_start = 0
        current_end = 0
        current_parts = []

    for line_number, raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            flush_current()
            section = ""
            continue

        normalized_line = _normalized_key(stripped.rstrip(":："))
        if normalized_line in {"标签", "tags"}:
            flush_current()
            section = "tags"
            saw_field = True
            continue
        if normalized_line in {"附件", "attachments"} and not _parse_known_field(raw_line):
            flush_current()
            section = "attachments"
            saw_field = True
            continue

        field_match = _parse_known_field(raw_line)
        if field_match:
            flush_current()
            field_name, source_key, value = field_match
            current_field = field_name
            current_key = source_key
            current_start = line_number
            current_end = line_number
            current_parts = [value] if value else []
            section = ""
            saw_field = True
            continue

        bullet_match = re.match(r"^\s*(?:o|[-•])\s+(.+)$", raw_line)
        if section and bullet_match:
            assign(section, section, [bullet_match.group(1)], line_number, line_number)
            continue

        if current_field:
            current_parts.append(stripped)
            current_end = line_number
            wrapped_fields_joined += 1
            continue
        if section:
            assign(section, section, [stripped], line_number, line_number)
            continue

        if not saw_field and normalized_line not in _HEADER_LINES and not set(stripped) <= {"=", "-"}:
            if not title_start:
                title_start = line_number
            title_end = line_number
            title_parts.append(stripped)
        elif saw_field:
            diagnostics.append(
                ZoteroParseDiagnosticV1(
                    code="orphan_continuation",
                    severity="warning",
                    message="Unattached continuation line was ignored",
                    entry_index=entry_index,
                    line_start=line_number,
                    line_end=line_number,
                )
            )

    flush_current()
    if title_parts and not paper.get("title"):
        paper["title"] = _normalize_field_value("title", title_parts)
        add_source("title", "implicit_title", title_start, title_end)

    if not paper.get("title"):
        diagnostics.append(
            ZoteroParseDiagnosticV1(
                code="missing_title",
                severity="error",
                message="Entry was skipped because it has no title",
                entry_index=entry_index,
                line_start=lines[0][0] if lines else None,
                line_end=lines[-1][0] if lines else None,
            )
        )
        return None, diagnostics, wrapped_fields_joined

    return (
        ZoteroRecordV1(
            paper=cast(PaperInfo, paper),
            field_sources={key: tuple(value) for key, value in sources.items()},
        ),
        diagnostics,
        wrapped_fields_joined,
    )


def _parse_standard_records(
    content: str,
) -> Tuple[List[ZoteroRecordV1], List[ZoteroParseDiagnosticV1], ZoteroParseStatsV1]:
    entries = _split_star_entries(content)
    records: List[ZoteroRecordV1] = []
    diagnostics: List[ZoteroParseDiagnosticV1] = []
    wrapped = 0
    for entry_index, lines in enumerate(entries, start=1):
        record, entry_diagnostics, joined = _parse_entry_lines(
            lines,
            entry_index=entry_index,
            parser_route="standard",
        )
        diagnostics.extend(entry_diagnostics)
        wrapped += joined
        if record is not None:
            records.append(record)
    return records, diagnostics, ZoteroParseStatsV1(
        detected_entries=len(entries),
        parsed_entries=len(records),
        skipped_entries=max(0, len(entries) - len(records)),
        wrapped_fields_joined=wrapped,
    )


def _split_loose_blocks(content: str, *, retry: bool) -> List[Tuple[int, List[Tuple[int, str]]]]:
    lines = list(enumerate(content.splitlines(), start=1))
    blocks: List[Tuple[int, List[Tuple[int, str]]]] = []
    current: List[Tuple[int, str]] = []
    start = 1

    def flush() -> None:
        nonlocal current, start
        if any(line.strip() for _, line in current):
            blocks.append((start, current))
        current = []

    for line_number, line in lines:
        stripped = line.strip()
        is_delimiter = bool(re.fullmatch(r"(?:---+|===+)", stripped))
        if retry and is_delimiter:
            flush()
            start = line_number + 1
            continue
        if not retry and not stripped and current:
            flush()
            start = line_number + 1
            continue
        if not current:
            start = line_number
        current.append((line_number, line))
    flush()
    return blocks


def _parse_loose_records(
    content: str,
    *,
    parser_route: str,
    retry: bool,
) -> Tuple[List[ZoteroRecordV1], List[ZoteroParseDiagnosticV1], ZoteroParseStatsV1]:
    blocks = _split_loose_blocks(content, retry=retry)
    records: List[ZoteroRecordV1] = []
    diagnostics: List[ZoteroParseDiagnosticV1] = []
    wrapped = 0
    for _start, lines in blocks:
        meaningful = [
            item
            for item in lines
            if _parse_known_field(item[1])
            or (
                item[1].strip()
                and _normalized_key(item[1].strip()) not in _HEADER_LINES
                and "生成时间" not in item[1]
                and "失败论文重跑报告" not in item[1]
                and not set(item[1].strip()) <= {"=", "-"}
            )
        ]
        if not meaningful:
            continue
        record, entry_diagnostics, joined = _parse_entry_lines(
            meaningful,
            entry_index=len(records) + 1,
            parser_route=parser_route,
        )
        diagnostics.extend(entry_diagnostics)
        wrapped += joined
        if record is not None:
            records.append(record)
    detected = len(records) + sum(1 for item in diagnostics if item.code == "missing_title")
    return records, diagnostics, ZoteroParseStatsV1(
        detected_entries=detected,
        parsed_entries=len(records),
        skipped_entries=max(0, detected - len(records)),
        wrapped_fields_joined=wrapped,
    )


def _generic_records(papers: Iterable[PaperInfo], parser_route: str) -> List[ZoteroRecordV1]:
    return [
        ZoteroRecordV1(
            paper=cast(PaperInfo, dict(paper)),
            field_sources={
                key: (
                    ZoteroFieldSourceV1(
                        field=key,
                        source_key="compatibility_projection",
                        line_start=0,
                        line_end=0,
                        parser_route=parser_route,
                    ),
                )
                for key, value in paper.items()
                if value not in (None, "", [], {})
            },
        )
        for paper in papers
    ]


def _failed_result(
    *,
    source_path: str,
    report_hash: str,
    code: str,
    message: str,
    routes_attempted: Sequence[str] = (),
) -> ZoteroParseResultV1:
    return ZoteroParseResultV1(
        source_path=source_path,
        report_hash=report_hash,
        status="failed",
        parser_route="none",
        routes_attempted=tuple(routes_attempted),
        records=(),
        diagnostics=(
            ZoteroParseDiagnosticV1(code=code, severity="error", message=message),
        ),
        stats=ZoteroParseStatsV1(),
        parse_confidence=0.0,
    )


def _result_from_records(
    *,
    source_path: str,
    report_hash: str,
    parser_route: Literal["standard", "retry_key_value", "regex_fallback"],
    routes_attempted: Sequence[str],
    records: Sequence[ZoteroRecordV1],
    diagnostics: Sequence[ZoteroParseDiagnosticV1],
    stats: ZoteroParseStatsV1,
    confidence: float,
) -> ZoteroParseResultV1:
    has_problem = stats.skipped_entries > 0 or any(
        diagnostic.severity in {"warning", "error"} for diagnostic in diagnostics
    )
    return ZoteroParseResultV1(
        source_path=source_path,
        report_hash=report_hash,
        status="partial" if has_problem else "ok",
        parser_route=parser_route,
        routes_attempted=tuple(routes_attempted),
        records=tuple(records),
        diagnostics=tuple(diagnostics),
        stats=stats,
        parse_confidence=max(0.0, min(1.0, confidence - (0.15 if has_problem else 0.0))),
    )


def parse_zotero_report_result(filepath: str) -> ZoteroParseResultV1:
    """Parse a report into a versioned result with provenance and diagnostics."""

    if not filepath:
        return _failed_result(
            source_path="",
            report_hash="",
            code="invalid_source_path",
            message="A non-empty Zotero report path is required",
        )
    source = Path(filepath).expanduser().resolve()
    source_path = str(source)
    if not source.exists():
        return _failed_result(
            source_path=source_path,
            report_hash="",
            code="source_missing",
            message=f"Zotero report does not exist: {source_path}",
        )
    try:
        raw_bytes = source.read_bytes()
        report_hash = hashlib.sha256(raw_bytes).hexdigest()
        content = read_text_file_with_fallbacks(source_path, logger=logger)
    except Exception as exc:
        return _failed_result(
            source_path=source_path,
            report_hash="",
            code="source_read_failed",
            message=f"Unable to read Zotero report: {exc}",
        )
    if not content or not content.strip():
        return _failed_result(
            source_path=source_path,
            report_hash=report_hash,
            code="empty_source",
            message="Zotero report is empty",
        )

    routes_attempted: List[str] = []
    retry_format = "失败论文重跑报告" in content or "failed paper retry report" in content.casefold()
    if retry_format or ("---" in content and re.search(r"(?m)^(?:标题|Title)\s*[:：]", content)):
        routes_attempted.append("retry_key_value")
        papers = parse_simple_key_value_format(content)
        records, diagnostics, stats = _parse_loose_records(
            content,
            parser_route="retry_key_value",
            retry=True,
        )
        if papers:
            if [dict(record.paper) for record in records] != [dict(paper) for paper in papers]:
                records = _generic_records(papers, "retry_key_value")
                stats = ZoteroParseStatsV1(
                    detected_entries=len(records),
                    parsed_entries=len(records),
                )
            return _result_from_records(
                source_path=source_path,
                report_hash=report_hash,
                parser_route="retry_key_value",
                routes_attempted=routes_attempted,
                records=records,
                diagnostics=diagnostics,
                stats=stats,
                confidence=0.9,
            )

    looks_standard = bool(re.search(r"(?m)^\s*\*\s*$", content)) or any(
        "\t" in line for line in content.splitlines()
    )
    if looks_standard:
        routes_attempted.append("standard")
        papers = parse_standard_zotero_format(content)
        records, diagnostics, stats = _parse_standard_records(content)
        if papers:
            if [dict(record.paper) for record in records] != [dict(paper) for paper in papers]:
                records = _generic_records(papers, "standard")
                stats = ZoteroParseStatsV1(
                    detected_entries=len(records),
                    parsed_entries=len(records),
                )
            return _result_from_records(
                source_path=source_path,
                report_hash=report_hash,
                parser_route="standard",
                routes_attempted=routes_attempted,
                records=records,
                diagnostics=diagnostics,
                stats=stats,
                confidence=0.97,
            )

    routes_attempted.append("regex_fallback")
    papers = parse_with_regex(content)
    records, diagnostics, stats = _parse_loose_records(
        content,
        parser_route="regex_fallback",
        retry=False,
    )
    if papers:
        if [dict(record.paper) for record in records] != [dict(paper) for paper in papers]:
            records = _generic_records(papers, "regex_fallback")
            stats = ZoteroParseStatsV1(
                detected_entries=len(records),
                parsed_entries=len(records),
            )
        return _result_from_records(
            source_path=source_path,
            report_hash=report_hash,
            parser_route="regex_fallback",
            routes_attempted=routes_attempted,
            records=records,
            diagnostics=diagnostics,
            stats=stats,
            confidence=0.65,
        )

    return _failed_result(
        source_path=source_path,
        report_hash=report_hash,
        code="unknown_format",
        message="No supported Zotero report format produced a valid entry",
        routes_attempted=routes_attempted,
    )


def parse_zotero_report(filepath: str) -> List[PaperInfo]:
    """Legacy compatibility projection of :func:`parse_zotero_report_result`."""

    result = parse_zotero_report_result(filepath)
    for diagnostic in result.diagnostics:
        log = logger.error if diagnostic.severity == "error" else logger.warning
        log("Zotero parse diagnostic [%s]: %s", diagnostic.code, diagnostic.message)
    return result.papers


def parse_standard_zotero_format(content: str) -> List[PaperInfo]:
    if not content:
        return []
    records, _diagnostics, _stats = _parse_standard_records(content)
    return [cast(PaperInfo, dict(record.paper)) for record in records]


def parse_simple_key_value_format(content: str) -> List[PaperInfo]:
    if not content:
        return []
    records, _diagnostics, _stats = _parse_loose_records(
        content,
        parser_route="retry_key_value",
        retry=True,
    )
    return [cast(PaperInfo, dict(record.paper)) for record in records]


def parse_with_regex(content: str) -> List[PaperInfo]:
    """Conservative fallback for colon-delimited exports without Zotero markers."""

    if not content:
        return []
    records, _diagnostics, _stats = _parse_loose_records(
        content,
        parser_route="regex_fallback",
        retry=False,
    )
    return [cast(PaperInfo, dict(record.paper)) for record in records]


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        parsed = parse_zotero_report_result(sys.argv[1])
        logger.info("Parsed %s Zotero records with status %s", len(parsed.records), parsed.status)
    else:
        logger.info("Usage: python zotero_parser.py <zotero-report-path>")

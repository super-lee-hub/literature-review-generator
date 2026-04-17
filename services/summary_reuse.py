from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence

from services.paper_identity import (
    build_paper_key,
    normalize_doi,
    normalized_author_surnames,
    normalized_title_key,
)
from services.text_io import load_json_file_with_fallbacks


class SummarySourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class SummarySource:
    path: str
    source_type: str
    priority: int
    label: str


@dataclass(frozen=True)
class SummaryRecord:
    summary: Dict[str, Any]
    source: SummarySource
    record_index: int
    title: str
    doi: str
    canonical_paper_key: str
    title_author_year_key: str

    @property
    def unique_identity(self) -> str:
        return self.doi or self.canonical_paper_key or self.title_author_year_key or f"{self.source.path}#{self.record_index}"


@dataclass(frozen=True)
class SummaryCandidate:
    summary: Dict[str, Any]
    source: SummarySource


@dataclass(frozen=True)
class SummaryMatch:
    match_type: str
    winner: Optional[SummaryRecord]
    ambiguous_candidates: tuple[SummaryRecord, ...] = ()

    @property
    def is_ambiguous(self) -> bool:
        return bool(self.ambiguous_candidates)


@dataclass(frozen=True)
class ResolvedSummarySet:
    summaries: List[Dict[str, Any]]
    source_items: List[Dict[str, Any]]
    rejected_candidates: List[Dict[str, Any]]


def load_summary_records(path: str | Path, *, logger: Any | None = None) -> List[Dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        raise SummarySourceError(f"Summary file not found: {target}")

    payload = load_json_file_with_fallbacks(target, logger=logger)
    if not isinstance(payload, list):
        raise SummarySourceError(f"Summary file must contain a JSON list: {target}")

    normalized: List[Dict[str, Any]] = []
    for item in payload:
        if isinstance(item, Mapping):
            normalized.append(dict(item))
    return normalized


def _safe_resolve(path: Path) -> Path | None:
    try:
        return path.expanduser().resolve()
    except OSError:
        return None


def _sorted_existing_paths(paths: Iterable[Path]) -> List[Path]:
    existing = [path for path in paths if path.exists()]
    return sorted(existing, key=lambda item: item.stat().st_mtime, reverse=True)


def collect_summary_sources(
    *,
    explicit_paths: Sequence[str] | None,
    output_root: str | Path | None,
    current_workspace_root: str | None,
    current_summary_file: str | None,
) -> List[SummarySource]:
    sources: List[SummarySource] = []
    seen_paths: set[str] = set()

    def _append_source(path: Path, *, source_type: str, priority: int, label: str) -> None:
        resolved = _safe_resolve(path)
        if resolved is None:
            return
        if current_summary and resolved == current_summary:
            return
        if current_workspace and current_workspace in resolved.parents:
            return
        resolved_str = str(resolved)
        if resolved_str in seen_paths:
            return
        seen_paths.add(resolved_str)
        sources.append(
            SummarySource(
                path=resolved_str,
                source_type=source_type,
                priority=priority,
                label=label,
            )
        )

    current_workspace = _safe_resolve(Path(current_workspace_root)) if current_workspace_root else None
    current_summary = _safe_resolve(Path(current_summary_file)) if current_summary_file else None

    for index, raw_path in enumerate(explicit_paths or []):
        raw_text = str(raw_path or "").strip()
        if not raw_text:
            continue
        _append_source(
            Path(raw_text),
            source_type="explicit",
            priority=index,
            label=f"explicit:{index + 1}",
        )

    if output_root:
        output_dir = _safe_resolve(Path(output_root))
        if output_dir and output_dir.exists():
            workspace_paths = _sorted_existing_paths(output_dir.rglob("artifacts/*_summaries.json"))
            auto_priority = len(sources)
            for offset, summary_path in enumerate(workspace_paths, start=auto_priority):
                _append_source(
                    summary_path,
                    source_type="workspace",
                    priority=offset,
                    label=summary_path.parent.parent.name,
                )

            legacy_paths = _sorted_existing_paths(output_dir.glob("*/*_summaries.json"))
            legacy_priority = len(sources)
            for offset, summary_path in enumerate(legacy_paths, start=legacy_priority):
                _append_source(
                    summary_path,
                    source_type="legacy_output",
                    priority=offset,
                    label=summary_path.parent.name,
                )

    return sources


def _summary_paper_info(summary: Mapping[str, Any]) -> Mapping[str, Any]:
    paper_info = summary.get("paper_info")
    if isinstance(paper_info, Mapping):
        return paper_info

    ai_summary = summary.get("ai_summary")
    if isinstance(ai_summary, Mapping):
        paper_metadata = ai_summary.get("paper_metadata")
        if isinstance(paper_metadata, Mapping):
            return paper_metadata
    return {}


def summary_success(summary: Mapping[str, Any]) -> bool:
    return str(summary.get("status") or "").strip().lower() == "success"


def summary_canonical_doi(summary: Mapping[str, Any]) -> str:
    paper_info = _summary_paper_info(summary)
    doi = normalize_doi(paper_info.get("doi"))
    if doi:
        return doi

    ai_summary = summary.get("ai_summary")
    if isinstance(ai_summary, Mapping):
        paper_metadata = ai_summary.get("paper_metadata")
        if isinstance(paper_metadata, Mapping):
            return normalize_doi(paper_metadata.get("doi"))
    return ""


def summary_canonical_paper_key(summary: Mapping[str, Any]) -> str:
    paper_info = dict(_summary_paper_info(summary))
    explicit_key = str(
        paper_info.get("canonical_paper_key")
        or paper_info.get("source_paper_id")
        or ""
    ).strip()
    if explicit_key:
        normalized_doi = normalize_doi(explicit_key)
        return normalized_doi or explicit_key
    return build_paper_key(paper_info)


def title_author_year_key_from_paper(paper: Mapping[str, Any]) -> str:
    title_key = normalized_title_key(paper.get("title"))
    author_surnames = normalized_author_surnames(paper.get("authors"))
    year = str(paper.get("year") or "").strip()
    if not title_key or title_key == "unknown_title" or not author_surnames or not year:
        return ""
    return f"{title_key}|{author_surnames[0]}|{year.casefold()}"


def summary_title_author_year_key(summary: Mapping[str, Any]) -> str:
    return title_author_year_key_from_paper(_summary_paper_info(summary))


def summary_title(summary: Mapping[str, Any]) -> str:
    paper_info = _summary_paper_info(summary)
    return str(paper_info.get("title") or "").strip()


def paper_canonical_paper_key(paper: Mapping[str, Any]) -> str:
    explicit_key = str(
        paper.get("canonical_paper_key")
        or paper.get("source_paper_id")
        or ""
    ).strip()
    if explicit_key:
        normalized_doi = normalize_doi(explicit_key)
        return normalized_doi or explicit_key
    return build_paper_key(paper)


def describe_summary_candidate(summary: Mapping[str, Any]) -> Dict[str, Any]:
    paper_info = dict(_summary_paper_info(summary))
    return {
        "title": str(paper_info.get("title") or ""),
        "doi": normalize_doi(paper_info.get("doi")),
        "paper_key": paper_canonical_paper_key(paper_info),
        "title_author_year_key": title_author_year_key_from_paper(paper_info),
    }


class SummaryCatalog:
    def __init__(
        self,
        *,
        sources: Sequence[SummarySource],
        records: Sequence[SummaryRecord],
        rejected_candidates: Sequence[Dict[str, Any]],
    ) -> None:
        self.sources = list(sources)
        self.records = list(records)
        self.rejected_candidates = list(rejected_candidates)
        self._by_doi: dict[str, list[SummaryRecord]] = defaultdict(list)
        self._by_paper_key: dict[str, list[SummaryRecord]] = defaultdict(list)
        self._by_title_author_year: dict[str, list[SummaryRecord]] = defaultdict(list)

        for record in self.records:
            if record.doi:
                self._by_doi[record.doi].append(record)
            if record.canonical_paper_key:
                self._by_paper_key[record.canonical_paper_key].append(record)
            if record.title_author_year_key:
                self._by_title_author_year[record.title_author_year_key].append(record)

    @classmethod
    def from_sources(
        cls,
        sources: Sequence[SummarySource],
        *,
        logger: Any | None = None,
    ) -> "SummaryCatalog":
        records: List[SummaryRecord] = []
        rejected: List[Dict[str, Any]] = []

        for source in sources:
            try:
                source_records = load_summary_records(source.path, logger=logger)
            except SummarySourceError as exc:
                if source.source_type == "explicit":
                    raise
                rejected.append(
                    {
                        "path": source.path,
                        "source_type": source.source_type,
                        "reason": str(exc),
                    }
                )
                continue

            for index, summary in enumerate(source_records):
                if not summary_success(summary):
                    rejected.append(
                        {
                            "path": source.path,
                            "source_type": source.source_type,
                            "record_index": index,
                            "reason": "summary_status_not_success",
                        }
                    )
                    continue

                records.append(
                    SummaryRecord(
                        summary=dict(summary),
                        source=source,
                        record_index=index,
                        title=summary_title(summary),
                        doi=summary_canonical_doi(summary),
                        canonical_paper_key=summary_canonical_paper_key(summary),
                        title_author_year_key=summary_title_author_year_key(summary),
                    )
                )

        return cls(sources=sources, records=records, rejected_candidates=rejected)

    @staticmethod
    def _sort_key(record: SummaryRecord) -> tuple[int, int, str]:
        return (record.source.priority, record.record_index, record.source.path)

    def _collapse_candidates(self, records: Sequence[SummaryRecord]) -> List[SummaryRecord]:
        winners: Dict[str, SummaryRecord] = {}
        for record in records:
            key = record.unique_identity
            current = winners.get(key)
            if current is None or self._sort_key(record) < self._sort_key(current):
                winners[key] = record
        return sorted(winners.values(), key=self._sort_key)

    def resolve_for_paper(self, paper: Mapping[str, Any]) -> Optional[SummaryMatch]:
        matchers = [
            ("doi_exact", normalize_doi(paper.get("doi")), self._by_doi),
            ("canonical_paper_key_exact", paper_canonical_paper_key(paper), self._by_paper_key),
            ("title_author_year_exact", title_author_year_key_from_paper(paper), self._by_title_author_year),
        ]
        for match_type, key, index in matchers:
            if not key:
                continue
            collapsed = self._collapse_candidates(index.get(key, ()))
            if not collapsed:
                continue
            if len(collapsed) == 1:
                return SummaryMatch(match_type=match_type, winner=collapsed[0])
            return SummaryMatch(
                match_type=match_type,
                winner=collapsed[0],
                ambiguous_candidates=tuple(collapsed),
            )
        return None

    def build_effective_summary_set(self) -> ResolvedSummarySet:
        winners: Dict[str, SummaryRecord] = {}
        for record in sorted(self.records, key=self._sort_key):
            winners.setdefault(record.unique_identity, record)

        summaries = [deepcopy(record.summary) for record in sorted(winners.values(), key=self._sort_key)]
        source_items = [
            {
                "path": source.path,
                "source_type": source.source_type,
                "label": source.label,
                "priority": source.priority,
            }
            for source in self.sources
        ]
        return ResolvedSummarySet(
            summaries=summaries,
            source_items=source_items,
            rejected_candidates=list(self.rejected_candidates),
        )


def index_reusable_summaries(
    sources: Sequence[SummarySource],
    *,
    logger: Any | None = None,
) -> tuple[Dict[str, SummaryCandidate], List[Dict[str, Any]]]:
    catalog = SummaryCatalog.from_sources(sources, logger=logger)
    reusable: Dict[str, SummaryCandidate] = {}
    rejected = list(catalog.rejected_candidates)

    for record in sorted(catalog.records, key=SummaryCatalog._sort_key):
        if not record.doi:
            rejected.append(
                {
                    "path": record.source.path,
                    "source_type": record.source.source_type,
                    "record_index": record.record_index,
                    "reason": "missing_doi",
                }
            )
            continue

        if record.doi not in reusable:
            reusable[record.doi] = SummaryCandidate(summary=dict(record.summary), source=record.source)
            continue

        rejected.append(
            {
                "path": record.source.path,
                "source_type": record.source.source_type,
                "record_index": record.record_index,
                "reason": "duplicate_doi_loser",
                "doi": record.doi,
                "winner_path": reusable[record.doi].source.path,
            }
        )

    return reusable, rejected


def build_effective_summary_set(
    sources: Sequence[SummarySource],
    *,
    logger: Any | None = None,
) -> ResolvedSummarySet:
    catalog = SummaryCatalog.from_sources(sources, logger=logger)
    return catalog.build_effective_summary_set()


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def build_reused_summary(
    *,
    current_paper: Mapping[str, Any],
    matched_summary: Mapping[str, Any],
    reuse_source: SummarySource,
    match_type: str,
    canonical_doi: str = "",
) -> Dict[str, Any]:
    summary_payload = deepcopy(dict(matched_summary))
    source_paper_info = dict(_summary_paper_info(matched_summary))
    merged_paper_info: MutableMapping[str, Any] = dict(source_paper_info)

    for key, value in dict(current_paper).items():
        if key in {
            "pdf_path",
            "source_mode",
            "source_paper_id",
            "source_descriptor",
            "source_pdf",
            "source_pdf_fingerprint",
            "canonical_paper_key",
            "paper_key_aliases",
            "metadata_confidence",
            "metadata_source_priority_snapshot",
        }:
            merged_paper_info[key] = value
        elif _has_value(value):
            merged_paper_info[key] = value

    resolved_doi = canonical_doi or normalize_doi(merged_paper_info.get("doi")) or normalize_doi(current_paper.get("doi"))
    if resolved_doi:
        merged_paper_info["doi"] = resolved_doi

    merged_paper_info.setdefault("canonical_paper_key", paper_canonical_paper_key(merged_paper_info))

    summary_payload["paper_info"] = dict(merged_paper_info)
    summary_payload["status"] = "success"
    summary_payload["source_mode"] = str(
        current_paper.get("source_mode")
        or matched_summary.get("source_mode")
        or merged_paper_info.get("source_mode")
        or ""
    )
    summary_payload["processing_time"] = str(matched_summary.get("processing_time") or "reused")
    summary_payload["text_length"] = int(matched_summary.get("text_length") or 0)
    summary_payload["preprocess"] = dict(matched_summary.get("preprocess") or {})
    summary_payload["stage1_input"] = {
        "input_mode": "reused_summary",
        "fallback_reason": match_type,
        "selected_visual_refs": [],
        "visual_manifest_path": "",
        "visual_bundle_path": "",
        "visual_selection_policy_snapshot": {},
        "multimodal_capability": {},
        "reuse_source_path": reuse_source.path,
        "reuse_source_type": reuse_source.source_type,
    }
    summary_payload["reuse_metadata"] = {
        "reused": True,
        "reason": match_type,
        "canonical_doi": resolved_doi,
        "source_path": reuse_source.path,
        "source_type": reuse_source.source_type,
        "source_label": reuse_source.label,
        "match_type": match_type,
    }
    return summary_payload

"""Build the corrected 84-paper PPH Stage 1 provenance crosswalk.

This command is provider-free. It combines the frozen selected-source
manifest, the corrected registered-summary coverage audit, and narrowly
validated provenance rebinds. It never generates a paper summary.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import html
import json
from pathlib import Path
import re
import sys
import unicodedata
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime.reconcile import validate_canonical_ai_summary
from services.artifact_registry import file_sha256
from services.job_workspace import atomic_write_json, utc_now_iso
from services.paper_identity import normalize_doi
from services.summary_reuse import (
    load_summary_records,
    summary_pdf_sha256,
    summary_zotero_parent_key,
)


EXPECTED_CORPUS_COUNT = 84
EXPECTED_ORIGINAL_MISSING_COUNT = 44
ALLOWED_DISPOSITIONS = frozenset(
    {
        "reusable_exact",
        "provenance_rebind",
        "ambiguous_manual",
        "truly_new",
    }
)
MATCH_PRIORITY = {
    "pdf_sha256_exact": 0,
    "doi_exact": 1,
    "zotero_parent_key_exact": 2,
    "title_author_year_exact": 3,
}
BEYOND_SKEPTICISM_KEY = "X9MQPVXA"
POLLUTED_PROVENANCE_KEYS = frozenset({"AWB2RFQK", "PKU6G8KZ", "4ZIFASZU"})


class CrosswalkError(RuntimeError):
    """Raised when the crosswalk cannot close without weakening identity."""


def _read_json(path: str | Path) -> Any:
    target = Path(path).expanduser().resolve()
    try:
        return json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CrosswalkError(f"cannot read JSON: {target}") from exc


def _normalize_title(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"[^\w\s]", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _year(value: Any) -> str:
    match = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return match.group(0) if match else ""


def _author_aliases(value: Any) -> set[str]:
    authors = value if isinstance(value, list) else ([value] if value else [])
    aliases: set[str] = set()
    for author in authors:
        normalized = unicodedata.normalize("NFKC", str(author or "")).casefold()
        parts = [
            re.sub(r"[^\w]", "", part, flags=re.UNICODE)
            for part in re.split(r"[\s,，]+", normalized)
            if part
        ]
        parts = [part for part in parts if part]
        if not parts:
            continue
        aliases.add("".join(parts))
        aliases.add("".join(reversed(parts)))
    return aliases


def _paper_objects(summary: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    objects: list[Mapping[str, Any]] = []
    paper_info = summary.get("paper_info")
    if isinstance(paper_info, Mapping):
        objects.append(paper_info)
    ai_summary = summary.get("ai_summary")
    if isinstance(ai_summary, Mapping):
        metadata = ai_summary.get("paper_metadata")
        if isinstance(metadata, Mapping):
            objects.append(metadata)
    return objects


def _summary_identity(summary: Mapping[str, Any]) -> dict[str, Any]:
    objects = _paper_objects(summary)
    return {
        "pdf_sha256": summary_pdf_sha256(summary),
        "zotero_key": summary_zotero_parent_key(summary),
        "dois": {
            normalized
            for item in objects
            for normalized in (normalize_doi(item.get("doi")),)
            if normalized
        },
        "titles": {
            normalized
            for item in objects
            for normalized in (_normalize_title(item.get("title")),)
            if normalized
        },
        "authors": set().union(
            *(_author_aliases(item.get("authors")) for item in objects)
        ),
        "years": {
            normalized
            for item in objects
            for normalized in (_year(item.get("year") or item.get("date")),)
            if normalized
        },
    }


def _row_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "pdf_sha256": str(row.get("pdf_sha256") or "").strip().casefold(),
        "zotero_key": str(row.get("zotero_parent_key") or "").strip().upper(),
        "doi": normalize_doi(row.get("doi")),
        "title": _normalize_title(row.get("title")),
        "authors": _author_aliases(row.get("authors")),
        "year": _year(row.get("year")),
    }


def _match_types(
    summary_identity: Mapping[str, Any],
    row: Mapping[str, Any],
) -> list[str]:
    current = _row_identity(row)
    matches: list[str] = []
    if (
        summary_identity["pdf_sha256"]
        and summary_identity["pdf_sha256"] == current["pdf_sha256"]
    ):
        matches.append("pdf_sha256_exact")
    if current["doi"] and current["doi"] in summary_identity["dois"]:
        matches.append("doi_exact")
    if (
        summary_identity["zotero_key"]
        and summary_identity["zotero_key"] == current["zotero_key"]
    ):
        matches.append("zotero_parent_key_exact")
    if (
        current["title"] in summary_identity["titles"]
        and current["year"] in summary_identity["years"]
        and bool(current["authors"] & summary_identity["authors"])
    ):
        matches.append("title_author_year_exact")
    return matches


def _load_selected_rows(path: Path) -> list[dict[str, Any]]:
    payload = _read_json(path)
    if not isinstance(payload, Mapping):
        raise CrosswalkError("selected source manifest is not an object")
    rows = payload.get("selected_sources")
    if not isinstance(rows, list) or len(rows) != EXPECTED_CORPUS_COUNT:
        raise CrosswalkError(
            f"selected source manifest must contain {EXPECTED_CORPUS_COUNT} rows"
        )
    normalized = [dict(row) for row in rows if isinstance(row, Mapping)]
    if len(normalized) != EXPECTED_CORPUS_COUNT:
        raise CrosswalkError("selected source manifest contains a non-object row")
    keys = [str(row.get("zotero_parent_key") or "") for row in normalized]
    if len(set(keys)) != EXPECTED_CORPUS_COUNT or any(not key for key in keys):
        raise CrosswalkError("selected source manifest Zotero keys are not unique")
    return sorted(normalized, key=lambda row: int(row.get("source_order") or 0))


def _load_registered_coverage(
    path: Path,
    selected_manifest: Path,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    payload = _read_json(path)
    if not isinstance(payload, Mapping):
        raise CrosswalkError("registered coverage audit is not an object")
    if int(payload.get("expected_corpus_count") or 0) != EXPECTED_CORPUS_COUNT:
        raise CrosswalkError("registered coverage audit has the wrong corpus count")
    if str(payload.get("selected_manifest_sha256") or "") != file_sha256(
        selected_manifest
    ):
        raise CrosswalkError("registered coverage audit is stale")
    covered = payload.get("covered")
    if not isinstance(covered, list):
        raise CrosswalkError("registered coverage audit has no covered list")
    covered_by_key = {
        str(item.get("canonical_paper_key") or ""): dict(item)
        for item in covered
        if isinstance(item, Mapping)
    }
    if len(covered_by_key) != int(payload.get("covered_count") or -1):
        raise CrosswalkError("registered coverage covered rows are inconsistent")
    return dict(payload), covered_by_key


def _summary_record(path: str | Path, index: int) -> dict[str, Any]:
    records = load_summary_records(str(Path(path).expanduser().resolve()))
    try:
        summary = records[index]
    except IndexError as exc:
        raise CrosswalkError(f"summary record index is outside file: {path}#{index}") from exc
    if not isinstance(summary, Mapping):
        raise CrosswalkError(f"summary record is not an object: {path}#{index}")
    if str(summary.get("status") or "").strip().casefold() != "success":
        raise CrosswalkError(f"summary record is not successful: {path}#{index}")
    validate_canonical_ai_summary(
        summary.get("ai_summary"),
        label=f"historical summary {path}#{index}",
    )
    return dict(summary)


def _find_unique_master_record(
    master_path: Path,
    row: Mapping[str, Any],
) -> tuple[int, dict[str, Any]]:
    records = load_summary_records(str(master_path))
    matches: list[tuple[int, dict[str, Any]]] = []
    for index, summary in enumerate(records):
        if not isinstance(summary, Mapping):
            continue
        if "title_author_year_exact" in _match_types(_summary_identity(summary), row):
            matches.append((index, dict(summary)))
    if len(matches) != 1:
        raise CrosswalkError(
            f"expected one title-author-year master match for "
            f"{row.get('zotero_parent_key')}, got {len(matches)}"
        )
    index, summary = matches[0]
    validate_canonical_ai_summary(
        summary.get("ai_summary"),
        label=f"provenance rebind {master_path}#{index}",
    )
    return index, summary


def _word_tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.findall(r"[\w]+", normalized, flags=re.UNICODE)


def _word_ngrams(text: str, size: int) -> set[str]:
    tokens = _word_tokens(text)
    return {
        " ".join(tokens[index : index + size])
        for index in range(max(0, len(tokens) - size + 1))
    }


def _pdf_evidence(path: Path) -> dict[str, Any]:
    try:
        import fitz
    except ImportError as exc:
        raise CrosswalkError("PyMuPDF is required for provenance adjudication") from exc
    try:
        document = fitz.open(path)
        text = "\n".join(page.get_text("text") for page in document)
        page_count = document.page_count
    except Exception as exc:
        raise CrosswalkError(f"cannot inspect PDF: {path}") from exc
    compact = unicodedata.normalize("NFKC", text).casefold()
    compact = re.sub(r"\s+", "", compact)
    compact = compact.replace("．", ".").replace("／", "/").replace("－", "-")
    doi_candidates = sorted(
        {
            match.rstrip(".,;)")
            for match in re.findall(
                r"10\.\d{4,9}/[-._;()/:a-z0-9&]+",
                compact,
                flags=re.IGNORECASE,
            )
        }
    )
    return {
        "path": str(path.resolve()),
        "sha256": file_sha256(path),
        "page_count": page_count,
        "text": text,
        "text_chars": len(text),
        "doi_candidates": doi_candidates,
    }


def _content_overlap(
    current_text: str,
    historical_text: str,
    *,
    ngram_size: int,
) -> dict[str, Any]:
    current = _word_ngrams(current_text, ngram_size)
    historical = _word_ngrams(historical_text, ngram_size)
    intersection = current & historical
    union = current | historical
    return {
        "ngram_size": ngram_size,
        "current_ngrams": len(current),
        "historical_ngrams": len(historical),
        "intersection": len(intersection),
        "jaccard": len(intersection) / len(union) if union else 1.0,
        "current_containment": len(intersection) / len(current) if current else 1.0,
        "historical_containment": (
            len(intersection) / len(historical) if historical else 1.0
        ),
    }


def _historical_pdf_path(summary: Mapping[str, Any]) -> Path:
    for item in _paper_objects(summary):
        for key in ("source_pdf", "pdf_path"):
            raw = str(item.get(key) or "").strip()
            if raw and Path(raw).is_file():
                return Path(raw).resolve()
    raise CrosswalkError("historical summary does not reference a readable PDF")


def _adjudicate_beyond(
    row: Mapping[str, Any],
    summary: Mapping[str, Any],
    current_pdf: Path,
) -> dict[str, Any]:
    historical_pdf = _historical_pdf_path(summary)
    current = _pdf_evidence(current_pdf)
    historical = _pdf_evidence(historical_pdf)
    overlap = _content_overlap(
        current["text"],
        historical["text"],
        ngram_size=8,
    )
    same_identity = "zotero_parent_key_exact" in _match_types(
        _summary_identity(summary),
        row,
    )
    equivalent = (
        same_identity
        and current["page_count"] == historical["page_count"]
        and overlap["historical_containment"] >= 0.95
    )
    if not equivalent:
        raise CrosswalkError("Beyond Skepticism PDF versions are not equivalent")
    return {
        "historical_pdf_path": historical["path"],
        "historical_pdf_sha256": historical["sha256"],
        "current_pdf_page_count": current["page_count"],
        "historical_pdf_page_count": historical["page_count"],
        "body_overlap": overlap,
        "adjudication": "same_paper_content_equivalent_pdf_version",
    }


def _adjudicate_polluted_record(
    row: Mapping[str, Any],
    summary: Mapping[str, Any],
    current_pdf: Path,
) -> dict[str, Any]:
    current = _pdf_evidence(current_pdf)
    paper_info = summary.get("paper_info")
    paper_info = paper_info if isinstance(paper_info, Mapping) else {}
    historical_text = str(paper_info.get("doi") or "")
    if len(historical_text) < 1_000:
        raise CrosswalkError("polluted provenance record has no retained source text")

    overlap = _content_overlap(
        current["text"],
        historical_text,
        ngram_size=4,
    )
    identity = _summary_identity(summary)
    title_author_year = "title_author_year_exact" in _match_types(identity, row)
    ai_dois = sorted(identity["dois"])
    doi_confirmed = bool(set(ai_dois) & set(current["doi_candidates"]))
    content_confirmed = max(
        overlap["current_containment"],
        overlap["historical_containment"],
    ) >= 0.50
    if not title_author_year or not (doi_confirmed or content_confirmed):
        raise CrosswalkError(
            f"polluted provenance record could not be rebound: "
            f"{row.get('zotero_parent_key')}"
        )
    return {
        "historical_pdf_path": "",
        "historical_pdf_sha256": "",
        "historical_source_text_sha256": hashlib.sha256(
            historical_text.encode("utf-8")
        ).hexdigest(),
        "historical_source_text_chars": len(historical_text),
        "current_pdf_page_count": current["page_count"],
        "current_pdf_doi_candidates": current["doi_candidates"],
        "historical_ai_dois": ai_dois,
        "doi_confirmed": doi_confirmed,
        "body_overlap": overlap,
        "adjudication": "title_authors_year_and_source_text_confirm_same_paper",
    }


def _discover_history(
    output_root: Path,
) -> tuple[list[Path], list[tuple[Path, int, dict[str, Any], dict[str, Any]]], list[dict[str, str]]]:
    files = sorted(
        {path.resolve() for path in output_root.rglob("*summaries.json") if path.is_file()},
        key=lambda path: str(path).casefold(),
    )
    records: list[tuple[Path, int, dict[str, Any], dict[str, Any]]] = []
    errors: list[dict[str, str]] = []
    for path in files:
        try:
            summaries = load_summary_records(str(path))
        except Exception as exc:
            errors.append(
                {
                    "path": str(path),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        for index, summary in enumerate(summaries):
            if not isinstance(summary, Mapping):
                continue
            if str(summary.get("status") or "").strip().casefold() != "success":
                continue
            record = dict(summary)
            records.append((path, index, record, _summary_identity(record)))
    return files, records, errors


def _full_history_match_stats(
    rows: Sequence[Mapping[str, Any]],
    history_records: Sequence[tuple[Path, int, dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    no_match: list[dict[str, Any]] = []
    matched_count = 0
    row_match_shapes: Counter[str] = Counter()
    for row in rows:
        match_types: set[str] = set()
        for _, _, _, identity in history_records:
            match_types.update(_match_types(identity, row))
        if match_types:
            matched_count += 1
            row_match_shapes["+".join(sorted(match_types, key=MATCH_PRIORITY.get))] += 1
        else:
            no_match.append(
                {
                    "source_order": int(row.get("source_order") or 0),
                    "zotero_key": str(row.get("zotero_parent_key") or ""),
                    "title": str(row.get("title") or ""),
                }
            )
    return {
        "matched_paper_count": matched_count,
        "no_match_paper_count": len(no_match),
        "row_match_shapes": dict(row_match_shapes),
        "no_match_papers": no_match,
    }


def _master_pool_stats(
    master_path: Path,
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    records = load_summary_records(str(master_path))
    strict_matches: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    unmatched_count = 0
    for index, summary in enumerate(records):
        if not isinstance(summary, Mapping):
            unmatched_count += 1
            continue
        identity = _summary_identity(summary)
        hits: list[tuple[Mapping[str, Any], str]] = []
        for row in rows:
            matches = _match_types(identity, row)
            if matches:
                best = min(matches, key=MATCH_PRIORITY.get)
                hits.append((row, best))
        if not hits:
            unmatched_count += 1
            continue
        best_priority = min(MATCH_PRIORITY[match_type] for _, match_type in hits)
        best_hits = [
            (row, match_type)
            for row, match_type in hits
            if MATCH_PRIORITY[match_type] == best_priority
        ]
        if len(best_hits) != 1:
            conflicts.append(
                {
                    "record_index": index,
                    "candidates": [
                        {
                            "zotero_key": str(row.get("zotero_parent_key") or ""),
                            "title": str(row.get("title") or ""),
                            "match_type": match_type,
                        }
                        for row, match_type in best_hits
                    ],
                }
            )
            continue
        row, match_type = best_hits[0]
        strict_matches.append(
            {
                "record_index": index,
                "zotero_key": str(row.get("zotero_parent_key") or ""),
                "title": str(row.get("title") or ""),
                "match_type": match_type,
            }
        )
    return {
        "record_count": len(records),
        "strict_unique_in_scope_record_count": len(strict_matches),
        "broad_in_scope_signal_record_count": len(strict_matches) + len(conflicts),
        "identity_conflict_record_count": len(conflicts),
        "out_of_scope_or_unmatched_record_count": unmatched_count,
        "strict_unique_formal_paper_count": len(
            {item["zotero_key"] for item in strict_matches}
        ),
        "strict_match_types": dict(
            Counter(item["match_type"] for item in strict_matches)
        ),
        "identity_conflicts": conflicts,
        "strict_matches": strict_matches,
    }


def _model_metadata(summary: Mapping[str, Any]) -> tuple[str, str, str]:
    model = str(summary.get("model") or summary.get("model_used") or "")
    provider = str(summary.get("provider") or "")
    status = str(summary.get("status") or "")
    return provider, model, status


def build_crosswalk(
    *,
    selected_manifest: Path,
    registered_coverage_path: Path,
    original_coverage_path: Path,
    output_root: Path,
    master_pool_path: Path,
    work_dir: Path,
) -> dict[str, Any]:
    rows = _load_selected_rows(selected_manifest)
    coverage, covered_by_canonical_key = _load_registered_coverage(
        registered_coverage_path,
        selected_manifest,
    )
    original_coverage = _read_json(original_coverage_path)
    if not isinstance(original_coverage, Mapping):
        raise CrosswalkError("original coverage audit is not an object")
    original_missing = original_coverage.get("missing")
    if not isinstance(original_missing, list) or len(original_missing) != EXPECTED_ORIGINAL_MISSING_COUNT:
        raise CrosswalkError(
            f"original coverage audit must contain {EXPECTED_ORIGINAL_MISSING_COUNT} missing rows"
        )

    files, history_records, history_errors = _discover_history(output_root)
    history_stats = _full_history_match_stats(rows, history_records)
    if history_stats["no_match_paper_count"] != 28:
        raise CrosswalkError(
            "full-history audit does not reproduce the 28 truly-new papers"
        )
    master_stats = _master_pool_stats(master_pool_path, rows)

    work_dir.mkdir(parents=True, exist_ok=True)
    crosswalk: list[dict[str, Any]] = []
    provenance_evidence: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows, start=1):
        canonical_key = str(row.get("canonical_paper_key") or "")
        zotero_key = str(row.get("zotero_parent_key") or "")
        current_pdf = (
            selected_manifest.parent / str(row.get("selected_pdf_path") or "")
        ).resolve()
        if not current_pdf.is_file():
            raise CrosswalkError(f"selected PDF is missing: {current_pdf}")
        if file_sha256(current_pdf) != str(row.get("pdf_sha256") or ""):
            raise CrosswalkError(f"selected PDF hash drift: {current_pdf}")

        historical_path = ""
        historical_index: int | None = None
        historical_pdf_sha256 = ""
        historical_provider = ""
        historical_model = ""
        historical_status = ""
        match_type = ""
        disposition = "truly_new"
        reason = (
            "Full-history scan found no PDF SHA256, normalized DOI, Zotero "
            "parent key, or title+author+year match."
        )

        covered = covered_by_canonical_key.get(canonical_key)
        if covered is not None:
            historical_path = str(covered.get("source_path") or "")
            historical_index = int(covered.get("source_record_index") or 0)
            summary = _summary_record(historical_path, historical_index)
            historical_pdf_sha256 = summary_pdf_sha256(summary)
            historical_provider, historical_model, historical_status = _model_metadata(
                summary
            )
            match_type = str(covered.get("match_type") or "")
            if zotero_key == BEYOND_SKEPTICISM_KEY:
                evidence = _adjudicate_beyond(row, summary, current_pdf)
                provenance_evidence[zotero_key] = evidence
                historical_pdf_sha256 = str(
                    evidence.get("historical_pdf_sha256") or ""
                )
                match_type = "zotero_parent_key_exact+body_content_equivalent"
                disposition = "provenance_rebind"
                reason = (
                    "Zotero key/title/year identify the same paper; PDF hashes "
                    "differ, but both files have 52 pages and the historical "
                    "8-word shingles are at least 95% contained in the current PDF."
                )
            elif match_type in {"pdf_sha256_exact", "doi_exact"}:
                disposition = "reusable_exact"
                if match_type == "pdf_sha256_exact":
                    reason = (
                        "Current PDF SHA256 exactly matches the historical "
                        "source_pdf_fingerprint."
                    )
                else:
                    reason = (
                        "Normalized DOI exactly matches a Registry-ready "
                        "canonical Stage 1 summary."
                    )
            else:
                disposition = "ambiguous_manual"
                reason = (
                    "Registered summary matched only by a lower-confidence "
                    f"identity route: {match_type}."
                )
        elif zotero_key in POLLUTED_PROVENANCE_KEYS:
            historical_index, summary = _find_unique_master_record(
                master_pool_path,
                row,
            )
            historical_path = str(master_pool_path.resolve())
            historical_provider, historical_model, historical_status = _model_metadata(
                summary
            )
            evidence = _adjudicate_polluted_record(row, summary, current_pdf)
            provenance_evidence[zotero_key] = evidence
            match_type = "title_author_year_exact+source_text_confirmed"
            disposition = "provenance_rebind"
            reason = (
                "Historical DOI/provenance fields are polluted, but normalized "
                "title, authors, year, canonical AI summary, and retained source "
                "text identify the current paper."
            )

        crosswalk.append(
            {
                "index": index,
                "source_order": int(row.get("source_order") or 0),
                "title": str(row.get("title") or ""),
                "authors": list(row.get("authors") or []),
                "year": str(row.get("year") or ""),
                "doi": normalize_doi(row.get("doi")),
                "zotero_key": zotero_key,
                "pdf_sha256": str(row.get("pdf_sha256") or ""),
                "historical_summary_path": historical_path,
                "historical_summary_record_index": historical_index,
                "historical_pdf_sha256": historical_pdf_sha256,
                "historical_provider": historical_provider,
                "historical_model": historical_model,
                "historical_status": historical_status,
                "match_type": match_type,
                "disposition": disposition,
                "model_required": disposition == "truly_new",
                "reason": reason,
            }
        )

    if len(crosswalk) != EXPECTED_CORPUS_COUNT:
        raise CrosswalkError("crosswalk does not contain exactly 84 rows")
    if [item["index"] for item in crosswalk] != list(
        range(1, EXPECTED_CORPUS_COUNT + 1)
    ):
        raise CrosswalkError("crosswalk indices are not contiguous")
    if any(item["disposition"] not in ALLOWED_DISPOSITIONS for item in crosswalk):
        raise CrosswalkError("crosswalk contains an unsupported disposition")
    disposition_counts = Counter(item["disposition"] for item in crosswalk)
    expected_counts = {
        "reusable_exact": 52,
        "provenance_rebind": 4,
        "ambiguous_manual": 0,
        "truly_new": 28,
    }
    if dict(disposition_counts) != {
        key: value for key, value in expected_counts.items() if value
    }:
        raise CrosswalkError(
            f"unexpected disposition counts: {dict(disposition_counts)}"
        )

    crosswalk_by_canonical_key = {
        str(row.get("canonical_paper_key") or ""): item
        for row, item in zip(rows, crosswalk)
    }
    original_breakdown: dict[str, list[dict[str, Any]]] = {
        disposition: [] for disposition in ALLOWED_DISPOSITIONS
    }
    for missing in original_missing:
        if not isinstance(missing, Mapping):
            raise CrosswalkError("original missing row is not an object")
        canonical_key = str(missing.get("canonical_paper_key") or "")
        item = crosswalk_by_canonical_key.get(canonical_key)
        if item is None:
            raise CrosswalkError(f"original missing key is outside corpus: {canonical_key}")
        original_breakdown[item["disposition"]].append(
            {
                "index": item["index"],
                "source_order": item["source_order"],
                "zotero_key": item["zotero_key"],
                "title": item["title"],
                "match_type": item["match_type"],
            }
        )
    original_breakdown_counts = {
        key: len(value) for key, value in original_breakdown.items()
    }
    if original_breakdown_counts != {
        "reusable_exact": 12,
        "provenance_rebind": 4,
        "ambiguous_manual": 0,
        "truly_new": 28,
    }:
        raise CrosswalkError(
            f"original 44-paper reclassification drifted: "
            f"{original_breakdown_counts}"
        )

    jsonl_path = work_dir / "stage1_84_crosswalk.jsonl"
    jsonl_text = "".join(
        json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
        for item in crosswalk
    )
    jsonl_path.write_text(jsonl_text, encoding="utf-8", newline="\n")
    if len(jsonl_path.read_text(encoding="utf-8").splitlines()) != EXPECTED_CORPUS_COUNT:
        raise CrosswalkError("JSONL crosswalk does not have exactly 84 lines")

    csv_path = work_dir / "stage1_84_crosswalk.csv"
    csv_fields = [
        "index",
        "source_order",
        "title",
        "authors",
        "year",
        "doi",
        "zotero_key",
        "pdf_sha256",
        "historical_summary_path",
        "historical_summary_record_index",
        "historical_pdf_sha256",
        "historical_provider",
        "historical_model",
        "historical_status",
        "match_type",
        "disposition",
        "model_required",
        "reason",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields)
        writer.writeheader()
        for item in crosswalk:
            csv_item = dict(item)
            csv_item["authors"] = json.dumps(item["authors"], ensure_ascii=False)
            csv_item["model_required"] = str(item["model_required"]).lower()
            writer.writerow(csv_item)

    reclassification_path = work_dir / "stage1_original_44_reclassification.json"
    atomic_write_json(
        str(reclassification_path),
        {
            "artifact_type": "pph_stage1_original_missing_reclassification",
            "schema_version": "pph-stage1-original-missing-reclassification-v1",
            "created_at": utc_now_iso(),
            "original_missing_count": EXPECTED_ORIGINAL_MISSING_COUNT,
            "counts": original_breakdown_counts,
            "pdf_sha256_exact_reusable": [
                item
                for item in original_breakdown["reusable_exact"]
                if item["match_type"] == "pdf_sha256_exact"
            ],
            "beyond_skepticism_version_rebind": [
                item
                for item in original_breakdown["provenance_rebind"]
                if item["zotero_key"] == BEYOND_SKEPTICISM_KEY
            ],
            "polluted_metadata_rebinds": [
                item
                for item in original_breakdown["provenance_rebind"]
                if item["zotero_key"] in POLLUTED_PROVENANCE_KEYS
            ],
            "truly_new": original_breakdown["truly_new"],
            "ambiguous_manual": original_breakdown["ambiguous_manual"],
        },
    )

    audit_path = work_dir / "stage1_84_crosswalk_audit.json"
    audit = {
        "artifact_type": "pph_stage1_84_crosswalk_audit",
        "schema_version": "pph-stage1-84-crosswalk-audit-v1",
        "created_at": utc_now_iso(),
        "provider_executed": False,
        "expected_corpus_count": EXPECTED_CORPUS_COUNT,
        "crosswalk_row_count": len(crosswalk),
        "crosswalk_jsonl_line_count": len(
            jsonl_path.read_text(encoding="utf-8").splitlines()
        ),
        "disposition_counts": expected_counts,
        "model_required_count": sum(
            bool(item["model_required"]) for item in crosswalk
        ),
        "selected_manifest": {
            "path": str(selected_manifest.resolve()),
            "sha256": file_sha256(selected_manifest),
        },
        "registered_coverage": {
            "path": str(registered_coverage_path.resolve()),
            "sha256": file_sha256(registered_coverage_path),
            "covered_count": int(coverage.get("covered_count") or 0),
            "missing_count": int(coverage.get("missing_count") or 0),
            "ambiguous_count": int(coverage.get("ambiguous_count") or 0),
        },
        "original_coverage": {
            "path": str(original_coverage_path.resolve()),
            "sha256": file_sha256(original_coverage_path),
            "covered_count": int(original_coverage.get("covered_count") or 0),
            "missing_count": int(original_coverage.get("missing_count") or 0),
        },
        "full_history_scan": {
            "output_root": str(output_root.resolve()),
            "summary_file_count": len(files),
            "parsed_summary_file_count": len(files) - len(history_errors),
            "parse_error_count": len(history_errors),
            "successful_summary_record_count": len(history_records),
            **history_stats,
            "parse_errors": history_errors,
        },
        "master_141_intersection": {
            "path": str(master_pool_path.resolve()),
            "sha256": file_sha256(master_pool_path),
            **master_stats,
        },
        "provenance_rebind_evidence": provenance_evidence,
        "outputs": {
            "jsonl": {
                "path": str(jsonl_path.resolve()),
                "sha256": file_sha256(jsonl_path),
            },
            "csv": {
                "path": str(csv_path.resolve()),
                "sha256": file_sha256(csv_path),
                "data_row_count": len(crosswalk),
            },
            "original_44_reclassification": {
                "path": str(reclassification_path.resolve()),
                "sha256": file_sha256(reclassification_path),
            },
        },
    }
    atomic_write_json(str(audit_path), audit)
    return {
        "status": "clean",
        "crosswalk_path": str(jsonl_path.resolve()),
        "crosswalk_sha256": file_sha256(jsonl_path),
        "crosswalk_row_count": len(crosswalk),
        "disposition_counts": expected_counts,
        "model_required_count": audit["model_required_count"],
        "audit_path": str(audit_path.resolve()),
        "audit_sha256": file_sha256(audit_path),
        "master_141_intersection": {
            key: master_stats[key]
            for key in (
                "record_count",
                "strict_unique_in_scope_record_count",
                "broad_in_scope_signal_record_count",
                "identity_conflict_record_count",
                "strict_unique_formal_paper_count",
            )
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the provider-free corrected 84-paper Stage 1 crosswalk."
    )
    parser.add_argument("--selected-manifest", type=Path, required=True)
    parser.add_argument("--registered-coverage", type=Path, required=True)
    parser.add_argument("--original-coverage", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--master-pool", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = build_crosswalk(
        selected_manifest=args.selected_manifest.expanduser().resolve(),
        registered_coverage_path=args.registered_coverage.expanduser().resolve(),
        original_coverage_path=args.original_coverage.expanduser().resolve(),
        output_root=args.output_root.expanduser().resolve(),
        master_pool_path=args.master_pool.expanduser().resolve(),
        work_dir=args.work_dir.expanduser().resolve(),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

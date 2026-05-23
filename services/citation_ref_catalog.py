from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from services.citation_metadata import normalize_summary_paper_metadata
from services.paper_identity import build_canonical_paper_key, normalize_doi


ARTIFACT_TYPE = "citation_ref_catalog"
ARTIFACT_VERSION = "v1"
CATALOG_SCOPE = "review_document"
ENTRY_SCOPE = "document"
ACTIVE_STATUS = "active"
TOMBSTONED_STATUS = "tombstoned"


def _canonical_json_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _summary_hash(summary: Mapping[str, Any]) -> str:
    explicit = str(
        summary.get("source_summary_hash")
        or summary.get("summary_hash")
        or summary.get("artifact_hash")
        or ""
    ).strip()
    if explicit:
        return explicit
    return _canonical_json_hash(summary)


def _paper_info(summary: Mapping[str, Any]) -> Dict[str, Any]:
    paper_info_raw = summary.get("paper_info", {})
    return dict(paper_info_raw) if isinstance(paper_info_raw, Mapping) else {}


def _normalize_authors(authors: Any) -> List[str]:
    if isinstance(authors, list):
        return [str(author).strip() for author in authors if str(author).strip()]
    if authors in (None, ""):
        return []
    return [str(authors).strip()]


def _paper_identity(summary: Mapping[str, Any]) -> Dict[str, Any]:
    paper_info = _paper_info(summary)
    normalized = normalize_summary_paper_metadata(summary)
    merged = {
        **paper_info,
        "title": normalized.title or paper_info.get("title"),
        "authors": normalized.authors or paper_info.get("authors") or [],
        "year": normalized.year or paper_info.get("year") or "",
        "doi": normalized.doi or paper_info.get("doi") or "",
    }
    canonical_key = str(
        paper_info.get("canonical_paper_key")
        or build_canonical_paper_key(merged)
        or paper_info.get("source_paper_id")
        or ""
    ).strip()
    doi = normalize_doi(merged.get("doi"))
    source_paper_id = str(paper_info.get("source_paper_id") or "").strip()
    identity_key = doi or canonical_key or source_paper_id
    return {
        "identity_key": identity_key,
        "paper_id": canonical_key or doi or source_paper_id or f"source:{_summary_hash(summary)[:12]}",
        "canonical_paper_key": canonical_key or doi or source_paper_id or f"source:{_summary_hash(summary)[:12]}",
        "title": str(merged.get("title") or "").strip(),
        "authors": _normalize_authors(merged.get("authors")),
        "year": str(merged.get("year") or "").strip(),
        "doi": doi,
        "source_paper_id": source_paper_id,
        "source_summary_hash": _summary_hash(summary),
    }


def _existing_entries_by_identity(existing_catalog: Optional[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    entries_by_identity: Dict[str, Dict[str, Any]] = {}
    for raw_entry in (existing_catalog or {}).get("entries", []):
        if not isinstance(raw_entry, Mapping):
            continue
        if raw_entry.get("status") == TOMBSTONED_STATUS:
            continue
        identity_values = [
            raw_entry.get("doi"),
            raw_entry.get("canonical_paper_key"),
            raw_entry.get("paper_id"),
        ]
        entry = dict(raw_entry)
        for value in identity_values:
            key = str(value or "").strip()
            if key:
                entries_by_identity.setdefault(key.casefold(), entry)
    return entries_by_identity


def _max_existing_ref_number(existing_catalog: Optional[Mapping[str, Any]]) -> int:
    max_number = 0
    for raw_entry in (existing_catalog or {}).get("entries", []):
        if not isinstance(raw_entry, Mapping):
            continue
        match = re.fullmatch(r"R(\d{3,})", str(raw_entry.get("ref_id") or ""))
        if match:
            max_number = max(max_number, int(match.group(1)))
    return max_number


def _next_ref_id(number: int) -> str:
    return f"R{number:03d}"


def extract_ref_ids_from_token(citation_token: Any) -> List[str]:
    match = re.fullmatch(r"\s*\[\[cite_ref:([^\]]+)\]\]\s*", str(citation_token or ""))
    if not match:
        return []
    return list(dict.fromkeys(re.findall(r"\bR\d{3,}\b", match.group(1))))


def _entry_hash_payload(entries: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "ref_id": entry.get("ref_id"),
            "scope": entry.get("scope"),
            "paper_id": entry.get("paper_id"),
            "canonical_paper_key": entry.get("canonical_paper_key"),
            "title": entry.get("title"),
            "authors": list(entry.get("authors") or []),
            "year": entry.get("year"),
            "doi": entry.get("doi"),
            "source_summary_hash": entry.get("source_summary_hash"),
            "status": entry.get("status"),
        }
        for entry in entries
    ]


def build_document_ref_catalog(
    paper_summaries: Sequence[Mapping[str, Any]],
    *,
    project_name: str,
    job_id: str,
    existing_catalog: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a durable document-level R### citation catalog.

    Existing R### ids are reused only for the exact same DOI/canonical identity.
    Missing old entries are retained as tombstones, and new papers append after
    the highest historical id.
    """
    existing_by_identity = _existing_entries_by_identity(existing_catalog)
    current_identity_keys: set[str] = set()
    assigned_ref_ids: set[str] = set()
    next_number = _max_existing_ref_number(existing_catalog) + 1
    entries: List[Dict[str, Any]] = []

    for summary in paper_summaries:
        identity = _paper_identity(summary)
        identity_candidates = [
            identity.get("doi"),
            identity.get("canonical_paper_key"),
            identity.get("paper_id"),
            identity.get("source_paper_id"),
        ]
        existing_entry: Optional[Dict[str, Any]] = None
        for candidate in identity_candidates:
            key = str(candidate or "").strip().casefold()
            if key and key in existing_by_identity:
                existing_entry = existing_by_identity[key]
                break

        if existing_entry:
            ref_id = str(existing_entry.get("ref_id") or "").strip()
        else:
            ref_id = _next_ref_id(next_number)
            next_number += 1

        assigned_ref_ids.add(ref_id)
        for candidate in identity_candidates:
            key = str(candidate or "").strip()
            if key:
                current_identity_keys.add(key.casefold())

        entries.append(
            {
                "ref_id": ref_id,
                "scope": ENTRY_SCOPE,
                "paper_id": identity["paper_id"],
                "canonical_paper_key": identity["canonical_paper_key"],
                "title": identity["title"],
                "authors": identity["authors"],
                "year": identity["year"],
                "doi": identity["doi"],
                "source_summary_hash": identity["source_summary_hash"],
                "status": ACTIVE_STATUS,
            }
        )

    for raw_entry in (existing_catalog or {}).get("entries", []):
        if not isinstance(raw_entry, Mapping):
            continue
        ref_id = str(raw_entry.get("ref_id") or "").strip()
        if not ref_id or ref_id in assigned_ref_ids:
            continue
        tombstone = dict(raw_entry)
        tombstone["status"] = TOMBSTONED_STATUS
        tombstone["scope"] = ENTRY_SCOPE
        entries.append(tombstone)

    entries.sort(key=lambda entry: int(str(entry["ref_id"])[1:]) if re.fullmatch(r"R\d{3,}", str(entry["ref_id"])) else 10**9)
    catalog_hash = _canonical_json_hash(_entry_hash_payload(entries))
    return {
        "artifact_type": ARTIFACT_TYPE,
        "artifact_version": ARTIFACT_VERSION,
        "created_from_job_id": job_id,
        "catalog_id": f"{ARTIFACT_TYPE}:{project_name}",
        "scope": CATALOG_SCOPE,
        "catalog_hash": catalog_hash,
        "entries": entries,
    }


def build_section_ref_view(
    catalog: Mapping[str, Any],
    *,
    ref_ids: Optional[Iterable[str]] = None,
    paper_ids: Optional[Iterable[str]] = None,
    canonical_paper_keys: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    ref_filter = {str(item).strip() for item in (ref_ids or []) if str(item).strip()}
    paper_filter = {str(item).strip() for item in (paper_ids or []) if str(item).strip()}
    key_filter = {str(item).strip() for item in (canonical_paper_keys or []) if str(item).strip()}

    filtered_entries: List[Dict[str, Any]] = []
    for raw_entry in catalog.get("entries", []):
        if not isinstance(raw_entry, Mapping):
            continue
        if raw_entry.get("status") != ACTIVE_STATUS:
            continue
        include = not ref_filter and not paper_filter and not key_filter
        include = include or str(raw_entry.get("ref_id") or "") in ref_filter
        include = include or str(raw_entry.get("paper_id") or "") in paper_filter
        include = include or str(raw_entry.get("canonical_paper_key") or "") in key_filter
        if include:
            filtered_entries.append(dict(raw_entry))

    return {
        "artifact_type": catalog.get("artifact_type", ARTIFACT_TYPE),
        "artifact_version": catalog.get("artifact_version", ARTIFACT_VERSION),
        "created_from_job_id": catalog.get("created_from_job_id", ""),
        "catalog_id": catalog.get("catalog_id", ""),
        "scope": "section_view",
        "catalog_hash": catalog.get("catalog_hash", ""),
        "entries": filtered_entries,
    }


def resolve_ref_id(catalog: Mapping[str, Any] | None, ref_id: str) -> Optional[Dict[str, Any]]:
    target = str(ref_id or "").strip()
    if not target or not catalog:
        return None
    for raw_entry in catalog.get("entries", []):
        if not isinstance(raw_entry, Mapping):
            continue
        if str(raw_entry.get("ref_id") or "").strip() == target and raw_entry.get("status") == ACTIVE_STATUS:
            return dict(raw_entry)
    return None


def _extract_ref_ids(value: Sequence[str] | str) -> List[str]:
    def _from_text(text: str) -> List[str]:
        direct = text.strip()
        if re.fullmatch(r"R\d{3,}", direct):
            return [direct]

        ref_ids: List[str] = []
        for token_match in re.finditer(r"\[\[cite_ref:[^\]]+\]\]", text):
            token = token_match.group(0)
            token_ref_ids = extract_ref_ids_from_token(token)
            if token_ref_ids:
                ref_ids.extend(token_ref_ids)
            else:
                ref_ids.append(token)
        return ref_ids

    if isinstance(value, str):
        return _from_text(value)

    ref_ids: List[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        parsed = _from_text(text)
        ref_ids.extend(parsed or [text])
    return ref_ids


def validate_citation_refs(
    catalog: Mapping[str, Any],
    ref_ids_or_text: Sequence[str] | str,
) -> Dict[str, Any]:
    ref_ids = _extract_ref_ids(ref_ids_or_text)
    active_ids = {
        str(entry.get("ref_id") or "")
        for entry in catalog.get("entries", [])
        if isinstance(entry, Mapping) and entry.get("status") == ACTIVE_STATUS
    }
    tombstoned_ids = {
        str(entry.get("ref_id") or "")
        for entry in catalog.get("entries", [])
        if isinstance(entry, Mapping) and entry.get("status") == TOMBSTONED_STATUS
    }
    unresolved = [ref_id for ref_id in ref_ids if ref_id not in active_ids and ref_id not in tombstoned_ids]
    tombstoned = [ref_id for ref_id in ref_ids if ref_id in tombstoned_ids]
    return {
        "valid": not unresolved and not tombstoned,
        "resolved": [ref_id for ref_id in ref_ids if ref_id in active_ids],
        "unresolved": unresolved,
        "tombstoned": tombstoned,
    }

from __future__ import annotations

import hashlib
import os
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Mapping

from file_finder import create_file_index, resolve_pdf_match
from runtime.stage_contracts import SourceBundle, build_source_bundle
from runtime.canonical_attachment_selector import canonicalize_attachment_candidates
from runtime.zotero_attachment_resolver import ZoteroAttachmentIndex
from services.source_identity import inspect_pdf_identity
from services.paper_identity import build_canonical_paper_key
from zotero_parser import parse_zotero_report_result


def _abs(path: str) -> str:
    return str(Path(path).resolve())


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity_cache_key(paper: Mapping[str, Any], path: str) -> str:
    return f"{_abs(path).casefold()}:{build_canonical_paper_key(paper)}"


def _inspect_pdf_candidate(
    paper: Mapping[str, Any],
    path: str,
    *,
    source: str,
    match_kind: str = "",
    score: float | None = None,
    identity_cache: dict[str, Any],
    hash_cache: dict[str, str],
) -> dict[str, Any]:
    resolved = _abs(path)
    path_cache_key = resolved.casefold()
    identity_cache_key = _identity_cache_key(paper, resolved)
    payload: dict[str, Any] = {
        "path": resolved,
        "source": source,
        "match_kind": match_kind,
        "score": score,
        "exists": Path(resolved).is_file(),
        "sha256": "",
        "identity_verdict": "",
        "artifact_status": "",
        "canonical_ready": False,
        "identity_reasons": [],
        "identity_diagnostics": [],
    }
    if not Path(resolved).is_file() or Path(resolved).suffix.casefold() != ".pdf":
        return payload
    if path_cache_key not in hash_cache:
        try:
            hash_cache[path_cache_key] = _sha256_file(resolved)
        except OSError as exc:
            hash_cache[path_cache_key] = f"hash_error:{type(exc).__name__}"
    payload["sha256"] = hash_cache[path_cache_key]
    if identity_cache_key not in identity_cache:
        identity_cache[identity_cache_key] = inspect_pdf_identity(paper, resolved)
    identity = identity_cache[identity_cache_key]
    payload.update(
        {
            "identity_verdict": str(identity.identity_verdict),
            "artifact_status": str(identity.artifact_status),
            "canonical_ready": bool(identity.canonical_ready),
            "identity_reasons": list(identity.reasons),
            "identity_diagnostics": list(identity.diagnostics),
        }
    )
    return payload


def _select_identity_candidate(
    candidates: list[dict[str, Any]],
    *,
    resolved_source: str,
) -> tuple[str, str, dict[str, Any] | None]:
    identity_matches = [item for item in candidates if item.get("canonical_ready")]
    if len(identity_matches) == 1:
        return "matched", "", identity_matches[0]
    if len(identity_matches) > 1:
        hashes = {str(item.get("sha256") or "") for item in identity_matches}
        if len(hashes) == 1 and "" not in hashes:
            selected = sorted(identity_matches, key=lambda item: str(item["path"]).casefold())[0]
            return "matched", "duplicate_identical_candidates", selected
        return "ambiguous", "multiple_identity_matches_with_different_hashes", None
    if candidates:
        return "quarantined", f"{resolved_source}_candidates_failed_identity", None
    return "not_found", "no_pdf_candidate", None


def discover_pdf_files(pdf_folder: str) -> list[str]:
    folder = Path(pdf_folder).resolve()
    if not folder.exists():
        raise FileNotFoundError(f"pdf_folder does not exist: {folder}")
    if not folder.is_dir():
        raise NotADirectoryError(f"pdf_folder is not a directory: {folder}")

    discovered = sorted(
        str(path.resolve())
        for path in folder.rglob("*.pdf")
        if path.is_file()
    )
    return discovered


def _direct_paper_stub(pdf_path: str) -> Dict[str, Any]:
    title = Path(pdf_path).stem
    return {
        "title": title,
        "pdf_path": pdf_path,
        "source_pdf": pdf_path,
        "attachments": [os.path.basename(pdf_path)],
    }


def build_direct_source_bundle(*, project_name: str, pdf_folder: str) -> SourceBundle:
    pdf_files = discover_pdf_files(pdf_folder)
    papers = [_direct_paper_stub(path) for path in pdf_files]
    return build_source_bundle(
        source_mode="direct",
        project_name=project_name,
        papers=papers,
        source_snapshot={
            "pdf_folder": _abs(pdf_folder),
            "pdf_count": len(pdf_files),
            "source_paths": list(pdf_files),
        },
    )


def build_zotero_source_bundle(*, project_name: str, zotero_report: str, library_path: str) -> SourceBundle:
    report_path = _abs(zotero_report)
    library_root = _abs(library_path)
    parse_result = parse_zotero_report_result(report_path)
    if parse_result.status == "failed":
        codes = ",".join(item.code for item in parse_result.diagnostics) or "unknown"
        raise ValueError(f"zotero_parse_failed:{codes}")
    parsed_papers = parse_result.papers
    file_index = create_file_index(library_root)
    zotero_attachments = ZoteroAttachmentIndex(library_root)
    identity_cache: dict[str, Any] = {}
    hash_cache: dict[str, str] = {}

    matched_papers: list[Dict[str, Any]] = []
    missing_titles: list[str] = []
    ambiguous_matches: list[Dict[str, Any]] = []
    pdf_resolutions: list[Dict[str, Any]] = []
    identity_results: list[Dict[str, Any]] = []
    quarantined_sources: list[Dict[str, Any]] = []
    canonicalization_records: list[Dict[str, Any]] = []
    canonicalization_events: Counter[str] = Counter()
    for paper_index, raw_paper in enumerate(parsed_papers):
        paper = dict(raw_paper)
        match_result = resolve_pdf_match(paper, library_root, file_index)
        zotero_resolution = zotero_attachments.resolve(paper)
        candidate_map: dict[str, dict[str, Any]] = {}

        raw_candidates = tuple(getattr(match_result, "candidates", ()) or ())
        for candidate in raw_candidates:
            candidate_payload = _inspect_pdf_candidate(
                paper,
                candidate.path,
                source="storage_filename_match",
                match_kind=str(candidate.match_kind),
                score=float(candidate.score),
                identity_cache=identity_cache,
                hash_cache=hash_cache,
            )
            if candidate_payload["exists"]:
                candidate_map[candidate_payload["path"].casefold()] = candidate_payload
        selected_path = str(getattr(match_result, "selected_path", "") or "")
        if not raw_candidates and selected_path:
            candidate_payload = _inspect_pdf_candidate(
                paper,
                selected_path,
                source="storage_filename_match",
                match_kind="selected_path",
                identity_cache=identity_cache,
                hash_cache=hash_cache,
            )
            if candidate_payload["exists"]:
                candidate_map[candidate_payload["path"].casefold()] = candidate_payload

        relation_paths: set[str] = set()
        for attachment in zotero_resolution.get("attachments", []):
            if not isinstance(attachment, Mapping) or not bool(attachment.get("exists")):
                continue
            path = str(attachment.get("resolved_path") or "")
            if not path:
                continue
            candidate_payload = _inspect_pdf_candidate(
                paper,
                path,
                source="zotero_db_attachment_relation",
                match_kind="db_relation",
                identity_cache=identity_cache,
                hash_cache=hash_cache,
            )
            candidate_payload.update(
                {
                    "attachment_key": str(attachment.get("attachment_key") or ""),
                    "date_added": str(attachment.get("date_added") or ""),
                    "attachment_title": str(attachment.get("attachment_title") or ""),
                    "raw_path": str(attachment.get("raw_path") or ""),
                    "link_mode": int(attachment.get("link_mode") or 0),
                }
            )
            if candidate_payload["exists"]:
                relation_paths.add(candidate_payload["path"].casefold())
                existing = candidate_map.get(candidate_payload["path"].casefold())
                if existing is None:
                    candidate_map[candidate_payload["path"].casefold()] = candidate_payload
                else:
                    existing.update(
                        {
                            "attachment_key": candidate_payload["attachment_key"],
                            "date_added": candidate_payload["date_added"],
                            "attachment_title": candidate_payload["attachment_title"],
                            "raw_path": candidate_payload["raw_path"],
                            "link_mode": candidate_payload["link_mode"],
                        }
                    )
                    sources = list(existing.get("source_labels") or [existing.get("source") or ""])
                    if "zotero_db_attachment_relation" not in sources:
                        sources.append("zotero_db_attachment_relation")
                    existing["source_labels"] = [item for item in sources if item]
                    existing["source"] = "+".join(existing["source_labels"])

        candidates = list(candidate_map.values())
        relation_candidates = [
            item for item in candidates if item["path"].casefold() in relation_paths
        ]
        other_candidates = [
            item for item in candidates if item["path"].casefold() not in relation_paths
        ]
        selection_candidates = relation_candidates or other_candidates
        canonical_selection = canonicalize_attachment_candidates(
            selection_candidates,
            parent_match_method=str(zotero_resolution.get("match_method") or ""),
            parent_count=int(zotero_resolution.get("parent_count") or 0),
        )
        for event, count in (canonical_selection.get("events") or {}).items():
            canonicalization_events[str(event)] += int(count or 0)
        canonicalization_records.append(
            {
                "paper_index": paper_index,
                "title": str(paper.get("title") or "unknown"),
                "parent_keys": [
                    str(parent.get("key") or "")
                    for parent in (zotero_resolution.get("parents") or [])
                    if isinstance(parent, Mapping)
                ],
                "selection": canonical_selection,
            }
        )
        selected_candidate = canonical_selection.get("selected")
        if canonical_selection.get("status") == "selected" and selected_candidate is not None:
            resolution_status = "matched"
            resolution_reason = ";".join(
                [
                    "canonical_primary_selected",
                    *[
                        str(item)
                        for item in (canonical_selection.get("selection_reason") or [])
                        if str(item)
                    ],
                ]
            )
        elif selection_candidates:
            resolution_status = "quarantined"
            resolution_reason = str(
                canonical_selection.get("unresolved_reason")
                or "no_safe_primary_after_canonicalization"
            )
            selected_candidate = None
        else:
            resolution_status = "not_found"
            resolution_reason = "no_pdf_candidate"
            selected_candidate = None

        resolution = match_result.to_dict()
        resolution.update(
            {
                "paper_index": paper_index,
                "title": str(paper.get("title") or "unknown"),
                "canonical_paper_key": build_canonical_paper_key(paper),
                "source_resolution_status": resolution_status,
                "source_resolution_reason": resolution_reason,
                "identity_candidates": candidates,
                "zotero_attachment_resolution": zotero_resolution,
                "canonical_attachment_selection": canonical_selection,
            }
        )
        pdf_resolutions.append(resolution)

        if (
            resolution_status == "quarantined"
            and len(selection_candidates) > 1
        ):
            ambiguous_matches.append(dict(resolution))
        if resolution_status == "quarantined":
            unresolved_candidates = relation_candidates or selection_candidates or candidates
            if unresolved_candidates:
                identity_result = identity_cache[
                    _identity_cache_key(paper, str(unresolved_candidates[0]["path"]))
                ]
                identity_payload = identity_result.to_dict()
                identity_payload.update(
                    {
                        "paper_index": paper_index,
                        "title": str(paper.get("title") or "unknown"),
                        "candidate_path": str(unresolved_candidates[0]["path"]),
                    }
                )
                identity_results.append(identity_payload)
                resolution["identity"] = identity_payload
                quarantined_sources.append(identity_payload)
            else:
                missing_titles.append(str(paper.get("title") or "unknown"))
            continue
        if resolution_status != "matched" or selected_candidate is None:
            missing_titles.append(str(paper.get("title") or "unknown"))
            continue

        pdf_path = str(selected_candidate["path"])
        paper["pdf_path"] = _abs(pdf_path)
        paper["source_pdf"] = paper["pdf_path"]
        paper["source_attachment_key"] = str(selected_candidate.get("attachment_key") or "")
        paper["source_attachment_role"] = str(
            selected_candidate.get("role") or "PRIMARY_FULLTEXT"
        )
        paper["source_attachment_version_class"] = str(
            selected_candidate.get("version_class") or "UNKNOWN_VERSION"
        )
        paper["source_attachment_selection"] = {
            "policy_version": canonical_selection.get("policy_version"),
            "canonical_primary_pdf": pdf_path,
            "canonical_attachment_key": str(selected_candidate.get("attachment_key") or ""),
            "selected_role": str(selected_candidate.get("role") or ""),
            "selected_version_class": str(selected_candidate.get("version_class") or ""),
            "selection_reason": list(canonical_selection.get("selection_reason") or []),
            "auxiliary_attachment_keys": [
                str(item.get("attachment_key") or "")
                for item in (canonical_selection.get("auxiliary_attachments") or [])
                if isinstance(item, Mapping) and str(item.get("attachment_key") or "")
            ],
            "rejected_attachment_keys": [
                str(item.get("attachment_key") or "")
                for item in (canonical_selection.get("rejected_attachments") or [])
                if isinstance(item, Mapping) and str(item.get("attachment_key") or "")
            ],
        }
        identity_result = identity_cache[_identity_cache_key(paper, paper["pdf_path"])]
        identity_payload = identity_result.to_dict()
        identity_payload.update(
            {
                "paper_index": paper_index,
                "title": str(paper.get("title") or "unknown"),
            }
        )
        identity_results.append(identity_payload)
        resolution["identity"] = identity_payload
        selected_role = str(selected_candidate.get("role") or "PRIMARY_FULLTEXT")
        if not identity_result.canonical_ready and selected_role != "SCANNED_PRIMARY":
            quarantined_sources.append(identity_payload)
            continue
        if identity_result.canonical_ready:
            paper["identity_verdict"] = identity_result.identity_verdict
            paper["artifact_status"] = identity_result.artifact_status
        paper["source_identity"] = identity_payload
        matched_papers.append(paper)

    return build_source_bundle(
        source_mode="zotero",
        project_name=project_name,
        papers=matched_papers,
        source_snapshot={
            "zotero_report": report_path,
            "library_path": library_root,
            "matched_count": len(matched_papers),
            "missing_titles": missing_titles,
            "ambiguous_matches": ambiguous_matches,
            "pdf_resolutions": pdf_resolutions,
            "identity_results": identity_results,
            "quarantined_sources": quarantined_sources,
            "canonicalization": {
                "policy_version": str(
                    canonicalization_records[0]["selection"].get("policy_version")
                    if canonicalization_records
                    else ""
                ),
                "event_counts": dict(canonicalization_events),
                "records": canonicalization_records,
            },
            "zotero_database": {
                "path": zotero_attachments.database_path,
                "access_mode": zotero_attachments.database_access_mode,
                "quick_check": zotero_attachments.database_integrity,
                "journal_present": zotero_attachments.journal_present,
                "diagnostics": list(zotero_attachments.database_diagnostics),
            },
            "canonical_ready": not quarantined_sources and not ambiguous_matches and not missing_titles,
            "zotero_parse": {
                "status": parse_result.status,
                "parser_route": parse_result.parser_route,
                "parser_version": parse_result.parser_version,
                "report_hash": parse_result.report_hash,
                "parse_confidence": parse_result.parse_confidence,
                "stats": parse_result.stats.to_dict(),
                "diagnostic_codes": [item.code for item in parse_result.diagnostics],
            },
        },
    )


def build_source_bundle_for_request(request: Any, *, project_name: str | None = None) -> SourceBundle:
    source_mode = str(getattr(request, "source_mode", "") or ("zotero" if getattr(request, "zotero_report", None) else "direct"))
    resolved_project_name = project_name or str(getattr(request, "project_name", "") or "").strip()
    if not resolved_project_name:
        pdf_folder = getattr(request, "pdf_folder", None)
        if pdf_folder:
            resolved_project_name = Path(str(pdf_folder)).resolve().name
        else:
            resolved_project_name = "auto-generate-ai-runtime"

    if source_mode == "zotero":
        zotero_report = str(getattr(request, "zotero_report", "") or "").strip()
        library_path = str(getattr(request, "library_path", "") or "").strip()
        if not zotero_report or not library_path:
            raise ValueError("zotero source mode requires zotero_report and library_path")
        return build_zotero_source_bundle(
            project_name=resolved_project_name,
            zotero_report=zotero_report,
            library_path=library_path,
        )
    pdf_folder = str(getattr(request, "pdf_folder", "") or "").strip()
    if not pdf_folder:
        raise ValueError("direct source mode requires pdf_folder")
    return build_direct_source_bundle(
        project_name=resolved_project_name,
        pdf_folder=pdf_folder,
    )

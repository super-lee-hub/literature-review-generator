from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Mapping

from file_finder import create_file_index, resolve_pdf_match
from runtime.stage_contracts import SourceBundle, build_source_bundle
from zotero_parser import parse_zotero_report_result


def _abs(path: str) -> str:
    return str(Path(path).resolve())


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

    matched_papers: list[Dict[str, Any]] = []
    missing_titles: list[str] = []
    ambiguous_matches: list[Dict[str, Any]] = []
    pdf_resolutions: list[Dict[str, Any]] = []
    for paper_index, raw_paper in enumerate(parsed_papers):
        paper = dict(raw_paper)
        match_result = resolve_pdf_match(paper, library_root, file_index)
        resolution = match_result.to_dict()
        resolution.update(
            {
                "paper_index": paper_index,
                "title": str(paper.get("title") or "unknown"),
            }
        )
        pdf_resolutions.append(resolution)
        if match_result.status == "ambiguous":
            ambiguous_matches.append(dict(resolution))
            continue
        pdf_path = match_result.selected_path
        if match_result.status != "matched" or not pdf_path:
            missing_titles.append(str(paper.get("title") or "unknown"))
            continue
        paper["pdf_path"] = _abs(pdf_path)
        paper["source_pdf"] = paper["pdf_path"]
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
        return build_zotero_source_bundle(
            project_name=resolved_project_name,
            zotero_report=str(getattr(request, "zotero_report", "") or ""),
            library_path=str(getattr(request, "library_path", "") or ""),
        )
    return build_direct_source_bundle(
        project_name=resolved_project_name,
        pdf_folder=str(getattr(request, "pdf_folder", "") or ""),
    )

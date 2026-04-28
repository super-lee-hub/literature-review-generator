from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from services.stage1_input_selector import select_stage1_input


STALE_LEVELS = {"REPROCESS", "BLOCK"}
LEGACY_STALE_LEVELS = {"FALLBACK", "REPROCESS", "BLOCK"}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Audit stage-one preprocess input quality.")
    parser.add_argument("--project", required=True, help="Project name or workspace directory name to audit.")
    parser.add_argument("--mode", default="all", choices=["all"], help="Reserved for future filters.")
    parser.add_argument("--output-root", default="output", help="Root output directory.")
    parser.add_argument("--write-report", action="store_true", help="Write JSON/CSV reports.")
    parser.add_argument("--report-dir", default="", help="Optional explicit report directory.")
    args = parser.parse_args(argv)

    output_root = Path(args.output_root)
    project_dirs = find_project_dirs(output_root, args.project)
    records = audit_project_dirs(project_dirs)
    bad_inputs = [record for record in records if record.get("is_bad_stage1_input")]
    stale_summaries = [
        record
        for record in bad_inputs
        if str(record.get("paper_status") or "").lower() == "success"
    ]

    report = {
        "artifact_type": "preprocess_quality_audit",
        "artifact_version": "v1",
        "project": args.project,
        "project_dirs": [str(path) for path in project_dirs],
        "record_count": len(records),
        "bad_stage1_input_count": len(bad_inputs),
        "stale_summary_count": len(stale_summaries),
        "records": records,
    }

    if args.write_report:
        report_dir = Path(args.report_dir) if args.report_dir else default_report_dir(output_root, args.project, project_dirs)
        write_reports(report_dir, report, bad_inputs, stale_summaries)
        safe_print(f"Wrote preprocess quality audit reports to {report_dir}")
    else:
        safe_print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def find_project_dirs(output_root: Path, project: str) -> List[Path]:
    if not output_root.exists():
        return []
    candidates: List[Path] = []
    seen: set[Path] = set()

    def append_unique(path: Optional[Path]) -> None:
        if path is None or not path.exists():
            return
        try:
            key = path.resolve()
        except Exception:
            key = path.absolute()
        if key in seen:
            return
        seen.add(key)
        candidates.append(path)

    exact = output_root / project
    if exact.is_dir():
        latest = _workspace_from_latest_pointer(exact / "_latest_job.json")
        append_unique(latest if latest and latest.exists() else exact)
    for child in output_root.iterdir():
        if not child.is_dir():
            continue
        if child.name == project and (child / "_latest_job.json").exists() and not (child / "artifacts" / "paper_artifacts").exists():
            continue
        if child.name == project or child.name.startswith(f"{project}__"):
            append_unique(child)
    return sorted(candidates, key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)


def audit_project_dirs(project_dirs: Iterable[Path]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for project_dir in project_dirs:
        paper_dir = project_dir / "artifacts" / "paper_artifacts"
        if not paper_dir.exists():
            continue
        for artifact_path in sorted(paper_dir.glob("*.json")):
            records.append(audit_paper_artifact(artifact_path))
    return records


def audit_paper_artifact(artifact_path: Path) -> Dict[str, Any]:
    payload = _load_json(artifact_path, default={})
    paper_info = payload.get("paper_info") or {}
    analysis = payload.get("analysis") or {}
    preprocess = analysis.get("preprocess") or {}
    existing_level = str(preprocess.get("stage1_quality_level") or "")
    selected_source = str(preprocess.get("selected_text_source") or "")
    reasons = list(preprocess.get("stage1_quality_reasons") or [])
    audit_origin = "native_stage1_quality" if existing_level else "legacy_rescore"

    if not existing_level:
        selected_source, existing_level, reasons = _score_legacy_preprocess(preprocess)

    is_fallback = existing_level == "FALLBACK"
    if audit_origin == "legacy_rescore":
        is_bad = existing_level in LEGACY_STALE_LEVELS
    else:
        is_bad = existing_level in STALE_LEVELS
    summary_status = _summary_status(
        quality_level=existing_level,
        is_bad=is_bad,
        is_fallback=is_fallback,
        audit_origin=audit_origin,
    )
    return {
        "paper_artifact_path": str(artifact_path),
        "paper_key": (payload.get("paper_identity") or {}).get("canonical_paper_key") or "",
        "title": paper_info.get("title") or "",
        "paper_status": analysis.get("status") or payload.get("status") or "",
        "audit_origin": audit_origin,
        "selected_text_source": selected_source,
        "stage1_quality_level": existing_level,
        "stage1_quality_reasons": reasons,
        "is_fallback_stage1_input": is_fallback,
        "is_bad_stage1_input": is_bad,
        "summary_status": summary_status,
    }


def write_reports(
    report_dir: Path,
    report: Dict[str, Any],
    bad_inputs: List[Dict[str, Any]],
    stale_summaries: List[Dict[str, Any]],
) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "preprocess_quality_audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (report_dir / "bad_stage1_inputs.json").write_text(
        json.dumps(bad_inputs, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (report_dir / "stale_summaries.json").write_text(
        json.dumps(stale_summaries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with (report_dir / "preprocess_quality_audit.csv").open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "paper_artifact_path",
            "paper_key",
            "title",
            "paper_status",
            "audit_origin",
            "selected_text_source",
            "stage1_quality_level",
            "stage1_quality_reasons",
            "is_fallback_stage1_input",
            "is_bad_stage1_input",
            "summary_status",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in report.get("records", []):
            row = dict(record)
            row["stage1_quality_reasons"] = ";".join(row.get("stage1_quality_reasons") or [])
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def default_report_dir(output_root: Path, project: str, project_dirs: List[Path]) -> Path:
    if project_dirs:
        artifacts_dir = project_dirs[0] / "artifacts"
        if artifacts_dir.exists() or project_dirs[0].exists():
            return artifacts_dir
    return output_root / project


def _score_legacy_preprocess(preprocess: Dict[str, Any]) -> tuple[str, str, List[str]]:
    markdown = _read_text(_resolve_path(preprocess.get("markdown_path")))
    plain = _read_text(_resolve_path(preprocess.get("plain_text_path")))
    selection = select_stage1_input(markdown_text=markdown, plain_text=plain, page_index=[])
    return selection.selected_source, selection.quality_level, selection.stage1_quality_reasons


def _summary_status(*, quality_level: str, is_bad: bool, is_fallback: bool, audit_origin: str) -> str:
    if is_bad and audit_origin == "legacy_rescore":
        return "stale_due_to_legacy_bad_stage1_input"
    if is_bad:
        return "stale_due_to_blocked_stage1_input"
    if is_fallback:
        return "current_with_fallback"
    return "current"


def safe_print(message: str) -> None:
    text = str(message)
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.write(_encode_for_stdout(text) + "\n")


def _encode_for_stdout(text: str) -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return str(text).encode(encoding, errors="backslashreplace").decode(encoding, errors="replace")


def _workspace_from_latest_pointer(path: Path) -> Optional[Path]:
    if not path.exists():
        return None
    payload = _load_json(path, default={})
    workspace = payload.get("workspace_path")
    return Path(workspace) if workspace else None


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _resolve_path(value: Any) -> Path:
    raw = str(value or "").replace("\\", "/")
    path = Path(raw)
    if path.is_absolute():
        return path
    return Path.cwd() / path


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from types import SimpleNamespace
from typing import Any, Iterable, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = REPO_ROOT / "output"
CONFIG_PATH = REPO_ROOT / "config.ini"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


PROJECTS: dict[str, dict[str, Any]] = {
    "S01": {
        "project_name": "pph_s01_dynamic_disadvantage",
        "job_id": "20260728_054303_5ab4252e",
        "expected_sections": 6,
    },
    "S02": {
        "project_name": "pph_s02_prior_concession",
        "job_id": "20260728_063103_df0fe480",
        "expected_sections": 5,
    },
    "S03": {
        "project_name": "pph_s03_concession_to_unfairness",
        "job_id": "20260728_063453_5344a69b",
        "expected_sections": 5,
    },
    "S04": {
        "project_name": "pph_s04_unfairness_continuance",
        "job_id": "20260728_063507_60155d3b",
        "expected_sections": 10,
    },
    "S05": {
        "project_name": "pph_s05_subjective_knowledge",
        "job_id": "20260728_063507_e48eec64",
        "expected_sections": 13,
    },
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def project_config(project_id: str) -> dict[str, Any]:
    try:
        return PROJECTS[project_id.upper()]
    except KeyError as exc:
        raise ValueError(f"unknown project id: {project_id}") from exc


def workspace_path(project_id: str) -> Path:
    config = project_config(project_id)
    return OUTPUT_ROOT / f"{config['project_name']}__{config['job_id']}"


def summary_path(project_id: str) -> Path:
    config = project_config(project_id)
    return workspace_path(project_id) / "artifacts" / f"{config['project_name']}_summaries.json"


def review_draft_path(project_id: str) -> Path:
    config = project_config(project_id)
    return (
        workspace_path(project_id)
        / "artifacts"
        / "review_drafts"
        / f"{config['project_name']}_review_draft_v2.json"
    )


def citation_manifest_path(project_id: str) -> Path:
    config = project_config(project_id)
    return (
        workspace_path(project_id)
        / "artifacts"
        / "citation_manifests"
        / f"{config['project_name']}_citation_manifest_v3.json"
    )


def citation_catalog_path(project_id: str) -> Path:
    config = project_config(project_id)
    return (
        workspace_path(project_id)
        / "artifacts"
        / "citation_catalogs"
        / f"{config['project_name']}_citation_ref_catalog.json"
    )


def report_docx_path(project_id: str) -> Path:
    config = project_config(project_id)
    return (
        workspace_path(project_id)
        / "reports"
        / f"{config['project_name']}_literature_review.docx"
    )


def resolve_repo_path(value: Any) -> Path:
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


def _prepare_manifest_path(preprocess: Mapping[str, Any]) -> Path:
    for field in ("manifest_path", "stage1_input_manifest_path"):
        value = str(preprocess.get(field) or "").strip()
        if value:
            candidate = resolve_repo_path(value)
            if field == "stage1_input_manifest_path":
                candidate = candidate.parent / "prepare_manifest.json"
            if candidate.is_file():
                return candidate
    cache_dir = str(preprocess.get("cache_dir") or "").strip()
    if cache_dir:
        candidate = resolve_repo_path(cache_dir) / "prepare_manifest.json"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("preprocess prepare_manifest.json is unavailable")


def normalize_preprocess(
    raw_preprocess: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    preprocess = deepcopy(dict(raw_preprocess))
    prepare_manifest_path = _prepare_manifest_path(preprocess)
    prepare_manifest = load_json(prepare_manifest_path)
    artifacts = dict(prepare_manifest.get("artifacts") or {})

    path_candidates = {
        "markdown_path": (
            preprocess.get("markdown_path"),
            artifacts.get("normalized_md"),
            prepare_manifest_path.parent / "normalized.md",
        ),
        "chunks_path": (
            preprocess.get("chunks_path"),
            artifacts.get("chunks"),
            prepare_manifest_path.parent / "chunks.json",
        ),
        "page_index_path": (
            preprocess.get("page_index_path"),
            artifacts.get("page_index"),
            prepare_manifest_path.parent / "page_index.json",
        ),
    }
    for field, candidates in path_candidates.items():
        resolved = None
        for candidate in candidates:
            if not str(candidate or "").strip():
                continue
            candidate_path = resolve_repo_path(candidate)
            if candidate_path.is_file():
                resolved = candidate_path
                break
        if resolved is None:
            raise FileNotFoundError(f"required preprocess evidence is missing: {field}")
        preprocess[field] = str(resolved)

    preprocess["manifest_path"] = str(prepare_manifest_path)
    source_pdf = str(prepare_manifest.get("pdf_path") or "").strip()
    if source_pdf:
        source_pdf_path = resolve_repo_path(source_pdf)
        if source_pdf_path.is_file():
            source_pdf = str(source_pdf_path)
        else:
            source_pdf = ""
    return preprocess, source_pdf


def artifact_hash_for_key(paper_key: str) -> str:
    return hashlib.sha256(paper_key.encode("utf-8")).hexdigest()[:16]


def _dependency_for_record(record: Any) -> Any:
    from services.artifact_registry import ArtifactDependencyRef

    return ArtifactDependencyRef(
        artifact_type=record.artifact_type,
        path=record.path,
        content_hash=record.content_hash,
        dependency_kind="local_job",
        job_id=record.job_id,
        artifact_id=record.artifact_id,
    )


def register_summary_evidence(project_id: str) -> dict[str, Any]:
    from services.artifact_registry import ArtifactRegistry, file_sha256
    from services.evidence_manifest import build_evidence_manifest_v1
    from services.job_workspace import JobWorkspace, atomic_write_json
    from services.paper_artifact import build_paper_artifact_v1

    config = project_config(project_id)
    workspace = JobWorkspace.create(
        str(OUTPUT_ROOT),
        config["project_name"],
        job_id=config["job_id"],
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    summaries = load_json(summary_path(project_id))
    if not isinstance(summaries, list):
        raise ValueError(f"{project_id} summary file is not a JSON array")

    registered: list[dict[str, Any]] = []
    reused: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for index, raw_summary in enumerate(summaries):
        if not isinstance(raw_summary, Mapping) or raw_summary.get("status") != "success":
            continue
        summary = deepcopy(dict(raw_summary))
        paper = deepcopy(dict(summary.get("paper_info") or {}))
        paper_key = str(
            paper.get("canonical_paper_key")
            or paper.get("source_paper_id")
            or paper.get("doi")
            or ""
        ).strip()
        if not paper_key:
            failed.append({"index": index, "reason": "missing canonical paper key"})
            continue
        artifact_hash = artifact_hash_for_key(paper_key)
        paper_artifact_id = f"paper_artifact:{artifact_hash}"
        evidence_manifest_id = f"evidence_manifest:{artifact_hash}"
        existing_paper = registry.get(paper_artifact_id)
        existing_evidence = registry.get(evidence_manifest_id)
        if (
            existing_paper is not None
            and existing_paper.status == "ready"
            and Path(existing_paper.path).is_file()
            and existing_paper.content_hash == file_sha256(existing_paper.path)
            and existing_evidence is not None
            and existing_evidence.status == "ready"
            and Path(existing_evidence.path).is_file()
            and existing_evidence.content_hash == file_sha256(existing_evidence.path)
        ):
            reused.append(
                {
                    "paper_key": paper_key,
                    "paper_artifact_id": paper_artifact_id,
                    "evidence_manifest_id": evidence_manifest_id,
                }
            )
            continue

        try:
            preprocess, recovered_source_pdf = normalize_preprocess(
                dict(summary.get("preprocess") or {})
            )
            if recovered_source_pdf:
                paper["source_pdf"] = recovered_source_pdf
                paper["pdf_path"] = recovered_source_pdf
                paper["source_pdf_fingerprint"] = file_sha256(recovered_source_pdf)
            paper["canonical_paper_key"] = paper_key
            paper.setdefault("source_paper_id", paper_key)
            summary["paper_info"] = paper
            summary["preprocess"] = preprocess

            paper_artifact = build_paper_artifact_v1(
                job_id=workspace.job_id,
                paper=paper,
                result=summary,
                paper_key=paper_key,
            )
            paper_payload = paper_artifact.to_dict()
            evidence_manifest = build_evidence_manifest_v1(
                job_id=workspace.job_id,
                canonical_paper_key=paper_key,
                preprocess=preprocess,
            )

            paper_path = Path(
                workspace.artifact_path(f"paper_artifacts/{artifact_hash}.json")
            )
            evidence_path = Path(
                workspace.artifact_path(
                    f"paper_artifacts/{artifact_hash}.evidence_manifest_v1.json"
                )
            )
            atomic_write_json(evidence_path, evidence_manifest.to_dict())
            paper_payload.setdefault("stage1_inputs", {})[
                "evidence_manifest_path"
            ] = str(evidence_path)
            paper_payload["stage1_inputs"]["evidence_manifest_hash"] = file_sha256(
                evidence_path
            )
            atomic_write_json(paper_path, paper_payload)

            evidence_dependencies = []
            for evidence_ref in evidence_manifest.artifacts:
                evidence_record = registry.register_file(
                    artifact_role="paper_evidence",
                    artifact_type=evidence_ref.artifact_type,
                    artifact_version="v1",
                    path=evidence_ref.path,
                    producer="scripts.pph_validation_closure.register_summary_evidence",
                    artifact_id=f"{evidence_ref.artifact_type}:{artifact_hash}",
                )
                evidence_dependencies.append(_dependency_for_record(evidence_record))

            evidence_record = registry.register_file(
                artifact_role="paper_evidence",
                artifact_type="evidence_manifest",
                artifact_version="v1",
                path=evidence_path,
                producer="scripts.pph_validation_closure.register_summary_evidence",
                artifact_id=evidence_manifest_id,
                depends_on=evidence_dependencies,
            )
            paper_dependencies = [
                *evidence_dependencies,
                _dependency_for_record(evidence_record),
            ]
            if recovered_source_pdf:
                source_record = registry.register_file(
                    artifact_role="source_pdf",
                    artifact_type="source_pdf",
                    artifact_version="v1",
                    path=recovered_source_pdf,
                    producer="scripts.pph_validation_closure.register_summary_evidence",
                    artifact_id=f"source_pdf:{artifact_hash}",
                )
                paper_dependencies.append(_dependency_for_record(source_record))

            paper_record = registry.register_file(
                artifact_role="paper_artifact",
                artifact_type="paper_artifact",
                artifact_version="v1",
                path=paper_path,
                producer="scripts.pph_validation_closure.register_summary_evidence",
                artifact_id=paper_artifact_id,
                depends_on=paper_dependencies,
                metadata={
                    "closure_source": "existing_summary_and_verified_preprocess_cache",
                    "summary_index": index,
                },
            )
            registered.append(
                {
                    "paper_key": paper_key,
                    "paper_artifact_id": paper_record.artifact_id,
                    "paper_artifact_hash": paper_record.content_hash,
                    "evidence_manifest_id": evidence_record.artifact_id,
                    "evidence_manifest_hash": evidence_record.content_hash,
                    "source_pdf": recovered_source_pdf,
                }
            )
        except Exception as exc:
            failed.append(
                {
                    "index": index,
                    "paper_key": paper_key,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )

    audit = {
        "schema_version": "pph_validation_input_closure_v1",
        "created_at": utc_now_iso(),
        "project_id": project_id,
        "project_name": config["project_name"],
        "job_id": config["job_id"],
        "summary_file": str(summary_path(project_id)),
        "summary_count": len(summaries),
        "registered_count": len(registered),
        "reused_count": len(reused),
        "failed_count": len(failed),
        "registered": registered,
        "reused": reused,
        "failed": failed,
    }
    audit_path = (
        workspace_path(project_id)
        / "artifacts"
        / f"{config['project_name']}_validation_input_closure_v1.json"
    )
    atomic_write_json(audit_path, audit)
    registry.register_file(
        artifact_role="audit",
        artifact_type="validation_input_closure",
        artifact_version="v1",
        path=audit_path,
        producer="scripts.pph_validation_closure.register_summary_evidence",
        artifact_id="validation_input_closure:v1",
        status="ready" if not failed else "quarantined",
        metadata={
            "registered_count": len(registered),
            "reused_count": len(reused),
            "failed_count": len(failed),
        },
    )
    return audit


def _find_ready_record_by_path(registry: Any, path: Path) -> Any | None:
    from services.artifact_registry import file_sha256

    resolved = path.resolve()
    for record in registry.list_records():
        record_path = Path(record.path)
        if (
            record_path.resolve() == resolved
            and record.status == "ready"
            and record_path.is_file()
            and record.content_hash == file_sha256(record_path)
        ):
            return record
    return None


def _require_current_ready_record(
    registry: Any,
    *,
    artifact_id: str,
    artifact_type: str,
    path: Path,
) -> Any:
    from services.artifact_registry import file_sha256

    record = registry.get(artifact_id)
    if record is None:
        raise ValueError(f"Registry artifact is missing: {artifact_id}")
    if record.artifact_type != artifact_type:
        raise ValueError(f"Registry artifact type mismatch: {artifact_id}")
    if record.status != "ready":
        raise ValueError(f"Registry artifact is not ready: {artifact_id}")
    record_path = Path(record.path)
    if record_path.resolve() != path.resolve():
        raise ValueError(f"Registry artifact path mismatch: {artifact_id}")
    if not record_path.is_file():
        raise ValueError(f"Registry artifact file is missing: {artifact_id}")
    if record.content_hash != file_sha256(record_path):
        raise ValueError(f"Registry artifact hash is stale: {artifact_id}")
    return record


def _citation_manifest_reuse_snapshot(
    registry: Any,
    *,
    draft_path: Path,
    catalog_path: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    registry.reload()
    draft_record = _require_current_ready_record(
        registry,
        artifact_id="review_draft_v2:full_review",
        artifact_type="review_draft",
        path=draft_path,
    )
    catalog_record = _find_ready_record_by_path(registry, catalog_path)
    if catalog_record is None or catalog_record.artifact_type != "citation_ref_catalog":
        raise ValueError("current citation catalog is not ready in Registry")
    manifest_record = _require_current_ready_record(
        registry,
        artifact_id="citation_manifest:v3",
        artifact_type="citation_manifest",
        path=manifest_path,
    )

    registry.verify_ready_dependencies(draft_record.depends_on)
    registry.verify_ready_dependencies(catalog_record.depends_on)
    verified_manifest_dependencies = registry.verify_ready_dependencies(
        manifest_record.depends_on
    )
    expected_dependencies = sorted(
        (
            record.artifact_type,
            record.artifact_id,
            record.content_hash,
        )
        for record in (draft_record, catalog_record)
    )
    actual_dependencies = sorted(
        (
            dependency.artifact_type,
            dependency.artifact_id,
            dependency.content_hash,
        )
        for dependency in verified_manifest_dependencies
    )
    if actual_dependencies != expected_dependencies:
        raise ValueError(
            "citation manifest Registry dependencies do not match the current draft/catalog"
        )
    return {
        "manifest_record": manifest_record,
        "draft_record": draft_record,
        "catalog_record": catalog_record,
    }


def ensure_citation_manifest(project_id: str, *, force: bool = False) -> dict[str, Any]:
    from services.artifact_registry import ArtifactRegistry, RegistryError, file_sha256
    from services.citation_manifest import build_citation_manifest_v3_from_review_draft
    from services.job_workspace import JobWorkspace, atomic_write_json

    config = project_config(project_id)
    workspace = JobWorkspace.create(
        str(OUTPUT_ROOT),
        config["project_name"],
        job_id=config["job_id"],
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    draft_path = review_draft_path(project_id)
    catalog_path = citation_catalog_path(project_id)
    manifest_path = citation_manifest_path(project_id)
    if not draft_path.is_file():
        raise FileNotFoundError(f"review draft is missing: {draft_path}")
    if not catalog_path.is_file():
        raise FileNotFoundError(f"citation catalog is missing: {catalog_path}")
    if manifest_path.is_file() and not force:
        try:
            reuse = _citation_manifest_reuse_snapshot(
                registry,
                draft_path=draft_path,
                catalog_path=catalog_path,
                manifest_path=manifest_path,
            )
        except (OSError, RegistryError, ValueError):
            reuse = None
        if reuse is not None:
            manifest_record = reuse["manifest_record"]
            return {
                "project_id": project_id,
                "status": "reused",
                "manifest_path": str(manifest_path),
                "manifest_hash": manifest_record.content_hash,
            }

    summaries = load_json(summary_path(project_id))
    draft = load_json(draft_path)
    catalog = load_json(catalog_path)
    registry.reload()
    literature_map = None
    for record in registry.list_records():
        if record.artifact_type == "literature_map" and record.status == "ready":
            candidate = Path(record.path)
            if candidate.is_file():
                literature_map = load_json(candidate)
                break

    draft_record = _require_current_ready_record(
        registry,
        artifact_id="review_draft_v2:full_review",
        artifact_type="review_draft",
        path=draft_path,
    )
    catalog_record = _find_ready_record_by_path(registry, catalog_path)
    if catalog_record is None or catalog_record.artifact_type != "citation_ref_catalog":
        raise ValueError("current citation catalog is not ready in Registry")
    registry.verify_ready_dependencies(draft_record.depends_on)
    registry.verify_ready_dependencies(catalog_record.depends_on)

    manifest = build_citation_manifest_v3_from_review_draft(
        job_id=workspace.job_id,
        project_name=config["project_name"],
        manifest_id="citation_manifest:v3",
        review_draft_path=str(draft_path),
        review_word_path=str(report_docx_path(project_id)),
        review_draft_v2=draft,
        paper_summaries=[dict(item) for item in summaries],
        literature_map=literature_map,
        citation_ref_catalog=catalog,
        citation_ref_catalog_path=str(catalog_path),
        citation_ref_catalog_hash=str(catalog.get("catalog_hash") or ""),
        legacy_citation_policy="report_only",
    )
    atomic_write_json(manifest_path, manifest.to_dict())
    migration_path = (
        manifest_path.parent
        / f"{config['project_name']}_citation_migration_report.json"
    )
    atomic_write_json(migration_path, manifest.migration_report.to_dict())

    dependencies = [
        _dependency_for_record(draft_record),
        _dependency_for_record(catalog_record),
    ]
    manifest_record = registry.register_file(
        artifact_role="citation_manifest",
        artifact_type="citation_manifest",
        artifact_version="v3",
        path=manifest_path,
        producer="scripts.pph_validation_closure.ensure_citation_manifest",
        artifact_id="citation_manifest:v3",
        depends_on=dependencies,
    )
    return {
        "project_id": project_id,
        "status": "created",
        "manifest_path": str(manifest_path),
        "manifest_hash": manifest_record.content_hash,
        "occurrence_count": len(manifest.occurrences),
        "citation_set_count": len(manifest.citation_sets),
        "paper_entry_count": len(manifest.paper_entries),
        "unresolved_count": int(
            manifest.migration_report.fallback_counters.get(
                "unresolved_occurrences",
                0,
            )
        ),
    }


def _require_topic_section_contract(
    project_id: str,
    sections: Iterable[int],
) -> list[int]:
    normalized = [int(section) for section in sections]
    expected = list(
        range(1, int(project_config(project_id)["expected_sections"]) + 1)
    )
    if normalized != expected:
        raise ValueError(
            f"{project_id} section contract mismatch: "
            f"expected={expected}, actual={normalized}"
        )
    return normalized


def _outline_sections(project_id: str) -> list[int]:
    config = project_config(project_id)
    adopted_path = (
        workspace_path(project_id)
        / "artifacts"
        / f"{config['project_name']}_adopted_final_outline.json"
    )
    if adopted_path.is_file():
        from outline.v2_models import AdoptedFinalOutline

        adopted = AdoptedFinalOutline.from_dict(load_json(adopted_path))
        return _require_topic_section_contract(
            project_id,
            range(1, len(adopted.outline.sections) + 1),
        )

    draft = review_draft_path(project_id)
    if draft.is_file():
        payload = load_json(draft)
        source = str(
            (payload.get("generation_context") or {}).get("outline_source_path") or ""
        )
        source_path = Path(source)
        if source and not source_path.is_file():
            source_path = resolve_repo_path(source)
        if source_path.is_file() and source_path.suffix.lower() == ".md":
            text = source_path.read_text(encoding="utf-8")
            return _require_topic_section_contract(
                project_id,
                [
                    int(value)
                    for value in re.findall(
                        r"^##\s*(\d+)\.",
                        text,
                        re.MULTILINE,
                    )
                ],
            )

    candidates = list(
        (workspace_path(project_id) / "artifacts").glob(
            f"{config['project_name']}*outline*.md"
        )
    )
    if candidates:
        text = candidates[0].read_text(encoding="utf-8")
        return _require_topic_section_contract(
            project_id,
            [
                int(value)
                for value in re.findall(
                    r"^##\s*(\d+)\.",
                    text,
                    re.MULTILINE,
                )
            ],
        )
    return _require_topic_section_contract(
        project_id,
        range(1, int(config["expected_sections"]) + 1),
    )


def _outline_section_titles(project_id: str) -> dict[int, str]:
    config = project_config(project_id)
    adopted_path = (
        workspace_path(project_id)
        / "artifacts"
        / f"{config['project_name']}_adopted_final_outline.json"
    )
    if adopted_path.is_file():
        from outline.v2_models import AdoptedFinalOutline

        adopted = AdoptedFinalOutline.from_dict(load_json(adopted_path))
        return {
            index: str(section.title).strip()
            for index, section in enumerate(adopted.outline.sections, start=1)
        }

    draft = review_draft_path(project_id)
    if draft.is_file():
        payload = load_json(draft)
        source = str(
            (payload.get("generation_context") or {}).get("outline_source_path") or ""
        )
        source_path = Path(source)
        if source and not source_path.is_file():
            source_path = resolve_repo_path(source)
        if source_path.is_file() and source_path.suffix.lower() == ".md":
            return _parse_markdown_outline_titles(source_path)

    candidates = list(
        (workspace_path(project_id) / "artifacts").glob(
            f"{config['project_name']}*outline*.md"
        )
    )
    if candidates:
        return _parse_markdown_outline_titles(candidates[0])
    return {}


def _canonical_outline_path(project_id: str) -> Path:
    config = project_config(project_id)
    adopted_path = (
        workspace_path(project_id)
        / "artifacts"
        / f"{config['project_name']}_adopted_final_outline.json"
    )
    if adopted_path.is_file():
        return adopted_path.resolve()

    draft = review_draft_path(project_id)
    if draft.is_file():
        payload = load_json(draft)
        source = str(
            (payload.get("generation_context") or {}).get("outline_source_path") or ""
        )
        source_path = Path(source)
        if source and not source_path.is_file():
            source_path = resolve_repo_path(source)
        if source_path.is_file() and source_path.suffix.lower() == ".md":
            return source_path.resolve()

    candidates = list(
        (workspace_path(project_id) / "artifacts").glob(
            f"{config['project_name']}*outline*.md"
        )
    )
    if candidates:
        return candidates[0].resolve()
    raise FileNotFoundError(f"canonical outline artifact is missing for {project_id}")


def _parse_markdown_outline_titles(path: Path) -> dict[int, str]:
    text = path.read_text(encoding="utf-8")
    return {
        int(number): title.strip()
        for number, title in re.findall(r"^##\s*(\d+)\.\s*(.+?)\s*$", text, re.MULTILINE)
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_staged_sha(
    payload: Mapping[str, Any],
    field_names: tuple[str, ...],
    actual_hash: str,
    *,
    label: str,
) -> None:
    expected = ""
    for field_name in field_names:
        expected = str(payload.get(field_name) or "").strip()
        if expected:
            break
    if not expected:
        raise ValueError(f"staged review is missing {label} sha256")
    if expected != actual_hash:
        raise ValueError(f"{label} sha256 mismatch")


def _resolve_optional_canonical_path(
    payload: Mapping[str, Any],
    field_name: str,
    canonical_path: Path,
    *,
    label: str,
) -> Path:
    raw_value = str(payload.get(field_name) or "").strip()
    if not raw_value:
        return canonical_path
    explicit_path = resolve_repo_path(raw_value)
    if explicit_path != canonical_path:
        raise ValueError(f"staged review {label} does not match the canonical project artifact")
    return explicit_path


def _active_catalog_ref_ids(catalog: Mapping[str, Any]) -> set[str]:
    return {
        str(entry.get("ref_id") or "")
        for entry in catalog.get("entries") or []
        if isinstance(entry, Mapping) and entry.get("status") == "active"
    }


def _validate_staged_content_tokens(content: str, valid_ref_ids: set[str]) -> None:
    from services.citation_ref_catalog import extract_ref_ids_from_token

    for match in re.finditer(r"\[\[[^\]]+\]\]", content):
        token = match.group(0)
        ref_ids = extract_ref_ids_from_token(token)
        if not ref_ids:
            raise ValueError(f"unknown citation token: {token}")
        unknown = [ref_id for ref_id in ref_ids if ref_id not in valid_ref_ids]
        if unknown:
            raise ValueError(f"unknown citation ref_id: {', '.join(unknown)}")

    text_without_tokens = re.sub(r"\[\[cite_ref:[^\]]+\]\]", "", content)
    bare_ref_ids = sorted(
        ref_id
        for ref_id in valid_ref_ids
        if re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(ref_id)}(?![A-Za-z0-9_])",
            text_without_tokens,
        )
    )
    if bare_ref_ids:
        raise ValueError(f"bare citation ref_id is not allowed: {', '.join(bare_ref_ids)}")

    legacy_patterns = [
        r"\[\[cite:[^\]]+\]\]",
        r"\([A-Z][A-Za-z'\u2018\u2019\u02bc-]+(?:\s+et\s+al\.)?,\s*(?:19|20)\d{2}[a-z]?\)",
        r"\b[A-Z][A-Za-z'\u2018\u2019\u02bc-]+(?:\s+[A-Z][A-Za-z'\u2018\u2019\u02bc-]+)*\s*\(\s*(?:19|20)\d{2}[a-z]?\s*\)",
    ]
    for pattern in legacy_patterns:
        if re.search(pattern, content):
            raise ValueError("legacy author-year citation is not allowed")


def validate_staged_review_import(
    project_id: str,
    staged_files: Iterable[Path],
) -> dict[str, Any]:
    from services.citation_ref_catalog import validate_document_ref_catalog

    normalized_project_id = project_id.upper()
    config = project_config(normalized_project_id)
    staged_paths = [Path(path).resolve() for path in staged_files]
    if not staged_paths:
        raise ValueError("at least one --staged-file is required")

    canonical_summary_path = summary_path(normalized_project_id).resolve()
    canonical_catalog_path = citation_catalog_path(normalized_project_id).resolve()
    canonical_outline_path = _canonical_outline_path(normalized_project_id)
    expected_sections = _outline_sections(normalized_project_id)
    expected_titles = _outline_section_titles(normalized_project_id)
    if set(expected_titles) != set(expected_sections):
        raise ValueError("canonical outline titles are unavailable or incomplete")

    merged_sections: list[dict[str, Any]] = []
    outline_file: Path | None = None
    catalog: dict[str, Any] | None = None
    catalog_ref_ids: set[str] = set()

    for staged_path in staged_paths:
        if not staged_path.is_file():
            raise FileNotFoundError(f"staged review file is missing: {staged_path}")
        payload = load_json(staged_path)
        if not isinstance(payload, Mapping):
            raise ValueError(f"staged review must be a JSON object: {staged_path}")
        if str(payload.get("project_id") or "").upper() != normalized_project_id:
            raise ValueError(f"staged review project_id mismatch: {staged_path}")

        current_outline = _resolve_optional_canonical_path(
            payload,
            "outline_file",
            canonical_outline_path,
            label="outline_file",
        )
        if not current_outline.is_file():
            raise FileNotFoundError(f"outline file is missing: {current_outline}")
        _require_staged_sha(
            payload,
            ("outline_file_sha256", "outline_sha256", "outline_hash"),
            _file_sha256(current_outline),
            label="outline file",
        )
        if outline_file is None:
            outline_file = current_outline
        elif outline_file != current_outline:
            raise ValueError("all staged review files for a project must use the same outline file")

        current_summary = _resolve_optional_canonical_path(
            payload,
            "summary_file",
            canonical_summary_path,
            label="summary_file",
        )
        _require_staged_sha(
            payload,
            ("summary_file_sha256", "summary_sha256", "summary_hash"),
            _file_sha256(current_summary),
            label="summary file",
        )

        current_catalog_path = _resolve_optional_canonical_path(
            payload,
            "citation_ref_catalog_path",
            canonical_catalog_path,
            label="citation_ref_catalog_path",
        )
        current_catalog = validate_document_ref_catalog(load_json(current_catalog_path))
        expected_catalog_hash = str(
            payload.get("citation_ref_catalog_hash") or payload.get("catalog_hash") or ""
        ).strip()
        if not expected_catalog_hash:
            raise ValueError("staged review is missing citation_ref_catalog_hash")
        if expected_catalog_hash != str(current_catalog.get("catalog_hash") or ""):
            raise ValueError("citation_ref_catalog_hash mismatch")
        if catalog is None:
            catalog = current_catalog
            catalog_ref_ids = _active_catalog_ref_ids(current_catalog)
        elif str(catalog.get("catalog_hash") or "") != str(current_catalog.get("catalog_hash") or ""):
            raise ValueError("all staged review files for a project must use the same catalog")

        sections = payload.get("sections")
        if not isinstance(sections, list):
            raise ValueError(f"staged review sections must be an array: {staged_path}")
        for section in sections:
            if not isinstance(section, Mapping):
                raise ValueError("staged review section must be an object")
            section_number = int(section.get("section_number") or 0)
            section_title = str(section.get("section_title") or "").strip()
            content = str(section.get("content") or "").strip()
            if not content:
                raise ValueError(f"section {section_number} content is empty")
            if section_title != expected_titles.get(section_number):
                raise ValueError(f"section {section_number} title mismatch")
            _validate_staged_content_tokens(content, catalog_ref_ids)
            merged_sections.append(
                {
                    "section_number": section_number,
                    "section_title": section_title,
                    "content": content,
                }
            )

    section_numbers = [int(section["section_number"]) for section in merged_sections]
    duplicates = sorted({number for number in section_numbers if section_numbers.count(number) > 1})
    if duplicates:
        raise ValueError(f"duplicate staged review sections: {duplicates}")
    if section_numbers != expected_sections:
        if sorted(section_numbers) != expected_sections:
            missing = [number for number in expected_sections if number not in section_numbers]
            extra = [number for number in section_numbers if number not in expected_sections]
            raise ValueError(
                f"staged review sections do not match canonical outline; missing={missing}, extra={extra}"
            )
        merged_sections.sort(key=lambda section: int(section["section_number"]))

    return {
        "project_id": normalized_project_id,
        "project_name": config["project_name"],
        "job_id": config["job_id"],
        "outline_file": str(outline_file),
        "outline_file_sha256": _file_sha256(outline_file) if outline_file else "",
        "summary_file": str(canonical_summary_path),
        "summary_file_sha256": _file_sha256(canonical_summary_path),
        "citation_ref_catalog_path": str(canonical_catalog_path),
        "citation_ref_catalog_hash": str((catalog or {}).get("catalog_hash") or ""),
        "section_count": len(merged_sections),
        "sections": merged_sections,
    }


def _restore_review_chain_backup(project_id: str, backup_root: Path, existing_paths: set[Path]) -> None:
    root = workspace_path(project_id)
    config = project_config(project_id)
    candidates = [
        report_docx_path(project_id),
        review_draft_path(project_id),
        citation_manifest_path(project_id),
        root / "checkpoints" / f"{config['project_name']}_review_checkpoint.json",
        root / "reports" / f"{config['project_name']}_failed_review_sections.json",
    ]
    for path in candidates:
        backup_path = backup_root / path.relative_to(root)
        if backup_path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_path, path)
        elif path not in existing_paths and path.exists():
            path.unlink()


def import_staged_review(project_id: str, staged_files: Iterable[Path]) -> dict[str, Any]:
    from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
    from runtime.orchestrator import AgentRuntimeBridge, AgentRuntimeSession
    from services.artifact_registry import file_sha256
    from services.job_runner import JobRunner

    staged = validate_staged_review_import(project_id, staged_files)
    normalized_project_id = str(staged["project_id"])
    config = project_config(normalized_project_id)
    root = workspace_path(normalized_project_id)
    chain_paths = {
        path
        for path in (
            report_docx_path(normalized_project_id),
            review_draft_path(normalized_project_id),
            citation_manifest_path(normalized_project_id),
            root / "checkpoints" / f"{config['project_name']}_review_checkpoint.json",
            root / "reports" / f"{config['project_name']}_failed_review_sections.json",
        )
        if path.exists()
    }
    backup_root = backup_review_chain(normalized_project_id)
    generator, workspace, registry = bind_existing_generator(normalized_project_id)
    bridge = AgentRuntimeBridge(
        RuntimeJobSpec(
            project_name=config["project_name"],
            source=RuntimeSourceSpec(mode="direct", pdf_folder=workspace.root_dir),
            job_id=config["job_id"],
            config=str(CONFIG_PATH),
            action="generate_review",
            queue_file=str(OUTPUT_ROOT / "_queue" / "queue.json"),
        )
    )
    session = AgentRuntimeSession(
        runner=JobRunner(),
        request=bridge.build_job_request(),
        generator=generator,
        context=SimpleNamespace(workspace=workspace, registry=registry),
    )
    try:
        stage_result = bridge.persist_review_chain(
            session,
            outline_file=str(staged["outline_file"]),
            review_sections=list(staged["sections"]),
            rebuild_docx=True,
            producer="scripts.pph_validation_closure.import_staged_review",
            generation_mode="staged_review_import",
        )
    except Exception:
        _restore_review_chain_backup(normalized_project_id, backup_root, chain_paths)
        raise

    post_run_audit = audit_project(normalized_project_id)
    artifacts = [
        {
            "path": artifact.path,
            "hash": file_sha256(artifact.path) if Path(artifact.path).is_file() else "",
            "artifact_type": artifact.artifact_type,
        }
        for artifact in stage_result.artifacts
    ]
    return {
        "project_id": normalized_project_id,
        "project_name": config["project_name"],
        "job_id": config["job_id"],
        "success": stage_result.success,
        "exit_code": 0 if stage_result.success else 1,
        "message": "staged review imported",
        "backup_root": str(backup_root),
        "section_count": staged["section_count"],
        "artifacts": artifacts,
        "post_run_audit": post_run_audit,
    }


def _require_clean_validation_result(
    registry: Any,
    *,
    validation_run_id: str,
    attempt_id: str = "",
) -> dict[str, Any]:
    from services.artifact_registry import file_sha256
    from validation.input_dependencies import (
        resolve_validation_input_dependencies,
        validate_validation_dependency_contract,
    )
    from validation.run_result import (
        ValidationExecutionStatus,
        ValidationRunDisposition,
        ValidationRunResultV1,
    )

    normalized_run_id = str(validation_run_id or "").strip()
    if not normalized_run_id:
        raise ValueError("validation_run_id is required for the clean validation gate")
    registry.reload()
    record = registry.get(normalized_run_id)
    if record is None:
        raise ValueError(f"validation run is not registered: {normalized_run_id}")
    if record.artifact_type != "validation_run_result" or record.artifact_version != "v1":
        raise ValueError("validation run Registry record is not typed ValidationRunResultV1")
    if record.status != "ready":
        raise ValueError(f"validation run Registry record is not ready: {record.status}")
    result_path = Path(record.path)
    if not result_path.is_file():
        raise ValueError("validation run result file is missing")
    if record.content_hash != file_sha256(result_path):
        raise ValueError("validation run result hash does not match Registry")

    payload = load_json(result_path)
    if not isinstance(payload, Mapping):
        raise ValueError("validation run result must be a JSON object")
    if payload.get("contract_satisfied") is not True:
        raise ValueError("serialized contract_satisfied must be true")
    result = ValidationRunResultV1.from_dict(payload)
    if result.artifact_type != "validation_run_result":
        raise ValueError("validation result is not typed ValidationRunResultV1")
    if result.validation_run_id != record.artifact_id:
        raise ValueError("validation result validation_run_id does not match Registry")
    if result.job_id != registry.job_id:
        raise ValueError("validation result job_id does not match Registry owner")
    if attempt_id and result.attempt_id != attempt_id:
        raise ValueError("validation result attempt_id does not match the current attempt")
    if result.execution_status is not ValidationExecutionStatus.SUCCEEDED:
        raise ValueError("validation execution_status must be succeeded")
    if result.validation_disposition is not ValidationRunDisposition.CLEAN:
        raise ValueError("validation_disposition must be clean")
    if result.contract_satisfied is not True:
        raise ValueError("validation result contract_satisfied must be true")

    validate_validation_dependency_contract(record, result.input_artifacts)
    resolved_dependencies = resolve_validation_input_dependencies(
        registry,
        result.input_artifacts,
    )
    verified_dependencies = registry.verify_ready_dependencies(record.depends_on)
    verified_identities = sorted(
        (
            dependency.artifact_type,
            dependency.artifact_id,
            dependency.content_hash,
        )
        for dependency in verified_dependencies
    )
    resolved_identities = sorted(
        (
            dependency.artifact_type,
            dependency.artifact_id,
            dependency.content_hash,
        )
        for dependency in resolved_dependencies
    )
    if verified_identities != resolved_identities:
        raise ValueError(
            "Validation input dependencies do not match the current Registry closure"
        )
    return {
        "validation_run_id": result.validation_run_id,
        "attempt_id": result.attempt_id,
        "path": str(result_path),
        "hash": record.content_hash,
        "execution_status": result.execution_status.value,
        "validation_disposition": result.validation_disposition.value,
        "contract_satisfied": result.contract_satisfied,
        "dependencies_verified": True,
        "expected_claim_count": result.expected_claim_count,
        "validated_claim_count": result.validated_claim_count,
        "evidence_complete": result.evidence_complete,
        "claim_verdict_counts": dict(result.claim_verdict_counts),
    }


def audit_project(project_id: str) -> dict[str, Any]:
    from services.artifact_registry import ArtifactRegistry, file_sha256
    from services.job_workspace import JobWorkspace

    config = project_config(project_id)
    workspace = JobWorkspace.create(
        str(OUTPUT_ROOT),
        config["project_name"],
        job_id=config["job_id"],
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    draft_path = review_draft_path(project_id)
    manifest_path = citation_manifest_path(project_id)
    actual_sections: list[int] = []
    if draft_path.is_file():
        draft = load_json(draft_path)
        actual_sections = [
            int(item.get("section_number") or 0)
            for item in (draft.get("content") or {}).get("sections") or []
        ]

    validation_records = [
        record
        for record in registry.list_records()
        if record.artifact_type == "validation_run_result"
    ]
    validation_records.sort(key=lambda item: item.created_at)
    latest_validation = None
    if validation_records:
        record = validation_records[-1]
        payload = load_json(Path(record.path)) if Path(record.path).is_file() else {}
        clean_gate = None
        clean_gate_error = ""
        try:
            clean_gate = _require_clean_validation_result(
                registry,
                validation_run_id=record.artifact_id,
            )
        except Exception as exc:
            clean_gate_error = f"{type(exc).__name__}: {exc}"
        latest_validation = {
            "artifact_id": record.artifact_id,
            "registry_status": record.status,
            "path": record.path,
            "hash": record.content_hash,
            "file_hash_current": (
                file_sha256(record.path) if Path(record.path).is_file() else ""
            ),
            "execution_status": payload.get("execution_status"),
            "validation_disposition": payload.get("validation_disposition"),
            "contract_satisfied": payload.get("contract_satisfied"),
            "expected_claim_count": payload.get("expected_claim_count"),
            "validated_claim_count": payload.get("validated_claim_count"),
            "evidence_complete": payload.get("evidence_complete"),
            "repair_status": payload.get("repair_status"),
            "recheck_status": payload.get("recheck_status"),
            "degradation_reasons": payload.get("degradation_reasons"),
            "clean_gate_satisfied": clean_gate is not None,
            "dependencies_verified": bool(
                clean_gate and clean_gate.get("dependencies_verified")
            ),
            "clean_gate_error": clean_gate_error,
        }

    return {
        "project_id": project_id,
        "project_name": config["project_name"],
        "job_id": config["job_id"],
        "workspace": str(workspace_path(project_id)),
        "expected_sections": _outline_sections(project_id),
        "actual_sections": actual_sections,
        "sections_complete": actual_sections == _outline_sections(project_id),
        "review_draft_exists": draft_path.is_file(),
        "citation_manifest_exists": manifest_path.is_file(),
        "ready_paper_artifact_count": sum(
            1
            for record in registry.list_records()
            if record.artifact_type == "paper_artifact" and record.status == "ready"
        ),
        "ready_evidence_manifest_count": sum(
            1
            for record in registry.list_records()
            if record.artifact_type == "evidence_manifest" and record.status == "ready"
        ),
        "latest_validation": latest_validation,
    }


def backup_review_chain(project_id: str) -> Path:
    config = project_config(project_id)
    root = workspace_path(project_id)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = (
        root
        / "artifacts"
        / "validation_closure_backups"
        / f"{timestamp}_{project_id.lower()}"
    )
    candidates = [
        report_docx_path(project_id),
        review_draft_path(project_id),
        citation_manifest_path(project_id),
        root / "checkpoints" / f"{config['project_name']}_review_checkpoint.json",
        root / "reports" / f"{config['project_name']}_failed_review_sections.json",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        destination = backup_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    return backup_root


def bind_existing_generator(project_id: str) -> tuple[Any, Any, Any]:
    import main as legacy_main
    from services.artifact_registry import ArtifactRegistry
    from services.config_compat import CompatConfigView
    from services.job_workspace import JobWorkspace
    from services.progress_state import ResumeStateReport

    config = project_config(project_id)
    root = workspace_path(project_id)
    workspace = JobWorkspace.from_workspace_path(
        str(root),
        config["project_name"],
        job_id=config["job_id"],
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    generator = legacy_main.LiteratureReviewGenerator(
        str(CONFIG_PATH),
        config["project_name"],
        None,
        str(OUTPUT_ROOT / "_queue" / "queue.json"),
        None,
        None,
    )
    if not generator.load_configuration() or generator.config is None:
        raise RuntimeError("configuration load failed")

    resume_payload = load_json(root / "artifacts" / "resume_state_report.json")
    resume_report = ResumeStateReport(**resume_payload)
    compat_config = (
        generator.compat_config
        if generator.compat_config is not None
        else CompatConfigView.from_config(generator.config)
    )
    generator.bind_job_workspace(
        workspace=workspace,
        artifact_registry=registry,
        compat_config=compat_config,
        fingerprint_bundle=dict(resume_report.fingerprint_bundle),
        resume_state_report=resume_report,
    )
    if not generator.load_existing_summaries():
        raise RuntimeError("unable to load existing summaries")
    return generator, workspace, registry


def prepare_outline_v2(project_id: str) -> dict[str, Any]:
    from services.artifact_registry import file_sha256

    config = project_config(project_id)
    generator, workspace, registry = bind_existing_generator(project_id)
    adopted_path = Path(
        workspace.artifact_path(
            f"{config['project_name']}_adopted_final_outline.json"
        )
    )
    adopted_record = registry.get("adopted_final_outline")
    if (
        adopted_record is not None
        and adopted_record.status == "ready"
        and adopted_path.is_file()
        and adopted_record.content_hash == file_sha256(adopted_path)
    ):
        return {
            "project_id": project_id,
            "project_name": config["project_name"],
            "job_id": config["job_id"],
            "status": "reused",
            "success": True,
            "exit_code": 0,
            "adopted_outline_path": str(adopted_path),
            "adopted_outline_hash": adopted_record.content_hash,
            "expected_sections": _outline_sections(project_id),
        }

    generated = bool(generator.create_literature_review_outline())
    adopted = bool(
        generated
        and generator.adopt_outline_v2(
            adopted_by="codex-validation-closure",
            reason=(
                "Explicit adoption after the current Outline v2 coverage and "
                "stage-health gates passed."
            ),
        )
    )
    adopted_record = registry.get("adopted_final_outline")
    success = bool(
        adopted
        and adopted_record is not None
        and adopted_record.status == "ready"
        and adopted_path.is_file()
        and adopted_record.content_hash == file_sha256(adopted_path)
    )
    return {
        "project_id": project_id,
        "project_name": config["project_name"],
        "job_id": config["job_id"],
        "status": "created" if success else "blocked",
        "success": success,
        "exit_code": 0 if success else 1,
        "outline_v2_generated": generated,
        "outline_v2_adopted": adopted,
        "adopted_outline_path": str(adopted_path) if adopted_path.is_file() else "",
        "adopted_outline_hash": (
            file_sha256(adopted_path) if adopted_path.is_file() else ""
        ),
        "expected_sections": _outline_sections(project_id) if success else [],
    }


def generate_full_review(project_id: str) -> dict[str, Any]:
    config = project_config(project_id)
    backup_root = backup_review_chain(project_id)
    root = workspace_path(project_id)
    checkpoint = (
        root
        / "checkpoints"
        / f"{config['project_name']}_review_checkpoint.json"
    )
    failed_sections = (
        root
        / "reports"
        / f"{config['project_name']}_failed_review_sections.json"
    )
    for path in (checkpoint, failed_sections):
        if path.is_file():
            path.unlink()

    generator, workspace, _registry = bind_existing_generator(project_id)
    success = bool(generator.generate_full_review_from_outline())
    return {
        "project_id": project_id,
        "project_name": config["project_name"],
        "job_id": config["job_id"],
        "workspace_path": workspace.root_dir,
        "success": success,
        "exit_code": 0 if success else 1,
        "message": "completed" if success else "review generation failed",
        "backup_root": str(backup_root),
        "post_run_audit": audit_project(project_id),
    }


def validate_project(project_id: str) -> dict[str, Any]:
    from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
    from runtime.orchestrator import AgentRuntimeBridge, AgentRuntimeSession
    from services.job_runner import JobRunner

    config = project_config(project_id)
    generator, workspace, registry = bind_existing_generator(project_id)
    attempt_id = (
        f"closure-{project_id.lower()}-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    bridge = AgentRuntimeBridge(
        RuntimeJobSpec(
            project_name=config["project_name"],
            source=RuntimeSourceSpec(mode="direct", pdf_folder=workspace.root_dir),
            job_id=config["job_id"],
            config=str(CONFIG_PATH),
            action="validate_review",
            queue_file=str(OUTPUT_ROOT / "_queue" / "queue.json"),
            metadata={
                "validation_required": True,
                "require_clean_validation": True,
                "allow_unvalidated_when_validation_optional": False,
            },
        )
    )
    request = bridge.build_job_request()
    session = AgentRuntimeSession(
        runner=JobRunner(),
        request=request,
        generator=generator,
        context=SimpleNamespace(workspace=workspace, registry=registry),
    )
    stage_result = bridge.run_validation(
        session,
        attempt_id=attempt_id,
        producer="scripts.pph_validation_closure.validate_project",
    )
    validation_run_id = str(
        (stage_result.metadata or {}).get("validation_run_id") or ""
    ).strip()
    clean_gate = None
    clean_gate_error = ""
    if stage_result.success:
        try:
            clean_gate = _require_clean_validation_result(
                registry,
                validation_run_id=validation_run_id,
                attempt_id=attempt_id,
            )
        except Exception as exc:
            clean_gate_error = f"{type(exc).__name__}: {exc}"
    else:
        clean_gate_error = str(
            (stage_result.metadata or {}).get("failure_reason")
            or "runtime validation stage failed"
        )
    success = bool(stage_result.success and clean_gate is not None)
    payload = {
        "project_id": project_id,
        "project_name": config["project_name"],
        "job_id": config["job_id"],
        "workspace_path": workspace.root_dir,
        "attempt_id": attempt_id,
        "success": success,
        "exit_code": 0 if success else 1,
        "message": (
            "clean validation contract satisfied"
            if success
            else "clean validation contract not satisfied"
        ),
        "stage_result": asdict(stage_result),
        "clean_validation_gate": clean_gate,
        "clean_validation_gate_error": clean_gate_error,
    }
    payload["post_run_audit"] = audit_project(project_id)
    return payload


def write_run_result(kind: str, project_id: str, payload: Mapping[str, Any]) -> Path:
    from services.job_workspace import atomic_write_json

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = (
        workspace_path(project_id)
        / "artifacts"
        / "validation_closure_runs"
        / f"{timestamp}_{project_id.lower()}_{kind}.json"
    )
    atomic_write_json(path, dict(payload))
    return path


def parse_project_ids(values: Iterable[str]) -> list[str]:
    result = []
    for value in values:
        normalized = value.upper()
        project_config(normalized)
        if normalized not in result:
            result.append(normalized)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Close the PPH review validation evidence chain without rerunning Stage 1."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in (
        "audit",
        "prepare-inputs",
        "ensure-manifest",
        "prepare-outline-v2",
        "generate-review",
        "validate",
    ):
        child = subparsers.add_parser(command)
        child.add_argument("projects", nargs="+", choices=sorted(PROJECTS))
        if command == "ensure-manifest":
            child.add_argument("--force", action="store_true")
    staged = subparsers.add_parser("import-staged-review")
    staged.add_argument("project", choices=sorted(PROJECTS))
    staged.add_argument(
        "--staged-file",
        action="append",
        required=True,
        type=Path,
        help="Staged review JSON file. Repeat to import multiple section shards.",
    )

    args = parser.parse_args(argv)
    project_ids = (
        parse_project_ids([args.project])
        if args.command == "import-staged-review"
        else parse_project_ids(args.projects)
    )
    exit_code = 0

    for project_id in project_ids:
        try:
            if args.command == "audit":
                payload = audit_project(project_id)
            elif args.command == "prepare-inputs":
                payload = register_summary_evidence(project_id)
                if payload["failed_count"]:
                    exit_code = 1
            elif args.command == "ensure-manifest":
                payload = ensure_citation_manifest(project_id, force=bool(args.force))
            elif args.command == "prepare-outline-v2":
                payload = prepare_outline_v2(project_id)
                if not payload.get("success"):
                    exit_code = 1
            elif args.command == "generate-review":
                payload = generate_full_review(project_id)
                if int(payload.get("exit_code") or 0) != 0:
                    exit_code = 1
            elif args.command == "validate":
                payload = validate_project(project_id)
                if int(payload.get("exit_code") or 0) != 0:
                    exit_code = 1
            elif args.command == "import-staged-review":
                payload = import_staged_review(project_id, args.staged_file)
                if int(payload.get("exit_code") or 0) != 0:
                    exit_code = 1
            else:
                raise AssertionError(f"unsupported command: {args.command}")
            result_path = write_run_result(args.command, project_id, payload)
            print(
                json.dumps(
                    {
                        "project_id": project_id,
                        "command": args.command,
                        "result_path": str(result_path),
                        "result": payload,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        except Exception as exc:
            exit_code = 1
            print(
                json.dumps(
                    {
                        "project_id": project_id,
                        "command": args.command,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

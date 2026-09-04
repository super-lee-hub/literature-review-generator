"""Cross-workspace Stage 1 source authority binding for validation (``validation_source_binding/v1``).

The Outline/Review provider lane consumes the compact ``outline_evidence_pack/v1``
projection (status / source_mode / paper_info / ai_summary).  That projection is
deliberately lossy: it drops ``stage1_input``, ``preprocess`` and the evidence
manifest references so provider prompts stay small.

Validation is a different lane.  It must adjudicate claims against the original
paper text, so it needs the canonical Stage 1 authority:

    paper_artifact -> stage1_inputs.evidence_manifest_path (+ hash)
                   -> manifest artifacts (normalized_text / chunks / page_index)

When the review runs as a downstream job (no Stage 1 in the current workspace)
those artifacts live in the *upstream* Stage 1 workspace.  This module builds a
durable binding to that upstream authority and resolves it back into real
paper artifacts, fail-closed on any identity mismatch.

Nothing here is ever shown to a provider: these are runtime/validator authority
metadata (paths + hashes), not prompt content.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

BINDING_ARTIFACT_TYPE = "validation_source_binding"
BINDING_ARTIFACT_VERSION = "v1"
REGISTRY_FILENAME = "artifact_registry.json"

# manifest artifact_type -> ReviewValidator preprocess_evidence field
_EVIDENCE_FIELD_MAP: dict[str, str] = {
    "normalized_text": "markdown_path",
    "plain_text": "plain_text_path",
    "chunks": "chunks_path",
    "page_index": "page_index_path",
    "structured_json": "structured_json_path",
}


def _sha256(path: str | Path) -> str:
    from services.artifact_registry import file_sha256

    return str(file_sha256(str(path)))


def discover_upstream_workspace(summary_source_path: str | Path) -> Path | None:
    """Walk up from a Stage 1 summary file to its owning job workspace."""

    candidate = Path(str(summary_source_path))
    try:
        if candidate.is_file():
            candidate = candidate.parent
    except OSError:
        return None
    for parent in (candidate, *candidate.parents):
        try:
            if (parent / REGISTRY_FILENAME).is_file():
                return parent
        except OSError:
            continue
    return None


def _read_json(path: str | Path) -> Any | None:
    try:
        return json.loads(Path(str(path)).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _registry_records(workspace: Path) -> tuple[list[dict[str, Any]], str]:
    payload = _read_json(workspace / REGISTRY_FILENAME)
    if not isinstance(payload, Mapping):
        return [], ""
    records = payload.get("artifacts")
    if not isinstance(records, list):
        return [], ""
    return [dict(item) for item in records if isinstance(item, Mapping)], str(payload.get("job_id") or "")


def _manifest_evidence(manifest_path: str) -> dict[str, dict[str, str]]:
    payload = _read_json(manifest_path)
    body = payload.get("payload") if isinstance(payload, Mapping) and isinstance(payload.get("payload"), Mapping) else payload
    if not isinstance(body, Mapping):
        return {}
    entries = body.get("artifacts")
    if not isinstance(entries, list):
        return {}
    resolved: dict[str, dict[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        artifact_type = str(entry.get("artifact_type") or "").strip()
        field = _EVIDENCE_FIELD_MAP.get(artifact_type)
        path = str(entry.get("path") or "").strip()
        if not field or not path:
            continue
        resolved[field] = {
            "path": path,
            "content_hash": str(entry.get("content_hash") or "").strip(),
            "manifest_artifact_type": artifact_type,
        }
    return resolved


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """Return ``value`` narrowed to a mapping, or an empty mapping."""

    return value if isinstance(value, Mapping) else {}


def _paper_key_of(payload: Mapping[str, Any]) -> str:
    identity = _as_mapping(payload.get("paper_identity"))
    for key in (identity.get("canonical_paper_key"), identity.get("source_paper_id")):
        text = str(key or "").strip()
        if text:
            return text
    info = _as_mapping(payload.get("paper_info"))
    return str(info.get("canonical_paper_key") or "").strip()


def build_validation_source_binding(
    *,
    summary_sources: Sequence[str],
    local_registry: Any | None = None,
    job_id: str = "",
) -> dict[str, Any]:
    """Build the durable upstream Stage 1 authority binding.

    Local (same-job) paper artifacts win when present: they are already
    authoritative for this workspace.  Otherwise every summary source path is
    traced back to its upstream workspace and its paper artifacts are recorded
    with hashes so validation can verify them before use.
    """

    papers: dict[str, dict[str, Any]] = {}
    diagnostics: list[str] = []
    workspaces: list[str] = []

    local_keys: set[str] = set()
    if local_registry is not None:
        try:
            for record in local_registry.list_records():
                if record.status != "ready" or record.artifact_type != "paper_artifact":
                    continue
                payload = _read_json(record.path)
                body = (
                    payload.get("payload")
                    if isinstance(payload, Mapping) and isinstance(payload.get("payload"), Mapping)
                    else payload
                )
                if isinstance(body, Mapping):
                    local_keys.add(_paper_key_of(body))
        except (OSError, AttributeError, TypeError):
            diagnostics.append("local_registry_unavailable")

    for source in summary_sources:
        text = str(source or "").strip()
        if not text:
            continue
        workspace = discover_upstream_workspace(text)
        if workspace is None:
            diagnostics.append(f"upstream_workspace_unresolved:{Path(text).name}")
            continue
        if str(workspace) in workspaces:
            continue
        workspaces.append(str(workspace))
        records, upstream_job_id = _registry_records(workspace)
        if not records:
            diagnostics.append(f"upstream_registry_unreadable:{Path(text).name}")
            continue
        for record in records:
            if str(record.get("artifact_type") or "") != "paper_artifact":
                continue
            if str(record.get("status") or "") != "ready":
                continue
            payload = _read_json(record.get("path") or "")
            body = (
                payload.get("payload")
                if isinstance(payload, Mapping) and isinstance(payload.get("payload"), Mapping)
                else payload
            )
            if not isinstance(body, Mapping):
                diagnostics.append(f"paper_artifact_unreadable:{record.get('artifact_id')}")
                continue
            paper_key = _paper_key_of(body)
            if not paper_key or paper_key in local_keys or paper_key in papers:
                continue
            stage1_inputs = _as_mapping(body.get("stage1_inputs"))
            manifest_path = str(stage1_inputs.get("evidence_manifest_path") or "").strip()
            entry: dict[str, Any] = {
                "canonical_paper_key": paper_key,
                "source_workspace_job_id": upstream_job_id,
                "source_workspace": str(workspace),
                "stage1_paper_artifact_id": str(record.get("artifact_id") or ""),
                "stage1_paper_artifact_path": str(record.get("path") or ""),
                "stage1_paper_artifact_hash": str(record.get("content_hash") or ""),
                "evidence_manifest_path": manifest_path,
                "evidence_manifest_hash": str(stage1_inputs.get("evidence_manifest_hash") or "").strip(),
                "evidence": {},
            }
            if manifest_path:
                try:
                    entry["evidence"] = _manifest_evidence(manifest_path)
                except OSError:
                    diagnostics.append(f"evidence_manifest_unreadable:{paper_key}")
            papers[paper_key] = entry

    binding = {
        "artifact_type": BINDING_ARTIFACT_TYPE,
        "artifact_version": BINDING_ARTIFACT_VERSION,
        "job_id": str(job_id or ""),
        "upstream_workspaces": tuple(workspaces),
        "papers": papers,
        "diagnostics": tuple(diagnostics),
        "bound_paper_count": len(papers),
    }
    return binding


def _registry_get(registry: Any, artifact_id: str) -> Any | None:
    if registry is None:
        return None
    if isinstance(registry, Mapping):
        for key in (artifact_id, f"paper:{artifact_id}"):
            if key in registry:
                return registry[key]
        entries = registry.get("artifacts")
        if isinstance(entries, list):
            for record in entries:
                if isinstance(record, Mapping) and str(record.get("artifact_id") or "") == artifact_id:
                    return record
        return None
    try:
        return registry.get(artifact_id)
    except (AttributeError, TypeError, KeyError):
        return None


def _record_value(record: Any, name: str) -> str:
    if isinstance(record, Mapping):
        return str(record.get(name) or "").strip()
    return str(getattr(record, name, "") or "").strip()


def resolve_bound_paper_artifacts(
    binding: Mapping[str, Any],
    *,
    external_registry_resolver: Any | None = None,
    present_paper_keys: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """Resolve binding entries back into authoritative Stage 1 paper artifacts.

    Fail-closed: any identity mismatch (missing registry, missing artifact,
    artifact hash drift, evidence manifest hash drift, wrong paper identity)
    yields a ``VALIDATION_SOURCE_AUTHORITY_INVALID`` diagnostic for that paper
    and the artifact is dropped.  It is never downgraded to an
    ``ai_summary``-only synthetic artifact.
    """

    artifacts: list[dict[str, Any]] = []
    problems: list[str] = []
    papers = binding.get("papers")
    if not isinstance(papers, Mapping) or not papers:
        return artifacts, tuple(problems)

    registry_cache: dict[str, Any] = {}

    def registry_for(entry: Mapping[str, Any]) -> Any | None:
        job_id = str(entry.get("source_workspace_job_id") or "").strip()
        if not job_id:
            return None
        if job_id in registry_cache:
            return registry_cache[job_id]
        registry = None
        if callable(external_registry_resolver):
            try:
                registry = external_registry_resolver(job_id)
            except (OSError, TypeError, ValueError, KeyError):
                registry = None
        if registry is None:
            workspace = str(entry.get("source_workspace") or "").strip()
            if workspace:
                payload = _read_json(Path(workspace) / REGISTRY_FILENAME)
                if isinstance(payload, Mapping):
                    registry = payload
        registry_cache[job_id] = registry
        return registry

    wanted = {str(item) for item in present_paper_keys if str(item)}
    for paper_key, entry in papers.items():
        if not isinstance(entry, Mapping):
            problems.append(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:binding_entry_invalid")
            continue
        if wanted and str(paper_key) not in wanted:
            continue
        artifact_path = str(entry.get("stage1_paper_artifact_path") or "").strip()
        expected_hash = str(entry.get("stage1_paper_artifact_hash") or "").strip()
        artifact_id = str(entry.get("stage1_paper_artifact_id") or "").strip()
        if not artifact_path or not expected_hash:
            problems.append(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:binding_incomplete")
            continue

        registry = registry_for(entry)
        if registry is not None and artifact_id:
            record = _registry_get(registry, artifact_id)
            if record is None:
                problems.append(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:artifact_not_in_registry")
                continue
            record_status = _record_value(record, "status")
            record_hash = _record_value(record, "content_hash")
            if record_status and record_status != "ready":
                problems.append(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:artifact_not_ready")
                continue
            if record_hash and record_hash != expected_hash:
                problems.append(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:artifact_hash_mismatch")
                continue
        try:
            actual_hash = _sha256(artifact_path)
        except OSError:
            problems.append(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:artifact_unreadable")
            continue
        if actual_hash != expected_hash:
            problems.append(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:artifact_hash_mismatch")
            continue

        payload = _read_json(artifact_path)
        body = (
            payload.get("payload")
            if isinstance(payload, Mapping) and isinstance(payload.get("payload"), Mapping)
            else payload
        )
        if not isinstance(body, Mapping):
            problems.append(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:artifact_payload_invalid")
            continue
        if _paper_key_of(body) != str(paper_key):
            problems.append(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:paper_identity_mismatch")
            continue

        manifest_path = str(entry.get("evidence_manifest_path") or "").strip()
        expected_manifest_hash = str(entry.get("evidence_manifest_hash") or "").strip()
        if manifest_path:
            if not Path(manifest_path).is_file():
                problems.append(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:evidence_manifest_missing")
                continue
            if expected_manifest_hash:
                try:
                    if _sha256(manifest_path) != expected_manifest_hash:
                        problems.append(
                            f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:evidence_manifest_hash_mismatch"
                        )
                        continue
                except OSError:
                    problems.append(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:evidence_manifest_unreadable")
                    continue

        stage1_inputs = _as_mapping(body.get("stage1_inputs"))
        merged_inputs = dict(stage1_inputs)
        if manifest_path:
            merged_inputs["evidence_manifest_path"] = manifest_path
            if expected_manifest_hash:
                merged_inputs["evidence_manifest_hash"] = expected_manifest_hash
        evidence_paths = _as_mapping(entry.get("evidence"))
        preprocess_evidence = {
            str(field): str(value.get("path") or "")
            for field, value in evidence_paths.items()
            if isinstance(value, Mapping) and str(value.get("path") or "").strip()
        }
        if preprocess_evidence:
            merged_inputs["preprocess_evidence"] = preprocess_evidence
        artifacts.append(
            {
                **body,
                "stage1_inputs": merged_inputs,
                "_validation_source_binding": {
                    "canonical_paper_key": str(paper_key),
                    "source_workspace_job_id": str(entry.get("source_workspace_job_id") or ""),
                    "stage1_paper_artifact_id": artifact_id,
                    "stage1_paper_artifact_hash": expected_hash,
                    "evidence_manifest_hash": expected_manifest_hash,
                },
            }
        )
    return artifacts, tuple(problems)

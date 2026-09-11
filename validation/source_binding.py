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
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from services.evidence_manifest import EvidenceManifestV1, verified_evidence_paths

BINDING_ARTIFACT_TYPE = "validation_source_binding"
BINDING_ARTIFACT_VERSION = "v1"
# The Registry schema version and the semantic binding contract version are
# separate.  A contract revision must produce a new binding identity without
# pretending that the Registry artifact schema itself is supported at a new
# version before its validator exists.
BINDING_CONTRACT_VERSION = "v1"
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


def _canonical_path(path: Any) -> str:
    if not path:
        return ""
    return os.path.normcase(os.path.abspath(os.fspath(path)))


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


def _canonical_manifest(manifest_path: str) -> tuple[EvidenceManifestV1, dict[str, str]]:
    body, failure = _manifest_payload(manifest_path)
    if body is None:
        raise ValueError(failure or "evidence_manifest_unreadable")
    manifest = EvidenceManifestV1.from_dict(body)
    verified = verified_evidence_paths(manifest)
    return manifest, verified


def _manifest_evidence(manifest_path: str) -> dict[str, dict[str, str]]:
    manifest, verified = _canonical_manifest(manifest_path)
    resolved: dict[str, dict[str, str]] = {}
    for item in manifest.artifacts:
        artifact_type = item.artifact_type
        field = _EVIDENCE_FIELD_MAP.get(artifact_type)
        path = verified.get(artifact_type, "")
        if not field:
            continue
        resolved[field] = {
            "path": path,
            "content_hash": item.content_hash,
            "manifest_artifact_type": artifact_type,
        }
    return resolved


def _manifest_failure_label(error: BaseException) -> str:
    """Map canonical manifest failures to stable source-binding diagnostics."""

    message = str(error).lower()
    for artifact_type in ("normalized_text", "chunks", "page_index"):
        if artifact_type in message and "hash" in message:
            return f"{artifact_type}_hash_mismatch"
        if artifact_type in message and "missing" in message:
            return f"{artifact_type}_missing"
    if "duplicate" in message:
        return "manifest_duplicate_evidence_type"
    if "unknown artifact type" in message:
        return "manifest_unknown_evidence_type"
    if "version" in message:
        return "manifest_version_mismatch"
    return "evidence_manifest_invalid"


def _find_registry_record(
    records: Iterable[Mapping[str, Any]],
    *,
    artifact_type: str,
    artifact_id: str = "",
    path: str = "",
    content_hash: str = "",
) -> Mapping[str, Any] | None:
    for record in records:
        if str(record.get("status") or "") != "ready":
            continue
        if str(record.get("artifact_type") or "") != artifact_type:
            continue
        if artifact_id and str(record.get("artifact_id") or "") != artifact_id:
            continue
        if path and _canonical_path(record.get("path")) != _canonical_path(path):
            continue
        if content_hash and str(record.get("content_hash") or "") != content_hash:
            continue
        return record
    return None


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
    source_identities: list[dict[str, str]] = []

    if local_registry is not None:
        try:
            reload_registry = getattr(local_registry, "reload", None)
            if callable(reload_registry):
                reload_registry()
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
        records, upstream_job_id = _registry_records(workspace)
        if not records:
            diagnostics.append(f"upstream_registry_unreadable:{Path(text).name}")
            continue
        source_identity = _summary_source_identity(
            text,
            records=records,
            upstream_job_id=upstream_job_id,
        )
        if source_identity:
            source_identities.append(source_identity)
        if str(workspace) in workspaces:
            continue
        workspaces.append(str(workspace))
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
            if not paper_key or (
                local_registry is not None
                and upstream_job_id
                and str(getattr(local_registry, "job_id", "")) == upstream_job_id
            ):
                continue
            stage1_inputs = _as_mapping(body.get("stage1_inputs"))
            if paper_key in papers:
                existing = papers[paper_key]
                if (
                    str(existing.get("stage1_paper_artifact_hash") or "")
                    != str(record.get("content_hash") or "")
                    or str(existing.get("evidence_manifest_hash") or "")
                    != str(stage1_inputs.get("evidence_manifest_hash") or "")
                ):
                    diagnostics.append(f"external_source_authority_ambiguous:{paper_key}")
                continue
            paper_path = _canonical_path(record.get("path"))
            paper_hash = str(record.get("content_hash") or "").strip()
            if (
                str(record.get("artifact_version") or "").strip() != "v1"
                or not paper_path
                or not paper_hash
                or not Path(paper_path).is_file()
            ):
                diagnostics.append(f"paper_artifact_identity_unverified:{paper_key}")
                continue
            manifest_path = _canonical_path(stage1_inputs.get("evidence_manifest_path"))
            manifest_hash = str(stage1_inputs.get("evidence_manifest_hash") or "").strip()
            manifest_record = _find_registry_record(
                records,
                artifact_type="evidence_manifest",
                path=manifest_path,
                content_hash=manifest_hash,
            )
            entry: dict[str, Any] = {
                "canonical_paper_key": paper_key,
                "source_workspace_job_id": upstream_job_id,
                "source_workspace": str(workspace),
                "stage1_paper_artifact_id": str(record.get("artifact_id") or ""),
                "stage1_paper_artifact_path": paper_path,
                "stage1_paper_artifact_version": str(record.get("artifact_version") or "").strip(),
                "stage1_paper_artifact_hash": paper_hash,
                "evidence_manifest_path": manifest_path,
                "evidence_manifest_hash": manifest_hash,
                "evidence_manifest_artifact_id": (
                    str(manifest_record.get("artifact_id") or "")
                    if manifest_record is not None
                    else ""
                ),
                "evidence_manifest_artifact_type": (
                    str(manifest_record.get("artifact_type") or "")
                    if manifest_record is not None
                    else "evidence_manifest"
                ),
                "evidence_manifest_artifact_version": (
                    str(manifest_record.get("artifact_version") or "")
                    if manifest_record is not None
                    else ""
                ),
                "evidence_manifest_job_id": (
                    str(manifest_record.get("job_id") or upstream_job_id)
                    if manifest_record is not None
                    else upstream_job_id
                ),
                "evidence": {},
            }
            if not manifest_record:
                diagnostics.append(f"evidence_manifest_registry_identity_unverified:{paper_key}")
            if manifest_path and manifest_hash:
                try:
                    entry["evidence"] = _manifest_evidence(manifest_path)
                except (OSError, TypeError, ValueError, KeyError):
                    diagnostics.append(f"evidence_manifest_unreadable:{paper_key}")
            papers[paper_key] = entry

    binding = {
        "artifact_type": BINDING_ARTIFACT_TYPE,
        "artifact_version": BINDING_ARTIFACT_VERSION,
        "binding_contract_version": BINDING_CONTRACT_VERSION,
        "job_id": str(job_id or ""),
        "upstream_workspaces": tuple(sorted(set(workspaces))),
        # A source path is intentionally not part of this identity.  Summary
        # sources are represented by their owning Registry job, artifact ID,
        # and content hash so a workspace move cannot silently change the
        # semantic binding while a changed source set does.
        "summary_source_identities": tuple(
            sorted(
                (dict(identity) for identity in source_identities),
                key=lambda identity: tuple(
                    str(identity.get(field) or "")
                    for field in (
                        "source_job_id",
                        "source_artifact_id",
                        "source_artifact_version",
                        "source_artifact_hash",
                    )
                ),
            )
        ),
        "papers": papers,
        "diagnostics": tuple(diagnostics),
        "bound_paper_count": len(papers),
    }
    return binding


def _summary_source_identity(
    source_path: str,
    *,
    records: Sequence[Mapping[str, Any]],
    upstream_job_id: str,
) -> dict[str, str]:
    """Return a path-independent identity for one summary source.

    A summary file is normally a durable ``summary_file`` Registry artifact.
    Portable or older workspaces may not have that record, so the exact file
    bytes remain a useful fallback identity.  The path is deliberately omitted
    from both forms.
    """

    normalized_path = _canonical_path(source_path)
    matches = [
        record
        for record in records
        if str(record.get("status") or "") == "ready"
        and str(record.get("artifact_type") or "") == "summary_file"
        and _canonical_path(record.get("path")) == normalized_path
    ]
    identities = {
        (
            str(record.get("job_id") or upstream_job_id),
            str(record.get("artifact_id") or ""),
            str(record.get("artifact_version") or ""),
            str(record.get("content_hash") or ""),
        )
        for record in matches
    }
    if len(identities) > 1:
        # Keep the ambiguity visible in the binding payload.  The orchestrator
        # will fail closed when it cannot select a unique current identity.
        return {
            "source_job_id": str(upstream_job_id or ""),
            "source_artifact_id": "",
            "source_artifact_version": "ambiguous",
            "source_artifact_hash": "",
        }
    if identities:
        job_id, artifact_id, artifact_version, content_hash = next(iter(identities))
        return {
            "source_job_id": job_id,
            "source_artifact_id": artifact_id,
            "source_artifact_version": artifact_version,
            "source_artifact_hash": content_hash,
        }
    if normalized_path and Path(normalized_path).is_file():
        try:
            content_hash = _sha256(normalized_path)
        except OSError:
            content_hash = ""
        return {
            "source_job_id": str(upstream_job_id or ""),
            "source_artifact_id": "",
            "source_artifact_version": "unregistered",
            "source_artifact_hash": content_hash,
        }
    return {
        "source_job_id": str(upstream_job_id or ""),
        "source_artifact_id": "",
        "source_artifact_version": "unresolved",
        "source_artifact_hash": "",
    }


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


def _manifest_payload(manifest_path: str) -> tuple[Mapping[str, Any] | None, str]:
    """Read an evidence manifest envelope and return (payload, failure_label)."""

    payload = _read_json(manifest_path)
    body = (
        payload.get("payload")
        if isinstance(payload, Mapping) and isinstance(payload.get("payload"), Mapping)
        else payload
    )
    if not isinstance(body, Mapping):
        return None, "evidence_manifest_unreadable"
    return body, ""


def verify_manifest_semantic_identity(
    *,
    manifest_path: str,
    paper_key: str,
    upstream_job_id: str,
) -> tuple[bool, str]:
    """A manifest hash match alone is not enough: the manifest must belong to
    the bound paper and upstream job with the expected artifact identity."""

    try:
        manifest, _verified = _canonical_manifest(manifest_path)
    except (OSError, TypeError, ValueError, KeyError) as exc:
        return False, str(exc) or "evidence_manifest_unreadable"
    if manifest.canonical_paper_key != str(paper_key):
        return False, "manifest_paper_identity_mismatch"
    if manifest.job_id != str(upstream_job_id):
        return False, "manifest_job_mismatch"
    return True, ""


def verify_leaf_evidence_bytes(
    *,
    paper_key: str,
    evidence: Mapping[str, Any],
) -> tuple[bool, str]:
    """Every leaf evidence file (normalized text / chunks / page index / ...)
    must exist with the exact content hash recorded in the manifest.  Validation
    adjudicates claims against these bytes, so they cannot be stale."""

    required_fields = {"markdown_path", "chunks_path", "page_index_path"}
    if not required_fields.issubset(evidence):
        missing = sorted(required_fields.difference(evidence))
        return False, f"required_evidence_missing:{','.join(missing)}"
    for field, value in evidence.items():
        if not isinstance(value, Mapping):
            continue
        leaf_path = str(value.get("path") or "").strip()
        expected_hash = str(value.get("content_hash") or "").strip()
        artifact_type = str(value.get("manifest_artifact_type") or field).strip()
        if not leaf_path:
            return False, f"{artifact_type}_path_missing"
        if not Path(leaf_path).is_file():
            return False, f"{artifact_type}_missing"
        if not expected_hash:
            return False, f"{artifact_type}_hash_missing"
        try:
            actual_hash = _sha256(leaf_path)
        except OSError:
            return False, f"{artifact_type}_unreadable"
        if actual_hash != expected_hash:
            return False, f"{artifact_type}_hash_mismatch"
    return True, ""


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
        if registry is not None:
            reload_registry = getattr(registry, "reload", None)
            if callable(reload_registry):
                try:
                    reload_registry()
                except (OSError, UnicodeError, TypeError, ValueError):
                    registry = None
        registry_cache[job_id] = registry
        return registry

    wanted = {str(item).strip() for item in present_paper_keys if str(item).strip()}
    for paper_key, entry in papers.items():
        if not isinstance(entry, Mapping):
            problems.append(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:binding_entry_invalid")
            continue
        canonical_key = str(paper_key).strip()
        if wanted and canonical_key not in wanted:
            continue
        artifact_path = _canonical_path(entry.get("stage1_paper_artifact_path"))
        expected_hash = str(entry.get("stage1_paper_artifact_hash") or "").strip()
        artifact_id = str(entry.get("stage1_paper_artifact_id") or "").strip()
        artifact_version = str(entry.get("stage1_paper_artifact_version") or "").strip()
        source_job_id = str(entry.get("source_workspace_job_id") or "").strip()
        manifest_path = _canonical_path(entry.get("evidence_manifest_path"))
        expected_manifest_hash = str(entry.get("evidence_manifest_hash") or "").strip()
        manifest_id = str(entry.get("evidence_manifest_artifact_id") or "").strip()
        manifest_type = str(
            entry.get("evidence_manifest_artifact_type") or ""
        ).strip()
        manifest_version = str(
            entry.get("evidence_manifest_artifact_version") or ""
        ).strip()
        manifest_job_id = str(entry.get("evidence_manifest_job_id") or "").strip()
        if (
            not artifact_path
            or not expected_hash
            or not artifact_id
            or artifact_version != "v1"
            or not source_job_id
            or not manifest_path
            or not expected_manifest_hash
            or not manifest_id
            or manifest_type != "evidence_manifest"
            or manifest_version != "v1"
            or not manifest_job_id
        ):
            problems.append(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:binding_incomplete")
            continue

        registry = registry_for(entry)
        if registry is None:
            # The binding declares an external Stage 1 authority; an
            # unresolvable registry is a hard failure, never a silent skip.
            problems.append(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:registry_missing")
            continue
        record = _registry_get(registry, artifact_id)
        if record is None:
            problems.append(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:artifact_not_in_registry")
            continue
        # Full record identity binding: status, type, version, job, path and
        # content hash must all match what the binding recorded at build time.
        record_status = _record_value(record, "status")
        if record_status != "ready":
            problems.append(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:artifact_not_ready")
            continue
        paper_identity_checks = (
            ("artifact_type", "paper_artifact", "artifact_type"),
            ("artifact_version", artifact_version, "artifact_version"),
            ("job_id", source_job_id, "job_id"),
            ("content_hash", expected_hash, "artifact_hash"),
        )
        paper_mismatch = next(
            (
                label
                for name, expected, label in paper_identity_checks
                if _record_value(record, name) != expected
            ),
            "",
        )
        if paper_mismatch:
            problems.append(
                f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:registry_{paper_mismatch}_mismatch"
            )
            continue
        if _canonical_path(_record_value(record, "path")) != artifact_path:
            problems.append(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:registry_path_mismatch")
            continue
        try:
            if _sha256(artifact_path) != expected_hash:
                problems.append(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:artifact_hash_mismatch")
                continue
        except OSError:
            problems.append(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:artifact_unreadable")
            continue

        manifest_record = _registry_get(registry, manifest_id)
        if manifest_record is None:
            problems.append(
                f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:evidence_manifest_not_in_registry"
            )
            continue
        if _record_value(manifest_record, "status") != "ready":
            problems.append(
                f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:evidence_manifest_not_ready"
            )
            continue
        manifest_checks = (
            ("artifact_type", manifest_type, "artifact_type"),
            ("artifact_version", manifest_version, "artifact_version"),
            ("job_id", manifest_job_id, "job_id"),
            ("content_hash", expected_manifest_hash, "hash"),
        )
        manifest_mismatch = next(
            (
                label
                for name, expected, label in manifest_checks
                if _record_value(manifest_record, name) != expected
            ),
            "",
        )
        if manifest_mismatch:
            problems.append(
                f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:evidence_manifest_registry_{manifest_mismatch}_mismatch"
            )
            continue
        if _canonical_path(_record_value(manifest_record, "path")) != manifest_path:
            problems.append(
                f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:evidence_manifest_registry_path_mismatch"
            )
            continue
        if not Path(manifest_path).is_file():
            problems.append(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:evidence_manifest_missing")
            continue
        try:
            if _sha256(manifest_path) != expected_manifest_hash:
                problems.append(
                    f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:evidence_manifest_hash_mismatch"
                )
                continue
            manifest, _verified = _canonical_manifest(manifest_path)
        except (OSError, TypeError, ValueError, KeyError) as exc:
            problems.append(
                f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:{_manifest_failure_label(exc)}:{exc}"
            )
            continue
        if manifest.canonical_paper_key != canonical_key:
            problems.append(
                f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:manifest_paper_identity_mismatch"
            )
            continue
        if manifest.job_id != source_job_id or manifest_job_id != source_job_id:
            problems.append(
                f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:manifest_job_mismatch"
            )
            continue
        try:
            canonical_evidence = _manifest_evidence(manifest_path)
        except (OSError, TypeError, ValueError, KeyError) as exc:
            problems.append(
                f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:{_manifest_failure_label(exc)}:{exc}"
            )
            continue
        bound_evidence = entry.get("evidence")
        if not isinstance(bound_evidence, Mapping) or dict(bound_evidence) != canonical_evidence:
            problems.append(
                f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:evidence_binding_mismatch"
            )
            continue
        leaves_ok, leaf_failure = verify_leaf_evidence_bytes(
            paper_key=canonical_key,
            evidence=canonical_evidence,
        )
        if not leaves_ok:
            problems.append(
                f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:{leaf_failure}"
            )
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
        if _paper_key_of(body) != canonical_key:
            problems.append(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{paper_key}:paper_identity_mismatch")
            continue

        stage1_inputs = _as_mapping(body.get("stage1_inputs"))
        merged_inputs = dict(stage1_inputs)
        merged_inputs["evidence_manifest_path"] = manifest_path
        merged_inputs["evidence_manifest_hash"] = expected_manifest_hash
        preprocess_evidence = {
            str(field): str(value.get("path") or "")
            for field, value in canonical_evidence.items()
            if isinstance(value, Mapping) and str(value.get("path") or "").strip()
        }
        merged_inputs["preprocess_evidence"] = preprocess_evidence
        artifacts.append(
            {
                **body,
                "stage1_inputs": merged_inputs,
                "_validation_source_binding": {
                    "canonical_paper_key": canonical_key,
                    "source_workspace_job_id": source_job_id,
                    "stage1_paper_artifact_id": artifact_id,
                    "stage1_paper_artifact_type": "paper_artifact",
                    "stage1_paper_artifact_version": artifact_version,
                    "stage1_paper_artifact_path": artifact_path,
                    "stage1_paper_artifact_hash": expected_hash,
                    "evidence_manifest_artifact_id": manifest_id,
                    "evidence_manifest_artifact_type": manifest_type,
                    "evidence_manifest_artifact_version": manifest_version,
                    "evidence_manifest_job_id": manifest_job_id,
                    "evidence_manifest_path": manifest_path,
                    "evidence_manifest_hash": expected_manifest_hash,
                    "evidence": dict(canonical_evidence),
                },
            }
        )
    return artifacts, tuple(problems)


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    import hashlib

    return hashlib.sha256(encoded).hexdigest()


def _mapping_from_pairs(value: Any) -> Mapping[str, Any]:
    """Normalize the tuple-of-pairs representation used by JSON boundaries."""

    if isinstance(value, Mapping):
        return value
    if isinstance(value, (list, tuple)):
        return {
            str(item[0]): item[1]
            for item in value
            if isinstance(item, (list, tuple)) and len(item) == 2
        }
    return {}


def _semantic_source_identity(value: Any) -> dict[str, str]:
    identity = _mapping_from_pairs(value)
    return {
        field: str(identity.get(field) or "").strip()
        for field in (
            "source_job_id",
            "source_artifact_id",
            "source_artifact_version",
            "source_artifact_hash",
        )
    }


def _semantic_evidence_identity(field: str, value: Any) -> dict[str, str]:
    evidence = _mapping_from_pairs(value)
    return {
        "manifest_artifact_type": str(
            evidence.get("manifest_artifact_type") or field
        ).strip(),
        "content_hash": str(evidence.get("content_hash") or "").strip(),
    }


def _validation_source_binding_semantic_payload(
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Project a binding onto source-authority semantics only.

    A binding payload also carries physical locators so the validator can find
    files and Registry records.  Those locators, the downstream owner, and
    diagnostics are operational metadata; including them in the identity
    would make a workspace relocation look like source drift.  The projection
    below deliberately names the complete semantic contract instead of
    recursively filtering arbitrary keys, so a future locator cannot silently
    become an identity input.
    """

    envelope_payload = binding.get("payload")
    if isinstance(envelope_payload, Mapping):
        binding = envelope_payload

    source_identities = [
        _semantic_source_identity(item)
        for item in (binding.get("summary_source_identities") or ())
    ]
    source_identities.sort(
        key=lambda item: tuple(item.get(field) or "" for field in item)
    )

    paper_payloads: list[dict[str, Any]] = []
    raw_papers = binding.get("papers")
    if isinstance(raw_papers, Mapping):
        paper_items = raw_papers.items()
    else:
        paper_items = ()
    paper_fields = (
        "canonical_paper_key",
        "source_workspace_job_id",
        "stage1_paper_artifact_id",
        "stage1_paper_artifact_version",
        "stage1_paper_artifact_hash",
        "evidence_manifest_artifact_id",
        "evidence_manifest_artifact_type",
        "evidence_manifest_artifact_version",
        "evidence_manifest_job_id",
        "evidence_manifest_hash",
    )
    for paper_key, raw_entry in paper_items:
        entry = _mapping_from_pairs(raw_entry)
        semantic_entry: dict[str, Any] = {
            field: str(entry.get(field) or "").strip()
            for field in paper_fields
        }
        semantic_entry["canonical_paper_key"] = str(
            semantic_entry.get("canonical_paper_key") or paper_key
        ).strip()
        raw_evidence = entry.get("evidence")
        evidence_items = (
            raw_evidence.items() if isinstance(raw_evidence, Mapping) else ()
        )
        semantic_entry["evidence"] = {
            str(field): _semantic_evidence_identity(str(field), value)
            for field, value in sorted(evidence_items, key=lambda item: str(item[0]))
        }
        paper_payloads.append(semantic_entry)
    paper_payloads.sort(key=lambda item: item.get("canonical_paper_key") or "")

    return {
        "artifact_type": str(binding.get("artifact_type") or "").strip(),
        "artifact_version": str(binding.get("artifact_version") or "").strip(),
        "binding_contract_version": str(
            binding.get("binding_contract_version") or ""
        ).strip(),
        "summary_source_identities": source_identities,
        "papers": paper_payloads,
    }


def validation_source_binding_semantic_hash(binding: Mapping[str, Any]) -> str:
    """Return the path-independent semantic identity of a binding."""

    return _stable_hash(_validation_source_binding_semantic_payload(binding))


def validation_source_binding_payload_hash(binding: Mapping[str, Any]) -> str:
    """Backward-compatible name for the semantic binding identity.

    Older callers used ``payload_hash`` for the content-addressed binding ID.
    The ID now hashes the explicit semantic projection, while the Registry's
    ``ArtifactRecord.content_hash`` remains the physical JSON-file hash.
    """

    return validation_source_binding_semantic_hash(binding)


def _registry_records_for_fingerprint(registry: Any) -> list[Any]:
    if registry is None:
        return []
    reload_registry = getattr(registry, "reload", None)
    if callable(reload_registry):
        reload_registry()
    try:
        if isinstance(registry, Mapping):
            values = registry.get("artifacts")
            return [item for item in values or () if isinstance(item, Mapping)]
        return list(registry.list_records())
    except (AttributeError, OSError, TypeError, ValueError):
        return []


def _fingerprint_record_value(record: Any, name: str) -> str:
    return _record_value(record, name)


def build_validation_source_authority_fingerprint(
    *,
    paper_artifacts: Sequence[Mapping[str, Any]],
    registry: Any,
    cited_paper_keys: Iterable[str],
    current_binding_artifact_id: str = "",
    current_binding_content_hash: str = "",
    current_binding_semantic_hash: str = "",
    binding_contract_version: str = BINDING_CONTRACT_VERSION,
) -> tuple[dict[str, Any], str, tuple[str, ...]]:
    """Build one path-independent fingerprint for all cited source authorities.

    The fingerprint contains durable artifact identities and the canonical
    EvidenceManifest leaf hashes.  Paths are deliberately excluded so an
    authority can move without changing Validation identity, while any byte,
    Registry status, or source-paper identity drift changes the result.
    """

    records = _registry_records_for_fingerprint(registry)
    cited = sorted({str(item).strip() for item in cited_paper_keys if str(item).strip()})
    by_key: dict[str, Mapping[str, Any]] = {}
    for artifact in paper_artifacts:
        key = _paper_key_of(artifact)
        if key and key not in by_key:
            by_key[key] = artifact

    paper_records_by_key: dict[str, Any] = {}
    for record in records:
        if _fingerprint_record_value(record, "artifact_type") != "paper_artifact":
            continue
        payload = _read_json(_fingerprint_record_value(record, "path"))
        body = (
            payload.get("payload")
            if isinstance(payload, Mapping) and isinstance(payload.get("payload"), Mapping)
            else payload
        )
        if isinstance(body, Mapping):
            key = _paper_key_of(body)
            if key and key not in paper_records_by_key:
                paper_records_by_key[key] = record

    fingerprint_entries: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    for paper_key in cited:
        artifact = by_key.get(paper_key)
        if artifact is None:
            diagnostics.append(f"source_authority_missing:{paper_key}")
            fingerprint_entries.append({"canonical_paper_key": paper_key, "status": "missing"})
            continue

        bound = artifact.get("_validation_source_binding")
        binding = bound if isinstance(bound, Mapping) else {}
        artifact_path = _canonical_path(
            binding.get("stage1_paper_artifact_path")
            or artifact.get("_registry_path")
            or _as_mapping(artifact.get("source")).get("source_pdf")
        )
        paper_record = None
        for record in records:
            if (
                _fingerprint_record_value(record, "artifact_type") == "paper_artifact"
                and _canonical_path(_fingerprint_record_value(record, "path")) == artifact_path
            ):
                paper_record = record
                break
        if paper_record is None:
            paper_record = paper_records_by_key.get(paper_key)
        if paper_record is not None and not artifact_path:
            artifact_path = _canonical_path(_fingerprint_record_value(paper_record, "path"))
        if paper_record is not None:
            try:
                if _sha256(_fingerprint_record_value(paper_record, "path")) != _fingerprint_record_value(
                    paper_record, "content_hash"
                ):
                    diagnostics.append(f"source_authority_paper_hash_mismatch:{paper_key}")
            except OSError:
                diagnostics.append(f"source_authority_paper_unreadable:{paper_key}")
        paper_artifact_id = str(
            binding.get("stage1_paper_artifact_id")
            or _fingerprint_record_value(paper_record, "artifact_id")
            if paper_record is not None
            else binding.get("stage1_paper_artifact_id") or ""
        ).strip()
        paper_artifact_version = str(
            binding.get("stage1_paper_artifact_version")
            or _fingerprint_record_value(paper_record, "artifact_version")
            if paper_record is not None
            else binding.get("stage1_paper_artifact_version") or ""
        ).strip()
        paper_artifact_hash = str(
            binding.get("stage1_paper_artifact_hash")
            or _fingerprint_record_value(paper_record, "content_hash")
            if paper_record is not None
            else binding.get("stage1_paper_artifact_hash") or ""
        ).strip()
        source_job_id = str(
            binding.get("source_workspace_job_id")
            or _fingerprint_record_value(paper_record, "job_id")
            if paper_record is not None
            else binding.get("source_workspace_job_id") or ""
        ).strip()

        stage1_inputs = _as_mapping(artifact.get("stage1_inputs"))
        manifest_path = _canonical_path(
            binding.get("evidence_manifest_path")
            or stage1_inputs.get("evidence_manifest_path")
        )
        manifest_hash = str(
            binding.get("evidence_manifest_hash")
            or stage1_inputs.get("evidence_manifest_hash")
            or ""
        ).strip()
        manifest_id = str(binding.get("evidence_manifest_artifact_id") or "").strip()
        manifest_version = str(binding.get("evidence_manifest_artifact_version") or "").strip()
        manifest_job_id = str(binding.get("evidence_manifest_job_id") or source_job_id).strip()
        manifest_record = None
        for record in records:
            if (
                _fingerprint_record_value(record, "artifact_type") == "evidence_manifest"
                and (
                    manifest_id
                    and _fingerprint_record_value(record, "artifact_id") == manifest_id
                    or not manifest_id
                    and _canonical_path(_fingerprint_record_value(record, "path")) == manifest_path
                )
            ):
                manifest_record = record
                break
        if manifest_record is not None:
            manifest_id = manifest_id or _fingerprint_record_value(manifest_record, "artifact_id")
            manifest_version = manifest_version or _fingerprint_record_value(manifest_record, "artifact_version")
            manifest_job_id = manifest_job_id or _fingerprint_record_value(manifest_record, "job_id")
            manifest_hash = manifest_hash or _fingerprint_record_value(manifest_record, "content_hash")
        # The binding already carries the canonical leaf hashes.  Use those
        # semantic identities first so a moved locator cannot change the
        # fingerprint.  When the manifest is physically available, re-read it
        # as an additional consistency check and refresh the same hash map.
        leaf_hashes: dict[str, str] = {}
        bound_evidence = binding.get("evidence")
        if isinstance(bound_evidence, Mapping):
            for field, value in bound_evidence.items():
                if isinstance(value, Mapping):
                    artifact_type = str(
                        value.get("manifest_artifact_type") or field
                    ).strip()
                    content_hash = str(value.get("content_hash") or "").strip()
                    if artifact_type and content_hash:
                        leaf_hashes[artifact_type] = content_hash
        if manifest_path and Path(manifest_path).is_file():
            try:
                actual_manifest_hash = _sha256(manifest_path)
                if manifest_hash and actual_manifest_hash != manifest_hash:
                    diagnostics.append(
                        f"source_authority_manifest_hash_mismatch:{paper_key}"
                    )
                    manifest_hash = actual_manifest_hash
                manifest, _verified = _canonical_manifest(manifest_path)
                if manifest.canonical_paper_key != paper_key:
                    raise ValueError("manifest paper identity mismatch")
                if manifest.job_id != source_job_id:
                    raise ValueError("manifest job identity mismatch")
                leaf_hashes = {
                    item.artifact_type: item.content_hash
                    for item in manifest.artifacts
                }
            except (OSError, TypeError, ValueError, KeyError) as exc:
                diagnostics.append(f"source_authority_manifest_invalid:{paper_key}:{exc}")

        if isinstance(bound_evidence, Mapping):
            for field, value in bound_evidence.items():
                if not isinstance(value, Mapping):
                    continue
                artifact_type = str(
                    value.get("manifest_artifact_type") or field
                ).strip()
                leaf_path = str(value.get("path") or "").strip()
                expected_leaf_hash = str(value.get("content_hash") or "").strip()
                if not leaf_path or not Path(leaf_path).is_file():
                    diagnostics.append(
                        f"source_authority_leaf_unreadable:{paper_key}:{artifact_type}"
                    )
                    continue
                try:
                    actual_leaf_hash = _sha256(leaf_path)
                except OSError:
                    diagnostics.append(
                        f"source_authority_leaf_unreadable:{paper_key}:{artifact_type}"
                    )
                    continue
                if expected_leaf_hash and actual_leaf_hash != expected_leaf_hash:
                    diagnostics.append(
                        f"source_authority_leaf_hash_mismatch:{paper_key}:{artifact_type}"
                    )
                    leaf_hashes[artifact_type] = actual_leaf_hash

        if (
            not paper_artifact_id
            or paper_artifact_version != "v1"
            or not paper_artifact_hash
            or not source_job_id
            or not manifest_id
            or manifest_version != "v1"
            or not manifest_job_id
            or not manifest_hash
            or not leaf_hashes
        ):
            diagnostics.append(f"source_authority_incomplete:{paper_key}")

        fingerprint_entries.append(
            {
                "binding_contract_version": str(
                    binding_contract_version or BINDING_CONTRACT_VERSION
                ),
                "canonical_paper_key": paper_key,
                "source_job_id": source_job_id,
                "paper_artifact_id": paper_artifact_id,
                "paper_artifact_version": paper_artifact_version,
                "paper_artifact_hash": paper_artifact_hash,
                "evidence_manifest_id": manifest_id,
                "evidence_manifest_version": manifest_version,
                "evidence_manifest_hash": manifest_hash,
                "normalized_text_hash": leaf_hashes.get("normalized_text", ""),
                "chunks_hash": leaf_hashes.get("chunks", ""),
                "page_index_hash": leaf_hashes.get("page_index", ""),
                "structured_json_hash": leaf_hashes.get("structured_json", ""),
            }
        )

    fingerprint = {
        "artifact_type": "validation_source_authority_fingerprint",
        "artifact_version": str(binding_contract_version or BINDING_ARTIFACT_VERSION),
        "binding_contract_version": str(
            binding_contract_version or BINDING_CONTRACT_VERSION
        ),
        "current_binding_artifact_id": str(current_binding_artifact_id or ""),
        "current_binding_semantic_hash": str(current_binding_semantic_hash or ""),
        # This is retained as audit metadata, but is intentionally excluded
        # by validation_source_authority_hash from the semantic checkpoint
        # identity because it hashes a path-bearing binding JSON file.
        "current_binding_content_hash": str(current_binding_content_hash or ""),
        "papers": fingerprint_entries,
    }
    return fingerprint, validation_source_authority_hash(fingerprint), tuple(
        dict.fromkeys(diagnostics)
    )


def validation_source_authority_hash(fingerprint: Mapping[str, Any]) -> str:
    """Return the canonical digest for a persisted authority fingerprint."""

    semantic = dict(fingerprint)
    # Registry content hashes and file paths are physical audit evidence.  The
    # source-authority checkpoint identity must remain stable when only the
    # binding file/locator moves.  Paper, manifest, and leaf hashes remain in
    # the fingerprint entries and therefore continue to invalidate on source
    # byte drift.
    semantic.pop("current_binding_content_hash", None)
    semantic.pop("current_binding_physical_hash", None)
    return _stable_hash(semantic)


__all__ = [
    "BINDING_ARTIFACT_TYPE",
    "BINDING_ARTIFACT_VERSION",
    "BINDING_CONTRACT_VERSION",
    "build_validation_source_authority_fingerprint",
    "build_validation_source_binding",
    "discover_upstream_workspace",
    "resolve_bound_paper_artifacts",
    "verify_leaf_evidence_bytes",
    "verify_manifest_semantic_identity",
    "validation_source_authority_hash",
    "validation_source_binding_semantic_hash",
    "validation_source_binding_payload_hash",
]

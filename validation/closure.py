"""Provider-free closure checks for the canonical review chain.

The validator and repair modules pre-date the runtime control plane and expose
useful, lower-level contracts.  This module is the small deterministic join
between those contracts and the durable :class:`ArtifactRegistry`: it verifies
the current draft/manifest identities, checks citation object coverage, and
projects a registered ``ValidationRunResultV1`` without treating a DOCX as a
source of truth.

The service is deliberately read-only by default.  A caller may persist the
result as a new derived artifact, but it never edits a READY input artifact.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from services.artifact_registry import ArtifactRecord, ArtifactRegistry, RegistryError
from services.job_workspace import JobWorkspace, atomic_write_json, utc_now_iso
from validation.run_result import (
    ValidationRunResultError,
    ValidationRunResultV1,
)


VALIDATION_CLOSURE_ARTIFACT_TYPE = "validation_closure"
VALIDATION_CLOSURE_ARTIFACT_VERSION = "v1"
_RENDER_POLICY_FIELDS = (
    "citation_style",
    "citation_locale",
    "citation_render_mode",
    "style_engine_version",
    "bibliography_sort_policy",
    "narrative_parenthetical_policy",
)


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_object(path: str | Path) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _record_payload(record: ArtifactRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "artifact_id": record.artifact_id,
        "artifact_type": record.artifact_type,
        "artifact_version": record.artifact_version,
        "artifact_role": record.artifact_role,
        "path": record.path,
        "content_hash": record.content_hash,
        "status": record.status,
    }


def _choose_record(
    records: Sequence[ArtifactRecord],
    *,
    artifact_type: str,
    version: str,
    preferred_ids: Iterable[str] = (),
) -> ArtifactRecord | None:
    candidates = [
        record
        for record in records
        if record.status == "ready"
        and record.artifact_type == artifact_type
        and record.artifact_version == version
    ]
    preferred = tuple(str(item) for item in preferred_ids if str(item))
    for artifact_id in preferred:
        for record in candidates:
            if record.artifact_id == artifact_id:
                return record
    return max(candidates, key=lambda item: (item.created_at, item.artifact_id), default=None)


def _iter_blocks(review_draft: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    content = review_draft.get("content")
    if not isinstance(content, Mapping):
        return []
    sections = content.get("sections")
    if not isinstance(sections, list):
        return []
    blocks: list[Mapping[str, Any]] = []
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        raw_blocks = section.get("blocks")
        if not isinstance(raw_blocks, list):
            continue
        blocks.extend(item for item in raw_blocks if isinstance(item, Mapping))
    return blocks


def _citation_ref_ids(block: Mapping[str, Any]) -> set[str]:
    refs = block.get("citation_refs")
    if not isinstance(refs, list):
        return set()
    result: set[str] = set()
    for ref in refs:
        if isinstance(ref, Mapping):
            for key in ("local_ref_id", "ref_id", "citation_id", "paper_key", "paper_id"):
                value = str(ref.get(key) or "").strip()
                if value:
                    result.add(value)
        elif str(ref).strip():
            result.add(str(ref).strip())
    return result


@dataclass(frozen=True)
class ValidationClosureResult:
    job_id: str
    status: str
    citation_status: str
    semantic_status: str
    repair_status: str
    input_artifacts: Mapping[str, Any]
    validation_artifact: Mapping[str, Any] | None
    citation_counts: Mapping[str, int]
    semantic: Mapping[str, Any]
    blocking_issues: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    checked_at: str = ""
    evidence_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["blocking_issues"] = list(self.blocking_issues)
        payload["findings"] = list(self.findings)
        return payload

    @property
    def clean(self) -> bool:
        return self.status == "clean"


@dataclass(frozen=True)
class _InputResolution:
    draft: ArtifactRecord | None
    manifest: ArtifactRecord | None
    validation: ArtifactRecord | None
    blocking: tuple[str, ...]


class ValidationClosureService:
    """Read-only canonical draft/manifest/validation closure service."""

    def __init__(self, workspace: JobWorkspace, registry: ArtifactRegistry) -> None:
        self.workspace = workspace
        self.registry = registry

    def _resolve_inputs(self) -> _InputResolution:
        records = self.registry.list_records()
        blocking: list[str] = []
        draft = _choose_record(
            records,
            artifact_type="review_draft",
            version="v3",
            preferred_ids=("review_draft",),
        )
        manifest = _choose_record(
            records,
            artifact_type="citation_manifest",
            version="v3",
            preferred_ids=("citation_manifest:v3",),
        )
        validation = _choose_record(
            records,
            artifact_type="validation_run_result",
            version="v1",
        )
        if draft is None:
            blocking.append("canonical_review_draft_missing")
        if manifest is None:
            blocking.append("canonical_citation_manifest_v3_missing")
        if validation is None:
            blocking.append("validation_run_result_missing")
        return _InputResolution(draft, manifest, validation, tuple(blocking))

    def _verify_record(self, record: ArtifactRecord | None, label: str, blocking: list[str]) -> None:
        if record is None:
            return
        try:
            ArtifactRegistry._verify_ready_artifact(record)
        except (OSError, RegistryError, ValueError, TypeError) as exc:
            blocking.append(f"{label}_hash_untrusted:{exc}")

    def inspect(self) -> ValidationClosureResult:
        resolution = self._resolve_inputs()
        blocking = list(resolution.blocking)
        findings: list[str] = []
        self._verify_record(resolution.draft, "review_draft", blocking)
        self._verify_record(resolution.manifest, "citation_manifest", blocking)
        self._verify_record(resolution.validation, "validation", blocking)

        draft = _json_object(resolution.draft.path) if resolution.draft else None
        manifest = _json_object(resolution.manifest.path) if resolution.manifest else None
        validation_payload = _json_object(resolution.validation.path) if resolution.validation else None
        if resolution.draft and draft is None:
            blocking.append("review_draft_json_unreadable")
        if resolution.manifest and manifest is None:
            blocking.append("citation_manifest_json_unreadable")
        if resolution.validation and validation_payload is None:
            blocking.append("validation_json_unreadable")

        if draft is not None:
            if draft.get("artifact_type") != "review_draft" or draft.get("artifact_version") != "v3":
                blocking.append("review_draft_schema_mismatch")
        if manifest is not None:
            if manifest.get("artifact_type") != "citation_manifest" or manifest.get("artifact_version") != "v3":
                blocking.append("citation_manifest_schema_mismatch")

        # A citation manifest is a derived view of the canonical draft.  The
        # registry dependency must name that exact draft and hash.
        if resolution.manifest and resolution.draft:
            matching_dependency = next(
                (
                    dependency
                    for dependency in resolution.manifest.depends_on
                    if dependency.artifact_id == resolution.draft.artifact_id
                ),
                None,
            )
            if matching_dependency is None:
                blocking.append("citation_manifest_draft_dependency_missing")
            elif matching_dependency.content_hash != resolution.draft.content_hash:
                blocking.append("citation_manifest_draft_dependency_hash_mismatch")

        blocks = _iter_blocks(draft or {})
        block_ids = {
            str(block.get("block_id") or "").strip()
            for block in blocks
            if str(block.get("block_id") or "").strip()
        }
        occurrences = manifest.get("occurrences") if isinstance(manifest, Mapping) else []
        occurrences = occurrences if isinstance(occurrences, list) else []
        occurrence_ids: set[str] = set()
        mapped_occurrences = 0
        unresolved_occurrences = 0
        for occurrence in occurrences:
            if not isinstance(occurrence, Mapping):
                blocking.append("citation_occurrence_not_object")
                continue
            occurrence_id = str(occurrence.get("occurrence_id") or "").strip()
            if occurrence_id:
                if occurrence_id in occurrence_ids:
                    blocking.append(f"duplicate_citation_occurrence:{occurrence_id}")
                occurrence_ids.add(occurrence_id)
            block_id = str(occurrence.get("block_id") or "").strip()
            paper_id = str(occurrence.get("paper_id") or occurrence.get("paper_key") or "").strip()
            if not block_id or block_id not in block_ids:
                blocking.append(f"citation_mapping_error:{occurrence_id or 'unknown'}")
            else:
                mapped_occurrences += 1
            if not paper_id:
                blocking.append(f"citation_source_identity_missing:{occurrence_id or 'unknown'}")
            if not occurrence.get("spans"):
                findings.append(f"citation_span_missing:{occurrence_id or 'unknown'}")

        unresolved_occurrences = sum(
            1
            for occurrence in occurrences
            if isinstance(occurrence, Mapping)
            and (
                not str(occurrence.get("ref_id") or "").strip()
                or not str(occurrence.get("paper_id") or occurrence.get("paper_key") or "").strip()
                or str(occurrence.get("paper_id") or "").strip() == "unknown"
            )
        )
        if unresolved_occurrences:
            findings.append(f"unresolved_citation_occurrences:{unresolved_occurrences}")

        render_policy = manifest.get("render_policy") if isinstance(manifest, Mapping) else None
        if not isinstance(render_policy, Mapping) or any(
            not str(render_policy.get(key) or "").strip() for key in _RENDER_POLICY_FIELDS
        ):
            blocking.append("citation_render_policy_snapshot_missing")

        bibliography = manifest.get("bibliography") if isinstance(manifest, Mapping) else []
        bibliography = bibliography if isinstance(bibliography, list) else []
        cited_papers = {
            str(item.get("paper_id") or item.get("paper_key") or "").strip()
            for item in occurrences
            if isinstance(item, Mapping)
        }
        unreferenced_bibliography = sum(
            1
            for entry in bibliography
            if isinstance(entry, Mapping)
            and bool(entry.get("is_cited", True))
            and str(entry.get("paper_id") or entry.get("paper_key") or "").strip() not in cited_papers
        )
        if unreferenced_bibliography:
            findings.append(f"unreferenced_bibliography_entries:{unreferenced_bibliography}")

        semantic_status = "unvalidated"
        semantic: dict[str, Any] = {
            "artifact_id": resolution.validation.artifact_id if resolution.validation else "",
            "contract_satisfied": False,
        }
        validation_result: ValidationRunResultV1 | None = None
        if validation_payload is not None:
            try:
                validation_result = ValidationRunResultV1.from_dict(validation_payload)
                validation_result.validate()
            except (ValidationRunResultError, TypeError, ValueError, KeyError) as exc:
                blocking.append(f"validation_contract_invalid:{exc}")
            else:
                semantic_status = validation_result.validation_disposition.value
                semantic.update(
                    {
                        "execution_status": validation_result.execution_status.value,
                        "validation_disposition": semantic_status,
                        "contract_satisfied": validation_result.contract_satisfied,
                        "diagnostics": list(validation_result.diagnostics),
                        "claim_verdict_counts": dict(validation_result.claim_verdict_counts),
                        "input_artifacts": validation_result.input_artifacts.to_dict(),
                    }
                )
                if resolution.draft and (
                    validation_result.input_artifacts.review_draft_id != resolution.draft.artifact_id
                    or validation_result.input_artifacts.review_draft_hash != resolution.draft.content_hash
                ):
                    blocking.append("validation_review_draft_input_stale")
                if resolution.manifest and (
                    validation_result.input_artifacts.citation_manifest_id != resolution.manifest.artifact_id
                    or validation_result.input_artifacts.citation_manifest_hash != resolution.manifest.content_hash
                ):
                    blocking.append("validation_citation_manifest_input_stale")
                if not validation_result.contract_satisfied:
                    blocking.append("validation_contract_not_satisfied")
                if semantic_status in {"needs_review", "unvalidated"}:
                    findings.append(f"validation_disposition:{semantic_status}")
                elif semantic_status == "findings":
                    findings.append("validation_disposition:findings")

        citation_status = "blocked" if any(
            item.startswith((
                "canonical_citation_manifest",
                "citation_manifest_",
                "citation_mapping_error",
                "citation_source_identity_missing",
                "citation_occurrence_",
                "citation_render_policy",
            ))
            for item in blocking
        ) else ("findings" if findings else "clean")
        if blocking:
            status = "blocked"
        elif semantic_status == "unvalidated":
            status = "unvalidated"
        elif semantic_status in {"needs_review", "unvalidated"}:
            status = "blocked"
        elif semantic_status == "findings" or findings:
            status = "findings"
        else:
            status = "clean"

        input_payload = {
            "review_draft": _record_payload(resolution.draft),
            "citation_manifest": _record_payload(resolution.manifest),
        }
        evidence_payload = {
            "job_id": self.workspace.job_id,
            "status": status,
            "inputs": input_payload,
            "validation": semantic,
            "citation_counts": {
                "occurrences": len(occurrences),
                "mapped_occurrences": mapped_occurrences,
                "unresolved_occurrences": unresolved_occurrences,
                "bibliography_entries": len(bibliography),
                "unreferenced_bibliography_entries": unreferenced_bibliography,
            },
            "blocking_issues": sorted(set(blocking)),
            "findings": sorted(set(findings)),
        }
        return ValidationClosureResult(
            job_id=self.workspace.job_id,
            status=status,
            citation_status=citation_status,
            semantic_status=semantic_status,
            repair_status=(
                str(validation_result.repair_status)
                if validation_result is not None
                else "not_requested"
            ),
            input_artifacts=input_payload,
            validation_artifact=_record_payload(resolution.validation),
            citation_counts=evidence_payload["citation_counts"],
            semantic=semantic,
            blocking_issues=tuple(sorted(set(blocking))),
            findings=tuple(sorted(set(findings))),
            checked_at=utc_now_iso(),
            evidence_hash=_stable_hash(evidence_payload),
        )


def persist_validation_closure(
    result: ValidationClosureResult,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
) -> ArtifactRecord:
    """Persist a closure report as a new derived, hash-verified artifact."""

    path = Path(workspace.report_path(f"validation_closure_{result.evidence_hash[:16]}.json"))
    atomic_write_json(str(path), result.to_dict())
    dependencies: list[dict[str, Any]] = []
    for item in result.input_artifacts.values():
        if isinstance(item, Mapping) and item.get("artifact_id"):
            dependencies.append(
                {
                    "artifact_id": item["artifact_id"],
                    "artifact_type": item.get("artifact_type", ""),
                    "path": item.get("path", ""),
                    "content_hash": item.get("content_hash", ""),
                }
            )
    if result.validation_artifact and result.validation_artifact.get("artifact_id"):
        dependencies.append(dict(result.validation_artifact))
    return registry.register_file(
        artifact_id=f"validation_closure:{result.evidence_hash[:24]}",
        artifact_role="validation_closure",
        artifact_type=VALIDATION_CLOSURE_ARTIFACT_TYPE,
        artifact_version=VALIDATION_CLOSURE_ARTIFACT_VERSION,
        path=path,
        producer="validation.closure.persist_validation_closure",
        depends_on=dependencies,
        metadata={
            "closure_status": result.status,
            "evidence_hash": result.evidence_hash,
        },
    )


__all__ = [
    "VALIDATION_CLOSURE_ARTIFACT_TYPE",
    "VALIDATION_CLOSURE_ARTIFACT_VERSION",
    "ValidationClosureResult",
    "ValidationClosureService",
    "persist_validation_closure",
]

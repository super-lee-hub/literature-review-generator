"""Declarative export bundles and read-only forensic attestation."""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import asdict, dataclass
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
import zipfile

from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRecord, ArtifactRegistry, RegistryError
from services.job_workspace import JobWorkspace, atomic_write_json, utc_now_iso
from services.queue_service import LocalPublicationContext
from runtime.provider_runtime import hash_json


EXPORT_BUNDLE_ARTIFACT_TYPE = "export_bundle"
EXPORT_BUNDLE_ARTIFACT_VERSION = "v1"
FORENSIC_ATTESTATION_ARTIFACT_TYPE = "forensic_attestation"
FORENSIC_ATTESTATION_ARTIFACT_VERSION = "v1"


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "artifact"


@dataclass(frozen=True)
class ExportBundleSpecV1:
    include_artifact_ids: tuple[str, ...] = ()
    include_artifact_types: tuple[str, ...] = ()
    include_artifact_roles: tuple[str, ...] = ()
    include_unready_descriptors: bool = False
    export_allowlisted_only: bool = True
    export_mode: str = "canonical"

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "ExportBundleSpecV1":
        value = payload or {}
        mode = str(value.get("export_mode") or "canonical").strip().lower()
        if mode not in {"canonical", "forensic"}:
            raise ValueError("export_mode must be 'canonical' or 'forensic'")
        return cls(
            include_artifact_ids=tuple(str(item) for item in (value.get("include_artifact_ids") or ()) if str(item)),
            include_artifact_types=tuple(str(item) for item in (value.get("include_artifact_types") or ()) if str(item)),
            include_artifact_roles=tuple(str(item) for item in (value.get("include_artifact_roles") or ()) if str(item)),
            include_unready_descriptors=bool(value.get("include_unready_descriptors", mode == "forensic")),
            export_allowlisted_only=bool(value.get("export_allowlisted_only", True)),
            export_mode=mode,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("include_artifact_ids", "include_artifact_types", "include_artifact_roles"):
            payload[key] = list(payload[key])
        return payload


@dataclass(frozen=True)
class ExportBundleResultV1:
    job_id: str
    status: str
    bundle_id: str
    bundle_path: str
    artifact_id: str
    manifest: Mapping[str, Any]
    issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["issues"] = list(self.issues)
        return payload


@dataclass(frozen=True)
class ForensicAttestationResultV1:
    job_id: str
    status: str
    report_path: str
    artifact_id: str
    dependency_graph: Mapping[str, Sequence[str]]
    verified_artifact_ids: tuple[str, ...]
    manual_modified_artifact_ids: tuple[str, ...]
    issues: tuple[str, ...]
    evidence_hash: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["verified_artifact_ids"] = list(self.verified_artifact_ids)
        payload["manual_modified_artifact_ids"] = list(self.manual_modified_artifact_ids)
        payload["issues"] = list(self.issues)
        return payload


def _matches(record: ArtifactRecord, spec: ExportBundleSpecV1) -> bool:
    if spec.include_artifact_ids and record.artifact_id not in spec.include_artifact_ids:
        return False
    if spec.include_artifact_types and record.artifact_type not in spec.include_artifact_types:
        return False
    if spec.include_artifact_roles and record.artifact_role not in spec.include_artifact_roles:
        return False
    return True


def _json_payload(record: ArtifactRecord) -> Mapping[str, Any] | None:
    try:
        value = json.loads(Path(record.path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if isinstance(value, Mapping):
        payload = value.get("payload")
        return payload if isinstance(payload, Mapping) else value
    return None


def _current_pointer_targets(registry: ArtifactRegistry) -> tuple[set[str], dict[str, str]]:
    """Read current repair pointers without selecting stale versions."""

    targets: set[str] = set()
    pointer_kinds: dict[str, str] = {}
    for pointer in registry.list_records():
        if pointer.status != "ready" or pointer.artifact_type != "current_artifact_pointer":
            continue
        payload = _json_payload(pointer)
        if payload is None:
            continue
        kind = str(payload.get("pointer_kind") or "").strip()
        target_id = str(payload.get("target_artifact_id") or "").strip()
        target_hash = str(payload.get("target_content_hash") or "").strip()
        target = registry.get(target_id) if target_id else None
        if (
            not kind
            or target is None
            or target.status != "ready"
            or not target_hash
            or target.content_hash != target_hash
        ):
            continue
        targets.add(target_id)
        pointer_kinds[kind] = target_id
    try:
        current_set = registry.resolve_current_artifact_set()
    except (OSError, RegistryError, ValueError, TypeError):
        current_set = None
    if current_set is not None:
        current_targets = {
            "review_draft": current_set.review_draft_artifact_id,
            "citation_manifest": current_set.citation_manifest_artifact_id,
            "review_docx": current_set.review_docx_artifact_id,
            (
                "validation_disposition"
                if current_set.validation_status == "not_requested"
                else "validation_run_result"
            ): (
                current_set.validation_disposition_artifact_id
                if current_set.validation_status == "not_requested"
                else current_set.validation_run_result_artifact_id
            ),
            "provider_receipt_closure": current_set.validation_receipt_closure_artifact_id,
        }
        for kind, target_id in current_targets.items():
            if target_id:
                targets.add(target_id)
                pointer_kinds[kind] = target_id
    return targets, pointer_kinds


def _is_current_canonical_record(
    record: ArtifactRecord,
    *,
    pointer_targets: set[str],
    pointer_kinds: Mapping[str, str],
) -> bool:
    if record.artifact_type == "current_artifact_pointer":
        return True
    if record.metadata.get("versioned") and record.artifact_id not in pointer_targets:
        return False
    legacy_by_kind = {
        "review_draft": {"review_draft"},
        "citation_manifest": {"citation_manifest_v3", "citation_manifest:v3"},
        "review_docx": {"review_docx"},
        "validation_run_result": set(),
    }
    for kind, target_id in pointer_kinds.items():
        if record.artifact_type in {
            kind,
            "validation_run_result_repaired" if kind == "validation_run_result" else "",
        }:
            if record.artifact_id == target_id:
                return True
            if record.artifact_id in legacy_by_kind.get(kind, set()):
                return False
    return True


def _derive_current_evidence(
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
) -> dict[str, Any]:
    """Derive export trust facts from the current runtime and Registry.

    Caller-provided completion/closure mappings are intentionally absent from
    this function.  Export and attestation use this single source of truth so
    a forged ``complete``/``clean`` mapping can never make a ZIP canonical.
    """

    issues: list[str] = []
    completion: dict[str, Any]
    try:
        from runtime.runner import AgentRuntimeRunner

        status = AgentRuntimeRunner.status(workspace.root_dir)
        completion = {
            "job_status": status.job_status,
            "completion_status": status.completion_status,
            "canonical_ready": bool(status.canonical_ready),
            "completion_reasons": list(status.completion_reasons),
            "completion_evidence_hash": status.completion_evidence_hash,
        }
        if status.completion_status != "complete" or not status.canonical_ready:
            issues.append("runtime_completion_not_canonical")
    except (OSError, RegistryError, ValueError, TypeError, RuntimeError) as exc:
        completion = {
            "job_status": "unknown",
            "completion_status": "blocked",
            "canonical_ready": False,
            "completion_reasons": [str(exc)],
        }
        issues.append(f"runtime_completion_unavailable:{exc}")

    try:
        from validation.closure import ValidationClosureService

        validation = ValidationClosureService(workspace, registry).inspect()
        closure = validation.to_dict()
        if validation.status not in {"clean", "not_requested"}:
            issues.append(f"validation_closure_not_clean:{validation.status}")
        elif validation.status == "not_requested" and not bool(
            (validation.semantic or {}).get("allow_unvalidated", False)
        ):
            issues.append("validation_disposition_policy_disallows_unvalidated")
    except (OSError, RegistryError, ValueError, TypeError, RuntimeError) as exc:
        closure = {"status": "blocked", "blocking_issues": [str(exc)]}
        issues.append(f"validation_closure_unavailable:{exc}")

    current_set_payload: Mapping[str, Any] = {}
    try:
        current_set = registry.resolve_current_artifact_set()
        if current_set is not None:
            current_set_payload = current_set.to_dict()
            if str(closure.get("status") or "") == "not_requested":
                semantic = closure.get("semantic")
                disposition_id = str(semantic.get("artifact_id") or "") if isinstance(semantic, Mapping) else ""
                disposition = registry.get(disposition_id) if disposition_id else None
                disposition_record_id = disposition.artifact_id if disposition is not None else ""
                disposition_record_hash = disposition.content_hash if disposition is not None else ""
                disposition_valid = False
                if disposition is not None and disposition.status == "ready":
                    try:
                        from validation.disposition import ValidationDispositionV1

                        disposition_payload = json.loads(Path(disposition.path).read_text(encoding="utf-8"))
                        typed_disposition = ValidationDispositionV1.from_dict(disposition_payload)
                        runtime_spec = registry.get("runtime_job_spec")
                        raw_spec = (
                            json.loads(Path(runtime_spec.path).read_text(encoding="utf-8"))
                            if runtime_spec is not None
                            else None
                        )
                        stage_plan = raw_spec.get("metadata", {}).get("stage_plan") if isinstance(raw_spec, Mapping) else None
                        disposition_valid = bool(
                            typed_disposition.job_id == registry.job_id
                            and typed_disposition.validation_enabled is False
                            and typed_disposition.validation_required is False
                            and typed_disposition.allow_unvalidated is True
                            and typed_disposition.status == "not_requested"
                            and runtime_spec is not None
                            and typed_disposition.spec_hash == runtime_spec.content_hash
                            and isinstance(stage_plan, Mapping)
                            and typed_disposition.stage_plan_hash == hash_json(stage_plan)
                            and typed_disposition.disposition_hash == typed_disposition.computed_hash()
                        )
                    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, KeyError):
                        disposition_valid = False
                if (
                    not disposition_valid
                    or current_set.validation_status != "not_requested"
                    or current_set.validation_disposition_artifact_id != disposition_record_id
                    or current_set.validation_disposition_artifact_hash != disposition_record_hash
                ):
                    issues.append("validation_disposition_current_set_binding_mismatch")
    except (OSError, RegistryError, ValueError, TypeError, RuntimeError) as exc:
        issues.append(f"current_artifact_set_unavailable:{exc}")

    closure_records: list[ArtifactRecord] = []
    closure_payloads: list[Mapping[str, Any]] = []
    stage_map_payload: Mapping[str, Any] = {
        "artifact_type": "current_stage_closure_map",
        "artifact_version": "v1",
        "job_id": registry.job_id,
        "current_set_id": "",
        "stages": {},
        "requested_stages": [],
        "spec_hash": "",
        "provider_closures_by_stage": {},
        "blocking_issues": ["current_stage_closure_map_unavailable"],
    }
    try:
        from validation.closure import resolve_current_stage_closure_map

        stage_map = resolve_current_stage_closure_map(registry)
        stage_map_payload = stage_map.to_dict()
        current_receipt_id = str(
            (stage_map.stages.get("validation_receipt_closure") or {}).get("artifact_id") or ""
        )
        current_receipt = registry.get(current_receipt_id) if current_receipt_id else None
        if current_receipt is not None and current_receipt.status == "ready":
            closure_records = [current_receipt]
            payload = _json_payload(current_receipt)
            if payload is not None:
                closure_payloads = [payload]
        if stage_map.blocking_issues:
            issues.extend(f"current_stage_closure_map:{item}" for item in stage_map.blocking_issues)
        if str(closure.get("status") or "") == "not_requested" and "validate" in {
            str(item).strip()
            for item in (stage_map_payload.get("requested_stages") or ())
            if str(item).strip()
        }:
            issues.append("validation_disposition_stage_plan_requests_validation")
    except (OSError, RegistryError, ValueError, TypeError) as exc:
        issues.append(f"current_stage_closure_map_unavailable:{exc}")
    receipt_complete = bool(closure_payloads) and bool(closure_payloads[0].get("complete"))
    receipt_closure = {
        "status": "clean" if receipt_complete else "blocked",
        "complete": receipt_complete,
        "artifact_ids": [record.artifact_id for record in closure_records],
        "closures": [dict(payload) for payload in closure_payloads],
    }
    if not receipt_complete:
        issues.append("provider_receipt_closure_incomplete")

    ready_final_outline = any(
        record.status == "ready" and record.artifact_type == "final_outline"
        for record in registry.list_records()
    )
    from outline.adoption_transaction import current_adoption_record

    current_adoption = current_adoption_record(registry)
    adoption_payload = _json_payload(current_adoption) if current_adoption is not None else None
    adoption = {
        "status": "adopted" if adoption_payload is not None else "not_adopted",
        "artifact_id": current_adoption.artifact_id if current_adoption is not None else "",
        "actor": str((adoption_payload or {}).get("actor") or ""),
        "reason": str((adoption_payload or {}).get("reason") or ""),
        "expected_hash": str((adoption_payload or {}).get("expected_hash") or ""),
        "adoption_identity": str((adoption_payload or {}).get("adoption_identity") or ""),
        "current_pointer_artifact_id": str(
            (adoption_payload or {}).get("current_pointer_artifact_id") or ""
        ),
    }
    if ready_final_outline and adoption_payload is None:
        issues.append("outline_adoption_missing")
    if adoption_payload is not None and not all(
        str(adoption_payload.get(field) or "").strip()
        for field in ("actor", "reason", "expected_hash")
    ):
        issues.append("outline_adoption_audit_incomplete")

    dependency_issues: list[str] = []
    for record in registry.list_records():
        if record.status != "ready":
            continue
        try:
            ArtifactRegistry._verify_ready_artifact(record)
            registry.verify_ready_dependencies(record.depends_on, owner_record=record)
        except (OSError, RegistryError, ValueError, TypeError) as exc:
            dependency_issues.append(f"{record.artifact_id}:{exc}")
    if dependency_issues:
        issues.extend(f"dependency_closure:{item}" for item in dependency_issues)

    return {
        "completion": completion,
        "closure": closure,
        "receipt_closure": receipt_closure,
        "current_stage_closure_map": stage_map_payload,
        "requested_stages": list(stage_map_payload.get("requested_stages") or ()),
        "spec_hash": str(stage_map_payload.get("spec_hash") or ""),
        "current_artifact_set": dict(current_set_payload),
        "adoption": adoption,
        "issues": sorted(set(issues)),
    }


_EXPORT_ALLOWLIST = frozenset(
    {
        "job_outcome",
        "runtime_job_spec",
        "source_bundle",
        "source_inventory",
        "summary",
        "summary_file",
        "summary_source_manifest",
        "outline_v3_node_output",
        "provider_receipt_closure",
        "adopted_outline",
        "outline_adoption_pointer",
        "outline_v3_adoption_current",
        "current_artifact_pointer",
        "review_draft",
        "citation_manifest",
        "validation_run_result",
        "validation_disposition",
        "stage1_summary_reuse_record",
        "validation_projection",
        "review_docx",
        "repair_plan",
        "repair_report",
        "repair_apply_result",
    }
)


class ExportBundleService:
    """Export verified Registry artifacts without inferring state from DOCX."""

    def __init__(self, workspace: JobWorkspace, registry: ArtifactRegistry) -> None:
        self.workspace = workspace
        self.registry = registry

    def export(
        self,
        *,
        spec: ExportBundleSpecV1 | Mapping[str, Any] | None = None,
        completion: Mapping[str, Any] | None = None,
        closure: Mapping[str, Any] | None = None,
    ) -> ExportBundleResultV1:
        export_spec = spec if isinstance(spec, ExportBundleSpecV1) else ExportBundleSpecV1.from_mapping(spec)
        derived = _derive_current_evidence(self.workspace, self.registry)
        # These parameters remain accepted only so older callers fail closed
        # at the same API boundary.  They are never used as trust evidence.
        caller_claims_supplied = completion is not None or closure is not None
        pointer_targets, pointer_kinds = _current_pointer_targets(self.registry)
        records = [
            record
            for record in self.registry.list_records()
            if _matches(record, export_spec)
            and _is_current_canonical_record(
                record,
                pointer_targets=pointer_targets,
                pointer_kinds=pointer_kinds,
            )
            and (
                export_spec.export_mode == "forensic"
                or record.status == "ready"
            )
            and (
                not export_spec.export_allowlisted_only
                or record.artifact_role in _EXPORT_ALLOWLIST
                or record.artifact_type in _EXPORT_ALLOWLIST
            )
        ]
        records.sort(key=lambda item: (item.artifact_type, item.artifact_id))
        issues: list[str] = list(derived["issues"])
        if caller_claims_supplied:
            issues.append("caller_completion_or_closure_ignored")
        file_entries: list[dict[str, Any]] = []
        ready_records: list[ArtifactRecord] = []
        ready_payloads: dict[str, bytes] = {}
        for record in records:
            entry = {
                "artifact_id": record.artifact_id,
                "artifact_role": record.artifact_role,
                "artifact_type": record.artifact_type,
                "artifact_version": record.artifact_version,
                "status": record.status,
                "path": record.path,
                "content_hash": record.content_hash,
                "depends_on": [dependency.to_dict() for dependency in record.depends_on],
                "metadata": dict(record.metadata),
            }
            if record.status == "ready":
                try:
                    ArtifactRegistry._verify_ready_artifact(record)
                except (OSError, RegistryError, ValueError, TypeError) as exc:
                    entry["integrity"] = "untrusted"
                    entry["error"] = str(exc)
                    issues.append(f"{record.artifact_id}: {exc}")
                else:
                    entry["integrity"] = "verified"
                    try:
                        ready_payloads[record.artifact_id] = Path(record.path).read_bytes()
                    except OSError as exc:
                        entry["integrity"] = "untrusted"
                        entry["error"] = str(exc)
                        issues.append(f"{record.artifact_id}: export read failed: {exc}")
                    else:
                        ready_records.append(record)
            elif export_spec.export_mode != "forensic" or not export_spec.include_unready_descriptors:
                continue
            else:
                entry["integrity"] = "descriptor_only"
            file_entries.append(entry)

        completion_manifest = dict(derived["completion"])
        closure_manifest = dict(derived["closure"])
        receipt_closure_manifest = dict(derived["receipt_closure"])
        adoption_manifest = dict(derived["adoption"])
        completion_status = str(completion_manifest.get("completion_status") or "unknown")
        closure_status = str(closure_manifest.get("status") or "unknown")
        validation_semantic = closure_manifest.get("semantic")
        unvalidated_allowed = bool(
            closure_status == "not_requested"
            and isinstance(validation_semantic, Mapping)
            and validation_semantic.get("allow_unvalidated", False)
            and validation_semantic.get("validation_disposition") == "not_requested"
            and not bool(closure_manifest.get("validation_required", False))
            and not bool(validation_semantic.get("validation_enabled", False))
            and not bool(validation_semantic.get("validation_required", False))
        )
        closure_trust_ready = closure_status == "clean" or (
            closure_status == "not_requested" and unvalidated_allowed
        )
        manual_modified = [
            entry["artifact_id"]
            for entry in file_entries
            if bool((entry.get("metadata") or {}).get("manual_modified"))
        ]
        trust_ready = not issues and (
            completion_status == "complete"
            and bool(completion_manifest.get("canonical_ready"))
            and closure_trust_ready
            and receipt_closure_manifest.get("status") == "clean"
            and (
                not any(record.artifact_type == "final_outline" for record in records)
                or adoption_manifest.get("status") == "adopted"
            )
            and not manual_modified
        )
        bundle_status = (
            "canonical_unvalidated"
            if trust_ready and unvalidated_allowed
            else "canonical_verified"
            if trust_ready
            else (
            "manual_repaired" if not issues and manual_modified else "untrusted"
            )
        )
        bundle_id = "export:" + _hash(
            {
                "job_id": self.workspace.job_id,
                "spec": export_spec.to_dict(),
                "records": file_entries,
                "completion": completion_manifest,
                "closure": closure_manifest,
                "receipt_closure": receipt_closure_manifest,
                "current_stage_closure_map": dict(derived["current_stage_closure_map"]),
                "requested_stages": list(derived["requested_stages"]),
                "spec_hash": str(derived["spec_hash"]),
                "adoption": adoption_manifest,
            }
        )[:24]
        # ``bundle_id`` is a logical identifier and uses ``:`` as its
        # namespace separator.  Keep the identifier stable, but use a
        # Windows-safe filename for the materialized ZIP.
        bundle_filename = f"{_safe_name(bundle_id)}.zip"
        bundle_path = Path(self.workspace.report_path(f"export_bundles/{bundle_filename}"))
        manifest = {
            "artifact_type": EXPORT_BUNDLE_ARTIFACT_TYPE,
            "artifact_version": EXPORT_BUNDLE_ARTIFACT_VERSION,
            "bundle_id": bundle_id,
            "job_id": self.workspace.job_id,
            "created_at": utc_now_iso(),
            "status": bundle_status if export_spec.export_mode == "canonical" else "forensic_untrusted",
            "export_mode": export_spec.export_mode,
            "spec": export_spec.to_dict(),
            "records": file_entries,
            "completion_manifest": completion_manifest,
            "validation_closure": closure_manifest,
            "validation_status": closure_status,
            "validation_not_requested": bool(closure_status == "not_requested"),
            "unvalidated_policy": {
                "allowed": unvalidated_allowed,
                "explicit_disposition": bool(closure_status == "not_requested"),
            },
            "provider_receipt_closure": receipt_closure_manifest,
            "current_stage_closure_map": dict(derived["current_stage_closure_map"]),
            "requested_stages": list(derived["requested_stages"]),
            "spec_hash": str(derived["spec_hash"]),
            "adoption": adoption_manifest,
            "manual_repaired": bool(manual_modified),
            "issues": sorted(set(issues)),
        }
        disposition_record = closure_manifest.get("validation_artifact")
        disposition_record = disposition_record if isinstance(disposition_record, Mapping) else {}
        semantic = validation_semantic if isinstance(validation_semantic, Mapping) else {}
        disposition_id = str(disposition_record.get("artifact_id") or semantic.get("artifact_id") or "")
        disposition_hash = str(
            disposition_record.get("content_hash") or semantic.get("disposition_hash") or ""
        )
        validation_required = bool(closure_manifest.get("validation_required", False))
        validation_enabled = bool(semantic.get("validation_enabled", False))
        unvalidated_warning = (
            "WARNING: semantic validation was not performed; this bundle is canonical_unvalidated."
            if bundle_status == "canonical_unvalidated"
            else ""
        )
        manifest.update(
            {
                "validation_required": validation_required,
                "validation_enabled": validation_enabled,
                "allow_unvalidated": unvalidated_allowed,
                "validation_disposition_artifact_id": disposition_id,
                "validation_disposition_artifact_hash": disposition_hash,
                "stage_plan_hash": str(semantic.get("stage_plan_hash") or ""),
                "runtime_spec_hash": str(derived["spec_hash"]),
                "validation_warning": unvalidated_warning,
            }
        )
        if export_spec.export_mode == "canonical" and bundle_status not in {
            "canonical_verified",
            "canonical_unvalidated",
        }:
            return ExportBundleResultV1(
                job_id=self.workspace.job_id,
                status="untrusted",
                bundle_id=bundle_id,
                bundle_path="",
                artifact_id="",
                manifest=manifest,
                issues=tuple(sorted(set(issues))),
            )
        checksums: dict[str, str] = {}
        bundle_buffer = io.BytesIO()
        with zipfile.ZipFile(bundle_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for record in ready_records:
                data = ready_payloads[record.artifact_id]
                arcname = f"artifacts/{_safe_name(record.artifact_id)}"
                archive.writestr(arcname, data)
                checksums[arcname] = hashlib.sha256(data).hexdigest()
            manifest["issues"] = sorted(set(issues))
            archive.writestr("provenance_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr("checksums.json", json.dumps(checksums, ensure_ascii=False, indent=2, sort_keys=True))
            archive.writestr(
                "completion_manifest.json",
                json.dumps(completion_manifest, ensure_ascii=False, indent=2),
            )
            archive.writestr(
                "validation_closure.json",
                json.dumps(closure_manifest, ensure_ascii=False, indent=2),
            )
            archive.writestr(
                "provider_receipt_closure.json",
                json.dumps(receipt_closure_manifest, ensure_ascii=False, indent=2),
            )
            archive.writestr(
                "EXPORT_STATUS.txt",
                "status=" + str(manifest["status"]) + "\n"
                + "validation_status=" + closure_status + "\n"
                + "validation_required=" + str(bool(closure_manifest.get("validation_required", False))).lower() + "\n"
                + "validation_enabled=" + str(validation_enabled).lower() + "\n"
                + "allow_unvalidated=" + str(unvalidated_allowed).lower() + "\n"
                + "validation_disposition_artifact_id=" + disposition_id + "\n"
                + "validation_disposition_artifact_hash=" + disposition_hash + "\n"
                + "stage_plan_hash=" + str(manifest.get("stage_plan_hash") or "") + "\n"
                + "runtime_spec_hash=" + str(manifest.get("runtime_spec_hash") or "") + "\n"
                + (unvalidated_warning + "\n" if unvalidated_warning else ""),
            )
        bundle_payload = bundle_buffer.getvalue()
        if not zipfile.is_zipfile(io.BytesIO(bundle_payload)):
            raise RegistryError("in-memory export bundle failed ZIP verification")
        artifact_id = f"export_bundle:{bundle_id}"
        registration_error = ""
        try:
            publication_context = getattr(self.registry, "publication_context", None) or LocalPublicationContext()
            publication = publication_context.publish_bytes(
                bundle_path,
                bundle_payload,
                registry=self.registry,
                register_kwargs={
                    "artifact_id": artifact_id,
                    "artifact_role": "export_bundle",
                    "artifact_type": EXPORT_BUNDLE_ARTIFACT_TYPE,
                    "artifact_version": EXPORT_BUNDLE_ARTIFACT_VERSION,
                    "producer": "runtime.export_bundle.ExportBundleService",
                    "depends_on": [ArtifactDependencyRefV2.from_record(record) for record in ready_records],
                    "metadata": {
                        # GUI and API consumers historically used ``status``;
                        # retain the explicit bundle name for newer callers.
                        "status": bundle_status,
                        "bundle_status": bundle_status,
                        "validation_status": closure_status,
                        "issue_count": len(issues),
                    },
                },
            )
            registered = publication.artifact
            if registered is None:
                raise RegistryError("export bundle publication returned no Registry record")
            artifact_id = registered.artifact_id
            final_bundle_path = Path(publication.final_path)
        except (OSError, RegistryError, ValueError, TypeError) as exc:
            registration_error = str(exc)
            issues.append(f"bundle_registration_failed:{exc}")
            bundle_status = "untrusted"
            manifest["status"] = "untrusted"
            manifest["issues"] = sorted(set(issues))
            final_bundle_path = Path("")
        return ExportBundleResultV1(
            job_id=self.workspace.job_id,
            status=bundle_status,
            bundle_id=bundle_id,
            bundle_path="" if registration_error else str(final_bundle_path),
            artifact_id="" if registration_error else artifact_id,
            manifest=manifest,
            issues=tuple(sorted(set([*issues, registration_error] if registration_error else issues))),
        )


class ForensicAttestationService:
    def __init__(self, workspace: JobWorkspace, registry: ArtifactRegistry) -> None:
        self.workspace = workspace
        self.registry = registry

    def attest(
        self,
        *,
        completion: Mapping[str, Any] | None = None,
        closure: Mapping[str, Any] | None = None,
        persist: bool = True,
    ) -> ForensicAttestationResultV1:
        derived = _derive_current_evidence(self.workspace, self.registry)
        # Caller claims are deliberately ignored.  Keep an audit marker so a
        # forensic report explains why the supplied values did not affect the
        # attestation.
        issues: list[str] = []
        issues.extend(str(item) for item in derived["issues"])
        if completion is not None or closure is not None:
            issues.append("caller_completion_or_closure_ignored")
        verified: list[str] = []
        manual: list[str] = []
        graph: dict[str, Sequence[str]] = {}
        records = self.registry.list_records()
        for record in records:
            graph[record.artifact_id] = tuple(
                dependency.artifact_id for dependency in record.depends_on if dependency.artifact_id
            )
            if bool(record.metadata.get("manual_modified")):
                manual.append(record.artifact_id)
            if record.status != "ready":
                continue
            try:
                ArtifactRegistry._verify_ready_artifact(record)
            except (OSError, RegistryError, ValueError, TypeError) as exc:
                issues.append(f"{record.artifact_id}: {exc}")
            else:
                verified.append(record.artifact_id)
        completion_manifest = dict(derived["completion"])
        closure_manifest = dict(derived["closure"])
        completion_status = str(completion_manifest.get("completion_status") or "unknown")
        closure_status = str(closure_manifest.get("status") or "unknown")
        unvalidated_allowed = bool(
            closure_status == "not_requested"
            and isinstance(closure_manifest.get("semantic"), Mapping)
            and closure_manifest["semantic"].get("allow_unvalidated", False)
        )
        if issues:
            status = "untrusted"
        elif manual:
            status = "manual_repaired"
        elif completion_status == "complete" and (
            closure_status == "clean" or (closure_status == "not_requested" and unvalidated_allowed)
        ):
            status = "canonical_unvalidated" if unvalidated_allowed else "canonical_verified"
        else:
            status = "untrusted"
            if completion_status != "complete":
                issues.append("completion_not_verified")
            if closure_status != "clean":
                issues.append("validation_closure_not_clean")
        evidence = {
            "job_id": self.workspace.job_id,
            "status": status,
            "graph": graph,
            "verified": sorted(verified),
            "manual": sorted(manual),
            "issues": sorted(set(issues)),
            "completion": completion_manifest,
            "closure": closure_manifest,
            "receipt_closure": dict(derived["receipt_closure"]),
            "adoption": dict(derived["adoption"]),
        }
        evidence_hash = _hash(evidence)
        report_path = Path(self.workspace.report_path(f"forensic_attestation_{evidence_hash[:16]}.json"))
        artifact_id = f"forensic_attestation:{evidence_hash[:24]}"
        report = {
            "artifact_type": FORENSIC_ATTESTATION_ARTIFACT_TYPE,
            "artifact_version": FORENSIC_ATTESTATION_ARTIFACT_VERSION,
            "checked_at": utc_now_iso(),
            "evidence_hash": evidence_hash,
            **evidence,
        }
        if persist:
            try:
                publication_context = getattr(self.registry, "publication_context", None) or LocalPublicationContext()
                publication = publication_context.publish_json(
                    report_path,
                    report,
                    registry=self.registry,
                    register_kwargs={
                        "artifact_id": artifact_id,
                        "artifact_role": "forensic_attestation",
                        "artifact_type": FORENSIC_ATTESTATION_ARTIFACT_TYPE,
                        "artifact_version": FORENSIC_ATTESTATION_ARTIFACT_VERSION,
                        "producer": "runtime.export_bundle.ForensicAttestationService",
                        "depends_on": [
                            ArtifactDependencyRefV2.from_record(record)
                            for record in records
                            if record.status == "ready" and record.artifact_id in verified
                        ],
                        "metadata": {"attestation_status": status, "evidence_hash": evidence_hash},
                    },
                )
                registered = publication.artifact
                if registered is None:
                    raise RegistryError("forensic attestation publication returned no Registry record")
                artifact_id = registered.artifact_id
                report_path = Path(publication.final_path)
            except (OSError, RegistryError, ValueError, TypeError) as exc:
                issues.append(f"attestation_registration_failed:{exc}")
                status = "untrusted"
                artifact_id = ""
                report_path = None
        return ForensicAttestationResultV1(
            job_id=self.workspace.job_id,
            status=status,
            report_path=str(report_path) if report_path is not None else "",
            artifact_id=artifact_id,
            dependency_graph=graph,
            verified_artifact_ids=tuple(sorted(verified)),
            manual_modified_artifact_ids=tuple(sorted(manual)),
            issues=tuple(sorted(set(issues))),
            evidence_hash=evidence_hash,
        )


__all__ = [
    "EXPORT_BUNDLE_ARTIFACT_TYPE",
    "EXPORT_BUNDLE_ARTIFACT_VERSION",
    "FORENSIC_ATTESTATION_ARTIFACT_TYPE",
    "FORENSIC_ATTESTATION_ARTIFACT_VERSION",
    "ExportBundleResultV1",
    "ExportBundleService",
    "ExportBundleSpecV1",
    "ForensicAttestationResultV1",
    "ForensicAttestationService",
]

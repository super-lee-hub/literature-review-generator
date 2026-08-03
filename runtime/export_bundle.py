"""Declarative export bundles and read-only forensic attestation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence
import zipfile

from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRecord, ArtifactRegistry, RegistryError
from services.job_workspace import JobWorkspace, atomic_write_json, utc_now_iso


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
        if validation.status != "clean":
            issues.append(f"validation_closure_not_clean:{validation.status}")
    except (OSError, RegistryError, ValueError, TypeError, RuntimeError) as exc:
        closure = {"status": "blocked", "blocking_issues": [str(exc)]}
        issues.append(f"validation_closure_unavailable:{exc}")

    closure_records = [
        record
        for record in registry.list_records()
        if record.status == "ready" and record.artifact_type == "provider_receipt_closure"
    ]
    closure_payloads: list[Mapping[str, Any]] = []
    for record in closure_records:
        payload = _json_payload(record)
        if payload is not None:
            closure_payloads.append(payload)
    receipt_complete = bool(closure_payloads) and all(
        bool(payload.get("complete")) for payload in closure_payloads
    )
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
    adoption_records = [
        record
        for record in registry.list_records()
        if record.status == "ready" and record.artifact_type == "adopted_outline"
    ]
    adoption_payload = _json_payload(adoption_records[-1]) if adoption_records else None
    adoption = {
        "status": "adopted" if adoption_payload is not None else "not_adopted",
        "artifact_id": adoption_records[-1].artifact_id if adoption_records else "",
        "actor": str((adoption_payload or {}).get("actor") or ""),
        "reason": str((adoption_payload or {}).get("reason") or ""),
        "expected_hash": str((adoption_payload or {}).get("expected_hash") or ""),
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
            registry.verify_ready_dependencies(record.depends_on)
        except (OSError, RegistryError, ValueError, TypeError) as exc:
            dependency_issues.append(f"{record.artifact_id}:{exc}")
    if dependency_issues:
        issues.extend(f"dependency_closure:{item}" for item in dependency_issues)

    return {
        "completion": completion,
        "closure": closure,
        "receipt_closure": receipt_closure,
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
        "review_draft",
        "citation_manifest",
        "validation_run_result",
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
        records = [
            record
            for record in self.registry.list_records()
            if _matches(record, export_spec)
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
        manual_modified = [
            entry["artifact_id"]
            for entry in file_entries
            if bool((entry.get("metadata") or {}).get("manual_modified"))
        ]
        trust_ready = not issues and (
            completion_status == "complete"
            and bool(completion_manifest.get("canonical_ready"))
            and closure_status == "clean"
            and receipt_closure_manifest.get("status") == "clean"
            and (
                not any(record.artifact_type == "final_outline" for record in records)
                or adoption_manifest.get("status") == "adopted"
            )
            and not manual_modified
        )
        bundle_status = "canonical_verified" if trust_ready else (
            "manual_repaired" if not issues and manual_modified else "untrusted"
        )
        bundle_id = "export:" + _hash(
            {
                "job_id": self.workspace.job_id,
                "spec": export_spec.to_dict(),
                "records": file_entries,
                "completion": completion_manifest,
                "closure": closure_manifest,
                "receipt_closure": receipt_closure_manifest,
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
            "provider_receipt_closure": receipt_closure_manifest,
            "adoption": adoption_manifest,
            "manual_repaired": bool(manual_modified),
            "issues": sorted(set(issues)),
        }
        if export_spec.export_mode == "canonical" and bundle_status != "canonical_verified":
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
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        temp_bundle_path = bundle_path.with_name(bundle_path.name + ".tmp")
        try:
            with zipfile.ZipFile(temp_bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
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
                archive.writestr("EXPORT_STATUS.txt", manifest["status"] + "\n")
        except Exception:
            try:
                temp_bundle_path.unlink()
            except OSError:
                pass
            raise
        if not zipfile.is_zipfile(temp_bundle_path):
            try:
                temp_bundle_path.unlink()
            except OSError:
                pass
            raise RegistryError("temporary export bundle failed ZIP verification")
        temp_bundle_path.replace(bundle_path)

        artifact_id = f"export_bundle:{bundle_id}"
        registration_error = ""
        try:
            registered = self.registry.register_file(
                artifact_id=artifact_id,
                artifact_role="export_bundle",
                artifact_type=EXPORT_BUNDLE_ARTIFACT_TYPE,
                artifact_version=EXPORT_BUNDLE_ARTIFACT_VERSION,
                path=bundle_path,
                producer="runtime.export_bundle.ExportBundleService",
                depends_on=[ArtifactDependencyRefV2.from_record(record) for record in ready_records],
                metadata={"bundle_status": bundle_status, "issue_count": len(issues)},
            )
            artifact_id = registered.artifact_id
        except (OSError, RegistryError, ValueError, TypeError) as exc:
            registration_error = str(exc)
            issues.append(f"bundle_registration_failed:{exc}")
            bundle_status = "untrusted"
            try:
                bundle_path.unlink()
            except OSError:
                pass
        return ExportBundleResultV1(
            job_id=self.workspace.job_id,
            status=bundle_status,
            bundle_id=bundle_id,
            bundle_path=str(bundle_path),
            artifact_id=artifact_id,
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
        if issues:
            status = "untrusted"
        elif manual:
            status = "manual_repaired"
        elif completion_status == "complete" and closure_status == "clean":
            status = "canonical_verified"
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
            atomic_write_json(str(report_path), report)
            try:
                registered = self.registry.register_file(
                    artifact_id=artifact_id,
                    artifact_role="forensic_attestation",
                    artifact_type=FORENSIC_ATTESTATION_ARTIFACT_TYPE,
                    artifact_version=FORENSIC_ATTESTATION_ARTIFACT_VERSION,
                    path=report_path,
                    producer="runtime.export_bundle.ForensicAttestationService",
                    depends_on=[
                        ArtifactDependencyRefV2.from_record(record)
                        for record in records
                        if record.status == "ready" and record.artifact_id in verified
                    ],
                    metadata={"attestation_status": status, "evidence_hash": evidence_hash},
                )
                artifact_id = registered.artifact_id
            except (OSError, RegistryError, ValueError, TypeError) as exc:
                issues.append(f"attestation_registration_failed:{exc}")
                status = "untrusted"
        return ForensicAttestationResultV1(
            job_id=self.workspace.job_id,
            status=status,
            report_path=str(report_path),
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

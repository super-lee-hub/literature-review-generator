"""Declarative export bundles and read-only forensic attestation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence
import zipfile

from services.artifact_registry import ArtifactRecord, ArtifactRegistry, RegistryError
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
    include_unready_descriptors: bool = True
    label_manual_repaired_legacy: bool = True

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "ExportBundleSpecV1":
        value = payload or {}
        return cls(
            include_artifact_ids=tuple(str(item) for item in (value.get("include_artifact_ids") or ()) if str(item)),
            include_artifact_types=tuple(str(item) for item in (value.get("include_artifact_types") or ()) if str(item)),
            include_artifact_roles=tuple(str(item) for item in (value.get("include_artifact_roles") or ()) if str(item)),
            include_unready_descriptors=bool(value.get("include_unready_descriptors", True)),
            label_manual_repaired_legacy=bool(value.get("label_manual_repaired_legacy", True)),
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
        records = [record for record in self.registry.list_records() if _matches(record, export_spec)]
        records.sort(key=lambda item: (item.artifact_type, item.artifact_id))
        issues: list[str] = []
        file_entries: list[dict[str, Any]] = []
        ready_records: list[ArtifactRecord] = []
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
                    ready_records.append(record)
            elif not export_spec.include_unready_descriptors:
                continue
            else:
                entry["integrity"] = "descriptor_only"
            file_entries.append(entry)

        completion_status = str((completion or {}).get("completion_status") or (completion or {}).get("status") or "unknown")
        closure_status = str((closure or {}).get("status") or "unknown")
        manual_modified = [
            entry["artifact_id"]
            for entry in file_entries
            if bool((entry.get("metadata") or {}).get("manual_modified"))
        ]
        bundle_status = "canonical_verified" if (
            not issues
            and completion_status == "complete"
            and closure_status == "clean"
            and not manual_modified
        ) else ("manual_repaired_legacy" if not issues and manual_modified else "untrusted")
        bundle_id = "export:" + _hash(
            {
                "job_id": self.workspace.job_id,
                "spec": export_spec.to_dict(),
                "records": file_entries,
                "completion": completion or {},
                "closure": closure or {},
            }
        )[:24]
        bundle_path = Path(self.workspace.report_path(f"export_bundles/{bundle_id}.zip"))
        manifest = {
            "artifact_type": EXPORT_BUNDLE_ARTIFACT_TYPE,
            "artifact_version": EXPORT_BUNDLE_ARTIFACT_VERSION,
            "bundle_id": bundle_id,
            "job_id": self.workspace.job_id,
            "created_at": utc_now_iso(),
            "status": bundle_status,
            "spec": export_spec.to_dict(),
            "records": file_entries,
            "completion_manifest": dict(completion or {}),
            "validation_closure": dict(closure or {}),
            "manual_repaired_legacy": bool(manual_modified),
            "issues": sorted(set(issues)),
        }
        checksums: dict[str, str] = {}
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for record in ready_records:
                try:
                    data = Path(record.path).read_bytes()
                except OSError as exc:
                    issues.append(f"{record.artifact_id}: export read failed: {exc}")
                    continue
                arcname = f"artifacts/{_safe_name(record.artifact_id)}"
                archive.writestr(arcname, data)
                checksums[arcname] = hashlib.sha256(data).hexdigest()
            archive.writestr("provenance_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr("checksums.json", json.dumps(checksums, ensure_ascii=False, indent=2, sort_keys=True))
            archive.writestr(
                "completion_manifest.json",
                json.dumps(dict(completion or {}), ensure_ascii=False, indent=2),
            )
            archive.writestr(
                "validation_closure.json",
                json.dumps(dict(closure or {}), ensure_ascii=False, indent=2),
            )
            archive.writestr("EXPORT_STATUS.txt", bundle_status + "\n")

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
                depends_on=[
                    {
                        "artifact_id": record.artifact_id,
                        "artifact_type": record.artifact_type,
                        "path": record.path,
                        "content_hash": record.content_hash,
                    }
                    for record in ready_records
                ],
                metadata={"bundle_status": bundle_status, "issue_count": len(issues)},
            )
            artifact_id = registered.artifact_id
        except (OSError, RegistryError, ValueError, TypeError) as exc:
            registration_error = str(exc)
            issues.append(f"bundle_registration_failed:{exc}")
            bundle_status = "untrusted"
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
        issues: list[str] = []
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
        completion_status = str((completion or {}).get("completion_status") or (completion or {}).get("status") or "unknown")
        closure_status = str((closure or {}).get("status") or "unknown")
        if issues:
            status = "untrusted"
        elif manual:
            status = "manual_repaired_legacy"
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
            "completion": completion or {},
            "closure": closure or {},
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
                        {
                            "artifact_id": record.artifact_id,
                            "artifact_type": record.artifact_type,
                            "path": record.path,
                            "content_hash": record.content_hash,
                        }
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

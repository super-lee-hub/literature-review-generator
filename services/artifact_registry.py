from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Iterable, List, Optional

from services.job_workspace import atomic_write_json, utc_now_iso


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ArtifactDependencyRef:
    artifact_type: str
    path: str
    content_hash: str = ""


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    artifact_role: str
    artifact_type: str
    artifact_version: str
    path: str
    producer: str
    job_id: str
    status: str
    content_hash: str
    depends_on: List[ArtifactDependencyRef] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)


class ArtifactRegistry:
    def __init__(self, registry_path: str, job_id: str) -> None:
        self.registry_path = os.path.abspath(registry_path)
        self.job_id = job_id
        self._artifacts: dict[str, ArtifactRecord] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.registry_path):
            return
        try:
            with open(self.registry_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return

        for item in payload.get("artifacts", []):
            depends_on = [
                ArtifactDependencyRef(**dependency)
                for dependency in item.get("depends_on", [])
                if isinstance(dependency, dict)
            ]
            record = ArtifactRecord(
                artifact_id=str(item.get("artifact_id")),
                artifact_role=str(item.get("artifact_role", "")),
                artifact_type=str(item.get("artifact_type", "")),
                artifact_version=str(item.get("artifact_version", "")),
                path=str(item.get("path", "")),
                producer=str(item.get("producer", "")),
                job_id=str(item.get("job_id", self.job_id)),
                status=str(item.get("status", "")),
                content_hash=str(item.get("content_hash", "")),
                depends_on=depends_on,
                created_at=str(item.get("created_at", utc_now_iso())),
            )
            self._artifacts[record.artifact_id] = record

    def save(self) -> None:
        payload = {
            "artifact_registry_version": "v1",
            "job_id": self.job_id,
            "updated_at": utc_now_iso(),
            "artifacts": [
                {
                    **asdict(record),
                    "depends_on": [asdict(item) for item in record.depends_on],
                }
                for record in self._artifacts.values()
            ],
        }
        atomic_write_json(self.registry_path, payload)

    def list_records(self) -> List[ArtifactRecord]:
        return list(self._artifacts.values())

    def get(self, artifact_id: str) -> Optional[ArtifactRecord]:
        return self._artifacts.get(artifact_id)

    def register_file(
        self,
        *,
        artifact_role: str,
        artifact_type: str,
        artifact_version: str,
        path: str,
        producer: str,
        status: str = "ready",
        depends_on: Iterable[ArtifactDependencyRef] | None = None,
        artifact_id: str | None = None,
    ) -> ArtifactRecord:
        abs_path = os.path.abspath(path)
        content_hash = file_sha256(abs_path) if os.path.exists(abs_path) else ""
        record = ArtifactRecord(
            artifact_id=artifact_id or f"{artifact_type}:{os.path.basename(abs_path)}",
            artifact_role=artifact_role,
            artifact_type=artifact_type,
            artifact_version=artifact_version,
            path=abs_path,
            producer=producer,
            job_id=self.job_id,
            status=status,
            content_hash=content_hash,
            depends_on=list(depends_on or []),
            created_at=utc_now_iso(),
        )
        self._artifacts[record.artifact_id] = record
        self.save()
        return record

    def register(
        self,
        *,
        artifact_id: str,
        artifact_type: str,
        artifact_version: str,
        path: str,
        producer: str,
        job_id: str,
        status: str = "ready",
        depends_on: List[Dict[str, str]] | None = None,
    ) -> ArtifactRecord:
        """Register an artifact with dependency tracking.
        
        This is the primary registration method used by repair_integration and other
        Week 4/5 modules. Converts dict-style dependencies to ArtifactDependencyRef.
        """
        abs_path = os.path.abspath(path)
        content_hash = file_sha256(abs_path) if os.path.exists(abs_path) else ""
        
        # Convert dict dependencies to ArtifactDependencyRef objects
        dependency_refs: List[ArtifactDependencyRef] = []
        if depends_on:
            for dep in depends_on:
                if isinstance(dep, dict):
                    # Get the dependency record to extract its type and hash
                    dep_record = self._artifacts.get(dep.get("artifact_id", ""))
                    if dep_record:
                        dependency_refs.append(ArtifactDependencyRef(
                            artifact_type=dep_record.artifact_type,
                            path=dep_record.path,
                            content_hash=dep_record.content_hash,
                        ))
                    else:
                        # Dependency not yet registered, use placeholder
                        dependency_refs.append(ArtifactDependencyRef(
                            artifact_type=dep.get("role", "unknown"),
                            path="",
                            content_hash="",
                        ))
        
        record = ArtifactRecord(
            artifact_id=artifact_id,
            artifact_role=artifact_type,
            artifact_type=artifact_type,
            artifact_version=artifact_version,
            path=abs_path,
            producer=producer,
            job_id=job_id,
            status=status,
            content_hash=content_hash,
            depends_on=dependency_refs,
            created_at=utc_now_iso(),
        )
        self._artifacts[record.artifact_id] = record
        self.save()
        return record


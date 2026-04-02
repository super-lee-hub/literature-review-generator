from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write_json(path: str, payload: Any) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    fd, temp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@dataclass(frozen=True)
class WorkspacePaths:
    root_dir: str
    artifacts_dir: str
    checkpoints_dir: str
    logs_dir: str
    reports_dir: str
    registry_path: str


@dataclass(frozen=True)
class LatestJobPointer:
    project_name: str
    job_id: str
    workspace_path: str
    artifact_registry_path: str
    resume_state: str
    fingerprint_bundle: Dict[str, Any]
    status: str
    updated_at: str


class JobWorkspace:
    def __init__(self, base_output_dir: str, project_name: str, job_id: str) -> None:
        self.base_output_dir = os.path.abspath(base_output_dir)
        self.project_name = project_name
        self.job_id = job_id
        self.paths = WorkspacePaths(
            root_dir=os.path.join(self.base_output_dir, f"{project_name}__{job_id}"),
            artifacts_dir=os.path.join(self.base_output_dir, f"{project_name}__{job_id}", "artifacts"),
            checkpoints_dir=os.path.join(self.base_output_dir, f"{project_name}__{job_id}", "checkpoints"),
            logs_dir=os.path.join(self.base_output_dir, f"{project_name}__{job_id}", "logs"),
            reports_dir=os.path.join(self.base_output_dir, f"{project_name}__{job_id}", "reports"),
            registry_path=os.path.join(self.base_output_dir, f"{project_name}__{job_id}", "artifact_registry.json"),
        )

    @classmethod
    def create(cls, base_output_dir: str, project_name: str, job_id: str | None = None) -> "JobWorkspace":
        workspace = cls(base_output_dir=base_output_dir, project_name=project_name, job_id=job_id or cls.generate_job_id())
        workspace.ensure_exists()
        return workspace

    @staticmethod
    def generate_job_id() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

    @classmethod
    def from_workspace_path(cls, workspace_path: str, project_name: str, job_id: str | None = None) -> "JobWorkspace":
        workspace_path = os.path.abspath(workspace_path)
        base_output_dir = os.path.dirname(workspace_path)
        derived_job_id = job_id
        prefix = f"{project_name}__"
        basename = os.path.basename(workspace_path)
        if derived_job_id is None and basename.startswith(prefix):
            derived_job_id = basename[len(prefix):]
        workspace = cls(base_output_dir=base_output_dir, project_name=project_name, job_id=derived_job_id or cls.generate_job_id())
        workspace.ensure_exists()
        return workspace

    def ensure_exists(self) -> None:
        os.makedirs(self.paths.root_dir, exist_ok=True)
        os.makedirs(self.paths.artifacts_dir, exist_ok=True)
        os.makedirs(self.paths.checkpoints_dir, exist_ok=True)
        os.makedirs(self.paths.logs_dir, exist_ok=True)
        os.makedirs(self.paths.reports_dir, exist_ok=True)

    @property
    def root_dir(self) -> str:
        return self.paths.root_dir

    def artifact_path(self, filename: str) -> str:
        return os.path.join(self.paths.artifacts_dir, filename)

    def checkpoint_path(self, filename: str) -> str:
        return os.path.join(self.paths.checkpoints_dir, filename)

    def report_path(self, filename: str) -> str:
        return os.path.join(self.paths.reports_dir, filename)

    def log_path(self, filename: str) -> str:
        return os.path.join(self.paths.logs_dir, filename)

    def project_pointer_dir(self) -> str:
        return os.path.join(self.base_output_dir, self.project_name)

    def latest_pointer_path(self) -> str:
        return os.path.join(self.project_pointer_dir(), "_latest_job.json")

    def write_latest_pointer(
        self,
        *,
        resume_state: str,
        fingerprint_bundle: Dict[str, Any],
        status: str,
    ) -> str:
        pointer = LatestJobPointer(
            project_name=self.project_name,
            job_id=self.job_id,
            workspace_path=self.paths.root_dir,
            artifact_registry_path=self.paths.registry_path,
            resume_state=resume_state,
            fingerprint_bundle=fingerprint_bundle,
            status=status,
            updated_at=utc_now_iso(),
        )
        pointer_dir = self.project_pointer_dir()
        os.makedirs(pointer_dir, exist_ok=True)
        atomic_write_json(self.latest_pointer_path(), asdict(pointer))
        return self.latest_pointer_path()


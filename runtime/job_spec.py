from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import json
from pathlib import Path
from typing import Any, Dict, Literal, Mapping, cast

from services.job_runner import JobRunRequest, resolve_stage1_reuse, validate_free_mode_options


SourceMode = Literal["direct", "zotero"]

_RUNTIME_SOURCE_FIELDS = frozenset({
    "mode",
    "pdf_folder",
    "zotero_report",
    "library_path",
})
_RUNTIME_JOB_FIELDS = frozenset({
    "project_name",
    "source",
    "job_id",
    "config",
    "action",
    "free_mode_profile",
    "free_mode_idea",
    "summary_file",
    "summary_sources",
    "reuse_stage1",
    "reuse_summary_files",
    "generate_section",
    "queue_file",
    "workspace_path",
    "metadata",
})
_RUNTIME_METADATA_FIELDS = frozenset({
    "review_batch_spec",
    "requested_stages",
    "free_mode_input",
    "free_mode_context",
    "review_intent",
    "validation_required",
    "require_clean_validation",
    "allow_unvalidated_when_validation_optional",
    "audit_actor",
    "audit_reason",
    "audit_scope",
    "stage_plan",
})


def _reject_unknown_fields(
    payload: Mapping[str, Any],
    allowed: frozenset[str],
    *,
    label: str,
) -> None:
    unknown = sorted(str(key) for key in payload if str(key) not in allowed)
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {', '.join(unknown)}")


def _optional_string(payload: Mapping[str, Any], name: str, default: str = "") -> str:
    if name not in payload or payload[name] is None:
        return default
    value = payload[name]
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a JSON string")
    return value


def _string_tuple(payload: Mapping[str, Any], name: str) -> tuple[str, ...]:
    if name not in payload or payload[name] is None:
        return ()
    value = payload[name]
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a JSON array of strings")
    if any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a JSON array of strings")
    return tuple(item.strip() for item in value if item.strip())


def _optional_bool(value: Any, *, field_name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a JSON boolean")
    return value


@dataclass(frozen=True)
class RuntimeSourceSpec:
    mode: SourceMode
    pdf_folder: str = ""
    zotero_report: str = ""
    library_path: str = ""

    def validate(self) -> None:
        if self.mode == "direct":
            if not self.pdf_folder:
                raise ValueError("direct source mode requires pdf_folder")
            return
        if self.mode == "zotero":
            if not self.zotero_report:
                raise ValueError("zotero source mode requires zotero_report")
            if not self.library_path:
                raise ValueError("zotero source mode requires library_path")
            return
        raise ValueError(f"unsupported source mode: {self.mode}")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RuntimeSourceSpec":
        if not isinstance(payload, Mapping):
            raise ValueError("source must be a JSON object")
        _reject_unknown_fields(payload, _RUNTIME_SOURCE_FIELDS, label="source")
        raw_mode = payload.get("mode", "direct")
        if raw_mode is None:
            raw_mode = "direct"
        if not isinstance(raw_mode, str):
            raise ValueError("source.mode must be a JSON string")
        mode = cast(SourceMode, raw_mode)
        return cls(
            mode=mode,
            pdf_folder=_optional_string(payload, "pdf_folder"),
            zotero_report=_optional_string(payload, "zotero_report"),
            library_path=_optional_string(payload, "library_path"),
        )


@dataclass(frozen=True)
class RuntimeJobSpec:
    project_name: str
    source: RuntimeSourceSpec
    job_id: str = ""
    config: str = "config.ini"
    action: str = "run_all"
    free_mode_profile: str = ""
    free_mode_idea: str = ""
    summary_file: str = ""
    summary_sources: tuple[str, ...] = ()
    reuse_stage1: bool | None = None
    reuse_summary_files: tuple[str, ...] = ()
    generate_section: int | None = None
    queue_file: str = "output/_queue/queue.json"
    workspace_path: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not str(self.project_name or "").strip():
            raise ValueError("project_name is required")
        self.source.validate()
        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata must be a JSON object")
        _reject_unknown_fields(self.metadata, _RUNTIME_METADATA_FIELDS, label="metadata")
        allowed_actions = {
            "analyze",
            "derive_review_batch",
            "generate_outline",
            "generate_review",
            "generate_section",
            "validate_review",
            "retry_failed",
            "retry_review_failed",
            "run_all",
        }
        if self.action not in allowed_actions:
            raise ValueError(f"unsupported action: {self.action}")
        if self.action == "generate_section":
            if self.generate_section is None:
                raise ValueError("generate_section action requires generate_section")
            if self.generate_section <= 0:
                raise ValueError("generate_section must be greater than 0")
        if self.action == "derive_review_batch" and not self.metadata.get("review_batch_spec"):
            raise ValueError("derive_review_batch action requires review_batch_spec metadata")
        if "free_mode_input" in self.metadata and not isinstance(
            self.metadata.get("free_mode_input"), Mapping
        ):
            raise ValueError("metadata.free_mode_input must be a JSON object")
        if "free_mode_input" not in self.metadata:
            free_mode_error = validate_free_mode_options(
                self.free_mode_profile,
                self.free_mode_idea,
            )
            if free_mode_error:
                raise ValueError(free_mode_error)
        for field_name in ("audit_actor", "audit_reason"):
            if field_name in self.metadata and not isinstance(self.metadata[field_name], str):
                raise ValueError(f"metadata.{field_name} must be a JSON string")
        for field_name in ("audit_scope", "stage_plan"):
            if field_name in self.metadata and not isinstance(self.metadata[field_name], Mapping):
                raise ValueError(f"metadata.{field_name} must be a JSON object")
        requested_stages = self.metadata.get("requested_stages")
        if requested_stages is not None:
            if not isinstance(requested_stages, (list, tuple)):
                raise ValueError("requested_stages must be a JSON array")
            if any(not isinstance(item, str) for item in requested_stages):
                raise ValueError("requested_stages must contain only JSON strings")
            allowed_stages = {
                "source_intake",
                "analyze",
                "derive_review_batch",
                "outline",
                "review",
                "validate",
            }
            invalid = [str(item) for item in requested_stages if str(item) not in allowed_stages]
            if invalid:
                raise ValueError(f"unsupported requested_stages entries: {invalid}")
            if self.action == "derive_review_batch" and tuple(
                str(item) for item in requested_stages if str(item) != "source_intake"
            ) not in {(), ("derive_review_batch",)}:
                raise ValueError(
                    "derive_review_batch requested_stages may contain only derive_review_batch"
                )
        for field_name in (
            "validation_required",
            "require_clean_validation",
            "allow_unvalidated_when_validation_optional",
        ):
            if field_name in self.metadata:
                _optional_bool(self.metadata[field_name], field_name=field_name)

    def to_job_request(self) -> JobRunRequest:
        self.validate()
        requested_stages_raw = self.metadata.get("requested_stages")
        requested_stages = (
            tuple(dict.fromkeys(str(item) for item in requested_stages_raw))
            if requested_stages_raw is not None
            else None
        )
        return JobRunRequest(
            config=self.config,
            project_name=self.project_name,
            job_id=self.job_id or None,
            pdf_folder=self.source.pdf_folder or None,
            action=self.action,
            free_mode_profile=self.free_mode_profile or None,
            free_mode_idea=self.free_mode_idea or None,
            summary_file=self.summary_file or None,
            summary_sources=tuple(item for item in self.summary_sources if str(item).strip()),
            reuse_stage1=resolve_stage1_reuse(self.action, self.reuse_stage1),
            reuse_summary_files=tuple(item for item in self.reuse_summary_files if str(item).strip()),
            run_all=self.action == "run_all",
            analyze_only=self.action == "analyze",
            generate_outline=self.action == "generate_outline",
            generate_review=self.action == "generate_review",
            generate_section=self.generate_section if self.action == "generate_section" else None,
            validate_review=self.action == "validate_review",
            retry_failed=self.action == "retry_failed",
            retry_review_failed=self.action == "retry_review_failed",
            progress_tracker=None,
            gui=False,
            source_mode=self.source.mode,
            zotero_report=self.source.zotero_report or None,
            library_path=self.source.library_path or None,
            queue_file=self.queue_file,
            workspace_path=self.workspace_path or None,
            requested_stages=requested_stages,
            # Keep omitted policy fields tri-state.  The durable StagePlan
            # builder is the only layer allowed to derive defaults from the
            # action, validation setting, and requested stages.
            validation_required=self.metadata.get("validation_required"),
            require_clean_validation=self.metadata.get("require_clean_validation"),
            allow_unvalidated_when_validation_optional=self.metadata.get(
                "allow_unvalidated_when_validation_optional"
            ),
            derived_summary_source=bool(self.metadata.get("review_batch_spec")),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["source"] = self.source.to_dict()
        payload["summary_sources"] = list(self.summary_sources)
        payload["reuse_summary_files"] = list(self.reuse_summary_files)
        return payload

    def resolved_from(self, origin_dir: str | Path) -> "RuntimeJobSpec":
        """Resolve every spec-owned path against the spec file, never the CWD."""

        origin = Path(origin_dir).expanduser().resolve()

        def resolve_path(value: str) -> str:
            if not value:
                return ""
            path = Path(value).expanduser()
            return str((path if path.is_absolute() else origin / path).resolve())

        metadata = dict(self.metadata)
        review_batch_spec = metadata.get("review_batch_spec")
        if isinstance(review_batch_spec, (str, Path)) and str(review_batch_spec).strip():
            metadata["review_batch_spec"] = resolve_path(str(review_batch_spec))
        elif isinstance(review_batch_spec, Mapping):
            from services.review_batch import ReviewBatchSpecV1

            metadata["review_batch_spec"] = ReviewBatchSpecV1.from_dict(
                review_batch_spec,
                origin_dir=origin,
            ).to_dict()

        return replace(
            self,
            source=replace(
                self.source,
                pdf_folder=resolve_path(self.source.pdf_folder),
                zotero_report=resolve_path(self.source.zotero_report),
                library_path=resolve_path(self.source.library_path),
            ),
            config=resolve_path(self.config),
            free_mode_profile=resolve_path(self.free_mode_profile),
            summary_file=resolve_path(self.summary_file),
            summary_sources=tuple(resolve_path(item) for item in self.summary_sources),
            reuse_summary_files=tuple(resolve_path(item) for item in self.reuse_summary_files),
            queue_file=resolve_path(self.queue_file),
            workspace_path=resolve_path(self.workspace_path),
            metadata=metadata,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RuntimeJobSpec":
        if not isinstance(payload, Mapping):
            raise ValueError("RuntimeJobSpec root must be a JSON object")
        _reject_unknown_fields(payload, _RUNTIME_JOB_FIELDS, label="RuntimeJobSpec")
        raw_source = payload.get("source", {})
        if not isinstance(raw_source, Mapping):
            raise ValueError("source must be a JSON object")
        raw_metadata = payload.get("metadata", {})
        if raw_metadata is None:
            raw_metadata = {}
        if not isinstance(raw_metadata, Mapping):
            raise ValueError("metadata must be a JSON object")
        _reject_unknown_fields(raw_metadata, _RUNTIME_METADATA_FIELDS, label="metadata")
        raw_generate_section = payload.get("generate_section")
        if raw_generate_section is not None and (
            isinstance(raw_generate_section, bool) or not isinstance(raw_generate_section, int)
        ):
            raise ValueError("generate_section must be a JSON integer")
        return cls(
            project_name=_optional_string(payload, "project_name"),
            source=RuntimeSourceSpec.from_dict(raw_source),
            job_id=_optional_string(payload, "job_id"),
            config=_optional_string(payload, "config", "config.ini"),
            action=_optional_string(payload, "action", "run_all"),
            free_mode_profile=_optional_string(payload, "free_mode_profile"),
            free_mode_idea=_optional_string(payload, "free_mode_idea"),
            summary_file=_optional_string(payload, "summary_file"),
            summary_sources=_string_tuple(payload, "summary_sources"),
            reuse_stage1=(
                _optional_bool(payload["reuse_stage1"], field_name="reuse_stage1")
                if payload.get("reuse_stage1") is not None
                else None
            ),
            reuse_summary_files=_string_tuple(payload, "reuse_summary_files"),
            generate_section=raw_generate_section,
            queue_file=_optional_string(payload, "queue_file", "output/_queue/queue.json"),
            workspace_path=_optional_string(payload, "workspace_path"),
            metadata=dict(raw_metadata),
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "RuntimeJobSpec":
        source_mode = cast(
            SourceMode,
            "zotero" if payload.get("zotero_report") else str(payload.get("source_mode") or "direct"),
        )
        return cls(
            project_name=str(payload.get("project_name") or ""),
            source=RuntimeSourceSpec(
                mode=source_mode,
                pdf_folder=str(payload.get("pdf_folder") or ""),
                zotero_report=str(payload.get("zotero_report") or ""),
                library_path=str(payload.get("library_path") or ""),
            ),
            job_id=str(payload.get("job_id") or ""),
            config=str(payload.get("config") or "config.ini"),
            action=str(payload.get("action") or "run_all"),
            free_mode_profile=str(payload.get("free_mode_profile") or ""),
            free_mode_idea=str(payload.get("free_mode_idea") or ""),
            summary_file=str(payload.get("summary_file") or ""),
            summary_sources=tuple(
                str(item).strip()
                for item in payload.get("summary_sources", [])
                if str(item).strip()
            ),
            reuse_stage1=(
                _optional_bool(payload["reuse_stage1"], field_name="reuse_stage1")
                if payload.get("reuse_stage1") is not None
                else None
            ),
            reuse_summary_files=tuple(
                str(item).strip()
                for item in payload.get("reuse_summary_files", [])
                if str(item).strip()
            ),
            generate_section=(
                int(payload["generate_section"])
                if payload.get("generate_section") is not None
                else None
            ),
            queue_file=str(payload.get("queue_file") or "output/_queue/queue.json"),
            workspace_path=str(payload.get("workspace_path") or ""),
            metadata=dict(payload.get("metadata") or {}),
        )


def load_runtime_job_spec(path: str | Path) -> RuntimeJobSpec:
    target = Path(path).expanduser().resolve()
    payload = json.loads(target.read_text(encoding="utf-8"))
    job_spec = RuntimeJobSpec.from_dict(payload).resolved_from(target.parent)
    job_spec.validate()
    return job_spec


def save_runtime_job_spec(path: str | Path, job_spec: RuntimeJobSpec) -> None:
    job_spec.validate()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(job_spec.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

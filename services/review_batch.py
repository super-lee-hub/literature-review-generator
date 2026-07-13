from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Literal, Mapping, Sequence

from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRecord, ArtifactRegistry, file_sha256
from services.job_workspace import JobWorkspace, atomic_write_json, utc_now_iso
from services.summary_reuse import SummaryCatalog, SummarySource


SELECTION_SCHEMA_VERSION = "summary-selection-v1"
BATCH_SCHEMA_VERSION = "review-batch-v1"
DuplicatePolicy = Literal["error", "first"]


class ReviewBatchError(RuntimeError):
    """Base error for deterministic Stage 1 subset derivation."""


class ParentSummaryIntegrityError(ReviewBatchError):
    """Raised when the selected parent summary artifact no longer matches its identity."""


class SummarySelectionError(ReviewBatchError):
    """Raised when the requested subset is missing, ambiguous, duplicated, or mis-sized."""


def _canonical_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resolve_relative(path: str, *, origin_dir: str | Path | None) -> str:
    target = Path(path).expanduser()
    if not target.is_absolute():
        if origin_dir is None:
            raise SummarySelectionError(f"relative path requires an explicit origin directory: {path}")
        target = Path(origin_dir) / target
    return str(target.resolve())


@dataclass(frozen=True)
class SummarySelectionSpecV1:
    parent_job_id: str
    parent_registry_path: str
    parent_artifact_id: str
    parent_content_hash: str
    parent_summary_path: str
    ordered_paper_keys: tuple[str, ...]
    expected_count: int
    duplicate_policy: DuplicatePolicy = "error"
    classification_file: str = ""
    classification_file_hash: str = ""
    identity_column: str = ""
    classification_column: str = ""
    value_filter: str = ""
    schema_version: str = SELECTION_SCHEMA_VERSION
    selection_hash: str = ""

    def __post_init__(self) -> None:
        normalized_keys = tuple(str(item).strip() for item in self.ordered_paper_keys if str(item).strip())
        object.__setattr__(self, "ordered_paper_keys", normalized_keys)
        expected_hash = _canonical_hash(self.hash_payload())
        if self.selection_hash and self.selection_hash != expected_hash:
            raise SummarySelectionError("selection_hash does not match SummarySelectionSpecV1 content")
        object.__setattr__(self, "selection_hash", expected_hash)
        self.validate()

    def validate(self) -> None:
        if self.schema_version != SELECTION_SCHEMA_VERSION:
            raise SummarySelectionError(f"unsupported selection schema: {self.schema_version}")
        for name in (
            "parent_job_id",
            "parent_registry_path",
            "parent_artifact_id",
            "parent_content_hash",
            "parent_summary_path",
        ):
            if not str(getattr(self, name)).strip():
                raise SummarySelectionError(f"{name} is required")
        if len(self.parent_content_hash) != 64:
            raise SummarySelectionError("parent_content_hash must be a SHA-256 digest")
        if self.expected_count <= 0:
            raise SummarySelectionError("expected_count must be greater than zero")
        if self.duplicate_policy not in {"error", "first"}:
            raise SummarySelectionError(f"unsupported duplicate_policy: {self.duplicate_policy}")
        if not self.ordered_paper_keys and not self.classification_file:
            raise SummarySelectionError("ordered_paper_keys or classification_file is required")
        if self.classification_file:
            required = {
                "classification_file_hash": self.classification_file_hash,
                "identity_column": self.identity_column,
                "classification_column": self.classification_column,
                "value_filter": self.value_filter,
            }
            missing = [name for name, value in required.items() if not str(value).strip()]
            if missing:
                raise SummarySelectionError(f"classification selection is missing: {', '.join(missing)}")

    def hash_payload(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "parent_job_id": self.parent_job_id,
            "parent_registry_path": self.parent_registry_path,
            "parent_artifact_id": self.parent_artifact_id,
            "parent_content_hash": self.parent_content_hash,
            "parent_summary_path": self.parent_summary_path,
            "ordered_paper_keys": list(self.ordered_paper_keys),
            "classification_file": self.classification_file,
            "classification_file_hash": self.classification_file_hash,
            "identity_column": self.identity_column,
            "classification_column": self.classification_column,
            "value_filter": self.value_filter,
            "expected_count": self.expected_count,
            "duplicate_policy": self.duplicate_policy,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {**self.hash_payload(), "selection_hash": self.selection_hash}

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        origin_dir: str | Path | None = None,
    ) -> "SummarySelectionSpecV1":
        parent_path = _resolve_relative(str(payload.get("parent_summary_path") or ""), origin_dir=origin_dir)
        parent_registry_path = _resolve_relative(
            str(payload.get("parent_registry_path") or ""),
            origin_dir=origin_dir,
        )
        classification_path = str(payload.get("classification_file") or "")
        if classification_path:
            classification_path = _resolve_relative(classification_path, origin_dir=origin_dir)
        return cls(
            parent_job_id=str(payload.get("parent_job_id") or ""),
            parent_registry_path=parent_registry_path,
            parent_artifact_id=str(payload.get("parent_artifact_id") or ""),
            parent_content_hash=str(payload.get("parent_content_hash") or "").lower(),
            parent_summary_path=parent_path,
            ordered_paper_keys=tuple(str(item) for item in payload.get("ordered_paper_keys", []) or []),
            classification_file=classification_path,
            classification_file_hash=str(payload.get("classification_file_hash") or "").lower(),
            identity_column=str(payload.get("identity_column") or ""),
            classification_column=str(payload.get("classification_column") or ""),
            value_filter=str(payload.get("value_filter") or ""),
            expected_count=int(payload.get("expected_count") or 0),
            duplicate_policy=str(payload.get("duplicate_policy") or "error"),  # type: ignore[arg-type]
            schema_version=str(payload.get("schema_version") or SELECTION_SCHEMA_VERSION),
            selection_hash=str(payload.get("selection_hash") or ""),
        )


@dataclass(frozen=True)
class ReviewBatchSpecV1:
    project_name: str
    selection: SummarySelectionSpecV1
    batch_label: str = ""
    schema_version: str = BATCH_SCHEMA_VERSION
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.schema_version != BATCH_SCHEMA_VERSION:
            raise SummarySelectionError(f"unsupported review batch schema: {self.schema_version}")
        if not self.project_name.strip():
            raise SummarySelectionError("project_name is required")
        self.selection.validate()

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "project_name": self.project_name,
            "batch_label": self.batch_label,
            "selection": self.selection.to_dict(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        origin_dir: str | Path | None = None,
    ) -> "ReviewBatchSpecV1":
        result = cls(
            project_name=str(payload.get("project_name") or ""),
            batch_label=str(payload.get("batch_label") or ""),
            selection=SummarySelectionSpecV1.from_dict(
                dict(payload.get("selection") or {}),
                origin_dir=origin_dir,
            ),
            schema_version=str(payload.get("schema_version") or BATCH_SCHEMA_VERSION),
            metadata=dict(payload.get("metadata") or {}),
        )
        result.validate()
        return result


@dataclass(frozen=True)
class ReviewBatchDerivationResultV1:
    project_name: str
    child_job_id: str
    parent_job_id: str
    parent_artifact_id: str
    parent_summary_hash: str
    selection_hash: str
    selected_count: int
    summary_path: str
    selection_manifest_path: str
    summary_artifact: ArtifactRecord
    selection_artifact: ArtifactRecord
    paper_artifacts: tuple[ArtifactRecord, ...] = ()
    stage1_model_calls: int = 0


def _classification_keys(spec: SummarySelectionSpecV1) -> tuple[str, ...]:
    if not spec.classification_file:
        return spec.ordered_paper_keys
    path = Path(spec.classification_file)
    if not path.is_file():
        raise SummarySelectionError(f"classification file not found: {path}")
    actual_hash = file_sha256(path)
    if actual_hash != spec.classification_file_hash:
        raise SummarySelectionError("classification_file_hash does not match file content")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if spec.identity_column not in (reader.fieldnames or []):
            raise SummarySelectionError(f"identity column not found: {spec.identity_column}")
        if spec.classification_column not in (reader.fieldnames or []):
            raise SummarySelectionError(f"classification column not found: {spec.classification_column}")
        keys = tuple(
            str(row.get(spec.identity_column) or "").strip()
            for row in reader
            if str(row.get(spec.classification_column) or "").strip() == spec.value_filter
            and str(row.get(spec.identity_column) or "").strip()
        )
    if spec.ordered_paper_keys and keys != spec.ordered_paper_keys:
        raise SummarySelectionError("classification-derived keys do not match ordered_paper_keys")
    return keys


def _deduplicate_keys(keys: Sequence[str], policy: DuplicatePolicy) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: list[str] = []
    ordered: list[str] = []
    for key in keys:
        if key in seen:
            duplicates.append(key)
            continue
        seen.add(key)
        ordered.append(key)
    if duplicates and policy == "error":
        raise SummarySelectionError(f"duplicate paper keys: {sorted(set(duplicates))}")
    return tuple(ordered)


def derive_review_batch(
    spec: ReviewBatchSpecV1,
    *,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    producer: str = "services.review_batch.derive_review_batch",
) -> ReviewBatchDerivationResultV1:
    """Materialize a child summary subset without crossing a Stage 1 provider boundary."""

    spec.validate()
    selection = spec.selection
    parent_path = Path(selection.parent_summary_path)
    if not parent_path.is_file():
        raise ParentSummaryIntegrityError(f"parent summary artifact not found: {parent_path}")
    actual_parent_hash = file_sha256(parent_path)
    if actual_parent_hash != selection.parent_content_hash:
        raise ParentSummaryIntegrityError("parent summary content hash changed")
    parent_registry = ArtifactRegistry(selection.parent_registry_path, selection.parent_job_id)
    parent_record = parent_registry.get(selection.parent_artifact_id)
    if parent_record is None:
        raise ParentSummaryIntegrityError("parent summary artifact is not registered")
    if (
        parent_record.job_id != selection.parent_job_id
        or parent_record.artifact_type != "summary_file"
        or parent_record.status != "ready"
        or parent_record.content_hash != selection.parent_content_hash
        or Path(parent_record.path).resolve() != parent_path.resolve()
    ):
        raise ParentSummaryIntegrityError("parent summary registry identity does not match selection")

    keys = _deduplicate_keys(_classification_keys(selection), selection.duplicate_policy)
    if len(keys) != selection.expected_count:
        raise SummarySelectionError(
            f"selection count mismatch: expected {selection.expected_count}, got {len(keys)}"
        )

    catalog = SummaryCatalog.from_sources(
        [SummarySource(path=str(parent_path), source_type="explicit", priority=0, label="parent_stage1")]
    )
    summaries: list[Dict[str, Any]] = []
    missing: list[str] = []
    ambiguous: Dict[str, list[Dict[str, Any]]] = {}
    for key in keys:
        match = catalog.resolve_for_paper({"canonical_paper_key": key})
        if match is None or match.winner is None:
            missing.append(key)
            continue
        if match.is_ambiguous:
            ambiguous[key] = [candidate.summary for candidate in match.ambiguous_candidates]
            continue
        summaries.append(dict(match.winner.summary))
    if missing or ambiguous:
        raise SummarySelectionError(
            json.dumps({"missing": missing, "ambiguous": sorted(ambiguous)}, ensure_ascii=False, sort_keys=True)
        )
    if len(summaries) != selection.expected_count:
        raise SummarySelectionError("resolved summary count does not match expected_count")

    parent_paper_records: Dict[str, tuple[ArtifactRecord, Dict[str, Any]]] = {}
    for record in parent_registry.list_records():
        if record.artifact_type != "paper_artifact" or record.status != "ready":
            continue
        try:
            payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        identity = payload.get("paper_identity", {}) if isinstance(payload, Mapping) else {}
        aliases = {
            str(identity.get("canonical_paper_key") or "").strip(),
            str(identity.get("source_paper_id") or "").strip(),
            *(str(item).strip() for item in identity.get("paper_key_aliases", []) or []),
        }
        for alias in aliases - {""}:
            parent_paper_records[alias] = (record, dict(payload))

    parent_dependency = ArtifactDependencyRefV2(
        dependency_kind="external_job",
        job_id=selection.parent_job_id,
        artifact_id=selection.parent_artifact_id,
        artifact_type="summary_file",
        path=str(parent_path.resolve()),
        content_hash=selection.parent_content_hash,
    )
    selection_manifest_path = workspace.artifact_path("summary_selection_v1.json")
    summary_path = workspace.artifact_path(f"{spec.project_name}_summaries.json")
    manifest = {
        "artifact_type": "summary_selection",
        "artifact_version": "v1",
        "schema_version": SELECTION_SCHEMA_VERSION,
        "project_name": spec.project_name,
        "child_job_id": workspace.job_id,
        "created_at": utc_now_iso(),
        "selection": selection.to_dict(),
        "selected_paper_keys": list(keys),
        "selected_count": len(summaries),
        "stage1_model_calls": 0,
    }
    atomic_write_json(selection_manifest_path, manifest)
    selection_record = registry.register_file(
        artifact_role="summary_selection",
        artifact_type="summary_selection",
        artifact_version="v1",
        path=selection_manifest_path,
        producer=producer,
        artifact_id=f"summary-selection:{selection.selection_hash}",
        depends_on=[parent_dependency],
        metadata={"selection_hash": selection.selection_hash, "selected_count": len(summaries)},
    )
    child_paper_records: list[ArtifactRecord] = []
    if parent_paper_records:
        for order, key in enumerate(keys, start=1):
            parent_paper = parent_paper_records.get(key)
            if parent_paper is None:
                raise ParentSummaryIntegrityError(
                    f"selected paper has no parent paper artifact/evidence dependency: {key}"
                )
            parent_paper_record, paper_payload = parent_paper
            paper_payload["projected_from"] = {
                "job_id": selection.parent_job_id,
                "artifact_id": parent_paper_record.artifact_id,
                "content_hash": parent_paper_record.content_hash,
            }
            child_paper_path = workspace.artifact_path(
                f"paper_artifacts/{order:04d}_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}.json"
            )
            atomic_write_json(child_paper_path, paper_payload)
            external_dependencies = [
                ArtifactDependencyRefV2(
                    dependency_kind="external_job",
                    job_id=selection.parent_job_id,
                    artifact_id=dependency.artifact_id,
                    artifact_type=dependency.artifact_type,
                    path=dependency.path,
                    content_hash=dependency.content_hash,
                )
                for dependency in parent_paper_record.depends_on
            ]
            external_dependencies.insert(
                0,
                ArtifactDependencyRefV2(
                    dependency_kind="external_job",
                    job_id=selection.parent_job_id,
                    artifact_id=parent_paper_record.artifact_id,
                    artifact_type=parent_paper_record.artifact_type,
                    path=parent_paper_record.path,
                    content_hash=parent_paper_record.content_hash,
                ),
            )
            child_paper_records.append(
                registry.register_file(
                    artifact_role="paper_artifact",
                    artifact_type="paper_artifact",
                    artifact_version=str(paper_payload.get("artifact_version") or "v1"),
                    path=child_paper_path,
                    producer=producer,
                    artifact_id=f"derived-paper:{selection.selection_hash}:{order:04d}",
                    depends_on=external_dependencies,
                    metadata={
                        "canonical_paper_key": key,
                        "parent_job_id": selection.parent_job_id,
                        "parent_artifact_id": parent_paper_record.artifact_id,
                    },
                )
            )
    atomic_write_json(summary_path, summaries)
    summary_record = registry.register_file(
        artifact_role="summary",
        artifact_type="summary_file",
        artifact_version="v1",
        path=summary_path,
        producer=producer,
        artifact_id=f"derived-summary:{selection.selection_hash}",
        depends_on=[
            parent_dependency,
            ArtifactDependencyRefV2(
                dependency_kind="local_job",
                job_id=workspace.job_id,
                artifact_id=selection_record.artifact_id,
                artifact_type=selection_record.artifact_type,
                path=selection_record.path,
                content_hash=selection_record.content_hash,
            ),
        ] + [
            ArtifactDependencyRefV2(
                dependency_kind="local_job",
                job_id=workspace.job_id,
                artifact_id=record.artifact_id,
                artifact_type=record.artifact_type,
                path=record.path,
                content_hash=record.content_hash,
            )
            for record in child_paper_records
        ],
        metadata={
            "parent_summary_hash": selection.parent_content_hash,
            "selection_hash": selection.selection_hash,
            "selected_count": len(summaries),
            "stage1_model_calls": 0,
        },
    )
    return ReviewBatchDerivationResultV1(
        project_name=spec.project_name,
        child_job_id=workspace.job_id,
        parent_job_id=selection.parent_job_id,
        parent_artifact_id=selection.parent_artifact_id,
        parent_summary_hash=selection.parent_content_hash,
        selection_hash=selection.selection_hash,
        selected_count=len(summaries),
        summary_path=summary_path,
        selection_manifest_path=selection_manifest_path,
        summary_artifact=summary_record,
        selection_artifact=selection_record,
        paper_artifacts=tuple(child_paper_records),
    )


def load_review_batch_spec(path: str | Path) -> ReviewBatchSpecV1:
    target = Path(path).expanduser().resolve()
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise SummarySelectionError("review batch spec must be a JSON object")
    return ReviewBatchSpecV1.from_dict(payload, origin_dir=target.parent)

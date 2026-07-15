from __future__ import annotations

import csv
import hashlib
import json
import os
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, Literal, Mapping, Sequence

from runtime.attempt_store import AttemptAlreadyRunningError, AttemptExecutionLease
from runtime.reconcile import RuntimeReconciler, validate_canonical_ai_summary
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRecord, ArtifactRegistry, file_sha256
from services.job_workspace import JobWorkspace, atomic_write_json, utc_now_iso
from services.summary_reuse import SummaryCatalog, SummarySource


SELECTION_SCHEMA_VERSION = "summary-selection-v1"
BATCH_SCHEMA_VERSION = "review-batch-v1"
DuplicatePolicy = Literal["error", "first_by_source_order"]
PAPER_EVIDENCE_TYPES = ("normalized_text", "chunks", "page_index")
PAPER_EVIDENCE_MANIFEST_TYPE = "evidence_manifest"
CHILD_OWNER_SCHEMA_VERSION = "review-batch-child-owner-v1"
CHILD_OWNER_ARTIFACT_ID = "review-batch-child-owner"
PROJECTION_RECEIPT_SCHEMA_VERSION = "review-batch-projection-receipt-v2"
PROJECTION_GENERATION_SCHEMA_VERSION = "review-batch-projection-generation-v1"
ProjectionReceiptStatus = Literal["projected", "superseded"]


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


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _validate_workspace_component(value: str, *, label: str) -> None:
    candidate = str(value)
    if (
        not candidate.strip()
        or candidate in {".", ".."}
        or "/" in candidate
        or "\\" in candidate
        or ":" in candidate
        or "\x00" in candidate
        or Path(candidate).is_absolute()
    ):
        raise SummarySelectionError(f"{label} must be a safe single path segment")


def _workspace_path_key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _is_reparse_path(path: str | Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(
        flag and int(getattr(info, "st_file_attributes", 0)) & flag
    )


class _DerivationLeaseTarget:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    def artifact_path(self, _relative_path: str) -> str:
        return self.path


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
        if str(self.duplicate_policy) == "first":
            object.__setattr__(self, "duplicate_policy", "first_by_source_order")
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
        if self.duplicate_policy not in {"error", "first_by_source_order"}:
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
            duplicate_policy=(
                "first_by_source_order"
                if str(payload.get("duplicate_policy") or "error") == "first"
                else str(payload.get("duplicate_policy") or "error")
            ),  # type: ignore[arg-type]
            schema_version=str(payload.get("schema_version") or SELECTION_SCHEMA_VERSION),
            selection_hash=str(payload.get("selection_hash") or ""),
        )


@dataclass(frozen=True)
class ReviewVariantSpecV1:
    project_name: str
    selection: SummarySelectionSpecV1
    variant_id: str = ""
    child_job_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized_id = self.variant_id.strip() or self.project_name.strip()
        object.__setattr__(self, "variant_id", normalized_id)
        object.__setattr__(self, "metadata", _freeze_json(dict(self.metadata)))
        self.validate()

    def validate(self) -> None:
        if not self.project_name.strip():
            raise SummarySelectionError("variant project_name is required")
        _validate_workspace_component(self.project_name, label="variant project_name")
        if not self.variant_id.strip():
            raise SummarySelectionError("variant_id is required")
        if self.child_job_id:
            _validate_workspace_component(self.child_job_id, label="variant child_job_id")
        self.selection.validate()

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "variant_id": self.variant_id,
            "project_name": self.project_name,
            "child_job_id": self.child_job_id,
            "selection": self.selection.to_dict(),
            "metadata": _thaw_json(self.metadata),
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        origin_dir: str | Path | None = None,
    ) -> "ReviewVariantSpecV1":
        return cls(
            variant_id=str(payload.get("variant_id") or ""),
            project_name=str(payload.get("project_name") or ""),
            child_job_id=str(payload.get("child_job_id") or ""),
            selection=SummarySelectionSpecV1.from_dict(
                dict(payload.get("selection") or {}),
                origin_dir=origin_dir,
            ),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True)
class ReviewBatchSpecV1:
    project_name: str
    selection: SummarySelectionSpecV1 | None = None
    variants: tuple[ReviewVariantSpecV1, ...] = ()
    batch_label: str = ""
    schema_version: str = BATCH_SCHEMA_VERSION
    metadata: Mapping[str, Any] = field(default_factory=dict)
    batch_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "variants", tuple(self.variants))
        object.__setattr__(self, "metadata", _freeze_json(dict(self.metadata)))
        self.validate()
        expected_id = "review-batch:" + _canonical_hash(self.hash_payload())[:24]
        object.__setattr__(self, "batch_id", expected_id)

    def validate(self) -> None:
        if self.schema_version != BATCH_SCHEMA_VERSION:
            raise SummarySelectionError(f"unsupported review batch schema: {self.schema_version}")
        if not self.project_name.strip():
            raise SummarySelectionError("project_name is required")
        _validate_workspace_component(self.project_name, label="review batch project_name")
        if self.selection is not None and self.variants:
            raise SummarySelectionError("review batch cannot define both selection and variants")
        if self.selection is None and not self.variants:
            raise SummarySelectionError("review batch requires selection or variants")
        if self.selection is not None:
            self.selection.validate()
            expected_id = "review-batch:" + _canonical_hash(self.hash_payload())[:24]
            if self.batch_id and self.batch_id != expected_id:
                raise SummarySelectionError("batch_id does not match ReviewBatchSpecV1 content")
            return
        for variant in self.variants:
            variant.validate()
        variant_ids = [item.variant_id for item in self.variants]
        project_names = [item.project_name for item in self.variants]
        child_job_ids = [item.child_job_id for item in self.variants if item.child_job_id]
        if len(set(variant_ids)) != len(variant_ids):
            raise SummarySelectionError("review batch variant_id values must be unique")
        if len(set(project_names)) != len(project_names):
            raise SummarySelectionError("review batch project_name values must be unique")
        if len(set(child_job_ids)) != len(child_job_ids):
            raise SummarySelectionError("review batch child_job_id values must be unique")
        parent_identities = {
            (
                item.selection.parent_job_id,
                str(Path(item.selection.parent_registry_path).resolve()),
                item.selection.parent_artifact_id,
                item.selection.parent_content_hash,
                str(Path(item.selection.parent_summary_path).resolve()),
            )
            for item in self.variants
        }
        if len(parent_identities) != 1:
            raise SummarySelectionError("all review batch variants must share one parent corpus")
        expected_id = "review-batch:" + _canonical_hash(self.hash_payload())[:24]
        if self.batch_id and self.batch_id != expected_id:
            raise SummarySelectionError("batch_id does not match ReviewBatchSpecV1 content")

    def variant_specs(self) -> tuple[ReviewVariantSpecV1, ...]:
        if self.variants:
            return self.variants
        assert self.selection is not None
        return (
            ReviewVariantSpecV1(
                variant_id=self.batch_label or self.project_name,
                project_name=self.project_name,
                selection=self.selection,
                metadata=self.metadata,
            ),
        )

    def parent_selection(self) -> SummarySelectionSpecV1:
        return self.variant_specs()[0].selection

    @property
    def is_multi_variant(self) -> bool:
        return bool(self.variants)

    def hash_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "project_name": self.project_name,
            "batch_label": self.batch_label,
            "metadata": _thaw_json(self.metadata),
        }
        if self.variants:
            payload["variants"] = [item.to_dict() for item in self.variants]
        else:
            assert self.selection is not None
            payload["selection"] = self.selection.to_dict()
        return payload

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {**self.hash_payload(), "batch_id": self.batch_id}

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        origin_dir: str | Path | None = None,
    ) -> "ReviewBatchSpecV1":
        raw_variants = payload.get("variants") or []
        variants = tuple(
            ReviewVariantSpecV1.from_dict(item, origin_dir=origin_dir)
            for item in raw_variants
            if isinstance(item, Mapping)
        )
        selection_payload = payload.get("selection")
        selection = (
            SummarySelectionSpecV1.from_dict(
                dict(selection_payload),
                origin_dir=origin_dir,
            )
            if isinstance(selection_payload, Mapping)
            else None
        )
        return cls(
            project_name=str(payload.get("project_name") or ""),
            batch_label=str(payload.get("batch_label") or ""),
            selection=selection,
            variants=variants,
            schema_version=str(payload.get("schema_version") or BATCH_SCHEMA_VERSION),
            metadata=dict(payload.get("metadata") or {}),
            batch_id=str(payload.get("batch_id") or ""),
        )


@dataclass(frozen=True)
class ReviewVariantDerivationResultV1:
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


@dataclass(frozen=True)
class ReviewBatchDerivationResultV1:
    project_name: str
    batch_id: str
    derivation_id: str
    parent_job_id: str
    parent_artifact_id: str
    parent_summary_hash: str
    manifest_path: str
    projection_path: str
    manifest_artifact: ArtifactRecord
    variant_results: tuple[ReviewVariantDerivationResultV1, ...]
    failed_variants: Mapping[str, str] = field(default_factory=dict)
    stage1_model_calls: int = 0

    @property
    def success(self) -> bool:
        return not self.failed_variants


def _derived_child_job_id(
    spec: ReviewBatchSpecV1,
    variant: ReviewVariantSpecV1,
    *,
    coordinator_job_id: str,
) -> str:
    if variant.child_job_id:
        return variant.child_job_id
    identity_hash = _canonical_hash(
        {
            "batch_id": spec.batch_id,
            "coordinator_job_id": coordinator_job_id,
            "variant_id": variant.variant_id,
        }
    )
    return f"review-{identity_hash[:24]}"


def _create_child_workspace(
    *,
    base_output_dir: str,
    project_name: str,
    child_job_id: str,
) -> JobWorkspace:
    _validate_workspace_component(project_name, label="variant project_name")
    _validate_workspace_component(child_job_id, label="variant child_job_id")
    workspace = JobWorkspace(base_output_dir, project_name, child_job_id)
    base = Path(workspace.base_output_dir).resolve()
    child = Path(workspace.root_dir).resolve()
    if os.path.normcase(str(child.parent)) != os.path.normcase(str(base)):
        raise SummarySelectionError("child workspace must remain inside the coordinator output root")
    workspace.ensure_exists()
    return workspace


def _bind_child_owner(
    *,
    spec: ReviewBatchSpecV1,
    variant: ReviewVariantSpecV1,
    coordinator_job_id: str,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    producer: str,
) -> ArtifactRecord:
    owner_path = Path(workspace.artifact_path("review_batch_child_owner_v1.json"))
    owner_record = registry.get(CHILD_OWNER_ARTIFACT_ID)
    expected = {
        "artifact_type": "review_batch_child_owner",
        "artifact_version": "v1",
        "schema_version": CHILD_OWNER_SCHEMA_VERSION,
        "batch_id": spec.batch_id,
        "variant_id": variant.variant_id,
        "selection_hash": variant.selection.selection_hash,
        "coordinator_job_id": coordinator_job_id,
        "child_job_id": workspace.job_id,
        "project_name": variant.project_name,
    }
    if owner_path.exists() or owner_record is not None:
        if not owner_path.is_file():
            raise SummarySelectionError("child workspace has an incomplete owner contract")
        try:
            payload = json.loads(owner_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SummarySelectionError("child workspace owner contract is invalid") from exc
        if (
            not isinstance(payload, Mapping)
            or set(payload) != {*expected, "created_at"}
            or not str(payload.get("created_at") or "")
            or any(payload.get(key) != value for key, value in expected.items())
        ):
            raise SummarySelectionError("child workspace is owned by another review batch variant")
        if owner_record is None:
            existing_records = registry.list_records()
            has_derived_records = any(
                record.artifact_type in {"summary_selection", "summary_file", "paper_artifact"}
                and (
                    record.artifact_id.startswith("summary-selection:")
                    or record.artifact_id.startswith("derived-summary:")
                    or record.artifact_id.startswith("derived-paper:")
                )
                for record in existing_records
            )
            has_derived_files = any(
                path.exists()
                for path in (
                    Path(workspace.artifact_path("summary_selection_v1.json")),
                    Path(workspace.artifact_path(f"{variant.project_name}_summaries.json")),
                    Path(workspace.artifact_path("paper_artifacts")),
                )
            )
            if has_derived_records or has_derived_files:
                raise SummarySelectionError(
                    "unregistered child owner cannot adopt a workspace with derivation outputs"
                )
            owner_record = registry.register_file(
                artifact_role="review_batch_child_owner",
                artifact_type="review_batch_child_owner",
                artifact_version="v1",
                path=owner_path,
                producer=producer,
                artifact_id=CHILD_OWNER_ARTIFACT_ID,
                metadata={
                    "batch_id": spec.batch_id,
                    "variant_id": variant.variant_id,
                    "selection_hash": variant.selection.selection_hash,
                    "coordinator_job_id": coordinator_job_id,
                },
            )
        if (
            owner_record.status != "ready"
            or owner_record.artifact_type != "review_batch_child_owner"
            or Path(owner_record.path).resolve() != owner_path.resolve()
            or owner_record.content_hash != file_sha256(owner_path)
        ):
            raise SummarySelectionError("child workspace owner Registry identity is invalid")
        return owner_record

    existing_records = registry.list_records()
    has_derived_records = any(
        record.artifact_type in {"summary_selection", "summary_file", "paper_artifact"}
        and (
            record.artifact_id.startswith("summary-selection:")
            or record.artifact_id.startswith("derived-summary:")
            or record.artifact_id.startswith("derived-paper:")
        )
        for record in existing_records
    )
    has_derived_files = any(
        path.exists()
        for path in (
            Path(workspace.artifact_path("summary_selection_v1.json")),
            Path(workspace.artifact_path(f"{variant.project_name}_summaries.json")),
            Path(workspace.artifact_path("paper_artifacts")),
        )
    )
    if has_derived_records or has_derived_files:
        raise SummarySelectionError("unowned child workspace already contains review derivation outputs")

    payload = {**expected, "created_at": utc_now_iso()}
    atomic_write_json(str(owner_path), payload)
    return registry.register_file(
        artifact_role="review_batch_child_owner",
        artifact_type="review_batch_child_owner",
        artifact_version="v1",
        path=owner_path,
        producer=producer,
        artifact_id=CHILD_OWNER_ARTIFACT_ID,
        metadata={
            "batch_id": spec.batch_id,
            "variant_id": variant.variant_id,
            "selection_hash": variant.selection.selection_hash,
            "coordinator_job_id": coordinator_job_id,
        },
    )


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


def _selected_parent_paper_records(
    keys: Sequence[str],
    *,
    workspace: JobWorkspace,
    parent_registry: ArtifactRegistry,
) -> Dict[str, tuple[ArtifactRecord, Dict[str, Any]]]:
    requested = set(keys)
    candidates: Dict[str, list[tuple[ArtifactRecord, Dict[str, Any]]]] = {
        key: [] for key in keys
    }
    malformed_record_ids: list[str] = []
    for record in parent_registry.list_records():
        if record.artifact_type != "paper_artifact":
            continue
        try:
            payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            malformed_record_ids.append(record.artifact_id)
            continue
        if not isinstance(payload, Mapping):
            malformed_record_ids.append(record.artifact_id)
            continue
        identity = payload.get("paper_identity")
        if not isinstance(identity, Mapping):
            malformed_record_ids.append(record.artifact_id)
            continue
        canonical_key = str(identity.get("canonical_paper_key") or "").strip()
        if not canonical_key:
            malformed_record_ids.append(record.artifact_id)
            continue
        if canonical_key in requested:
            candidates[canonical_key].append((record, dict(payload)))

    missing = [key for key in keys if not candidates[key]]
    duplicate = {key: len(candidates[key]) for key in keys if len(candidates[key]) > 1}
    if missing or duplicate:
        details: Dict[str, Any] = {"missing": missing, "duplicate": duplicate}
        if missing and malformed_record_ids:
            details["malformed_parent_artifact_ids"] = sorted(malformed_record_ids)
        raise ParentSummaryIntegrityError(
            "parent paper artifact selection failed: "
            + json.dumps(details, ensure_ascii=False, sort_keys=True)
        )

    reconciler = RuntimeReconciler(
        workspace,
        parent_registry,
        external_registry_resolver=lambda job_id: (
            parent_registry if job_id == parent_registry.job_id else None
        ),
    )
    selected: Dict[str, tuple[ArtifactRecord, Dict[str, Any]]] = {}
    for key in keys:
        record, payload = candidates[key][0]
        if not record.depends_on:
            raise ParentSummaryIntegrityError(
                f"parent paper artifact has no evidence dependencies: {key}"
            )
        unhashed_dependencies = [
            dependency.artifact_id
            for dependency in record.depends_on
            if not dependency.content_hash
        ]
        if unhashed_dependencies:
            raise ParentSummaryIntegrityError(
                f"parent paper artifact has unhashed evidence dependencies for {key}: "
                f"{sorted(unhashed_dependencies)}"
            )
        try:
            reconciler.validate_record(record, registry=parent_registry)
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            raise ParentSummaryIntegrityError(
                f"parent paper artifact is invalid for {key}: {exc}"
            ) from exc
        _validate_parent_paper_evidence(
            key,
            record=record,
            parent_registry=parent_registry,
        )
        selected[key] = (record, payload)
    return selected


def _validate_parent_paper_evidence(
    canonical_key: str,
    *,
    record: ArtifactRecord,
    parent_registry: ArtifactRegistry,
) -> None:
    required_types = (*PAPER_EVIDENCE_TYPES, PAPER_EVIDENCE_MANIFEST_TYPE)
    dependencies_by_type = {
        artifact_type: [
            dependency
            for dependency in record.depends_on
            if dependency.artifact_type == artifact_type
        ]
        for artifact_type in required_types
    }
    invalid_counts = {
        artifact_type: len(dependencies)
        for artifact_type, dependencies in dependencies_by_type.items()
        if len(dependencies) != 1
    }
    if invalid_counts:
        raise ParentSummaryIntegrityError(
            f"parent paper artifact evidence dependency count is invalid for {canonical_key}: "
            f"{invalid_counts}"
        )

    manifest_ref = dependencies_by_type[PAPER_EVIDENCE_MANIFEST_TYPE][0]
    manifest_record = parent_registry.get(manifest_ref.artifact_id)
    if manifest_record is None:
        raise ParentSummaryIntegrityError(
            f"parent paper evidence manifest is not registered for {canonical_key}"
        )
    try:
        manifest_payload = json.loads(Path(manifest_record.path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ParentSummaryIntegrityError(
            f"parent paper evidence manifest is unreadable for {canonical_key}: {exc}"
        ) from exc
    if not isinstance(manifest_payload, Mapping):
        raise ParentSummaryIntegrityError(
            f"parent paper evidence manifest is malformed for {canonical_key}"
        )
    if str(manifest_payload.get("canonical_paper_key") or "").strip() != canonical_key:
        raise ParentSummaryIntegrityError(
            f"parent paper evidence manifest identity mismatch for {canonical_key}"
        )
    manifest_items = manifest_payload.get("artifacts")
    if not isinstance(manifest_items, list):
        raise ParentSummaryIntegrityError(
            f"parent paper evidence manifest artifacts are malformed for {canonical_key}"
        )
    manifest_by_type: Dict[str, list[Mapping[str, Any]]] = {
        artifact_type: [
            item
            for item in manifest_items
            if isinstance(item, Mapping)
            and str(item.get("artifact_type") or "") == artifact_type
        ]
        for artifact_type in PAPER_EVIDENCE_TYPES
    }
    invalid_manifest_counts = {
        artifact_type: len(items)
        for artifact_type, items in manifest_by_type.items()
        if len(items) != 1
    }
    if invalid_manifest_counts or len(manifest_items) != len(PAPER_EVIDENCE_TYPES):
        raise ParentSummaryIntegrityError(
            f"parent paper evidence manifest set is invalid for {canonical_key}: "
            f"{invalid_manifest_counts}"
        )
    for artifact_type in PAPER_EVIDENCE_TYPES:
        dependency = dependencies_by_type[artifact_type][0]
        manifest_item = manifest_by_type[artifact_type][0]
        if (
            Path(dependency.path).resolve() != Path(str(manifest_item.get("path") or "")).resolve()
            or dependency.content_hash != str(manifest_item.get("content_hash") or "")
        ):
            raise ParentSummaryIntegrityError(
                f"parent paper evidence manifest does not match {artifact_type} dependency "
                f"for {canonical_key}"
            )


def _validate_summary_paper_lineage(
    canonical_key: str,
    *,
    summary: Mapping[str, Any],
    paper_payload: Mapping[str, Any],
) -> None:
    summary_paper_info = summary.get("paper_info")
    artifact_paper_info = paper_payload.get("paper_info")
    artifact_identity = paper_payload.get("paper_identity")
    if (
        not isinstance(summary_paper_info, Mapping)
        or not isinstance(artifact_paper_info, Mapping)
        or not isinstance(artifact_identity, Mapping)
    ):
        raise ParentSummaryIntegrityError(
            f"summary/paper artifact lineage mismatch for {canonical_key}: paper identity is malformed"
        )

    canonical_identities = {
        str(summary_paper_info.get("canonical_paper_key") or "").strip(),
        str(artifact_paper_info.get("canonical_paper_key") or "").strip(),
        str(artifact_identity.get("canonical_paper_key") or "").strip(),
    }
    if canonical_identities != {canonical_key}:
        raise ParentSummaryIntegrityError(
            f"summary/paper artifact lineage mismatch for {canonical_key}: canonical identity diverged"
        )
    source_identities = {
        str(summary_paper_info.get("source_paper_id") or "").strip(),
        str(artifact_paper_info.get("source_paper_id") or "").strip(),
        str(artifact_identity.get("source_paper_id") or "").strip(),
    }
    if "" in source_identities or len(source_identities) != 1:
        raise ParentSummaryIntegrityError(
            f"summary/paper artifact lineage mismatch for {canonical_key}: source identity diverged"
        )
    if dict(summary_paper_info) != dict(artifact_paper_info):
        raise ParentSummaryIntegrityError(
            f"summary/paper artifact lineage mismatch for {canonical_key}: paper_info diverged"
        )

    analysis = paper_payload.get("analysis")
    artifact_ai_summary = analysis.get("ai_summary") if isinstance(analysis, Mapping) else None
    try:
        canonical_summary = validate_canonical_ai_summary(
            summary.get("ai_summary"),
            label=f"selected summary {canonical_key} ai_summary",
        )
        canonical_artifact_summary = validate_canonical_ai_summary(
            artifact_ai_summary,
            label=f"parent paper artifact {canonical_key} ai_summary",
        )
    except (TypeError, ValueError) as exc:
        raise ParentSummaryIntegrityError(
            f"summary/paper artifact lineage mismatch for {canonical_key}: {exc}"
        ) from exc
    if dict(canonical_summary) != dict(canonical_artifact_summary):
        raise ParentSummaryIntegrityError(
            f"summary/paper artifact lineage mismatch for {canonical_key}: ai_summary diverged"
        )


def _existing_variant_result(
    spec: ReviewVariantSpecV1,
    *,
    keys: Sequence[str],
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    resolve_parent_registry: Callable[[str], ArtifactRegistry | None],
    allow_existing: bool,
) -> ReviewVariantDerivationResultV1 | None:
    selection = spec.selection
    selection_path = Path(workspace.artifact_path("summary_selection_v1.json"))
    summary_path = Path(workspace.artifact_path(f"{spec.project_name}_summaries.json"))
    paper_paths = tuple(
        Path(
            workspace.artifact_path(
                f"paper_artifacts/{order:04d}_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}.json"
            )
        )
        for order, key in enumerate(keys, start=1)
    )
    selection_id = f"summary-selection:{selection.selection_hash}"
    summary_id = f"derived-summary:{selection.selection_hash}"
    paper_ids = tuple(
        f"derived-paper:{selection.selection_hash}:{order:04d}"
        for order in range(1, len(keys) + 1)
    )
    records = {
        artifact_id: registry.get(artifact_id)
        for artifact_id in (selection_id, summary_id, *paper_ids)
    }
    paths = (selection_path, summary_path, *paper_paths)
    any_existing = any(record is not None for record in records.values()) or any(
        path.exists() for path in paths
    )
    if not any_existing:
        return None
    if not allow_existing:
        raise SummarySelectionError("child workspace already contains review derivation outputs")
    if any(record is None for record in records.values()) or any(
        not path.is_file() for path in paths
    ):
        return None

    selection_record = records[selection_id]
    summary_record = records[summary_id]
    paper_records = tuple(records[artifact_id] for artifact_id in paper_ids)
    assert selection_record is not None and summary_record is not None
    assert all(record is not None for record in paper_records)
    typed_paper_records = tuple(record for record in paper_records if record is not None)
    expected_paths = {
        selection_record.artifact_id: selection_path.resolve(),
        summary_record.artifact_id: summary_path.resolve(),
        **{
            record.artifact_id: paper_path.resolve()
            for record, paper_path in zip(typed_paper_records, paper_paths)
        },
    }
    if any(
        Path(record.path).resolve() != expected_paths[record.artifact_id]
        for record in (selection_record, summary_record, *typed_paper_records)
    ):
        raise SummarySelectionError("child review derivation paths do not match their Registry identities")
    if any(
        record.status != "ready"
        for record in (selection_record, summary_record, *typed_paper_records)
    ):
        raise SummarySelectionError("child review derivation contains non-ready artifacts")
    if (
        selection_record.metadata.get("selection_hash") != selection.selection_hash
        or summary_record.metadata.get("selection_hash") != selection.selection_hash
        or selection_record.metadata.get("selected_count") != len(keys)
        or summary_record.metadata.get("selected_count") != len(keys)
    ):
        raise SummarySelectionError("child review derivation metadata does not match its owner")

    reconciler = RuntimeReconciler(
        workspace,
        registry,
        external_registry_resolver=resolve_parent_registry,
    )
    try:
        reconciler.validate_record(summary_record)
    except Exception as exc:
        raise SummarySelectionError("existing child review derivation failed validation") from exc
    return ReviewVariantDerivationResultV1(
        project_name=spec.project_name,
        child_job_id=workspace.job_id,
        parent_job_id=selection.parent_job_id,
        parent_artifact_id=selection.parent_artifact_id,
        parent_summary_hash=selection.parent_content_hash,
        selection_hash=selection.selection_hash,
        selected_count=len(keys),
        summary_path=str(summary_path),
        selection_manifest_path=str(selection_path),
        summary_artifact=summary_record,
        selection_artifact=selection_record,
        paper_artifacts=typed_paper_records,
    )


def validate_review_batch_parent(
    selection: SummarySelectionSpecV1,
) -> tuple[Path, ArtifactRegistry]:
    """Resolve a parent Registry only after its summary identity and hash validate."""

    selection.validate()
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
    return parent_path, parent_registry


def _matches_json_ignoring_created_at(actual: Any, expected: Mapping[str, Any]) -> bool:
    if not isinstance(actual, Mapping):
        return False
    actual_payload = dict(actual)
    expected_payload = dict(expected)
    actual_created_at = str(actual_payload.pop("created_at", "") or "")
    expected_payload.pop("created_at", None)
    return bool(actual_created_at) and actual_payload == expected_payload


def _ensure_json_artifact(
    *,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    path: str | Path,
    artifact_id: str,
    artifact_role: str,
    artifact_type: str,
    artifact_version: str,
    producer: str,
    depends_on: Sequence[ArtifactDependencyRefV2],
    metadata: Mapping[str, Any],
    payload: Any,
    payload_matches: Callable[[Any], bool],
    external_registry_resolver: Callable[[str], ArtifactRegistry | None],
) -> ArtifactRecord:
    """Create, adopt, or reuse one deterministic child artifact without overwrite."""

    target = Path(path)
    existing = registry.get(artifact_id)
    if existing is not None and not target.is_file():
        raise SummarySelectionError(
            f"registered child artifact file is missing: {artifact_id}"
        )
    if existing is None and not target.exists():
        atomic_write_json(str(target), payload)
    if not target.is_file():
        raise SummarySelectionError(f"child artifact path is not a file: {artifact_id}")
    try:
        persisted_payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SummarySelectionError(
            f"child artifact is not valid JSON: {artifact_id}"
        ) from exc
    if not payload_matches(persisted_payload):
        raise SummarySelectionError(
            f"child artifact payload conflicts with deterministic derivation: {artifact_id}"
        )

    dependency_list = list(depends_on)
    metadata_dict = dict(metadata)
    candidate = ArtifactRecord(
        artifact_id=artifact_id,
        artifact_role=artifact_role,
        artifact_type=artifact_type,
        artifact_version=artifact_version,
        path=str(target),
        producer=producer,
        job_id=workspace.job_id,
        status="ready",
        content_hash=file_sha256(target),
        depends_on=dependency_list,
        metadata=metadata_dict,
    )
    reconciler = RuntimeReconciler(
        workspace,
        registry,
        external_registry_resolver=external_registry_resolver,
    )
    if existing is not None:
        immutable_identity = (
            existing.artifact_role,
            existing.artifact_type,
            existing.artifact_version,
            existing.job_id,
            existing.status,
            _workspace_path_key(existing.path),
            existing.content_hash,
            existing.depends_on,
            existing.metadata,
        )
        candidate_identity = (
            candidate.artifact_role,
            candidate.artifact_type,
            candidate.artifact_version,
            candidate.job_id,
            candidate.status,
            _workspace_path_key(candidate.path),
            candidate.content_hash,
            candidate.depends_on,
            candidate.metadata,
        )
        if immutable_identity != candidate_identity:
            raise SummarySelectionError(
                f"registered child artifact conflicts with deterministic derivation: {artifact_id}"
            )
        reconciler.validate_record(existing)
        return existing

    reconciler.validate_record(candidate)
    registered = registry.register_file(
        artifact_role=artifact_role,
        artifact_type=artifact_type,
        artifact_version=artifact_version,
        path=target,
        producer=producer,
        artifact_id=artifact_id,
        depends_on=dependency_list,
        external_registry_resolver=external_registry_resolver,
        metadata=metadata_dict,
    )
    reconciler.validate_record(registered)
    return registered


def derive_review_variant(
    spec: ReviewVariantSpecV1,
    *,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    allow_existing: bool = False,
    producer: str = "services.review_batch.derive_review_batch",
) -> ReviewVariantDerivationResultV1:
    """Materialize a child summary subset without crossing a Stage 1 provider boundary."""

    spec.validate()
    selection = spec.selection
    parent_path, parent_registry = validate_review_batch_parent(selection)

    def resolve_parent_registry(job_id: str) -> ArtifactRegistry | None:
        return parent_registry if job_id == selection.parent_job_id else None

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

    parent_paper_records = _selected_parent_paper_records(
        keys,
        workspace=workspace,
        parent_registry=parent_registry,
    )
    for key, summary in zip(keys, summaries):
        _parent_record, paper_payload = parent_paper_records[key]
        _validate_summary_paper_lineage(
            key,
            summary=summary,
            paper_payload=paper_payload,
        )

    existing = _existing_variant_result(
        spec,
        keys=keys,
        workspace=workspace,
        registry=registry,
        resolve_parent_registry=resolve_parent_registry,
        allow_existing=allow_existing,
    )
    if existing is not None:
        return existing

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
    selection_record = _ensure_json_artifact(
        workspace=workspace,
        registry=registry,
        path=selection_manifest_path,
        artifact_id=f"summary-selection:{selection.selection_hash}",
        artifact_role="summary_selection",
        artifact_type="summary_selection",
        artifact_version="v1",
        producer=producer,
        depends_on=[parent_dependency],
        metadata={"selection_hash": selection.selection_hash, "selected_count": len(summaries)},
        payload=manifest,
        payload_matches=lambda actual: _matches_json_ignoring_created_at(actual, manifest),
        external_registry_resolver=resolve_parent_registry,
    )
    child_paper_records: list[ArtifactRecord] = []
    for order, key in enumerate(keys, start=1):
        parent_paper_record, paper_payload = parent_paper_records[key]
        paper_payload["created_from_job_id"] = workspace.job_id
        paper_payload["created_at"] = utc_now_iso()
        paper_payload["projected_from"] = {
            "job_id": selection.parent_job_id,
            "artifact_id": parent_paper_record.artifact_id,
            "content_hash": parent_paper_record.content_hash,
        }
        child_paper_path = workspace.artifact_path(
            f"paper_artifacts/{order:04d}_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}.json"
        )
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
        paper_version = str(paper_payload.get("artifact_version") or "v1")
        child_paper_records.append(
            _ensure_json_artifact(
                workspace=workspace,
                registry=registry,
                path=child_paper_path,
                artifact_id=f"derived-paper:{selection.selection_hash}:{order:04d}",
                artifact_role="paper_artifact",
                artifact_type="paper_artifact",
                artifact_version=paper_version,
                producer=producer,
                depends_on=external_dependencies,
                metadata={
                    "canonical_paper_key": key,
                    "parent_job_id": selection.parent_job_id,
                    "parent_artifact_id": parent_paper_record.artifact_id,
                },
                payload=paper_payload,
                payload_matches=lambda actual, expected=dict(paper_payload): (
                    _matches_json_ignoring_created_at(actual, expected)
                ),
                external_registry_resolver=resolve_parent_registry,
            )
        )
    summary_record = _ensure_json_artifact(
        workspace=workspace,
        registry=registry,
        path=summary_path,
        artifact_id=f"derived-summary:{selection.selection_hash}",
        artifact_role="summary",
        artifact_type="summary_file",
        artifact_version="v1",
        producer=producer,
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
        payload=summaries,
        payload_matches=lambda actual: actual == summaries,
        external_registry_resolver=resolve_parent_registry,
    )
    return ReviewVariantDerivationResultV1(
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


def _derive_review_batch_owned(
    spec: ReviewBatchSpecV1,
    *,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    derivation_id: str = "",
    projection_generation: int | None = None,
    producer: str = "services.review_batch.derive_review_batch",
) -> ReviewVariantDerivationResultV1 | ReviewBatchDerivationResultV1:
    """Derive one legacy variant or coordinate all variants in one durable batch."""

    spec.validate()
    variants = spec.variant_specs()
    if not spec.is_multi_variant:
        return derive_review_variant(
            variants[0],
            workspace=workspace,
            registry=registry,
            allow_existing=True,
            producer=producer,
        )
    if (
        isinstance(projection_generation, bool)
        or not isinstance(projection_generation, int)
        or projection_generation <= 0
    ):
        raise ReviewBatchError(
            "multi-variant review batch requires a positive projection generation"
        )

    parent_selection = spec.parent_selection()
    if parent_selection.parent_job_id == workspace.job_id:
        raise SummarySelectionError(
            "review batch parent job_id must differ from the coordinator job_id"
        )
    resolved_child_job_ids = tuple(
        _derived_child_job_id(
            spec,
            variant,
            coordinator_job_id=workspace.job_id,
        )
        for variant in variants
    )
    if len(set(resolved_child_job_ids)) != len(resolved_child_job_ids):
        raise SummarySelectionError("resolved review batch child job IDs must be unique")
    reserved_job_ids = {parent_selection.parent_job_id, workspace.job_id}
    conflicts = sorted(set(resolved_child_job_ids) & reserved_job_ids)
    if conflicts:
        raise SummarySelectionError(
            f"review batch child job IDs conflict with reserved jobs: {conflicts}"
        )
    parent_registry = ArtifactRegistry(
        parent_selection.parent_registry_path,
        parent_selection.parent_job_id,
    )
    external_registries: Dict[str, ArtifactRegistry] = {
        parent_selection.parent_job_id: parent_registry,
    }

    def resolve_external_registry(job_id: str) -> ArtifactRegistry | None:
        return external_registries.get(job_id)

    results: list[ReviewVariantDerivationResultV1] = []
    failures: Dict[str, str] = {}
    manifest_variants: list[Dict[str, Any]] = []
    for variant, child_job_id in zip(variants, resolved_child_job_ids):
        child_workspace = JobWorkspace(
            workspace.base_output_dir,
            variant.project_name,
            child_job_id,
        )
        child_lease: AttemptExecutionLease | None = None
        try:
            child_workspace = _create_child_workspace(
                base_output_dir=workspace.base_output_dir,
                project_name=variant.project_name,
                child_job_id=child_job_id,
            )
            child_lease = AttemptExecutionLease(child_workspace)
            child_lease.acquire()
            child_registry = ArtifactRegistry(
                child_workspace.paths.registry_path,
                child_workspace.job_id,
            )
            _bind_child_owner(
                spec=spec,
                variant=variant,
                coordinator_job_id=workspace.job_id,
                workspace=child_workspace,
                registry=child_registry,
                producer=producer,
            )
            external_registries[child_workspace.job_id] = child_registry
            result = derive_review_variant(
                variant,
                workspace=child_workspace,
                registry=child_registry,
                allow_existing=True,
                producer=producer,
            )
        except Exception as exc:
            failure_reason = f"{type(exc).__name__}: {exc}"
            failures[variant.variant_id] = failure_reason
            manifest_variants.append(
                {
                    "variant_id": variant.variant_id,
                    "project_name": variant.project_name,
                    "child_job_id": child_workspace.job_id,
                    "child_workspace_path": child_workspace.root_dir,
                    "child_registry_path": child_workspace.paths.registry_path,
                    "selection_hash": variant.selection.selection_hash,
                    "status": "failed",
                    "selected_count": 0,
                    "stage1_model_calls": 0,
                    "output_artifacts": {},
                    "failure_reason": failure_reason,
                }
            )
            continue
        finally:
            if child_lease is not None:
                child_lease.release()
        results.append(result)
        manifest_variants.append(
            {
                "variant_id": variant.variant_id,
                "project_name": variant.project_name,
                "child_job_id": result.child_job_id,
                "child_workspace_path": child_workspace.root_dir,
                "child_registry_path": child_workspace.paths.registry_path,
                "selection_hash": result.selection_hash,
                "status": "completed",
                "selected_count": result.selected_count,
                "stage1_model_calls": result.stage1_model_calls,
                "output_artifacts": {
                    "summary": {
                        "artifact_id": result.summary_artifact.artifact_id,
                        "artifact_type": result.summary_artifact.artifact_type,
                        "path": result.summary_artifact.path,
                        "content_hash": result.summary_artifact.content_hash,
                    },
                    "selection": {
                        "artifact_id": result.selection_artifact.artifact_id,
                        "artifact_type": result.selection_artifact.artifact_type,
                        "path": result.selection_artifact.path,
                        "content_hash": result.selection_artifact.content_hash,
                    },
                },
                "failure_reason": "",
            }
        )

    invocation_identity = derivation_id.strip() or uuid.uuid4().hex
    manifest_derivation_id = hashlib.sha256(
        f"{spec.batch_id}\0{workspace.job_id}\0{invocation_identity}".encode("utf-8")
    ).hexdigest()[:24]
    manifest_path = workspace.artifact_path(
        f"review_batch_manifests/{manifest_derivation_id}.json"
    )
    manifest_artifact_id = f"{spec.batch_id}:{manifest_derivation_id}"
    if Path(manifest_path).exists() or registry.get(manifest_artifact_id) is not None:
        raise ReviewBatchError(
            f"review batch derivation identity already exists: {manifest_derivation_id}"
        )
    projection_path = workspace.artifact_path("review_batch_manifest.json")
    manifest = {
        "artifact_type": "review_batch_manifest",
        "artifact_version": "v1",
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": spec.batch_id,
        "derivation_id": manifest_derivation_id,
        "projection_generation": projection_generation,
        "project_name": spec.project_name,
        "batch_label": spec.batch_label,
        "coordinator_job_id": workspace.job_id,
        "created_at": utc_now_iso(),
        "parent": {
            "job_id": parent_selection.parent_job_id,
            "registry_path": parent_selection.parent_registry_path,
            "artifact_id": parent_selection.parent_artifact_id,
            "content_hash": parent_selection.parent_content_hash,
            "summary_path": parent_selection.parent_summary_path,
        },
        "variants": manifest_variants,
        "completed_variant_count": len(results),
        "failed_variant_count": len(failures),
        "stage1_model_calls": 0,
        "status": "completed" if not failures else "needs_review",
    }
    atomic_write_json(manifest_path, manifest)
    parent_dependency = ArtifactDependencyRefV2(
        dependency_kind="external_job",
        job_id=parent_selection.parent_job_id,
        artifact_id=parent_selection.parent_artifact_id,
        artifact_type="summary_file",
        path=parent_selection.parent_summary_path,
        content_hash=parent_selection.parent_content_hash,
    )
    manifest_dependencies = [parent_dependency]
    for result in results:
        manifest_dependencies.extend(
            ArtifactDependencyRefV2(
                dependency_kind="external_job",
                job_id=result.child_job_id,
                artifact_id=record.artifact_id,
                artifact_type=record.artifact_type,
                path=record.path,
                content_hash=record.content_hash,
            )
            for record in (result.selection_artifact, result.summary_artifact)
        )
    manifest_record = registry.register_file(
        artifact_role="review_batch_manifest",
        artifact_type="review_batch_manifest",
        artifact_version="v1",
        path=manifest_path,
        producer=producer,
        artifact_id=manifest_artifact_id,
        depends_on=manifest_dependencies,
        external_registry_resolver=resolve_external_registry,
        metadata={
            "batch_id": spec.batch_id,
            "derivation_id": manifest_derivation_id,
            "projection_generation": projection_generation,
            "variant_count": len(variants),
            "completed_variant_count": len(results),
            "failed_variant_count": len(failures),
            "stage1_model_calls": 0,
        },
    )
    _commit_review_batch_projection(
        workspace=workspace,
        registry=registry,
        batch_id=spec.batch_id,
        manifest_derivation_id=manifest_derivation_id,
        manifest_artifact_id=manifest_artifact_id,
        manifest_path=manifest_path,
        manifest_payload=manifest,
        projection_generation=projection_generation,
    )
    return ReviewBatchDerivationResultV1(
        project_name=spec.project_name,
        batch_id=spec.batch_id,
        derivation_id=manifest_derivation_id,
        parent_job_id=parent_selection.parent_job_id,
        parent_artifact_id=parent_selection.parent_artifact_id,
        parent_summary_hash=parent_selection.parent_content_hash,
        manifest_path=manifest_path,
        projection_path=projection_path,
        manifest_artifact=manifest_record,
        variant_results=tuple(results),
        failed_variants=dict(failures),
    )


def _recover_review_batch_manifest(
    spec: ReviewBatchSpecV1,
    *,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    manifest_path: str,
    manifest_derivation_id: str,
    projection_generation: int,
    producer: str,
    existing_record: ArtifactRecord | None = None,
) -> ReviewBatchDerivationResultV1:
    """Recover a complete immutable manifest without rerunning child derivation."""

    try:
        payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ReviewBatchError("orphan review batch manifest must be a JSON object")
        if (
            str(payload.get("batch_id") or "") != spec.batch_id
            or str(payload.get("derivation_id") or "") != manifest_derivation_id
            or str(payload.get("coordinator_job_id") or "") != workspace.job_id
            or payload.get("projection_generation") != projection_generation
        ):
            raise ReviewBatchError(
                "orphan review batch manifest projection identity is inconsistent"
            )
        parent = payload.get("parent")
        raw_variants = payload.get("variants")
        if not isinstance(parent, Mapping) or not isinstance(raw_variants, list):
            raise ReviewBatchError("orphan review batch manifest is structurally incomplete")

        parent_dependency = ArtifactDependencyRefV2(
            dependency_kind="external_job",
            job_id=str(parent.get("job_id") or ""),
            artifact_id=str(parent.get("artifact_id") or ""),
            artifact_type="summary_file",
            path=str(parent.get("summary_path") or ""),
            content_hash=str(parent.get("content_hash") or ""),
        )
        parent_registry_path = str(parent.get("registry_path") or "")
        external_registries: Dict[str, ArtifactRegistry] = {
            parent_dependency.job_id: ArtifactRegistry(
                parent_registry_path,
                parent_dependency.job_id,
            )
        }
        dependencies: list[ArtifactDependencyRefV2] = [parent_dependency]
        completed_variants: list[
            tuple[
                Mapping[str, Any],
                ArtifactRegistry,
                ArtifactRecord,
                ArtifactRecord,
            ]
        ] = []
        failures: Dict[str, str] = {}
        for raw_variant in raw_variants:
            if not isinstance(raw_variant, Mapping):
                raise ReviewBatchError("orphan review batch manifest has an invalid variant")
            variant_id = str(raw_variant.get("variant_id") or "")
            status = str(raw_variant.get("status") or "")
            if status == "failed":
                failures[variant_id] = str(raw_variant.get("failure_reason") or "")
                continue
            if status != "completed":
                raise ReviewBatchError(
                    f"orphan review batch manifest has unsupported status {status!r}"
                )
            child_job_id = str(raw_variant.get("child_job_id") or "")
            child_registry = ArtifactRegistry(
                str(raw_variant.get("child_registry_path") or ""),
                child_job_id,
            )
            external_registries[child_job_id] = child_registry
            outputs = raw_variant.get("output_artifacts")
            if not isinstance(outputs, Mapping):
                raise ReviewBatchError("orphan review batch variant outputs are missing")
            linked_records: list[ArtifactRecord] = []
            for output_name in ("selection", "summary"):
                link = outputs.get(output_name)
                if not isinstance(link, Mapping):
                    raise ReviewBatchError(
                        f"orphan review batch {output_name} link is missing"
                    )
                dependency = ArtifactDependencyRefV2(
                    dependency_kind="external_job",
                    job_id=child_job_id,
                    artifact_id=str(link.get("artifact_id") or ""),
                    artifact_type=str(link.get("artifact_type") or ""),
                    path=str(link.get("path") or ""),
                    content_hash=str(link.get("content_hash") or ""),
                )
                dependencies.append(dependency)
                linked_record = child_registry.get(dependency.artifact_id)
                if linked_record is None:
                    raise ReviewBatchError(
                        f"orphan review batch output is not registered: {dependency.artifact_id}"
                    )
                linked_records.append(linked_record)
            completed_variants.append(
                (raw_variant, child_registry, linked_records[1], linked_records[0])
            )

        def resolve_external_registry(job_id: str) -> ArtifactRegistry | None:
            return external_registries.get(job_id)

        manifest_artifact_id = f"{spec.batch_id}:{manifest_derivation_id}"
        metadata = {
            "batch_id": spec.batch_id,
            "derivation_id": manifest_derivation_id,
            "projection_generation": projection_generation,
            "variant_count": len(raw_variants),
            "completed_variant_count": len(completed_variants),
            "failed_variant_count": len(failures),
            "stage1_model_calls": 0,
        }
        candidate_record = ArtifactRecord(
            artifact_id=manifest_artifact_id,
            artifact_role="review_batch_manifest",
            artifact_type="review_batch_manifest",
            artifact_version="v1",
            path=manifest_path,
            producer=producer,
            job_id=workspace.job_id,
            status="ready",
            content_hash=file_sha256(manifest_path),
            depends_on=dependencies,
            metadata=metadata,
            created_at=str(payload.get("created_at") or utc_now_iso()),
        )
        reconciler = RuntimeReconciler(
            workspace,
            registry,
            external_registry_resolver=resolve_external_registry,
        )
        reconciler.validate_record(candidate_record)
        if existing_record is None:
            manifest_record = registry.register_file(
                artifact_role=candidate_record.artifact_role,
                artifact_type=candidate_record.artifact_type,
                artifact_version=candidate_record.artifact_version,
                path=manifest_path,
                producer=producer,
                artifact_id=manifest_artifact_id,
                depends_on=dependencies,
                external_registry_resolver=resolve_external_registry,
                metadata=metadata,
            )
        else:
            existing_identity = (
                existing_record.artifact_id,
                existing_record.artifact_role,
                existing_record.artifact_type,
                existing_record.artifact_version,
                existing_record.job_id,
                existing_record.status,
                _workspace_path_key(existing_record.path),
                existing_record.content_hash,
                existing_record.depends_on,
                existing_record.metadata,
            )
            candidate_identity = (
                candidate_record.artifact_id,
                candidate_record.artifact_role,
                candidate_record.artifact_type,
                candidate_record.artifact_version,
                candidate_record.job_id,
                candidate_record.status,
                _workspace_path_key(candidate_record.path),
                candidate_record.content_hash,
                candidate_record.depends_on,
                candidate_record.metadata,
            )
            if existing_identity != candidate_identity:
                raise ReviewBatchError(
                    "registered review batch manifest identity conflicts with immutable file"
                )
            reconciler.validate_record(existing_record)
            manifest_record = existing_record
        projection_path = workspace.artifact_path("review_batch_manifest.json")
        _commit_review_batch_projection(
            workspace=workspace,
            registry=registry,
            batch_id=spec.batch_id,
            manifest_derivation_id=manifest_derivation_id,
            manifest_artifact_id=manifest_artifact_id,
            manifest_path=manifest_path,
            manifest_payload=payload,
            projection_generation=projection_generation,
        )

        variant_results = tuple(
            ReviewVariantDerivationResultV1(
                project_name=str(raw_variant.get("project_name") or ""),
                child_job_id=str(raw_variant.get("child_job_id") or ""),
                parent_job_id=parent_dependency.job_id,
                parent_artifact_id=parent_dependency.artifact_id,
                parent_summary_hash=parent_dependency.content_hash,
                selection_hash=str(raw_variant.get("selection_hash") or ""),
                selected_count=int(raw_variant.get("selected_count") or 0),
                summary_path=summary_record.path,
                selection_manifest_path=selection_record.path,
                summary_artifact=summary_record,
                selection_artifact=selection_record,
                paper_artifacts=tuple(
                    record
                    for record in child_registry.list_records()
                    if record.artifact_type == "paper_artifact" and record.status == "ready"
                ),
            )
            for raw_variant, child_registry, summary_record, selection_record in completed_variants
        )
        return ReviewBatchDerivationResultV1(
            project_name=spec.project_name,
            batch_id=spec.batch_id,
            derivation_id=manifest_derivation_id,
            parent_job_id=parent_dependency.job_id,
            parent_artifact_id=parent_dependency.artifact_id,
            parent_summary_hash=parent_dependency.content_hash,
            manifest_path=manifest_path,
            projection_path=projection_path,
            manifest_artifact=manifest_record,
            variant_results=variant_results,
            failed_variants=failures,
        )
    except ReviewBatchError:
        raise
    except Exception as exc:
        raise ReviewBatchError(
            f"cannot recover review batch manifest: {exc}"
        ) from exc


def validate_review_batch_layout(
    spec: ReviewBatchSpecV1,
    *,
    workspace: JobWorkspace,
) -> tuple[str, ...]:
    """Validate coordinator, parent, and child identities without writing state."""

    spec.validate()
    if not spec.is_multi_variant:
        raise SummarySelectionError("review batch layout validation requires multi-variant spec")
    if spec.project_name != workspace.project_name:
        raise SummarySelectionError(
            "review batch coordinator project does not match its workspace"
        )
    variants = spec.variant_specs()
    parent_selection = spec.parent_selection()
    if parent_selection.parent_job_id == workspace.job_id:
        raise SummarySelectionError(
            "review batch parent job_id must differ from the coordinator job_id"
        )
    resolved_child_job_ids = tuple(
        _derived_child_job_id(
            spec,
            variant,
            coordinator_job_id=workspace.job_id,
        )
        for variant in variants
    )
    if len(set(resolved_child_job_ids)) != len(resolved_child_job_ids):
        raise SummarySelectionError("resolved review batch child job IDs must be unique")
    reserved_job_ids = {parent_selection.parent_job_id, workspace.job_id}
    conflicts = sorted(set(resolved_child_job_ids) & reserved_job_ids)
    if conflicts:
        raise SummarySelectionError(
            f"review batch child job IDs conflict with reserved jobs: {conflicts}"
        )

    child_workspaces = tuple(
        JobWorkspace(
            workspace.base_output_dir,
            variant.project_name,
            child_job_id,
        )
        for variant, child_job_id in zip(variants, resolved_child_job_ids)
    )
    manifest_dir = Path(workspace.artifact_path("review_batch_manifests"))
    coordinator_paths = (
        Path(workspace.root_dir),
        Path(workspace.paths.artifacts_dir),
        manifest_dir,
    )
    if any(_is_reparse_path(path) for path in coordinator_paths):
        raise SummarySelectionError(
            "review batch coordinator workspace must not contain a symlink or reparse point"
        )
    artifacts_root_key = _workspace_path_key(workspace.paths.artifacts_dir)
    if _workspace_path_key(manifest_dir.parent) != artifacts_root_key:
        raise SummarySelectionError(
            "review batch manifest directory is outside the coordinator workspace"
        )
    child_sensitive_paths = tuple(
        (
            Path(child.root_dir),
            Path(child.paths.artifacts_dir),
            Path(child.paths.registry_path),
            Path(child.artifact_path("job_attempts")),
            Path(child.artifact_path("paper_artifacts")),
        )
        for child in child_workspaces
    )
    if any(
        _is_reparse_path(path)
        for child_paths in child_sensitive_paths
        for path in child_paths
    ):
        raise SummarySelectionError(
            "review batch child workspace must not be a symlink or reparse point"
        )
    child_workspace_keys = tuple(
        _workspace_path_key(child.root_dir) for child in child_workspaces
    )
    output_root_key = _workspace_path_key(workspace.base_output_dir)
    if any(
        _workspace_path_key(Path(child_key).parent) != output_root_key
        for child_key in child_workspace_keys
    ):
        raise SummarySelectionError(
            "review batch child workspace is outside the coordinator output root"
        )
    for child, paths in zip(child_workspaces, child_sensitive_paths):
        root_key = _workspace_path_key(child.root_dir)
        artifacts_key = _workspace_path_key(child.paths.artifacts_dir)
        if (
            _workspace_path_key(Path(artifacts_key).parent) != root_key
            or _workspace_path_key(Path(child.paths.registry_path).parent) != root_key
            or any(
                _workspace_path_key(Path(path).parent) != artifacts_key
                for path in paths[3:]
            )
        ):
            raise SummarySelectionError(
                "review batch child artifact paths are outside the child workspace"
            )
    coordinator_workspace_key = _workspace_path_key(workspace.root_dir)
    parent_workspace_key = _workspace_path_key(
        Path(parent_selection.parent_registry_path).parent
    )
    if parent_workspace_key == coordinator_workspace_key:
        raise SummarySelectionError(
            "review batch parent workspace aliases the coordinator workspace"
        )
    if coordinator_workspace_key in child_workspace_keys:
        raise SummarySelectionError(
            "review batch child workspace aliases the coordinator workspace"
        )
    if parent_workspace_key in child_workspace_keys:
        raise SummarySelectionError(
            "review batch child workspace aliases the parent workspace"
        )
    if len(set(child_workspace_keys)) != len(child_workspace_keys):
        raise SummarySelectionError("review batch child workspace paths must be unique")
    return resolved_child_job_ids


def _review_batch_projection_generation_path(
    *,
    workspace: JobWorkspace,
    manifest_derivation_id: str,
) -> Path:
    return Path(
        workspace.artifact_path(
            f"review_batch_manifests/.{manifest_derivation_id}.projection.generation"
        )
    )


def _read_review_batch_projection_generations(
    *,
    workspace: JobWorkspace,
) -> Dict[str, tuple[int, str]]:
    directory = Path(workspace.artifact_path("review_batch_manifests"))
    if not directory.exists():
        return {}
    if not directory.is_dir() or _is_reparse_path(directory):
        raise ReviewBatchError("review batch projection generation directory is invalid")

    reservations: Dict[str, tuple[int, str]] = {}
    generations: set[int] = set()
    required_fields = {
        "artifact_type",
        "artifact_version",
        "schema_version",
        "batch_id",
        "derivation_id",
        "projection_generation",
        "coordinator_job_id",
    }
    for path in sorted(directory.glob(".*.projection.generation")):
        if not path.is_file() or _is_reparse_path(path):
            raise ReviewBatchError("review batch projection generation is not a regular file")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReviewBatchError("review batch projection generation is invalid") from exc
        if not isinstance(payload, Mapping) or set(payload) != required_fields:
            raise ReviewBatchError("review batch projection generation has an invalid schema")
        derivation_id = str(payload.get("derivation_id") or "")
        batch_id = str(payload.get("batch_id") or "")
        generation = payload.get("projection_generation")
        if (
            payload.get("artifact_type") != "review_batch_projection_generation"
            or payload.get("artifact_version") != "v1"
            or payload.get("schema_version") != PROJECTION_GENERATION_SCHEMA_VERSION
            or payload.get("coordinator_job_id") != workspace.job_id
            or not batch_id
            or len(derivation_id) != 24
            or any(char not in "0123456789abcdef" for char in derivation_id)
            or path.name != f".{derivation_id}.projection.generation"
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation <= 0
            or derivation_id in reservations
            or generation in generations
        ):
            raise ReviewBatchError("review batch projection generation is inconsistent")
        reservations[derivation_id] = (generation, batch_id)
        generations.add(generation)

    if generations and generations != set(range(1, max(generations) + 1)):
        raise ReviewBatchError("review batch projection generations are not contiguous")
    return reservations


def _reserve_review_batch_projection_generation(
    *,
    workspace: JobWorkspace,
    batch_id: str,
    manifest_derivation_id: str,
) -> int:
    reservations = _read_review_batch_projection_generations(workspace=workspace)
    existing = reservations.get(manifest_derivation_id)
    if existing is not None:
        generation, reserved_batch_id = existing
        if reserved_batch_id != batch_id:
            raise ReviewBatchError(
                "review batch projection generation conflicts with derivation identity"
            )
        return generation

    generation = max((item[0] for item in reservations.values()), default=0) + 1
    reservation_path = _review_batch_projection_generation_path(
        workspace=workspace,
        manifest_derivation_id=manifest_derivation_id,
    )
    atomic_write_json(
        str(reservation_path),
        {
            "artifact_type": "review_batch_projection_generation",
            "artifact_version": "v1",
            "schema_version": PROJECTION_GENERATION_SCHEMA_VERSION,
            "batch_id": batch_id,
            "derivation_id": manifest_derivation_id,
            "projection_generation": generation,
            "coordinator_job_id": workspace.job_id,
        },
    )
    persisted = _read_review_batch_projection_generations(workspace=workspace).get(
        manifest_derivation_id
    )
    if persisted != (generation, batch_id):
        raise ReviewBatchError("review batch projection generation was not durable")
    return generation


def _review_batch_projection_receipt_path(
    *,
    workspace: JobWorkspace,
    manifest_derivation_id: str,
) -> Path:
    return Path(
        workspace.artifact_path(
            f"review_batch_manifests/.{manifest_derivation_id}.projection.receipt"
        )
    )


def _write_review_batch_projection_receipt(
    *,
    workspace: JobWorkspace,
    batch_id: str,
    manifest_derivation_id: str,
    manifest_artifact_id: str,
    manifest_path: str | Path,
    projection_generation: int,
    projection_status: ProjectionReceiptStatus,
    head_derivation_id: str,
    head_projection_generation: int,
    head_manifest_hash: str,
) -> None:
    receipt_path = _review_batch_projection_receipt_path(
        workspace=workspace,
        manifest_derivation_id=manifest_derivation_id,
    )
    payload = {
        "artifact_type": "review_batch_projection_receipt",
        "artifact_version": "v1",
        "schema_version": PROJECTION_RECEIPT_SCHEMA_VERSION,
        "batch_id": batch_id,
        "derivation_id": manifest_derivation_id,
        "manifest_artifact_id": manifest_artifact_id,
        "manifest_hash": file_sha256(manifest_path),
        "projection_generation": projection_generation,
        "projection_status": projection_status,
        "head_derivation_id": head_derivation_id,
        "head_projection_generation": head_projection_generation,
        "head_manifest_hash": head_manifest_hash,
        "coordinator_job_id": workspace.job_id,
        "created_at": utc_now_iso(),
    }
    atomic_write_json(str(receipt_path), payload)


def _has_valid_review_batch_projection_receipt(
    *,
    workspace: JobWorkspace,
    batch_id: str,
    manifest_derivation_id: str,
    manifest_artifact_id: str,
    manifest_path: str | Path,
    projection_generation: int,
) -> bool:
    receipt_path = _review_batch_projection_receipt_path(
        workspace=workspace,
        manifest_derivation_id=manifest_derivation_id,
    )
    if not receipt_path.exists():
        return False
    if not receipt_path.is_file() or _is_reparse_path(receipt_path):
        raise ReviewBatchError("review batch projection receipt is not a file")
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewBatchError("review batch projection receipt is invalid") from exc
    expected = {
        "artifact_type": "review_batch_projection_receipt",
        "artifact_version": "v1",
        "schema_version": PROJECTION_RECEIPT_SCHEMA_VERSION,
        "batch_id": batch_id,
        "derivation_id": manifest_derivation_id,
        "manifest_artifact_id": manifest_artifact_id,
        "manifest_hash": file_sha256(manifest_path),
        "projection_generation": projection_generation,
        "coordinator_job_id": workspace.job_id,
    }
    if (
        not isinstance(payload, Mapping)
        or set(payload)
        != {
            *expected,
            "projection_status",
            "head_derivation_id",
            "head_projection_generation",
            "head_manifest_hash",
            "created_at",
        }
        or not str(payload.get("created_at") or "")
        or payload.get("projection_status") not in {"projected", "superseded"}
        or len(str(payload.get("head_derivation_id") or "")) != 24
        or any(
            char not in "0123456789abcdef"
            for char in str(payload.get("head_derivation_id") or "")
        )
        or isinstance(payload.get("head_projection_generation"), bool)
        or not isinstance(payload.get("head_projection_generation"), int)
        or int(payload.get("head_projection_generation") or 0) <= 0
        or len(str(payload.get("head_manifest_hash") or "")) != 64
        or any(
            char not in "0123456789abcdef"
            for char in str(payload.get("head_manifest_hash") or "")
        )
        or any(payload.get(key) != value for key, value in expected.items())
    ):
        raise ReviewBatchError("review batch projection receipt conflicts with manifest")
    head_derivation_id = str(payload["head_derivation_id"])
    head_generation = int(payload["head_projection_generation"])
    projection_status = str(payload["projection_status"])
    if (
        projection_status == "projected"
        and (
            head_derivation_id != manifest_derivation_id
            or head_generation != projection_generation
            or payload.get("head_manifest_hash") != expected["manifest_hash"]
        )
    ) or (
        projection_status == "superseded"
        and (
            head_derivation_id == manifest_derivation_id
            or head_generation <= projection_generation
        )
    ):
        raise ReviewBatchError("review batch projection receipt head is inconsistent")
    return True


def _validated_registered_projection_manifest(
    *,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    payload: Mapping[str, Any],
) -> tuple[str, str, int, Path]:
    batch_id = str(payload.get("batch_id") or "")
    derivation_id = str(payload.get("derivation_id") or "")
    generation = payload.get("projection_generation")
    if (
        str(payload.get("coordinator_job_id") or "") != workspace.job_id
        or not batch_id
        or len(derivation_id) != 24
        or any(char not in "0123456789abcdef" for char in derivation_id)
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 0
    ):
        raise ReviewBatchError("review batch projection identity is invalid")

    manifest_path = Path(
        workspace.artifact_path(f"review_batch_manifests/{derivation_id}.json")
    )
    if not manifest_path.is_file() or _is_reparse_path(manifest_path):
        raise ReviewBatchError("review batch projection immutable manifest is missing")
    try:
        immutable_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewBatchError("review batch projection immutable manifest is invalid") from exc
    if not isinstance(immutable_payload, Mapping) or immutable_payload != payload:
        raise ReviewBatchError("review batch projection differs from immutable manifest")

    artifact_id = f"{batch_id}:{derivation_id}"
    record = registry.get(artifact_id)
    if (
        record is None
        or record.artifact_type != "review_batch_manifest"
        or record.status != "ready"
        or _workspace_path_key(record.path) != _workspace_path_key(manifest_path)
        or record.content_hash != file_sha256(manifest_path)
        or record.metadata.get("projection_generation") != generation
    ):
        raise ReviewBatchError("review batch projection Registry identity is invalid")
    parent = payload.get("parent")
    variants = payload.get("variants")
    if not isinstance(parent, Mapping) or not isinstance(variants, list):
        raise ReviewBatchError("review batch projection manifest links are invalid")
    try:
        parent_job_id = str(parent.get("job_id") or "")
        external_registries: Dict[str, ArtifactRegistry] = {
            parent_job_id: ArtifactRegistry(
                str(parent.get("registry_path") or ""),
                parent_job_id,
            )
        }
        for raw_variant in variants:
            if not isinstance(raw_variant, Mapping):
                raise ReviewBatchError("review batch projection variant is invalid")
            if str(raw_variant.get("status") or "") != "completed":
                continue
            child_job_id = str(raw_variant.get("child_job_id") or "")
            external_registries[child_job_id] = ArtifactRegistry(
                str(raw_variant.get("child_registry_path") or ""),
                child_job_id,
            )
        RuntimeReconciler(
            workspace,
            registry,
            external_registry_resolver=lambda job_id: external_registries.get(job_id),
        ).validate_record(record)
    except ReviewBatchError:
        raise
    except Exception as exc:
        raise ReviewBatchError(
            "review batch projection manifest failed full validation"
        ) from exc
    return batch_id, derivation_id, generation, manifest_path


def _committed_review_batch_projection_head(
    *,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
) -> tuple[Mapping[str, Any], str, int, Path] | None:
    registry.reload()
    reservations = _read_review_batch_projection_generations(workspace=workspace)
    committed: Dict[int, tuple[Mapping[str, Any], str, Path]] = {}
    for record in registry.list_records():
        if record.artifact_type != "review_batch_manifest" or record.status != "ready":
            continue
        try:
            payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReviewBatchError("committed review batch manifest is invalid") from exc
        if not isinstance(payload, Mapping):
            raise ReviewBatchError("committed review batch manifest must be a JSON object")
        batch_id, derivation_id, generation, path = (
            _validated_registered_projection_manifest(
                workspace=workspace,
                registry=registry,
                payload=payload,
            )
        )
        if record.artifact_id != f"{batch_id}:{derivation_id}":
            raise ReviewBatchError("committed review batch manifest identity is inconsistent")
        if reservations.get(derivation_id) != (generation, batch_id):
            raise ReviewBatchError(
                "committed review batch manifest generation is not reserved"
            )
        if generation in committed:
            raise ReviewBatchError("committed review batch projection generation is duplicated")
        committed[generation] = (payload, derivation_id, path)
    if not committed:
        return None
    head_generation = max(committed)
    head_payload, head_derivation_id, head_path = committed[head_generation]
    return head_payload, head_derivation_id, head_generation, head_path


def _repair_review_batch_projection_from_registry(
    *,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
) -> tuple[Mapping[str, Any], str, int, Path] | None:
    head = _committed_review_batch_projection_head(
        workspace=workspace,
        registry=registry,
    )
    projection_path = Path(workspace.artifact_path("review_batch_manifest.json"))
    if head is None:
        if projection_path.exists():
            raise ReviewBatchError(
                "review batch projection exists without a committed manifest"
            )
        return None

    head_payload, _head_derivation_id, _head_generation, head_path = head
    if projection_path.exists() and (
        not projection_path.is_file() or _is_reparse_path(projection_path)
    ):
        raise ReviewBatchError("review batch projection path is invalid")
    current_payload: Any = None
    if projection_path.exists():
        try:
            current_payload = json.loads(projection_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            current_payload = None
    if current_payload != head_payload:
        atomic_write_json(str(projection_path), head_payload)

    try:
        persisted_payload = json.loads(projection_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewBatchError("review batch projection was not durable") from exc
    if (
        persisted_payload != head_payload
        or file_sha256(projection_path) != file_sha256(head_path)
    ):
        raise ReviewBatchError("review batch projection does not match committed head")
    return head


def _commit_review_batch_projection(
    *,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    batch_id: str,
    manifest_derivation_id: str,
    manifest_artifact_id: str,
    manifest_path: str | Path,
    manifest_payload: Mapping[str, Any],
    projection_generation: int,
) -> ProjectionReceiptStatus:
    candidate_identity = _validated_registered_projection_manifest(
        workspace=workspace,
        registry=registry,
        payload=manifest_payload,
    )
    if (
        candidate_identity[:3]
        != (batch_id, manifest_derivation_id, projection_generation)
        or _workspace_path_key(candidate_identity[3])
        != _workspace_path_key(manifest_path)
    ):
        raise ReviewBatchError("review batch projection candidate identity is inconsistent")

    head = _repair_review_batch_projection_from_registry(
        workspace=workspace,
        registry=registry,
    )
    if head is None:
        raise ReviewBatchError("review batch projection has no committed head")
    _head_payload, head_derivation_id, head_generation, head_path = head
    projection_status: ProjectionReceiptStatus = (
        "projected"
        if head_derivation_id == manifest_derivation_id
        else "superseded"
    )
    if projection_status == "superseded" and head_generation <= projection_generation:
        raise ReviewBatchError("review batch projection head is not monotonic")

    _write_review_batch_projection_receipt(
        workspace=workspace,
        batch_id=batch_id,
        manifest_derivation_id=manifest_derivation_id,
        manifest_artifact_id=manifest_artifact_id,
        manifest_path=manifest_path,
        projection_generation=projection_generation,
        projection_status=projection_status,
        head_derivation_id=head_derivation_id,
        head_projection_generation=head_generation,
        head_manifest_hash=file_sha256(head_path),
    )
    return projection_status


def derive_review_batch(
    spec: ReviewBatchSpecV1,
    *,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    derivation_id: str = "",
    producer: str = "services.review_batch.derive_review_batch",
) -> ReviewVariantDerivationResultV1 | ReviewBatchDerivationResultV1:
    """Derive one legacy variant or coordinate one exclusively owned batch."""

    spec.validate()
    if not spec.is_multi_variant:
        return _derive_review_batch_owned(
            spec,
            workspace=workspace,
            registry=registry,
            derivation_id=derivation_id,
            producer=producer,
        )

    if registry.job_id != workspace.job_id or _workspace_path_key(
        registry.registry_path
    ) != _workspace_path_key(workspace.paths.registry_path):
        raise SummarySelectionError(
            "review batch coordinator Registry does not belong to its workspace"
        )

    validate_review_batch_layout(spec, workspace=workspace)

    invocation_identity = derivation_id.strip() or uuid.uuid4().hex
    manifest_derivation_id = hashlib.sha256(
        f"{spec.batch_id}\0{workspace.job_id}\0{invocation_identity}".encode("utf-8")
    ).hexdigest()[:24]
    manifest_path = workspace.artifact_path(
        f"review_batch_manifests/{manifest_derivation_id}.json"
    )
    manifest_artifact_id = f"{spec.batch_id}:{manifest_derivation_id}"

    lease_path = workspace.artifact_path(
        f"review_batch_manifests/.{manifest_derivation_id}.execution.lock"
    )
    derivation_lease = AttemptExecutionLease(_DerivationLeaseTarget(lease_path))
    try:
        derivation_lease.acquire()
    except AttemptAlreadyRunningError as exc:
        raise ReviewBatchError(
            f"review batch derivation is already active: {manifest_derivation_id}"
        ) from exc
    coordinator_lease = AttemptExecutionLease(
        _DerivationLeaseTarget(
            workspace.artifact_path(
                "review_batch_manifests/.coordinator-projection.execution.lock"
            )
        )
    )
    try:
        coordinator_lease.acquire()
    except AttemptAlreadyRunningError as exc:
        derivation_lease.release()
        raise ReviewBatchError(
            "another review batch derivation is active for this coordinator"
        ) from exc
    try:
        projection_generation = _reserve_review_batch_projection_generation(
            workspace=workspace,
            batch_id=spec.batch_id,
            manifest_derivation_id=manifest_derivation_id,
        )
        registry.reload()
        existing_record = registry.get(manifest_artifact_id)
        if existing_record is not None:
            if not Path(manifest_path).is_file():
                raise ReviewBatchError(
                    "registered review batch derivation manifest is missing"
                )
            if _has_valid_review_batch_projection_receipt(
                workspace=workspace,
                batch_id=spec.batch_id,
                manifest_derivation_id=manifest_derivation_id,
                manifest_artifact_id=manifest_artifact_id,
                manifest_path=manifest_path,
                projection_generation=projection_generation,
            ):
                _repair_review_batch_projection_from_registry(
                    workspace=workspace,
                    registry=registry,
                )
                raise ReviewBatchError(
                    f"review batch derivation identity already exists: {manifest_derivation_id}"
                )
            return _recover_review_batch_manifest(
                spec,
                workspace=workspace,
                registry=registry,
                manifest_path=manifest_path,
                manifest_derivation_id=manifest_derivation_id,
                projection_generation=projection_generation,
                producer=producer,
                existing_record=existing_record,
            )
        receipt_path = _review_batch_projection_receipt_path(
            workspace=workspace,
            manifest_derivation_id=manifest_derivation_id,
        )
        if receipt_path.exists():
            if not Path(manifest_path).is_file():
                raise ReviewBatchError(
                    "review batch projection receipt exists without immutable manifest"
                )
            _has_valid_review_batch_projection_receipt(
                workspace=workspace,
                batch_id=spec.batch_id,
                manifest_derivation_id=manifest_derivation_id,
                manifest_artifact_id=manifest_artifact_id,
                manifest_path=manifest_path,
                projection_generation=projection_generation,
            )
            raise ReviewBatchError(
                "review batch projection receipt exists without Registry identity"
            )
        if Path(manifest_path).exists():
            return _recover_review_batch_manifest(
                spec,
                workspace=workspace,
                registry=registry,
                manifest_path=manifest_path,
                manifest_derivation_id=manifest_derivation_id,
                projection_generation=projection_generation,
                producer=producer,
            )
        return _derive_review_batch_owned(
            spec,
            workspace=workspace,
            registry=registry,
            derivation_id=invocation_identity,
            projection_generation=projection_generation,
            producer=producer,
        )
    finally:
        coordinator_lease.release()
        derivation_lease.release()


def load_review_batch_spec(path: str | Path) -> ReviewBatchSpecV1:
    target = Path(path).expanduser().resolve()
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise SummarySelectionError("review batch spec must be a JSON object")
    return ReviewBatchSpecV1.from_dict(payload, origin_dir=target.parent)

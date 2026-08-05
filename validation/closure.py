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

from services.artifact_registry import (
    ArtifactRecord,
    ArtifactRegistry,
    CurrentArtifactSetV1,
    RegistryError,
    file_sha256,
)
from services.job_workspace import JobWorkspace, atomic_write_json, utc_now_iso
from runtime.provider_runtime import hash_json
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
    current_set_id: str = ""
    stage_closure_map_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["blocking_issues"] = list(self.blocking_issues)
        payload["findings"] = list(self.findings)
        return payload

    @property
    def clean(self) -> bool:
        return self.status == "clean"

    @property
    def unvalidated(self) -> bool:
        return self.status == "not_requested"


@dataclass(frozen=True)
class _InputResolution:
    draft: ArtifactRecord | None
    manifest: ArtifactRecord | None
    validation: ArtifactRecord | None
    blocking: tuple[str, ...]


@dataclass(frozen=True)
class CurrentStageClosureMapV1:
    """Resolved stage closures for the one current artifact set."""

    job_id: str
    current_set_id: str
    stages: Mapping[str, Mapping[str, Any]]
    resolved_at: str
    map_hash: str
    blocking_issues: tuple[str, ...] = ()
    requested_stages: tuple[str, ...] = ()
    spec_hash: str = ""
    provider_closures_by_stage: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    current_set_required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": "current_stage_closure_map",
            "artifact_version": "v1",
            "job_id": self.job_id,
            "current_set_id": self.current_set_id,
            "stages": {str(key): dict(value) for key, value in self.stages.items()},
            "resolved_at": self.resolved_at,
            "map_hash": self.map_hash,
            "blocking_issues": list(self.blocking_issues),
            "requested_stages": list(self.requested_stages),
            "spec_hash": self.spec_hash,
            "provider_closures_by_stage": {
                str(key): dict(value) for key, value in self.provider_closures_by_stage.items()
            },
            "current_set_required": self.current_set_required,
        }


_PROVIDER_STAGE_NAMES = {
    "analyze": "stage1_analyze",
    "outline": "stage2_outline",
    "review": "stage3_review",
    "validate": "stage4_validate",
}
_PROVIDER_RECEIPT_STAGE_NAMES = {
    "analyze": "stage1_analyze",
    "outline": "outline_v3",
    "review": "stage3_review",
    "validate": "stage4_validate",
}
_ACTION_STAGE_DEFAULTS = {
    "analyze": ("analyze",),
    "derive_review_batch": ("derive_review_batch",),
    "generate_outline": ("outline",),
    "generate_review": ("outline", "review"),
    "generate_section": ("outline", "review"),
    "retry_failed": ("analyze",),
    "retry_review_failed": ("outline", "review"),
    "validate_review": ("validate",),
    # The production runner persists a typed stage plan.  This fallback is
    # retained only for legacy workspaces that predate that durable plan.
    "run_all": ("analyze", "outline", "review"),
}


def _durable_requested_stages(registry: ArtifactRegistry) -> tuple[tuple[str, ...], str, tuple[str, ...]]:
    """Read the immutable job spec before deriving any provider-stage scope."""

    spec_path, path_issues = _durable_runtime_spec_path(registry)
    if path_issues:
        return (), "", path_issues
    if not spec_path.is_file():
        return (), "", ()
    try:
        raw = json.loads(spec_path.read_text(encoding="utf-8"))
        spec_hash = _stable_hash(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return (), "", (f"runtime_job_spec_untrusted:{exc}",)
    if not isinstance(raw, Mapping):
        return (), spec_hash, ("runtime_job_spec_untrusted:root is not an object",)
    metadata = raw.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    requested = metadata.get("requested_stages")
    if requested is None:
        action = str(raw.get("action") or "run_all")
        requested = _ACTION_STAGE_DEFAULTS.get(action, ())
    if not isinstance(requested, (list, tuple)):
        return (), spec_hash, ("runtime_job_spec_untrusted:requested_stages is not an array",)
    normalized = tuple(
        dict.fromkeys(
            str(item).strip()
            for item in requested
            if str(item).strip() and str(item).strip() != "source_intake"
        )
    )
    return normalized, spec_hash, ()


def _durable_runtime_spec_path(
    registry: ArtifactRegistry,
) -> tuple[Path, tuple[str, ...]]:
    """Resolve the Registry-selected immutable runtime spec, with a legacy fallback."""

    try:
        record = registry.get("runtime_job_spec")
    except (OSError, RegistryError, TypeError, ValueError) as exc:
        return Path(registry.registry_path).parent / "artifacts" / "runtime_job_spec_v1.json", (
            f"runtime_job_spec_registry_untrusted:{exc}",
        )
    if record is not None:
        if record.status != "ready":
            return Path(record.path), ("runtime_job_spec_registry_not_ready",)
        spec_path = Path(record.path)
        try:
            ArtifactRegistry._verify_ready_artifact(record)
        except (OSError, RegistryError, TypeError, ValueError) as exc:
            return spec_path, (f"runtime_job_spec_registry_untrusted:{exc}",)
        return spec_path, ()

    workspace_root = Path(registry.registry_path).parent
    # Keep fixed-path lookup only as a read-only compatibility fallback for
    # pre-publication workspaces.  New writers register the versioned path.
    candidates = (
        workspace_root / "artifacts" / "runtime_job_spec_v1.json",
        workspace_root / "runtime_job_spec_v1.json",
    )
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[0]), ()


def _durable_current_set_required(
    registry: ArtifactRegistry,
    requested_stages: Sequence[str],
) -> bool:
    """Read the persisted stage-plan current-set gate without trusting a projection."""

    fallback = "validate" in requested_stages
    spec_path, path_issues = _durable_runtime_spec_path(registry)
    if path_issues:
        return fallback
    raw = _json_object(spec_path)
    metadata = raw.get("metadata") if isinstance(raw, Mapping) else None
    stage_plan = metadata.get("stage_plan") if isinstance(metadata, Mapping) else None
    if not isinstance(stage_plan, Mapping) or "current_artifact_set_required" not in stage_plan:
        return fallback
    return bool(stage_plan.get("current_artifact_set_required"))


def _payload_for_record(record: ArtifactRecord) -> tuple[Mapping[str, Any] | None, list[Mapping[str, Any]]]:
    """Load a JSON closure or JSONL provider ledger without trusting projections."""

    path = Path(record.path)
    if path.suffix.casefold() == ".jsonl":
        receipts: list[Mapping[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                value = json.loads(line)
                if isinstance(value, Mapping):
                    receipts.append(value)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None, []
        return None, receipts
    value = _json_object(path)
    if value is None:
        return None, []
    payload = value.get("payload")
    return (payload if isinstance(payload, Mapping) else value), []


def _provider_record_for_stage(
    stage: str,
    records: Sequence[ArtifactRecord],
    current_set: CurrentArtifactSetV1 | None,
) -> ArtifactRecord | None:
    preferred_ids: dict[str, tuple[str, ...]] = {
        "analyze": ("stage1:provider_receipt_closure",),
        "outline": ("outline-v3:provider_receipt_closure",),
        "review": ("review:provider_receipt_closure",),
        "validate": (
            current_set.validation_receipt_closure_artifact_id if current_set else "",
            "validation:provider_receipt_closure",
        ),
    }
    ready = [
        item for item in records
        if item.status == "ready"
        and item.artifact_type == "provider_receipt_closure"
    ]
    for artifact_id in preferred_ids.get(stage, ()):
        if not artifact_id:
            continue
        candidate = next((item for item in ready if item.artifact_id == artifact_id), None)
        if candidate is not None:
            return candidate
    expected_stage_name = _PROVIDER_STAGE_NAMES.get(stage, stage)
    expected_receipt_stage_name = _PROVIDER_RECEIPT_STAGE_NAMES.get(stage, expected_stage_name)
    candidates: list[ArtifactRecord] = []
    for item in ready:
        metadata_stage = str(item.metadata.get("stage_name") or "")
        if metadata_stage == expected_stage_name:
            candidates.append(item)
            continue
        payload, receipts = _payload_for_record(item)
        stage_names = {
            str(row.get("stage_name") or "")
            for row in receipts
            if str(row.get("stage_name") or "")
        }
        if payload and str(payload.get("stage_name") or "") == expected_stage_name:
            candidates.append(item)
        elif expected_stage_name in stage_names:
            candidates.append(item)
    return max(candidates, key=lambda item: (item.created_at, item.artifact_id), default=None)


def _terminal_for_stage(
    stage: str,
    records: Sequence[ArtifactRecord],
) -> tuple[ArtifactRecord | None, Mapping[str, Any] | None]:
    candidates: list[tuple[ArtifactRecord, Mapping[str, Any]]] = []
    expected_stage_name = _PROVIDER_STAGE_NAMES.get(stage, stage)
    accepted_stage_names = {stage, expected_stage_name}
    for record in records:
        if record.status != "ready" or record.artifact_type != "runtime_stage_terminal":
            continue
        payload = _json_object(record.path)
        if payload is not None and str(payload.get("stage_name") or "") in accepted_stage_names:
            candidates.append((record, payload))
    return max(candidates, key=lambda item: (item[0].created_at, item[0].artifact_id), default=(None, None))


def _provider_closure_entry(
    stage: str,
    record: ArtifactRecord | None,
    terminal_record: ArtifactRecord | None,
    terminal_payload: Mapping[str, Any] | None,
    registry: ArtifactRegistry,
) -> tuple[dict[str, Any], list[str]]:
    """Build a stage-indexed, hash-bound closure descriptor."""

    expected_stage_name = _PROVIDER_STAGE_NAMES.get(stage, stage)
    expected_receipt_stage_name = _PROVIDER_RECEIPT_STAGE_NAMES.get(stage, expected_stage_name)
    terminal_status = str((terminal_payload or {}).get("status") or "")
    terminal_model_calls = int((terminal_payload or {}).get("model_call_count") or 0)
    entry: dict[str, Any] = {
        "stage": stage,
        "stage_name": expected_stage_name,
        "requested": True,
        "provider_closure_required": terminal_model_calls > 0,
        "terminal_artifact_id": terminal_record.artifact_id if terminal_record else "",
        "terminal_artifact_hash": terminal_record.content_hash if terminal_record else "",
        "terminal_status": terminal_status,
        "model_call_count": terminal_model_calls,
        "status": "missing",
        "complete": False,
        "closure_epoch_id": "",
        "expected_call_ids": [],
        "observed_call_ids": [],
        "input_hashes": [],
        "config_hashes": [],
        "schema_hashes": [],
        "call_graph_hash": "",
        "artifact_id": "",
        "artifact_type": "",
        "artifact_version": "",
        "content_hash": "",
        "registry_dependencies": [],
        "registry_dependency_ids": [],
        "registry_dependency_hashes": [],
    }
    blocking: list[str] = []
    if terminal_record is None:
        blocking.append(f"requested_stage_terminal_missing:{stage}")
    elif terminal_status != "succeeded":
        blocking.append(f"requested_stage_terminal_not_succeeded:{stage}")
    if record is None:
        # A terminal call count/ledger row is not an expected-call contract.
        # Every requested provider stage must publish a closure artifact, even
        # when its expected set is empty.
        blocking.append(f"provider_closure_missing:{stage}")
        return entry, blocking

    entry.update(
        {
            "artifact_id": record.artifact_id,
            "artifact_type": record.artifact_type,
            "artifact_version": record.artifact_version,
            "content_hash": record.content_hash,
            "registry_dependencies": [dependency.to_dict() for dependency in record.depends_on],
            "registry_dependency_ids": [dependency.artifact_id for dependency in record.depends_on],
            "registry_dependency_hashes": [dependency.content_hash for dependency in record.depends_on],
        }
    )
    try:
        ArtifactRegistry._verify_ready_artifact(record)
        registry.verify_ready_dependencies(record.depends_on)
    except (OSError, RegistryError, ValueError, TypeError) as exc:
        blocking.append(f"provider_closure_untrusted:{stage}:{exc}")
    payload, receipt_rows = _payload_for_record(record)
    raw_envelope = _json_object(record.path)
    closure = dict(payload) if isinstance(payload, Mapping) else {}
    # Stage 1 keeps the closure result under ``payload`` and the predeclared
    # call graph in the outer envelope; review/validation embed the full
    # contract in the payload.  Normalize both durable layouts before any
    # identity or expected-call comparison so neither projection can weaken
    # the same closure contract.
    if isinstance(raw_envelope, Mapping):
        for field_name in (
            "job_id",
            "stage_name",
            "attempt_id",
            "logical_attempt_identity",
            "closure_epoch_id",
            "expected_call_graph_hash",
            "expected_calls",
        ):
            if field_name not in closure and field_name in raw_envelope:
                closure[field_name] = raw_envelope[field_name]
    # A closure artifact normally depends on a durable receipt ledger.  Copy
    # the stage-bound receipt facts into the stage descriptor so the map does
    # not reduce input/config/schema provenance to a closure hash alone.
    dependency_epoch = str(
        closure.get("closure_epoch_id")
        or (raw_envelope or {}).get("closure_epoch_id")
        or record.metadata.get("closure_epoch_id")
        or ""
    )
    dependency_receipts: list[Mapping[str, Any]] = []
    historical_dependency_receipts: list[Mapping[str, Any]] = []
    for dependency in record.depends_on:
        dependency_record = registry.get(dependency.artifact_id)
        if dependency_record is None or dependency_record.status != "ready":
            continue
        _dependency_payload, dependency_rows = _payload_for_record(dependency_record)
        for row in dependency_rows:
            if row.get("stage_name") and str(row.get("stage_name")) != expected_receipt_stage_name:
                continue
            row_epoch = str(row.get("closure_epoch_id") or "")
            if row_epoch and row_epoch != dependency_epoch:
                historical_dependency_receipts.append(row)
            else:
                dependency_receipts.append(row)
    if dependency_receipts:
        receipt_rows = dependency_receipts
    if historical_dependency_receipts:
        blocking.append(f"provider_closure_historical_receipts:{stage}")
    if raw_envelope is None:
        blocking.append(f"provider_closure_envelope_unreadable:{stage}")
    outer_job_id = str((raw_envelope or {}).get("job_id") or "")
    if outer_job_id != registry.job_id:
        blocking.append(f"provider_closure_outer_job_mismatch:{stage}")
    closure_job_id = str(closure.get("job_id") or "")
    if closure_job_id != registry.job_id:
        blocking.append(f"provider_closure_job_mismatch:{stage}")
    closure_stage_name = str(
        closure.get("stage_name")
        or (raw_envelope or {}).get("stage_name")
        or record.metadata.get("stage_name")
        or ""
    )
    if closure_stage_name != expected_stage_name:
        blocking.append(f"provider_closure_stage_name_mismatch:{stage}")
    closure_epoch_id = str(
        closure.get("closure_epoch_id")
        or (raw_envelope or {}).get("closure_epoch_id")
        or record.metadata.get("closure_epoch_id")
        or ""
    )
    if not closure_epoch_id:
        blocking.append(f"provider_closure_epoch_missing:{stage}")
    expected_graph_hash = str(
        closure.get("expected_call_graph_hash")
        or (raw_envelope or {}).get("expected_call_graph_hash")
        or record.metadata.get("expected_call_graph_hash")
        or ""
    )
    if not expected_graph_hash:
        blocking.append(f"provider_closure_expected_graph_missing:{stage}")
    raw_expected_calls = closure.get("expected_calls")
    expected_call_payloads = (
        [item for item in raw_expected_calls if isinstance(item, Mapping)]
        if isinstance(raw_expected_calls, list)
        else []
    )
    if not isinstance(raw_expected_calls, list) or len(expected_call_payloads) != len(raw_expected_calls):
        blocking.append(f"provider_closure_expected_calls_missing:{stage}")
    expected_payload_ids = [str(item.get("call_id") or "") for item in expected_call_payloads]
    expected_call_count = len(expected_call_payloads)
    if any(not item for item in expected_payload_ids) or len(set(expected_payload_ids)) != len(expected_payload_ids):
        blocking.append(f"provider_closure_expected_call_identity_invalid:{stage}")
    expected_graph_hashes = {
        str(item.get("expected_call_graph_hash") or "")
        for item in expected_call_payloads
        if str(item.get("expected_call_graph_hash") or "")
    }
    if expected_graph_hash and expected_graph_hashes and expected_graph_hashes != {expected_graph_hash}:
        blocking.append(f"provider_closure_expected_graph_mismatch:{stage}")
    expected_attempt_ids = {
        str(item.get("attempt_id") or "")
        for item in expected_call_payloads
        if str(item.get("attempt_id") or "")
    }
    expected_logical_attempts = {
        str(item.get("logical_attempt_identity") or "")
        for item in expected_call_payloads
        if str(item.get("logical_attempt_identity") or "")
    }
    closure_attempt_id = str(closure.get("attempt_id") or "")
    closure_logical_attempt = str(closure.get("logical_attempt_identity") or "")
    # ``attempt_id`` is call-scoped for Outline v3 and review DAG entries,
    # while the closure envelope binds the stage-level attempt.  The shared
    # logical attempt identity is the durable stage binding in that layout;
    # Stage 1 may use the same value for both fields.  Keep the per-receipt
    # call-level attempt checks below so accepting the stage binding cannot
    # weaken call identity validation.
    if expected_call_count and closure_attempt_id not in (
        expected_attempt_ids | expected_logical_attempts
    ):
        blocking.append(f"provider_closure_attempt_binding_mismatch:{stage}")
    if expected_call_count and closure_logical_attempt not in expected_logical_attempts:
        blocking.append(f"provider_closure_logical_attempt_binding_mismatch:{stage}")
    dependency_records = [
        dependency_record
        for dependency in record.depends_on
        if (dependency_record := registry.get(dependency.artifact_id)) is not None
        and dependency_record.status == "ready"
    ]
    # Stage 1 publishes a separate, typed expected-call graph because its
    # closure is assembled from paper-level reuse/generation work.  Outline,
    # review, and validation persist their expected calls in their own
    # stage-specific call-plan/closure contracts and do not publish this
    # Stage-1 artifact type.  Keep the common hash and expected-call checks
    # below for every stage, but require the separate Registry graph only for
    # the stage that owns that contract.
    if stage == "analyze":
        expected_graph_records = [
            dependency_record
            for dependency_record in dependency_records
            if dependency_record.artifact_type == "provider_expected_call_graph"
            and dependency_record.artifact_version == "v1"
        ]
        if not expected_graph_records:
            blocking.append(f"provider_closure_expected_graph_dependency_missing:{stage}")
        else:
            for graph_record in expected_graph_records:
                graph_payload = _json_object(graph_record.path)
                if str(graph_record.metadata.get("expected_call_graph_hash") or "") != expected_graph_hash:
                    blocking.append(f"provider_closure_expected_graph_dependency_hash_mismatch:{stage}")
                if str((graph_payload or {}).get("expected_call_graph_hash") or "") != expected_graph_hash:
                    blocking.append(f"provider_closure_expected_graph_file_hash_mismatch:{stage}")
                source_bundle_record = next(
                    (
                        dependency_record
                        for dependency_record in dependency_records
                        if dependency_record.artifact_type == "source_bundle"
                        and dependency_record.artifact_version == "v1"
                    ),
                    None,
                )
                runtime_spec_record = next(
                    (
                        dependency_record
                        for dependency_record in dependency_records
                        if dependency_record.artifact_type == "runtime_job_spec"
                        and dependency_record.artifact_version == "v1"
                    ),
                    None,
                )
                if source_bundle_record is None:
                    blocking.append(f"provider_closure_source_bundle_dependency_missing:{stage}")
                elif str((graph_payload or {}).get("source_bundle_hash") or "") != source_bundle_record.content_hash:
                    blocking.append(f"provider_closure_expected_graph_source_bundle_mismatch:{stage}")
                if runtime_spec_record is None:
                    blocking.append(f"provider_closure_runtime_spec_dependency_missing:{stage}")
                elif str((graph_payload or {}).get("runtime_spec_hash") or "") != runtime_spec_record.content_hash:
                    blocking.append(f"provider_closure_expected_graph_runtime_spec_mismatch:{stage}")
    dependency_paths = {
        str(Path(dependency_record.path).resolve()): dependency_record
        for dependency_record in dependency_records
    }
    for expected_call in expected_call_payloads:
        call_id = str(expected_call.get("call_id") or "") or "unknown"
        identity_fields = {
            "job_id": (str(expected_call.get("job_id") or ""), registry.job_id),
            "stage_name": (str(expected_call.get("stage_name") or ""), expected_receipt_stage_name),
            "attempt_id": (str(expected_call.get("attempt_id") or ""), "<non-empty>"),
            "node_id": (str(expected_call.get("node_id") or ""), "<non-empty>"),
            "logical_attempt_identity": (
                str(expected_call.get("logical_attempt_identity") or ""),
                "<non-empty>",
            ),
            "closure_epoch_id": (str(expected_call.get("closure_epoch_id") or ""), closure_epoch_id),
            "expected_call_graph_hash": (
                str(expected_call.get("expected_call_graph_hash") or ""),
                expected_graph_hash,
            ),
        }
        for field_name, (actual, required) in identity_fields.items():
            if (required == "<non-empty>" and not actual) or (
                required != "<non-empty>" and actual != required
            ):
                blocking.append(
                    f"provider_closure_expected_call_identity_mismatch:{stage}:{call_id}:{field_name}"
                )
        artifact_path = str(expected_call.get("artifact_path") or "").strip()
        registry_file_hash = str(expected_call.get("registry_file_hash") or "").strip()
        if not artifact_path:
            blocking.append(f"provider_closure_expected_artifact_path_missing:{stage}:{call_id}")
            continue
        if not registry_file_hash:
            blocking.append(f"provider_closure_expected_registry_hash_missing:{stage}:{call_id}")
            continue
        resolved_path = str(Path(artifact_path).resolve())
        dependency_record = dependency_paths.get(resolved_path)
        if dependency_record is None:
            blocking.append(
                f"provider_closure_expected_artifact_dependency_missing:{stage}:{call_id}"
            )
        elif dependency_record.content_hash != registry_file_hash:
            blocking.append(
                f"provider_closure_expected_artifact_dependency_hash_mismatch:{stage}:{call_id}"
            )
        elif stage == "analyze":
            try:
                registry.verify_ready_dependencies(dependency_record.depends_on)
            except (OSError, RegistryError, ValueError, TypeError) as exc:
                blocking.append(
                    f"provider_closure_expected_artifact_dependency_untrusted:{stage}:{call_id}:{exc}"
                )
        try:
            if file_sha256(artifact_path) != registry_file_hash:
                blocking.append(f"provider_closure_expected_file_hash_mismatch:{stage}:{call_id}")
        except (OSError, TypeError, ValueError):
            blocking.append(f"provider_closure_expected_artifact_unreadable:{stage}:{call_id}")
    expected_ids = [str(item) for item in closure.get("expected_call_ids") or () if str(item)]
    observed_ids = [str(item) for item in closure.get("observed_call_ids") or () if str(item)]
    # Never derive the expected set from the receipt ledger.  The closure
    # payload must carry the predeclared call graph explicitly.
    entry["closure_epoch_id"] = str(closure.get("closure_epoch_id") or record.metadata.get("closure_epoch_id") or "")
    entry["expected_call_ids"] = sorted(set(expected_ids))
    entry["observed_call_ids"] = sorted(set(observed_ids))
    entry["input_hashes"] = sorted({str(item.get("input_hash") or "") for item in receipt_rows if str(item.get("input_hash") or "")})
    entry["config_hashes"] = sorted({str(item.get("config_hash") or "") for item in receipt_rows if str(item.get("config_hash") or "")})
    entry["schema_hashes"] = sorted({str(item.get("schema_hash") or "") for item in receipt_rows if str(item.get("schema_hash") or "")})
    entry["call_graph_hash"] = expected_graph_hash
    if set(expected_ids) != set(expected_payload_ids):
        blocking.append(f"provider_closure_expected_call_set_mismatch:{stage}")
    if set(observed_ids) != set(expected_ids):
        blocking.append(f"provider_closure_observed_call_set_mismatch:{stage}")
    if entry["closure_epoch_id"] != closure_epoch_id:
        blocking.append(f"provider_closure_epoch_mismatch:{stage}")
    ledger_records = [
        dependency_record
        for dependency_record in dependency_records
        if dependency_record.artifact_type == "provider_receipt_ledger"
    ]
    if expected_call_count > 0:
        if not ledger_records:
            blocking.append(f"provider_closure_receipt_ledger_dependency_missing:{stage}")
        else:
            for ledger_record in ledger_records:
                try:
                    ArtifactRegistry._verify_ready_artifact(ledger_record)
                except (OSError, RegistryError, ValueError, TypeError) as exc:
                    blocking.append(f"provider_closure_receipt_ledger_untrusted:{stage}:{exc}")
    else:
        if terminal_model_calls != 0:
            blocking.append(f"provider_closure_zero_call_terminal_model_count:{stage}")
        if observed_ids or receipt_rows:
            blocking.append(f"provider_closure_zero_call_observed_receipts:{stage}")
        if ledger_records:
            blocking.append(f"provider_closure_zero_call_receipt_ledger_present:{stage}")
        source_evidence_records = [
            dependency_record
            for dependency_record in dependency_records
            if dependency_record.artifact_type in {
                "summary_source_manifest",
                "stage1_summary_reuse_record",
            }
        ]
        if not source_evidence_records:
            blocking.append(f"provider_closure_zero_call_source_evidence_missing:{stage}")
    reuse_records = [
        dependency_record
        for dependency_record in dependency_records
        if dependency_record.artifact_type == "stage1_summary_reuse_record"
        and dependency_record.artifact_version == "v1"
    ]
    if stage == "analyze" and (reuse_records or expected_call_count == 0):
        source_record = next(
            (
                dependency_record
                for dependency_record in dependency_records
                if dependency_record.artifact_type == "source_bundle"
                and dependency_record.artifact_version == "v1"
            ),
            None,
        )
        source_payload = _json_object(source_record.path) if source_record is not None else None
        source_items_value = (
            (source_payload or {}).get("paper_work_items")
            if isinstance(source_payload, Mapping)
            else []
        )
        source_items = source_items_value if isinstance(source_items_value, list) else []
        expected_papers = {
            str(item.get("canonical_paper_key") or "")
            for item in source_items
            if isinstance(item, Mapping) and str(item.get("canonical_paper_key") or "")
        }
        generated_papers = {
            str(item.get("node_id") or "")
            for item in expected_call_payloads
            if str(item.get("node_id") or "")
        }
        expected_reused_papers = expected_papers - generated_papers
        reused_papers: list[str] = []
        for reuse_record in reuse_records:
            reuse_payload = _json_object(reuse_record.path) or {}
            identity = reuse_payload.get("source_bundle_paper_identity")
            paper_key = str(identity.get("canonical_paper_key") or "") if isinstance(identity, Mapping) else ""
            if not paper_key or paper_key in reused_papers or (expected_papers and paper_key not in expected_papers):
                blocking.append(f"provider_closure_reuse_identity_invalid:{stage}")
            else:
                reused_papers.append(paper_key)
            try:
                registry.verify_ready_dependencies(reuse_record.depends_on)
            except (OSError, RegistryError, ValueError, TypeError) as exc:
                blocking.append(f"provider_closure_reuse_dependency_invalid:{stage}")
            dependency_by_id = {
                dependency.artifact_id: dependency
                for dependency in reuse_record.depends_on
                if dependency.artifact_id
            }
            source_id = str(reuse_payload.get("registered_source_artifact_id") or "")
            source_hash = str(reuse_payload.get("registered_source_artifact_hash") or "")
            manifest_id = str(reuse_payload.get("source_summary_manifest_id") or "")
            manifest_hash = str(reuse_payload.get("source_summary_manifest_hash") or "")
            evidence_id = str(reuse_payload.get("current_evidence_manifest_id") or "")
            evidence_hash = str(reuse_payload.get("current_evidence_manifest_hash") or "")
            runtime_id = str(reuse_payload.get("current_runtime_spec_id") or "")
            runtime_hash = str(reuse_payload.get("current_runtime_spec_hash") or "")
            for label, artifact_id, content_hash in (
                ("source", source_id, source_hash),
                ("source_manifest", manifest_id, manifest_hash),
                ("evidence_manifest", evidence_id, evidence_hash),
                ("runtime_spec", runtime_id, runtime_hash),
                (
                    "source_provider_receipt_closure",
                    str(reuse_payload.get("source_provider_receipt_closure_id") or ""),
                    str(reuse_payload.get("source_provider_receipt_closure_hash") or ""),
                ),
                (
                    "source_provider_receipt_ledger",
                    str(reuse_payload.get("source_provider_receipt_ledger_id") or ""),
                    str(reuse_payload.get("source_provider_receipt_ledger_hash") or ""),
                ),
            ):
                if not artifact_id and not content_hash:
                    continue
                dependency = dependency_by_id.get(artifact_id)
                if dependency is None or dependency.content_hash != content_hash:
                    blocking.append(f"provider_closure_reuse_{label}_binding_invalid:{stage}")
                else:
                    dependency_record = registry.get(artifact_id)
                    if dependency_record is None or dependency_record.content_hash != content_hash:
                        blocking.append(f"provider_closure_reuse_{label}_record_invalid:{stage}")
                    elif label == "source_provider_receipt_closure" and (
                        dependency_record.artifact_type != "provider_receipt_closure"
                        or dependency_record.artifact_version != "v1"
                    ):
                        blocking.append(f"provider_closure_reuse_{label}_type_invalid:{stage}")
                    elif label == "source_provider_receipt_ledger" and (
                        dependency_record.artifact_type != "provider_receipt_ledger"
                        or dependency_record.artifact_version != "v1"
                    ):
                        blocking.append(f"provider_closure_reuse_{label}_type_invalid:{stage}")
            source_record = registry.get(source_id) if source_id else None
            if source_record is None or source_record.status != "ready":
                blocking.append(f"provider_closure_reuse_source_artifact_missing:{stage}")
            else:
                try:
                    ArtifactRegistry._verify_ready_artifact(source_record)
                    source_payload = json.loads(Path(source_record.path).read_text(encoding="utf-8"))
                    source_payload_items = (
                        source_payload
                        if isinstance(source_payload, list)
                        else [source_payload]
                        if isinstance(source_payload, Mapping)
                        else []
                    )
                    found_payload = next(
                        (
                            item
                            for item in source_payload_items
                            if isinstance(item, Mapping)
                            and isinstance(item.get("ai_summary"), Mapping)
                            and str(
                                (item.get("paper_info") or {}).get("canonical_paper_key")
                                if isinstance(item.get("paper_info"), Mapping)
                                else ""
                            ) == paper_key
                        ),
                        None,
                    )
                    summary_payload_hash = str(reuse_payload.get("summary_payload_hash") or "")
                    if found_payload is None or hash_json(found_payload.get("ai_summary")) != summary_payload_hash:
                        blocking.append(f"provider_closure_reuse_source_payload_mismatch:{stage}")
                except (OSError, RegistryError, ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
                    blocking.append(f"provider_closure_reuse_source_artifact_untrusted:{stage}:{exc}")
        if expected_reused_papers and set(reused_papers) != expected_reused_papers:
            blocking.append(f"provider_closure_reuse_identity_coverage_incomplete:{stage}")
        if not expected_reused_papers and reused_papers:
            blocking.append(f"provider_closure_reuse_identity_unexpected:{stage}")
    terminal_output_ids = {
        str(item.get("artifact_id") or "")
        for item in (terminal_payload or {}).get("output_artifact_refs") or ()
        if isinstance(item, Mapping) and str(item.get("artifact_id") or "")
    }
    if record.artifact_id not in terminal_output_ids and not any(
        str(item.get("content_hash") or "") == record.content_hash
        for item in (terminal_payload or {}).get("output_artifact_refs") or ()
        if isinstance(item, Mapping)
    ):
        blocking.append(f"provider_closure_terminal_dependency_missing:{stage}")
    expected_by_id = {
        str(item.get("call_id") or ""): item
        for item in expected_call_payloads
        if str(item.get("call_id") or "")
    }
    receipt_row_ids = {str(row.get("call_id") or "") for row in receipt_rows if str(row.get("call_id") or "")}
    if receipt_rows and receipt_row_ids != set(observed_ids):
        blocking.append(f"provider_closure_receipt_set_mismatch:{stage}")
    for row in receipt_rows:
        row_stage = str(row.get("stage_name") or "")
        row_job = str(row.get("job_id") or "")
        row_epoch = str(row.get("closure_epoch_id") or "")
        if row_job != registry.job_id or row_stage != expected_receipt_stage_name or row_epoch != closure_epoch_id:
            blocking.append(f"provider_closure_receipt_identity_mismatch:{stage}")
        expected_row = expected_by_id.get(str(row.get("call_id") or ""))
        if expected_row is None:
            continue
        for field_name in (
            "attempt_id",
            "node_id",
            "logical_attempt_identity",
            "prompt_hash",
            "input_hash",
            "config_hash",
            "schema_hash",
        ):
            expected_value = str(expected_row.get(field_name) or "")
            if not expected_value or str(row.get(field_name) or "") != expected_value:
                blocking.append(f"provider_closure_receipt_binding_mismatch:{stage}:{field_name}")
    if not str(closure.get("closure_hash") or ""):
        blocking.append(f"provider_closure_hash_missing:{stage}")
    complete = bool(closure.get("complete")) if payload is not None else False
    entry["complete"] = complete
    entry["status"] = "complete" if complete else "blocked"
    if not complete:
        blocking.append(f"provider_closure_incomplete:{stage}")
    for key in ("missing_call_ids", "stale_call_ids", "failed_call_ids", "incomplete_call_ids", "hash_mismatches", "unexpected_receipts", "retry_exceeded_call_ids", "usage_incomplete_call_ids"):
        value = closure.get(key)
        if value:
            blocking.append(f"provider_closure_{key}:{stage}")
    observed_stage_names = {
        str(item.get("stage_name") or "") for item in receipt_rows if str(item.get("stage_name") or "")
    }
    if observed_stage_names and observed_stage_names != {expected_receipt_stage_name}:
        blocking.append(f"provider_closure_stage_mismatch:{stage}")
    return entry, blocking


def resolve_current_stage_closure_map(
    registry: ArtifactRegistry,
    *,
    job_id: str | None = None,
) -> CurrentStageClosureMapV1:
    """Resolve exact current stage artifacts through the atomic set pointer."""

    resolved_job_id = str(job_id or registry.job_id)
    blocking: list[str] = []
    current_set: CurrentArtifactSetV1 | None = None
    try:
        current_set = registry.resolve_current_artifact_set()
    except (OSError, RegistryError, ValueError, TypeError) as exc:
        blocking.append(f"current_artifact_set_untrusted:{exc}")
    stages: dict[str, Mapping[str, Any]] = {}
    provider_closures: dict[str, Mapping[str, Any]] = {}
    requested_stages, spec_hash, spec_blocking = _durable_requested_stages(registry)
    blocking.extend(spec_blocking)
    current_set_required = _durable_current_set_required(registry, requested_stages)
    if current_set is None and not blocking and current_set_required:
        blocking.append("current_artifact_set_missing")
    records = registry.list_records()
    if current_set is not None:
        targets = (
            ("review", current_set.review_draft_artifact_id, current_set.review_draft_artifact_hash),
            ("citation_manifest", current_set.citation_manifest_artifact_id, current_set.citation_manifest_artifact_hash),
            ("review_docx", current_set.review_docx_artifact_id, current_set.review_docx_artifact_hash),
            (
                "validation",
                current_set.validation_disposition_artifact_id
                if current_set.validation_status == "not_requested"
                else current_set.validation_run_result_artifact_id,
                current_set.validation_disposition_artifact_hash
                if current_set.validation_status == "not_requested"
                else current_set.validation_run_result_artifact_hash,
            ),
            ("validation_receipt_closure", current_set.validation_receipt_closure_artifact_id, current_set.validation_receipt_closure_artifact_hash),
        )
        for stage, artifact_id, expected_hash in targets:
            record = registry.get(artifact_id)
            if record is None or record.status != "ready":
                blocking.append(f"current_stage_artifact_missing:{stage}")
                continue
            if record.content_hash != expected_hash:
                blocking.append(f"current_stage_artifact_hash_mismatch:{stage}")
                continue
            stages[stage] = _record_payload(record) or {}
    logical_stages = tuple(stage for stage in requested_stages if stage in _PROVIDER_STAGE_NAMES)
    for logical_stage in logical_stages:
        terminal_record, terminal_payload = _terminal_for_stage(logical_stage, records)
        provider_record = _provider_record_for_stage(logical_stage, records, current_set)
        entry, entry_blocking = _provider_closure_entry(
            logical_stage,
            provider_record,
            terminal_record,
            terminal_payload,
            registry,
        )
        provider_closures[logical_stage] = entry
        blocking.extend(entry_blocking)
    payload = {
        "job_id": resolved_job_id,
        "current_set_id": current_set.set_id if current_set else "",
        "stages": stages,
        "requested_stages": list(requested_stages),
        "spec_hash": spec_hash,
        "provider_closures_by_stage": provider_closures,
        "blocking_issues": sorted(set(blocking)),
    }
    return CurrentStageClosureMapV1(
        job_id=resolved_job_id,
        current_set_id=payload["current_set_id"],
        stages=stages,
        resolved_at=utc_now_iso(),
        map_hash=_stable_hash(payload),
        blocking_issues=tuple(sorted(set(blocking))),
        requested_stages=requested_stages,
        spec_hash=spec_hash,
        provider_closures_by_stage=provider_closures,
        current_set_required=current_set_required,
    )


class ValidationClosureService:
    """Read-only canonical draft/manifest/validation closure service."""

    def __init__(self, workspace: JobWorkspace, registry: ArtifactRegistry) -> None:
        self.workspace = workspace
        self.registry = registry

    def _resolve_inputs(self) -> _InputResolution:
        records = self.registry.list_records()
        blocking: list[str] = []
        try:
            current_set = self.registry.resolve_current_artifact_set()
        except (OSError, RegistryError, ValueError, TypeError) as exc:
            if self.registry.current_artifact_set_pointer() is not None:
                return _InputResolution(None, None, None, (f"current_artifact_set_untrusted:{exc}",))
            current_set = None
        if current_set is not None:
            draft = self.registry.get(current_set.review_draft_artifact_id)
            manifest = self.registry.get(current_set.citation_manifest_artifact_id)
            validation = (
                self.registry.get(current_set.validation_disposition_artifact_id)
                if current_set.validation_status == "not_requested"
                else self.registry.get(current_set.validation_run_result_artifact_id)
            )
            if draft is None:
                blocking.append("current_set_review_draft_missing")
            if manifest is None:
                blocking.append("current_set_citation_manifest_missing")
            if validation is None:
                blocking.append(
                    "current_set_validation_disposition_missing"
                    if current_set.validation_status == "not_requested"
                    else "current_set_validation_missing"
                )
            elif current_set.validation_status == "not_requested" and validation.artifact_type != "validation_disposition":
                blocking.append("current_set_validation_disposition_type_mismatch")
            return _InputResolution(draft, manifest, validation, tuple(blocking))
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
        if resolution.validation is not None and resolution.validation.artifact_type == "validation_disposition":
            from validation.disposition import ValidationDispositionV1

            try:
                disposition = ValidationDispositionV1.from_dict(validation_payload or {})
            except (TypeError, ValueError, KeyError) as exc:
                blocking.append(f"validation_disposition_contract_invalid:{exc}")
            else:
                semantic_status = "not_requested"
                semantic.update(
                    {
                        "status": disposition.status,
                        "validation_disposition": disposition.status,
                        "contract_satisfied": True,
                        "validation_enabled": disposition.validation_enabled,
                        "validation_required": disposition.validation_required,
                        "allow_unvalidated": disposition.allow_unvalidated,
                        "actor": disposition.actor,
                        "reason": disposition.reason,
                        "stage_plan_hash": disposition.stage_plan_hash,
                        "spec_hash": disposition.spec_hash,
                        "disposition_hash": disposition.disposition_hash,
                    }
                )
                exact_inputs = (
                    ("review_draft", disposition.review_draft_artifact_id, disposition.review_draft_artifact_hash, resolution.draft),
                    ("citation_manifest", disposition.citation_manifest_artifact_id, disposition.citation_manifest_artifact_hash, resolution.manifest),
                    ("review_docx", disposition.review_docx_artifact_id, disposition.review_docx_artifact_hash, self.registry.get(disposition.review_docx_artifact_id)),
                )
                for label, artifact_id, content_hash, record in exact_inputs:
                    if record is None or record.artifact_id != artifact_id or record.content_hash != content_hash:
                        blocking.append(f"validation_disposition_{label}_input_stale")
                runtime_spec = self.registry.get("runtime_job_spec")
                if runtime_spec is None or runtime_spec.content_hash != disposition.spec_hash:
                    blocking.append("validation_disposition_spec_stale")
        elif validation_payload is not None:
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
        elif semantic_status == "not_requested":
            status = "not_requested"
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
        pointer = self.registry.current_artifact_set_pointer()
        try:
            stage_map = resolve_current_stage_closure_map(self.registry)
        except (OSError, RegistryError, ValueError, TypeError) as exc:
            stage_map = CurrentStageClosureMapV1(
                job_id=self.workspace.job_id,
                current_set_id="",
                stages={},
                resolved_at=utc_now_iso(),
                map_hash=_stable_hash({"error": str(exc)}),
                blocking_issues=(f"current_stage_closure_map_untrusted:{exc}",),
            )
        for issue in stage_map.blocking_issues:
            if issue not in blocking:
                blocking.append(issue)
        if stage_map.blocking_issues:
            status = "blocked"
        evidence_payload["status"] = status
        evidence_payload["blocking_issues"] = sorted(set(blocking))
        evidence_payload["stage_closure_map_hash"] = stage_map.map_hash
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
            current_set_id=str((pointer.metadata if pointer else {}).get("current_set_id") or ""),
            stage_closure_map_hash=stage_map.map_hash,
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
    "CurrentStageClosureMapV1",
    "ValidationClosureService",
    "persist_validation_closure",
    "resolve_current_stage_closure_map",
]

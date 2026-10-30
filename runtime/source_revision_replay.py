"""Plan-bound, read-only parent response lookup for SourceRevision children.

This module does not create provider receipts, invoke transports, reserve
provider budget, or mutate a parent job. Stage executors supply their current
request and v2 source-scope metadata, validate the returned response with
their existing parser, publish a child-owned output artifact, then use this
module to publish and verify a child-owned reuse proof.

The first version accepts only direct successful calls owned by the
designated parent. A parent call already marked verified reuse is an explicit
reconciliation blocker until its ancestor owner chain is supported.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from runtime.provider_receipt_closure import ExpectedProviderCall
from runtime.provider_response_snapshot import (
    ProviderResponseSnapshotError,
    ProviderResponseSnapshotStore,
)
from runtime.provider_runtime import ProviderCallReceiptV1, hash_json
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
)
from services.job_workspace import JobWorkspace, publish_json_artifact

SOURCE_REVISION_PLAN_ARTIFACT_ID = "source_revision_plan"
SOURCE_REVISION_PLAN_ARTIFACT_TYPE = "source_revision_plan"
SOURCE_REVISION_PLAN_ARTIFACT_VERSION = "v1"
SOURCE_REVISION_REUSE_ARTIFACT_TYPE = "source_revision_provider_reuse"
SOURCE_REVISION_REUSE_ARTIFACT_VERSION = "v1"
SOURCE_REVISION_REPLAY_SCOPE_VERSION = "source-revision-replay-scope/v1"
_SOURCE_REVISION_CLOSURE_STAGE_BY_CALL = {
    "outline_v3": "stage2_outline",
    "stage3_review": "stage3_review",
}
_SCOPE_FIELDS = frozenset({
    "schema_version",
    "canonical_paper_keys",
    "selected_source_content_hash",
    "non_source_scope_hash",
})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class SourceRevisionReplayError(RuntimeError):
    """A plan-bound provider response cannot be safely reused."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = str(code)
        detail = str(message or "").strip()
        super().__init__(f"{self.code}: {detail}" if detail else self.code)


@dataclass(frozen=True)
class SourceRevisionReplayMatch:
    """Detached parent response and verified source-owner records."""

    stage_name: str
    call_id: str
    request_hash: str
    route_identity: Mapping[str, str]
    scope: Mapping[str, Any]
    receipt: ProviderCallReceiptV1
    response: Any
    parent_closure: ArtifactRecord
    parent_ledger: ArtifactRecord
    response_snapshot: ArtifactRecord
    accepted_output: ArtifactRecord
    parent_scope_record: ArtifactRecord
    parent_lineage_records: tuple[ArtifactRecord, ...]
    parent_expected_call: Mapping[str, Any]
    _context_token: object


@dataclass(frozen=True)
class SourceRevisionReplayLookup:
    status: Literal["match", "no_match"]
    match: SourceRevisionReplayMatch | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status == "match" and self.match is None:
            raise ValueError("matched SourceRevision lookup requires a match")
        if self.status == "no_match" and self.match is not None:
            raise ValueError("no-match SourceRevision lookup cannot carry a match")


def _digest(value: Any, label: str) -> str:
    result = str(value or "")
    if _DIGEST.fullmatch(result) is None:
        raise SourceRevisionReplayError("invalid_digest", f"{label} must be a lowercase SHA-256")
    return result


def _absolute(value: Any, label: str) -> Path:
    path = Path(str(value or ""))
    if not path.is_absolute() or ".." in path.parts:
        raise SourceRevisionReplayError("invalid_path", f"{label} must be absolute and normalized")
    return path


def _decode_json(raw: bytes, label: str) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SourceRevisionReplayError("duplicate_json_key", f"{label} contains duplicate keys")
            result[key] = value
        return result

    try:
        return json.loads(raw, object_pairs_hook=unique)
    except SourceRevisionReplayError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise SourceRevisionReplayError("invalid_json", f"{label} is unreadable JSON") from exc


def _record_json(registry: ArtifactRegistry, record: ArtifactRecord) -> Any:
    """Read a record from one byte buffer tied to its exact Registry row."""
    if record.job_id != registry.job_id or record.status != "ready":
        raise SourceRevisionReplayError("foreign_or_unready_artifact", record.artifact_id)
    try:
        raw = Path(record.path).read_bytes()
    except OSError as exc:
        raise SourceRevisionReplayError("artifact_unreadable", record.artifact_id) from exc
    if hashlib.sha256(raw).hexdigest() != record.content_hash:
        raise SourceRevisionReplayError("artifact_hash_mismatch", record.artifact_id)
    payload = _decode_json(raw, record.artifact_id)
    if registry.get(record.artifact_id) != record:
        raise SourceRevisionReplayError("artifact_registry_changed", record.artifact_id)
    return payload


def _artifact_payload(document: Any) -> Mapping[str, Any]:
    if not isinstance(document, Mapping):
        raise SourceRevisionReplayError("artifact_payload_invalid")
    payload = document.get("payload")
    return payload if isinstance(payload, Mapping) else document


def _record_ref(record: ArtifactRecord, *, external: bool = False) -> dict[str, str]:
    return {
        "dependency_kind": "external_job" if external else "local_job",
        "job_id": record.job_id,
        "artifact_id": record.artifact_id,
        "artifact_type": record.artifact_type,
        "path": record.path,
        "content_hash": record.content_hash,
    }


def _same_path(left: str | Path, right: str | Path) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _path_within(path: str | Path, root: str | Path) -> bool:
    try:
        Path(path).resolve(strict=True).relative_to(Path(root).resolve(strict=True))
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _expected_call(value: ExpectedProviderCall | Mapping[str, Any]) -> ExpectedProviderCall:
    try:
        return value if isinstance(value, ExpectedProviderCall) else ExpectedProviderCall.from_mapping(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SourceRevisionReplayError("expected_call_invalid") from exc


def _semantic_call_identity(call: ExpectedProviderCall) -> dict[str, Any]:
    """Project request identity, omitting run-local and future proof fields."""
    return {
        "call_id": call.call_id,
        "stage_name": call.stage_name,
        "node_id": call.node_id,
        "prompt_id": call.prompt_id,
        "prompt_version": call.prompt_version,
        "prompt_sha256": call.prompt_sha256,
        "prompt_hash": call.prompt_hash,
        "input_hash": call.input_hash,
        "config_hash": call.config_hash,
        "schema_hash": call.schema_hash,
        "provider": call.provider,
        "model": call.model,
        "endpoint": call.endpoint,
        "endpoint_type": call.endpoint_type,
        "request_variants": [dict(row) for row in call.request_variants],
        "usage_required": bool(call.usage_required),
    }


def _provider_request_call_identity(call: ExpectedProviderCall) -> dict[str, Any]:
    """Compare semantic request identity without run-local job/call IDs."""
    payload = _semantic_call_identity(call)
    payload.pop("call_id", None)
    return payload


def _child_output_call_identity(call: ExpectedProviderCall) -> dict[str, str]:
    return {
        field: str(getattr(call, field) or "")
        for field in (
            "output_hash", "provider_response_hash", "normalized_output_hash",
            "artifact_payload_hash", "artifact_content_hash", "registry_file_hash",
            "artifact_path", "registered_artifact_hash", "replay_output_hash", "node_output_hash",
        )
    }


def _verify_child_output_hashes(
    registry: ArtifactRegistry,
    record: ArtifactRecord,
    call: ExpectedProviderCall,
) -> None:
    """Recompute semantic and envelope hashes from the registered child bytes."""
    document = _record_json(registry, record)
    if not isinstance(document, Mapping) or document.get("status") != "ready":
        raise SourceRevisionReplayError("child_reuse_proof_output_payload_invalid")

    if record.artifact_type == "outline_artifact" and record.artifact_version == "v3":
        from outline.v3_artifacts import OutlineArtifact

        artifact = OutlineArtifact.from_dict(document)
        if (
            document.get("artifact_type") != "outline_artifact"
            or document.get("artifact_version") != "v3"
            or document.get("job_id") != registry.job_id
            or artifact.job_id != registry.job_id
            or document.get("content_hash") != artifact.content_hash
        ):
            raise SourceRevisionReplayError("child_reuse_proof_outline_output_hash_invalid")
        payload_hash = hash_json(artifact.payload)
        content_hash = artifact.content_hash
    elif record.artifact_type == "review_section" and record.artifact_version == "v3":
        section = document.get("section")
        if (
            document.get("artifact_type") != "review_section"
            or document.get("artifact_version") != "v3"
            or document.get("job_id") != registry.job_id
            or document.get("section_id") != call.node_id
            or not isinstance(section, Mapping)
        ):
            raise SourceRevisionReplayError("child_reuse_proof_writer_output_invalid")
        payload_hash = hash_json(section)
        content_hash = str(document.get("content_hash") or "")
        if content_hash != payload_hash:
            raise SourceRevisionReplayError("child_reuse_proof_writer_output_hash_invalid")
    else:
        raise SourceRevisionReplayError("child_reuse_proof_output_type_unsupported")

    if (
        call.artifact_payload_hash != payload_hash
        or call.artifact_content_hash != content_hash
        or call.registered_artifact_hash != content_hash
        or call.node_output_hash != content_hash
    ):
        raise SourceRevisionReplayError("child_reuse_proof_output_hash_mismatch")


def _route_identity(value: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise SourceRevisionReplayError("route_identity_missing")
    fields = ("route", "provider", "model", "endpoint", "endpoint_type", "config_hash", "schema_hash")
    result: dict[str, str] = {}
    for field in fields:
        raw = value.get(field)
        if not isinstance(raw, str) or not raw.strip():
            raise SourceRevisionReplayError("route_identity_incomplete", f"route identity lacks {field}")
        result[field] = raw.strip()
    if value.get("route_fingerprint") is not None:
        fingerprint = value.get("route_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise SourceRevisionReplayError("route_identity_incomplete", "route_fingerprint is invalid")
        result["route_fingerprint"] = fingerprint.strip()
    return result


def _validate_scope(
    scope: Mapping[str, Any],
    source_rows: Mapping[str, tuple[str, str]],
) -> dict[str, Any]:
    if not isinstance(scope, Mapping) or set(scope) != _SCOPE_FIELDS:
        raise SourceRevisionReplayError(
            "scope_binding_missing",
            "SourceRevision scope fields are incomplete or unknown",
        )
    if scope.get("schema_version") != SOURCE_REVISION_REPLAY_SCOPE_VERSION:
        raise SourceRevisionReplayError("scope_binding_unsupported")
    raw_keys = scope.get("canonical_paper_keys")
    if (
        not isinstance(raw_keys, list)
        or any(not isinstance(item, str) or not item.strip() for item in raw_keys)
        or len(raw_keys) != len(set(raw_keys))
        or raw_keys != sorted(raw_keys)
    ):
        raise SourceRevisionReplayError("canonical_source_keys_invalid")
    selected_hash = _digest(scope.get("selected_source_content_hash"), "selected_source_content_hash")
    non_source_hash = _digest(scope.get("non_source_scope_hash"), "non_source_scope_hash")
    changed_selected = False
    for key in raw_keys:
        source_identity = source_rows.get(key)
        if source_identity is None:
            raise SourceRevisionReplayError("canonical_source_outside_plan", key)
        old_hash, new_hash = source_identity
        changed_selected = changed_selected or old_hash != new_hash
    return {
        "schema_version": SOURCE_REVISION_REPLAY_SCOPE_VERSION,
        "canonical_paper_keys": list(raw_keys),
        "selected_source_content_hash": selected_hash,
        "non_source_scope_hash": non_source_hash,
        "eligible": not changed_selected,
    }


def build_outline_source_revision_scope_v1(
    request: Mapping[str, Any],
) -> dict[str, Any]:
    """Use Outline's actual v2 source-free and selected-source projection."""
    try:
        from outline.topic_source_scope import topic_source_replay_scope_v1

        scope = topic_source_replay_scope_v1(request)
    except Exception as exc:
        raise SourceRevisionReplayError("outline_current_scope_invalid") from exc
    if scope is None:
        raise SourceRevisionReplayError("outline_current_scope_missing")
    if not isinstance(scope, Mapping) or set(scope) != _SCOPE_FIELDS:
        raise SourceRevisionReplayError("outline_current_scope_invalid")
    keys = scope.get("canonical_paper_keys")
    if (
        scope.get("schema_version") != SOURCE_REVISION_REPLAY_SCOPE_VERSION
        or not isinstance(keys, list)
        or any(not isinstance(key, str) or not key for key in keys)
        or len(keys) != len(set(keys))
        or keys != sorted(keys)
    ):
        raise SourceRevisionReplayError("outline_current_scope_invalid")
    _digest(scope.get("selected_source_content_hash"), "selected_source_content_hash")
    _digest(scope.get("non_source_scope_hash"), "non_source_scope_hash")
    return dict(scope)


def build_writer_source_revision_scope_v1(
    writer_task_scope: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Build an S3 scope from the actual v2 Writer task and context pins."""
    if (
        not isinstance(writer_task_scope, Mapping)
        or writer_task_scope.get("provider_semantic_contract_version") != "source-closed-provider/v2"
        or not isinstance(binding, Mapping)
    ):
        raise SourceRevisionReplayError("writer_selected_source_scope_missing")
    selected = writer_task_scope.get("selected_source_binding")
    contexts = writer_task_scope.get("selected_context_sources")
    if not isinstance(selected, Mapping) or not isinstance(contexts, list):
        raise SourceRevisionReplayError("writer_selected_source_scope_missing")
    try:
        from services.writer_task_scope import verify_writer_selected_source_scope_v2

        verify_writer_selected_source_scope_v2(
            writer_task_scope,
            provider_semantic_contract_version="source-closed-provider/v2",
        )
    except Exception as exc:
        raise SourceRevisionReplayError("writer_selected_source_scope_invalid") from exc
    keys = selected.get("paper_keys")
    if (
        not isinstance(keys, list)
        or any(not isinstance(key, str) or not key for key in keys)
        or len(keys) != len(set(keys))
        or keys != sorted(keys)
        or selected.get("schema_version") != "writer-selected-source-binding/v2"
    ):
        raise SourceRevisionReplayError("writer_selected_source_scope_invalid")
    # selected_source_binding hashes the complete canonical task bundles,
    # context source pins, and selected catalog. Rehash the stored v2 pair so
    # the caller cannot substitute a compact wire alias for the accepted scope.
    selected_hash = hash_json({
        "selected_source_binding": dict(selected),
        "selected_context_sources": contexts,
    })
    non_source_fields = (
        "binding_version", "stage_name", "section_id",
        "free_mode_input_artifact_id", "free_mode_input_artifact_hash", "free_mode_context_hash",
        "outline_section_hash", "evidence_packet_hash",
        "writer_provider", "writer_model", "writer_endpoint", "writer_config_hash",
        "system_prompt_hash", "prompt_id", "prompt_version", "prompt_sha256",
        "prompt_template_hash", "prompt_hash", "prompt_payload_hash",
        "output_schema_hash", "context_profile_hash", "application_schema_version",
        "provider_semantic_contract_version",
    )
    non_source = {name: binding[name] for name in non_source_fields if name in binding}
    return {
        "schema_version": SOURCE_REVISION_REPLAY_SCOPE_VERSION,
        "canonical_paper_keys": list(keys),
        "selected_source_content_hash": selected_hash,
        "non_source_scope_hash": hash_json(non_source),
    }


def writer_source_revision_scope_v1(
    writer_task_scope: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Alias for the public current-call scope builder."""
    return build_writer_source_revision_scope_v1(writer_task_scope, binding)


class SourceRevisionReplayContext:
    """Read-only, exact-parent reuse authority for one SourceRevision child."""

    def __init__(
        self,
        *,
        child_registry: ArtifactRegistry,
        plan_record: ArtifactRecord,
        plan_body: Mapping[str, Any],
        parent_registry: ArtifactRegistry,
        external_registry_resolver: Callable[[str], ArtifactRegistry | None],
    ) -> None:
        self.child_registry = child_registry
        self.plan_record = plan_record
        self.plan_body = dict(plan_body)
        self.parent_registry = parent_registry
        self.external_registry_resolver = external_registry_resolver
        self._context_token = object()
        self.child_runtime_spec_record: ArtifactRecord | None = None
        self._parent_workspace = self._bind_plan_and_registries()
        self._source_rows = {
            str(old["canonical_paper_key"]): (
                str(old["pdf_sha256"]), str(new["pdf_sha256"]),
            )
            for old, new in zip(
                self.plan_body["old_sources"], self.plan_body["new_sources"], strict=True,
            )
        }
        self._authority_records: list[ArtifactRecord] = []
        self._snapshot_records: list[ArtifactRecord] = []
        self._parent_records: dict[str, ArtifactRecord] = {}
        self._verify_plan_parent_union()

    def _resolve_registry(self, job_id: str) -> ArtifactRegistry | None:
        try:
            registry = self.external_registry_resolver(str(job_id))
        except Exception as exc:
            raise SourceRevisionReplayError("external_registry_resolution_failed", str(job_id)) from exc
        if (
            str(job_id) == self.parent_registry.job_id
            and (
                registry is None
                or registry.job_id != self.parent_registry.job_id
                or not _same_path(registry.registry_path, self.parent_registry.registry_path)
            )
        ):
            raise SourceRevisionReplayError("designated_parent_registry_mismatch")
        return registry

    def _bind_plan_and_registries(self) -> JobWorkspace:
        from runtime.source_revision import classify_replacement_delta
        from runtime.source_revision_admission import _validate_body

        try:
            _validate_body(self.plan_body)
        except Exception as exc:
            raise SourceRevisionReplayError("source_revision_plan_invalid", str(exc)) from exc
        try:
            delta = classify_replacement_delta(
                [
                    {key: row[key] for key in ("canonical_paper_key", "source_pdf", "pdf_sha256")}
                    for row in self.plan_body["old_sources"]
                ],
                [
                    {key: row[key] for key in ("canonical_paper_key", "source_pdf", "pdf_sha256")}
                    for row in self.plan_body["new_sources"]
                ],
            )
        except Exception as exc:
            raise SourceRevisionReplayError("source_revision_delta_invalid") from exc
        if (
            delta.changed_paper_key != self.plan_body["changed_paper_key"]
            or list(delta.unchanged_paper_keys) != self.plan_body["unchanged_paper_keys"]
        ):
            raise SourceRevisionReplayError("source_revision_delta_binding_mismatch")
        child_id = str(self.plan_body["child_job_id"])
        parent = self.plan_body["parent"]
        if (
            self.child_registry.job_id != child_id
            or self.plan_record.job_id != child_id
            or self.plan_record.status != "ready"
            or self.plan_record.artifact_id != SOURCE_REVISION_PLAN_ARTIFACT_ID
            or self.plan_record.artifact_type != SOURCE_REVISION_PLAN_ARTIFACT_TYPE
            or self.plan_record.artifact_version != SOURCE_REVISION_PLAN_ARTIFACT_VERSION
        ):
            raise SourceRevisionReplayError("child_plan_registry_binding_mismatch")
        child_root = Path(self.child_registry.registry_path).resolve().parent
        if child_root != _absolute(self.plan_body["child_workspace_path"], "child workspace").resolve():
            raise SourceRevisionReplayError("child_plan_workspace_mismatch")
        if not _path_within(self.plan_record.path, child_root):
            raise SourceRevisionReplayError("child_plan_artifact_outside_workspace")
        plan_document = _record_json(self.child_registry, self.plan_record)
        plan_payload = _artifact_payload(plan_document)
        if hash_json(plan_payload) != hash_json(self.plan_body):
            raise SourceRevisionReplayError("child_plan_artifact_body_mismatch")
        if self.plan_record.metadata.get("plan_content_hash") not in (None, hash_json(self.plan_body)):
            raise SourceRevisionReplayError("child_plan_artifact_content_hash_mismatch")

        runtime_spec_record = self.child_registry.get("runtime_job_spec")
        if (
            runtime_spec_record is None
            or runtime_spec_record.status != "ready"
            or runtime_spec_record.job_id != child_id
            or runtime_spec_record.artifact_type != "runtime_job_spec"
            or runtime_spec_record.artifact_version != "v1"
        ):
            raise SourceRevisionReplayError("child_runtime_spec_registry_binding_missing")
        self.child_runtime_spec_record = runtime_spec_record
        runtime_spec_document = _record_json(self.child_registry, runtime_spec_record)
        runtime_spec_payload = _artifact_payload(runtime_spec_document)
        if not isinstance(runtime_spec_payload, Mapping):
            raise SourceRevisionReplayError("child_runtime_spec_payload_invalid")
        from runtime.job_spec import RuntimeJobSpec
        from runtime.source_revision import (
            PROVIDER_SEMANTIC_CONTRACT_V2,
            SourceRevisionPlanRefV1,
            child_core_spec_hash,
        )

        try:
            child_spec = RuntimeJobSpec.from_mapping(runtime_spec_payload)
            reference = SourceRevisionPlanRefV1.from_mapping(
                child_spec.metadata.get("source_revision_plan", {}),
            )
        except Exception as exc:
            raise SourceRevisionReplayError("child_runtime_spec_plan_binding_invalid") from exc
        plan_file = _absolute(reference.path, "child source revision plan file")
        parent_root = _absolute(self.plan_body["parent"]["workspace_path"], "parent workspace").resolve()
        if plan_file.resolve().is_relative_to(child_root) or plan_file.resolve().is_relative_to(parent_root):
            raise SourceRevisionReplayError("child_source_revision_plan_file_inside_workspace")
        try:
            plan_bytes = plan_file.read_bytes()
        except OSError as exc:
            raise SourceRevisionReplayError("child_source_revision_plan_file_missing") from exc
        plan_file_payload = _decode_json(plan_bytes, "child source revision plan file")
        if (
            hashlib.sha256(plan_bytes).hexdigest() != reference.file_sha256
            or hash_json(plan_file_payload) != reference.content_hash
            or hash_json(plan_file_payload) != hash_json(self.plan_body)
        ):
            raise SourceRevisionReplayError("child_source_revision_plan_file_binding_mismatch")
        if (
            child_spec.job_id != child_id
            or child_spec.project_name != self.plan_body["child_project_name"]
            or not _same_path(child_spec.workspace_path, child_root)
            or child_spec.metadata.get("provider_semantic_contract_version") != PROVIDER_SEMANTIC_CONTRACT_V2
            or child_core_spec_hash(child_spec.to_dict()) != self.plan_body["child_core_spec_hash"]
        ):
            raise SourceRevisionReplayError("child_runtime_spec_core_binding_mismatch")
        try:
            from runtime.source_revision_admission import verify_source_revision_plan_v1

            freshly_verified_body = verify_source_revision_plan_v1(child_spec)
        except Exception as exc:
            raise SourceRevisionReplayError("source_revision_plan_fresh_verification_failed") from exc
        if hash_json(freshly_verified_body) != hash_json(self.plan_body):
            raise SourceRevisionReplayError("source_revision_plan_fresh_verification_mismatch")

        parent_job_id = str(parent["job_id"])
        parent_workspace_path = _absolute(parent["workspace_path"], "parent workspace")
        parent_registry_path = _absolute(parent["registry_path"], "parent Registry")
        if (
            self.parent_registry.job_id != parent_job_id
            or not _same_path(self.parent_registry.registry_path, parent_registry_path)
            or not _same_path(parent_registry_path, parent_workspace_path / "artifact_registry.json")
        ):
            raise SourceRevisionReplayError("designated_parent_registry_mismatch")
        suffix = f"__{parent_job_id}"
        basename = parent_workspace_path.name
        if not basename.endswith(suffix) or len(basename) <= len(suffix):
            raise SourceRevisionReplayError("designated_parent_workspace_identity_invalid")
        parent_project = basename[:-len(suffix)]
        parent_workspace = JobWorkspace(
            str(parent_workspace_path.parent), parent_project, parent_job_id,
        )
        if not _same_path(parent_workspace.root_dir, parent_workspace_path):
            raise SourceRevisionReplayError("designated_parent_workspace_identity_invalid")
        self._resolve_registry(parent_job_id)
        return parent_workspace

    @staticmethod
    def _ref_matches_record(ref: Mapping[str, Any], record: ArtifactRecord, owner_job_id: str) -> bool:
        return (
            ref.get("job_id") == owner_job_id == record.job_id
            and ref.get("artifact_id") == record.artifact_id
            and ref.get("artifact_type") == record.artifact_type
            and ref.get("artifact_version") == record.artifact_version
            and ref.get("artifact_role") == record.artifact_role
            and _same_path(str(ref.get("path") or ""), record.path)
            and ref.get("content_hash") == record.content_hash
            and record.status == "ready"
        )

    def _verify_plan_parent_union(self) -> None:
        parent = self.plan_body["parent"]
        parent_job = str(parent["job_id"])
        _revision, raw_records = self.parent_registry.capture_record_snapshot()
        records = {record.artifact_id: record for record in raw_records}
        if self.parent_registry._read_registry_unlocked() != (_revision, records):
            raise SourceRevisionReplayError("parent_registry_snapshot_changed")
        for refs, target in (
            (parent["authority_refs"], self._authority_records),
            (parent["response_snapshot_refs"], self._snapshot_records),
        ):
            for ref in refs:
                record = records.get(str(ref.get("artifact_id") or ""))
                if record is None or not self._ref_matches_record(ref, record, parent_job):
                    raise SourceRevisionReplayError("parent_plan_reference_mismatch")
                if not _path_within(record.path, parent["workspace_path"]):
                    raise SourceRevisionReplayError("parent_plan_artifact_outside_workspace", record.artifact_id)
                target.append(record)
                self._parent_records[record.artifact_id] = record
        if not any(record.artifact_type == "current_artifact_set" for record in self._authority_records):
            raise SourceRevisionReplayError("parent_current_set_authority_missing")
        if not any(record.artifact_type == "provider_receipt_closure" for record in self._authority_records):
            raise SourceRevisionReplayError("parent_provider_closure_authority_missing")
        if not self._snapshot_records:
            raise SourceRevisionReplayError("parent_response_snapshot_refs_missing")
        plan_dependencies = {
            (
                dependency.dependency_kind, dependency.job_id, dependency.artifact_id,
                dependency.artifact_type, str(Path(dependency.path).resolve()),
                dependency.content_hash,
            )
            for dependency in self.plan_record.depends_on
        }
        for record in (*self._authority_records, *self._snapshot_records):
            identity = (
                "external_job", record.job_id, record.artifact_id, record.artifact_type,
                str(Path(record.path).resolve()), record.content_hash,
            )
            if identity not in plan_dependencies:
                raise SourceRevisionReplayError(
                    "child_plan_external_dependency_missing",
                    f"plan artifact does not depend on {record.artifact_id}",
                )
        expected_external = {
            (
                "external_job", record.job_id, record.artifact_id, record.artifact_type,
                str(Path(record.path).resolve()), record.content_hash,
            )
            for record in (*self._authority_records, *self._snapshot_records)
        }
        actual_external = {
            dependency for dependency in plan_dependencies if dependency[0] == "external_job"
        }
        if actual_external != expected_external:
            raise SourceRevisionReplayError("child_plan_external_dependency_set_mismatch")
        resolver = self._resolve_registry
        try:
            self.child_registry.verify_ready_artifact_closure(
                self.plan_record, external_registry_resolver=resolver,
            )
            self.parent_registry.verify_ready_artifact_closures(
                [*self._authority_records, *self._snapshot_records],
                external_registry_resolver=resolver,
                expected_record_snapshot=(_revision, records),
            )
        except Exception as exc:
            raise SourceRevisionReplayError("source_revision_plan_closure_invalid", str(exc)) from exc
        if self.parent_registry._read_registry_unlocked() != (_revision, records):
            raise SourceRevisionReplayError("parent_registry_changed_during_plan_verification")

    def _reachable_from_registry_roots(
        self, roots: Sequence[ArtifactRecord],
    ) -> dict[str, ArtifactRecord]:
        found = {record.artifact_id: record for record in roots}
        pending = list(roots)
        seen: set[tuple[str, str]] = set()
        while pending:
            owner = pending.pop()
            identity = (owner.job_id, owner.artifact_id)
            if identity in seen:
                continue
            seen.add(identity)
            for dependency in owner.depends_on:
                if dependency.dependency_kind == "local_job" and dependency.job_id != owner.job_id:
                    raise SourceRevisionReplayError("parent_local_dependency_owner_mismatch")
                target_registry = self._resolve_registry(dependency.job_id)
                if target_registry is None:
                    raise SourceRevisionReplayError("parent_dependency_registry_missing")
                target = target_registry.get(dependency.artifact_id)
                if target is None or target.status != "ready":
                    raise SourceRevisionReplayError("parent_dependency_missing", dependency.artifact_id)
                if (
                    target.job_id != dependency.job_id
                    or target.artifact_type != dependency.artifact_type
                    or not _same_path(target.path, dependency.path)
                    or target.content_hash != dependency.content_hash
                ):
                    raise SourceRevisionReplayError("parent_dependency_reference_mismatch", dependency.artifact_id)
                if (
                    not _path_within(target.path, Path(target_registry.registry_path).parent)
                    and not self._is_plan_bound_parent_source_pdf(
                        dependency, target_registry, target,
                    )
                ):
                    if target.artifact_type == "source_pdf":
                        raise SourceRevisionReplayError(
                            "parent_source_pdf_not_plan_bound", target.artifact_id,
                        )
                    raise SourceRevisionReplayError("parent_dependency_outside_owner_workspace", target.artifact_id)
                found[target.artifact_id] = target
                pending.append(target)
        return found

    def _is_plan_bound_parent_source_pdf(
        self,
        dependency: Any,
        target_registry: ArtifactRegistry,
        target: ArtifactRecord,
    ) -> bool:
        """Allow only exact old-source PDFs outside the designated parent workspace."""
        if (
            target.artifact_type != "source_pdf"
            or dependency.dependency_kind != "local_job"
            or dependency.job_id != self.parent_registry.job_id
            or target.job_id != self.parent_registry.job_id
            or target_registry.job_id != self.parent_registry.job_id
            or not _same_path(target_registry.registry_path, self.parent_registry.registry_path)
        ):
            return False
        matching_rows = [
            row for row in self.plan_body["old_sources"]
            if _same_path(row["source_pdf"], target.path)
        ]
        if len(matching_rows) != 1:
            return False
        row = matching_rows[0]
        try:
            current_hash = hashlib.sha256(Path(target.path).read_bytes()).hexdigest()
        except OSError:
            return False
        return (
            row["pdf_sha256"] == dependency.content_hash
            == target.content_hash == current_hash
        )

    def _reachable_from_plan_authority(self) -> dict[str, ArtifactRecord]:
        return self._reachable_from_registry_roots(self._authority_records)

    def _provider_closures(
        self,
        expected_call_stage_name: str,
    ) -> list[tuple[ArtifactRecord, Mapping[str, Any]]]:
        expected_closure_stage = _SOURCE_REVISION_CLOSURE_STAGE_BY_CALL.get(
            str(expected_call_stage_name or "").strip(),
        )
        if expected_closure_stage is None:
            raise SourceRevisionReplayError(
                "unsupported_source_revision_provider_stage",
                str(expected_call_stage_name or ""),
            )
        result: list[tuple[ArtifactRecord, Mapping[str, Any]]] = []
        for record in self._authority_records:
            if record.artifact_type != "provider_receipt_closure":
                continue
            document = _record_json(self.parent_registry, record)
            payload = _artifact_payload(document)
            document_stage = (
                str(document.get("stage_name") or "").strip()
                if isinstance(document, Mapping) else ""
            )
            payload_stage = (
                str(payload.get("stage_name") or "").strip()
                if isinstance(payload, Mapping) else ""
            )
            metadata_stage = str(record.metadata.get("stage_name") or "").strip()
            closure_stage = payload_stage or document_stage or metadata_stage
            if not closure_stage:
                raise SourceRevisionReplayError(
                    "parent_provider_closure_stage_missing", record.artifact_id,
                )
            if closure_stage != expected_closure_stage:
                continue
            if (
                not isinstance(payload, Mapping)
                or payload.get("job_id") != self.parent_registry.job_id
                or payload.get("complete") is not True
                or not isinstance(payload.get("expected_calls"), list)
                or not isinstance(payload.get("expected_call_ids"), list)
                or not isinstance(payload.get("observed_call_ids"), list)
            ):
                raise SourceRevisionReplayError("parent_provider_closure_incomplete", record.artifact_id)
            result.append((record, payload))
        return result

    def _all_parent_expected_calls(
        self,
        expected_call_stage_name: str,
    ) -> list[tuple[ArtifactRecord, Mapping[str, Any], ExpectedProviderCall]]:
        result = []
        for closure, payload in self._provider_closures(expected_call_stage_name):
            for raw in payload["expected_calls"]:
                if not isinstance(raw, Mapping):
                    raise SourceRevisionReplayError("parent_expected_call_invalid", closure.artifact_id)
                try:
                    call = ExpectedProviderCall.from_mapping(raw)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise SourceRevisionReplayError("parent_expected_call_invalid", closure.artifact_id) from exc
                if call.job_id == self.parent_registry.job_id:
                    result.append((closure, payload, call))
        return result

    def _closure_calls_for_request(
        self, current: ExpectedProviderCall, request_hash: str,
    ) -> list[tuple[ArtifactRecord, Mapping[str, Any], ExpectedProviderCall]]:
        matches = []
        for record, payload in self._provider_closures(current.stage_name):
            for raw in payload["expected_calls"]:
                if not isinstance(raw, Mapping):
                    raise SourceRevisionReplayError("parent_expected_call_invalid", record.artifact_id)
                try:
                    call = ExpectedProviderCall.from_mapping(raw)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise SourceRevisionReplayError("parent_expected_call_invalid", record.artifact_id) from exc
                if (
                    call.stage_name == current.stage_name
                    and call.node_id == current.node_id
                    and call.job_id == self.parent_registry.job_id
                    and call.input_hash == request_hash
                ):
                    matches.append((record, payload, call))
        return matches

    def _scope_record_reachable(
        self,
        scope_record: ArtifactRecord,
        reachable: Mapping[str, ArtifactRecord],
    ) -> None:
        if (
            scope_record.job_id != self.parent_registry.job_id
            or scope_record.status != "ready"
            or self.parent_registry.get(scope_record.artifact_id) != scope_record
            or scope_record.artifact_id not in reachable
            or reachable[scope_record.artifact_id] != scope_record
            or not _path_within(scope_record.path, self.plan_body["parent"]["workspace_path"])
        ):
            raise SourceRevisionReplayError("parent_scope_record_not_accepted")
        try:
            self.parent_registry.verify_ready_artifact_closure(
                scope_record, external_registry_resolver=self._resolve_registry,
            )
        except Exception as exc:
            raise SourceRevisionReplayError("parent_scope_record_closure_invalid") from exc

    def _parent_scope_from_record(
        self,
        scope_record: ArtifactRecord,
        call: ExpectedProviderCall,
        reachable: Mapping[str, ArtifactRecord],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        self._scope_record_reachable(scope_record, reachable)
        if scope_record.artifact_type == "outline_request_payload_audit":
            try:
                raw = Path(scope_record.path).read_bytes()
            except OSError as exc:
                raise SourceRevisionReplayError("parent_scope_record_unreadable") from exc
            if hashlib.sha256(raw).hexdigest() != scope_record.content_hash:
                raise SourceRevisionReplayError("parent_scope_record_hash_mismatch")
            rows = []
            for number, line in enumerate(raw.splitlines(), start=1):
                if not line.strip():
                    continue
                row = _decode_json(line, f"outline audit row {number}")
                if isinstance(row, Mapping) and (
                    row.get("call_id") == call.call_id or row.get("attempt_id") == call.call_id
                ):
                    rows.append(row)
            if len(rows) != 1:
                raise SourceRevisionReplayError("parent_scope_row_missing_or_ambiguous")
            row = rows[0]
            if row.get("payload_hash") != call.input_hash:
                raise SourceRevisionReplayError("parent_scope_request_hash_mismatch")
            scope = row.get("source_revision_scope")
            if not isinstance(scope, Mapping):
                raise SourceRevisionReplayError(
                    "missing_parent_scope_binding",
                    "outline_request_payload_audit/v1 lacks captured source_revision_scope",
                )
            if (
                isinstance(row.get("paper_keys"), list)
                and row.get("paper_keys") != scope.get("canonical_paper_keys")
            ):
                raise SourceRevisionReplayError("parent_scope_canonical_keys_mismatch")
            route = row.get("route")
            if not isinstance(route, Mapping):
                raise SourceRevisionReplayError("outline_scope_route_missing")
            route_value = {
                "route": str(route.get("config_section") or ""),
                "provider": str(route.get("provider") or ""),
                "model": str(route.get("model") or ""),
                "endpoint": str(route.get("api_base_host") or ""),
                "endpoint_type": str(route.get("endpoint_type") or ""),
                "config_hash": str(row.get("config_hash") or ""),
                "schema_hash": str(row.get("schema_hash") or ""),
            }
            if row.get("route_fingerprint") is not None:
                route_value["route_fingerprint"] = str(row.get("route_fingerprint") or "")
            return _validate_scope(scope, self._source_rows), _route_identity(route_value)
        if scope_record.artifact_type == "review_section" and scope_record.artifact_version == "v3":
            document = _record_json(self.parent_registry, scope_record)
            if not isinstance(document, Mapping) or document.get("job_id") != self.parent_registry.job_id:
                raise SourceRevisionReplayError("parent_writer_section_identity_mismatch")
            section = document.get("section")
            binding = document.get("binding")
            if not isinstance(section, Mapping) or not isinstance(binding, Mapping):
                raise SourceRevisionReplayError("parent_writer_section_binding_missing")
            if (
                document.get("status") != "ready"
                or document.get("section_id") != call.node_id
                or binding.get("prompt_payload_hash") != call.input_hash
            ):
                raise SourceRevisionReplayError("parent_writer_section_request_mismatch")
            writer_scope = section.get("writer_task_scope")
            if not isinstance(writer_scope, Mapping):
                raise SourceRevisionReplayError("parent_writer_task_scope_missing")
            scope = build_writer_source_revision_scope_v1(writer_scope, binding)
            route_value = {
                "route": "Writer_API",
                "provider": call.provider,
                "model": call.model,
                "endpoint": call.endpoint,
                "endpoint_type": call.endpoint_type,
                "config_hash": call.config_hash,
                "schema_hash": call.schema_hash,
            }
            return _validate_scope(scope, self._source_rows), _route_identity(route_value)
        raise SourceRevisionReplayError(
            "parent_scope_artifact_type_unsupported", scope_record.artifact_type,
        )

    def _writer_lineage(
        self,
        *,
        call: ExpectedProviderCall,
        receipt: ProviderCallReceiptV1,
        output: ArtifactRecord,
        reachable: Mapping[str, ArtifactRecord],
    ) -> tuple[ArtifactRecord, ...]:
        if call.stage_name != "stage3_review":
            return ()
        replay_records = [
            record for record in reachable.values()
            if record.artifact_type == "review_replay_ledger"
            and record.artifact_version == "v1"
            and record.job_id == self.parent_registry.job_id
        ]
        if len(replay_records) != 1:
            raise SourceRevisionReplayError("parent_writer_replay_lineage_missing_or_ambiguous")
        replay_record = replay_records[0]
        try:
            raw = Path(replay_record.path).read_bytes()
        except OSError as exc:
            raise SourceRevisionReplayError("parent_writer_replay_lineage_unreadable") from exc
        if hashlib.sha256(raw).hexdigest() != replay_record.content_hash:
            raise SourceRevisionReplayError("parent_writer_replay_lineage_hash_mismatch")
        rows = []
        for number, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            row = _decode_json(line, f"Writer replay row {number}")
            if isinstance(row, Mapping) and row.get("section_id") == call.node_id:
                rows.append(row)
        if len(rows) != 1:
            raise SourceRevisionReplayError("parent_writer_replay_row_missing_or_ambiguous")
        replay = rows[0]
        if (
            replay.get("job_id") != self.parent_registry.job_id
            or replay.get("stage_name") != "stage3_review"
            or replay.get("receipt_id") != receipt.receipt_id
            or replay.get("receipt_hash") != hash_json(receipt.to_dict())
            or replay.get("artifact_id") != output.artifact_id
            or replay.get("artifact_path") != output.path
            or replay.get("registry_file_hash") != output.content_hash
        ):
            raise SourceRevisionReplayError("parent_writer_replay_row_binding_mismatch")
        snapshot_id = str(replay.get("receipt_snapshot_artifact_id") or "")
        snapshot = reachable.get(snapshot_id)
        if (
            snapshot is None
            or snapshot.artifact_type != "review_provider_receipt_snapshot"
            or snapshot.artifact_version != "v1"
            or snapshot.job_id != self.parent_registry.job_id
            or snapshot.content_hash != replay.get("receipt_snapshot_content_hash")
            or not _same_path(snapshot.path, str(replay.get("receipt_snapshot_path") or ""))
        ):
            raise SourceRevisionReplayError("parent_writer_receipt_snapshot_lineage_mismatch")
        try:
            self.parent_registry.verify_ready_artifact_closures(
                [replay_record, snapshot, output],
                external_registry_resolver=self._resolve_registry,
            )
            snapshot_payload = _artifact_payload(_record_json(self.parent_registry, snapshot))
        except Exception as exc:
            raise SourceRevisionReplayError("parent_writer_lineage_closure_invalid") from exc
        snapshot_receipt = snapshot_payload.get("receipt")
        if (
            not isinstance(snapshot_receipt, Mapping)
            or hash_json(snapshot_receipt) != hash_json(receipt.to_dict())
        ):
            raise SourceRevisionReplayError("parent_writer_receipt_snapshot_mismatch")
        return replay_record, snapshot

    def _parent_receipt(
        self,
        call: ExpectedProviderCall,
        closure_payload: Mapping[str, Any],
        reachable: Mapping[str, ArtifactRecord],
    ) -> tuple[ProviderCallReceiptV1, ArtifactRecord]:
        if not call.closure_epoch_id or not call.logical_attempt_identity:
            raise SourceRevisionReplayError("parent_expected_call_attempt_identity_missing")
        if call.verified_reuse:
            raise SourceRevisionReplayError(
                "unsupported_parent_reuse_authority",
                "matching parent ExpectedProviderCall is itself verified reuse from an ancestor",
            )
        ledgers = [
            record for record in reachable.values()
            if record.artifact_type == "provider_receipt_ledger"
            and record.status == "ready" and record.job_id == self.parent_registry.job_id
        ]
        rows: list[tuple[ProviderCallReceiptV1, ArtifactRecord]] = []
        for ledger in ledgers:
            try:
                self.parent_registry.verify_ready_artifact_closure(
                    ledger, external_registry_resolver=self._resolve_registry,
                )
                raw = Path(ledger.path).read_bytes()
            except Exception as exc:
                raise SourceRevisionReplayError("parent_receipt_ledger_invalid", ledger.artifact_id) from exc
            if hashlib.sha256(raw).hexdigest() != ledger.content_hash:
                raise SourceRevisionReplayError("parent_receipt_ledger_hash_mismatch", ledger.artifact_id)
            if self.parent_registry.get(ledger.artifact_id) != ledger:
                raise SourceRevisionReplayError("parent_receipt_ledger_registry_changed", ledger.artifact_id)
            for number, line in enumerate(raw.splitlines(), start=1):
                if not line.strip():
                    continue
                value = _decode_json(line, f"provider receipt ledger line {number}")
                if not isinstance(value, Mapping):
                    raise SourceRevisionReplayError("parent_receipt_row_invalid", ledger.artifact_id)
                try:
                    receipt = ProviderCallReceiptV1.from_dict(value)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise SourceRevisionReplayError("parent_receipt_row_invalid", ledger.artifact_id) from exc
                if (
                    receipt.job_id == self.parent_registry.job_id
                    and receipt.call_id == call.call_id
                    and receipt.stage_name == call.stage_name
                    and receipt.node_id == call.node_id
                    and receipt.closure_epoch_id == call.closure_epoch_id
                    and receipt.attempt_id == call.attempt_id
                    and receipt.logical_attempt_identity == call.logical_attempt_identity
                ):
                    rows.append((receipt, ledger))
        if not rows:
            raise SourceRevisionReplayError("parent_original_receipt_missing")
        expected_ids = closure_payload.get("expected_call_ids")
        observed_calls = closure_payload.get("observed_call_ids")
        observed_receipts = closure_payload.get("observed_receipt_ids")
        if (
            not isinstance(expected_ids, list)
            or call.call_id not in expected_ids
            or not isinstance(observed_calls, list)
            or call.call_id not in observed_calls
        ):
            raise SourceRevisionReplayError("parent_closure_call_projection_mismatch")
        if isinstance(observed_receipts, list):
            rows = [pair for pair in rows if pair[0].receipt_id in observed_receipts]
            if not rows:
                raise SourceRevisionReplayError("parent_closure_receipt_projection_mismatch")
        by_hash: dict[str, list[tuple[ProviderCallReceiptV1, ArtifactRecord]]] = {}
        for receipt, ledger in rows:
            by_hash.setdefault(hash_json(receipt.to_dict()), []).append((receipt, ledger))
        if len(by_hash) > 1:
            newest = max(
                (pair for group in by_hash.values() for pair in group),
                key=lambda pair: (
                    str(pair[0].finished_at or ""), int(pair[0].sequence), int(pair[0].attempts),
                ),
            )
            latest_hash = hash_json(newest[0].to_dict())
            rows = by_hash[latest_hash]
        owners = {(ledger.artifact_id, ledger.content_hash) for _receipt, ledger in rows}
        if len(owners) != 1:
            raise SourceRevisionReplayError("ambiguous_parent_receipt_owner")
        receipt, ledger = rows[0]
        if receipt.status != "success" or receipt.error_kind is not None:
            raise SourceRevisionReplayError("parent_original_receipt_not_successful")
        if (
            receipt.incomplete_reason or receipt.timeout_kind
            or receipt.finish_reason.strip().casefold() in {
                "length", "max_tokens", "max_output_tokens", "incomplete",
            }
            or receipt.test_only
        ):
            raise SourceRevisionReplayError("parent_original_receipt_incomplete")
        try:
            receipt.validate_acceptance_authority(
                expected_job_id=self.parent_registry.job_id,
                expected_attempt_id=call.attempt_id,
                expected_stage_name=call.stage_name,
            )
        except Exception as exc:
            raise SourceRevisionReplayError("parent_original_receipt_authority_invalid") from exc
        if (
            receipt.prompt_hash != call.prompt_hash
            or receipt.prompt_id != call.prompt_id
            or receipt.prompt_version != call.prompt_version
            or receipt.prompt_sha256 != call.prompt_sha256
            or receipt.input_hash != call.input_hash
            or receipt.config_hash != call.config_hash
            or receipt.schema_hash != call.schema_hash
            or receipt.provider != call.provider
            or receipt.model != call.model
            or receipt.endpoint != call.endpoint
            or receipt.endpoint_type != call.endpoint_type
        ):
            raise SourceRevisionReplayError("parent_receipt_expected_call_mismatch")
        if not call.provider_response_hash or receipt.response_hash != call.provider_response_hash:
            raise SourceRevisionReplayError("parent_receipt_provider_response_hash_mismatch")
        if not call.normalized_output_hash or receipt.response_hash != call.normalized_output_hash:
            raise SourceRevisionReplayError("parent_receipt_normalized_output_hash_mismatch")
        return receipt, ledger

    def _accepted_output(
        self,
        call: ExpectedProviderCall,
        reachable: Mapping[str, ArtifactRecord],
    ) -> ArtifactRecord:
        if not call.artifact_path or not call.registry_file_hash or not call.artifact_content_hash:
            raise SourceRevisionReplayError("parent_accepted_output_ref_missing")
        matches = [
            record for record in reachable.values()
            if record.job_id == self.parent_registry.job_id
            and record.status == "ready"
            and _same_path(record.path, call.artifact_path)
        ]
        if len(matches) != 1:
            raise SourceRevisionReplayError("parent_accepted_output_missing_or_ambiguous")
        record = matches[0]
        if not _path_within(record.path, self.plan_body["parent"]["workspace_path"]):
            raise SourceRevisionReplayError("parent_accepted_output_outside_workspace")
        if record.content_hash != call.registry_file_hash:
            raise SourceRevisionReplayError("parent_accepted_output_registry_hash_mismatch")
        document = _record_json(self.parent_registry, record)
        if not isinstance(document, Mapping):
            raise SourceRevisionReplayError("parent_accepted_output_invalid")
        logical = document.get("payload")
        if logical is None:
            logical = document.get("section")
        if logical is None:
            logical = document.get("analysis")
        logical_hash = str(document.get("content_hash") or "") or hash_json(logical)
        if logical_hash != call.artifact_content_hash:
            raise SourceRevisionReplayError("parent_accepted_output_content_hash_mismatch")
        if call.artifact_payload_hash and hash_json(logical) != call.artifact_payload_hash:
            raise SourceRevisionReplayError("parent_accepted_output_payload_hash_mismatch")
        return record

    def _snapshot_for_receipt(
        self, receipt: ProviderCallReceiptV1,
    ) -> tuple[ArtifactRecord, Any]:
        key = hash_json({"job_id": receipt.job_id, "receipt_id": receipt.receipt_id})
        artifact_id = f"provider-response-snapshot:{key}"
        refs = [record for record in self._snapshot_records if record.artifact_id == artifact_id]
        if len(refs) != 1:
            raise SourceRevisionReplayError("parent_response_snapshot_ref_missing_or_ambiguous")
        record = refs[0]
        store = ProviderResponseSnapshotStore(
            self._parent_workspace, self.parent_registry, publication_context=None,
        )
        try:
            verified_record = store.read_registered(receipt)
            content = store.read_registered_content(receipt)
        except ProviderResponseSnapshotError as exc:
            raise SourceRevisionReplayError("parent_response_snapshot_invalid", str(exc)) from exc
        if verified_record != record:
            raise SourceRevisionReplayError("parent_response_snapshot_plan_ref_mismatch")
        return record, content

    def _find_match(
        self,
        *,
        expected_call: ExpectedProviderCall,
        request_hash: str,
        current_scope: Mapping[str, Any],
        scope_record: ArtifactRecord | None,
        route_identity: Mapping[str, Any],
        allow_verified_reuse: bool = False,
    ) -> SourceRevisionReplayLookup:
        current_scope_value = _validate_scope(current_scope, self._source_rows)
        current_route = _route_identity(route_identity)
        if (
            expected_call.job_id != self.child_registry.job_id
            or (expected_call.verified_reuse and not allow_verified_reuse)
            or not expected_call.call_id
        ):
            raise SourceRevisionReplayError("child_expected_call_identity_invalid")
        if (
            request_hash != expected_call.input_hash
            or current_route["config_hash"] != expected_call.config_hash
            or current_route["schema_hash"] != expected_call.schema_hash
            or current_route["provider"] != expected_call.provider
            or current_route["model"] != expected_call.model
            or current_route["endpoint"] != expected_call.endpoint
            or current_route["endpoint_type"] != expected_call.endpoint_type
        ):
            raise SourceRevisionReplayError("child_request_route_contract_mismatch")

        reachable = self._reachable_from_plan_authority()
        candidates = self._closure_calls_for_request(expected_call, request_hash)
        if not candidates:
            same_node = any(
                call.stage_name == expected_call.stage_name and call.node_id == expected_call.node_id
                for _closure, _payload, call in self._all_parent_expected_calls(expected_call.stage_name)
            )
            return SourceRevisionReplayLookup(
                "no_match", reason="request_hash_changed" if same_node else "no_parent_call",
            )
        semantic_candidates = [
            item for item in candidates
            if hash_json(_provider_request_call_identity(item[2]))
            == hash_json(_provider_request_call_identity(expected_call))
        ]
        if not semantic_candidates:
            return SourceRevisionReplayLookup("no_match", reason="provider_call_identity_changed")
        if any(item[2].verified_reuse for item in semantic_candidates):
            raise SourceRevisionReplayError(
                "unsupported_parent_reuse_authority",
                "matching parent ExpectedProviderCall is itself verified reuse from an ancestor",
            )
        if not current_scope_value["eligible"]:
            return SourceRevisionReplayLookup("no_match", reason="changed_source_key")

        matches: list[SourceRevisionReplayMatch] = []
        no_match_reasons: list[str] = []
        for closure_record, _closure_payload, parent_call in semantic_candidates:
            if closure_record.artifact_id not in reachable:
                raise SourceRevisionReplayError("parent_call_closure_not_plan_bound")
            closure_reachable = self._reachable_from_registry_roots((closure_record,))
            parent_output = self._accepted_output(parent_call, closure_reachable)
            parent_scope_record = self._find_parent_scope_record(
                call=parent_call,
                supplied=scope_record,
                reachable=reachable,
                accepted_output=parent_output,
            )
            parent_scope_value, parent_route = self._parent_scope_from_record(
                parent_scope_record, parent_call, reachable,
            )
            if not parent_scope_value["eligible"]:
                no_match_reasons.append("changed_source_key")
                continue
            if (
                parent_scope_value["canonical_paper_keys"] != current_scope_value["canonical_paper_keys"]
                or parent_scope_value["selected_source_content_hash"]
                != current_scope_value["selected_source_content_hash"]
            ):
                no_match_reasons.append("selected_source_scope_changed")
                continue
            if parent_scope_value["non_source_scope_hash"] != current_scope_value["non_source_scope_hash"]:
                no_match_reasons.append("non_source_scope_changed")
                continue
            if (
                parent_route["route"] != current_route["route"]
                or any(parent_route[field] != current_route[field] for field in (
                    "provider", "model", "endpoint", "endpoint_type", "config_hash", "schema_hash",
                ))
                or (
                    "route_fingerprint" in current_route
                    and parent_route.get("route_fingerprint") != current_route["route_fingerprint"]
                )
            ):
                no_match_reasons.append("route_identity_changed")
                continue
            receipt, ledger = self._parent_receipt(parent_call, _closure_payload, closure_reachable)
            if parent_call.stage_name == "outline_v3":
                outline_call_prefix = "outline:"
                receipt_route_matches = (
                    parent_call.call_id.startswith(outline_call_prefix)
                    and receipt.route == parent_call.call_id.removeprefix(outline_call_prefix)
                    and receipt.metadata.get("config_section") == parent_route["route"]
                )
            else:
                receipt_route_matches = receipt.route == current_route["route"]
            if not receipt_route_matches:
                no_match_reasons.append("route_identity_changed")
                continue
            snapshot, response = self._snapshot_for_receipt(receipt)
            lineage = self._writer_lineage(
                call=parent_call, receipt=receipt, output=parent_output, reachable=closure_reachable,
            )
            matches.append(SourceRevisionReplayMatch(
                stage_name=expected_call.stage_name,
                call_id=expected_call.call_id,
                request_hash=request_hash,
                route_identity=dict(current_route),
                scope={key: value for key, value in current_scope_value.items() if key != "eligible"},
                receipt=receipt,
                response=response,
                parent_closure=closure_record,
                parent_ledger=ledger,
                response_snapshot=snapshot,
                accepted_output=parent_output,
                parent_scope_record=parent_scope_record,
                parent_lineage_records=lineage,
                parent_expected_call=asdict(parent_call),
                _context_token=self._context_token,
            ))
        if not matches:
            reason = no_match_reasons[0] if no_match_reasons else "no_parent_call"
            return SourceRevisionReplayLookup("no_match", reason=reason)
        output_identities = {
            (item.accepted_output.content_hash, item.receipt.response_hash) for item in matches
        }
        if len(output_identities) > 1:
            raise SourceRevisionReplayError("ambiguous_parent_call_outputs")
        matches.sort(key=lambda item: (item.parent_closure.artifact_id, item.receipt.receipt_id))
        return SourceRevisionReplayLookup("match", match=matches[0])

    def _find_parent_scope_record(
        self,
        *,
        call: ExpectedProviderCall,
        supplied: ArtifactRecord | None,
        reachable: Mapping[str, ArtifactRecord],
        accepted_output: ArtifactRecord,
    ) -> ArtifactRecord:
        candidates: list[ArtifactRecord] = []
        if call.stage_name == "stage3_review":
            candidates = [accepted_output]
        else:
            for record in reachable.values():
                if record.artifact_type != "outline_request_payload_audit" or record.job_id != self.parent_registry.job_id:
                    continue
                try:
                    raw = Path(record.path).read_bytes()
                except OSError as exc:
                    raise SourceRevisionReplayError("parent_scope_record_unreadable") from exc
                if hashlib.sha256(raw).hexdigest() != record.content_hash:
                    raise SourceRevisionReplayError("parent_scope_record_hash_mismatch")
                for number, line in enumerate(raw.splitlines(), start=1):
                    if not line.strip():
                        continue
                    row = _decode_json(line, f"outline audit row {number}")
                    if (
                        isinstance(row, Mapping)
                        and (row.get("call_id") == call.call_id or row.get("attempt_id") == call.call_id)
                        and row.get("payload_hash") == call.input_hash
                    ):
                        candidates.append(record)
                        break
        if supplied is not None:
            if supplied not in candidates:
                raise SourceRevisionReplayError("parent_scope_record_not_accepted")
            candidates = [supplied]
        if len(candidates) != 1:
            raise SourceRevisionReplayError("parent_scope_record_missing_or_ambiguous")
        supplied = candidates[0]
        if (
            supplied.job_id != self.parent_registry.job_id
            or supplied.status != "ready"
            or self.parent_registry.get(supplied.artifact_id) != supplied
            or supplied.artifact_id not in reachable
        ):
            raise SourceRevisionReplayError("parent_scope_record_not_accepted")
        return supplied

    def lookup_response(
        self,
        *,
        expected_call: ExpectedProviderCall | Mapping[str, Any],
        request_payload: Any,
        current_scope: Mapping[str, Any],
        parent_scope_record: ArtifactRecord | None,
        route_identity: Mapping[str, Any],
    ) -> SourceRevisionReplayLookup:
        """Find an exact direct-parent response; changed scopes return no_match."""
        call = _expected_call(expected_call)
        return self._find_match(
            expected_call=call,
            request_hash=hash_json(request_payload),
            current_scope=current_scope,
            scope_record=parent_scope_record,
            route_identity=route_identity,
        )

    def _external_refs(self, match: SourceRevisionReplayMatch) -> tuple[ArtifactDependencyRefV2, ...]:
        records = (
            match.parent_closure,
            match.parent_ledger,
            match.response_snapshot,
            match.accepted_output,
            match.parent_scope_record,
            *match.parent_lineage_records,
        )
        unique: dict[tuple[str, str], ArtifactDependencyRefV2] = {}
        for record in records:
            if record.job_id != self.parent_registry.job_id:
                raise SourceRevisionReplayError("reuse_parent_record_foreign")
            unique[(record.job_id, record.artifact_id)] = ArtifactDependencyRefV2.from_record(
                record, dependency_kind="external_job",
            )
        return tuple(unique.values())

    def publish_child_reuse_proof(
        self,
        *,
        match: SourceRevisionReplayMatch,
        child_output_record: ArtifactRecord,
        child_plan_record: ArtifactRecord,
        child_expected_call: ExpectedProviderCall | Mapping[str, Any],
        publication_context: Any,
        path: str | Path,
        stage_lineage_records: Sequence[ArtifactRecord] = (),
    ) -> ArtifactRecord:
        """Publish proof after the child parser has published its output."""
        if not isinstance(match, SourceRevisionReplayMatch) or match._context_token is not self._context_token:
            raise SourceRevisionReplayError("reuse_match_not_from_bound_context")
        call = _expected_call(child_expected_call)
        if (
            child_plan_record != self.plan_record
            or self.child_runtime_spec_record is None
            or child_output_record.job_id != self.child_registry.job_id
            or child_output_record.status != "ready"
            or self.child_registry.get(child_output_record.artifact_id) != child_output_record
            or call.job_id != self.child_registry.job_id
            or call.call_id != match.call_id
            or call.stage_name != match.stage_name
            or call.input_hash != match.request_hash
            or call.output_hash != match.receipt.response_hash
            or not call.artifact_path
            or not _same_path(call.artifact_path, child_output_record.path)
            or call.registry_file_hash != child_output_record.content_hash
            or not call.artifact_content_hash
            or not call.artifact_payload_hash
            or call.provider_response_hash != match.receipt.response_hash
            or call.normalized_output_hash != match.receipt.response_hash
        ):
            raise SourceRevisionReplayError("child_reuse_proof_call_or_output_mismatch")
        _verify_child_output_hashes(self.child_registry, child_output_record, call)
        target = Path(path).expanduser().resolve()
        child_root = Path(self.child_registry.registry_path).resolve().parent
        if not target.is_relative_to(child_root):
            raise SourceRevisionReplayError("child_reuse_proof_path_outside_workspace")
        extras: list[ArtifactRecord] = []
        for record in stage_lineage_records:
            if record.job_id not in {self.child_registry.job_id, self.parent_registry.job_id}:
                raise SourceRevisionReplayError("reuse_proof_lineage_owner_invalid")
            owner = self.child_registry if record.job_id == self.child_registry.job_id else self.parent_registry
            if owner.get(record.artifact_id) != record or record.status != "ready":
                raise SourceRevisionReplayError("reuse_proof_lineage_record_changed", record.artifact_id)
            extras.append(record)
        local_dependencies = [
            ArtifactDependencyRefV2.from_record(self.plan_record),
            ArtifactDependencyRefV2.from_record(self.child_runtime_spec_record),
            ArtifactDependencyRefV2.from_record(child_output_record),
        ]
        external_dependencies = list(self._external_refs(match))
        for record in extras:
            ref = ArtifactDependencyRefV2.from_record(
                record,
                dependency_kind="local_job" if record.job_id == self.child_registry.job_id else "external_job",
            )
            (local_dependencies if record.job_id == self.child_registry.job_id else external_dependencies).append(ref)
        dep_map = {
            (ref.dependency_kind, ref.job_id, ref.artifact_id): ref
            for ref in (*local_dependencies, *external_dependencies)
        }
        payload: dict[str, Any] = {
            "artifact_type": SOURCE_REVISION_REUSE_ARTIFACT_TYPE,
            "artifact_version": SOURCE_REVISION_REUSE_ARTIFACT_VERSION,
            "status": "ready",
            "job_id": self.child_registry.job_id,
            "plan_artifact_id": self.plan_record.artifact_id,
            "plan_artifact_hash": self.plan_record.content_hash,
            "plan_content_hash": hash_json(self.plan_body),
            "child_runtime_spec_ref": _record_ref(self.child_runtime_spec_record),
            "child_call": _semantic_call_identity(call),
            "child_output_call": _child_output_call_identity(call),
            "request_hash": match.request_hash,
            "route_identity": dict(match.route_identity),
            "scope": dict(match.scope),
            "source_authority": {
                "parent_job_id": self.parent_registry.job_id,
                "parent_registry_path": self.parent_registry.registry_path,
                "parent_closure_ref": _record_ref(match.parent_closure, external=True),
                "parent_ledger_ref": _record_ref(match.parent_ledger, external=True),
                "parent_response_snapshot_ref": _record_ref(match.response_snapshot, external=True),
                "parent_accepted_output_ref": _record_ref(match.accepted_output, external=True),
                "parent_scope_record_ref": _record_ref(match.parent_scope_record, external=True),
                "parent_lineage_refs": [
                    _record_ref(record, external=True) for record in match.parent_lineage_records
                ],
                "receipt_id": match.receipt.receipt_id,
                "receipt_hash": hash_json(match.receipt.to_dict()),
                "receipt_response_hash": match.receipt.response_hash,
                "parent_call_id": str(match.parent_expected_call.get("call_id") or ""),
                "parent_expected_call_hash": hash_json(match.parent_expected_call),
            },
            "child_output_ref": _record_ref(child_output_record),
            "stage_lineage_refs": [
                _record_ref(record, external=record.job_id != self.child_registry.job_id)
                for record in extras
            ],
        }
        payload["proof_hash"] = hash_json(payload)
        artifact_id = (
            f"source-revision-reuse:{hash_json(self.plan_body)[:16]}:"
            f"{hash_json([call.stage_name, call.call_id, call.closure_epoch_id])[:24]}"
        )
        try:
            record = publish_json_artifact(
                publication_context,
                self.child_registry,
                target,
                payload,
                artifact_id=artifact_id,
                artifact_role=SOURCE_REVISION_REUSE_ARTIFACT_TYPE,
                artifact_type=SOURCE_REVISION_REUSE_ARTIFACT_TYPE,
                artifact_version=SOURCE_REVISION_REUSE_ARTIFACT_VERSION,
                producer="runtime.source_revision_replay.SourceRevisionReplayContext",
                depends_on=list(dep_map.values()),
                external_registry_resolver=self._resolve_registry,
                metadata={
                    "job_id": self.child_registry.job_id,
                    "stage_name": call.stage_name,
                    "call_id": call.call_id,
                    "plan_content_hash": hash_json(self.plan_body),
                    "source_job_id": self.parent_registry.job_id,
                },
            )
        except Exception as exc:
            raise SourceRevisionReplayError("child_reuse_proof_publication_failed") from exc
        from dataclasses import replace

        bound_call = replace(
            call,
            verified_reuse=True,
            reuse_evidence_artifact_id=record.artifact_id,
            reuse_evidence_artifact_hash=record.content_hash,
            reuse_evidence_record_hash=hash_json(asdict(record)),
        )
        self.verify_child_reuse_proof(
            record,
            child_expected_call=bound_call,
            child_output_record=child_output_record,
            current_scope=match.scope,
            route_identity=match.route_identity,
            external_registry_resolver=self._resolve_registry,
            stage_lineage_records=stage_lineage_records,
        )
        return record

    def verified_provider_reuse_authority_v1(
        self,
        *,
        match: SourceRevisionReplayMatch,
        proof_record: ArtifactRecord,
        child_expected_call: ExpectedProviderCall | Mapping[str, Any],
        child_output_record: ArtifactRecord,
        route_identity: Sequence[str],
        stage_lineage_records: Sequence[ArtifactRecord] = (),
    ) -> Any:
        """Adapt a fully reverified child proof to the existing StagePlan reuse gate.

        The caller supplies the resolved route object's own physical identity
        tuple, child ExpectedProviderCall, and accepted output record. The
        proof is independently checked against the current match scope/route,
        child call/output, and complete parent dependency closure before the
        returned authority can cross the StagePlan reuse gate.
        """
        if not isinstance(match, SourceRevisionReplayMatch) or match._context_token is not self._context_token:
            raise SourceRevisionReplayError("reuse_match_not_from_bound_context")
        self.verify_child_reuse_proof(
            proof_record,
            child_expected_call=child_expected_call,
            child_output_record=child_output_record,
            current_scope=match.scope,
            route_identity=match.route_identity,
            stage_lineage_records=stage_lineage_records,
        )
        identity = tuple(str(value).strip() for value in route_identity)
        if (
            len(identity) < 3
            or any(not value for value in identity[:3])
            or tuple(identity[:3]) != (
                match.route_identity["provider"],
                match.route_identity["model"],
                match.route_identity["endpoint_type"],
            )
            or len(identity) != 4
            or not identity[3]
        ):
            raise SourceRevisionReplayError("verified_reuse_route_identity_mismatch")
        from runtime.provider_routes import _safe_host

        endpoint = match.route_identity["endpoint"]
        expected_host = _safe_host(endpoint)
        if not expected_host and "://" not in endpoint:
            expected_host = endpoint.strip().casefold()
        if not expected_host or identity[3] != expected_host:
            raise SourceRevisionReplayError("verified_reuse_route_identity_mismatch")
        from runtime.stage_planning import VerifiedProviderReuseAuthorityV1

        return VerifiedProviderReuseAuthorityV1(
            request_hash=match.request_hash,
            route_identity=identity,
            receipt_hash=hash_json(match.receipt.to_dict()),
            output_hash=_digest(match.receipt.response_hash, "provider response hash"),
            authority_hash=hash_json({
                "proof_artifact_id": proof_record.artifact_id,
                "proof_content_hash": proof_record.content_hash,
                "proof_record_hash": hash_json(asdict(proof_record)),
                "plan_content_hash": hash_json(self.plan_body),
            }),
        )

    def verify_child_reuse_proof(
        self,
        proof_record: ArtifactRecord,
        *,
        child_expected_call: ExpectedProviderCall | Mapping[str, Any],
        child_output_record: ArtifactRecord,
        current_scope: Mapping[str, Any],
        route_identity: Mapping[str, Any],
        external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
        stage_lineage_records: Sequence[ArtifactRecord] = (),
    ) -> None:
        """Independently verify proof bytes, child output, plan, and parent closure."""
        call = _expected_call(child_expected_call)
        if (
            proof_record.job_id != self.child_registry.job_id
            or proof_record.status != "ready"
            or proof_record.artifact_type != SOURCE_REVISION_REUSE_ARTIFACT_TYPE
            or proof_record.artifact_version != SOURCE_REVISION_REUSE_ARTIFACT_VERSION
            or self.child_registry.get(proof_record.artifact_id) != proof_record
        ):
            raise SourceRevisionReplayError("child_reuse_proof_registry_binding_invalid")
        resolver = external_registry_resolver or self._resolve_registry
        try:
            self.child_registry.verify_ready_artifact_closure(
                proof_record, external_registry_resolver=resolver,
            )
        except Exception as exc:
            raise SourceRevisionReplayError("child_reuse_proof_dependency_closure_invalid") from exc
        payload = _record_json(self.child_registry, proof_record)
        if not isinstance(payload, Mapping):
            raise SourceRevisionReplayError("child_reuse_proof_payload_invalid")
        without_hash = dict(payload)
        proof_hash = str(without_hash.pop("proof_hash", "") or "")
        if _DIGEST.fullmatch(proof_hash) is None or hash_json(without_hash) != proof_hash:
            raise SourceRevisionReplayError("child_reuse_proof_hash_mismatch")
        if (
            payload.get("artifact_type") != SOURCE_REVISION_REUSE_ARTIFACT_TYPE
            or payload.get("artifact_version") != SOURCE_REVISION_REUSE_ARTIFACT_VERSION
            or payload.get("status") != "ready"
            or payload.get("job_id") != self.child_registry.job_id
            or payload.get("plan_artifact_id") != self.plan_record.artifact_id
            or payload.get("plan_artifact_hash") != self.plan_record.content_hash
            or payload.get("plan_content_hash") != hash_json(self.plan_body)
            or self.child_runtime_spec_record is None
            or payload.get("child_runtime_spec_ref") != _record_ref(self.child_runtime_spec_record)
            or payload.get("child_call") != _semantic_call_identity(call)
            or payload.get("child_output_call") != _child_output_call_identity(call)
            or payload.get("request_hash") != call.input_hash
        ):
            raise SourceRevisionReplayError("child_reuse_proof_current_binding_mismatch")
        normalized_scope = _validate_scope(current_scope, self._source_rows)
        normalized_scope.pop("eligible", None)
        normalized_route = _route_identity(route_identity)
        if (
            normalized_scope != payload.get("scope")
            or normalized_route != payload.get("route_identity")
        ):
            raise SourceRevisionReplayError("child_reuse_proof_current_scope_mismatch")
        if (
            not call.verified_reuse
            or call.reuse_evidence_artifact_id != proof_record.artifact_id
            or call.reuse_evidence_artifact_hash != proof_record.content_hash
            or call.reuse_evidence_record_hash != hash_json(asdict(proof_record))
        ):
            raise SourceRevisionReplayError("child_reuse_proof_expected_call_reference_mismatch")
        if (
            self.child_registry.get(child_output_record.artifact_id) != child_output_record
            or payload.get("child_output_ref") != _record_ref(child_output_record)
            or child_output_record.job_id != self.child_registry.job_id
            or child_output_record.status != "ready"
            or not _path_within(child_output_record.path, Path(self.child_registry.registry_path).parent)
            or not call.artifact_path
            or not _same_path(call.artifact_path, child_output_record.path)
            or call.registry_file_hash != child_output_record.content_hash
        ):
            raise SourceRevisionReplayError("child_reuse_proof_output_reference_mismatch")
        _verify_child_output_hashes(self.child_registry, child_output_record, call)
        expected_stage_lineage_refs = [
            _record_ref(record, external=record.job_id != self.child_registry.job_id)
            for record in stage_lineage_records
        ]
        if payload.get("stage_lineage_refs") != expected_stage_lineage_refs:
            raise SourceRevisionReplayError("child_reuse_proof_stage_lineage_mismatch")
        authority = payload.get("source_authority")
        if not isinstance(authority, Mapping):
            raise SourceRevisionReplayError("child_reuse_proof_source_authority_missing")
        for field in (
            "parent_closure_ref", "parent_ledger_ref", "parent_response_snapshot_ref",
            "parent_accepted_output_ref", "parent_scope_record_ref",
        ):
            raw_ref = authority.get(field)
            if not isinstance(raw_ref, Mapping) or raw_ref.get("dependency_kind") != "external_job":
                raise SourceRevisionReplayError("child_reuse_proof_external_ref_missing", field)
        expected_dependencies = {
            (dependency.dependency_kind, dependency.job_id, dependency.artifact_id)
            for dependency in proof_record.depends_on
        }
        raw_refs = [
            authority["parent_closure_ref"], authority["parent_ledger_ref"],
            authority["parent_response_snapshot_ref"], authority["parent_accepted_output_ref"],
            authority["parent_scope_record_ref"], *list(authority.get("parent_lineage_refs") or []),
        ]
        for raw_ref in raw_refs:
            if not isinstance(raw_ref, Mapping):
                raise SourceRevisionReplayError("child_reuse_proof_external_ref_invalid")
            key = (
                str(raw_ref.get("dependency_kind") or ""),
                str(raw_ref.get("job_id") or ""),
                str(raw_ref.get("artifact_id") or ""),
            )
            if key not in expected_dependencies:
                raise SourceRevisionReplayError("child_reuse_proof_dependency_missing")

        scope_record_ref = authority.get("parent_scope_record_ref")
        if not isinstance(scope_record_ref, Mapping):
            raise SourceRevisionReplayError("child_reuse_proof_scope_ref_missing")
        scope_record = self.parent_registry.get(str(scope_record_ref.get("artifact_id") or ""))
        if scope_record is None:
            raise SourceRevisionReplayError("child_reuse_proof_scope_record_missing")
        rechecked = self._find_match(
            expected_call=call,
            request_hash=call.input_hash,
            current_scope=current_scope,
            scope_record=scope_record,
            route_identity=route_identity,
            allow_verified_reuse=True,
        )
        if rechecked.status != "match" or rechecked.match is None:
            raise SourceRevisionReplayError("child_reuse_proof_parent_authority_no_longer_matches")
        actual = rechecked.match
        actual_authority = {
            "parent_job_id": self.parent_registry.job_id,
            "parent_registry_path": self.parent_registry.registry_path,
            "parent_closure_ref": _record_ref(actual.parent_closure, external=True),
            "parent_ledger_ref": _record_ref(actual.parent_ledger, external=True),
            "parent_response_snapshot_ref": _record_ref(actual.response_snapshot, external=True),
            "parent_accepted_output_ref": _record_ref(actual.accepted_output, external=True),
            "parent_scope_record_ref": _record_ref(actual.parent_scope_record, external=True),
            "parent_lineage_refs": [
                _record_ref(record, external=True) for record in actual.parent_lineage_records
            ],
            "receipt_id": actual.receipt.receipt_id,
            "receipt_hash": hash_json(actual.receipt.to_dict()),
            "receipt_response_hash": actual.receipt.response_hash,
            "parent_call_id": str(actual.parent_expected_call.get("call_id") or ""),
            "parent_expected_call_hash": hash_json(actual.parent_expected_call),
        }
        if dict(authority) != actual_authority:
            raise SourceRevisionReplayError("child_reuse_proof_source_authority_mismatch")
        required_dependencies = {
            ("local_job", self.child_registry.job_id, self.plan_record.artifact_id),
            ("local_job", self.child_registry.job_id, self.child_runtime_spec_record.artifact_id),
            ("local_job", self.child_registry.job_id, child_output_record.artifact_id),
            *{
                ("external_job", record.job_id, record.artifact_id)
                for record in (
                    actual.parent_closure, actual.parent_ledger, actual.response_snapshot,
                    actual.accepted_output, actual.parent_scope_record,
                    *actual.parent_lineage_records,
                )
            },
        }
        for record in stage_lineage_records:
            required_dependencies.add((
                "local_job" if record.job_id == self.child_registry.job_id else "external_job",
                record.job_id, record.artifact_id,
            ))
        if required_dependencies != expected_dependencies:
            raise SourceRevisionReplayError("child_reuse_proof_dependency_set_mismatch")
        resolved_parent = resolver(self.parent_registry.job_id)
        if (
            resolved_parent is None
            or resolved_parent.job_id != self.parent_registry.job_id
            or not _same_path(resolved_parent.registry_path, self.parent_registry.registry_path)
        ):
            raise SourceRevisionReplayError("child_reuse_proof_parent_registry_changed")


__all__ = [
    "SOURCE_REVISION_PLAN_ARTIFACT_ID",
    "SOURCE_REVISION_PLAN_ARTIFACT_TYPE",
    "SOURCE_REVISION_PLAN_ARTIFACT_VERSION",
    "SOURCE_REVISION_REPLAY_SCOPE_VERSION",
    "SOURCE_REVISION_REUSE_ARTIFACT_TYPE",
    "SOURCE_REVISION_REUSE_ARTIFACT_VERSION",
    "SourceRevisionReplayContext",
    "SourceRevisionReplayError",
    "SourceRevisionReplayLookup",
    "SourceRevisionReplayMatch",
    "build_outline_source_revision_scope_v1",
    "build_writer_source_revision_scope_v1",
    "writer_source_revision_scope_v1",
]

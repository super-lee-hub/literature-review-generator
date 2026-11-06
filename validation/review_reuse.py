"""Read-only verification for Writer section replay reuse."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runtime.provider_runtime import ProviderCallReceiptV1, hash_json
from services.artifact_registry import (
    ArtifactClosureBatchError,
    ArtifactRecord,
    ArtifactRegistry,
    RegistryError,
    RegistrySnapshotChanged,
    UnverifiedDependency,
    file_sha256,
)


@dataclass
class _ReviewReuseVerification:
    registry: ArtifactRegistry
    resolver: Callable[[str], ArtifactRegistry | None] | None
    revision: int
    records: dict[str, ArtifactRecord]
    roots: dict[str, ArtifactRecord] = field(default_factory=dict)
    source_revision_context: Any | None = None

    def finish(self) -> None:
        try:
            if self.roots:
                self.registry.verify_ready_artifact_closures(
                    self.roots.values(),
                    external_registry_resolver=self.resolver,
                    expected_record_snapshot=(self.revision, self.records),
                )
            revision, records = self.registry.capture_record_snapshot()
            if (revision != self.revision
                    or {record.artifact_id: record for record in records} != self.records):
                raise RegistrySnapshotChanged("Writer replay Registry snapshot changed during verification")
        except ArtifactClosureBatchError as exc:
            if isinstance(exc.original_error, RegistryError):
                raise exc.original_error from exc
            raise UnverifiedDependency(f"Writer replay closure proof failed: {exc}") from exc
        except RegistryError:
            raise
        except Exception as exc:
            # The original per-call closure checks also converted resolver and
            # file-read failures to blocking findings. Deferred proof must do so.
            raise UnverifiedDependency(f"Writer replay closure proof failed: {exc}") from exc


_REVIEW_REUSE_VERIFICATION: ContextVar[_ReviewReuseVerification | None] = ContextVar(
    "review_reuse_verification", default=None,
)


@contextmanager
def review_reuse_verification_scope(
    registry: ArtifactRegistry,
    *,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> Iterator[None]:
    """Keep per-call bindings, then prove their shared graph before returning."""
    revision, records = registry.capture_record_snapshot()
    proof = _ReviewReuseVerification(
        registry, external_registry_resolver, revision,
        {record.artifact_id: record for record in records},
    )
    token = _REVIEW_REUSE_VERIFICATION.set(proof)
    try:
        yield
        proof.finish()
    finally:
        _REVIEW_REUSE_VERIFICATION.reset(token)


def _verify_reuse_artifact_closure(
    registry: ArtifactRegistry,
    record: ArtifactRecord,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> None:
    proof = _REVIEW_REUSE_VERIFICATION.get()
    if (proof is None or proof.registry is not registry
            or proof.resolver is not external_registry_resolver):
        registry.verify_ready_artifact_closure(
            record, external_registry_resolver=external_registry_resolver,
        )
        return
    if record != proof.records.get(record.artifact_id):
        raise RegistrySnapshotChanged(f"Writer replay Registry record changed: {record.artifact_id}")
    if record.status != "ready" or record.job_id != registry.job_id:
        raise UnverifiedDependency(f"Writer replay record is not local and READY: {record.artifact_id}")
    registry._verify_ready_artifact(record)
    proof.roots[record.artifact_id] = record


def _issue(reason: str, stage: str, call_id: str) -> str:
    return f"provider_closure_review_reuse_{reason}:{stage}:{call_id}"


def _string(value: Any) -> str:
    return str(value or "")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _record_is(
    record: ArtifactRecord,
    *,
    artifact_id: str,
    artifact_role: str,
    artifact_type: str,
    artifact_version: str,
    job_id: str,
    registry: ArtifactRegistry,
) -> bool:
    return (
        record.artifact_id == artifact_id
        and record.artifact_role == artifact_role
        and record.artifact_type == artifact_type
        and record.artifact_version == artifact_version
        and record.job_id == job_id == registry.job_id
        and record.status == "ready"
        and bool(record.path)
        and _is_sha256(record.content_hash)
    )


def _passed_dependency(
    record: ArtifactRecord,
    dependency_records: Sequence[ArtifactRecord],
) -> bool:
    matches = [item for item in dependency_records if item.artifact_id == record.artifact_id]
    return len(matches) == 1 and all(
        getattr(matches[0], field_name) == getattr(record, field_name)
        for field_name in (
            "artifact_id",
            "artifact_role",
            "artifact_type",
            "artifact_version",
            "path",
            "job_id",
            "status",
            "content_hash",
        )
    )


def _has_exact_local_dependency(
    owner: ArtifactRecord,
    dependency: ArtifactRecord,
) -> bool:
    matches = [
        item
        for item in owner.depends_on
        if item.artifact_id == dependency.artifact_id
    ]
    return len(matches) == 1 and all(
        getattr(matches[0], field_name) == expected
        for field_name, expected in (
            ("dependency_kind", "local_job"),
            ("job_id", dependency.job_id),
            ("artifact_id", dependency.artifact_id),
            ("artifact_type", dependency.artifact_type),
            ("path", dependency.path),
            ("content_hash", dependency.content_hash),
        )
    )


def _read_json_object(path: str) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def review_reuse_call_issues(
    *,
    stage: str,
    expected_call: Mapping[str, Any],
    registry: ArtifactRegistry,
    dependency_records: Sequence[ArtifactRecord],
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> list[str]:
    """Verify a saved Writer replay against its ledger, snapshot, and section.

    The closure epoch and call-graph identity checked here belong to the
    original Writer call.  This verifier deliberately has no dependency on
    the current stage closure's epoch or graph.
    """

    call_id = _string(expected_call.get("call_id")) or "unknown"
    issues: list[str] = []

    def fail(reason: str) -> list[str]:
        return [_issue(reason, stage, call_id)]

    if expected_call.get("verified_reuse") is not True:
        return fail("call_not_marked_reuse")

    source_proof = registry.get(str(expected_call.get("reuse_evidence_artifact_id") or ""))
    if source_proof is not None and source_proof.artifact_type == "source_revision_provider_reuse":
        try:
            if source_proof not in dependency_records:
                raise ValueError("SourceRevision proof is not a direct stage dependency")
            if external_registry_resolver is None:
                raise ValueError("SourceRevision proof has no parent resolver")
            from runtime.source_revision_binding import (
                open_registered_source_revision_replay_context,
                verify_registered_source_revision_reuse,
            )

            batch = _REVIEW_REUSE_VERIFICATION.get()
            context = batch.source_revision_context if batch is not None and batch.registry is registry else None
            if context is None:
                context = open_registered_source_revision_replay_context(registry, external_registry_resolver)
                if batch is not None and batch.registry is registry:
                    batch.source_revision_context = context
            verify_registered_source_revision_reuse(context, expected_call)
            _verify_reuse_artifact_closure(registry, source_proof, external_registry_resolver)
            return []
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            return fail(f"source_revision_authority_invalid:{exc}")

    job_id = _string(expected_call.get("job_id"))
    section_id = _string(expected_call.get("node_id"))
    attempt_id = _string(expected_call.get("attempt_id"))
    if (
        not job_id
        or job_id != registry.job_id
        or not section_id
        or call_id != f"review:{section_id}"
        or _string(expected_call.get("stage_name")) != "stage3_review"
        or not attempt_id
    ):
        return fail("expected_call_identity_invalid")

    evidence_id = _string(expected_call.get("reuse_evidence_artifact_id"))
    evidence_hash = _string(expected_call.get("reuse_evidence_artifact_hash"))
    row_hash = _string(expected_call.get("reuse_evidence_record_hash"))
    section_path = _string(expected_call.get("artifact_path"))
    section_file_hash = _string(expected_call.get("registry_file_hash"))
    section_payload_hash = _string(expected_call.get("artifact_payload_hash"))
    expected_response_hashes = {
        name: _string(expected_call.get(name))
        for name in (
            "provider_response_hash",
            "output_hash",
            "normalized_output_hash",
            "replay_output_hash",
        )
    }
    expected_section_hashes = {
        name: _string(expected_call.get(name))
        for name in (
            "artifact_content_hash",
            "registered_artifact_hash",
            "node_output_hash",
        )
    }
    expected_graph_hash = _string(expected_call.get("expected_call_graph_hash"))
    original_epoch = _string(expected_call.get("closure_epoch_id"))
    original_identity = _string(expected_call.get("logical_attempt_identity"))
    if (
        evidence_id != "review_replay"
        or not _is_sha256(evidence_hash)
        or not _is_sha256(row_hash)
        or not section_path
        or not _is_sha256(section_file_hash)
        or not _is_sha256(section_payload_hash)
        or not all(_is_sha256(value) for value in expected_response_hashes.values())
        or not all(_is_sha256(value) for value in expected_section_hashes.values())
        or not _is_sha256(expected_graph_hash)
        or not original_epoch
        or not original_identity
    ):
        return fail("expected_reuse_evidence_incomplete")

    try:
        replay_record = registry.get(evidence_id)
    except Exception:
        return fail("replay_registry_unreadable")
    if replay_record is None or not _record_is(
        replay_record,
        artifact_id="review_replay",
        artifact_role="review_replay",
        artifact_type="review_replay_ledger",
        artifact_version="v1",
        job_id=job_id,
        registry=registry,
    ):
        return fail("replay_registry_record_invalid")
    if (
        replay_record.content_hash != evidence_hash
        or not _passed_dependency(replay_record, dependency_records)
    ):
        return fail("replay_registry_dependency_mismatch")
    try:
        _verify_reuse_artifact_closure(
            registry, replay_record,
            external_registry_resolver=external_registry_resolver,
        )
    except Exception:
        return fail("replay_registry_closure_invalid")

    replay_path = Path(replay_record.path)
    try:
        if file_sha256(replay_path) != evidence_hash:
            return fail("replay_file_hash_mismatch")
        replay_lines = replay_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError, TypeError, ValueError):
        return fail("replay_file_unreadable")

    replay_rows: list[Mapping[str, Any]] = []
    for line in replay_lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return fail("replay_row_invalid_json")
        if not isinstance(row, Mapping):
            return fail("replay_row_invalid_shape")
        replay_rows.append(row)

    matching_rows = [
        row
        for row in replay_rows
        if _string(row.get("section_id")) == section_id
        and _string(row.get("artifact_path")) == section_path
        and _string(row.get("registry_file_hash")) == section_file_hash
        and _string(row.get("artifact_content_hash")) == section_payload_hash
        and _string(row.get("expected_call_graph_hash")) == expected_graph_hash
        and _string(row.get("closure_epoch_id")) == original_epoch
        and _string(row.get("logical_attempt_identity")) == original_identity
    ]
    if len(matching_rows) != 1:
        return fail("replay_exact_row_missing_or_ambiguous")
    replay = matching_rows[0]
    if hash_json(replay) != row_hash:
        return fail("replay_exact_row_hash_mismatch")
    if (
        _string(replay.get("replay_version")) != "review-section-replay-v1"
        or _string(replay.get("job_id")) != job_id
        or _string(replay.get("stage_name")) != "stage3_review"
        or _string(replay.get("section_id")) != section_id
        or _string(replay.get("expected_call_graph_hash")) != expected_graph_hash
        or _string(replay.get("closure_epoch_id")) != original_epoch
        or _string(replay.get("logical_attempt_identity")) != original_identity
    ):
        return fail("replay_original_identity_mismatch")

    section_artifact_id = _string(replay.get("artifact_id"))
    binding_hash = _string(replay.get("binding_hash"))
    receipt_id = _string(replay.get("receipt_id"))
    receipt_hash = _string(replay.get("receipt_hash"))
    snapshot_id = _string(replay.get("receipt_snapshot_artifact_id"))
    snapshot_path = _string(replay.get("receipt_snapshot_path"))
    snapshot_hash = _string(replay.get("receipt_snapshot_content_hash"))
    normalized_output_hash = _string(replay.get("normalized_output_hash"))
    if (
        not section_artifact_id
        or not _is_sha256(binding_hash)
        or not receipt_id
        or not _is_sha256(receipt_hash)
        or not snapshot_id
        or not snapshot_path
        or not _is_sha256(snapshot_hash)
        or not _is_sha256(normalized_output_hash)
        or _string(replay.get("artifact_path")) != section_path
        or _string(replay.get("registry_file_hash")) != section_file_hash
        or _string(replay.get("artifact_content_hash")) != section_payload_hash
    ):
        return fail("replay_record_incomplete")

    safe_section_id = "".join(
        char if char.isalnum() or char in {"-", "_"} else "_"
        for char in section_id
    )
    if section_artifact_id != f"review-section:{section_id}:{binding_hash[:24]}":
        return fail("section_artifact_id_invalid")
    try:
        section_record = registry.get(section_artifact_id)
        snapshot_record = registry.get(snapshot_id)
    except Exception:
        return fail("dependency_registry_unreadable")
    if section_record is None or not _record_is(
        section_record,
        artifact_id=section_artifact_id,
        artifact_role="review_section",
        artifact_type="review_section",
        artifact_version="v3",
        job_id=job_id,
        registry=registry,
    ):
        return fail("section_registry_record_invalid")
    if (
        section_record.path != section_path
        or section_record.content_hash != section_file_hash
        or not _passed_dependency(section_record, dependency_records)
        or not _has_exact_local_dependency(replay_record, section_record)
    ):
        return fail("section_registry_dependency_mismatch")

    if snapshot_record is None or not _record_is(
        snapshot_record,
        artifact_id=snapshot_id,
        artifact_role="provider_receipt_snapshot",
        artifact_type="review_provider_receipt_snapshot",
        artifact_version="v1",
        job_id=job_id,
        registry=registry,
    ):
        return fail("snapshot_registry_record_invalid")
    if (
        snapshot_record.path != snapshot_path
        or snapshot_record.content_hash != snapshot_hash
        or not _passed_dependency(snapshot_record, dependency_records)
        or not _has_exact_local_dependency(replay_record, snapshot_record)
    ):
        return fail("snapshot_registry_dependency_mismatch")

    if snapshot_id != (
        f"review:receipt-snapshot:{safe_section_id}:"
        f"{binding_hash[:24]}:{receipt_hash[:24]}"
    ):
        return fail("snapshot_artifact_id_invalid")

    try:
        _verify_reuse_artifact_closure(
            registry, section_record,
            external_registry_resolver=external_registry_resolver,
        )
        _verify_reuse_artifact_closure(
            registry, snapshot_record,
            external_registry_resolver=external_registry_resolver,
        )
    except Exception:
        return fail("dependency_registry_closure_invalid")

    section_envelope = _read_json_object(section_record.path)
    if section_envelope is None:
        return fail("section_file_unreadable")
    section = section_envelope.get("section")
    binding = section_envelope.get("binding")
    if not isinstance(section, Mapping) or not isinstance(binding, Mapping):
        return fail("section_envelope_invalid")
    logical_section_hash = hash_json(section)
    if (
        _string(section_envelope.get("artifact_type")) != "review_section"
        or _string(section_envelope.get("artifact_version")) != "v3"
        or _string(section_envelope.get("status")) != "ready"
        or _string(section_envelope.get("job_id")) != job_id
        or _string(section_envelope.get("section_id")) != section_id
        or _string(section_envelope.get("binding_hash")) != binding_hash
        or _string(section_record.metadata.get("binding_hash")) != binding_hash
        or hash_json(binding) != binding_hash
        or _string(section_envelope.get("content_hash")) != logical_section_hash
        or logical_section_hash != section_payload_hash
        or _string(replay.get("artifact_content_hash")) != logical_section_hash
    ):
        return fail("section_logical_hash_or_binding_mismatch")

    if (
        _string(binding.get("stage_name")) != "stage3_review"
        or _string(binding.get("section_id")) != section_id
        or _string(binding.get("prompt_payload_hash")) != _string(expected_call.get("input_hash"))
        or _string(binding.get("prompt_hash")) != _string(expected_call.get("prompt_hash"))
        or _string(binding.get("writer_config_hash")) != _string(expected_call.get("config_hash"))
        or _string(binding.get("output_schema_hash")) != _string(expected_call.get("schema_hash"))
    ):
        return fail("request_binding_mismatch")
    if not isinstance(section.get("provider_receipt_ids"), list) or receipt_id not in section[
        "provider_receipt_ids"
    ]:
        return fail("section_receipt_reference_missing")

    snapshot = _read_json_object(snapshot_record.path)
    if snapshot is None:
        return fail("snapshot_file_unreadable")
    receipt_payload = snapshot.get("receipt")
    if not isinstance(receipt_payload, Mapping):
        return fail("snapshot_receipt_invalid")
    if (
        _string(snapshot.get("artifact_type")) != "review_provider_receipt_snapshot"
        or _string(snapshot.get("artifact_version")) != "v1"
        or _string(snapshot.get("status")) != "partial"
        or _string(snapshot.get("job_id")) != job_id
        or _string(snapshot.get("stage_name")) != "stage3_review"
        or _string(snapshot.get("section_id")) != section_id
        or _string(snapshot.get("binding_hash")) != binding_hash
        or _string(snapshot.get("closure_epoch_id")) != original_epoch
        or _string(snapshot.get("logical_attempt_identity")) != original_identity
        or _string(snapshot.get("expected_call_graph_hash")) != expected_graph_hash
        or _string(snapshot.get("call_id")) != call_id
        or _string(snapshot.get("receipt_id")) != receipt_id
        or _string(snapshot.get("receipt_hash")) != receipt_hash
        or hash_json(receipt_payload) != receipt_hash
    ):
        return fail("snapshot_receipt_binding_mismatch")

    try:
        receipt = ProviderCallReceiptV1.from_dict(receipt_payload)
    except Exception:
        return fail("receipt_schema_invalid")
    expected_receipt_values = (
        ("artifact_type", "provider_call_receipt"),
        ("artifact_version", "v2"),
        ("receipt_id", receipt_id),
        ("job_id", job_id),
        ("attempt_id", attempt_id),
        ("stage_name", "stage3_review"),
        ("node_id", section_id),
        ("call_id", call_id),
        ("closure_epoch_id", original_epoch),
        ("logical_attempt_identity", original_identity),
        ("prompt_hash", _string(expected_call.get("prompt_hash"))),
        ("input_hash", _string(expected_call.get("input_hash"))),
        ("config_hash", _string(expected_call.get("config_hash"))),
        ("schema_hash", _string(expected_call.get("schema_hash"))),
        ("prompt_id", _string(expected_call.get("prompt_id"))),
        ("prompt_version", _string(expected_call.get("prompt_version"))),
        ("prompt_sha256", _string(expected_call.get("prompt_sha256"))),
        ("route", "Writer_API"),
    )
    for field_name, wanted in expected_receipt_values:
        if wanted and _string(getattr(receipt, field_name)) != wanted:
            issues.append(_issue(f"receipt_{field_name}_mismatch", stage, call_id))
    optional_receipt_values = (
        ("provider", expected_call.get("provider")),
        ("model", expected_call.get("model")),
        ("endpoint", expected_call.get("endpoint")),
        ("endpoint_type", expected_call.get("endpoint_type")),
    )
    for field_name, raw_wanted in optional_receipt_values:
        wanted = _string(raw_wanted)
        if wanted and _string(getattr(receipt, field_name)) != wanted:
            issues.append(_issue(f"receipt_{field_name}_mismatch", stage, call_id))
    bound_model = _string(binding.get("writer_model"))
    bound_endpoint_type = _string(binding.get("writer_endpoint"))
    bound_provider = _string(binding.get("writer_provider"))
    if bound_model and receipt.model != bound_model:
        issues.append(_issue("receipt_model_binding_mismatch", stage, call_id))
    if bound_endpoint_type and receipt.endpoint_type != bound_endpoint_type:
        issues.append(_issue("receipt_endpoint_type_binding_mismatch", stage, call_id))
    if bound_provider and bound_provider != "configured" and receipt.provider != bound_provider:
        issues.append(_issue("receipt_provider_binding_mismatch", stage, call_id))

    if (
        receipt.status != "success"
        or receipt.incomplete_reason
        or receipt.finish_reason == "length"
        or receipt.test_only is not False
    ):
        issues.append(_issue("receipt_not_production_success", stage, call_id))
    try:
        max_attempts = int(expected_call.get("max_attempts") or 0)
    except (TypeError, ValueError):
        max_attempts = 0
    if max_attempts < 1 or receipt.attempts > max_attempts:
        issues.append(_issue("receipt_attempt_limit_mismatch", stage, call_id))
    if bool(expected_call.get("usage_required")) and receipt.usage_status not in {
        "reported",
        "provider_not_supported",
    }:
        issues.append(_issue("receipt_usage_incomplete", stage, call_id))

    ledger_matches = [
        item
        for item in dependency_records
        if item.artifact_id == "review_provider_receipts"
    ]
    if len(ledger_matches) != 1:
        return issues + [_issue("receipt_ledger_dependency_missing_or_ambiguous", stage, call_id)]
    ledger_record = ledger_matches[0]
    if not _record_is(
        ledger_record,
        artifact_id="review_provider_receipts",
        artifact_role="provider_receipts",
        artifact_type="provider_receipt_ledger",
        artifact_version="v1",
        job_id=job_id,
        registry=registry,
    ):
        return issues + [_issue("receipt_ledger_registry_record_invalid", stage, call_id)]
    try:
        registered_ledger = registry.get("review_provider_receipts")
        if (
            registered_ledger is None
            or not _passed_dependency(registered_ledger, dependency_records)
            or registered_ledger.path != ledger_record.path
            or registered_ledger.content_hash != ledger_record.content_hash
        ):
            return issues + [_issue("receipt_ledger_registry_dependency_mismatch", stage, call_id)]
        _verify_reuse_artifact_closure(
            registry, registered_ledger,
            external_registry_resolver=external_registry_resolver,
        )
        if file_sha256(registered_ledger.path) != registered_ledger.content_hash:
            return issues + [_issue("receipt_ledger_file_hash_mismatch", stage, call_id)]
        ledger_lines = Path(registered_ledger.path).read_text(encoding="utf-8").splitlines()
    except Exception:
        return issues + [_issue("receipt_ledger_closure_or_file_invalid", stage, call_id)]
    ledger_receipt_rows: list[Mapping[str, Any]] = []
    for line in ledger_lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return issues + [_issue("receipt_ledger_row_invalid_json", stage, call_id)]
        if not isinstance(row, Mapping):
            return issues + [_issue("receipt_ledger_row_invalid_shape", stage, call_id)]
        if _string(row.get("receipt_id")) == receipt_id:
            ledger_receipt_rows.append(row)
    if (
        len(ledger_receipt_rows) != 1
        or hash_json(ledger_receipt_rows[0]) != receipt_hash
        or _string(ledger_receipt_rows[0].get("job_id")) != job_id
        or _string(ledger_receipt_rows[0].get("stage_name")) != "stage3_review"
        or _string(ledger_receipt_rows[0].get("call_id")) != call_id
    ):
        issues.append(_issue("snapshot_receipt_ledger_row_mismatch", stage, call_id))

    response_hash = _string(receipt.response_hash)
    if (
        not _is_sha256(response_hash)
        or response_hash != normalized_output_hash
        or response_hash != _string(replay.get("normalized_output_hash"))
    ):
        issues.append(_issue("normalized_output_hash_mismatch", stage, call_id))
    for field_name, expected_hash in expected_response_hashes.items():
        if expected_hash != response_hash:
            issues.append(_issue(f"expected_{field_name}_mismatch", stage, call_id))
    for field_name, expected_hash in expected_section_hashes.items():
        if expected_hash != logical_section_hash:
            issues.append(_issue(f"expected_{field_name}_mismatch", stage, call_id))

    return issues

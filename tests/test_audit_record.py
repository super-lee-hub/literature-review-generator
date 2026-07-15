from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path

import pytest

from services.audit_record import (
    AuditArtifactRefV1,
    AuditRecordCollisionError,
    AuditRecordIntegrityError,
    AuditRecordV1,
    AuditSecretDetected,
    assert_no_audit_id_collisions,
)
from services.artifact_registry import ArtifactRegistry


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def _record(*, audit_id: str = "audit-1", reason: str = "selected exact candidate") -> AuditRecordV1:
    return AuditRecordV1.create(
        audit_id=audit_id,
        audit_type="identity_override",
        job_id="job-123",
        attempt_id="attempt-2",
        producer="runtime.identity_gate",
        actor="researcher@example.test",
        reason=reason,
        scope={"candidate_hash": HASH_A, "identity_verdict": "ambiguous"},
        target_artifacts=[
            AuditArtifactRefV1(
                artifact_id="source_inventory",
                content_hash=HASH_A,
                job_id="job-123",
                artifact_type="source_inventory",
            )
        ],
        input_artifact_refs=[
            AuditArtifactRefV1(artifact_id="zotero_report", content_hash=HASH_B, job_id="job-123")
        ],
        output_artifact_refs=[
            AuditArtifactRefV1(artifact_id="identity_decision", content_hash=HASH_C, job_id="job-123")
        ],
        input_hashes={"candidate": HASH_A, "report": HASH_B},
        policy_snapshot={"allow_ambiguous_exact_candidate": True, "mismatch_override": False},
        disposition="approved",
        created_at="2026-07-13T00:00:00Z",
    )


@pytest.mark.parametrize(
    "audit_type",
    [
        "identity_override",
        "legacy_reuse",
        "outline_manual_adoption",
        "dependency_force_delete",
        "artifact_quarantine_release",
    ],
)
def test_audit_record_supports_all_public_audit_types(audit_type: str) -> None:
    payload = _record().to_dict()
    payload["audit_type"] = audit_type
    payload.pop("record_hash")
    restored = AuditRecordV1.from_dict(payload)
    assert restored.audit_type == audit_type


def test_audit_record_is_immutable_and_round_trip_hash_is_stable() -> None:
    record = _record()
    payload = json.loads(json.dumps(record.to_dict()))
    restored = AuditRecordV1.from_dict(payload)

    assert restored == record
    assert restored.record_hash == record.record_hash
    with pytest.raises(FrozenInstanceError):
        record.reason = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        record.policy_snapshot["mismatch_override"] = True  # type: ignore[index]


def test_audit_record_round_trips_through_artifact_registry(tmp_path: Path) -> None:
    original = _record()
    audit_path = tmp_path / "audit_record_v1.json"
    audit_path.write_text(json.dumps(original.to_dict(), ensure_ascii=False), encoding="utf-8")
    registry_path = tmp_path / "artifact_registry.json"
    registry = ArtifactRegistry(registry_path, original.job_id)

    registered = registry.register_file(
        artifact_id=original.audit_id,
        artifact_role="audit",
        artifact_type="audit_record",
        artifact_version="v1",
        path=audit_path,
        producer=original.producer,
    )
    reloaded = ArtifactRegistry(registry_path, original.job_id)
    restored = AuditRecordV1.from_dict(json.loads(Path(registered.path).read_text(encoding="utf-8")))

    assert reloaded.get(original.audit_id) == registered
    assert restored == original
    assert restored.record_hash == original.record_hash


def test_audit_record_hash_detects_tampering() -> None:
    payload = _record().to_dict()
    payload["reason"] = "silently changed"
    with pytest.raises(AuditRecordIntegrityError, match="hash"):
        AuditRecordV1.from_dict(payload)


def test_audit_id_collision_detection_allows_idempotent_duplicate_only() -> None:
    original = _record()
    identical = AuditRecordV1.from_dict(original.to_dict())
    assert_no_audit_id_collisions([original, identical])

    conflicting = _record(reason="different decision")
    with pytest.raises(AuditRecordCollisionError, match="multiple immutable records"):
        assert_no_audit_id_collisions([original, conflicting])


@pytest.mark.parametrize(
    "policy_snapshot",
    [
        {"api_key": "super-secret-value"},
        {"nested": {"access_token": "token-material"}},
        {"route": "Bearer abcdefghijklmnop"},
        {"provider": "sk-abcdefghijklmnop"},
        {"endpoint": "https://example.test/run?api_key=abcdefghijklmnop"},
    ],
)
def test_audit_record_rejects_secret_material(policy_snapshot: dict[str, object]) -> None:
    with pytest.raises(AuditSecretDetected):
        AuditRecordV1.create(
            audit_type="legacy_reuse",
            job_id="job-123",
            attempt_id="attempt-1",
            producer="runtime",
            actor="operator",
            reason="explicit legacy artifact reuse",
            scope={"artifact": "summary"},
            target_artifacts=[AuditArtifactRefV1("summary", HASH_A)],
            policy_snapshot=policy_snapshot,
            disposition="approved",
        )


def test_audit_record_allows_boolean_secret_presence_flags_without_secret_value() -> None:
    record = AuditRecordV1.create(
        audit_type="legacy_reuse",
        job_id="job-123",
        attempt_id="attempt-1",
        producer="runtime",
        actor="operator",
        reason="explicit legacy artifact reuse",
        scope={"artifact": "summary"},
        target_artifacts=[AuditArtifactRefV1("summary", HASH_A)],
        policy_snapshot={"api_key_present": True},
        disposition="approved",
    )
    assert record.policy_snapshot["api_key_present"] is True


def test_audit_refs_reject_paths_and_claim_level_validation_payloads() -> None:
    with pytest.raises(ValueError, match="identity only"):
        AuditArtifactRefV1.from_dict(
            {
                "artifact_id": "validation",
                "content_hash": HASH_A,
                "path": "C:/sensitive/workspace/validation.json",
                "claim_verdict": "unsupported",
            }
        )


def test_audit_record_rejects_embedded_claim_level_conclusions() -> None:
    with pytest.raises(ValueError, match="ValidationRunResultV1"):
        AuditRecordV1.create(
            audit_type="outline_manual_adoption",
            job_id="job-123",
            attempt_id="attempt-1",
            producer="outline.adoption",
            actor="operator",
            reason="manual review completed",
            scope={"claim_results": [{"claim_verdict": "unsupported"}]},
            target_artifacts=[AuditArtifactRefV1("outline", HASH_A)],
            disposition="approved",
        )


def test_audit_reader_rejects_non_object_reference_entries() -> None:
    payload = _record().to_dict()
    payload["target_artifacts"] = ["source_inventory"]
    payload.pop("record_hash")
    with pytest.raises(ValueError, match="reference objects"):
        AuditRecordV1.from_dict(payload)

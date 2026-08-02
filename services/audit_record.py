from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Iterable, Literal, Mapping, Sequence, Tuple, cast
import uuid

from services.job_workspace import utc_now_iso


AUDIT_SCHEMA_VERSION = "v1"
AuditType = Literal[
    "identity_override",
    "outline_manual_adoption",
    "dependency_force_delete",
    "artifact_quarantine_release",
]

_AUDIT_TYPES = frozenset(
    {
        "identity_override",
        "outline_manual_adoption",
        "dependency_force_delete",
        "artifact_quarantine_release",
    }
)
_SHA256_RE = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")
_CREDENTIAL_VALUE_RE = re.compile(
    r"(?:\bBearer\s+[A-Za-z0-9._~+/=-]{12,}|\bBasic\s+[A-Za-z0-9+/=]{12,}|\bsk-[A-Za-z0-9_-]{12,}|(?:api[_-]?key|access[_-]?token|password|secret)=[^&\s]{8,})",
    flags=re.IGNORECASE,
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|_)(?:api_?key|access_?token|refresh_?token|password|passwd|secret|authorization|cookie|credential)s?(?:$|_)",
    flags=re.IGNORECASE,
)
_CLAIM_RESULT_KEYS = frozenset(
    {
        "claim_verdict",
        "claim_verdicts",
        "claim_result",
        "claim_results",
        "validation_conclusion",
        "validation_results",
    }
)


class AuditRecordError(ValueError):
    """Base error for invalid or unsafe audit records."""


class AuditRecordIntegrityError(AuditRecordError):
    """Raised when a serialized record's declared hash is incorrect."""


class AuditRecordCollisionError(AuditRecordError):
    """Raised when one audit ID identifies different immutable content."""


class AuditSecretDetected(AuditRecordError):
    """Raised when a record would persist credential material."""


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise AuditRecordError(f"value is not JSON serializable: {type(value).__name__}")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _canonical_json(payload: Any) -> str:
    try:
        return json.dumps(
            _thaw_json(_freeze_json(payload)),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AuditRecordError(f"payload is not canonical JSON: {exc}") from exc


def _audit_hash(payload: Mapping[str, Any]) -> str:
    encoded = f"auto-generate\x00audit-record-v1\x00{_canonical_json(payload)}".encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_hash(value: str, *, field_name: str) -> str:
    normalized = value.strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise AuditRecordError(f"{field_name} must be a SHA-256 digest")
    return normalized.removeprefix("sha256:")


def _scan_for_secrets(value: Any, *, path: str = "record") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if _SENSITIVE_KEY_RE.search(key_text) and item not in (None, "", False, True):
                raise AuditSecretDetected(f"credential-bearing field is forbidden at {path}.{key_text}")
            _scan_for_secrets(item, path=f"{path}.{key_text}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _scan_for_secrets(item, path=f"{path}[{index}]")
        return
    if isinstance(value, str) and _CREDENTIAL_VALUE_RE.search(value):
        raise AuditSecretDetected(f"credential-like value is forbidden at {path}")


def _scan_for_claim_results(value: Any, *, path: str = "record") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).strip().lower()
            if key_text in _CLAIM_RESULT_KEYS:
                raise AuditRecordError(
                    f"claim-level validation conclusions belong in ValidationRunResultV1, not {path}.{key}"
                )
            _scan_for_claim_results(item, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _scan_for_claim_results(item, path=f"{path}[{index}]")


@dataclass(frozen=True)
class AuditArtifactRefV1:
    artifact_id: str
    content_hash: str
    job_id: str = ""
    artifact_type: str = ""

    def __post_init__(self) -> None:
        if not self.artifact_id.strip():
            raise AuditRecordError("artifact_id is required in audit references")
        object.__setattr__(
            self,
            "content_hash",
            _normalize_hash(self.content_hash, field_name=f"content_hash for {self.artifact_id}"),
        )

    def to_dict(self) -> dict[str, str]:
        payload = {"artifact_id": self.artifact_id, "content_hash": self.content_hash}
        if self.job_id:
            payload["job_id"] = self.job_id
        if self.artifact_type:
            payload["artifact_type"] = self.artifact_type
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AuditArtifactRefV1":
        forbidden = set(payload).intersection({"path", "claim", "claim_verdict", "validation_result"})
        if forbidden:
            raise AuditRecordError(
                f"audit artifact references may contain identity only; forbidden keys: {sorted(forbidden)}"
            )
        return cls(
            artifact_id=str(payload.get("artifact_id") or ""),
            content_hash=str(payload.get("content_hash") or ""),
            job_id=str(payload.get("job_id") or ""),
            artifact_type=str(payload.get("artifact_type") or ""),
        )


def _normalize_refs(values: Iterable[AuditArtifactRefV1 | Mapping[str, Any]]) -> Tuple[AuditArtifactRefV1, ...]:
    refs = tuple(
        value if isinstance(value, AuditArtifactRefV1) else AuditArtifactRefV1.from_dict(value)
        for value in values
    )
    identities = [(item.job_id, item.artifact_id, item.content_hash) for item in refs]
    if len(set(identities)) != len(identities):
        raise AuditRecordError("audit artifact references must be unique")
    return refs


def _normalize_input_hashes(values: Mapping[str, Any]) -> Mapping[str, str]:
    normalized: dict[str, str] = {}
    for key, value in sorted(values.items(), key=lambda pair: str(pair[0])):
        name = str(key).strip()
        if not name:
            raise AuditRecordError("input hash names are required")
        normalized[name] = _normalize_hash(str(value), field_name=f"input_hashes.{name}")
    return MappingProxyType(normalized)


@dataclass(frozen=True)
class AuditRecordV1:
    audit_id: str
    schema_version: str
    audit_type: AuditType
    job_id: str
    attempt_id: str
    producer: str
    actor: str
    reason: str
    scope: Mapping[str, Any]
    target_artifacts: Tuple[AuditArtifactRefV1, ...]
    input_artifact_refs: Tuple[AuditArtifactRefV1, ...]
    output_artifact_refs: Tuple[AuditArtifactRefV1, ...]
    input_hashes: Mapping[str, str]
    policy_snapshot: Mapping[str, Any]
    disposition: str
    created_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", _freeze_json(self.scope))
        object.__setattr__(self, "target_artifacts", _normalize_refs(self.target_artifacts))
        object.__setattr__(self, "input_artifact_refs", _normalize_refs(self.input_artifact_refs))
        object.__setattr__(self, "output_artifact_refs", _normalize_refs(self.output_artifact_refs))
        object.__setattr__(self, "input_hashes", _normalize_input_hashes(self.input_hashes))
        object.__setattr__(self, "policy_snapshot", _freeze_json(self.policy_snapshot))
        self.validate()

    def validate(self) -> None:
        if self.schema_version != AUDIT_SCHEMA_VERSION:
            raise AuditRecordError(f"unsupported audit schema version: {self.schema_version}")
        if self.audit_type not in _AUDIT_TYPES:
            raise AuditRecordError(f"unsupported audit type: {self.audit_type}")
        required_text = {
            "audit_id": self.audit_id,
            "job_id": self.job_id,
            "attempt_id": self.attempt_id,
            "producer": self.producer,
            "actor": self.actor,
            "reason": self.reason,
            "disposition": self.disposition,
            "created_at": self.created_at,
        }
        missing = [name for name, value in required_text.items() if not value.strip()]
        if missing:
            raise AuditRecordError(f"required audit fields are empty: {', '.join(missing)}")
        if not self.target_artifacts:
            raise AuditRecordError("target_artifacts must identify at least one audited artifact")
        _scan_for_secrets(self._hash_payload())
        _scan_for_claim_results(self._hash_payload())

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "schema_version": self.schema_version,
            "audit_type": self.audit_type,
            "job_id": self.job_id,
            "attempt_id": self.attempt_id,
            "producer": self.producer,
            "actor": self.actor,
            "reason": self.reason,
            "scope": _thaw_json(self.scope),
            "target_artifacts": [item.to_dict() for item in self.target_artifacts],
            "input_artifact_refs": [item.to_dict() for item in self.input_artifact_refs],
            "output_artifact_refs": [item.to_dict() for item in self.output_artifact_refs],
            "input_hashes": dict(self.input_hashes),
            "policy_snapshot": _thaw_json(self.policy_snapshot),
            "disposition": self.disposition,
            "created_at": self.created_at,
        }

    @property
    def record_hash(self) -> str:
        return _audit_hash(self._hash_payload())

    def to_dict(self) -> dict[str, Any]:
        return {**self._hash_payload(), "record_hash": self.record_hash}

    @classmethod
    def create(
        cls,
        *,
        audit_type: AuditType,
        job_id: str,
        attempt_id: str,
        producer: str,
        actor: str,
        reason: str,
        scope: Mapping[str, Any],
        target_artifacts: Sequence[AuditArtifactRefV1 | Mapping[str, Any]],
        input_artifact_refs: Sequence[AuditArtifactRefV1 | Mapping[str, Any]] = (),
        output_artifact_refs: Sequence[AuditArtifactRefV1 | Mapping[str, Any]] = (),
        input_hashes: Mapping[str, str] | None = None,
        policy_snapshot: Mapping[str, Any] | None = None,
        disposition: str,
        audit_id: str | None = None,
        created_at: str | None = None,
    ) -> "AuditRecordV1":
        return cls(
            audit_id=audit_id or f"audit-{uuid.uuid4().hex}",
            schema_version=AUDIT_SCHEMA_VERSION,
            audit_type=audit_type,
            job_id=job_id,
            attempt_id=attempt_id,
            producer=producer,
            actor=actor,
            reason=reason,
            scope=scope,
            target_artifacts=_normalize_refs(target_artifacts),
            input_artifact_refs=_normalize_refs(input_artifact_refs),
            output_artifact_refs=_normalize_refs(output_artifact_refs),
            input_hashes=dict(input_hashes or {}),
            policy_snapshot=dict(policy_snapshot or {}),
            disposition=disposition,
            created_at=created_at or utc_now_iso(),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AuditRecordV1":
        def parse_refs(field_name: str) -> Tuple[AuditArtifactRefV1, ...]:
            raw = payload.get(field_name) or ()
            if isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, Sequence):
                raise AuditRecordError(f"{field_name} must be a sequence of artifact reference objects")
            if any(not isinstance(item, Mapping) for item in raw):
                raise AuditRecordError(f"{field_name} entries must be artifact reference objects")
            return tuple(AuditArtifactRefV1.from_dict(cast(Mapping[str, Any], item)) for item in raw)

        record = cls(
            audit_id=str(payload.get("audit_id") or ""),
            schema_version=str(payload.get("schema_version") or AUDIT_SCHEMA_VERSION),
            audit_type=cast(AuditType, str(payload.get("audit_type") or "")),
            job_id=str(payload.get("job_id") or ""),
            attempt_id=str(payload.get("attempt_id") or ""),
            producer=str(payload.get("producer") or ""),
            actor=str(payload.get("actor") or ""),
            reason=str(payload.get("reason") or ""),
            scope=dict(payload.get("scope") or {}),
            target_artifacts=parse_refs("target_artifacts"),
            input_artifact_refs=parse_refs("input_artifact_refs"),
            output_artifact_refs=parse_refs("output_artifact_refs"),
            input_hashes={str(key): str(value) for key, value in dict(payload.get("input_hashes") or {}).items()},
            policy_snapshot=dict(payload.get("policy_snapshot") or {}),
            disposition=str(payload.get("disposition") or ""),
            created_at=str(payload.get("created_at") or ""),
        )
        declared_hash = str(payload.get("record_hash") or "")
        if declared_hash and declared_hash != record.record_hash:
            raise AuditRecordIntegrityError("audit record hash does not match immutable content")
        return record


def assert_no_audit_id_collisions(records: Iterable[AuditRecordV1]) -> None:
    seen: dict[str, str] = {}
    for record in records:
        existing_hash = seen.get(record.audit_id)
        if existing_hash is not None and existing_hash != record.record_hash:
            raise AuditRecordCollisionError(
                f"audit_id {record.audit_id!r} identifies multiple immutable records"
            )
        seen[record.audit_id] = record.record_hash

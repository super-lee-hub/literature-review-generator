from __future__ import annotations

"""The single fail-closed completion/readiness evaluator.

All human-facing status surfaces may project this result, but they must not
invent a second success rule.  The evaluator is pure and reads only an evidence
snapshot supplied by a caller; it never writes Registry, outcomes, or pointers.
"""

from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any, Literal, Mapping, Sequence

from services.job_outcome import JobOutcomeV1
from services.job_workspace import utc_now_iso


CompletionStatus = Literal["complete", "incomplete", "blocked", "failed"]


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(f"auto-generate\x00completion-evidence\x00{encoded}".encode("utf-8")).hexdigest()


def _unique_strings(values: Sequence[Any] | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value).strip() for value in (values or ()) if str(value).strip()))


@dataclass(frozen=True)
class CompletionEvidenceV1:
    job_id: str
    job_status: str
    required_stages: tuple[str, ...] = ()
    completed_stages: tuple[str, ...] = ()
    failed_stage: str | None = None
    artifact_registry_verified: bool = False
    canonical_artifacts: Mapping[str, bool] = field(default_factory=dict)
    validation_required: bool = False
    require_clean_validation: bool = False
    allow_unvalidated_when_validation_optional: bool = False
    validation_status: str = "missing"
    provider_receipts_complete: bool = False
    provider_receipt_closure: Mapping[str, Any] | None = None
    current_stage_closure_map: Mapping[str, Any] | None = None
    declared_canonical_ready: bool | None = None
    degradation_reasons: tuple[str, ...] = ()
    evidence_sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.job_id.strip():
            raise ValueError("completion evidence job_id is required")
        object.__setattr__(self, "required_stages", _unique_strings(self.required_stages))
        object.__setattr__(self, "completed_stages", _unique_strings(self.completed_stages))
        object.__setattr__(self, "degradation_reasons", _unique_strings(self.degradation_reasons))
        object.__setattr__(self, "evidence_sources", _unique_strings(self.evidence_sources))
        object.__setattr__(self, "canonical_artifacts", {
            str(key): bool(value) for key, value in self.canonical_artifacts.items()
        })
        if isinstance(self.provider_receipt_closure, Mapping):
            object.__setattr__(
                self,
                "provider_receipts_complete",
                bool(self.provider_receipt_closure.get("complete", False)),
            )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "CompletionEvidenceV1":
        raw_canonical_artifacts = payload.get("canonical_artifacts")
        canonical_artifacts = (
            {str(key): bool(value) for key, value in raw_canonical_artifacts.items()}
            if isinstance(raw_canonical_artifacts, Mapping)
            else {}
        )
        return cls(
            job_id=str(payload.get("job_id") or ""),
            job_status=str(payload.get("job_status") or ""),
            required_stages=tuple(payload.get("required_stages") or ()),
            completed_stages=tuple(payload.get("completed_stages") or ()),
            failed_stage=str(payload.get("failed_stage") or "") or None,
            artifact_registry_verified=bool(payload.get("artifact_registry_verified", False)),
            canonical_artifacts=canonical_artifacts,
            validation_required=bool(payload.get("validation_required", False)),
            require_clean_validation=bool(payload.get("require_clean_validation", False)),
            allow_unvalidated_when_validation_optional=bool(
                payload.get("allow_unvalidated_when_validation_optional", False)
            ),
            validation_status=str(payload.get("validation_status") or "missing"),
            provider_receipts_complete=bool(payload.get("provider_receipts_complete", False)),
            provider_receipt_closure=(
                dict(payload["provider_receipt_closure"])
                if isinstance(payload.get("provider_receipt_closure"), Mapping)
                else None
            ),
            current_stage_closure_map=(
                dict(payload["current_stage_closure_map"])
                if isinstance(payload.get("current_stage_closure_map"), Mapping)
                else None
            ),
            declared_canonical_ready=(
                bool(payload["declared_canonical_ready"])
                if "declared_canonical_ready" in payload
                else None
            ),
            degradation_reasons=tuple(payload.get("degradation_reasons") or ()),
            evidence_sources=tuple(payload.get("evidence_sources") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["required_stages"] = list(self.required_stages)
        payload["completed_stages"] = list(self.completed_stages)
        payload["degradation_reasons"] = list(self.degradation_reasons)
        payload["evidence_sources"] = list(self.evidence_sources)
        return payload


@dataclass(frozen=True)
class CompletionEvaluationV1:
    evaluator_version: str
    job_id: str
    status: CompletionStatus
    canonical_ready: bool
    requires_attention: bool
    reasons: tuple[str, ...]
    evidence_hash: str
    evaluated_at: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        payload["success"] = self.canonical_ready
        return payload


class CanonicalCompletionEvaluator:
    """Evaluate one completion contract from an immutable evidence snapshot."""

    VERSION = "canonical-completion-evaluator-v1"

    @classmethod
    def evaluate(
        cls,
        evidence: CompletionEvidenceV1 | Mapping[str, Any],
    ) -> CompletionEvaluationV1:
        snapshot = evidence if isinstance(evidence, CompletionEvidenceV1) else CompletionEvidenceV1.from_mapping(evidence)
        reasons: list[str] = []

        if snapshot.declared_canonical_ready is False:
            reasons.append("declared_canonical_ready:false")
        if snapshot.failed_stage:
            reasons.append(f"failed_stage:{snapshot.failed_stage}")
        missing_stages = sorted(set(snapshot.required_stages) - set(snapshot.completed_stages))
        if missing_stages:
            reasons.append("missing_stages:" + ",".join(missing_stages))
        if snapshot.job_status in {"failed", "cancelled"}:
            reasons.append(f"job_status:{snapshot.job_status}")
        elif snapshot.job_status != "completed":
            reasons.append(f"job_status:{snapshot.job_status or 'unknown'}")
        if not snapshot.artifact_registry_verified:
            reasons.append("artifact_registry_unverified")
        for artifact_id, ready in sorted(snapshot.canonical_artifacts.items()):
            if not ready:
                reasons.append(f"canonical_artifact_unready:{artifact_id}")
        if not snapshot.provider_receipts_complete:
            reasons.append("provider_receipts_incomplete")
        if snapshot.current_stage_closure_map is not None:
            current_map = snapshot.current_stage_closure_map
            requested_map_stages = {
                str(stage).strip()
                for stage in (current_map.get("requested_stages") or ())
                if str(stage).strip() and str(stage).strip() != "source_intake"
            }
            required_outcome_stages = {
                str(stage).strip()
                for stage in snapshot.required_stages
                if str(stage).strip() and str(stage).strip() != "source_intake"
            }
            if requested_map_stages != required_outcome_stages:
                reasons.append("current_stage_closure:stage_set_mismatch")
            provider_stage_names = {"analyze", "outline", "review", "validate"}
            expected_provider_stages = required_outcome_stages & provider_stage_names
            actual_provider_stages = {
                str(stage).strip()
                for stage in (current_map.get("provider_closures_by_stage") or {})
                if str(stage).strip()
            }
            if actual_provider_stages != expected_provider_stages:
                reasons.append("current_stage_closure:provider_stage_set_mismatch")
            current_set_required = bool(current_map.get("current_set_required", True))
            if current_set_required and str(current_map.get("current_set_id") or "") == "":
                reasons.append("current_artifact_set_missing")
            for issue in current_map.get("blocking_issues") or ():
                reasons.append(f"current_stage_closure:{issue}")
        if snapshot.validation_status not in {"clean", "findings", "not_requested", "missing"}:
            reasons.append(f"validation_status_invalid:{snapshot.validation_status}")
        if snapshot.validation_required:
            if snapshot.validation_status == "missing":
                reasons.append("validation_missing")
            elif snapshot.validation_status == "not_requested":
                reasons.append("validation_not_requested:required")
            elif snapshot.require_clean_validation and snapshot.validation_status != "clean":
                reasons.append(f"validation_not_clean:{snapshot.validation_status}")
            elif snapshot.validation_status not in {"clean", "findings"}:
                reasons.append(f"validation_not_complete:{snapshot.validation_status}")
        elif snapshot.validation_status == "not_requested" and not snapshot.allow_unvalidated_when_validation_optional:
            reasons.append("validation_not_requested:policy_disallows_unvalidated")
        reasons.extend(f"degradation:{reason}" for reason in snapshot.degradation_reasons)
        reasons = list(dict.fromkeys(reasons))

        if snapshot.job_status in {"failed", "cancelled"} or snapshot.failed_stage:
            status: CompletionStatus = "failed"
        elif any(reason.startswith(("declared_canonical_ready:", "artifact_registry_", "canonical_artifact_", "provider_receipts_", "validation_", "current_artifact_set_", "current_stage_closure:", "degradation:")) for reason in reasons):
            status = "blocked"
        elif reasons:
            status = "incomplete"
        else:
            status = "complete"

        canonical_ready = status == "complete"
        return CompletionEvaluationV1(
            evaluator_version=cls.VERSION,
            job_id=snapshot.job_id,
            status=status,
            canonical_ready=canonical_ready,
            requires_attention=not canonical_ready,
            reasons=tuple(reasons),
            evidence_hash=_canonical_hash(snapshot.to_dict()),
            evaluated_at=utc_now_iso(),
        )

    @classmethod
    def from_job_outcome(
        cls,
        outcome: JobOutcomeV1 | Mapping[str, Any],
        *,
        artifact_registry_verified: bool,
        canonical_artifacts: Mapping[str, bool] | None = None,
        validation_status: str = "clean",
        provider_receipts_complete: bool = False,
    ) -> CompletionEvaluationV1:
        typed = outcome if isinstance(outcome, JobOutcomeV1) else JobOutcomeV1.from_dict(outcome)
        policy = dict(typed.readiness_policy_snapshot)
        return cls.evaluate(
            CompletionEvidenceV1(
                job_id=typed.job_id,
                job_status=typed.job_status,
                required_stages=typed.required_stages,
                completed_stages=typed.completed_stages,
                failed_stage=typed.failed_stage,
                artifact_registry_verified=artifact_registry_verified,
                canonical_artifacts=canonical_artifacts or {},
                validation_required=bool(policy.get("validation_required", False)),
                require_clean_validation=bool(policy.get("require_clean_validation", False)),
                allow_unvalidated_when_validation_optional=bool(
                    policy.get("allow_unvalidated_when_validation_optional", False)
                ),
                validation_status=validation_status,
                provider_receipts_complete=provider_receipts_complete,
                declared_canonical_ready=typed.canonical_ready,
                degradation_reasons=typed.degradation_reasons,
                evidence_sources=("job_outcome_v1", "artifact_registry"),
            )
        )


CanonicalCompletionResultV1 = CompletionEvaluationV1
CanonicalCompletionEvidenceV1 = CompletionEvidenceV1


__all__ = [
    "CanonicalCompletionEvaluator",
    "CanonicalCompletionEvidenceV1",
    "CanonicalCompletionResultV1",
    "CompletionEvidenceV1",
    "CompletionEvaluationV1",
    "CompletionStatus",
]

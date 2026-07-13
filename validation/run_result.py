from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple, cast


VALIDATION_RUN_ARTIFACT_TYPE = "validation_run_result"
VALIDATION_RUN_ARTIFACT_VERSION = "v1"
VALIDATION_RUN_SCHEMA_VERSION = "validation-run-result-v1"


class ValidationRunResultError(ValueError):
    """Raised when a validation run result violates its public contract."""


class ClaimVerdict(str, Enum):
    SUPPORTED = "supported"
    PARTIAL_SUPPORT = "partial_support"
    EVIDENCE_GAP = "evidence_gap"
    UNSUPPORTED = "unsupported"
    CONTRADICTED = "contradicted"
    WRONG_SOURCE = "wrong_source"
    NEEDS_REVIEW = "needs_review"


class ValidationExecutionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class ValidationRunDisposition(str, Enum):
    CLEAN = "clean"
    FINDINGS = "findings"
    NEEDS_REVIEW = "needs_review"
    UNVALIDATED = "unvalidated"


_ALL_VERDICTS = tuple(item.value for item in ClaimVerdict)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _json_value(asdict(cast(Any, value)))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_value(to_dict())
    if hasattr(value, "__dict__"):
        return _json_value(vars(value))
    return str(value)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _string_list(values: Iterable[Any]) -> Tuple[str, ...]:
    return tuple(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _legacy_conclusion(value: Any) -> str:
    conclusion = _field(value, "conclusion", "")
    return str(getattr(conclusion, "value", conclusion) or "")


def _details(value: Any) -> Dict[str, Any]:
    raw = _field(value, "details", {}) or {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _canonical_status(value: Any) -> str:
    details = _details(value)
    status = str(_field(value, "evidence_status", "") or details.get("evidence_status") or "").strip().lower()
    if status:
        return status
    ai_validation = details.get("ai_validation")
    if isinstance(ai_validation, Mapping):
        status = str(ai_validation.get("status") or "").strip().lower()
        if status:
            return status
    return str(
        _field(value, "adjudication_status", "")
        or details.get("adjudication_status")
        or ""
    ).strip().lower()


def claim_verdict_for_result(value: Any) -> ClaimVerdict:
    """Project legacy validator axes into the sole public claim verdict.

    The projection is deliberately fail-closed: an unknown status or ambiguous
    claim-paper alignment becomes ``needs_review``.  In particular, absence of
    source-grounded evidence remains ``evidence_gap`` and can never fall through
    to ``unsupported``.
    """

    details = _details(value)
    status = _canonical_status(value)
    disposition = str(
        _field(value, "disposition", "") or details.get("disposition") or ""
    ).strip().lower()
    conclusion = _legacy_conclusion(value).strip().upper()
    low_confidence = bool(_field(value, "low_confidence", False))
    claim_unit_results = details.get("claim_unit_results") or []
    ambiguous_alignment = any(
        isinstance(item, Mapping)
        and str(item.get("reason") or "") == "ambiguous_claim_paper_alignment"
        for item in claim_unit_results
    )

    if ambiguous_alignment or status in {"needs_review", "low_confidence", "uncertain"}:
        return ClaimVerdict.NEEDS_REVIEW
    if status in {"wrong_source", "mapping_error"} or conclusion == "WRONG_SOURCE":
        return ClaimVerdict.WRONG_SOURCE
    if status in {"contradicted", "contradiction", "refuted"}:
        return ClaimVerdict.CONTRADICTED
    if status in {"supported", "clean_supported"}:
        if disposition == "narrowed_and_kept":
            return ClaimVerdict.PARTIAL_SUPPORT
        return ClaimVerdict.SUPPORTED
    if status in {"partial", "partial_support"} or disposition == "narrowed_and_kept":
        return ClaimVerdict.PARTIAL_SUPPORT
    if status == "evidence_gap":
        if low_confidence and disposition == "manual_review":
            return ClaimVerdict.NEEDS_REVIEW
        return ClaimVerdict.EVIDENCE_GAP
    if status == "unsupported":
        return ClaimVerdict.UNSUPPORTED
    if not status:
        if conclusion == "SUPPORTED":
            return ClaimVerdict.SUPPORTED
        if conclusion == "PARTIAL_SUPPORT":
            return ClaimVerdict.PARTIAL_SUPPORT
        if conclusion == "NEEDS_REVIEW":
            return ClaimVerdict.NEEDS_REVIEW
        # Legacy UNSUPPORTED without an explicit source-grounded status is not
        # strong enough to establish the new unsupported contract.
        if conclusion == "UNSUPPORTED":
            return ClaimVerdict.NEEDS_REVIEW
    return ClaimVerdict.NEEDS_REVIEW


def reduce_validation_disposition(
    execution_status: ValidationExecutionStatus | str,
    verdicts: Iterable[ClaimVerdict | str],
) -> ValidationRunDisposition:
    execution = ValidationExecutionStatus(str(getattr(execution_status, "value", execution_status)))
    if execution is not ValidationExecutionStatus.SUCCEEDED:
        return ValidationRunDisposition.UNVALIDATED
    normalized = {
        ClaimVerdict(str(getattr(item, "value", item)))
        for item in verdicts
    }
    if normalized.intersection(
        {ClaimVerdict.WRONG_SOURCE, ClaimVerdict.CONTRADICTED, ClaimVerdict.NEEDS_REVIEW}
    ):
        return ValidationRunDisposition.NEEDS_REVIEW
    if normalized.intersection(
        {ClaimVerdict.PARTIAL_SUPPORT, ClaimVerdict.EVIDENCE_GAP, ClaimVerdict.UNSUPPORTED}
    ):
        return ValidationRunDisposition.FINDINGS
    return ValidationRunDisposition.CLEAN


@dataclass(frozen=True)
class ClaimValidationResultV1:
    claim_result_id: str
    claim_unit_ids: Tuple[str, ...]
    citation_set_key: str
    paper_ids: Tuple[str, ...]
    block_ids: Tuple[str, ...]
    claim_text: str
    claim_context: str
    verdict: ClaimVerdict
    reasoning_summary: str
    repair_hint: str
    root_causes: Tuple[str, ...]
    span_start: int | None
    span_end: int | None
    alignment_status: str
    alignment_confidence: float
    low_confidence: bool
    details: Mapping[str, Any]
    evidence_candidates: Tuple[Mapping[str, Any], ...]
    compatibility: Mapping[str, Any]

    @classmethod
    def from_validation_result(cls, result: Any) -> "ClaimValidationResultV1":
        details = _details(result)
        raw_units = _field(result, "claim_units", []) or details.get("claim_units") or []
        units = [dict(item) for item in raw_units if isinstance(item, Mapping)]
        target = _field(result, "target_claim_unit", {}) or details.get("target_claim_unit") or {}
        target_mapping = dict(target) if isinstance(target, Mapping) else {}
        claim_unit_ids = _string_list(item.get("claim_unit_id") for item in units)
        if not claim_unit_ids and target_mapping.get("claim_unit_id"):
            claim_unit_ids = (str(target_mapping["claim_unit_id"]),)
        citation_set_key = str(
            _field(result, "citation_set_key", "")
            or details.get("citation_set_key")
            or _field(result, "citation_id", "")
        )
        claim_text = str(_field(result, "claim_text", "") or "")
        stable_key = "|".join([citation_set_key, *claim_unit_ids, claim_text])
        claim_result_id = "claim:" + hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:24]
        root_causes = _string_list(
            getattr(item, "value", item)
            for item in (_field(result, "root_causes", []) or [])
        )
        serialized_candidates = tuple(
            _json_value(item)
            for item in (_field(result, "evidence_candidates", []) or [])
        )
        verdict = claim_verdict_for_result(result)
        claim_unit_results = [
            dict(item)
            for item in (details.get("claim_unit_results") or [])
            if isinstance(item, Mapping)
        ]
        alignment_status = ""
        alignment_confidence = 0.0
        if claim_unit_results:
            alignment_status = str(claim_unit_results[0].get("alignment_status") or "")
            try:
                alignment_confidence = float(claim_unit_results[0].get("alignment_confidence") or 0.0)
            except (TypeError, ValueError):
                alignment_confidence = 0.0
        return cls(
            claim_result_id=claim_result_id,
            claim_unit_ids=claim_unit_ids,
            citation_set_key=citation_set_key,
            paper_ids=_string_list(_field(result, "paper_ids", []) or [_field(result, "paper_id", "")]),
            block_ids=_string_list(_field(result, "block_ids", []) or []),
            claim_text=claim_text,
            claim_context=str(_field(result, "claim_context", "") or ""),
            verdict=verdict,
            reasoning_summary=str(_field(result, "reasoning_summary", "") or ""),
            repair_hint=str(_field(result, "repair_hint", "") or ""),
            root_causes=root_causes,
            span_start=target_mapping.get("span_start") if isinstance(target_mapping.get("span_start"), int) else None,
            span_end=target_mapping.get("span_end") if isinstance(target_mapping.get("span_end"), int) else None,
            alignment_status=alignment_status,
            alignment_confidence=alignment_confidence,
            low_confidence=bool(_field(result, "low_confidence", False)),
            details=_json_value(details),
            evidence_candidates=serialized_candidates,
            compatibility={
                "legacy_conclusion": _legacy_conclusion(result),
                "legacy_evidence_status": _canonical_status(result),
                "legacy_repair_disposition": str(
                    _field(result, "disposition", "") or details.get("disposition") or ""
                ),
            },
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claim_result_id": self.claim_result_id,
            "claim_unit_ids": list(self.claim_unit_ids),
            "citation_set_key": self.citation_set_key,
            "paper_ids": list(self.paper_ids),
            "block_ids": list(self.block_ids),
            "claim_text": self.claim_text,
            "claim_context": self.claim_context,
            "verdict": self.verdict.value,
            "reasoning_summary": self.reasoning_summary,
            "repair_hint": self.repair_hint,
            "root_causes": list(self.root_causes),
            "span_start": self.span_start,
            "span_end": self.span_end,
            "alignment_status": self.alignment_status,
            "alignment_confidence": self.alignment_confidence,
            "low_confidence": self.low_confidence,
            "details": _json_value(self.details),
            "evidence_candidates": _json_value(self.evidence_candidates),
            "compatibility": _json_value(self.compatibility),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ClaimValidationResultV1":
        return cls(
            claim_result_id=str(payload.get("claim_result_id") or ""),
            claim_unit_ids=_string_list(payload.get("claim_unit_ids") or []),
            citation_set_key=str(payload.get("citation_set_key") or ""),
            paper_ids=_string_list(payload.get("paper_ids") or []),
            block_ids=_string_list(payload.get("block_ids") or []),
            claim_text=str(payload.get("claim_text") or ""),
            claim_context=str(payload.get("claim_context") or ""),
            verdict=ClaimVerdict(str(payload.get("verdict") or "needs_review")),
            reasoning_summary=str(payload.get("reasoning_summary") or ""),
            repair_hint=str(payload.get("repair_hint") or ""),
            root_causes=_string_list(payload.get("root_causes") or []),
            span_start=payload.get("span_start") if isinstance(payload.get("span_start"), int) else None,
            span_end=payload.get("span_end") if isinstance(payload.get("span_end"), int) else None,
            alignment_status=str(payload.get("alignment_status") or ""),
            alignment_confidence=float(payload.get("alignment_confidence") or 0.0),
            low_confidence=bool(payload.get("low_confidence", False)),
            details=dict(payload.get("details") or {}),
            evidence_candidates=tuple(
                dict(item) for item in (payload.get("evidence_candidates") or []) if isinstance(item, Mapping)
            ),
            compatibility=dict(payload.get("compatibility") or {}),
        )


@dataclass(frozen=True)
class ValidationRunResultV1:
    artifact_type: str
    artifact_version: str
    schema_version: str
    validation_run_id: str
    report_id: str
    job_id: str
    attempt_id: str
    created_at: str
    updated_at: str
    execution_status: ValidationExecutionStatus
    validation_disposition: ValidationRunDisposition
    repair_policy: str
    claim_results: Tuple[ClaimValidationResultV1, ...]
    claim_verdict_counts: Mapping[str, int]
    contradicted_count: int
    total_claims: int
    diagnostics: Tuple[str, ...]
    failure_reason: str
    compatibility_status: str = "verified"

    def validate(self) -> None:
        if self.artifact_type != VALIDATION_RUN_ARTIFACT_TYPE:
            raise ValidationRunResultError(f"unexpected artifact_type: {self.artifact_type}")
        if self.artifact_version != VALIDATION_RUN_ARTIFACT_VERSION:
            raise ValidationRunResultError(f"unsupported artifact_version: {self.artifact_version}")
        if self.schema_version != VALIDATION_RUN_SCHEMA_VERSION:
            raise ValidationRunResultError(f"unsupported schema_version: {self.schema_version}")
        actual = {name: 0 for name in _ALL_VERDICTS}
        for result in self.claim_results:
            actual[result.verdict.value] += 1
        declared = {name: int(self.claim_verdict_counts.get(name, 0)) for name in _ALL_VERDICTS}
        if set(self.claim_verdict_counts) != set(_ALL_VERDICTS) or declared != actual:
            raise ValidationRunResultError("claim_verdict_counts do not match claim_results")
        if self.total_claims != len(self.claim_results):
            raise ValidationRunResultError("total_claims does not match claim_results")
        if self.contradicted_count != actual[ClaimVerdict.CONTRADICTED.value]:
            raise ValidationRunResultError("contradicted_count does not match claim_verdict_counts")
        expected_disposition = reduce_validation_disposition(
            self.execution_status,
            (item.verdict for item in self.claim_results),
        )
        if self.compatibility_status == "verified" and self.validation_disposition is not expected_disposition:
            raise ValidationRunResultError("validation_disposition does not match execution status and verdicts")

    @classmethod
    def create(
        cls,
        *,
        job_id: str,
        execution_status: ValidationExecutionStatus | str,
        claim_results: Sequence[ClaimValidationResultV1] = (),
        report_id: str = "",
        attempt_id: str = "",
        repair_policy: str = "report_only",
        diagnostics: Sequence[str] = (),
        failure_reason: str = "",
        compatibility_status: str = "verified",
        validation_run_id: str = "",
        created_at: str = "",
    ) -> "ValidationRunResultV1":
        execution = ValidationExecutionStatus(str(getattr(execution_status, "value", execution_status)))
        now = created_at or _utc_now_iso()
        results = tuple(claim_results)
        counts = {name: 0 for name in _ALL_VERDICTS}
        for result in results:
            counts[result.verdict.value] += 1
        run_id = validation_run_id or report_id or (
            "validation-run:" + hashlib.sha256(f"{job_id}|{attempt_id}|{now}".encode("utf-8")).hexdigest()[:24]
        )
        disposition = (
            ValidationRunDisposition.UNVALIDATED
            if compatibility_status != "verified"
            else reduce_validation_disposition(execution, (item.verdict for item in results))
        )
        instance = cls(
            artifact_type=VALIDATION_RUN_ARTIFACT_TYPE,
            artifact_version=VALIDATION_RUN_ARTIFACT_VERSION,
            schema_version=VALIDATION_RUN_SCHEMA_VERSION,
            validation_run_id=run_id,
            report_id=report_id or run_id,
            job_id=job_id,
            attempt_id=attempt_id,
            created_at=now,
            updated_at=now,
            execution_status=execution,
            validation_disposition=disposition,
            repair_policy=repair_policy,
            claim_results=results,
            claim_verdict_counts=counts,
            contradicted_count=counts[ClaimVerdict.CONTRADICTED.value],
            total_claims=len(results),
            diagnostics=_string_list(diagnostics),
            failure_reason=failure_reason,
            compatibility_status=compatibility_status,
        )
        instance.validate()
        return instance

    @classmethod
    def from_report(
        cls,
        report: Any,
        *,
        job_id: str,
        attempt_id: str = "",
        repair_policy: str = "report_only",
    ) -> "ValidationRunResultV1":
        results = tuple(
            ClaimValidationResultV1.from_validation_result(item)
            for item in (_field(report, "citation_results", []) or [])
        )
        return cls.create(
            job_id=job_id,
            execution_status=ValidationExecutionStatus.SUCCEEDED,
            claim_results=results,
            report_id=str(_field(report, "report_id", "") or ""),
            attempt_id=attempt_id,
            repair_policy=repair_policy,
            created_at=str(_field(report, "created_at", "") or ""),
        )

    @classmethod
    def from_legacy_report(cls, payload: Mapping[str, Any], *, job_id: str = "") -> "ValidationRunResultV1":
        results = tuple(
            ClaimValidationResultV1.from_validation_result(item)
            for item in (payload.get("citation_results") or [])
            if isinstance(item, Mapping)
        )
        return cls.create(
            job_id=job_id or str(payload.get("job_id") or "legacy-unknown"),
            execution_status=ValidationExecutionStatus.SKIPPED,
            claim_results=results,
            report_id=str(payload.get("report_id") or "legacy-validation-report"),
            repair_policy=str(payload.get("repair_policy") or "legacy_unknown"),
            diagnostics=("legacy_validation_report_unverified",),
            failure_reason="legacy validation artifact does not satisfy ValidationRunResultV1",
            compatibility_status="legacy_unverified",
            created_at=str(payload.get("created_at") or "") or _utc_now_iso(),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "schema_version": self.schema_version,
            "validation_run_id": self.validation_run_id,
            "report_id": self.report_id,
            "job_id": self.job_id,
            "attempt_id": self.attempt_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "execution_status": self.execution_status.value,
            "validation_disposition": self.validation_disposition.value,
            "repair_policy": self.repair_policy,
            "claim_results": [item.to_dict() for item in self.claim_results],
            "claim_verdict_counts": dict(self.claim_verdict_counts),
            "contradicted_count": self.contradicted_count,
            "total_claims": self.total_claims,
            "diagnostics": list(self.diagnostics),
            "failure_reason": self.failure_reason,
            "compatibility_status": self.compatibility_status,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ValidationRunResultV1":
        if str(payload.get("artifact_type") or "") != VALIDATION_RUN_ARTIFACT_TYPE:
            return cls.from_legacy_report(payload)
        instance = cls(
            artifact_type=str(payload.get("artifact_type") or ""),
            artifact_version=str(payload.get("artifact_version") or ""),
            schema_version=str(payload.get("schema_version") or ""),
            validation_run_id=str(payload.get("validation_run_id") or ""),
            report_id=str(payload.get("report_id") or ""),
            job_id=str(payload.get("job_id") or ""),
            attempt_id=str(payload.get("attempt_id") or ""),
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            execution_status=ValidationExecutionStatus(str(payload.get("execution_status") or "skipped")),
            validation_disposition=ValidationRunDisposition(
                str(payload.get("validation_disposition") or "unvalidated")
            ),
            repair_policy=str(payload.get("repair_policy") or "report_only"),
            claim_results=tuple(
                ClaimValidationResultV1.from_dict(item)
                for item in (payload.get("claim_results") or [])
                if isinstance(item, Mapping)
            ),
            claim_verdict_counts={
                str(key): int(value)
                for key, value in dict(payload.get("claim_verdict_counts") or {}).items()
            },
            contradicted_count=int(payload.get("contradicted_count") or 0),
            total_claims=int(payload.get("total_claims") or 0),
            diagnostics=_string_list(payload.get("diagnostics") or []),
            failure_reason=str(payload.get("failure_reason") or ""),
            compatibility_status=str(payload.get("compatibility_status") or "verified"),
        )
        instance.validate()
        return instance

    @property
    def contract_satisfied(self) -> bool:
        return (
            self.compatibility_status == "verified"
            and self.execution_status is ValidationExecutionStatus.SUCCEEDED
        )

    def stable_hash(self) -> str:
        payload = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

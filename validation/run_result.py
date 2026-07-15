from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, is_dataclass
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
_EXTENDED_CONTRACT_FIELDS = frozenset(
    {
        "input_artifacts",
        "expected_claim_count",
        "validated_claim_count",
        "review_has_citations",
        "evidence_complete",
        "review_cleanliness",
        "repair_status",
        "recheck_status",
        "degradation_reasons",
    }
)
_BLOCKING_REPAIR_STATUSES = frozenset({"failed", "incomplete"})
_BLOCKING_RECHECK_STATUSES = frozenset({"failed", "incomplete", "pending", "required"})


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


def _nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationRunResultError(f"{field_name} must be a non-negative integer")
    return value


def _boolean(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationRunResultError(f"{field_name} must be boolean")
    return value


def _optional_boolean(value: Any, field_name: str) -> bool | None:
    if value is None:
        return None
    return _boolean(value, field_name)


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
    *,
    expected_claim_count: int | None = None,
    validated_claim_count: int | None = None,
    review_has_citations: bool | None = None,
    evidence_complete: bool = True,
    repair_status: str = "not_requested",
    recheck_status: str = "not_required",
    degradation_reasons: Sequence[str] = (),
) -> ValidationRunDisposition:
    execution = ValidationExecutionStatus(str(getattr(execution_status, "value", execution_status)))
    if execution is not ValidationExecutionStatus.SUCCEEDED:
        return ValidationRunDisposition.UNVALIDATED
    normalized_verdicts = tuple(
        ClaimVerdict(str(getattr(item, "value", item)))
        for item in verdicts
    )
    effective_validated_count = (
        len(normalized_verdicts)
        if validated_claim_count is None
        else validated_claim_count
    )
    if expected_claim_count is not None and validated_claim_count is not None:
        if expected_claim_count != validated_claim_count:
            return ValidationRunDisposition.NEEDS_REVIEW
    if effective_validated_count == 0 and review_has_citations is not False:
        return ValidationRunDisposition.NEEDS_REVIEW
    if not evidence_complete:
        return ValidationRunDisposition.NEEDS_REVIEW
    if _string_list(degradation_reasons):
        return ValidationRunDisposition.NEEDS_REVIEW
    if repair_status.strip().lower() in _BLOCKING_REPAIR_STATUSES:
        return ValidationRunDisposition.NEEDS_REVIEW
    if recheck_status.strip().lower() in _BLOCKING_RECHECK_STATUSES:
        return ValidationRunDisposition.NEEDS_REVIEW
    normalized = set(normalized_verdicts)
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
class ValidationInputArtifactsV1:
    """Content-addressed identities for inputs consumed by Validation."""

    review_draft_id: str = ""
    review_draft_hash: str = ""
    citation_manifest_id: str = ""
    citation_manifest_hash: str = ""
    evidence_manifest_ids: Tuple[str, ...] = ()
    evidence_manifest_hashes: Tuple[str, ...] = ()

    def validate(self) -> None:
        if bool(self.review_draft_id) != bool(self.review_draft_hash):
            raise ValidationRunResultError(
                "review draft artifact identity requires both id and hash"
            )
        if bool(self.citation_manifest_id) != bool(self.citation_manifest_hash):
            raise ValidationRunResultError(
                "citation manifest artifact identity requires both id and hash"
            )
        if len(self.evidence_manifest_ids) != len(self.evidence_manifest_hashes):
            raise ValidationRunResultError(
                "evidence manifest artifact ids and hashes must have equal length"
            )
        if any(not item for item in self.evidence_manifest_ids):
            raise ValidationRunResultError("evidence manifest artifact ids must be non-empty")
        if any(not item for item in self.evidence_manifest_hashes):
            raise ValidationRunResultError("evidence manifest artifact hashes must be non-empty")

    @classmethod
    def from_value(
        cls,
        value: "ValidationInputArtifactsV1 | Mapping[str, Any] | None",
    ) -> "ValidationInputArtifactsV1":
        if isinstance(value, ValidationInputArtifactsV1):
            value.validate()
            return value
        if value is not None and not isinstance(value, Mapping):
            raise ValidationRunResultError("input_artifacts must be an object")
        payload: Mapping[str, Any] = value if isinstance(value, Mapping) else {}
        instance = cls(
            review_draft_id=str(payload.get("review_draft_id") or "").strip(),
            review_draft_hash=str(payload.get("review_draft_hash") or "").strip(),
            citation_manifest_id=str(payload.get("citation_manifest_id") or "").strip(),
            citation_manifest_hash=str(payload.get("citation_manifest_hash") or "").strip(),
            evidence_manifest_ids=tuple(
                str(item).strip()
                for item in (payload.get("evidence_manifest_ids") or ())
            ),
            evidence_manifest_hashes=tuple(
                str(item).strip()
                for item in (payload.get("evidence_manifest_hashes") or ())
            ),
        )
        instance.validate()
        return instance

    def to_dict(self) -> Dict[str, Any]:
        return {
            "review_draft_id": self.review_draft_id,
            "review_draft_hash": self.review_draft_hash,
            "citation_manifest_id": self.citation_manifest_id,
            "citation_manifest_hash": self.citation_manifest_hash,
            "evidence_manifest_ids": list(self.evidence_manifest_ids),
            "evidence_manifest_hashes": list(self.evidence_manifest_hashes),
        }


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
    input_artifacts: ValidationInputArtifactsV1 = field(
        default_factory=ValidationInputArtifactsV1
    )
    expected_claim_count: int = 0
    validated_claim_count: int = 0
    review_has_citations: bool | None = None
    evidence_complete: bool = False
    review_cleanliness: ValidationRunDisposition = ValidationRunDisposition.UNVALIDATED
    repair_status: str = "not_requested"
    recheck_status: str = "not_required"
    degradation_reasons: Tuple[str, ...] = ()
    compatibility_status: str = "verified"

    def validate(self) -> None:
        if self.artifact_type != VALIDATION_RUN_ARTIFACT_TYPE:
            raise ValidationRunResultError(f"unexpected artifact_type: {self.artifact_type}")
        if self.artifact_version != VALIDATION_RUN_ARTIFACT_VERSION:
            raise ValidationRunResultError(f"unsupported artifact_version: {self.artifact_version}")
        if self.schema_version != VALIDATION_RUN_SCHEMA_VERSION:
            raise ValidationRunResultError(f"unsupported schema_version: {self.schema_version}")
        self.input_artifacts.validate()
        _nonnegative_int(self.expected_claim_count, "expected_claim_count")
        _nonnegative_int(self.validated_claim_count, "validated_claim_count")
        _optional_boolean(self.review_has_citations, "review_has_citations")
        _boolean(self.evidence_complete, "evidence_complete")
        actual = {name: 0 for name in _ALL_VERDICTS}
        for result in self.claim_results:
            actual[result.verdict.value] += 1
        declared = {name: int(self.claim_verdict_counts.get(name, 0)) for name in _ALL_VERDICTS}
        if set(self.claim_verdict_counts) != set(_ALL_VERDICTS) or declared != actual:
            raise ValidationRunResultError("claim_verdict_counts do not match claim_results")
        if self.total_claims != len(self.claim_results):
            raise ValidationRunResultError("total_claims does not match claim_results")
        if self.validated_claim_count != len(self.claim_results):
            raise ValidationRunResultError("validated_claim_count does not match claim_results")
        if self.total_claims != self.validated_claim_count:
            raise ValidationRunResultError("total_claims does not match validated_claim_count")
        if self.contradicted_count != actual[ClaimVerdict.CONTRADICTED.value]:
            raise ValidationRunResultError("contradicted_count does not match claim_verdict_counts")
        count_complete = self.expected_claim_count == self.validated_claim_count
        zero_claim_failure = (
            self.validated_claim_count == 0 and self.review_has_citations is not False
        )
        if self.evidence_complete and (not count_complete or zero_claim_failure):
            raise ValidationRunResultError(
                "evidence_complete cannot be true when expected claims are unmet"
            )
        if not self.repair_status.strip():
            raise ValidationRunResultError("repair_status must be non-empty")
        if not self.recheck_status.strip():
            raise ValidationRunResultError("recheck_status must be non-empty")
        expected_disposition = reduce_validation_disposition(
            self.execution_status,
            (item.verdict for item in self.claim_results),
            expected_claim_count=self.expected_claim_count,
            validated_claim_count=self.validated_claim_count,
            review_has_citations=self.review_has_citations,
            evidence_complete=self.evidence_complete,
            repair_status=self.repair_status,
            recheck_status=self.recheck_status,
            degradation_reasons=self.degradation_reasons,
        )
        if self.compatibility_status == "verified":
            if self.validation_disposition is not expected_disposition:
                raise ValidationRunResultError(
                    "validation_disposition does not match execution status and completeness"
                )
            if self.review_cleanliness is not expected_disposition:
                raise ValidationRunResultError(
                    "review_cleanliness does not match validation disposition"
                )
        elif (
            self.validation_disposition is not ValidationRunDisposition.UNVALIDATED
            or self.review_cleanliness is not ValidationRunDisposition.UNVALIDATED
        ):
            raise ValidationRunResultError(
                "unverified validation results must remain unvalidated"
            )

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
        input_artifacts: ValidationInputArtifactsV1 | Mapping[str, Any] | None = None,
        expected_claim_count: int | None = None,
        validated_claim_count: int | None = None,
        review_has_citations: bool | None = None,
        evidence_complete: bool | None = None,
        repair_status: str = "not_requested",
        recheck_status: str = "not_required",
        degradation_reasons: Sequence[str] = (),
        compatibility_status: str = "verified",
        validation_run_id: str = "",
        created_at: str = "",
    ) -> "ValidationRunResultV1":
        execution = ValidationExecutionStatus(str(getattr(execution_status, "value", execution_status)))
        now = created_at or _utc_now_iso()
        results = tuple(claim_results)
        actual_validated_count = len(results)
        if validated_claim_count is not None:
            declared_validated_count = _nonnegative_int(
                validated_claim_count,
                "validated_claim_count",
            )
            if declared_validated_count != actual_validated_count:
                raise ValidationRunResultError(
                    "validated_claim_count does not match claim_results"
                )
        if expected_claim_count is None:
            expected_count = actual_validated_count
        else:
            expected_count = _nonnegative_int(expected_claim_count, "expected_claim_count")
        if review_has_citations is not None and not isinstance(review_has_citations, bool):
            raise ValidationRunResultError("review_has_citations must be boolean")
        if evidence_complete is not None and not isinstance(evidence_complete, bool):
            raise ValidationRunResultError("evidence_complete must be boolean")
        has_citations = True if results and review_has_citations is None else review_has_citations
        count_complete = expected_count == actual_validated_count
        run_completed = execution is ValidationExecutionStatus.SUCCEEDED
        zero_claim_failure = (
            run_completed
            and actual_validated_count == 0
            and has_citations is not False
        )
        complete = (
            run_completed and count_complete and not zero_claim_failure
            if evidence_complete is None
            else evidence_complete and run_completed and count_complete and not zero_claim_failure
        )
        degradation = list(_string_list(degradation_reasons))
        if not count_complete:
            degradation.append("expected_claim_count_unmet")
        if zero_claim_failure and has_citations is True:
            degradation.append("citations_present_without_validated_claims")
        elif zero_claim_failure:
            degradation.append("citation_presence_unknown_for_zero_claims")
        if not complete and count_complete and not zero_claim_failure:
            degradation.append("validation_evidence_incomplete")
        normalized_repair_status = str(repair_status or "").strip()
        normalized_recheck_status = str(recheck_status or "").strip()
        if normalized_repair_status.lower() in _BLOCKING_REPAIR_STATUSES:
            degradation.append(f"repair_status:{normalized_repair_status.lower()}")
        if normalized_recheck_status.lower() in _BLOCKING_RECHECK_STATUSES:
            degradation.append(f"recheck_status:{normalized_recheck_status.lower()}")
        normalized_degradation = _string_list(degradation)
        normalized_input_artifacts = ValidationInputArtifactsV1.from_value(input_artifacts)
        counts = {name: 0 for name in _ALL_VERDICTS}
        for result in results:
            counts[result.verdict.value] += 1
        run_id = validation_run_id or report_id or (
            "validation-run:" + hashlib.sha256(f"{job_id}|{attempt_id}|{now}".encode("utf-8")).hexdigest()[:24]
        )
        disposition = (
            ValidationRunDisposition.UNVALIDATED
            if compatibility_status != "verified"
            else reduce_validation_disposition(
                execution,
                (item.verdict for item in results),
                expected_claim_count=expected_count,
                validated_claim_count=actual_validated_count,
                review_has_citations=has_citations,
                evidence_complete=complete,
                repair_status=normalized_repair_status,
                recheck_status=normalized_recheck_status,
                degradation_reasons=normalized_degradation,
            )
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
            input_artifacts=normalized_input_artifacts,
            expected_claim_count=expected_count,
            validated_claim_count=actual_validated_count,
            review_has_citations=has_citations,
            evidence_complete=complete,
            review_cleanliness=disposition,
            repair_status=normalized_repair_status,
            recheck_status=normalized_recheck_status,
            degradation_reasons=normalized_degradation,
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
        input_artifacts: ValidationInputArtifactsV1 | Mapping[str, Any] | None = None,
        expected_claim_count: int | None = None,
        review_has_citations: bool | None = None,
        evidence_complete: bool | None = None,
        repair_status: str = "not_requested",
        recheck_status: str = "not_required",
        degradation_reasons: Sequence[str] = (),
    ) -> "ValidationRunResultV1":
        results = tuple(
            ClaimValidationResultV1.from_validation_result(item)
            for item in (_field(report, "citation_results", []) or [])
        )
        declared_expected = expected_claim_count
        if declared_expected is None:
            declared_expected = _field(report, "expected_claim_count", None)
        if declared_expected is None:
            declared_expected = _field(report, "total_citations", len(results))
        normalized_expected = _nonnegative_int(
            declared_expected,
            "expected_claim_count",
        )
        declared_has_citations = review_has_citations
        if declared_has_citations is None:
            declared_has_citations = _field(report, "review_has_citations", None)
        if declared_has_citations is None and normalized_expected > 0:
            declared_has_citations = True
        return cls.create(
            job_id=job_id,
            execution_status=ValidationExecutionStatus.SUCCEEDED,
            claim_results=results,
            report_id=str(_field(report, "report_id", "") or ""),
            attempt_id=attempt_id,
            repair_policy=repair_policy,
            input_artifacts=(
                input_artifacts
                if input_artifacts is not None
                else _field(report, "input_artifacts", None)
            ),
            expected_claim_count=normalized_expected,
            review_has_citations=declared_has_citations,
            evidence_complete=evidence_complete,
            repair_status=repair_status,
            recheck_status=recheck_status,
            degradation_reasons=degradation_reasons,
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
            expected_claim_count=int(payload.get("total_citations") or len(results)),
            review_has_citations=bool(payload.get("total_citations") or results),
            evidence_complete=False,
            repair_status="legacy_unknown",
            recheck_status="legacy_unknown",
            degradation_reasons=("legacy_validation_report_unverified",),
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
            "input_artifacts": self.input_artifacts.to_dict(),
            "expected_claim_count": self.expected_claim_count,
            "validated_claim_count": self.validated_claim_count,
            "review_has_citations": self.review_has_citations,
            "evidence_complete": self.evidence_complete,
            "review_cleanliness": self.review_cleanliness.value,
            "repair_status": self.repair_status,
            "recheck_status": self.recheck_status,
            "degradation_reasons": list(self.degradation_reasons),
            "compatibility_status": self.compatibility_status,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ValidationRunResultV1":
        if str(payload.get("artifact_type") or "") != VALIDATION_RUN_ARTIFACT_TYPE:
            return cls.from_legacy_report(payload)
        has_extended_contract = _EXTENDED_CONTRACT_FIELDS.issubset(payload)
        compatibility_status = str(payload.get("compatibility_status") or "verified")
        degradation_reasons = _string_list(payload.get("degradation_reasons") or ())
        if not has_extended_contract:
            compatibility_status = "legacy_unverified"
            degradation_reasons = _string_list(
                (*degradation_reasons, "legacy_validation_run_result_contract_incomplete")
            )
        claim_results = tuple(
            ClaimValidationResultV1.from_dict(item)
            for item in (payload.get("claim_results") or [])
            if isinstance(item, Mapping)
        )
        validated_claim_count = (
            _nonnegative_int(
                payload.get("validated_claim_count"),
                "validated_claim_count",
            )
            if has_extended_contract
            else len(claim_results)
        )
        expected_claim_count = (
            _nonnegative_int(
                payload.get("expected_claim_count"),
                "expected_claim_count",
            )
            if has_extended_contract
            else int(payload.get("total_claims") or len(claim_results))
        )
        validation_disposition = (
            ValidationRunDisposition(str(payload.get("validation_disposition") or "unvalidated"))
            if has_extended_contract and compatibility_status == "verified"
            else ValidationRunDisposition.UNVALIDATED
        )
        review_cleanliness = (
            ValidationRunDisposition(str(payload.get("review_cleanliness") or "unvalidated"))
            if has_extended_contract and compatibility_status == "verified"
            else ValidationRunDisposition.UNVALIDATED
        )
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
            validation_disposition=validation_disposition,
            repair_policy=str(payload.get("repair_policy") or "report_only"),
            claim_results=claim_results,
            claim_verdict_counts={
                str(key): int(value)
                for key, value in dict(payload.get("claim_verdict_counts") or {}).items()
            },
            contradicted_count=int(payload.get("contradicted_count") or 0),
            total_claims=int(payload.get("total_claims") or 0),
            diagnostics=_string_list(payload.get("diagnostics") or []),
            failure_reason=str(payload.get("failure_reason") or ""),
            input_artifacts=ValidationInputArtifactsV1.from_value(
                payload.get("input_artifacts") if has_extended_contract else None
            ),
            expected_claim_count=expected_claim_count,
            validated_claim_count=validated_claim_count,
            review_has_citations=(
                _optional_boolean(
                    payload.get("review_has_citations"),
                    "review_has_citations",
                )
                if has_extended_contract
                else expected_claim_count > 0
            ),
            evidence_complete=(
                _boolean(payload.get("evidence_complete"), "evidence_complete")
                if has_extended_contract
                else False
            ),
            review_cleanliness=review_cleanliness,
            repair_status=(
                str(payload.get("repair_status") or "")
                if has_extended_contract
                else "legacy_unknown"
            ),
            recheck_status=(
                str(payload.get("recheck_status") or "")
                if has_extended_contract
                else "legacy_unknown"
            ),
            degradation_reasons=degradation_reasons,
            compatibility_status=compatibility_status,
        )
        instance.validate()
        return instance

    @property
    def contract_satisfied(self) -> bool:
        primary_inputs_verified = all(
            (
                self.input_artifacts.review_draft_id,
                self.input_artifacts.review_draft_hash,
                self.input_artifacts.citation_manifest_id,
                self.input_artifacts.citation_manifest_hash,
            )
        )
        evidence_inputs_verified = (
            self.review_has_citations is False
            or bool(self.input_artifacts.evidence_manifest_ids)
        )
        return (
            self.compatibility_status == "verified"
            and self.execution_status is ValidationExecutionStatus.SUCCEEDED
            and self.evidence_complete
            and self.expected_claim_count == self.validated_claim_count
            and primary_inputs_verified
            and evidence_inputs_verified
            and self.repair_status.strip().lower() not in _BLOCKING_REPAIR_STATUSES
            and self.recheck_status.strip().lower() not in _BLOCKING_RECHECK_STATUSES
            and not (
                self.validated_claim_count == 0
                and self.review_has_citations is not False
            )
        )

    def stable_hash(self) -> str:
        payload = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

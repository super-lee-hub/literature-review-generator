"""Current, durable review-validation execution.

The public entry point in this module accepts only
``ValidationExecutionService``.  It loads the registered current artifacts,
validates citation bundles against paper evidence, optionally runs the
configured Validator provider, and persists the canonical v1 run result plus
its projections.  The historical top-level ``validator`` module is not part
of this execution path.
"""

from __future__ import annotations

from datetime import datetime
from dataclasses import asdict
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from services.artifact_registry import ArtifactDependencyRefV2, file_sha256
from services.job_workspace import publish_bytes_artifact, publish_json_artifact
from services.model_selection import get_validator_api_config
from services.repair_policy import (
    ValidationRepairPolicy,
    parse_repair_policy,
    requires_manual_confirmation,
    unsafe_auto_rewrite_enabled,
)
from validation.edge_checkpoint import ValidationEdgeCheckpointStore
from validation.adjudication_checkpoint import AdjudicationCheckpointStore, sanitized_route_hash
from validation.adjudication_reuse import (
    adjudication_call_id,
)
from validation.llm_adjudicator import build_adjudication_packet, run_adjudication_stage
from validation.review_validator import (
    CitationValidationResult,
    EvidenceStatus,
    ReviewValidationReport,
    ReviewValidator,
    RootCause,
    ValidationConclusion,
    ValidationDisposition,
)
from validation.run_result import (
    ClaimVerdict,
    ValidationExecutionStatus,
    ValidationInputArtifactsV1,
    ValidationRunResultV1,
)


def _log(service: Any, level: str, message: str) -> None:
    logger = getattr(service, "logger", None)
    method = getattr(logger, level, None) or getattr(logger, "info", None)
    if callable(method):
        method(message)


def _read_json(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


def _normal_path(path: Any) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path))) if path else ""


def _cited_paper_ids(manifest: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for citation_set in manifest.get("citation_sets", ()) or ():
        if not isinstance(citation_set, Mapping):
            continue
        values.extend(
            str(item).strip()
            for item in (
                citation_set.get("paper_ids")
                or citation_set.get("paper_keys")
                or ()
            )
            if str(item).strip()
        )
    for occurrence in manifest.get("occurrences", ()) or ():
        if not isinstance(occurrence, Mapping):
            continue
        value = str(
            occurrence.get("paper_id") or occurrence.get("paper_key") or ""
        ).strip()
        if value:
            values.append(value)
    return list(dict.fromkeys(values))


def _load_inputs(
    service: Any,
    *,
    review_draft_override: Mapping[str, Any] | None = None,
    citation_manifest_override: Mapping[str, Any] | None = None,
    paper_artifacts_override: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
]:
    review_path = str(service.review_draft_path)
    manifest_path = str(service.citation_manifest_path)
    review_draft = (
        dict(review_draft_override)
        if isinstance(review_draft_override, Mapping)
        else (_read_json(review_path) if Path(review_path).is_file() else None)
    )
    citation_manifest = (
        dict(citation_manifest_override)
        if isinstance(citation_manifest_override, Mapping)
        else (
            _read_json(manifest_path) if Path(manifest_path).is_file() else None
        )
    )
    if review_draft is None:
        _log(service, "error", f"Missing current review draft: {review_path}")
    if citation_manifest is None:
        _log(service, "error", f"Missing current citation manifest: {manifest_path}")

    paper_artifacts: list[dict[str, Any]] = [
        dict(item)
        for item in (paper_artifacts_override or ())
        if isinstance(item, Mapping)
    ]
    records = list(service.artifact_registry.list_records())
    if paper_artifacts_override is None:
        for record in records:
            if record.artifact_type != "paper_artifact" or record.status != "ready":
                continue
            payload = _read_json(record.path)
            if payload is not None:
                paper_artifacts.append(payload)
        loaded_paths = {
            _normal_path(item.get("_registry_path"))
            for item in paper_artifacts
            if isinstance(item, Mapping)
        }
        for record in service.paper_artifact_records:
            if record.status != "ready" or _normal_path(record.path) in loaded_paths:
                continue
            payload = _read_json(record.path)
            if payload is not None:
                paper_artifacts.append(payload)

    binding_records: list[Any] = []
    if not paper_artifacts:
        # Lane B: recover the authoritative upstream Stage 1 artifacts through
        # the durable validation_source_binding/v1.  When a binding exists it
        # is the only acceptable source of paper artifacts: any identity
        # mismatch fails closed (VALIDATION_SOURCE_AUTHORITY_INVALID) instead
        # of degrading to an ai_summary-only synthetic artifact.  The legacy
        # summary fallback below is kept only for jobs with no binding at all.
        binding_records = [
            record
            for record in records
            if record.artifact_type == "validation_source_binding" and record.status == "ready"
        ]
        if binding_records:
            binding_payload = _read_json(binding_records[-1].path)
            if isinstance(binding_payload, Mapping) and isinstance(binding_payload.get("payload"), Mapping):
                binding_payload = binding_payload["payload"]
            if isinstance(binding_payload, Mapping):
                from validation.source_binding import resolve_bound_paper_artifacts

                bound_artifacts, binding_problems = resolve_bound_paper_artifacts(
                    binding_payload,
                    external_registry_resolver=getattr(
                        service, "validation_external_registry_resolver", None
                    ),
                    present_paper_keys=(
                        str(service.get_paper_key(summary.get("paper_info") or {}))
                        for summary in service.summaries
                        if isinstance(summary.get("paper_info"), Mapping)
                    ),
                )
                for problem in binding_problems:
                    _log(service, "error", problem)
                paper_artifacts.extend(bound_artifacts)

    if not paper_artifacts and not binding_records:
        # No binding and no local paper artifacts: legacy summary-only job.
        for summary in service.summaries:
            paper = summary.get("paper_info") or {}
            if not isinstance(paper, Mapping):
                continue
            paper_key = service.get_paper_key(paper)
            paper_artifacts.append(
                {
                    "paper_identity": {
                        "canonical_paper_key": paper_key,
                        "source_paper_id": str(paper.get("pdf_path") or ""),
                    },
                    "analysis": {"ai_summary": summary.get("ai_summary") or {}},
                    "source": {"source_pdf": str(paper.get("pdf_path") or "")},
                    "stage1_inputs": {},
                }
            )

    preprocess_evidence: dict[str, Any] = {}
    paper_metadata: dict[str, Any] = {}
    for artifact in paper_artifacts:
        identity = artifact.get("paper_identity") or {}
        if not isinstance(identity, Mapping):
            continue
        evidence = artifact.get("stage1_inputs", {}).get("preprocess_evidence", {})
        for key in (
            identity.get("canonical_paper_key"),
            identity.get("source_paper_id"),
        ):
            normalized = str(key or "").strip()
            if normalized:
                preprocess_evidence[normalized] = evidence
                paper_metadata[normalized] = dict(identity)
    return review_draft, citation_manifest, paper_artifacts, preprocess_evidence, paper_metadata


def _input_contract(
    service: Any,
    review_draft: Mapping[str, Any],
    citation_manifest: Mapping[str, Any],
    paper_artifacts: Sequence[Mapping[str, Any]],
    *,
    review_draft_record_override: Any | None = None,
    citation_manifest_record_override: Any | None = None,
) -> tuple[ValidationInputArtifactsV1, int, bool, bool, tuple[str, ...]]:
    records = list(service.artifact_registry.list_records())
    degradation: list[str] = []

    def registered_identity(
        path: str,
        artifact_type: str,
        record_override: Any | None = None,
    ) -> tuple[str, str]:
        normalized = _normal_path(path)
        if record_override is not None:
            if (
                str(getattr(record_override, "artifact_type", "")) != artifact_type
                or _normal_path(getattr(record_override, "path", "")) != normalized
                or not normalized
                or not Path(normalized).is_file()
            ):
                degradation.append(f"{artifact_type}_artifact_identity_unverified")
                return "", ""
            actual = file_sha256(normalized)
            if str(getattr(record_override, "content_hash", "")) != actual:
                degradation.append(f"{artifact_type}_artifact_hash_mismatch")
                return "", ""
            return str(getattr(record_override, "artifact_id", "")), actual
        matches = [
            record
            for record in records
            if record.status == "ready"
            and record.artifact_type == artifact_type
            and _normal_path(record.path) == normalized
        ]
        if len(matches) != 1 or not normalized or not Path(normalized).is_file():
            degradation.append(f"{artifact_type}_artifact_identity_unverified")
            return "", ""
        actual = file_sha256(normalized)
        if matches[0].content_hash != actual:
            degradation.append(f"{artifact_type}_artifact_hash_mismatch")
            return "", ""
        return matches[0].artifact_id, actual

    review_path = str(
        getattr(review_draft_record_override, "path", "")
        or service.review_draft_path
    )
    manifest_path = str(
        getattr(citation_manifest_record_override, "path", "")
        or service.citation_manifest_path
    )
    review_id, review_hash = registered_identity(
        review_path,
        "review_draft",
        review_draft_record_override,
    )
    manifest_id, manifest_hash = registered_identity(
        manifest_path,
        "citation_manifest",
        citation_manifest_record_override,
    )
    cited_ids = _cited_paper_ids(citation_manifest)
    citation_sets = citation_manifest.get("citation_sets") or ()
    occurrences = citation_manifest.get("occurrences") or ()
    draft_citation_count = sum(
        len(block.get("citations") or ())
        for section in (review_draft.get("content", {}).get("sections") or ())
        if isinstance(section, Mapping)
        for block in (section.get("blocks") or ())
        if isinstance(block, Mapping)
    )
    review_has_citations = bool(
        draft_citation_count or citation_sets or occurrences or cited_ids
    )
    expected_claim_count = len(citation_sets) if isinstance(citation_sets, list) else 0
    if review_has_citations and expected_claim_count == 0:
        expected_claim_count = max(
            len(occurrences) if isinstance(occurrences, list) else 0,
            1 if draft_citation_count else 0,
        )
        degradation.append("citation_set_inventory_missing")
    if draft_citation_count and not (citation_sets or occurrences):
        degradation.append("citation_manifest_missing_review_citations")
    if review_has_citations and not cited_ids:
        degradation.append("citation_paper_identity_missing")

    by_id: dict[str, Mapping[str, Any]] = {}
    for artifact in paper_artifacts:
        identity = artifact.get("paper_identity") or {}
        source = artifact.get("source") or {}
        for alias in (
            identity.get("canonical_paper_key"),
            identity.get("source_paper_id"),
            source.get("source_pdf") if isinstance(source, Mapping) else "",
        ):
            value = str(alias or "").strip()
            if value:
                by_id.setdefault(value, artifact)

    evidence_identities: list[tuple[str, str]] = []
    for paper_id in cited_ids:
        artifact = by_id.get(paper_id)
        if artifact is None:
            degradation.append(f"cited_paper_artifact_missing:{paper_id}")
            continue
        stage1_inputs = artifact.get("stage1_inputs") or {}
        if not isinstance(stage1_inputs, Mapping):
            degradation.append(f"evidence_manifest_missing:{paper_id}")
            continue
        evidence_path = str(stage1_inputs.get("evidence_manifest_path") or "")
        evidence_hash = str(stage1_inputs.get("evidence_manifest_hash") or "")
        normalized = _normal_path(evidence_path)
        if not normalized or not Path(normalized).is_file():
            degradation.append(f"evidence_manifest_missing:{paper_id}")
            continue
        actual = file_sha256(normalized)
        if not evidence_hash or evidence_hash != actual:
            degradation.append(f"evidence_manifest_hash_mismatch:{paper_id}")
            continue
        evidence_id = ""
        for record in records:
            if (
                record.status == "ready"
                and record.artifact_type == "evidence_manifest"
                and _normal_path(record.path) == normalized
                and record.content_hash == actual
            ):
                evidence_id = record.artifact_id
                break
        if not evidence_id:
            for record in records:
                for dependency in record.depends_on:
                    if (
                        dependency.artifact_type == "evidence_manifest"
                        and _normal_path(dependency.path) == normalized
                        and dependency.content_hash == actual
                    ):
                        evidence_id = dependency.artifact_id
                        break
                if evidence_id:
                    break
        if not evidence_id:
            degradation.append(f"evidence_manifest_identity_unverified:{paper_id}")
        else:
            evidence_identities.append((evidence_id, actual))

    unique_evidence = list(dict.fromkeys(evidence_identities))
    input_artifacts = ValidationInputArtifactsV1(
        review_draft_id=review_id,
        review_draft_hash=review_hash,
        citation_manifest_id=manifest_id,
        citation_manifest_hash=manifest_hash,
        evidence_manifest_ids=tuple(item[0] for item in unique_evidence),
        evidence_manifest_hashes=tuple(item[1] for item in unique_evidence),
    )
    evidence_complete = not degradation and (
        not review_has_citations or bool(unique_evidence)
    )
    return (
        input_artifacts,
        expected_claim_count,
        review_has_citations,
        evidence_complete,
        tuple(dict.fromkeys(degradation)),
    )


def _build_report(results: Sequence[CitationValidationResult]) -> ReviewValidationReport:
    values = list(results)
    return ReviewValidationReport(
        report_id=f"validation_report_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}",
        created_at=datetime.now().isoformat(),
        total_citations=len(values),
        supported_count=sum(
            item.conclusion is ValidationConclusion.SUPPORTED
            and item.disposition != ValidationDisposition.NARROWED_AND_KEPT.value
            for item in values
        ),
        partial_support_count=sum(
            item.conclusion is ValidationConclusion.PARTIAL_SUPPORT for item in values
        ),
        unsupported_count=sum(
            item.conclusion is ValidationConclusion.UNSUPPORTED for item in values
        ),
        wrong_source_count=sum(
            item.conclusion is ValidationConclusion.WRONG_SOURCE for item in values
        ),
        needs_review_count=sum(
            item.conclusion is ValidationConclusion.NEEDS_REVIEW for item in values
        ),
        contradicted_count=sum(
            item.conclusion is ValidationConclusion.CONTRADICTED for item in values
        ),
        citation_results=values,
        narrowed_and_kept_count=sum(
            item.disposition == ValidationDisposition.NARROWED_AND_KEPT.value
            for item in values
        ),
        evidence_gap_count=sum(
            item.evidence_status == EvidenceStatus.EVIDENCE_GAP.value for item in values
        ),
    )


def _confidence(value: Any) -> float:
    try:
        if isinstance(value, str) and value.strip().endswith("%"):
            value = float(value.strip()[:-1]) / 100
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    if result > 1:
        result /= 100
    return min(max(result, 0.0), 1.0)


def _apply_adjudication(result: CitationValidationResult, report: Mapping[str, Any]) -> CitationValidationResult:
    """Apply only explicit, known provider statuses to a current result."""

    status = str(report.get("status") or "").strip().lower()
    disposition = str(report.get("disposition") or "").strip().lower()
    confidence = _confidence(report.get("confidence"))
    low_confidence = bool(report.get("low_confidence")) or confidence < 0.55
    details = dict(result.details or {})
    details["ai_validation"] = dict(report)
    details["ai_confidence"] = confidence
    details["adjudication_stage"] = str(report.get("adjudication_stage") or "primary")
    details["adjudication_status"] = str(
        report.get("adjudication_status") or status or result.evidence_status
    )
    details["repair_scope"] = str(report.get("repair_scope") or "none")
    details["summary_paper_ids"] = list(report.get("summary_paper_ids") or result.paper_ids)
    details["manual_review_reason"] = str(report.get("manual_review_reason") or "")

    known = {
        "supported",
        "clean_supported",
        "partial_support",
        "partial",
        "evidence_gap",
        "unsupported",
        "contradicted",
        "wrong_source",
        "mapping_error",
        "low_confidence",
        "needs_review",
    }
    if status not in known:
        status = "needs_review"
        low_confidence = True
        disposition = ValidationDisposition.MANUAL_REVIEW.value
        details["manual_review_reason"] = (
            details["manual_review_reason"]
            or "Validator returned an unknown status; manual review is required."
        )
    if not disposition:
        disposition = (
            ValidationDisposition.MANUAL_REVIEW.value
            if low_confidence
            else result.disposition
        )
    if status in {"supported", "clean_supported"}:
        conclusion = (
            ValidationConclusion.PARTIAL_SUPPORT
            if disposition == ValidationDisposition.NARROWED_AND_KEPT.value
            else ValidationConclusion.SUPPORTED
        )
        evidence_status = EvidenceStatus.CLEAN_SUPPORTED.value
        roots: list[RootCause] = []
    elif status in {"partial_support", "partial", "evidence_gap"}:
        conclusion = ValidationConclusion.PARTIAL_SUPPORT
        evidence_status = EvidenceStatus.EVIDENCE_GAP.value
        roots = [RootCause.INSUFFICIENT_CONTEXT]
    elif status in {"wrong_source", "mapping_error"}:
        conclusion = ValidationConclusion.WRONG_SOURCE
        evidence_status = EvidenceStatus.WRONG_SOURCE.value
        roots = [RootCause.CITATION_MAPPING_ERROR]
    elif status == "contradicted":
        conclusion = ValidationConclusion.CONTRADICTED
        evidence_status = EvidenceStatus.CONTRADICTED.value
        roots = [RootCause.REVIEW_DRIFT]
    elif status == "unsupported":
        conclusion = ValidationConclusion.UNSUPPORTED
        evidence_status = EvidenceStatus.UNSUPPORTED.value
        roots = [RootCause.INSUFFICIENT_CONTEXT]
    else:
        conclusion = ValidationConclusion.NEEDS_REVIEW
        evidence_status = EvidenceStatus.NEEDS_REVIEW.value
        roots = [RootCause.LOW_CONFIDENCE]
        low_confidence = True
        disposition = ValidationDisposition.MANUAL_REVIEW.value

    details["evidence_status"] = evidence_status
    details["disposition"] = disposition
    details["low_confidence"] = low_confidence
    return CitationValidationResult(
        citation_id=result.citation_id,
        paper_id=result.paper_id,
        conclusion=conclusion,
        root_causes=roots,
        evidence_candidates=result.evidence_candidates,
        details=details,
        claim_text=result.claim_text,
        claim_context=result.claim_context,
        evidence_excerpt_list=result.evidence_excerpt_list,
        reasoning_summary=str(report.get("reasoning") or result.reasoning_summary),
        repair_hint=str(report.get("repair_hint") or result.repair_hint),
        citation_set_key=result.citation_set_key,
        paper_ids=list(result.paper_ids),
        block_ids=list(result.block_ids),
        low_confidence=low_confidence,
        evidence_status=evidence_status,
        disposition=disposition,
        block_context=result.block_context,
        claim_units=list(result.claim_units),
        target_claim_unit=dict(result.target_claim_unit),
        claim_type=str(report.get("claim_type") or result.claim_type),
        claim_type_confidence=_confidence(
            report.get("claim_type_confidence")
            if report.get("claim_type_confidence") is not None
            else result.claim_type_confidence
        ),
        adjudication_status=str(details["adjudication_status"]),
        adjudication_stage=str(details["adjudication_stage"]),
        escalated=str(details["adjudication_stage"]) == "stronger",
    )


def _adjudicate(service: Any, results: Sequence[CitationValidationResult]) -> list[CitationValidationResult]:
    config = get_validator_api_config(
        {"Validator_API": dict(service.settings.section("Validator_API"))}
    )
    if not str(config.get("api_key") or "").strip() or not str(config.get("model") or "").strip():
        return list(results)
    checkpoint_root = getattr(service.workspace.paths, "checkpoints_dir", "")
    checkpoint_store = AdjudicationCheckpointStore(
        Path(checkpoint_root) / "validation_adjudication"
    )
    route_hash = sanitized_route_hash(config)
    output: list[CitationValidationResult] = []
    for result in results:
        if not result.claim_text.strip() or not result.paper_ids:
            output.append(result)
            continue
        packet = build_adjudication_packet(result, stage="primary")
        key = checkpoint_store.key_for(
            packet=asdict(packet),
            stage=packet.stage,
            route_hash=route_hash,
        )
        with checkpoint_store.single_flight(key):
            report, reuse_record, reuse_error = service.find_verified_adjudication_reuse(
                packet=packet,
                api_config=config,
            )
            if report is not None and reuse_record is not None:
                raw_reuse = json.loads(Path(reuse_record.path).read_text(encoding="utf-8"))
                output_record = service.artifact_registry.get(
                    str(raw_reuse.get("provider_output_artifact_id") or "")
                )
                if output_record is None or output_record.status != "ready":
                    report = None
                else:
                    service.register_verified_reuse_call(
                        packet=packet,
                        api_config=config,
                        reuse_record=reuse_record,
                        output_record=output_record,
                        output_payload=report,
                    )
            if report is None:
                if reuse_record is not None and reuse_error:
                    _log(
                        service,
                        "warning",
                        f"adjudication reuse rejected: {reuse_error}",
                    )
                report = run_adjudication_stage(service, config, packet)
                if isinstance(report, Mapping):
                    call_id = adjudication_call_id(packet)
                    expected = getattr(service, "_expected_provider_calls", {}).get(call_id)
                    if expected is not None and expected.artifact_path:
                        output_record = next(
                            (
                                record
                                for record in service.artifact_registry.list_records()
                                if record.status == "ready"
                                and Path(record.path).resolve()
                                == Path(expected.artifact_path).resolve()
                            ),
                            None,
                        )
                        receipt = next(
                            (
                                item
                                for item in service.provider_receipt_ledger.list_receipts()
                                if item.call_id == call_id and item.status == "success"
                            ),
                            None,
                        )
                        if output_record is not None and receipt is not None:
                            service.publish_adjudication_reuse_record(
                                packet=packet,
                                api_config=config,
                                output_record=output_record,
                                receipt=receipt,
                            )
        if isinstance(report, Mapping):
            output.append(_apply_adjudication(result, report))
        else:
            output.append(result)
    return output


def _write_reports(
    service: Any,
    result: ValidationRunResultV1,
    repair_policy: ValidationRepairPolicy,
    *,
    output_dir: str | os.PathLike[str] | None = None,
    result_artifact_id: str = "",
    result_artifact_type: str = "validation_run_result",
    result_artifact_role: str = "validation",
    dependency_records: Sequence[Any] | None = None,
) -> dict[str, str]:
    workspace = service.workspace
    if output_dir:
        report_root = Path(output_dir)
        report_root.mkdir(parents=True, exist_ok=True)
        result_path = str(report_root / "validation_run_result_v1.json")
        report_path = str(report_root / "validation_report.txt")
        manual_path = str(report_root / "manual_review_report.json")
        completion_path = str(report_root / "validation_completion.json")
    else:
        result_path = workspace.report_path(
            f"{workspace.project_name}_validation_run_result_v1.json"
        )
        report_path = workspace.report_path(f"{workspace.project_name}_validation_report.txt")
        manual_path = workspace.report_path(
            f"{workspace.project_name}_manual_review_report.json"
        )
        completion_path = workspace.report_path(
            f"{workspace.project_name}_validation_completion.json"
        )
    registry = service.artifact_registry
    publication_context = getattr(service, "publication_context", None)
    if publication_context is None:
        from services.queue_service import LocalPublicationContext

        publication_context = LocalPublicationContext()
    dependencies: list[ArtifactDependencyRefV2] = []
    supplied_records = [
        record
        for record in (
            dependency_records
            if dependency_records is not None
            else (
                service.review_draft_record,
                service.citation_manifest_record,
            )
        )
        if record is not None
    ]
    # The canonical Validation payload names every input identity, including
    # evidence manifests.  Its Registry edge set must carry the same exact
    # identity multiset; retaining only draft/manifest edges would make a
    # result appear valid until a later reconcile/resume pass.
    records_by_id = {
        str(record.artifact_id): record
        for record in supplied_records
        if str(getattr(record, "artifact_id", ""))
    }
    for artifact_id in (
        result.input_artifacts.review_draft_id,
        result.input_artifacts.citation_manifest_id,
        *result.input_artifacts.evidence_manifest_ids,
    ):
        normalized_id = str(artifact_id or "")
        if not normalized_id or normalized_id in records_by_id:
            continue
        resolved = registry.get(normalized_id)
        if resolved is not None:
            records_by_id[normalized_id] = resolved
    for record in records_by_id.values():
        if record is not None:
            dependencies.append(ArtifactDependencyRefV2.from_record(record))
    canonical_record = publish_json_artifact(
        publication_context,
        registry,
        result_path,
        result.to_dict(),
        artifact_role=result_artifact_role,
        artifact_type=result_artifact_type,
        artifact_version="v1",
        producer="validation.current_validation",
        artifact_id=result_artifact_id or result.validation_run_id,
        status="ready" if result.contract_satisfied and not output_dir else "quarantined",
        depends_on=dependencies,
        metadata={
            "execution_status": result.execution_status.value,
            "validation_disposition": result.validation_disposition.value,
            "contract_satisfied": result.contract_satisfied,
        },
    )
    result_path = canonical_record.path
    lines = [
        "auto-generate validation report",
        f"generated_at: {result.updated_at}",
        f"validation_run_id: {result.validation_run_id}",
        f"execution_status: {result.execution_status.value}",
        f"validation_disposition: {result.validation_disposition.value}",
        f"repair_policy: {repair_policy.value}",
        f"total_claims: {result.total_claims}",
    ]
    lines.extend(
        f"{verdict.value}: {result.claim_verdict_counts[verdict.value]}"
        for verdict in ClaimVerdict
    )
    for index, claim in enumerate(result.claim_results, start=1):
        lines.extend(
            [
                f"{index}. citation_set: {claim.citation_set_key or claim.claim_result_id}",
                f"   papers: {', '.join(claim.paper_ids) or '?'}",
                f"   claim_verdict: {claim.verdict.value}",
                f"   claim: {claim.claim_text[:300]}",
                f"   reasoning: {claim.reasoning_summary}",
            ]
        )
    report_record = publish_bytes_artifact(
        publication_context,
        registry,
        report_path,
        "\n".join(lines).encode("utf-8"),
        artifact_role="validation_projection",
        artifact_type="validation_report_projection",
        artifact_version="v1",
        producer="validation.current_validation",
        artifact_id=f"validation-report:{Path(report_path).name}",
        status=canonical_record.status,
        depends_on=[ArtifactDependencyRefV2.from_record(canonical_record)],
    )
    report_path = report_record.path
    manual_items = [
        {
            "citation_set_key": claim.citation_set_key,
            "paper_ids": list(claim.paper_ids),
            "claim_text": claim.claim_text,
            "reasoning_summary": claim.reasoning_summary,
            "repair_hint": claim.repair_hint,
            "claim_verdict": claim.verdict.value,
            "manual_review_reason": str(claim.details.get("manual_review_reason") or ""),
        }
        for claim in result.claim_results
        if claim.verdict
        in {ClaimVerdict.NEEDS_REVIEW, ClaimVerdict.WRONG_SOURCE, ClaimVerdict.CONTRADICTED}
    ]
    manual_record = publish_json_artifact(
        publication_context,
        registry,
        manual_path,
        {
            "generated_at": result.updated_at,
            "validation_run_id": result.validation_run_id,
            "repair_policy": repair_policy.value,
            "requires_manual_confirmation": requires_manual_confirmation(repair_policy),
            "unsafe_auto_rewrite_enabled": unsafe_auto_rewrite_enabled(repair_policy),
            "total_items": len(manual_items),
            "items": manual_items,
        },
        artifact_role="validation_projection",
        artifact_type="manual_review_projection",
        artifact_version="v1",
        producer="validation.current_validation",
        artifact_id=f"manual-review:{Path(manual_path).name}",
        status=canonical_record.status,
        depends_on=[ArtifactDependencyRefV2.from_record(canonical_record)],
    )
    manual_path = manual_record.path
    completion_record = publish_json_artifact(
        publication_context,
        registry,
        completion_path,
        {
            "artifact_type": "validation_completion_projection",
            "artifact_version": "v1",
            "validation_run_id": result.validation_run_id,
            "execution_status": result.execution_status.value,
            "validation_disposition": result.validation_disposition.value,
            "claim_verdict_counts": dict(result.claim_verdict_counts),
            "contradicted_count": result.contradicted_count,
            "total_claims": result.total_claims,
            "canonical_result_path": canonical_record.path,
            "canonical_result_hash": result.stable_hash(),
        },
        artifact_role="validation_projection",
        artifact_type="validation_completion_projection",
        artifact_version="v1",
        producer="validation.current_validation",
        artifact_id=f"validation-completion:{Path(completion_path).name}",
        status=canonical_record.status,
        depends_on=[ArtifactDependencyRefV2.from_record(canonical_record)],
    )
    completion_path = completion_record.path
    return {
        "validation_run_result_file": result_path,
        "report_file": report_path,
        "manual_report_file": manual_path,
        "completion_report_file": completion_path,
    }


def _terminal(
    service: Any,
    *,
    status: ValidationExecutionStatus,
    policy: ValidationRepairPolicy,
    diagnostic: str,
    failure_reason: str = "",
    output_dir: str | os.PathLike[str] | None = None,
    result_artifact_id: str = "",
    result_artifact_type: str = "validation_run_result",
    result_artifact_role: str = "validation",
    dependency_records: Sequence[Any] | None = None,
) -> dict[str, Any]:
    result = ValidationRunResultV1.create(
        job_id=service.job_id,
        attempt_id=service.attempt_id,
        execution_status=status,
        report_id=f"validation-terminal:{service.attempt_id}:{diagnostic}",
        repair_policy=policy.value,
        diagnostics=(diagnostic,),
        failure_reason=failure_reason,
        review_has_citations=False,
        evidence_complete=False,
    )
    paths = _write_reports(
        service,
        result,
        policy,
        output_dir=output_dir,
        result_artifact_id=result_artifact_id,
        result_artifact_type=result_artifact_type,
        result_artifact_role=result_artifact_role,
        dependency_records=dependency_records,
    )
    return {
        "success": status in {ValidationExecutionStatus.SKIPPED},
        "report": None,
        "review_draft": None,
        "citation_manifest": None,
        "paper_artifacts": None,
        "validation_run_result": result,
        "validation_run_result_payload": result.to_dict(),
        "execution_status": result.execution_status.value,
        "validation_disposition": result.validation_disposition.value,
        **paths,
    }


def run_current_validation(
    service: Any,
    *,
    review_draft_override: Mapping[str, Any] | None = None,
    citation_manifest_override: Mapping[str, Any] | None = None,
    paper_artifacts_override: Sequence[Mapping[str, Any]] | None = None,
    review_draft_record_override: Any | None = None,
    citation_manifest_record_override: Any | None = None,
    output_dir: str | os.PathLike[str] | None = None,
    result_artifact_id: str = "",
    result_artifact_type: str = "validation_run_result",
    result_artifact_role: str = "validation",
) -> dict[str, Any]:
    """Execute current review validation from durable service-owned inputs.

    Explicit overrides are used only by the repair revalidation boundary.  The
    input records still carry their durable paths and hashes, so a repaired
    artifact cannot be validated merely because an in-memory dictionary looks
    plausible.
    """

    if not hasattr(service, "artifact_registry") or not hasattr(service, "workspace"):
        raise TypeError("run_current_validation requires ValidationExecutionService")
    try:
        policy = parse_repair_policy(service.settings.repair_policy())
    except Exception:
        policy = ValidationRepairPolicy.REPORT_ONLY
    if not service.stage2_validation_enabled():
        return _terminal(
            service,
            status=ValidationExecutionStatus.SKIPPED,
            policy=ValidationRepairPolicy.REPORT_ONLY,
            diagnostic="review_validation_disabled",
            output_dir=output_dir,
            result_artifact_id=result_artifact_id,
            result_artifact_type=result_artifact_type,
            result_artifact_role=result_artifact_role,
            dependency_records=(
                review_draft_record_override,
                citation_manifest_record_override,
            )
            if output_dir
            else None,
        )

    review_draft, citation_manifest, paper_artifacts, preprocess, metadata = _load_inputs(
        service,
        review_draft_override=review_draft_override,
        citation_manifest_override=citation_manifest_override,
        paper_artifacts_override=paper_artifacts_override,
    )
    if review_draft is None or citation_manifest is None:
        return _terminal(
            service,
            status=ValidationExecutionStatus.FAILED,
            policy=policy,
            diagnostic="validation_inputs_missing",
            failure_reason="current review draft or citation manifest is missing",
            output_dir=output_dir,
            result_artifact_id=result_artifact_id,
            result_artifact_type=result_artifact_type,
            result_artifact_role=result_artifact_role,
            dependency_records=(
                review_draft_record_override,
                citation_manifest_record_override,
            )
            if output_dir
            else None,
        )

    checkpoint_root = getattr(service.workspace.paths, "checkpoints_dir", "")
    validator = ReviewValidator(
        review_draft,
        citation_manifest,
        paper_artifacts,
        preprocess,
        metadata,
        edge_checkpoint_store=ValidationEdgeCheckpointStore(checkpoint_root),
    )
    worker_count = max(1, int(getattr(service.settings.runtime, "max_workers", 1) or 1))
    try:
        base_report = validator.validate(max_workers=worker_count)
    except TypeError:
        base_report = validator.validate()
    results = _adjudicate(service, base_report.citation_results)
    report = _build_report(results)
    (
        input_artifacts,
        expected_claim_count,
        review_has_citations,
        evidence_complete,
        degradation_reasons,
    ) = _input_contract(
        service,
        review_draft,
        citation_manifest,
        paper_artifacts,
        review_draft_record_override=review_draft_record_override,
        citation_manifest_record_override=citation_manifest_record_override,
    )
    result = ValidationRunResultV1.from_report(
        report,
        job_id=service.job_id,
        attempt_id=service.attempt_id,
        repair_policy=policy.value,
        input_artifacts=input_artifacts,
        expected_claim_count=expected_claim_count,
        review_has_citations=review_has_citations,
        evidence_complete=evidence_complete,
        repair_status="report_only" if policy is ValidationRepairPolicy.REPORT_ONLY else "not_needed",
        recheck_status="not_required",
        degradation_reasons=degradation_reasons,
    )
    paths = _write_reports(
        service,
        result,
        policy,
        output_dir=output_dir,
        result_artifact_id=result_artifact_id,
        result_artifact_type=result_artifact_type,
        result_artifact_role=result_artifact_role,
        dependency_records=(
            review_draft_record_override,
            citation_manifest_record_override,
        )
        if output_dir
        else None,
    )
    manual_items = [
        item for item in report.citation_results
        if item.conclusion is ValidationConclusion.NEEDS_REVIEW
    ]
    return {
        "success": bool(result.execution_status is ValidationExecutionStatus.SUCCEEDED),
        "status": "success",
        "report": report,
        "review_draft": review_draft,
        "citation_manifest": citation_manifest,
        "paper_artifacts": paper_artifacts,
        "manual_review_items": manual_items,
        "repair_policy": policy.value,
        "unsafe_auto_rewrite_enabled": unsafe_auto_rewrite_enabled(policy),
        "validation_run_result": result,
        "validation_run_result_payload": result.to_dict(),
        "execution_status": result.execution_status.value,
        "validation_disposition": result.validation_disposition.value,
        "revalidation": bool(output_dir),
        **paths,
    }


__all__ = ["run_current_validation"]

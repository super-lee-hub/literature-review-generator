"""Current, durable review-validation execution.

The public entry point in this module accepts only
``ValidationExecutionService``.  It loads the registered current artifacts,
validates citation bundles against paper evidence, optionally runs the
configured Validator provider, and persists the canonical v1 run result plus
its projections.  The historical top-level ``validator`` module is not part
of this execution path.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from ai_interface import _load_api_runtime_settings
from models import APIConfig
from runtime.provider_context import ProviderContextProfile
from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderBudgetExceeded,
    hash_json,
)
from services.artifact_registry import ArtifactDependencyRefV2, file_sha256
from services.job_workspace import publish_bytes_artifact, publish_json_artifact
from services.model_capabilities import resolve_model_capability
from services.model_selection import get_validator_api_config
from services.repair_policy import (
    ValidationRepairPolicy,
    parse_repair_policy,
    requires_manual_confirmation,
    unsafe_auto_rewrite_enabled,
)
from validation.adjudication_checkpoint import (
    AdjudicationCheckpointStore,
    sanitized_route_hash,
)
from validation.adjudication_reuse import (
    _request_payload as build_adjudication_request_payload,
)
from validation.adjudication_reuse import (
    adjudication_call_id,
    adjudication_schema_hash,
)
from validation.edge_checkpoint import ValidationEdgeCheckpointStore
from validation.evidence_loader import ValidationSourceAuthorityError
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


_CITATION_TOKEN = re.compile(r"\[\[cite(?:_ref)?:[^\]]+\]\]")


def _review_text_inventory(
    review_draft: Mapping[str, Any],
) -> tuple[int, dict[str, str], tuple[str, ...]]:
    """Count citations and collect canonical paragraph/cell text bindings."""
    from services.review_draft import iter_review_text_blocks, validate_review_section_writer_scope

    content = review_draft.get("content")
    sections = content.get("sections") if isinstance(content, Mapping) else None
    if not isinstance(sections, list):
        return 0, {}, ("review_sections_missing",)

    citation_count = 0
    native_cell_text: dict[str, str] = {}
    seen_block_ids: set[str] = set()
    issues: list[str] = []
    for section_index, section in enumerate(sections, start=1):
        if not isinstance(section, Mapping):
            issues.append(f"review_section_writer_scope_invalid:{section_index}")
            continue
        try:
            text_blocks = validate_review_section_writer_scope(section)
        except (AttributeError, KeyError, TypeError, ValueError):
            issues.append(f"review_section_writer_scope_invalid:{section_index}")
            try:
                text_blocks = list(iter_review_text_blocks(section))
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
        for block in text_blocks:
            if not isinstance(block, Mapping):
                continue
            block_id = str(block.get("block_id") or "").strip()
            if not block_id or block_id in seen_block_ids:
                issues.append(f"review_text_block_identity_invalid:{section_index}")
            else:
                seen_block_ids.add(block_id)
            text = str(block.get("text") or "")
            citations = block.get("citations")
            structured_count = len(citations) if isinstance(citations, list) else 0
            token_count = len(_CITATION_TOKEN.findall(text))
            citation_count += max(structured_count, token_count)
            if block.get("table_id") and block_id:
                native_cell_text[block_id] = text
    return citation_count, native_cell_text, tuple(dict.fromkeys(issues))


def _native_citation_span_matches_text(
    occurrence: Mapping[str, Any],
    block_text: str,
) -> bool:
    token = occurrence.get("citation_token")
    spans = occurrence.get("spans")
    if not isinstance(token, str) or not token or not isinstance(spans, list) or not spans:
        return False
    for span in spans:
        if not isinstance(span, Mapping):
            return False
        start, end = span.get("start_offset"), span.get("end_offset")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or not 0 <= start < end <= len(block_text)
        ):
            return False
        actual = block_text[start:end]
        if actual != token or span.get("text") != actual:
            return False
    return True


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

    reload_registry = getattr(service.artifact_registry, "reload", None)
    if callable(reload_registry):
        reload_registry()
    paper_artifacts: list[dict[str, Any]] = [
        dict(item)
        for item in (paper_artifacts_override or ())
        if isinstance(item, Mapping)
    ]
    records = list(service.artifact_registry.list_records())
    source_authority_diagnostics: list[str] = []
    if paper_artifacts_override is None:
        for record in records:
            if record.artifact_type != "paper_artifact" or record.status != "ready":
                continue
            payload = _read_json(record.path)
            if payload is not None:
                try:
                    if file_sha256(record.path) != record.content_hash:
                        source_authority_diagnostics.append(
                            f"paper_artifact_hash_mismatch:{record.artifact_id}"
                        )
                        continue
                except OSError:
                    source_authority_diagnostics.append(
                        f"paper_artifact_unreadable:{record.artifact_id}"
                    )
                    continue
                payload["_registry_path"] = record.path
                payload["_registry_artifact_id"] = record.artifact_id
                payload["_registry_artifact_hash"] = record.content_hash
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
                try:
                    if file_sha256(record.path) != record.content_hash:
                        source_authority_diagnostics.append(
                            f"paper_artifact_hash_mismatch:{record.artifact_id}"
                        )
                        continue
                except OSError:
                    source_authority_diagnostics.append(
                        f"paper_artifact_unreadable:{record.artifact_id}"
                    )
                    continue
                payload["_registry_path"] = record.path
                payload["_registry_artifact_id"] = record.artifact_id
                payload["_registry_artifact_hash"] = record.content_hash
                paper_artifacts.append(payload)

    all_binding_records: list[Any] = [
        record
        for record in records
        if record.artifact_type == "validation_source_binding" and record.status == "ready"
    ]
    binding_records: list[Any] = []
    binding_history_present = bool(all_binding_records)
    if paper_artifacts_override is None and all_binding_records:
        # Lane B is resolved per cited canonical paper key.  A local artifact
        # for one paper must not suppress an external authority for another.
        from validation.source_binding import (
            resolve_bound_paper_artifacts,
            validation_source_binding_payload_hash,
            validation_source_binding_semantic_hash,
        )

        current_binding_id = str(
            getattr(service, "current_validation_source_binding_id", "") or ""
        ).strip()
        current_binding_hash = str(
            getattr(service, "current_validation_source_binding_hash", "") or ""
        ).strip()
        current_binding_semantic_hash = str(
            getattr(service, "current_validation_source_binding_semantic_hash", "")
            or ""
        ).strip()
        current_binding_content_hash = str(
            getattr(service, "current_validation_source_binding_content_hash", "")
            or ""
        ).strip()
        current_binding_record = getattr(service, "validation_source_binding_record", None)
        has_explicit_selector = any(
            hasattr(service, attribute)
            for attribute in (
                "validation_source_binding_record",
                "current_validation_source_binding_id",
                "current_validation_source_binding_hash",
                "current_validation_source_binding_semantic_hash",
                "current_validation_source_binding_content_hash",
            )
        )
        if current_binding_record is not None:
            current_binding_id = str(
                getattr(current_binding_record, "artifact_id", "") or current_binding_id
            ).strip()
            current_binding_content_hash = str(
                getattr(current_binding_record, "content_hash", "")
                or current_binding_content_hash
            ).strip()
            metadata = getattr(current_binding_record, "metadata", {})
            current_binding_semantic_hash = str(
                metadata.get("semantic_payload_hash")
                if isinstance(metadata, Mapping)
                else ""
            ).strip()
            if not current_binding_semantic_hash:
                raw_current_payload = _read_json(current_binding_record.path)
                if isinstance(raw_current_payload, Mapping) and isinstance(
                    raw_current_payload.get("payload"), Mapping
                ):
                    raw_current_payload = raw_current_payload["payload"]
                if isinstance(raw_current_payload, Mapping):
                    current_binding_semantic_hash = validation_source_binding_semantic_hash(
                        raw_current_payload
                    )
        elif not current_binding_content_hash and not current_binding_semantic_hash:
            # Compatibility for pre-semantic callers: their one hash field was
            # the physical Registry content hash.
            current_binding_content_hash = current_binding_hash

        if current_binding_id:
            matches = [
                record
                for record in all_binding_records
                if record.artifact_id == current_binding_id
                and (
                    not current_binding_content_hash
                    or record.content_hash == current_binding_content_hash
                )
            ]
            if len(matches) == 1:
                selected = matches[0]
                if (
                    current_binding_content_hash
                    and selected.content_hash != current_binding_content_hash
                ):
                    source_authority_diagnostics.append(
                        "validation_source_binding_hash_mismatch"
                    )
                else:
                    from validation.source_binding import BINDING_ARTIFACT_VERSION

                    if selected.artifact_version != BINDING_ARTIFACT_VERSION:
                        source_authority_diagnostics.append(
                            "validation_source_binding_version_mismatch"
                        )
                    else:
                        raw_selected_payload = _read_json(selected.path)
                        if isinstance(raw_selected_payload, Mapping) and isinstance(
                            raw_selected_payload.get("payload"), Mapping
                        ):
                            raw_selected_payload = raw_selected_payload["payload"]
                        selected_semantic_hash = (
                            validation_source_binding_semantic_hash(
                                raw_selected_payload
                            )
                            if isinstance(raw_selected_payload, Mapping)
                            else ""
                        )
                        if (
                            current_binding_semantic_hash
                            and selected_semantic_hash != current_binding_semantic_hash
                        ):
                            source_authority_diagnostics.append(
                                "validation_source_binding_semantic_hash_mismatch"
                            )
                        else:
                            binding_records = [selected]
            else:
                source_authority_diagnostics.append(
                    "validation_source_binding_current_identity_unresolved"
                )
        elif not has_explicit_selector:
            if len(all_binding_records) == 1:
                # Compatibility for direct loader callers from before the runtime
                # carried an explicit selected binding identity.
                binding_records = list(all_binding_records)
            elif len(all_binding_records) > 1:
                # Never aggregate an unbounded history.  If an older caller did
                # not provide the selected identity, the only safe fallback is a
                # single semantic payload shared by all ready records; otherwise
                # fail closed instead of manufacturing an authority ambiguity.
                candidates_by_identity: dict[str, list[Any]] = {}
                for record in all_binding_records:
                    raw_payload = _read_json(record.path)
                    if isinstance(raw_payload, Mapping) and isinstance(
                        raw_payload.get("payload"), Mapping
                    ):
                        raw_payload = raw_payload["payload"]
                    if not isinstance(raw_payload, Mapping):
                        continue
                    try:
                        identity = validation_source_binding_payload_hash(raw_payload)
                    except (TypeError, ValueError):
                        continue
                    candidates_by_identity.setdefault(identity, []).append(record)
                if len(candidates_by_identity) == 1:
                    binding_records = [
                        sorted(
                            next(iter(candidates_by_identity.values())),
                            key=lambda record: record.artifact_id,
                        )[0]
                    ]
                else:
                    source_authority_diagnostics.append(
                        "validation_source_binding_current_identity_ambiguous"
                    )
        cited_keys = _cited_paper_ids(citation_manifest or {})
        if not cited_keys:
            cited_keys = list(
                dict.fromkeys(
                    str(service.get_paper_key(summary.get("paper_info") or {})).strip()
                    for summary in service.summaries
                    if isinstance(summary.get("paper_info"), Mapping)
                    and str(service.get_paper_key(summary.get("paper_info") or {})).strip()
                )
            )

        def paper_key(artifact: Mapping[str, Any]) -> str:
            identity = artifact.get("paper_identity")
            if not isinstance(identity, Mapping):
                return ""
            return str(
                identity.get("canonical_paper_key")
                or identity.get("source_paper_id")
                or ""
            ).strip()

        def authority_identity(artifact: Mapping[str, Any]) -> tuple[str, ...]:
            binding = artifact.get("_validation_source_binding")
            if isinstance(binding, Mapping):
                return (
                    str(binding.get("source_workspace_job_id") or ""),
                    str(binding.get("stage1_paper_artifact_id") or ""),
                    str(binding.get("stage1_paper_artifact_hash") or ""),
                    str(binding.get("evidence_manifest_artifact_id") or ""),
                    str(binding.get("evidence_manifest_hash") or ""),
                )
            stage1_inputs = artifact.get("stage1_inputs")
            inputs = stage1_inputs if isinstance(stage1_inputs, Mapping) else {}
            artifact_id = str(artifact.get("_registry_artifact_id") or "").strip()
            paper_record = next(
                (
                    record
                    for record in records
                    if record.artifact_type == "paper_artifact"
                    and (
                        artifact_id
                        and record.artifact_id == artifact_id
                        or not artifact_id
                        and _normal_path(record.path) == _normal_path(artifact.get("_registry_path"))
                    )
                ),
                None,
            )
            manifest_path = _normal_path(inputs.get("evidence_manifest_path"))
            manifest_hash = str(inputs.get("evidence_manifest_hash") or "")
            manifest_record = next(
                (
                    record
                    for record in records
                    if record.artifact_type == "evidence_manifest"
                    and _normal_path(record.path) == manifest_path
                    and record.content_hash == manifest_hash
                ),
                None,
            )
            return (
                str(getattr(paper_record, "job_id", "") or ""),
                str(getattr(paper_record, "artifact_id", artifact_id) or artifact_id),
                str(getattr(paper_record, "content_hash", "") or artifact.get("_registry_artifact_hash") or ""),
                str(getattr(manifest_record, "job_id", "") or ""),
                str(getattr(manifest_record, "artifact_id", "") or ""),
                str(getattr(manifest_record, "content_hash", "") or manifest_hash),
            )

        local_by_key: dict[str, list[dict[str, Any]]] = {}
        for artifact in paper_artifacts:
            key = paper_key(artifact)
            if key:
                local_by_key.setdefault(key, []).append(artifact)
        for key, candidates in local_by_key.items():
            identities = {authority_identity(candidate) for candidate in candidates}
            if len(identities) > 1:
                source_authority_diagnostics.append(
                    f"validation_source_authority_ambiguous:{key}"
                )

        bound_by_key: dict[str, list[dict[str, Any]]] = {}
        for binding_record in binding_records:
            binding_payload = _read_json(binding_record.path)
            if isinstance(binding_payload, Mapping) and isinstance(binding_payload.get("payload"), Mapping):
                binding_payload = binding_payload["payload"]
            if not isinstance(binding_payload, Mapping):
                source_authority_diagnostics.append("validation_source_binding_unreadable")
                continue
            try:
                binding_hash_matches = file_sha256(binding_record.path) == binding_record.content_hash
                if not binding_hash_matches:
                    source_authority_diagnostics.append("validation_source_binding_hash_mismatch")
                    bound_artifacts = []
                    binding_problems = ("validation_source_binding_hash_mismatch",)
                else:
                    bound_artifacts, binding_problems = resolve_bound_paper_artifacts(
                        binding_payload,
                        external_registry_resolver=getattr(
                            service, "validation_external_registry_resolver", None
                        ),
                        present_paper_keys=cited_keys,
                    )
            except (OSError, TypeError, ValueError) as exc:
                bound_artifacts = []
                binding_problems = (f"validation_source_binding_invalid:{exc}",)
            for problem in binding_problems:
                source_authority_diagnostics.append(str(problem))
                _log(service, "error", str(problem))
            for bound in bound_artifacts:
                key = paper_key(bound)
                if not key:
                    source_authority_diagnostics.append("validation_source_binding_paper_key_missing")
                    continue
                bound_by_key.setdefault(key, []).append(bound)

        for key, bound_candidates in bound_by_key.items():
            authorities = [*local_by_key.get(key, []), *bound_candidates]
            identities = {authority_identity(candidate) for candidate in authorities}
            if len(identities) > 1:
                source_authority_diagnostics.append(
                    f"validation_source_authority_ambiguous:{key}"
                )
                continue
            if not local_by_key.get(key):
                chosen = bound_candidates[0]
                paper_artifacts.append(chosen)
                local_by_key[key] = [chosen]

    if not paper_artifacts and not binding_history_present:
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
    setattr(service, "_validation_source_authority_diagnostics", tuple(source_authority_diagnostics))
    for artifact in paper_artifacts:
        identity = artifact.get("paper_identity") or {}
        if not isinstance(identity, Mapping):
            continue
        stage1_inputs = artifact.get("stage1_inputs")
        evidence = (
            stage1_inputs.get("preprocess_evidence", {})
            if isinstance(stage1_inputs, Mapping)
            else {}
        )
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
    reload_registry = getattr(service.artifact_registry, "reload", None)
    if callable(reload_registry):
        reload_registry()
    records = list(service.artifact_registry.list_records())
    degradation: list[str] = list(
        str(item)
        for item in getattr(service, "_validation_source_authority_diagnostics", ())
        if str(item).strip()
    )

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
    draft_citation_count, native_cell_text, section_issues = _review_text_inventory(review_draft)
    degradation.extend(section_issues)
    if isinstance(occurrences, list):
        for index, occurrence in enumerate(occurrences, start=1):
            if not isinstance(occurrence, Mapping):
                continue
            block_id = str(occurrence.get("block_id") or "").strip()
            if block_id in native_cell_text and not _native_citation_span_matches_text(
                occurrence,
                native_cell_text[block_id],
            ):
                occurrence_id = str(occurrence.get("occurrence_id") or index)
                degradation.append(f"native_table_citation_span_invalid:{occurrence_id}")
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
    binding_semantic_hash = str(
        getattr(service, "current_validation_source_binding_semantic_hash", "")
        or ""
    ).strip()
    legacy_binding_hash = str(
        getattr(service, "current_validation_source_binding_hash", "") or ""
    ).strip()
    binding_content_hash = str(
        getattr(service, "current_validation_source_binding_content_hash", "")
        or ""
    ).strip()
    if not binding_semantic_hash:
        binding_semantic_hash = legacy_binding_hash
    if not binding_content_hash and not hasattr(
        service, "current_validation_source_binding_semantic_hash"
    ):
        # Compatibility for callers that predate the semantic/content split.
        binding_content_hash = legacy_binding_hash
    input_artifacts = ValidationInputArtifactsV1(
        review_draft_id=review_id,
        review_draft_hash=review_hash,
        citation_manifest_id=manifest_id,
        citation_manifest_hash=manifest_hash,
        evidence_manifest_ids=tuple(item[0] for item in unique_evidence),
        evidence_manifest_hashes=tuple(item[1] for item in unique_evidence),
        validation_source_binding_id=str(
            getattr(service, "current_validation_source_binding_id", "") or ""
        ).strip(),
        validation_source_binding_hash=binding_semantic_hash,
        validation_source_binding_semantic_hash=binding_semantic_hash,
        validation_source_binding_content_hash=binding_content_hash,
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


def _positive_setting(value: Any, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _nonnegative_setting(value: Any, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _validator_stage_pretransport_inventory(
    service: Any,
    planned_requests: Sequence[tuple[Any, Any, Mapping[str, Any]]],
    api_config: APIConfig,
    *,
    scope: str,
) -> dict[str, Any]:
    """Measure exact eligible adjudication requests before the first call.

    The inventory stores only hashes, citation/paper/claim-unit bindings, and
    token/attempt bounds. It deliberately omits the prompt and evidence text.
    It is an upper bound because each planned row can still be satisfied by
    verified adjudication reuse under the existing single-flight boundary.
    This does not reserve the batch: ``run_adjudication_stage`` retains its
    existing per-call ProviderRuntime admission.
    """

    request_timeout_seconds, requested_attempts = _load_api_runtime_settings(
        api_config
    )
    requested_attempts = max(1, int(requested_attempts))
    attempt_limit = getattr(service, "validator_attempt_limit", None)
    if callable(attempt_limit):
        effective_attempts = max(1, int(cast(int, attempt_limit(api_config))))
        expected_call_attempt_cap = effective_attempts
    else:
        runtime_settings = getattr(service.settings, "runtime", None)
        try:
            validation_retry_limit = max(
                0, int(getattr(runtime_settings, "validation_retry_limit", 1))
            )
        except (TypeError, ValueError):
            validation_retry_limit = 1
        expected_call_attempt_cap = max(1, validation_retry_limit + 1)
        effective_attempts = (
            1
            if validation_retry_limit == 0
            else min(requested_attempts, expected_call_attempt_cap)
        )

    capability = resolve_model_capability(api_config)
    model = str(api_config.get("model") or "")
    output_fallback = 4_096
    reasoning_reserve = _nonnegative_setting(
        api_config.get("reasoning_reserve_tokens"), 0
    )
    safety_margin = _positive_setting(
        api_config.get("safety_margin_tokens"), 256
    )
    model_context_limit = _positive_setting(
        api_config.get("max_context_tokens"), 128_000
    )
    provider = str(api_config.get("provider_family") or capability.provider_family)
    endpoint_type = str(api_config.get("endpoint_type") or capability.endpoint_type)

    rows: list[dict[str, Any]] = []
    input_tokens_all_attempts = 0
    output_tokens_all_attempts = 0
    reasoning_tokens_all_attempts = 0
    profile_errors = 0
    for _result, packet, payload in planned_requests:
        output_tokens = max(1, int(payload.get("max_output_tokens") or output_fallback))
        # The transport profile reserves the per-request output allowance.
        # Use the actual packet limit rather than the config's higher ceiling.
        try:
            packet_profile = ProviderContextProfile.conservative(
                provider=provider,
                model=model,
                endpoint_type=endpoint_type,
                model_context_limit=model_context_limit,
                max_output_tokens=output_tokens,
                reasoning_reserve=reasoning_reserve,
                safety_margin=safety_margin,
            )
            estimate = packet_profile.estimate_request(payload)
            input_tokens: int | None = int(estimate["estimated_input_tokens"])
            within_input_budget: bool | None = bool(estimate["within_budget"])
            input_budget: int | None = int(packet_profile.input_budget)
        except (TypeError, ValueError):
            packet_profile = None
            estimate = {}
            input_tokens = None
            within_input_budget = None
            input_budget = None
            profile_errors += 1
        claim_unit_ids = sorted(
            {
                str(item.get("claim_unit_id") or "").strip()
                for item in packet.claim_units
                if isinstance(item, Mapping) and str(item.get("claim_unit_id") or "").strip()
            }
        )
        target_claim_unit_id = str(
            (packet.target_claim_unit or {}).get("claim_unit_id") or ""
        ).strip()
        if target_claim_unit_id and target_claim_unit_id not in claim_unit_ids:
            claim_unit_ids.append(target_claim_unit_id)
            claim_unit_ids.sort()
        paper_ids = sorted({str(value).strip() for value in packet.paper_ids if str(value).strip()})
        rows.append(
            {
                "call_id": adjudication_call_id(packet),
                "node_id": f"{packet.stage}:{packet.citation_set_key or 'validation'}",
                "adjudication_stage": packet.stage,
                "citation_set_key": str(packet.citation_set_key or ""),
                "paper_ids": paper_ids,
                "claim_unit_ids": claim_unit_ids,
                "target_claim_unit_id": target_claim_unit_id,
                "paper_evidence_packet_hashes": {
                    paper_id: hash_json(packet.per_paper_evidence_packets.get(paper_id) or {})
                    for paper_id in paper_ids
                },
                "request_hash": hash_json(payload),
                "schema_hash": adjudication_schema_hash(packet),
                "estimated_input_tokens": input_tokens,
                "input_budget": input_budget,
                "within_input_budget": within_input_budget,
                "estimate_status": (
                    "complete" if packet_profile is not None else "provider_profile_invalid"
                ),
                "requested_output_tokens": output_tokens,
                "reasoning_reserve_tokens": reasoning_reserve,
                "attempts_upper_bound": effective_attempts,
                "expected_call_max_attempts": expected_call_attempt_cap,
                "attempt_contract_matches_transport": (
                    effective_attempts <= expected_call_attempt_cap
                ),
                "request_timeout_seconds": int(request_timeout_seconds),
                "connect_timeout_seconds": _positive_setting(
                    api_config.get("connect_timeout_seconds"),
                    min(10, int(request_timeout_seconds)),
                ),
                "read_timeout_seconds": _positive_setting(
                    api_config.get("read_timeout_seconds"),
                    int(request_timeout_seconds),
                ),
                "total_timeout_seconds": _positive_setting(
                    api_config.get("total_timeout_seconds"),
                    int(request_timeout_seconds),
                ),
                "first_token_timeout_enforced": False,
                "conditional_on": "verified_adjudication_reuse_not_found",
            }
        )
        input_tokens_all_attempts += (input_tokens or 0) * effective_attempts
        output_tokens_all_attempts += output_tokens * effective_attempts
        reasoning_tokens_all_attempts += reasoning_reserve * effective_attempts

    request_plan_hash = hash_json(rows)
    attempt_contract_mismatch_count = sum(
        not bool(item["attempt_contract_matches_transport"]) for item in rows
    )
    plan: dict[str, Any] = {
        "schema_version": "validator-pretransport-inventory/v1",
        "stage_name": "stage4_validate",
        "scope": str(scope or "primary_validation"),
        "status": "materialized_upper_bound" if not profile_errors else "incomplete_profile",
        "request_builder": (
            "validation.llm_adjudicator.build_adjudication_packet -> "
            "validation.adjudication_reuse._request_payload -> "
            "ai_interface.canonical_provider_request_payload"
        ),
        "route_fingerprint": sanitized_route_hash(api_config),
        "provider_posts_emitted_at_plan": 0,
        "per_call_atomic_admission": True,
        "request_timeout_seconds": int(request_timeout_seconds),
        "request_count": len(rows),
        "logical_call_upper_bound": len(rows),
        "physical_attempts_upper_bound": sum(
            int(item["attempts_upper_bound"]) for item in rows
        ),
        "attempt_contract_mismatch_count": attempt_contract_mismatch_count,
        "estimated_input_tokens_all_attempts_upper_bound": input_tokens_all_attempts,
        "estimated_output_tokens_all_attempts_upper_bound": output_tokens_all_attempts,
        "estimated_reasoning_tokens_all_attempts_upper_bound": reasoning_tokens_all_attempts,
        "profile_error_count": profile_errors,
        "provider_transport_wall_seconds_upper_bound": sum(
            int(item["total_timeout_seconds"]) for item in rows
        ),
        "timeout_semantics": (
            "connect/read timeouts are clamped by total_timeout_seconds; "
            "first_token_timeout_seconds is not enforced by this transport"
        ),
        "first_token_timeout_enforced": False,
        "request_plan_hash": request_plan_hash,
        "requests": rows,
        "outcomes": [],
    }
    plan["inventory_hash"] = hash_json(plan)
    return plan


def _preflight_validator_aggregate_scope(
    service: Any,
    planned_requests: Sequence[tuple[Any, Any, Mapping[str, Any]]],
    api_config: APIConfig,
    inventory: dict[str, Any],
    *,
    controller: Any | None,
    verified_reuse: Mapping[str, Mapping[str, Any]],
) -> None:
    """Preflight the exact Validator miss set against the remaining run budget."""

    from runtime.provider_context import ProviderContextProfile
    from runtime.provider_routes import build_reachable_provider_route_plan
    from runtime.stage_planning import (
        ProviderStageRequestInventoryV1,
        VerifiedProviderReuseAuthorityV1,
        build_full_stage_request_plan_v1,
        build_provider_request_plan_row_v1,
        build_stage_plan,
    )

    if not planned_requests:
        return
    if controller is None or not isinstance(controller.budget, ProviderAggregateBudgetV2):
        return

    route_config = dict(getattr(service.settings, "sections", {}) or {})
    route_config["Validator_API"] = dict(api_config)
    stage_plan = build_stage_plan(
        action="validate_review",
        requested_stages=("validate",),
        validation_enabled=True,
        validation_required=True,
    )
    route_plan = build_reachable_provider_route_plan(
        route_config,
        action="validate_review",
        requested_stages=("validate",),
        stage_plan=stage_plan,
    )
    route = route_plan.route_for_role("validator")
    if not route.resolved:
        raise ProviderBudgetExceeded(
            "Validator stage preflight cannot bind its request inventory to the reachable route"
        )

    capability = resolve_model_capability(api_config)
    try:
        model_context_limit = max(1, int(api_config.get("max_context_tokens") or 128_000))
    except (TypeError, ValueError):
        model_context_limit = 128_000
    try:
        configured_output_tokens = max(1, int(api_config.get("max_output_tokens") or 4_096))
    except (TypeError, ValueError):
        configured_output_tokens = 4_096
    profile = ProviderContextProfile.conservative(
        provider=str(api_config.get("provider_family") or capability.provider_family),
        model=str(api_config.get("model") or ""),
        endpoint_type=str(api_config.get("endpoint_type") or capability.endpoint_type),
        model_context_limit=model_context_limit,
        max_output_tokens=configured_output_tokens,
        reasoning_reserve=_nonnegative_setting(
            api_config.get("reasoning_reserve_tokens"), 0
        ),
        safety_margin=_positive_setting(api_config.get("safety_margin_tokens"), 256),
    )
    inventory_rows = {
        str(item["call_id"]): item for item in inventory.get("requests") or ()
    }
    rows = []
    for _result, packet, payload in planned_requests:
        call_id = adjudication_call_id(packet)
        inventory_row = inventory_rows[call_id]
        cached = verified_reuse.get(call_id)
        reuse_authority = None
        retry_attempts = max(0, int(inventory_row["attempts_upper_bound"]) - 1)
        if cached is not None:
            raw_reuse = cached["raw_reuse"]
            reuse_authority = VerifiedProviderReuseAuthorityV1(
                request_hash=hash_json(payload),
                route_identity=route.identity,
                receipt_hash=str(raw_reuse.get("source_receipt_hash") or ""),
                output_hash=str(raw_reuse.get("provider_output_artifact_hash") or ""),
                authority_hash=str(cached["reuse_record"].content_hash),
            )
            retry_attempts = 0
        request_output_tokens = max(
            1, int(payload.get("max_output_tokens") or configured_output_tokens)
        )
        total_timeout = max(1, int(inventory_row["total_timeout_seconds"]))
        rows.append(
            build_provider_request_plan_row_v1(
                stage_name="validate",
                request_id=call_id,
                source_builder=(
                    "validation.llm_adjudicator.build_adjudication_packet -> "
                    "validation.adjudication_reuse._request_payload"
                ),
                route=route,
                request_payload=payload,
                profile=profile,
                retry_attempts=retry_attempts,
                requested_output_tokens=request_output_tokens,
                reasoning_reserve_tokens=profile.reasoning_reserve,
                verified_reuse=reuse_authority,
                retry_policy="shared_optional",
                wall_seconds_upper_bound=float(total_timeout),
            )
        )

    stage_inventory = ProviderStageRequestInventoryV1(
        stage_name="validate",
        source_builder=(
            "validation.llm_adjudicator.build_adjudication_packet -> "
            "validation.adjudication_reuse._request_payload"
        ),
        requests=tuple(rows),
    )
    snapshot = controller.snapshot()
    budget = controller.budget

    def remaining(field: str, limit: int) -> int:
        return max(
            0,
            limit - int(snapshot[f"{field}_used"]) - int(snapshot[f"{field}_reserved"]),
        )

    remaining_budget = ProviderAggregateBudgetV2(
        max_provider_calls_total=remaining("calls", budget.max_provider_calls_total),
        max_output_tokens_total=remaining(
            "output_tokens", budget.max_output_tokens_total
        ),
        max_retry_attempts_total=remaining(
            "retry_attempts", budget.max_retry_attempts_total
        ),
        max_wall_seconds=max(
            0.0,
            budget.max_wall_seconds - float(snapshot["elapsed_seconds"]),
        ),
    )
    projection = build_full_stage_request_plan_v1(
        stage_plan=stage_plan,
        reachable_route_plan=route_plan,
        stage_inventories=(stage_inventory,),
        aggregate_budget=remaining_budget,
    )
    budget_status = projection["budget_status"]
    preflight_status = (
        "blocked_budget"
        if any(value == "exceeded" for value in budget_status.values())
        else "within_budget"
        if projection["ready_for_transport"]
        else "incomplete_envelope"
    )
    inventory["aggregate_preflight"] = {
        "schema_version": "validator-aggregate-preflight/v1",
        "status": preflight_status,
        "provider_posts_emitted": 0,
        "remaining_budget": remaining_budget.to_dict(),
        "budget_status": budget_status,
        "totals": projection["totals"],
        "projection_identity_hash": projection["projection_identity_hash"],
    }
    inventory.pop("inventory_hash", None)
    inventory["inventory_hash"] = hash_json(inventory)
    service._validator_stage_pretransport_inventory = inventory

    if preflight_status == "within_budget":
        return
    if preflight_status == "blocked_budget":
        labels = (
            ("provider_calls", "provider call"),
            ("requested_output_tokens", "output token"),
            ("provider_retries", "retry"),
            ("per_request_context", "provider request context"),
            ("wall_time", "wall-time"),
        )
        exceeded = next(
            (label for key, label in labels if budget_status.get(key) == "exceeded"),
            "aggregate provider resource",
        )
        raise ProviderBudgetExceeded(
            f"Validator stage preflight exceeds remaining aggregate {exceeded} budget "
            f"(requests={len(rows)}, projection={projection['projection_identity_hash']})"
        )
    raise ProviderBudgetExceeded(
        "Validator stage preflight cannot prove a complete remaining-budget envelope "
        f"(requests={len(rows)}, projection={projection['projection_identity_hash']})"
    )


def _adjudicate(
    service: Any,
    results: Sequence[CitationValidationResult],
    *,
    scope: str = "primary_validation",
    input_records: Sequence[Any] | None = None,
    paper_records: Sequence[Any] | None = None,
    repair_transaction_record: Any | None = None,
) -> list[CitationValidationResult]:
    config = get_validator_api_config(
        {"Validator_API": dict(service.settings.section("Validator_API"))}
    )
    if not str(config.get("api_key") or "").strip() or not str(config.get("model") or "").strip():
        service._validator_stage_pretransport_inventory = {
            "schema_version": "validator-pretransport-inventory/v1",
            "stage_name": "stage4_validate",
            "scope": scope,
            "status": "route_unconfigured",
            "request_builder": (
                "validation.llm_adjudicator.build_adjudication_packet -> "
                "validation.adjudication_reuse._request_payload -> "
                "ai_interface.canonical_provider_request_payload"
            ),
            "route_fingerprint": sanitized_route_hash(config),
            "provider_posts_emitted_at_plan": 0,
            "per_call_atomic_admission": True,
            "request_count": 0,
            "logical_call_upper_bound": 0,
            "physical_attempts_upper_bound": 0,
            "request_plan_hash": hash_json([]),
            "requests": [],
            "outcomes": [],
        }
        service._validator_stage_pretransport_inventory["inventory_hash"] = hash_json(
            service._validator_stage_pretransport_inventory
        )
        return list(results)
    checkpoint_root = getattr(service.workspace.paths, "checkpoints_dir", "")
    checkpoint_store = AdjudicationCheckpointStore(
        Path(checkpoint_root) / "validation_adjudication"
    )
    route_hash = sanitized_route_hash(config)
    planned: list[dict[str, Any]] = []
    planned_requests: list[tuple[Any, Any, Mapping[str, Any]]] = []
    for result in results:
        if not result.claim_text.strip() or not result.paper_ids:
            planned.append({"result": result, "packet": None, "checkpoint_key": None})
            continue
        packet = build_adjudication_packet(result, stage="primary")
        key = checkpoint_store.key_for(
            packet=asdict(packet),
            stage=packet.stage,
            route_hash=route_hash,
        )
        request_payload = build_adjudication_request_payload(packet, config)
        planned.append(
            {"result": result, "packet": packet, "checkpoint_key": key}
        )
        planned_requests.append((result, packet, request_payload))
    inventory = _validator_stage_pretransport_inventory(
        service,
        planned_requests,
        config,
        scope=scope,
    )
    service._validator_stage_pretransport_inventory = inventory
    from runtime.provider_runtime import provider_budget_controller_from_environment

    aggregate_controller = provider_budget_controller_from_environment()
    verified_reuse: dict[str, dict[str, Any]] = {}
    if aggregate_controller is not None and isinstance(
        aggregate_controller.budget, ProviderAggregateBudgetV2
    ):
        for item in planned:
            packet = item.get("packet")
            key = item.get("checkpoint_key")
            if packet is None or key is None:
                continue
            with checkpoint_store.single_flight(key):
                report, reuse_record, reuse_error = service.find_verified_adjudication_reuse(
                    packet=packet,
                    api_config=config,
                )
                item["preflight_reuse_error"] = reuse_error
                if report is None or reuse_record is None:
                    continue
                raw_reuse = json.loads(Path(reuse_record.path).read_text(encoding="utf-8"))
                output_record = service.artifact_registry.get(
                    str(raw_reuse.get("provider_output_artifact_id") or "")
                )
                if output_record is None or output_record.status != "ready":
                    continue
                call_id = adjudication_call_id(packet)
                reuse_state = {
                    "report": report,
                    "reuse_record": reuse_record,
                    "output_record": output_record,
                    "raw_reuse": raw_reuse,
                }
                item["preflight_verified_reuse"] = reuse_state
                verified_reuse[call_id] = reuse_state
    _preflight_validator_aggregate_scope(
        service,
        planned_requests,
        config,
        inventory,
        controller=aggregate_controller,
        verified_reuse=verified_reuse,
    )
    dependencies = list(input_records) if input_records is not None else [
        getattr(service, "review_draft_record", None), getattr(service, "citation_manifest_record", None),
    ]
    dependencies.extend(paper_records if paper_records is not None else getattr(service, "paper_artifact_records", ()) or ())
    dependencies.extend(getattr(service, "visual_artifact_records", ()) or ())
    dependencies.append(getattr(service, "validation_source_binding_record", None))
    unique_dependencies = {record.artifact_id: record for record in dependencies if record is not None}
    provisional = False
    for record in unique_dependencies.values():
        if (scope == "repair_revalidation" and record.status == "quarantined"
                and record.artifact_type in {"review_draft", "citation_manifest"}
                and record.metadata.get("repair_validation_candidate") is True):
            current = service.artifact_registry.get(record.artifact_id)
            source = service.artifact_registry.get(str(record.metadata.get("source_artifact_id") or ""))
            transaction = repair_transaction_record
            if (transaction is None or transaction.job_id != service.job_id
                    or transaction.artifact_type != "repair_transaction" or transaction.status != "quarantined"
                    or service.artifact_registry.get(transaction.artifact_id) != transaction
                    or file_sha256(transaction.path) != transaction.content_hash):
                raise RuntimeError("provisional repair input lacks its bound repair transaction")
            transaction_payload = _read_json(transaction.path)
            if (not isinstance(transaction_payload, Mapping) or transaction_payload.get("status") != "quarantined"
                    or transaction_payload.get("job_id") != service.job_id or source is None
                    or source.artifact_id not in transaction_payload.get("applied_artifact_ids", ())):
                raise RuntimeError("provisional repair source is outside its repair transaction")
            if (current != record or record.job_id != service.job_id or file_sha256(record.path) != record.content_hash
                    or source is None or source.job_id != service.job_id or source.content_hash != record.content_hash
                    or file_sha256(source.path) != source.content_hash):
                raise RuntimeError("provisional repair input binding changed before Validator inventory")
            provisional = True
        else:
            service.artifact_registry.verify_ready_artifact_closure(record)
    if provisional:
        if repair_transaction_record is None:
            raise RuntimeError("provisional Validator inventory has no repair transaction")
        unique_dependencies[repair_transaction_record.artifact_id] = repair_transaction_record
    inventory["input_artifacts"] = [
        {"artifact_id": record.artifact_id, "artifact_hash": record.content_hash, "status": record.status}
        for record in sorted(unique_dependencies.values(), key=lambda record: record.artifact_id)
    ]
    inventory["validation_source_authority_hash"] = str(
        getattr(service, "validation_source_authority_hash", "") or ""
    )
    inventory["inventory_hash"] = hash_json({key: value for key, value in inventory.items() if key != "inventory_hash"})
    inventory_record = publish_json_artifact(
        service.publication_context, service.artifact_registry,
        service.workspace.artifact_path(f"validation/pretransport_{inventory['inventory_hash']}.json"),
        dict(inventory), artifact_id=f"validator-pretransport:{inventory['inventory_hash']}",
        artifact_role="validator_pretransport_inventory", artifact_type="validator_pretransport_inventory",
        artifact_version="v1", producer="validation.current_validation",
        status="quarantined" if provisional else "ready",
        depends_on=[ArtifactDependencyRefV2.from_record(record) for record in unique_dependencies.values()],
    )
    service._validator_stage_pretransport_inventory_record = inventory_record
    output: list[CitationValidationResult] = []
    outcomes: list[dict[str, str]] = []
    for item in planned:
        result = item["result"]
        packet = item.get("packet")
        key = item.get("checkpoint_key")
        if packet is None or key is None:
            output.append(result)
            continue
        call_id = adjudication_call_id(packet)
        cached = item.get("preflight_verified_reuse")
        if isinstance(cached, Mapping):
            service.register_verified_reuse_call(
                packet=packet,
                api_config=config,
                reuse_record=cached["reuse_record"],
                output_record=cached["output_record"],
                output_payload=cached["report"],
            )
            outcomes.append({"call_id": call_id, "status": "verified_reuse"})
            output.append(_apply_adjudication(result, cached["report"]))
            continue
        outcome_status = "provider_call_not_started"
        with checkpoint_store.single_flight(key):
            report, reuse_record, reuse_error = service.find_verified_adjudication_reuse(
                packet=packet,
                api_config=config,
            )
            if not reuse_error:
                reuse_error = str(item.get("preflight_reuse_error") or "")
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
                    outcome_status = "verified_reuse"
            if report is None:
                if reuse_record is not None and reuse_error:
                    _log(
                        service,
                        "warning",
                        f"adjudication reuse rejected: {reuse_error}",
                    )
                outcome_status = "transport_call_dispatched"
                report = run_adjudication_stage(service, config, packet)
                if isinstance(report, Mapping):
                    outcome_status = "transport_call_returned_result"
                else:
                    outcome_status = "transport_call_returned_no_result"
                if isinstance(report, Mapping):
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
            if report is not None and not isinstance(reuse_record, Mapping) and outcome_status == "provider_call_not_started":
                outcome_status = "reuse_report_without_record"
        outcomes.append({"call_id": call_id, "status": outcome_status})
        if isinstance(report, Mapping):
            output.append(_apply_adjudication(result, report))
        else:
            output.append(result)
    inventory["outcomes"] = outcomes
    inventory["verified_reuse_count"] = sum(
        item["status"] == "verified_reuse" for item in outcomes
    )
    inventory["transport_call_candidate_count"] = sum(
        item["status"] not in {"verified_reuse", "reuse_report_without_record"}
        for item in outcomes
    )
    inventory["inventory_hash"] = hash_json(
        {key: value for key, value in inventory.items() if key != "inventory_hash"}
    )
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
    dependency_error = ""
    declared_count = bool(
        result.input_artifacts.review_draft_id
        or result.input_artifacts.citation_manifest_id
        or result.input_artifacts.evidence_manifest_ids
    )
    if declared_count:
        from validation.input_dependencies import (
            ValidationInputDependencyError,
            resolve_validation_input_dependencies,
        )

        try:
            dependencies = resolve_validation_input_dependencies(
                registry,
                result.input_artifacts,
                external_registry_resolver=getattr(
                    service, "validation_external_registry_resolver", None
                ),
            )
        except ValidationInputDependencyError as exc:
            dependency_error = str(exc)
            dependencies = []
    else:
        for record in records_by_id.values():
            if record is not None:
                dependencies.append(ArtifactDependencyRefV2.from_record(record))
    dependencies_verified = bool(not declared_count or (not dependency_error and dependencies))
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
        status=(
            "ready"
            if result.contract_satisfied and dependencies_verified and not output_dir
            else "quarantined"
        ),
        depends_on=dependencies,
        external_registry_resolver=getattr(
            service, "validation_external_registry_resolver", None
        ),
        metadata={
            "execution_status": result.execution_status.value,
            "validation_disposition": result.validation_disposition.value,
            "contract_satisfied": result.contract_satisfied,
            "dependency_error": dependency_error,
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
    validation_scope: str = "current_validation",
    result_artifact_id: str = "",
    result_artifact_type: str = "validation_run_result",
    result_artifact_role: str = "validation",
    repair_transaction_record: Any | None = None,
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
    invalid_review_structure = tuple(
        reason
        for reason in degradation_reasons
        if reason.startswith((
            "review_section_writer_scope_invalid:",
            "review_text_block_identity_invalid:",
            "native_table_citation_span_invalid:",
        ))
    )
    if invalid_review_structure:
        return _terminal(
            service,
            status=ValidationExecutionStatus.FAILED,
            policy=policy,
            diagnostic="validation_review_structure_invalid",
            failure_reason="; ".join(invalid_review_structure),
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
    from validation.source_binding import (
        BINDING_CONTRACT_VERSION,
        build_validation_source_authority_fingerprint,
    )

    binding_record = getattr(service, "validation_source_binding_record", None)
    binding_metadata = getattr(binding_record, "metadata", {})
    binding_contract_version = str(
        binding_metadata.get("binding_contract_version")
        if isinstance(binding_metadata, Mapping)
        else ""
    ).strip() or BINDING_CONTRACT_VERSION

    source_fingerprint, source_authority_hash, source_diagnostics = (
        build_validation_source_authority_fingerprint(
            paper_artifacts=paper_artifacts,
            registry=service.artifact_registry,
            cited_paper_keys=_cited_paper_ids(citation_manifest),
            current_binding_artifact_id=str(
                getattr(service, "current_validation_source_binding_id", "") or ""
            ).strip(),
            current_binding_content_hash=str(
                getattr(
                    service,
                    "current_validation_source_binding_content_hash",
                    "",
                )
                or ""
            ).strip(),
            current_binding_semantic_hash=str(
                getattr(
                    service,
                    "current_validation_source_binding_semantic_hash",
                    "",
                )
                or getattr(service, "current_validation_source_binding_hash", "")
                or ""
            ).strip(),
            binding_contract_version=binding_contract_version,
        )
    )
    if source_diagnostics:
        _log(
            service,
            "error",
            "validation source authority fingerprint: " + "; ".join(source_diagnostics),
        )
        evidence_complete = False
    if hasattr(service, "bind_validation_source_authority"):
        service.bind_validation_source_authority(source_fingerprint, source_authority_hash)
    input_artifacts = replace(
        input_artifacts,
        validation_source_authority_hash=source_authority_hash,
        validation_source_authority_fingerprint=source_fingerprint,
    )
    degradation_reasons = tuple(
        dict.fromkeys((*degradation_reasons, *source_diagnostics))
    )
    if source_diagnostics:
        return _terminal(
            service,
            status=ValidationExecutionStatus.FAILED,
            policy=policy,
            diagnostic="validation_source_authority_invalid",
            failure_reason="; ".join(source_diagnostics),
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
        validation_source_authority_hash=source_authority_hash,
    )
    worker_count = max(1, int(getattr(service.settings.runtime, "max_workers", 1) or 1))
    try:
        base_report = validator.validate(max_workers=worker_count)
    except TypeError:
        base_report = validator.validate()
    except ValidationSourceAuthorityError as exc:
        return _terminal(
            service,
            status=ValidationExecutionStatus.FAILED,
            policy=policy,
            diagnostic="validation_source_authority_invalid",
            failure_reason=str(exc),
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
    results = _adjudicate(
        service,
        base_report.citation_results,
        scope=validation_scope,
        input_records=(review_draft_record_override or service.review_draft_record,
                       citation_manifest_record_override or service.citation_manifest_record)
        if review_draft_record_override is not None or citation_manifest_record_override is not None else None,
        paper_records=[service.artifact_registry.get(str(item.get("_registry_artifact_id") or ""))
                       for item in paper_artifacts]
        if paper_artifacts_override is not None else None,
        repair_transaction_record=repair_transaction_record,
    )
    report = _build_report(results)
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
    validator_inventory = getattr(
        service, "_validator_stage_pretransport_inventory", None
    )
    if isinstance(validator_inventory, Mapping):
        inventory_diagnostic = "validator_pretransport_inventory_v1:" + json.dumps(
            dict(validator_inventory),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        result = replace(
            result,
            diagnostics=(*result.diagnostics, inventory_diagnostic),
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
        "validator_pretransport_inventory": (
            dict(validator_inventory)
            if isinstance(validator_inventory, Mapping)
            else None
        ),
        "execution_status": result.execution_status.value,
        "validation_disposition": result.validation_disposition.value,
        "revalidation": bool(output_dir),
        **paths,
    }


__all__ = ["run_current_validation"]

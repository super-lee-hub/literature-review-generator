"""Evidence-bound Writer Review v3 execution."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, cast

from models import APIConfig
from runtime.provider_completion import ProviderCompletionEvaluator
from runtime.provider_receipt_closure import (
    ExpectedProviderCall,
    ProviderReceiptClosure,
)
from runtime.provider_routes import build_reachable_provider_route_plan
from runtime.source_revision import PROVIDER_SEMANTIC_CONTRACT_V2
from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderBudgetExceeded,
    ProviderBudgetV1,
    ProviderCallReceiptV1,
    ProviderResponseSnapshotSink,
    ProviderRuntime,
    ProviderRuntimeContractError,
    ProviderRuntimeLedger,
    _redact_mapping,
    canonical_provider_request_payload,
    compute_closure_epoch_id,
    hash_json,
    hash_text,
    provider_budget_controller_from_environment,
)
from runtime.stage_planning import (
    ProviderRequestPlanRowV1,
    ProviderStageRequestInventoryV1,
    VerifiedProviderReuseAuthorityV1,
    build_full_stage_request_plan_v1,
    build_provider_request_plan_row_v1,
)
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    file_sha256,
)
from services.citation_ref_catalog import (
    build_document_ref_catalog,
    extract_ref_ids_from_token,
    resolve_ref_id,
    validate_document_ref_catalog,
)
from services.job_workspace import (
    JobWorkspace,
    publish_bytes_artifact,
    publish_json_artifact,
)
from services.prompt_registry import PromptRegistry, PromptRegistryError
from services.queue_service import LocalPublicationContext
from services.settings import ApplicationSettings

WriterCallable = Callable[..., Mapping[str, Any]]


@dataclass(frozen=True)
class ReviewGenerationResult:
    sections: tuple[dict[str, Any], ...]
    citation_ref_catalog: dict[str, Any]
    citation_ref_catalog_path: str
    receipt_ids: tuple[str, ...]
    receipt_ledger_path: str


@dataclass(frozen=True)
class _PreparedWriterSection:
    section_number: int
    section_id: str
    section: Mapping[str, Any]
    packet: Mapping[str, Any]
    allowed_ref_ids: tuple[str, ...]
    runtime: ProviderRuntime
    prompt: str
    request_payload: dict[str, Any]
    binding: dict[str, Any]
    persisted: dict[str, Any] | None
    writer_task_scope: dict[str, Any] | None


class ReviewGenerationService:
    """Run the configured Writer once per durable evidence-bound section."""

    def __init__(
        self,
        *,
        job_id: str,
        attempt_id: str,
        workspace: JobWorkspace,
        artifact_registry: ArtifactRegistry,
        settings: ApplicationSettings,
        summaries: Sequence[Mapping[str, Any]],
        writer: WriterCallable | None = None,
        cancellation_checker: Callable[[], None] | None = None,
        logger: logging.Logger | None = None,
        publication_context: Any | None = None,
        response_snapshot_sink: ProviderResponseSnapshotSink | None = None,
        source_revision_replay_context: Any | None = None,
        provider_semantic_contract_version: str | None = None,
    ) -> None:
        self.job_id = str(job_id)
        self.attempt_id = str(attempt_id or "review")
        self.workspace = workspace
        self.registry = artifact_registry
        self.response_snapshot_sink = response_snapshot_sink
        self.source_revision_replay_context = source_revision_replay_context
        self._source_revision_sections: dict[str, dict[str, Any]] = {}
        self._source_revision_proofs: dict[str, tuple[Any, Any, Any]] = {}
        self._source_revision_catalog: Mapping[str, Any] = {}
        if provider_semantic_contract_version not in (None, PROVIDER_SEMANTIC_CONTRACT_V2):
            raise ValueError("unsupported Writer provider semantic contract")
        self.provider_semantic_contract_version = provider_semantic_contract_version
        self._writer_source_inventory_authority: tuple[str, str, str] | None = None
        self.publication_context = (
            publication_context
            or getattr(artifact_registry, "publication_context", None)
            or LocalPublicationContext()
        )
        self.settings = settings
        self.prompt_registry = PromptRegistry()
        self._review_prompt_identity = self.prompt_registry.identity("review.section_writer.system.v3")
        self.summaries = [dict(item) for item in summaries]
        self.writer = writer
        self.cancellation_checker = cancellation_checker
        self.logger = logger or logging.getLogger("auto_generate.review")
        self.receipt_ledger_target_path = self.workspace.artifact_path(
            "review_provider_receipts.jsonl"
        )
        self.receipt_ledger = ProviderRuntimeLedger(
            self.workspace.artifact_path(
                f".publication-staging/provider-receipts/review/{self.attempt_id}.jsonl"
            )
        )
        self.receipt_ledger_path = ""
        self._expected_provider_calls: dict[str, ExpectedProviderCall] = {}
        self._verified_reuse_proofs: dict[str, dict[str, str]] = {}
        self._verified_reuse_receipts: dict[str, ProviderCallReceiptV1] = {}
        self._receipt_snapshot_records: dict[str, Any] = {}
        self.expected_call_graph_hash = ""
        self.closure_epoch_id = ""
        self.provider_request_inventory: ProviderStageRequestInventoryV1 | None = None

    def run(
        self,
        *,
        outline_payload: Mapping[str, Any],
        evidence_packets: Sequence[Mapping[str, Any]],
        free_mode_context: Mapping[str, Any] | None = None,
    ) -> ReviewGenerationResult:
        self.free_mode_context = dict(free_mode_context or {})
        if self.provider_semantic_contract_version == PROVIDER_SEMANTIC_CONTRACT_V2 and self.free_mode_context:
            raise RuntimeError("source-closed Writer Free Mode context projection is not supported yet")
        self._expected_provider_calls = {}
        self._verified_reuse_proofs = {}
        self._verified_reuse_receipts = {}
        self._receipt_snapshot_records = {}
        self._source_revision_sections = {}
        self._source_revision_proofs = {}
        self.provider_request_inventory = None
        from services.writer_source_inventory import load_writer_source_inventory_v1

        source_inventory = load_writer_source_inventory_v1(self.registry)
        if self.provider_semantic_contract_version == PROVIDER_SEMANTIC_CONTRACT_V2:
            if source_inventory is None or not source_inventory.is_verified:
                raise RuntimeError("source-closed Writer requires the full verified current source inventory")
            self._writer_source_inventory_authority = (
                source_inventory.artifact_id, source_inventory.artifact_hash, source_inventory.registry_job_id,
            )
        candidate_output_scope = self._verified_writer_candidate_scope(
            outline_payload=outline_payload,
            evidence_packets=evidence_packets,
            source_inventory=source_inventory,
        )
        catalog, catalog_path = self._build_and_persist_catalog()
        self._source_revision_catalog = catalog
        packet_by_section = {
            str(packet.get("section_id") or "").strip(): dict(packet)
            for packet in evidence_packets
            if isinstance(packet, Mapping) and str(packet.get("section_id") or "").strip()
        }
        sections: list[dict[str, Any]] = []
        raw_sections = outline_payload.get("sections")
        if not isinstance(raw_sections, list):
            raise RuntimeError("Review v3 outline payload has no sections array")

        section_ids = tuple(
            str(raw_section.get("section_id") or f"section_{number}").strip()
            for number, raw_section in enumerate(raw_sections, start=1)
            if isinstance(raw_section, Mapping)
        )
        if not section_ids:
            raise RuntimeError("Review v3 outline contains no executable sections")
        writer_config_for_epoch = dict(self.settings.section("Writer_API"))
        writer_system_prompt = self._system_prompt()
        writer_max_output_tokens = self._max_output_tokens(writer_config_for_epoch)
        if "transport_retries" in writer_config_for_epoch:
            from ai_interface import _load_api_runtime_settings

            writer_transport_attempt_limit = _load_api_runtime_settings(writer_config_for_epoch)[1]
        else:
            writer_transport_attempt_limit = int(self.settings.runtime.transport_retries)
        self.expected_call_graph_hash = hash_json(
            {
                "stage_name": "stage3_review",
                "call_ids": [f"review:{section_id}" for section_id in section_ids],
                "schema_hash": hashlib.sha256(b"review_draft_v3_writer_section").hexdigest(),
            }
        )
        self.closure_epoch_id = compute_closure_epoch_id(
            job_id=self.job_id,
            stage_name="stage3_review",
            logical_attempt_identity=self.attempt_id,
            expected_call_graph_hash=self.expected_call_graph_hash,
            current_input_artifact_hashes={
                "outline": hash_json(outline_payload),
                "catalog": hash_json(catalog),
                "evidence_packets": hash_json(evidence_packets),
                "summaries": hash_json(self.summaries),
                "free_mode_context": hash_json(self.free_mode_context),
            },
            provider_config_hash=hash_json(_redact_mapping(writer_config_for_epoch)),
            schema_version="review-v3",
        )

        # Prepare the complete section graph, request bodies, and verified
        # resume state before the first Writer transport.
        prepared_sections: list[_PreparedWriterSection] = []
        for number, raw_section in enumerate(raw_sections, start=1):
            if not isinstance(raw_section, Mapping):
                continue
            section_id = str(raw_section.get("section_id") or f"section_{number}").strip()
            packet = packet_by_section.get(section_id)
            if packet is None:
                raise RuntimeError(f"Review v3 has no evidence packet for section {section_id}")
            self._require_nonempty_packet(packet, section_id)
            allowed_ref_ids = self._allowed_ref_ids(packet, catalog)
            task_scope = None
            if source_inventory is not None:
                from services.writer_task_scope import build_writer_task_scope_v1

                task_scope = build_writer_task_scope_v1(
                    packet,
                    catalog,
                    source_inventory=source_inventory,
                    candidate_output_scope=candidate_output_scope,
                    provider_semantic_contract_version=self.provider_semantic_contract_version,
                )
                if task_scope.get("usable_for_provider_admission") is not True:
                    reasons = sorted({reason for task in task_scope["tasks"] for reason in task["reason_codes"]})
                    raise RuntimeError(f"Writer source task scope for {section_id} requires review: {', '.join(reasons)}")
                if "writer_table_plan" in raw_section:
                    from services.writer_table_plan import bind_writer_table_plan_v1

                    task_scope = bind_writer_table_plan_v1(task_scope, packet, raw_section["writer_table_plan"])
            runtime = self._new_runtime(
                section_id,
                writer_config=writer_config_for_epoch,
            )
            prompt = self._prompt(raw_section, packet, catalog, allowed_ref_ids, writer_task_scope=task_scope)
            request_payload = self._writer_request_payload(
                prompt,
                system_prompt=writer_system_prompt,
                max_output_tokens=writer_max_output_tokens,
            )
            binding = self._section_binding(
                section_id=section_id,
                raw_section=raw_section,
                packet=packet,
                catalog=catalog,
                request_payload=request_payload,
                runtime=runtime,
                writer_config=writer_config_for_epoch,
            )
            self._expected_provider_calls[f"review:{section_id}"] = ExpectedProviderCall(
                call_id=f"review:{section_id}",
                job_id=self.job_id,
                attempt_id=runtime.attempt_id,
                stage_name=runtime.stage_name,
                node_id=section_id,
                closure_epoch_id=self.closure_epoch_id,
                logical_attempt_identity=self.attempt_id,
                expected_call_graph_hash=self.expected_call_graph_hash,
                prompt_id=self._review_prompt_identity.prompt_id,
                prompt_version=self._review_prompt_identity.version,
                prompt_sha256=self._review_prompt_identity.sha256,
                prompt_hash=str(binding["prompt_hash"]),
                input_hash=str(binding["prompt_payload_hash"]),
                config_hash=str(binding["writer_config_hash"]),
                schema_hash=runtime.schema_hash,
                max_attempts=max(1, self.settings.runtime.node_retry_limit + 1),
                usage_required=str(writer_config_for_epoch.get("endpoint_type") or "responses")
                not in {"internal", "fixture"},
            )
            persisted = self._load_section(
                section_id,
                raw_section=raw_section,
                packet=packet,
                catalog=catalog,
                binding=binding,
            )
            prepared_sections.append(
                _PreparedWriterSection(
                    section_number=number,
                    section_id=section_id,
                    section=dict(raw_section),
                    packet=dict(packet),
                    allowed_ref_ids=allowed_ref_ids,
                    runtime=runtime,
                    prompt=prompt,
                    request_payload=request_payload,
                    binding=binding,
                    persisted=persisted,
                    writer_task_scope=task_scope,
                )
            )

        (
            self.provider_request_inventory,
            writer_route_plan,
        ) = self._build_provider_request_inventory(
            prepared_sections,
            writer_config=writer_config_for_epoch,
            max_output_tokens=writer_max_output_tokens,
            transport_attempt_limit=writer_transport_attempt_limit,
        )
        self._preflight_provider_request_inventory(
            self.provider_request_inventory,
            writer_route_plan,
        )

        for prepared in prepared_sections:
            self._check_cancelled()
            number = prepared.section_number
            section_id = prepared.section_id
            raw_section = prepared.section
            packet = prepared.packet
            allowed_ref_ids = prepared.allowed_ref_ids
            runtime = prepared.runtime
            prompt = prepared.prompt
            request_payload = prepared.request_payload
            binding = prepared.binding
            reused_section = self._source_revision_sections.get(f"review:{section_id}")
            if reused_section is not None:
                sections.append(reused_section)
                continue
            if prepared.persisted is not None:
                sections.append(prepared.persisted)
                continue
            provider_result = self._call_writer(
                section_number=number,
                section=raw_section,
                packet=packet,
                catalog=catalog,
                allowed_ref_ids=allowed_ref_ids,
                runtime=runtime,
                prompt=prompt,
                system_prompt=writer_system_prompt,
                writer_config=writer_config_for_epoch,
                max_output_tokens=writer_max_output_tokens,
                transport_attempt_limit=writer_transport_attempt_limit,
                expected_input_hash=str(binding["prompt_payload_hash"]),
            )
            self._ensure_receipt(
                runtime,
                prompt=prompt,
                input_payload=request_payload,
                result=provider_result,
                api_config=writer_config_for_epoch,
            )
            if prepared.writer_task_scope is not None:
                from services.writer_task_scope import validate_writer_task_output_v1

                content = provider_result.get("content", provider_result)
                if isinstance(content, str):
                    content = json.loads(content)
                if not isinstance(content, Mapping):
                    raise RuntimeError("Writer section content must be an object")
                scoped_output = validate_writer_task_output_v1(
                    prepared.writer_task_scope, content,
                    provider_semantic_contract_version=self.provider_semantic_contract_version,
                )
                if scoped_output["scope_status"] != "ready":
                    self._persist_writer_review_disposition(prepared, scoped_output, provider_result)
                    raise RuntimeError(f"Writer section {section_id} requires source review; disposition saved")
            blocks = self._normalize_blocks(
                provider_result,
                section_number=number,
                allowed_ref_ids=allowed_ref_ids,
                catalog=catalog,
                writer_task_scope=prepared.writer_task_scope,
            )
            section_payload = {
                    "section_number": number,
                    "section_title": str(
                        raw_section.get("title") or raw_section.get("section_id") or f"Section {number}"
                    ).strip(),
                    "blocks": blocks,
                    "evidence_packet_id": section_id,
                    "provider_receipt_ids": [receipt.receipt_id for receipt in runtime.receipts],
                }
            if prepared.writer_task_scope is not None:
                content = provider_result.get("content", provider_result)
                if isinstance(content, str):
                    content = json.loads(content)
                section_payload["writer_task_scope"] = prepared.writer_task_scope
                section_payload["writer_task_dispositions"] = content["task_dispositions"]
                if self.provider_semantic_contract_version == PROVIDER_SEMANTIC_CONTRACT_V2:
                    section_payload["provider_semantic_contract_version"] = PROVIDER_SEMANTIC_CONTRACT_V2
            receipt = runtime.receipts[-1] if runtime.receipts else None
            receipt_snapshot = (
                self._persist_receipt_snapshot(
                    section_id=section_id,
                    binding=binding,
                    receipt=receipt,
                )
                if receipt is not None
                else None
            )
            section_record = self._persist_section(
                section_id,
                section_payload,
                raw_section=raw_section,
                packet=packet,
                catalog=catalog,
                binding=binding,
            )
            sections.append(section_payload)
            if receipt is not None:
                logical_hash = hash_json(section_payload)
                self._expected_provider_calls[f"review:{section_id}"] = replace(
                    self._expected_provider_calls[f"review:{section_id}"],
                    provider_response_hash=receipt.response_hash or "",
                    output_hash=receipt.response_hash or "",
                    normalized_output_hash=receipt.response_hash or "",
                    artifact_payload_hash=logical_hash,
                    artifact_content_hash=logical_hash,
                    registry_file_hash=section_record.content_hash,
                    artifact_path=section_record.path,
                    registered_artifact_hash=logical_hash,
                    node_output_hash=logical_hash,
                )
                if receipt_snapshot is None:
                    raise RuntimeError("Writer receipt snapshot was not persisted for a completed section")
                self._receipt_snapshot_records[f"review:{section_id}"] = receipt_snapshot
                self._persist_review_replay(
                    section_id=section_id,
                    binding=binding,
                    section_record=section_record,
                    section_hash=logical_hash,
                    receipt=receipt,
                    receipt_snapshot=receipt_snapshot,
                )

        if not sections:
            raise RuntimeError("Review v3 Writer produced no sections")
        self._register_receipt_ledger()
        self._persist_receipt_closure()
        return ReviewGenerationResult(
            sections=tuple(sections),
            citation_ref_catalog=catalog,
            citation_ref_catalog_path=str(catalog_path),
            receipt_ids=tuple(receipt.receipt_id for receipt in self.receipt_ledger.list_receipts()),
            receipt_ledger_path=self.receipt_ledger_path,
        )

    def _persist_writer_review_disposition(
        self,
        prepared: _PreparedWriterSection,
        scoped_output: Mapping[str, Any],
        provider_result: Mapping[str, Any],
    ) -> None:
        self._register_receipt_ledger()
        dependencies = []
        for identifier in ("outline-v3:outline_content_layers", "review_provider_receipts"):
            record = self.registry.get(identifier)
            if record is None or record.status != "ready":
                raise RuntimeError("Writer review disposition lost its source or receipt authority")
            dependencies.append(ArtifactDependencyRefV2.from_record(record))
        payload = {
            "artifact_type": "review_writer_review_disposition", "artifact_version": "v1",
            "job_id": self.job_id, "section_id": prepared.section_id,
            "status": "needs_review", "canonical_ready": False,
            "request_hash": hash_json(prepared.request_payload),
            "writer_task_scope": prepared.writer_task_scope,
            "validated_output": dict(scoped_output),
            "provider_result_hash": hash_json(provider_result),
            "provider_receipt_ids": [item.receipt_id for item in prepared.runtime.receipts],
        }
        digest = hash_json(payload)
        publish_json_artifact(
            self.publication_context, self.registry,
            self.workspace.artifact_path(f"review_source_review/{digest[:24]}.json"), payload,
            artifact_id=f"review:source_review:{digest[:24]}", artifact_role="review_source_review",
            artifact_type="review_writer_review_disposition", artifact_version="v1",
            producer="services.review_generation_service.ReviewGenerationService",
            status="quarantined", depends_on=dependencies,
        )

    def _build_and_persist_catalog(self) -> tuple[dict[str, Any], Path]:
        path = Path(self.workspace.artifact_path("citation_ref_catalog.json"))
        existing: Mapping[str, Any] | None = None
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, Mapping):
                    validate_document_ref_catalog(loaded)
                    existing = loaded
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                existing = None
        catalog = build_document_ref_catalog(
            self.summaries,
            project_name=self.workspace.project_name,
            job_id=self.job_id,
            existing_catalog=existing,
        )
        validate_document_ref_catalog(catalog)
        dependencies: list[ArtifactDependencyRefV2] = []
        summary_record = self.registry.get("summary_file")
        if summary_record is not None and summary_record.status == "ready":
            dependencies.append(
                ArtifactDependencyRefV2(
                    dependency_kind="local_job",
                    job_id=summary_record.job_id,
                    artifact_id=summary_record.artifact_id,
                    artifact_type=summary_record.artifact_type,
                    path=summary_record.path,
                    content_hash=summary_record.content_hash,
                )
            )
        record = publish_json_artifact(
            self.publication_context,
            self.registry,
            path,
            catalog,
            artifact_role="citation_ref_catalog",
            artifact_type="citation_ref_catalog",
            artifact_version="v1",
            producer="services.review_generation_service.ReviewGenerationService",
            artifact_id="citation_ref_catalog",
            depends_on=dependencies,
            metadata={"catalog_hash": catalog["catalog_hash"]},
        )
        return catalog, Path(record.path)

    def _section_path(self, section_id: str, binding_hash: str = "") -> Path:
        safe = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in section_id)
        suffix = f"_{binding_hash[:24]}" if binding_hash else ""
        return Path(self.workspace.artifact_path(f"review_sections/{safe}{suffix}.json"))

    def _review_replay_path(self) -> Path:
        return Path(self.workspace.artifact_path("review/review_replay.jsonl"))

    def _current_adoption_binding(self) -> dict[str, str]:
        try:
            from outline.adoption_transaction import current_adoption_record

            adoption = current_adoption_record(self.registry)
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
            adoption = None
        final = self.registry.get("outline-v3:final_outline")
        return {
            "adoption_artifact_id": adoption.artifact_id if adoption is not None else "",
            "adoption_artifact_hash": adoption.content_hash if adoption is not None else "",
            "final_outline_hash": final.content_hash if final is not None and final.status == "ready" else "",
        }

    def _verified_writer_candidate_scope(
        self,
        *,
        outline_payload: Mapping[str, Any],
        evidence_packets: Sequence[Mapping[str, Any]],
        source_inventory: Any,
    ) -> Any | None:
        """Resolve candidate scope only from the exact adopted Registry lineage.

        The paper-level Study wording exception is narrow and optional. This
        method returns no authority unless the current adopted outline, packet
        set, candidate scope and verified Stage 1 inventory all match exactly.
        """
        try:
            from outline.adoption_transaction import current_adoption_record
            from outline.candidate_output_scope import (
                CandidateOutputClaimGroupV1,
                CandidateOutputScopeV1,
                CandidateOutputSupportSlotV1,
                validate_candidate_output_scope_intrinsic_v1,
            )
            from services.writer_source_inventory import VerifiedWriterSourceInventoryV1
            from services.writer_task_scope import _verified_candidate_output_scope

            if (
                type(source_inventory) is not VerifiedWriterSourceInventoryV1
                or not source_inventory.is_verified
                or source_inventory.registry_job_id != self.job_id
            ):
                return None
            registry_job_id = getattr(self.registry, "job_id", "")
            scope_record = self.registry.get("outline-v3:candidate_output_scope")
            packet_record = self.registry.get("outline-v3:section_evidence_packets")
            final_record = self.registry.get("outline-v3:final_outline")
            adoption_record = current_adoption_record(self.registry)
            if (
                not registry_job_id
                or registry_job_id != self.job_id
                or scope_record is None
                or packet_record is None
                or final_record is None
                or adoption_record is None
                or any(
                    record.status != "ready" or record.job_id != self.job_id
                    for record in (scope_record, packet_record, final_record, adoption_record)
                )
                or (
                    scope_record.artifact_type,
                    scope_record.artifact_version,
                    scope_record.artifact_role,
                ) != ("outline_candidate_output_scope", "v1", "outline_candidate_output_scope")
                or (
                    packet_record.artifact_type,
                    packet_record.artifact_version,
                    packet_record.artifact_role,
                ) != ("section_evidence_packet_set", "v3", "outline_v3_node_output")
                or (
                    final_record.artifact_type,
                    final_record.artifact_version,
                    final_record.artifact_role,
                ) != ("final_outline", "v3", "outline_v3_node_output")
                or (
                    adoption_record.artifact_type,
                    adoption_record.artifact_version,
                ) != ("adopted_outline", "v3")
            ):
                return None

            verified = {
                record.artifact_id: self.registry.verify_ready_artifact_closure(record)
                for record in (scope_record, packet_record, final_record, adoption_record)
            }
            scope_record = verified[scope_record.artifact_id]
            packet_record = verified[packet_record.artifact_id]
            final_record = verified[final_record.artifact_id]
            adoption_record = verified[adoption_record.artifact_id]

            def read_json(record: Any) -> dict[str, Any]:
                raw = Path(record.path).read_bytes()
                if hashlib.sha256(raw).hexdigest() != record.content_hash:
                    raise ValueError("Registry artifact bytes changed")
                decoded = json.loads(raw.decode("utf-8"))
                if not isinstance(decoded, Mapping):
                    raise TypeError("Registry artifact must be an object")
                return dict(decoded)

            def read_outline_payload(record: Any, expected_type: str) -> dict[str, Any]:
                from outline.v3_artifacts import AdoptedOutline, FinalOutline, SectionEvidencePacketSet

                envelope = read_json(record)
                if (
                    set(envelope) != {
                        "artifact_type", "artifact_version", "job_id", "dependency_hashes",
                        "payload", "blocking_diagnostics", "status", "content_hash",
                    }
                    or envelope.get("artifact_type") != expected_type
                    or envelope.get("artifact_version") != "v3"
                    or envelope.get("job_id") != self.job_id
                    or envelope.get("status") != "ready"
                    or envelope.get("blocking_diagnostics") != []
                    or (
                        (expected_type != "adopted_outline" or "content_hash" in record.metadata)
                        and envelope.get("content_hash") != record.metadata.get("content_hash")
                    )
                ):
                    raise ValueError("Outline envelope does not match its Registry record")
                artifact_class = {
                    "final_outline": FinalOutline,
                    "adopted_outline": AdoptedOutline,
                    "section_evidence_packet_set": SectionEvidencePacketSet,
                }[expected_type]
                artifact = artifact_class.from_dict(envelope)
                if artifact.content_hash != envelope.get("content_hash"):
                    raise ValueError("Outline payload hash is invalid")
                payload = envelope.get("payload")
                if not isinstance(payload, Mapping):
                    raise TypeError("Outline payload is missing")
                return dict(payload)

            def has_ancestor(root: Any, target: Any) -> bool:
                pending = [root]
                visited: set[str] = set()
                while pending:
                    current = pending.pop()
                    if current.artifact_id in visited:
                        continue
                    visited.add(current.artifact_id)
                    for dependency in current.depends_on:
                        if (
                            dependency.job_id == target.job_id
                            and dependency.artifact_id == target.artifact_id
                            and dependency.content_hash == target.content_hash
                        ):
                            return True
                        if dependency.dependency_kind != "local_job":
                            continue
                        child = self.registry.get(dependency.artifact_id)
                        if (
                            child is not None
                            and child.status == "ready"
                            and child.job_id == dependency.job_id
                            and child.content_hash == dependency.content_hash
                        ):
                            pending.append(child)
                return False

            inventory_record = self.registry.get(source_inventory.artifact_id)
            if (
                inventory_record is None
                or inventory_record.status != "ready"
                or inventory_record.job_id != source_inventory.registry_job_id
                or inventory_record.content_hash != source_inventory.artifact_hash
                or not any(
                    dependency.job_id == inventory_record.job_id
                    and dependency.artifact_id == inventory_record.artifact_id
                    and dependency.content_hash == inventory_record.content_hash
                    for dependency in scope_record.depends_on
                )
                or not has_ancestor(packet_record, scope_record)
                or not has_ancestor(final_record, packet_record)
                or not has_ancestor(adoption_record, final_record)
            ):
                return None

            scope_payload = read_json(scope_record)
            validate_candidate_output_scope_intrinsic_v1(scope_payload)
            binding = scope_payload["source_inventory_binding"]
            if binding != {
                "registry_job_id": source_inventory.registry_job_id,
                "artifact_id": source_inventory.artifact_id,
                "artifact_hash": source_inventory.artifact_hash,
                "content_hash": source_inventory.content_hash,
            }:
                return None
            scope = CandidateOutputScopeV1(
                registry_job_id=binding["registry_job_id"],
                source_inventory_artifact_id=binding["artifact_id"],
                source_inventory_artifact_hash=binding["artifact_hash"],
                source_inventory_content_hash=binding["content_hash"],
                task_ids=tuple(scope_payload["task_ids"]),
                selected_relation_ids=tuple(scope_payload["selected_relation_ids"]),
                claim_groups=tuple(
                    CandidateOutputClaimGroupV1(
                        **{
                            **group,
                            "paper_keys": tuple(group["paper_keys"]),
                            "support_slot_ids": tuple(group["support_slot_ids"]),
                            "relation_ids": tuple(group["relation_ids"]),
                            "task_contract": tuple(group.get("task_contract", {}).items()),
                        }
                    )
                    for group in scope_payload["claim_groups"]
                ),
                claim_slots=tuple(
                    CandidateOutputSupportSlotV1(
                        **{
                            **slot,
                            "source_claim_ids": tuple(slot["source_claim_ids"]),
                            "evidence_ids": tuple(slot["evidence_ids"]),
                            "source_field_ids": tuple(slot["source_field_ids"]),
                            "relation_ids": tuple(slot["relation_ids"]),
                        }
                    )
                    for slot in scope_payload["claim_slots"]
                ),
            )
            if scope.to_dict() != scope_payload:
                return None
            final_payload = read_outline_payload(final_record, "final_outline")
            adoption_payload = read_outline_payload(adoption_record, "adopted_outline")
            if (
                dict(outline_payload) != final_payload
                or adoption_payload.get("final_outline_hash") != final_record.content_hash
                or adoption_payload.get("final_outline") != final_payload
            ):
                return None
            packet_payload = read_outline_payload(packet_record, "section_evidence_packet_set")
            packets = packet_payload.get("packets")
            if (
                not isinstance(packets, list)
                or [dict(packet) for packet in evidence_packets] != packets
            ):
                return None
            scope.validate(final_payload)
            return _verified_candidate_output_scope(scope)
        except (ImportError, OSError, UnicodeError, ValueError, TypeError, RuntimeError, KeyError, AttributeError):
            return None

    def _section_binding(
        self,
        *,
        section_id: str,
        raw_section: Mapping[str, Any],
        packet: Mapping[str, Any],
        catalog: Mapping[str, Any],
        request_payload: Mapping[str, Any],
        runtime: ProviderRuntime,
        writer_config: Mapping[str, Any],
    ) -> dict[str, Any]:
        profile = self._provider_context_profile(writer_config)
        adoption = self._current_adoption_binding()
        free_mode = self.free_mode_context
        binding = {
            "binding_version": "review-section-binding-v1",
            "stage_name": runtime.stage_name,
            "section_id": section_id,
            "free_mode_input_artifact_id": str(free_mode.get("free_mode_input_artifact_id") or ""),
            "free_mode_input_artifact_hash": str(free_mode.get("free_mode_input_artifact_hash") or ""),
            "free_mode_context_hash": str(free_mode.get("free_mode_context_hash") or ""),
            "adoption_artifact_id": adoption["adoption_artifact_id"],
            "adoption_artifact_hash": adoption["adoption_artifact_hash"],
            "final_outline_hash": adoption["final_outline_hash"],
            "outline_section_hash": self._input_hash(raw_section),
            "evidence_packet_hash": self._input_hash(packet),
            "source_summary_hashes": sorted(
                str(item).strip()
                for item in (packet.get("source_summary_hashes") or ())
                if str(item).strip()
            ),
            "citation_catalog_hash": str(catalog.get("catalog_hash") or ""),
            "writer_provider": str(writer_config.get("provider_family") or "configured"),
            "writer_model": str(writer_config.get("model") or ""),
            "writer_endpoint": str(writer_config.get("endpoint_type") or "responses"),
            "writer_config_hash": hash_json(_redact_mapping(dict(writer_config))),
            "system_prompt_hash": hash_text(str(request_payload.get("system") or "")),
            "prompt_id": self._review_prompt_identity.prompt_id,
            "prompt_version": self._review_prompt_identity.version,
            "prompt_sha256": self._review_prompt_identity.sha256,
            "prompt_template_hash": self._review_prompt_identity.sha256,
            "prompt_hash": hash_text(str(request_payload.get("user") or "")),
            "prompt_payload_hash": hash_json(request_payload),
            "output_schema_hash": runtime.schema_hash,
            "context_profile_hash": hash_json(
                {
                    "provider": profile.provider,
                    "model": profile.model,
                    "endpoint_type": profile.endpoint_type,
                    "model_context_limit": profile.model_context_limit,
                    "verified_context_limit": profile.verified_context_limit,
                    "input_budget": profile.input_budget,
                    "max_output_tokens": profile.max_output_tokens,
                    "reasoning_reserve": profile.reasoning_reserve,
                    "safety_margin": profile.safety_margin,
                    "tokenizer_strategy": profile.tokenizer_strategy,
                }
            ),
            "application_schema_version": str(self.settings.config_schema),
        }
        if self.provider_semantic_contract_version == PROVIDER_SEMANTIC_CONTRACT_V2:
            binding["provider_semantic_contract_version"] = PROVIDER_SEMANTIC_CONTRACT_V2
            binding["source_inventory_authority"] = ArtifactDependencyRefV2.from_record(
                self._current_writer_inventory_record_v2(),
            ).to_dict()
        return binding

    def _current_writer_inventory_record_v2(self) -> ArtifactRecord:
        authority = self._writer_source_inventory_authority
        if authority is None:
            raise RuntimeError("source-closed Writer inventory authority has not been verified")
        artifact_id, expected_hash, owner = authority
        record = self.registry.get(artifact_id)
        if (record is None or record.status != "ready" or record.content_hash != expected_hash
                or record.job_id != owner or owner != self.job_id):
            raise RuntimeError("source-closed Writer current inventory authority changed")
        self.registry.verify_ready_artifact_closure(record)
        return record

    def _load_review_replay(self, *, section_id: str, binding: Mapping[str, Any]) -> Mapping[str, Any] | None:
        current = self.registry.get("review_replay")
        if current is None:
            return None
        if (
            current.status != "ready"
            or current.job_id != self.job_id
            or current.artifact_role != "review_replay"
            or current.artifact_type != "review_replay_ledger"
            or current.artifact_version != "v1"
        ):
            raise RuntimeError("Review v3 saved replay Registry authority is not ready and local")
        path = Path(current.path)
        try:
            if not path.is_file() or current.content_hash != file_sha256(path):
                raise RuntimeError("Review v3 saved replay Registry hash does not match its bytes")
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise RuntimeError("Review v3 saved replay Registry bytes are unreadable") from exc
        binding_hash = hash_json(binding)
        matches: list[Mapping[str, Any]] = []
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Review v3 saved replay line {line_number} is invalid JSON"
                ) from exc
            if not isinstance(payload, Mapping):
                raise RuntimeError(f"Review v3 saved replay line {line_number} is not an object")
            if (
                str(payload.get("section_id") or "") == section_id
                and str(payload.get("binding_hash") or "") == binding_hash
            ):
                matches.append(dict(payload))
        if len(matches) > 1:
            raise RuntimeError(
                f"Review v3 saved replay has ambiguous same-binding records for {section_id}"
            )
        return matches[0] if matches else None

    def _receipt_snapshot_path(
        self,
        *,
        section_id: str,
        binding_hash: str,
        receipt_hash: str,
    ) -> Path:
        safe = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in section_id)
        return Path(
            self.workspace.artifact_path(
                f"review/receipt_snapshots/{safe}_{binding_hash[:24]}_{receipt_hash[:24]}.json"
            )
        )

    @staticmethod
    def _receipt_snapshot_artifact_id(
        *,
        section_id: str,
        binding_hash: str,
        receipt_hash: str,
    ) -> str:
        safe = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in section_id)
        return f"review:receipt-snapshot:{safe}:{binding_hash[:24]}:{receipt_hash[:24]}"

    def _persist_receipt_snapshot(
        self,
        *,
        section_id: str,
        binding: Mapping[str, Any],
        receipt: ProviderCallReceiptV1,
    ) -> Any:
        binding_hash = hash_json(binding)
        receipt_payload = receipt.to_dict()
        receipt_hash = hash_json(receipt_payload)
        snapshot_payload = {
            "artifact_type": "review_provider_receipt_snapshot",
            "artifact_version": "v1",
            "status": "partial",
            "job_id": self.job_id,
            "stage_name": "stage3_review",
            "section_id": section_id,
            "binding_hash": binding_hash,
            "closure_epoch_id": receipt.closure_epoch_id,
            "logical_attempt_identity": receipt.logical_attempt_identity,
            "call_id": receipt.call_id,
            "expected_call_graph_hash": self.expected_call_graph_hash,
            "receipt_id": receipt.receipt_id,
            "receipt_hash": receipt_hash,
            "receipt": receipt_payload,
        }
        return publish_json_artifact(
            self.publication_context,
            self.registry,
            self._receipt_snapshot_path(
                section_id=section_id,
                binding_hash=binding_hash,
                receipt_hash=receipt_hash,
            ),
            snapshot_payload,
            artifact_role="provider_receipt_snapshot",
            artifact_type="review_provider_receipt_snapshot",
            artifact_version="v1",
            producer="services.review_generation_service.ReviewGenerationService",
            artifact_id=self._receipt_snapshot_artifact_id(
                section_id=section_id,
                binding_hash=binding_hash,
                receipt_hash=receipt_hash,
            ),
            metadata={
                "status": "partial",
                "job_id": self.job_id,
                "stage_name": "stage3_review",
                "section_id": section_id,
                "binding_hash": binding_hash,
                "receipt_hash": receipt_hash,
            },
        )

    def _persist_review_replay(
        self,
        *,
        section_id: str,
        binding: Mapping[str, Any],
        section_record: Any,
        section_hash: str,
        receipt: ProviderCallReceiptV1,
        receipt_snapshot: Any,
    ) -> None:
        path = self._review_replay_path()
        binding_hash = hash_json(binding)
        existing = self._load_review_replay(section_id=section_id, binding=binding)
        receipt_hash = hash_json(receipt.to_dict())
        payload = {
            "replay_version": "review-section-replay-v1",
            "job_id": self.job_id,
            "stage_name": "stage3_review",
            "closure_epoch_id": self.closure_epoch_id,
            "logical_attempt_identity": receipt.logical_attempt_identity,
            "expected_call_graph_hash": self.expected_call_graph_hash,
            "section_id": section_id,
            "binding_hash": binding_hash,
            "artifact_id": section_record.artifact_id,
            "artifact_path": section_record.path,
            "artifact_content_hash": section_hash,
            "registry_file_hash": section_record.content_hash,
            "receipt_id": receipt.receipt_id,
            "receipt_hash": receipt_hash,
            "normalized_output_hash": receipt.response_hash or "",
            "receipt_snapshot_artifact_id": receipt_snapshot.artifact_id,
            "receipt_snapshot_path": receipt_snapshot.path,
            "receipt_snapshot_content_hash": receipt_snapshot.content_hash,
        }
        if existing == payload:
            return
        current = self.registry.get("review_replay")
        existing_bytes = b""
        if current is not None and current.status == "ready":
            try:
                existing_bytes = Path(current.path).read_bytes()
            except OSError:
                existing_bytes = b""
        elif path.is_file():
            try:
                existing_bytes = path.read_bytes()
            except OSError:
                existing_bytes = b""
        dependencies = list(current.depends_on) if current is not None else []
        if all(
            dependency.artifact_id != section_record.artifact_id
            for dependency in dependencies
        ):
            dependencies.append(ArtifactDependencyRefV2.from_record(section_record))
        if all(
            dependency.artifact_id != receipt_snapshot.artifact_id
            for dependency in dependencies
        ):
            dependencies.append(ArtifactDependencyRefV2.from_record(receipt_snapshot))
        line = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        record = publish_bytes_artifact(
            self.publication_context,
            self.registry,
            path,
            existing_bytes + line,
            artifact_role="review_replay",
            artifact_type="review_replay_ledger",
            artifact_version="v1",
            producer="services.review_generation_service.ReviewGenerationService",
            artifact_id="review_replay",
            depends_on=dependencies,
            metadata={"binding_version": "review-section-binding-v1"},
        )
        del record

    @staticmethod
    def _replay_has_dependency(replay_record: Any, dependency_record: Any) -> bool:
        if replay_record is None or dependency_record is None:
            return False
        return any(
            dependency.job_id == dependency_record.job_id
            and dependency.artifact_id == dependency_record.artifact_id
            and dependency.artifact_type == dependency_record.artifact_type
            and str(Path(dependency.path).resolve()) == str(Path(dependency_record.path).resolve())
            and dependency.content_hash == dependency_record.content_hash
            for dependency in replay_record.depends_on
        )

    def _load_receipt_snapshot(
        self,
        *,
        section_id: str,
        binding: Mapping[str, Any],
        replay: Mapping[str, Any],
        expected: ExpectedProviderCall,
        replay_record: Any,
    ) -> tuple[ProviderCallReceiptV1, Any, str]:
        snapshot_id = str(replay.get("receipt_snapshot_artifact_id") or "")
        snapshot_path = str(replay.get("receipt_snapshot_path") or "")
        snapshot_hash = str(replay.get("receipt_snapshot_content_hash") or "")
        replay_receipt_hash = str(replay.get("receipt_hash") or "")
        if not all((snapshot_id, snapshot_path, snapshot_hash, replay_receipt_hash)):
            raise RuntimeError(
                f"Review v3 same-binding replay lacks receipt snapshot authority for {section_id}"
            )
        snapshot_record = self.registry.get(snapshot_id)
        if (
            snapshot_record is None
            or snapshot_record.status != "ready"
            or snapshot_record.job_id != self.job_id
            or snapshot_record.artifact_role != "provider_receipt_snapshot"
            or snapshot_record.artifact_type != "review_provider_receipt_snapshot"
            or snapshot_record.artifact_version != "v1"
            or snapshot_record.path != snapshot_path
            or snapshot_record.content_hash != snapshot_hash
            or not self._replay_has_dependency(replay_record, snapshot_record)
        ):
            raise RuntimeError(
                f"Review v3 same-binding receipt snapshot Registry reference is missing, foreign, or changed for {section_id}"
            )
        snapshot_file = Path(snapshot_record.path)
        try:
            if file_sha256(snapshot_file) != snapshot_hash:
                raise RuntimeError(
                    f"Review v3 same-binding receipt snapshot bytes changed for {section_id}"
                )
            snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Review v3 same-binding receipt snapshot is unreadable for {section_id}"
            ) from exc
        binding_hash = hash_json(binding)
        replay_epoch = str(replay.get("closure_epoch_id") or "")
        replay_identity = str(replay.get("logical_attempt_identity") or "")
        replay_graph_hash = str(replay.get("expected_call_graph_hash") or "")
        receipt_payload = snapshot.get("receipt") if isinstance(snapshot, Mapping) else None
        if not isinstance(snapshot, Mapping) or not isinstance(receipt_payload, Mapping):
            raise RuntimeError(
                f"Review v3 same-binding receipt snapshot payload is invalid for {section_id}"
            )
        receipt_hash = hash_json(receipt_payload)
        if (
            snapshot.get("artifact_type") != "review_provider_receipt_snapshot"
            or snapshot.get("artifact_version") != "v1"
            or snapshot.get("status") != "partial"
            or str(snapshot.get("job_id") or "") != self.job_id
            or str(snapshot.get("stage_name") or "") != "stage3_review"
            or str(snapshot.get("section_id") or "") != section_id
            or str(snapshot.get("binding_hash") or "") != binding_hash
            or str(snapshot.get("closure_epoch_id") or "") != replay_epoch
            or str(snapshot.get("logical_attempt_identity") or "") != replay_identity
            or not replay_graph_hash
            or str(snapshot.get("expected_call_graph_hash") or "") != replay_graph_hash
            or str(snapshot.get("call_id") or "") != expected.call_id
            or str(snapshot.get("receipt_id") or "") != str(replay.get("receipt_id") or "")
            or str(snapshot.get("receipt_hash") or "") != receipt_hash
            or replay_receipt_hash != receipt_hash
        ):
            raise RuntimeError(
                f"Review v3 same-binding receipt snapshot hash or binding mismatch for {section_id}"
            )
        try:
            receipt = ProviderCallReceiptV1.from_dict(receipt_payload)
        except (ProviderRuntimeContractError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Review v3 same-binding provider receipt is invalid for {section_id}"
            ) from exc
        writer_config = self.settings.section("Writer_API")
        expected_provider = str(
            writer_config.get("provider_family") or writer_config.get("provider") or "generic"
        )
        expected_endpoint_type = str(writer_config.get("endpoint_type") or "responses")
        if receipt.test_only and not self._fixture_context_enabled():
            raise RuntimeError(
                f"Review v3 same-binding test-only receipt cannot authorize production reuse for {section_id}"
            )
        if (
            receipt.status != "success"
            or receipt.incomplete_reason
            or receipt.finish_reason == "length"
            or receipt.attempts > expected.max_attempts
            or receipt.job_id != self.job_id
            or receipt.attempt_id != expected.attempt_id
            or receipt.stage_name != expected.stage_name
            or receipt.node_id != expected.node_id
            or receipt.call_id != expected.call_id
            or receipt.closure_epoch_id != replay_epoch
            or receipt.logical_attempt_identity != replay_identity
            or not replay_identity
            or receipt.prompt_hash != expected.prompt_hash
            or receipt.prompt_id != expected.prompt_id
            or receipt.prompt_version != expected.prompt_version
            or receipt.prompt_sha256 != expected.prompt_sha256
            or receipt.input_hash != expected.input_hash
            or receipt.config_hash != expected.config_hash
            or receipt.schema_hash != expected.schema_hash
            or receipt.route != "Writer_API"
            or receipt.provider != expected_provider
            or receipt.model != str(writer_config.get("model") or "")
            or receipt.endpoint != str(writer_config.get("api_base") or "")
            or receipt.endpoint_type != expected_endpoint_type
            or receipt.response_hash != str(replay.get("normalized_output_hash") or "")
            or (expected.usage_required and receipt.usage_status not in {"reported", "provider_not_supported"})
        ):
            raise RuntimeError(
                f"Review v3 same-binding provider receipt identity or request mismatch for {section_id}"
            )
        return receipt, snapshot_record, receipt_hash

    def _load_section(
        self,
        section_id: str,
        *,
        raw_section: Mapping[str, Any],
        packet: Mapping[str, Any],
        catalog: Mapping[str, Any],
        binding: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        expected_binding_hash = hash_json(binding)
        current_section = self.registry.get(f"review-section:{section_id}")
        current_binding_hash = str((current_section.metadata if current_section else {}).get("binding_hash") or "")
        if current_binding_hash and current_binding_hash != expected_binding_hash:
            return None
        replay = self._load_review_replay(section_id=section_id, binding=binding)
        if replay is None:
            if current_binding_hash == expected_binding_hash:
                raise RuntimeError(
                    f"Review v3 same-binding saved section lacks replay authority for {section_id}"
                )
            return None
        replay_epoch_id = str(replay.get("closure_epoch_id") or "")
        if (
            replay.get("replay_version") != "review-section-replay-v1"
            or str(replay.get("job_id") or "") != self.job_id
            or str(replay.get("stage_name") or "") != "stage3_review"
            or str(replay.get("section_id") or "") != section_id
            or str(replay.get("binding_hash") or "") != expected_binding_hash
            or not replay_epoch_id
            or not str(replay.get("expected_call_graph_hash") or "")
        ):
            raise RuntimeError(
                f"Review v3 same-binding replay identity is invalid for {section_id}"
            )
        record = self.registry.get(str(replay.get("artifact_id") or ""))
        expected_artifact_id = f"review-section:{section_id}:{expected_binding_hash[:24]}"
        if (
            record is None
            or record.status != "ready"
            or record.job_id != self.job_id
            or record.artifact_role != "review_section"
            or record.artifact_type != "review_section"
            or record.artifact_version != "v3"
            or record.artifact_id != expected_artifact_id
            or str((record.metadata or {}).get("binding_hash") or "") != expected_binding_hash
        ):
            raise RuntimeError(
                f"Review v3 same-binding section Registry reference is missing, foreign, or changed for {section_id}"
            )
        if current_section is not None and current_binding_hash == expected_binding_hash:
            selected_id = str((current_section.metadata or {}).get("current_version_artifact_id") or "")
            if selected_id and selected_id != record.artifact_id:
                raise RuntimeError(
                    f"Review v3 same-binding current section pointer differs from replay for {section_id}"
                )
        path = Path(record.path)
        try:
            if not path.is_file() or record.content_hash != file_sha256(path):
                raise RuntimeError(
                    f"Review v3 same-binding section Registry hash does not match its bytes for {section_id}"
                )
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Review v3 same-binding section artifact is unreadable for {section_id}"
            ) from exc
        payload = envelope.get("section") if isinstance(envelope, Mapping) else None
        if (
            not isinstance(envelope, Mapping)
            or envelope.get("status") != "ready"
            or envelope.get("binding_hash") != expected_binding_hash
            or envelope.get("binding") != dict(binding)
            or not isinstance(payload, Mapping)
        ):
            raise RuntimeError(
                f"Review v3 same-binding section envelope is invalid for {section_id}"
            )
        section_hash = hash_json(payload)
        if (
            envelope.get("content_hash") != section_hash
            or str(replay.get("artifact_id") or "") != record.artifact_id
            or str(replay.get("artifact_path") or "") != record.path
            or str(replay.get("artifact_content_hash") or "") != section_hash
            or str(replay.get("registry_file_hash") or "") != record.content_hash
        ):
            raise RuntimeError(
                f"Review v3 same-binding replay does not match the immutable section for {section_id}"
            )
        reuse_record = self.registry.get("review_replay")
        if reuse_record is None or not self._replay_has_dependency(reuse_record, record):
            raise RuntimeError(
                f"Review v3 replay Registry dependencies omit the immutable section for {section_id}"
            )
        expected = self._expected_provider_calls.get(f"review:{section_id}")
        if expected is None:
            raise RuntimeError(
                f"Review v3 same-binding replay has no expected call contract for {section_id}"
            )
        receipt, snapshot_record, receipt_hash = self._load_receipt_snapshot(
            section_id=section_id,
            binding=binding,
            replay=replay,
            expected=expected,
            replay_record=reuse_record,
        )
        if receipt.receipt_id not in (payload.get("provider_receipt_ids") or []):
            raise RuntimeError(
                f"Review v3 same-binding section does not cite its exact provider receipt for {section_id}"
            )
        self.receipt_ledger.append(receipt)
        self._expected_provider_calls[expected.call_id] = replace(
            expected,
            closure_epoch_id=receipt.closure_epoch_id,
            logical_attempt_identity=receipt.logical_attempt_identity,
            expected_call_graph_hash=str(replay["expected_call_graph_hash"]),
            provider_response_hash=receipt.response_hash or "",
            output_hash=receipt.response_hash or "",
            normalized_output_hash=receipt.response_hash or "",
            artifact_payload_hash=section_hash,
            artifact_content_hash=section_hash,
            registry_file_hash=record.content_hash,
            artifact_path=str(path),
            registered_artifact_hash=section_hash,
            replay_output_hash=receipt.response_hash or "",
            node_output_hash=section_hash,
            verified_reuse=True,
            reuse_evidence_artifact_id=reuse_record.artifact_id,
            reuse_evidence_artifact_hash=reuse_record.content_hash,
            reuse_evidence_record_hash=hash_json(replay),
        )
        self._receipt_snapshot_records[expected.call_id] = snapshot_record
        self._verified_reuse_receipts[expected.call_id] = receipt
        self._verified_reuse_proofs[expected.call_id] = {
            "receipt_hash": receipt_hash,
            "output_hash": receipt.response_hash or "",
            "reuse_evidence_artifact_id": reuse_record.artifact_id,
            "reuse_evidence_artifact_hash": reuse_record.content_hash,
            "reuse_evidence_record_hash": hash_json(replay),
            "receipt_snapshot_artifact_id": snapshot_record.artifact_id,
            "receipt_snapshot_path": snapshot_record.path,
            "receipt_snapshot_content_hash": snapshot_record.content_hash,
            "authority_hash": hash_json(
                {
                    "authority_version": "review-section-replay-authority-v2",
                    "call_id": expected.call_id,
                    "binding_hash": expected_binding_hash,
                    "receipt_id": receipt.receipt_id,
                    "receipt_hash": receipt_hash,
                    "receipt_snapshot_artifact_id": snapshot_record.artifact_id,
                    "receipt_snapshot_path": snapshot_record.path,
                    "receipt_snapshot_content_hash": snapshot_record.content_hash,
                    "reuse_evidence_artifact_id": reuse_record.artifact_id,
                    "reuse_evidence_artifact_hash": reuse_record.content_hash,
                    "reuse_evidence_record_hash": hash_json(replay),
                    "artifact_id": record.artifact_id,
                    "artifact_content_hash": section_hash,
                    "registry_file_hash": record.content_hash,
                }
            ),
        }
        return dict(payload)

    @staticmethod
    def _input_hash(value: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _persist_section(
        self,
        section_id: str,
        section: Mapping[str, Any],
        *,
        raw_section: Mapping[str, Any],
        packet: Mapping[str, Any],
        catalog: Mapping[str, Any],
        binding: Mapping[str, Any],
    ) -> Any:
        section_hash = hash_json(section)
        binding_hash = hash_json(binding)
        path = self._section_path(section_id, binding_hash)
        section_payload = {
            "artifact_type": "review_section",
            "artifact_version": "v3",
            "status": "ready",
            "job_id": self.job_id,
            "section_id": section_id,
            "binding_hash": binding_hash,
            "binding": dict(binding),
            "content_hash": section_hash,
            "section": dict(section),
        }
        dependencies: list[ArtifactDependencyRefV2] = []
        if self.provider_semantic_contract_version == PROVIDER_SEMANTIC_CONTRACT_V2:
            dependencies.append(ArtifactDependencyRefV2.from_record(self._current_writer_inventory_record_v2()))
        for artifact_id in (
            "outline-v3:section_evidence_packets",
            "citation_ref_catalog",
            "outline-v3:final_outline",
            "outline-v3:adoption:current",
        ):
            record = self.registry.get(artifact_id)
            if record is not None and record.status == "ready":
                dependencies.append(ArtifactDependencyRefV2.from_record(record))
        immutable_record = publish_json_artifact(
            self.publication_context,
            self.registry,
            path,
            section_payload,
            artifact_role="review_section",
            artifact_type="review_section",
            artifact_version="v3",
            producer="services.review_generation_service.ReviewGenerationService",
            artifact_id=f"review-section:{section_id}:{binding_hash[:24]}",
            depends_on=dependencies,
            metadata={
                "immutable": True,
                "section_id": section_id,
                "binding_hash": binding_hash,
                "section_content_hash": section_hash,
                "versioned_artifact_id": f"review-section:{section_id}:{binding_hash[:24]}",
            },
        )
        publish_json_artifact(
            self.publication_context,
            self.registry,
            path,
            section_payload,
            artifact_role="review_section",
            artifact_type="review_section",
            artifact_version="v3",
            producer="services.review_generation_service.ReviewGenerationService",
            artifact_id=f"review-section:{section_id}",
            depends_on=dependencies + [ArtifactDependencyRefV2.from_record(immutable_record)],
            metadata={
                "pointer_role": "current",
                "section_id": section_id,
                "binding_hash": binding_hash,
                "current_version_artifact_id": immutable_record.artifact_id,
                "section_content_hash": section_hash,
            },
        )
        return immutable_record

    def _reuse_source_revision_section(self, prepared: _PreparedWriterSection) -> None:
        context = self.source_revision_replay_context
        scope = prepared.writer_task_scope
        if context is None or scope is None or prepared.persisted is not None:
            return
        from runtime.source_revision_replay import build_writer_source_revision_scope_v1
        from services.writer_task_scope import validate_writer_task_output_v1

        call_id = f"review:{prepared.section_id}"
        expected = self._expected_provider_calls[call_id]
        controller = provider_budget_controller_from_environment()
        if controller is None:
            raise RuntimeError("SourceRevision Writer reuse requires the original finite child budget")
        controller.assert_response_reuse_allowed(
            job_id=self.job_id, stage_name=expected.stage_name, call_id=call_id,
        )
        lookup = context.lookup_response(
            expected_call=expected, request_payload=prepared.request_payload,
            current_scope=build_writer_source_revision_scope_v1(scope, prepared.binding),
            parent_scope_record=None,
            route_identity={"route": "Writer_API", **{key: getattr(expected, key) for key in (
                "provider", "model", "endpoint", "endpoint_type", "config_hash", "schema_hash",
            )}},
        )
        match = lookup.match
        if match is None:
            return
        content = match.response
        if not isinstance(content, Mapping):
            raise RuntimeError("SourceRevision Writer response is not an object")
        checked = validate_writer_task_output_v1(
            scope, content, provider_semantic_contract_version=self.provider_semantic_contract_version,
        )
        if checked["scope_status"] != "ready":
            raise RuntimeError("SourceRevision Writer response requires source review")
        result = {"status": "success", "content": content}
        blocks = self._normalize_blocks(
            result, section_number=prepared.section_number,
            allowed_ref_ids=prepared.allowed_ref_ids, catalog=self._source_revision_catalog,
            writer_task_scope=scope,
        )
        section = {
            "section_number": prepared.section_number,
            "section_title": str(prepared.section.get("title") or prepared.section_id).strip(),
            "blocks": blocks, "evidence_packet_id": prepared.section_id,
            "provider_receipt_ids": [], "writer_task_scope": scope,
            "writer_task_dispositions": content["task_dispositions"],
            "provider_semantic_contract_version": PROVIDER_SEMANTIC_CONTRACT_V2,
        }
        record = self._persist_section(
            prepared.section_id, section, raw_section=prepared.section,
            packet=prepared.packet, catalog=self._source_revision_catalog, binding=prepared.binding,
        )
        logical_hash = hash_json(section)
        expected = replace(
            expected, provider_response_hash=match.receipt.response_hash,
            output_hash=match.receipt.response_hash, normalized_output_hash=hash_json(content),
            artifact_payload_hash=logical_hash, artifact_content_hash=logical_hash,
            registry_file_hash=record.content_hash, artifact_path=record.path,
            registered_artifact_hash=logical_hash, node_output_hash=logical_hash,
        )
        proof = context.publish_child_reuse_proof(
            match=match, child_output_record=record, child_plan_record=context.plan_record,
            child_expected_call=expected, publication_context=self.publication_context,
            path=self.workspace.artifact_path(f"source_revision_writer/{hash_json(call_id)}.json"),
        )
        self._expected_provider_calls[call_id] = replace(
            expected, verified_reuse=True, reuse_evidence_artifact_id=proof.artifact_id,
            reuse_evidence_artifact_hash=proof.content_hash,
            reuse_evidence_record_hash=hash_json(asdict(proof)),
        )
        self._source_revision_sections[call_id] = section
        self._source_revision_proofs[call_id] = (proof, record, match)

    def _persist_receipt_closure(self) -> None:
        for call_id, (proof, output, match) in self._source_revision_proofs.items():
            context = self.source_revision_replay_context
            if context is None:
                raise RuntimeError("SourceRevision Writer closure lost its executing context")
            context.verify_child_reuse_proof(
                proof, child_expected_call=self._expected_provider_calls[call_id],
                child_output_record=output, current_scope=match.scope,
                route_identity=match.route_identity,
                external_registry_resolver=context.external_registry_resolver,
            )
        reuse_record = self.registry.get("review_replay")
        if reuse_record is not None and reuse_record.status == "ready":
            for call_id, proof in self._verified_reuse_proofs.items():
                expected = self._expected_provider_calls.get(call_id)
                if expected is not None and expected.verified_reuse:
                    self._expected_provider_calls[call_id] = replace(
                        expected,
                        reuse_evidence_artifact_id=reuse_record.artifact_id,
                        reuse_evidence_artifact_hash=reuse_record.content_hash,
                        reuse_evidence_record_hash=proof["reuse_evidence_record_hash"],
                    )
        all_receipts = list(self.receipt_ledger.list_receipts())
        scoped_receipts = [
            receipt
            for receipt in all_receipts
            if receipt.job_id == self.job_id and receipt.stage_name == "stage3_review"
        ]
        out_of_scope_receipts = [receipt for receipt in all_receipts if receipt not in scoped_receipts]
        reuse_call_ids = {
            call_id for call_id, expected in self._expected_provider_calls.items()
            if expected.verified_reuse
        }
        closure = ProviderReceiptClosure.evaluate(
            self._expected_provider_calls.values(),
            [receipt for receipt in scoped_receipts if receipt.call_id not in reuse_call_ids],
            out_of_scope=out_of_scope_receipts,
        )
        path = Path(self.workspace.artifact_path("review_provider_receipt_closure.json"))
        test_only = any(receipt.test_only for receipt in scoped_receipts)
        closure_payload = {
            **closure.to_dict(),
            "job_id": self.job_id,
            "stage_name": "stage3_review",
            "attempt_id": self.attempt_id,
            "logical_attempt_identity": self.attempt_id,
            "closure_epoch_id": self.closure_epoch_id,
            "expected_call_graph_hash": self.expected_call_graph_hash,
            "test_only": test_only,
            "authority_scope": "offline_fixture" if test_only else "provider_transport",
            "expected_calls": [
                asdict(expected)
                for expected in self._expected_provider_calls.values()
            ],
        }
        closure_document = {
            "artifact_type": "provider_receipt_closure",
            "artifact_version": "v1",
            "job_id": self.job_id,
            "stage_name": "stage3_review",
            "attempt_id": self.attempt_id,
            "closure_epoch_id": self.closure_epoch_id,
            "expected_call_graph_hash": self.expected_call_graph_hash,
            "test_only": test_only,
            "payload": closure_payload,
        }
        dependencies: list[ArtifactDependencyRefV2] = []
        dependency_ids: set[str] = set()

        def add_dependency(record: Any) -> None:
            if record is None or record.status != "ready" or record.artifact_id in dependency_ids:
                return
            dependency_ids.add(record.artifact_id)
            dependencies.append(ArtifactDependencyRefV2.from_record(record))

        ledger = self.registry.get("review_provider_receipts")
        add_dependency(ledger)
        add_dependency(self.registry.get("review_replay"))
        for proof, _output, _match in self._source_revision_proofs.values():
            add_dependency(proof)
        for artifact_id in (
            "citation_ref_catalog",
            "outline-v3:final_outline",
            "outline-v3:section_evidence_packets",
            "outline-v3:adoption:current",
        ):
            add_dependency(self.registry.get(artifact_id))
        for snapshot_record in self._receipt_snapshot_records.values():
            add_dependency(snapshot_record)
        for expected in self._expected_provider_calls.values():
            expected_path = str(expected.artifact_path or "").strip()
            if not expected_path:
                continue
            expected_resolved = Path(expected_path).resolve()
            add_dependency(
                next(
                    (
                        record
                        for record in self.registry.list_records()
                        if record.status == "ready"
                        and record.artifact_type == "review_section"
                        and record.metadata.get("immutable") is True
                        and Path(record.path).resolve() == expected_resolved
                    ),
                    None,
                )
            )
        publish_json_artifact(
            self.publication_context,
            self.registry,
            path,
            closure_document,
            artifact_role="provider_receipt_closure",
            artifact_type="provider_receipt_closure",
            artifact_version="v1",
            producer="services.review_generation_service.ReviewGenerationService",
            artifact_id="review:provider_receipt_closure",
            depends_on=dependencies,
            metadata={
                "job_id": self.job_id,
                "stage_name": "stage3_review",
                "attempt_id": self.attempt_id,
                "closure_epoch_id": self.closure_epoch_id,
                "expected_call_graph_hash": self.expected_call_graph_hash,
                "closure_hash": closure.closure_hash,
                "complete": closure.complete,
                "test_only": test_only,
            },
        )

    def _call_writer(
        self,
        *,
        section_number: int,
        section: Mapping[str, Any],
        packet: Mapping[str, Any],
        catalog: Mapping[str, Any],
        allowed_ref_ids: Sequence[str],
        runtime: ProviderRuntime,
        prompt: str,
        system_prompt: str,
        writer_config: Mapping[str, Any],
        max_output_tokens: int,
        transport_attempt_limit: int,
        expected_input_hash: str,
    ) -> Mapping[str, Any]:
        frozen_config = dict(writer_config)
        request_payload = self._writer_request_payload(
            prompt,
            system_prompt=system_prompt,
            max_output_tokens=max_output_tokens,
        )
        if hash_json(request_payload) != expected_input_hash:
            raise RuntimeError("Review v3 Writer request changed after preflight")
        if self.writer is not None:
            if not self._fixture_context_enabled():
                raise RuntimeError("Opaque Writer callbacks require explicit in-process test dependencies; production transport is service-owned")
            runtime.test_only = True
            profile = self._provider_context_profile(frozen_config)
            estimate = profile.estimate_request(request_payload)
            admission = runtime.admit(
                estimated_tokens=max(1, int(estimate["estimated_input_tokens"])),
                requested_output_tokens=max_output_tokens,
                requested_retry_attempts=0,
            )
            try:
                value = self.writer(
                    prompt_text=prompt,
                    writer_api_config=frozen_config,
                    section_number=section_number,
                    section=dict(section),
                    evidence_packet=dict(packet),
                    citation_ref_catalog=dict(catalog),
                )
                attempts = value.get("attempts", 1) if isinstance(value, Mapping) else None
                if not isinstance(value, Mapping) or isinstance(attempts, bool) or not isinstance(attempts, int) or attempts != 1:
                    raise RuntimeError("Offline Writer callback must return one fixture result without transport retries")
            except Exception:
                runtime.complete(
                    admission=admission, prompt=prompt, input_payload=request_payload, api_config=frozen_config,
                    result={"status": "failed", "error_kind": "invalid_response", "attempts": 1},
                    metadata={"execution_mode": "offline_fixture_callback", "transport_started": False},
                )
                raise
            runtime.complete(
                admission=admission, prompt=prompt, input_payload=request_payload, api_config=frozen_config,
                result=value, metadata={"execution_mode": "offline_fixture_callback", "transport_started": False},
            )
            return dict(value)

        from ai_interface import _call_ai_api_detailed

        if not str(frozen_config.get("api_key") or "").strip() or not str(frozen_config.get("model") or "").strip():
            raise RuntimeError("Writer_API is not configured for Review v3")
        result = _call_ai_api_detailed(
            prompt,
            cast(APIConfig, frozen_config),
            system_prompt,
            max_tokens=max_output_tokens,
            temperature=0.2,
            response_format="json",
            logger=self.logger,
            retry_attempts=transport_attempt_limit,
            provider_runtime=runtime,
        )
        completion = ProviderCompletionEvaluator.evaluate(
            result,
            minimum_output=2,
            expect_json=True,
        )
        if completion.status != "complete":
            raise RuntimeError(
                f"Writer output is {completion.status}: "
                f"{completion.error_kind or completion.incomplete_reason or 'invalid output'}"
            )
        content = completion.content
        if not isinstance(content, Mapping):
            raise RuntimeError("Writer output must be a JSON object")
        # Preserve transport usage/finish metadata for the durable receipt;
        # only the normalized JSON content is replaced by the completion
        # evaluator's validated object.
        normalized_result = dict(result)
        normalized_result["status"] = "success"
        normalized_result["content"] = dict(content)
        return normalized_result

    def _normalize_blocks(
        self,
        provider_result: Mapping[str, Any],
        *,
        section_number: int,
        allowed_ref_ids: Sequence[str],
        catalog: Mapping[str, Any],
        writer_task_scope: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if str(provider_result.get("status") or "success").strip().lower() != "success":
            raise RuntimeError(
                f"Writer failed: {provider_result.get('error_kind') or provider_result.get('message') or 'unknown error'}"
            )
        content = provider_result.get("content", provider_result)
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Writer returned non-JSON section content") from exc
        if not isinstance(content, Mapping):
            raise RuntimeError("Writer section content must be an object")
        if writer_task_scope is not None:
            from services.writer_task_scope import validate_writer_task_output_v1

            scoped_output = validate_writer_task_output_v1(
                writer_task_scope, content,
                provider_semantic_contract_version=self.provider_semantic_contract_version,
            )
            if scoped_output.get("scope_status") != "ready":
                raise RuntimeError("Writer returned non-adoptable source tasks requiring review")
            if writer_task_scope.get("schema_version") == "writer_task_scope/v2":
                return self._normalize_table_projection(
                    scoped_output, scope=writer_task_scope, section_number=section_number,
                    allowed_ref_ids=allowed_ref_ids, catalog=catalog,
                )
            content = scoped_output
        raw_blocks = content.get("blocks")
        if not isinstance(raw_blocks, list):
            raise RuntimeError("Writer section content must contain a blocks array")

        blocks: list[dict[str, Any]] = []
        for order, raw_block in enumerate(raw_blocks, start=1):
            if not isinstance(raw_block, Mapping):
                raise RuntimeError(f"Writer block {section_number}:{order} is not an object")
            text = str(raw_block.get("text") or "").strip()
            if not text:
                raise RuntimeError(f"Writer block {section_number}:{order} is empty")
            token_ref_ids = self._token_refs(text)
            explicit = raw_block.get("ref_ids") or raw_block.get("citation_ref_ids") or ()
            explicit_ref_ids = [str(item).strip() for item in explicit if str(item).strip()]
            missing_explicit_ref_ids = [
                ref_id for ref_id in explicit_ref_ids if ref_id not in set(token_ref_ids)
            ]
            if missing_explicit_ref_ids:
                # Explicit refs are promises about the rendered text, not a
                # side-channel list.  Materialize every promised ref as a
                # structured token so occurrence spans and the manifest see
                # the same citation truth source.
                token = f"[[cite_ref:{', '.join(missing_explicit_ref_ids)}]]"
                text = f"{text} {token}"
            ref_ids = list(dict.fromkeys([*self._token_refs(text), *explicit_ref_ids]))
            if not ref_ids:
                raise RuntimeError(f"Writer block {section_number}:{order} has no structured citation")
            invalid = [ref_id for ref_id in ref_ids if ref_id not in allowed_ref_ids]
            unresolved = [ref_id for ref_id in ref_ids if resolve_ref_id(catalog, ref_id) is None]
            if invalid:
                raise RuntimeError(
                    f"Writer block {section_number}:{order} cites papers outside its evidence packet: {invalid}"
                )
            if unresolved:
                raise RuntimeError(
                    f"Writer block {section_number}:{order} has unresolved citation refs: {unresolved}"
                )
            citations: list[dict[str, Any]] = []
            for token_index, match in enumerate(
                re.finditer(r"\[\[cite_ref:[^\]]+\]\]", text),
                start=1,
            ):
                token = match.group(0)
                cluster_start, cluster_end = match.span()
                for occurrence_index, ref_id in enumerate(
                    extract_ref_ids_from_token(token),
                    start=1,
                ):
                    citations.append(
                        {
                            "local_ref_id": (
                                f"s{section_number}_b{order}_cite_"
                                f"{token_index}_{occurrence_index}"
                            ),
                            "citation_token": token,
                            "ref_id": ref_id,
                            "raw_text": token,
                            "mode": "parenthetical",
                            "span_start": cluster_start,
                            "span_end": cluster_end,
                            "cluster_index": token_index,
                            "occurrence_index": occurrence_index,
                        }
                    )
            blocks.append(
                {
                    "block_id": f"s{section_number}_b{order}",
                    "block_kind": "paragraph",
                    "block_order": order,
                    "text": text,
                    "citations": citations,
                    "block_source": "writer_v3",
                }
            )
            if writer_task_scope is not None:
                blocks[-1].update({
                    name: raw_block[name]
                    for name in ("writer_task_id", "writer_output_unit_id", "writer_task_basis_hash")
                })
        if not blocks:
            raise RuntimeError(f"Writer produced no blocks for section {section_number}")
        return blocks

    def _normalize_table_projection(
        self, validated_output: Mapping[str, Any], *, scope: Mapping[str, Any],
        section_number: int, allowed_ref_ids: Sequence[str], catalog: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        from services.writer_table_layout import project_writer_table_layouts_v1

        normalized_units = self._normalize_blocks(
            {"status": "success", "content": {"blocks": validated_output["blocks"]}},
            section_number=section_number, allowed_ref_ids=allowed_ref_ids, catalog=catalog,
        )
        normalized_by_unit = {
            raw["writer_output_unit_id"]: normalized
            for raw, normalized in zip(validated_output["blocks"], normalized_units)
        }
        projected = project_writer_table_layouts_v1(scope, validated_output)["blocks"]

        def normalize_unit(unit):
            normalized = dict(normalized_by_unit[unit["writer_output_unit_id"]])
            normalized.update({key: value for key, value in unit.items() if key != "text"})
            for index, citation in enumerate(normalized["citations"], start=1):
                citation["local_ref_id"] = f"{unit['block_id']}_cite_{index}"
            return normalized

        blocks = []
        for order, block in enumerate(projected, start=1):
            if block["block_kind"] == "table":
                normalized = {**block, "text": "", "citations": [], "block_source": "writer_v3_local_table_projection"}
                normalized["rows"] = [{
                    "row_id": row["row_id"], "cells": [
                        normalize_unit(cell) if cell["cell_kind"] == "factual_output_unit" else dict(cell)
                        for cell in row["cells"]
                    ],
                } for row in block["rows"]]
            else:
                normalized = normalize_unit(block)
            normalized["block_order"] = order
            blocks.append(normalized)
        return blocks

    def _allowed_ref_ids(
        self,
        packet: Mapping[str, Any],
        catalog: Mapping[str, Any],
    ) -> tuple[str, ...]:
        paper_keys = {
            str(item).strip()
            for item in (packet.get("paper_keys") or packet.get("must_use_paper_keys") or [])
            if str(item).strip()
        }
        if not paper_keys:
            raise RuntimeError(f"section evidence packet {packet.get('section_id')} has no paper keys")
        ref_ids = [
            str(entry.get("ref_id") or "").strip()
            for entry in catalog.get("entries", [])
            if isinstance(entry, Mapping)
            and entry.get("status") == "active"
            and str(entry.get("canonical_paper_key") or "").strip() in paper_keys
        ]
        if not ref_ids:
            raise RuntimeError(
                f"section evidence packet {packet.get('section_id')} has no catalog-resolvable papers"
            )
        return tuple(dict.fromkeys(ref_ids))

    @staticmethod
    def _token_refs(text: str) -> list[str]:
        refs: list[str] = []
        for match in re.finditer(r"\[\[cite_ref:[^\]]+\]\]", text):
            refs.extend(extract_ref_ids_from_token(match.group(0)))
        return list(dict.fromkeys(refs))

    @staticmethod
    def _require_nonempty_packet(packet: Mapping[str, Any], section_id: str) -> None:
        required = ("planned_claims", "paper_keys", "source_summary_hashes", "retrieval_provenance")
        missing = [field for field in required if not packet.get(field)]
        if missing:
            raise RuntimeError(
                f"section evidence packet {section_id} is incomplete: {', '.join(missing)}"
            )

    def _prompt(
        self,
        section: Mapping[str, Any],
        packet: Mapping[str, Any],
        catalog: Mapping[str, Any],
        allowed_ref_ids: Sequence[str],
        *,
        writer_task_scope: Mapping[str, Any] | None = None,
    ) -> str:
        evidence = self._summaries_for_packet(packet)
        payload = {
            "section": dict(section),
            "evidence_packet": dict(packet),
            "source_evidence": evidence,
            "citation_ref_catalog": [
                dict(entry)
                for entry in catalog.get("entries", [])
                if isinstance(entry, Mapping) and str(entry.get("ref_id") or "") in set(allowed_ref_ids)
            ],
            "allowed_citation_ref_ids": list(allowed_ref_ids),
            "output_contract": {
                "json_object": {"blocks": [{"text": "paragraph with [[cite_ref:R###]]"}]},
                "must_use_only_allowed_refs": True,
                "must_ground_each_block_in_packet": True,
            },
            "free_mode_context": dict(self.free_mode_context) if self.free_mode_context else None,
        }
        if writer_task_scope is not None:
            from services.writer_wire_projection import (
                project_writer_scope_for_provider_v1,
            )

            if self.provider_semantic_contract_version == PROVIDER_SEMANTIC_CONTRACT_V2 and self.free_mode_context:
                raise RuntimeError("source-closed Writer Free Mode context projection is not supported yet")
            payload["writer_task_scope"] = project_writer_scope_for_provider_v1(
                writer_task_scope, provider_semantic_contract_version=self.provider_semantic_contract_version,
            )
            payload.pop("source_evidence")
            payload["section_context"] = {
                name: packet[name]
                for name in (
                    "section_goal", "research_question_link", "paper_roles", "paper_keys",
                    "must_use_paper_keys", "relation_ids", "contradictions", "boundary_conditions", "gaps",
                ) if name in packet
            }
            payload.pop("evidence_packet")
            payload["output_contract"] = {
                **dict(writer_task_scope["output_contract"]),
                "schema_version": writer_task_scope["schema_version"],
                "requires_one_disposition_per_task": True,
                "requires_primary_unit_for_covered_task": True,
                "requires_exact_task_unit_and_basis_ids": True,
            }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _summaries_for_packet(self, packet: Mapping[str, Any]) -> list[dict[str, Any]]:
        keys = {
            str(item).strip()
            for item in (packet.get("paper_keys") or [])
            if str(item).strip()
        }
        selected: list[dict[str, Any]] = []
        for summary in self.summaries:
            paper = summary.get("paper_info")
            if not isinstance(paper, Mapping):
                continue
            if str(paper.get("canonical_paper_key") or "").strip() not in keys:
                continue
            ai_summary = summary.get("ai_summary")
            core = ai_summary.get("core_analysis", {}) if isinstance(ai_summary, Mapping) else {}
            selected.append(
                {
                    "canonical_paper_key": paper.get("canonical_paper_key"),
                    "title": paper.get("title"),
                    "authors": paper.get("authors"),
                    "year": paper.get("year"),
                    "summary": core.get("summary") if isinstance(core, Mapping) else "",
                    "methodology": core.get("methodology") if isinstance(core, Mapping) else "",
                    "findings": core.get("findings") if isinstance(core, Mapping) else "",
                    "conclusions": core.get("conclusions") if isinstance(core, Mapping) else "",
                }
            )
        if not selected:
            raise RuntimeError(f"section evidence packet {packet.get('section_id')} has no source summaries")
        return selected

    def _new_runtime(
        self,
        section_id: str,
        *,
        writer_config: Mapping[str, Any] | None = None,
    ) -> ProviderRuntime:
        config = dict(
            writer_config
            if writer_config is not None
            else self.settings.section("Writer_API")
        )
        return ProviderRuntime(
            budget=ProviderBudgetV1(
                max_calls=max(1, self.settings.runtime.node_retry_limit + 1),
                max_retries_per_call=self.settings.runtime.node_retry_limit,
            ),
            ledger=self.receipt_ledger,
            job_id=self.job_id,
            # Section calls use a stable node attempt identity so an exact
            # durable section replay remains reusable across job resumes.
            attempt_id=f"review:{section_id}",
            stage_name="stage3_review",
            route="Writer_API",
            node_id=section_id,
            call_id=f"review:{section_id}",
            closure_epoch_id=self.closure_epoch_id,
            logical_attempt_identity=self.attempt_id,
            endpoint_type=str(config.get("endpoint_type") or "responses"),
            test_only=self.writer is not None,
            schema_hash=hashlib.sha256(b"review_draft_v3_writer_section").hexdigest(),
            prompt_id=self._review_prompt_identity.prompt_id,
            prompt_version=self._review_prompt_identity.version,
            prompt_sha256=self._review_prompt_identity.sha256,
            response_snapshot_sink=self.response_snapshot_sink,
        )

    def _build_provider_request_inventory(
        self,
        prepared_sections: Sequence[_PreparedWriterSection],
        *,
        writer_config: Mapping[str, Any],
        max_output_tokens: int,
        transport_attempt_limit: int,
    ) -> tuple[ProviderStageRequestInventoryV1, Any]:
        from runtime.provider_context import ProviderContextProfile

        route_config = dict(self.settings.sections)
        route_config["Writer_API"] = dict(writer_config)
        route_plan = build_reachable_provider_route_plan(
            route_config,
            action="generate_review",
            requested_stages=("review",),
            free_mode_enabled=bool(self.free_mode_context),
        )
        route = route_plan.route_for_role("writer")
        if not route.resolved:
            raise RuntimeError("Review v3 Writer route is unresolved before request inventory")

        bound_profile = self._provider_context_profile(writer_config)
        profile = ProviderContextProfile.conservative(
            provider=route.provider_family,
            model=route.model,
            endpoint_type=route.endpoint_type,
            model_context_limit=bound_profile.model_context_limit,
            max_output_tokens=max_output_tokens,
            reasoning_reserve=bound_profile.reasoning_reserve,
            safety_margin=bound_profile.safety_margin,
            tokenizer_strategy=bound_profile.tokenizer_strategy,
        )
        wall_seconds = self._writer_total_timeout_seconds(writer_config)
        rows: list[ProviderRequestPlanRowV1] = []
        for prepared in prepared_sections:
            call_id = f"review:{prepared.section_id}"
            request_hash = hash_json(prepared.request_payload)
            if request_hash != str(prepared.binding.get("prompt_payload_hash") or ""):
                raise RuntimeError(
                    f"Review v3 Writer request identity changed before inventory for {call_id}"
                )
            expected = self._expected_provider_calls[call_id]
            self._expected_provider_calls[call_id] = replace(
                expected,
                provider=route.provider_family,
                model=route.model,
                endpoint=str(writer_config.get("api_base") or ""),
                endpoint_type=(
                    str(writer_config.get("endpoint_type") or "responses")
                    if self.writer is not None
                    else route.endpoint_type
                ),
            )
            if self.source_revision_replay_context is not None:
                self._reuse_source_revision_section(prepared)
            reused_receipt = self._verified_reuse_receipts.get(call_id)
            if reused_receipt is not None and (
                reused_receipt.provider != route.provider_family
                or reused_receipt.model != route.model
                or reused_receipt.endpoint != str(writer_config.get("api_base") or "")
                or (
                    not reused_receipt.test_only
                    and reused_receipt.endpoint_type != route.endpoint_type
                )
                or (
                    reused_receipt.test_only
                    and writer_config.get("endpoint_type")
                    and reused_receipt.endpoint_type != str(writer_config.get("endpoint_type"))
                )
            ):
                raise RuntimeError(
                    f"Review v3 same-binding receipt route differs from the current Writer route for {call_id}"
                )
            verified_reuse: VerifiedProviderReuseAuthorityV1 | None = None
            reuse_proof = self._verified_reuse_proofs.get(call_id)
            source_reuse = self._source_revision_proofs.get(call_id)
            if source_reuse is not None:
                proof, output, match = source_reuse
                context = self.source_revision_replay_context
                if context is None:
                    raise RuntimeError("SourceRevision Writer inventory lost its executing context")
                verified_reuse = context.verified_provider_reuse_authority_v1(
                    match=match, proof_record=proof, route_identity=route.identity,
                    child_expected_call=self._expected_provider_calls[call_id], child_output_record=output,
                )
            elif prepared.persisted is not None:
                if reuse_proof is None:
                    raise RuntimeError(
                        f"Review v3 Writer replay lacks verified receipt authority for {call_id}"
                    )
                verified_reuse = VerifiedProviderReuseAuthorityV1(
                    request_hash=request_hash,
                    route_identity=route.identity,
                    receipt_hash=reuse_proof["receipt_hash"],
                    output_hash=reuse_proof["output_hash"],
                    authority_hash=reuse_proof["authority_hash"],
                )
            requested_attempts = prepared.runtime.max_attempts_for_call(
                transport_attempt_limit
            )
            rows.append(
                build_provider_request_plan_row_v1(
                    stage_name="review",
                    request_id=call_id,
                    source_builder=(
                        "services.review_generation_service.ReviewGenerationService._writer_request_payload"
                    ),
                    route=route,
                    request_payload=prepared.request_payload,
                    profile=profile,
                    retry_attempts=max(0, requested_attempts - 1),
                    retry_policy="shared_optional",
                    requested_output_tokens=max_output_tokens,
                    reasoning_reserve_tokens=profile.reasoning_reserve,
                    verified_reuse=verified_reuse,
                    wall_seconds_upper_bound=wall_seconds,
                )
            )

        return (
            ProviderStageRequestInventoryV1(
                stage_name="review",
                source_builder="services.review_generation_service writer request builder",
                requests=tuple(rows),
            ),
            route_plan,
        )

    @staticmethod
    def _writer_total_timeout_seconds(writer_config: Mapping[str, Any]) -> float:
        # This is the same request-level configuration path used by
        # _call_ai_api_detailed; its transport loop shares one total deadline
        # across all retries and bounds response reads by that deadline.
        from ai_interface import _load_api_runtime_settings

        request_timeout, _ = _load_api_runtime_settings(cast(APIConfig, writer_config))
        try:
            configured_total = int(
                str(writer_config.get("total_timeout_seconds") or "").strip()
            )
        except (TypeError, ValueError):
            configured_total = 0
        return float(configured_total if configured_total > 0 else max(1, int(request_timeout)))

    def _preflight_provider_request_inventory(
        self,
        inventory: ProviderStageRequestInventoryV1,
        route_plan: Any,
    ) -> None:
        fresh_rows = [row for row in inventory.requests if not row.verified_reuse]
        over_context = [
            row
            for row in fresh_rows
            if not row.request_estimate.within_input_budget
            or not row.request_estimate.within_context_budget
        ]
        if over_context:
            call_id_hashes = ", ".join(row.request_key_hash for row in over_context)
            raise ProviderBudgetExceeded(
                "Writer stage preflight exceeds the provider request context budget: "
                + call_id_hashes
            )

        controller = provider_budget_controller_from_environment()
        if controller is None or not isinstance(controller.budget, ProviderAggregateBudgetV2):
            return

        snapshot = controller.snapshot()
        budget = controller.budget
        def remaining(field: str, limit: int) -> int:
            return max(
                0,
                limit - int(snapshot[f"{field}_used"]) - int(snapshot[f"{field}_reserved"]),
            )

        remaining_budget = ProviderAggregateBudgetV2(
            max_provider_calls_total=remaining("calls", budget.max_provider_calls_total),
            max_output_tokens_total=remaining("output_tokens", budget.max_output_tokens_total),
            max_retry_attempts_total=remaining("retry_attempts", budget.max_retry_attempts_total),
            max_wall_seconds=max(
                0.0,
                budget.max_wall_seconds - float(snapshot["elapsed_seconds"]),
            ),
        )
        if fresh_rows and remaining_budget.max_wall_seconds <= 0:
            raise ProviderBudgetExceeded(
                "Writer stage preflight has no remaining aggregate wall-time budget"
            )
        projection = build_full_stage_request_plan_v1(
            stage_plan=route_plan.stage_plan,
            reachable_route_plan=route_plan,
            stage_inventories=(inventory,),
            aggregate_budget=remaining_budget,
        )
        budget_status = projection["budget_status"]
        for key, label in (
            ("provider_calls", "provider call"),
            ("requested_output_tokens", "output token"),
            ("provider_retries", "retry"),
        ):
            if budget_status.get(key) == "exceeded":
                raise ProviderBudgetExceeded(
                    f"Writer stage preflight exceeds remaining aggregate {label} budget"
                )
        # Summed configured request deadlines describe worst-case exposure,
        # not the minimum duration required for the section work. The shared
        # durable deadline still bounds admission and each active transport.

    def _ensure_receipt(
        self,
        runtime: ProviderRuntime,
        *,
        prompt: str,
        input_payload: Mapping[str, Any],
        result: Mapping[str, Any],
        api_config: Mapping[str, Any],
    ) -> None:
        if runtime.receipts:
            return
        if not self._fixture_context_enabled():
            raise RuntimeError("Writer transport returned no instrumented provider receipt")
        runtime.test_only = True
        try:
            request = dict(input_payload)
            profile = self._provider_context_profile(api_config)
            estimate = profile.estimate_request(request)
            admission = runtime.admit(
                estimated_tokens=max(1, int(estimate["estimated_input_tokens"])),
                requested_output_tokens=max(0, int(profile.max_output_tokens)),
                requested_retry_attempts=max(0, int(result.get("attempts") or 1) - 1),
            )
            runtime.complete(
                admission=admission,
                prompt=prompt,
                input_payload=input_payload,
                api_config=dict(api_config),
                result=result,
                metadata={"execution_mode": "offline_mocked_transport", "transport_started": False},
            )
        except ProviderBudgetExceeded:
            runtime.blocked_receipt(
                prompt=prompt,
                input_payload=input_payload,
                api_config=dict(api_config),
                message="Writer did not produce a provider receipt before its budget closed",
            )
            raise

    @staticmethod
    def _fixture_context_enabled() -> bool:
        from runtime.test_dependencies import current_runtime_test_dependencies

        dependencies = current_runtime_test_dependencies()
        if dependencies is None:
            return False
        dependencies.validate()
        return True

    def _provider_context_profile(
        self,
        writer_config: Mapping[str, Any] | None = None,
    ) -> Any:
        from runtime.provider_context import ProviderContextProfile

        config = dict(
            writer_config
            if writer_config is not None
            else self.settings.section("Writer_API")
        )
        return ProviderContextProfile.from_api_config(
            config,
            max_output_tokens=self._max_output_tokens(config),
            default_model="writer",
        )

    def _register_receipt_ledger(self) -> None:
        if not self.receipt_ledger.path.is_file():
            return
        payload = self.receipt_ledger.path.read_bytes()
        if not payload.strip():
            self.receipt_ledger_path = ""
            return
        record = publish_bytes_artifact(
            self.publication_context,
            self.registry,
            self.receipt_ledger_target_path,
            payload,
            artifact_role="provider_receipts",
            artifact_type="provider_receipt_ledger",
            artifact_version="v1",
            producer="services.review_generation_service.ReviewGenerationService",
            artifact_id="review_provider_receipts",
            metadata={"receipt_count": len(self.receipt_ledger.list_receipts())},
        )
        self.receipt_ledger_path = record.path

    def _check_cancelled(self) -> None:
        if self.cancellation_checker is not None:
            self.cancellation_checker()

    def _max_output_tokens(
        self,
        writer_config: Mapping[str, Any] | None = None,
    ) -> int:
        config = (
            writer_config
            if writer_config is not None
            else self.settings.section("Writer_API")
        )
        from runtime.provider_context import writer_output_token_limit

        return writer_output_token_limit(config)

    def _writer_request_payload(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Build the same canonical request identity used by provider transport."""

        return canonical_provider_request_payload(
            prompt=prompt,
            system_prompt=system_prompt if system_prompt is not None else self._system_prompt(),
            user_content=None,
            response_format="json",
            max_output_tokens=(
                self._max_output_tokens()
                if max_output_tokens is None
                else int(max_output_tokens)
            ),
            temperature=0.2,
        )

    @staticmethod
    def _system_prompt() -> str:
        try:
            return PromptRegistry().read("review.section_writer.system.v3")
        except PromptRegistryError:
            return "You are an academic literature review writer. Return only the requested JSON object."


__all__ = ["ReviewGenerationResult", "ReviewGenerationService"]

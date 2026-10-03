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
from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderBudgetExceeded,
    ProviderBudgetV1,
    ProviderRuntime,
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
    ) -> None:
        self.job_id = str(job_id)
        self.attempt_id = str(attempt_id or "review")
        self.workspace = workspace
        self.registry = artifact_registry
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
        self._expected_provider_calls = {}
        self._verified_reuse_proofs = {}
        self.provider_request_inventory = None
        from services.writer_source_inventory import load_writer_source_inventory_v1

        source_inventory = load_writer_source_inventory_v1(self.registry)
        catalog, catalog_path = self._build_and_persist_catalog()
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

                task_scope = build_writer_task_scope_v1(packet, catalog, source_inventory=source_inventory)
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
                scoped_output = validate_writer_task_output_v1(prepared.writer_task_scope, content)
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
            section_record = self._persist_section(
                section_id,
                section_payload,
                raw_section=raw_section,
                packet=packet,
                catalog=catalog,
                binding=binding,
            )
            sections.append(section_payload)
            if runtime.receipts:
                receipt = runtime.receipts[-1]
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
                self._persist_review_replay(
                    section_id=section_id,
                    binding=binding,
                    section_record=section_record,
                    section_hash=logical_hash,
                    receipt_id=receipt.receipt_id,
                    normalized_output_hash=receipt.response_hash or "",
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
        return {
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

    def _load_review_replay(self, *, section_id: str, binding: Mapping[str, Any]) -> Mapping[str, Any] | None:
        current = self.registry.get("review_replay")
        path = Path(current.path) if current is not None and current.status == "ready" else self._review_replay_path()
        if not path.is_file():
            return None
        binding_hash = hash_json(binding)
        found: Mapping[str, Any] | None = None
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                payload = json.loads(line)
                if not isinstance(payload, Mapping):
                    continue
                if (
                    str(payload.get("section_id") or "") == section_id
                    and str(payload.get("binding_hash") or "") == binding_hash
                ):
                    found = dict(payload)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return found

    def _persist_review_replay(
        self,
        *,
        section_id: str,
        binding: Mapping[str, Any],
        section_record: Any,
        section_hash: str,
        receipt_id: str,
        normalized_output_hash: str,
    ) -> None:
        path = self._review_replay_path()
        binding_hash = hash_json(binding)
        existing = self._load_review_replay(section_id=section_id, binding=binding)
        payload = {
            "replay_version": "review-section-replay-v1",
            "job_id": self.job_id,
            "stage_name": "stage3_review",
            "closure_epoch_id": self.closure_epoch_id,
            "section_id": section_id,
            "binding_hash": binding_hash,
            "artifact_id": section_record.artifact_id,
            "artifact_path": section_record.path,
            "artifact_content_hash": section_hash,
            "registry_file_hash": section_record.content_hash,
            "receipt_id": receipt_id,
            "normalized_output_hash": normalized_output_hash,
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

    def _load_section(
        self,
        section_id: str,
        *,
        raw_section: Mapping[str, Any],
        packet: Mapping[str, Any],
        catalog: Mapping[str, Any],
        binding: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        record = self.registry.get(f"review-section:{section_id}")
        path = Path(record.path) if record is not None else self._section_path(section_id)
        if record is None or record.status != "ready" or not path.is_file():
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(envelope, Mapping) or envelope.get("status") != "ready":
            return None
        payload = envelope.get("section")
        if not isinstance(payload, Mapping):
            return None
        expected_binding_hash = hash_json(binding)
        if envelope.get("binding_hash") != expected_binding_hash:
            return None
        if envelope.get("binding") != dict(binding):
            return None
        section_hash = hash_json(payload)
        if envelope.get("content_hash") != section_hash:
            return None
        try:
            if record.content_hash != file_sha256(path):
                return None
        except OSError:
            return None
        replay = self._load_review_replay(section_id=section_id, binding=binding)
        if replay is None:
            return None
        replay_epoch_id = str(replay.get("closure_epoch_id") or "")
        if (
            str(replay.get("job_id") or "") != self.job_id
            or str(replay.get("stage_name") or "") != "stage3_review"
            or not replay_epoch_id
        ):
            return None
        reuse_record = self.registry.get("review_replay")
        if reuse_record is None or reuse_record.status != "ready":
            return None
        receipt_id = str(replay.get("receipt_id") or "")
        receipt = next(
            (item for item in self.receipt_ledger.list_receipts() if item.receipt_id == receipt_id),
            None,
        )
        expected = self._expected_provider_calls.get(f"review:{section_id}")
        if expected is None or receipt is None or receipt.status != "success":
            return None
        if receipt.test_only and not self._fixture_context_enabled():
            return None
        if (
            receipt.job_id != self.job_id
            or receipt.attempt_id != expected.attempt_id
            or receipt.stage_name != expected.stage_name
            or receipt.node_id != expected.node_id
            or receipt.call_id != expected.call_id
            or receipt.closure_epoch_id != replay_epoch_id
            or receipt.prompt_hash != expected.prompt_hash
            or receipt.input_hash != expected.input_hash
            or receipt.config_hash != expected.config_hash
            or receipt.schema_hash != expected.schema_hash
            or receipt.response_hash != str(replay.get("normalized_output_hash") or "")
            or str(replay.get("artifact_path") or "") != str(path)
            or str(replay.get("artifact_content_hash") or "") != section_hash
            or str(replay.get("registry_file_hash") or "") != record.content_hash
        ):
            return None
        if expected.usage_required and receipt.usage_status not in {"reported", "provider_not_supported"}:
            return None
        self._expected_provider_calls[expected.call_id] = replace(
            expected,
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
        receipt_hash = hash_json(receipt.to_dict())
        self._verified_reuse_proofs[expected.call_id] = {
            "receipt_hash": receipt_hash,
            "output_hash": receipt.response_hash or "",
            "reuse_evidence_artifact_id": reuse_record.artifact_id,
            "reuse_evidence_artifact_hash": reuse_record.content_hash,
            "reuse_evidence_record_hash": hash_json(replay),
            "authority_hash": hash_json(
                {
                    "authority_version": "review-section-replay-authority-v1",
                    "call_id": expected.call_id,
                    "binding_hash": expected_binding_hash,
                    "receipt_id": receipt.receipt_id,
                    "receipt_hash": receipt_hash,
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

    def _persist_receipt_closure(self) -> None:
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
        closure = ProviderReceiptClosure.evaluate(
            self._expected_provider_calls.values(),
            scoped_receipts,
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
        for artifact_id in (
            "citation_ref_catalog",
            "outline-v3:final_outline",
            "outline-v3:section_evidence_packets",
            "outline-v3:adoption:current",
        ):
            add_dependency(self.registry.get(artifact_id))
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

            scoped_output = validate_writer_task_output_v1(writer_task_scope, content)
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
            from services.writer_wire_projection import project_writer_scope_for_provider_v1

            payload["writer_task_scope"] = project_writer_scope_for_provider_v1(writer_task_scope)
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
            verified_reuse: VerifiedProviderReuseAuthorityV1 | None = None
            reuse_proof = self._verified_reuse_proofs.get(call_id)
            if prepared.persisted is not None:
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
            ("wall_time", "wall-time"),
        ):
            if budget_status.get(key) == "exceeded":
                raise ProviderBudgetExceeded(
                    f"Writer stage preflight exceeds remaining aggregate {label} budget"
                )

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
        raw = config.get("max_output_tokens") or 32000
        try:
            return max(256, int(raw))
        except (TypeError, ValueError):
            return 32000

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

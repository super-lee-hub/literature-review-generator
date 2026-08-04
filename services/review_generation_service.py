"""Evidence-bound Writer Review v3 execution."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence, cast

from models import APIConfig
from runtime.provider_completion import ProviderCompletionEvaluator
from runtime.provider_runtime import (
    ProviderBudgetV1,
    ProviderBudgetExceeded,
    ProviderRuntime,
    ProviderRuntimeLedger,
    _redact_mapping,
    hash_json,
    hash_text,
)
from runtime.provider_receipt_closure import ExpectedProviderCall, ProviderReceiptClosure
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry, file_sha256
from services.citation_ref_catalog import (
    build_document_ref_catalog,
    extract_ref_ids_from_token,
    resolve_ref_id,
    validate_document_ref_catalog,
)
from services.job_workspace import JobWorkspace, atomic_write_json
from services.settings import ApplicationSettings


WriterCallable = Callable[..., Mapping[str, Any]]


@dataclass(frozen=True)
class ReviewGenerationResult:
    sections: tuple[dict[str, Any], ...]
    citation_ref_catalog: dict[str, Any]
    citation_ref_catalog_path: str
    receipt_ids: tuple[str, ...]
    receipt_ledger_path: str


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
    ) -> None:
        self.job_id = str(job_id)
        self.attempt_id = str(attempt_id or "review")
        self.workspace = workspace
        self.registry = artifact_registry
        self.settings = settings
        self.summaries = [dict(item) for item in summaries]
        self.writer = writer
        self.cancellation_checker = cancellation_checker
        self.logger = logger or logging.getLogger("auto_generate.review")
        self.receipt_ledger = ProviderRuntimeLedger(
            self.workspace.artifact_path("review_provider_receipts.jsonl")
        )
        self._expected_provider_calls: dict[str, ExpectedProviderCall] = {}

    def run(
        self,
        *,
        outline_payload: Mapping[str, Any],
        evidence_packets: Sequence[Mapping[str, Any]],
    ) -> ReviewGenerationResult:
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

        for number, raw_section in enumerate(raw_sections, start=1):
            self._check_cancelled()
            if not isinstance(raw_section, Mapping):
                continue
            section_id = str(raw_section.get("section_id") or f"section_{number}").strip()
            packet = packet_by_section.get(section_id)
            if packet is None:
                raise RuntimeError(f"Review v3 has no evidence packet for section {section_id}")
            self._require_nonempty_packet(packet, section_id)
            allowed_ref_ids = self._allowed_ref_ids(packet, catalog)
            runtime = self._new_runtime(section_id)
            call_id = f"review:{section_id}"
            writer_config = dict(self.settings.section("Writer_API"))
            prompt = self._prompt(raw_section, packet, catalog, allowed_ref_ids)
            request_payload = self._writer_request_payload(prompt)
            binding = self._section_binding(
                section_id=section_id,
                raw_section=raw_section,
                packet=packet,
                catalog=catalog,
                request_payload=request_payload,
                runtime=runtime,
                writer_config=writer_config,
            )
            self._expected_provider_calls[call_id] = ExpectedProviderCall(
                call_id=call_id,
                job_id=self.job_id,
                attempt_id=runtime.attempt_id,
                stage_name=runtime.stage_name,
                node_id=section_id,
                prompt_hash=str(binding["prompt_hash"]),
                input_hash=str(binding["prompt_payload_hash"]),
                config_hash=str(binding["writer_config_hash"]),
                schema_hash=runtime.schema_hash,
                max_attempts=max(1, self.settings.runtime.node_retry_limit + 1),
                usage_required=str(writer_config.get("endpoint_type") or "responses")
                not in {"internal", "fixture"},
            )
            persisted = self._load_section(
                section_id,
                raw_section=raw_section,
                packet=packet,
                catalog=catalog,
                binding=binding,
            )
            if persisted is not None:
                sections.append(persisted)
                continue
            provider_result = self._call_writer(
                section_number=number,
                section=raw_section,
                packet=packet,
                catalog=catalog,
                allowed_ref_ids=allowed_ref_ids,
                runtime=runtime,
            )
            self._ensure_receipt(
                runtime,
                prompt=prompt,
                input_payload=request_payload,
                result=provider_result,
            )
            blocks = self._normalize_blocks(
                provider_result,
                section_number=number,
                allowed_ref_ids=allowed_ref_ids,
                catalog=catalog,
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
            receipt_ledger_path=str(self.receipt_ledger.path),
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
        atomic_write_json(str(path), catalog)
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
        self.registry.register_file(
            artifact_role="citation_ref_catalog",
            artifact_type="citation_ref_catalog",
            artifact_version="v1",
            path=str(path),
            producer="services.review_generation_service.ReviewGenerationService",
            artifact_id="citation_ref_catalog",
            depends_on=dependencies,
            metadata={"catalog_hash": catalog["catalog_hash"]},
        )
        return catalog, path

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
        profile = self._provider_context_profile()
        adoption = self._current_adoption_binding()
        return {
            "binding_version": "review-section-binding-v1",
            "stage_name": runtime.stage_name,
            "section_id": section_id,
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
            "system_prompt_hash": hash_text(self._system_prompt()),
            "prompt_template_hash": hash_text("review-writer-v3:section-json-v1"),
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
        path = self._review_replay_path()
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
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
        self.registry.register_file(
            artifact_role="review_replay",
            artifact_type="review_replay_ledger",
            artifact_version="v1",
            path=str(path),
            producer="services.review_generation_service.ReviewGenerationService",
            artifact_id="review_replay",
            metadata={"binding_version": "review-section-binding-v1"},
        )

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
        receipt_id = str(replay.get("receipt_id") or "")
        receipt = next(
            (item for item in self.receipt_ledger.list_receipts() if item.receipt_id == receipt_id),
            None,
        )
        expected = self._expected_provider_calls.get(f"review:{section_id}")
        if expected is None or receipt is None or receipt.status != "success":
            return None
        if (
            receipt.job_id != self.job_id
            or receipt.attempt_id != expected.attempt_id
            or receipt.stage_name != expected.stage_name
            or receipt.node_id != expected.node_id
            or receipt.call_id != expected.call_id
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
        )
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
        atomic_write_json(
            str(path),
            {
                "status": "ready",
                "job_id": self.job_id,
                "section_id": section_id,
                "binding_hash": binding_hash,
                "binding": dict(binding),
                "content_hash": section_hash,
                "section": dict(section),
            },
        )
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
        immutable_record = self.registry.register_file(
            artifact_role="review_section",
            artifact_type="review_section",
            artifact_version="v3",
            path=str(path),
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
        self.registry.register_file(
            artifact_role="review_section",
            artifact_type="review_section",
            artifact_version="v3",
            path=str(path),
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
        atomic_write_json(str(path), {"job_id": self.job_id, "payload": closure.to_dict()})
        dependencies: list[ArtifactDependencyRefV2] = []
        ledger = self.registry.get("review_provider_receipts")
        if ledger is not None and ledger.status == "ready":
            dependencies.append(ArtifactDependencyRefV2.from_record(ledger))
        self.registry.register_file(
            artifact_role="provider_receipt_closure",
            artifact_type="provider_receipt_closure",
            artifact_version="v1",
            path=str(path),
            producer="services.review_generation_service.ReviewGenerationService",
            artifact_id="review:provider_receipt_closure",
            depends_on=dependencies,
            metadata={"closure_hash": closure.closure_hash, "complete": closure.complete},
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
    ) -> Mapping[str, Any]:
        prompt = self._prompt(section, packet, catalog, allowed_ref_ids)
        writer_config = dict(self.settings.section("Writer_API"))
        if self.writer is not None:
            value = self.writer(
                prompt_text=prompt,
                writer_api_config=writer_config,
                provider_runtime=runtime,
                section_number=section_number,
                section=dict(section),
                evidence_packet=dict(packet),
                citation_ref_catalog=dict(catalog),
            )
            if not isinstance(value, Mapping):
                raise RuntimeError("Writer returned a non-object")
            return dict(value)

        from ai_interface import _call_ai_api_detailed

        if not str(writer_config.get("api_key") or "").strip() or not str(writer_config.get("model") or "").strip():
            raise RuntimeError("Writer_API is not configured for Review v3")
        result = _call_ai_api_detailed(
            prompt,
            cast(APIConfig, writer_config),
            self._system_prompt(),
            max_tokens=self._max_output_tokens(),
            temperature=0.2,
            response_format="json",
            logger=self.logger,
            retry_attempts=self.settings.runtime.transport_retries,
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
        if not blocks:
            raise RuntimeError(f"Writer produced no blocks for section {section_number}")
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

    def _new_runtime(self, section_id: str) -> ProviderRuntime:
        config = dict(self.settings.section("Writer_API"))
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
            endpoint_type=str(config.get("endpoint_type") or "responses"),
            schema_hash=hashlib.sha256(b"review_draft_v3_writer_section").hexdigest(),
        )

    def _ensure_receipt(
        self,
        runtime: ProviderRuntime,
        *,
        prompt: str,
        input_payload: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> None:
        if runtime.receipts:
            return
        try:
            request = dict(input_payload)
            profile = self._provider_context_profile()
            estimate = profile.estimate_request(request)
            admission = runtime.admit(
                estimated_tokens=max(1, int(estimate["estimated_input_tokens"]))
            )
            runtime.complete(
                admission=admission,
                prompt=prompt,
                input_payload=input_payload,
                api_config=dict(self.settings.section("Writer_API")),
                result=result,
                metadata={"execution_mode": "injected_writer"},
            )
        except ProviderBudgetExceeded:
            runtime.blocked_receipt(
                prompt=prompt,
                input_payload=input_payload,
                api_config=dict(self.settings.section("Writer_API")),
                message="Writer did not produce a provider receipt before its budget closed",
            )

    def _provider_context_profile(self) -> Any:
        from runtime.provider_context import ProviderContextProfile

        config = dict(self.settings.section("Writer_API"))
        try:
            context_limit = max(1, int(config.get("max_context_tokens") or 128_000))
        except (TypeError, ValueError):
            context_limit = 128_000
        try:
            output_tokens = max(1, int(config.get("max_output_tokens") or self._max_output_tokens()))
        except (TypeError, ValueError):
            output_tokens = self._max_output_tokens()
        return ProviderContextProfile.conservative(
            provider=str(config.get("provider_family") or "configured"),
            model=str(config.get("model") or "writer"),
            endpoint_type=str(config.get("endpoint_type") or "responses"),
            model_context_limit=context_limit,
            max_output_tokens=output_tokens,
        )

    def _register_receipt_ledger(self) -> None:
        if not self.receipt_ledger.path.is_file():
            return
        self.registry.register_file(
            artifact_role="provider_receipts",
            artifact_type="provider_receipt_ledger",
            artifact_version="v1",
            path=str(self.receipt_ledger.path),
            producer="services.review_generation_service.ReviewGenerationService",
            artifact_id="review_provider_receipts",
            metadata={"receipt_count": len(self.receipt_ledger.list_receipts())},
        )

    def _check_cancelled(self) -> None:
        if self.cancellation_checker is not None:
            self.cancellation_checker()

    def _max_output_tokens(self) -> int:
        raw = self.settings.section("Writer_API").get("max_output_tokens") or 32000
        try:
            return max(256, int(raw))
        except (TypeError, ValueError):
            return 32000

    def _writer_request_payload(self, prompt: str) -> dict[str, Any]:
        """Build the exact payload hashed by the bound provider runtime."""

        return {
            "system": self._system_prompt(),
            "user": prompt,
            "user_content": None,
            "response_format": "json",
            "max_output_tokens": self._max_output_tokens(),
            "temperature": 0.2,
        }

    @staticmethod
    def _system_prompt() -> str:
        path = Path(__file__).resolve().parents[1] / "prompts" / "prompt_system_section.txt"
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return "You are an academic literature review writer. Return only the requested JSON object."


__all__ = ["ReviewGenerationResult", "ReviewGenerationService"]

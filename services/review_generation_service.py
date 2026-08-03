"""Evidence-bound Writer Review v3 execution."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import logging
from pathlib import Path
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
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
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
            persisted = self._load_section(
                section_id,
                raw_section=raw_section,
                packet=packet,
                catalog=catalog,
            )
            if persisted is not None:
                sections.append(persisted)
                continue
            runtime = self._new_runtime(section_id)
            call_id = f"review:{section_id}"
            self._expected_provider_calls[call_id] = ExpectedProviderCall(
                call_id=call_id,
                job_id=self.job_id,
                attempt_id=self.attempt_id,
                stage_name="stage3_review",
                node_id=section_id,
                max_attempts=max(1, self.settings.runtime.node_retry_limit + 1),
                usage_required=False,
            )
            prompt = self._prompt(raw_section, packet, catalog, allowed_ref_ids)
            request_payload = self._writer_request_payload(prompt)
            writer_config = dict(self.settings.section("Writer_API"))
            self._expected_provider_calls[call_id] = replace(
                self._expected_provider_calls[call_id],
                prompt_hash=hash_text(prompt),
                input_hash=hash_json(request_payload),
                config_hash=hash_json(_redact_mapping(writer_config)),
                schema_hash=runtime.schema_hash,
            )
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
            self._persist_section(
                section_id,
                section_payload,
                raw_section=raw_section,
                packet=packet,
                catalog=catalog,
            )
            sections.append(section_payload)
            if runtime.receipts:
                receipt = runtime.receipts[-1]
                section_record = self.registry.get(f"review-section:{section_id}")
                self._expected_provider_calls[f"review:{section_id}"] = replace(
                    self._expected_provider_calls[f"review:{section_id}"],
                    output_hash=receipt.response_hash or "",
                    normalized_output_hash=receipt.response_hash or "",
                    registered_artifact_hash=(section_record.content_hash if section_record else ""),
                    node_output_hash=(section_record.content_hash if section_record else ""),
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

    def _section_path(self, section_id: str) -> Path:
        safe = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in section_id)
        return Path(self.workspace.artifact_path(f"review_sections/{safe}.json"))

    def _load_section(
        self,
        section_id: str,
        *,
        raw_section: Mapping[str, Any],
        packet: Mapping[str, Any],
        catalog: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        record = self.registry.get(f"review-section:{section_id}")
        path = self._section_path(section_id)
        if record is None or record.status != "ready" or not path.is_file():
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(envelope, Mapping) or envelope.get("status") != "ready":
            return None
        if envelope.get("outline_section_hash") != hashlib.sha256(
            json.dumps(dict(raw_section), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest():
            return None
        if envelope.get("evidence_packet_hash") != hashlib.sha256(
            json.dumps(dict(packet), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest():
            return None
        if envelope.get("catalog_hash") != str(catalog.get("catalog_hash") or ""):
            return None
        payload = envelope.get("section")
        return dict(payload) if isinstance(payload, Mapping) else None

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
    ) -> None:
        path = self._section_path(section_id)
        atomic_write_json(
            str(path),
            {
                "status": "ready",
                "job_id": self.job_id,
                "section_id": section_id,
                "outline_section_hash": self._input_hash(raw_section),
                "evidence_packet_hash": self._input_hash(packet),
                "catalog_hash": str(catalog.get("catalog_hash") or ""),
                "section": dict(section),
            },
        )
        dependencies: list[ArtifactDependencyRefV2] = []
        for artifact_id in ("outline-v3:section_evidence_packets", "citation_ref_catalog"):
            record = self.registry.get(artifact_id)
            if record is not None and record.status == "ready":
                dependencies.append(ArtifactDependencyRefV2.from_record(record))
        self.registry.register_file(
            artifact_role="review_section",
            artifact_type="review_section",
            artifact_version="v3",
            path=str(path),
            producer="services.review_generation_service.ReviewGenerationService",
            artifact_id=f"review-section:{section_id}",
            depends_on=dependencies,
            metadata={"section_id": section_id, "catalog_hash": str(catalog.get("catalog_hash") or "")},
        )

    def _persist_receipt_closure(self) -> None:
        expected_call_ids = set(self._expected_provider_calls)
        closure = ProviderReceiptClosure.evaluate(
            self._expected_provider_calls.values(),
            [
                receipt
                for receipt in self.receipt_ledger.list_receipts()
                if receipt.call_id in expected_call_ids
            ],
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
        return {"status": "success", "content": dict(content)}

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
            ref_ids = list(dict.fromkeys([*token_ref_ids, *explicit_ref_ids]))
            if not token_ref_ids and explicit_ref_ids:
                token = f"[[cite_ref:{', '.join(explicit_ref_ids)}]]"
                text = f"{text} {token}"
                ref_ids = self._token_refs(text)
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
            token = f"[[cite_ref:{', '.join(ref_ids)}]]"
            token_start = text.find(token)
            token_end = token_start + len(token) if token_start >= 0 else None
            citations = [
                {
                    "local_ref_id": f"s{section_number}_b{order}_cite_{index}",
                    "citation_token": token,
                    "ref_id": ref_id,
                    "raw_text": token,
                    "mode": "parenthetical",
                    "span_start": token_start if token_start >= 0 else None,
                    "span_end": token_end,
                }
                for index, ref_id in enumerate(ref_ids, start=1)
            ]
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
        for token in __import__("re").findall(r"\[\[cite_ref:[^\]]+\]\]", text):
            refs.extend(extract_ref_ids_from_token(token))
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
            attempt_id=self.attempt_id,
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
            admission = runtime.admit(estimated_tokens=max(1, len(prompt) // 4))
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

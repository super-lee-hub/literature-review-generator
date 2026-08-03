from __future__ import annotations

"""Production Stage 1 reader execution.

The runtime used to treat Stage 1 as a summary-file import.  This service owns
the missing boundary: PDF/Zotero work items are preprocessed, a traceable
text/visual input is built, the configured reader is called under the provider
runtime, and only a substantive canonical summary is returned to the runtime
checkpoint.
"""

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence, cast

from preprocess.service import PreprocessManager
from preprocess.visual_artifacts import Stage1VisualArtifactBuilder
from models import APIConfig
from runtime.provider_runtime import (
    ProviderBudgetV1,
    ProviderBudgetExceeded,
    ProviderRuntime,
    ProviderRuntimeLedger,
)
from runtime.stage_contracts import PaperWorkItem, SourceBundle
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry, file_sha256
from services.job_workspace import JobWorkspace
from services.settings import ApplicationSettings
from services.stage1_input_builder import Stage1InputBuilder
from services.stage1_input_completeness import build_completeness_metrics, has_blocking_stage1_reason
from summary_schema import is_canonical_ai_summary, normalize_ai_summary


ReaderCallable = Callable[..., Mapping[str, Any]]

_PLACEHOLDER_RE = re.compile(
    r"(?:^|\b)(?:dummy|placeholder|not\s+provided|not\s+available|unknown|n/?a|\.\.\.)"
    r"(?:$|\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Stage1AnalysisResult:
    summaries: tuple[dict[str, Any], ...]
    source_items: tuple[dict[str, Any], ...]
    receipt_ids: tuple[str, ...]
    receipt_ledger_path: str
    reused_count: int
    generated_count: int


class Stage1AnalysisService:
    """Generate or resume canonical Stage 1 summaries for a source bundle."""

    def __init__(
        self,
        *,
        job_id: str,
        attempt_id: str,
        workspace: JobWorkspace,
        artifact_registry: ArtifactRegistry,
        config: Mapping[str, Any],
        settings: ApplicationSettings,
        cancellation_checker: Callable[[], None] | None = None,
        reader: ReaderCallable | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.job_id = str(job_id)
        self.attempt_id = str(attempt_id or "stage1")
        self.workspace = workspace
        self.registry = artifact_registry
        self.config = {
            str(section): dict(values) if isinstance(values, Mapping) else values
            for section, values in config.items()
        }
        self.settings = settings
        self.cancellation_checker = cancellation_checker
        self.reader = reader
        self.logger = logger or logging.getLogger("auto_generate.stage1")
        self.receipt_ledger = ProviderRuntimeLedger(
            self.workspace.artifact_path("provider_receipts.jsonl")
        )

    def run(
        self,
        bundle: SourceBundle,
        *,
        existing_summaries: Sequence[Mapping[str, Any]] = (),
    ) -> Stage1AnalysisResult:
        bundle.validate()
        existing = self._index_existing(existing_summaries, bundle)
        summaries: list[dict[str, Any]] = []
        source_items: list[dict[str, Any]] = []
        reused_count = 0
        generated_count = 0

        for item in bundle.paper_work_items:
            self._check_cancelled()
            paper_key = self._paper_key(item)
            previous = existing.get(paper_key)
            if previous is not None:
                summaries.append(dict(previous))
                source_items.append(
                    {
                        "canonical_paper_key": paper_key,
                        "source_paper_id": item.source_paper_id,
                        "source_pdf": item.source_pdf,
                        "disposition": "reused",
                        "provider_receipt_ids": [],
                    }
                )
                reused_count += 1
                continue

            summary, receipt_ids = self._generate_one(item)
            summaries.append(summary)
            source_items.append(
                {
                    "canonical_paper_key": paper_key,
                    "source_paper_id": item.source_paper_id,
                    "source_pdf": item.source_pdf,
                    "disposition": "provider_generated",
                    "provider_receipt_ids": list(receipt_ids),
                }
            )
            generated_count += 1

        if len(summaries) != len(bundle.paper_work_items):
            raise RuntimeError("Stage 1 did not produce one result for every source work item")

        self._register_receipt_ledger()
        return Stage1AnalysisResult(
            summaries=tuple(summaries),
            source_items=tuple(source_items),
            receipt_ids=tuple(receipt.receipt_id for receipt in self.receipt_ledger.list_receipts()),
            receipt_ledger_path=str(self.receipt_ledger.path),
            reused_count=reused_count,
            generated_count=generated_count,
        )

    def _generate_one(self, item: PaperWorkItem) -> tuple[dict[str, Any], tuple[str, ...]]:
        source_pdf = str(item.source_pdf or "").strip()
        if not source_pdf or not Path(source_pdf).is_file():
            raise RuntimeError(
                f"Stage 1 source PDF is missing for {self._paper_key(item)}: {source_pdf or '<empty>'}"
            )

        preprocess = self._preprocess(source_pdf)
        preprocess_metadata = self._preprocess_metadata(preprocess)
        visual_bundle = self._build_visual_bundle(item, preprocess_metadata)
        stage1_settings = dict(self.settings.section("Stage1_Input"))
        if not stage1_settings:
            stage1_settings = {
                "send_extracted_text": "true",
                "send_selected_visuals": "true",
                "send_original_pdf": "never",
            }
        if str(self.settings.section("Multimodal").get("enabled") or "").strip().lower() in {
            "false",
            "0",
            "no",
        }:
            stage1_settings["send_selected_visuals"] = "false"

        primary_config = dict(self.settings.section("Primary_Reader_API"))
        built_input = Stage1InputBuilder(logger=self.logger).build(
            prompt_template=self._prompt_template(),
            paper_text=preprocess.stage1_input_text,
            reader_api_config=primary_config,
            visual_bundle=visual_bundle,
            pdf_path=source_pdf,
            stage1_input_settings=stage1_settings,
            preprocess_metadata=preprocess_metadata,
        )

        runtime = ProviderRuntime(
            budget=ProviderBudgetV1(
                max_calls=max(2, self.settings.runtime.node_retry_limit + 2),
                max_retries_per_call=self.settings.runtime.node_retry_limit,
            ),
            ledger=self.receipt_ledger,
            job_id=self.job_id,
            attempt_id=self.attempt_id,
            stage_name="stage1_analyze",
            route="Primary_Reader_API",
            node_id=self._paper_key(item),
            schema_hash=self._schema_hash(),
        )
        provider_result = self._call_reader(
            item=item,
            built_input=built_input,
            primary_config=primary_config,
            backup_config=dict(self.settings.section("Backup_Reader_API")),
            runtime=runtime,
        )
        self._ensure_receipt(
            runtime,
            prompt=built_input.prompt_text,
            input_payload=built_input.to_metadata_dict(),
            api_config=primary_config,
            result=provider_result,
        )
        ai_summary = self._canonical_substantive_summary(provider_result)
        return (
            {
                "status": "success",
                "paper_info": {
                    **dict(item.paper_info),
                    "canonical_paper_key": item.canonical_paper_key,
                    "source_paper_id": item.source_paper_id,
                    "source_pdf": source_pdf,
                    "source_mode": item.source_mode,
                },
                "source_mode": item.source_mode,
                "text_length": len(preprocess.stage1_input_text),
                "processing_time": "",
                "ai_summary": ai_summary,
                "preprocess": preprocess_metadata,
                "stage1_input": built_input.to_metadata_dict(),
                "provider": {
                    "route": runtime.route,
                    "model": str(primary_config.get("model") or ""),
                    "receipt_ids": [receipt.receipt_id for receipt in runtime.receipts],
                    "receipt_ledger_path": str(self.receipt_ledger.path),
                },
            },
            tuple(receipt.receipt_id for receipt in runtime.receipts),
        )

    def _preprocess(self, source_pdf: str) -> Any:
        preprocess_config = {
            str(section): dict(values) if isinstance(values, Mapping) else values
            for section, values in self.config.items()
        }
        preprocess_section = dict(preprocess_config.get("Preprocess") or {})
        preprocess_section.setdefault(
            "cache_dir", self.workspace.artifact_path("preprocess_cache")
        )
        preprocess_config["Preprocess"] = preprocess_section
        result = PreprocessManager(preprocess_config, logger=self.logger).prepare_pdf(source_pdf)
        if result is None:
            raise RuntimeError(f"Stage 1 preprocessing failed or was disabled for {source_pdf}")
        reasons = list(getattr(result, "stage1_quality_reasons", []) or [])
        if has_blocking_stage1_reason(reasons):
            raise RuntimeError(
                f"Stage 1 preprocessing is incomplete for {source_pdf}: {', '.join(reasons)}"
            )
        if not str(result.stage1_input_text or "").strip():
            fallback_text = str(result.plain_text or result.markdown_text or "").strip()
            if fallback_text:
                result = replace(
                    result,
                    stage1_input_text=fallback_text,
                    selected_text_source="plain_text_fallback",
                    stage1_quality_level="fallback",
                )
        if not str(result.stage1_input_text or "").strip():
            raise RuntimeError(f"Stage 1 preprocessing produced empty input for {source_pdf}")
        return result

    def _preprocess_metadata(self, result: Any) -> dict[str, Any]:
        metadata = asdict(result)
        metadata["page_diagnostics"] = [
            dict(item) if isinstance(item, Mapping) else item
            for item in metadata.get("page_diagnostics", [])
        ]
        metadata["selected_text_length"] = len(str(result.stage1_input_text or ""))
        metadata["stage1_page_count"] = len(list(result.page_index or []))
        metadata["stage1_quality_reasons"] = list(
            metadata.get("stage1_quality_reasons")
            or metadata.get("stage1_quality", {}).get("reasons", [])
            or []
        )
        metadata["stage1_completeness_metrics"] = build_completeness_metrics(
            text=str(result.stage1_input_text or ""),
            page_count=len(list(result.page_index or [])),
            selected_text_length=len(str(result.stage1_input_text or "")),
            chunk_count=int(result.chunk_count or 0),
        )
        return metadata

    def _build_visual_bundle(
        self,
        item: PaperWorkItem,
        preprocess_metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        visual_settings = dict(self.settings.section("Stage1_Visual"))
        enabled = str(visual_settings.get("enabled", "true")).strip().lower() not in {
            "false",
            "0",
            "no",
        }
        if not enabled:
            return {}
        digest = hashlib.sha256(self._paper_key(item).encode("utf-8")).hexdigest()[:24]
        output_dir = self.workspace.artifact_path(f"stage1_visuals/{digest}")
        bundle = Stage1VisualArtifactBuilder(logger=self.logger).build_bundle(
            job_id=self.job_id,
            paper_key=self._paper_key(item),
            paper_info=item.paper_info,
            source_pdf=str(item.source_pdf),
            output_dir=output_dir,
            artifact_registry=self.registry,
            preprocess_metadata=preprocess_metadata,
        )
        return bundle.to_dict() if bundle is not None else {}

    def _call_reader(
        self,
        *,
        item: PaperWorkItem,
        built_input: Any,
        primary_config: Mapping[str, Any],
        backup_config: Mapping[str, Any],
        runtime: ProviderRuntime,
    ) -> Mapping[str, Any]:
        if self.reader is not None:
            value = self.reader(
                prompt_text=built_input.prompt_text,
                primary_api_config=dict(primary_config),
                backup_api_config=dict(backup_config),
                user_content=built_input.user_message_content,
                provider_runtime=runtime,
                paper_info=dict(item.paper_info),
            )
        else:
            from ai_interface import get_summary_from_ai_with_fallback

            value = get_summary_from_ai_with_fallback(
                built_input.prompt_text,
                cast(APIConfig, dict(primary_config)),
                cast(APIConfig, dict(backup_config)),
                logger=self.logger,
                config=self.config,
                user_content=built_input.user_message_content,
                return_detailed=True,
                provider_runtime=runtime,
            )
        if not isinstance(value, Mapping):
            raise RuntimeError(f"Stage 1 reader returned a non-object for {self._paper_key(item)}")
        return dict(value)

    @staticmethod
    def _canonical_substantive_summary(provider_result: Mapping[str, Any]) -> dict[str, Any]:
        if str(provider_result.get("status") or "").strip().lower() != "success":
            error_kind = str(provider_result.get("error_kind") or "provider_failure")
            message = str(provider_result.get("message") or "Stage 1 reader failed")
            raise RuntimeError(f"Stage 1 reader failed ({error_kind}): {message}")
        content = provider_result.get("content")
        if not isinstance(content, Mapping):
            content = provider_result.get("ai_summary")
        normalized = normalize_ai_summary(content)
        if not is_canonical_ai_summary(normalized):
            raise RuntimeError("Stage 1 reader did not return the canonical summary schema")
        core = normalized.get("core_analysis")
        if not isinstance(core, Mapping):
            raise RuntimeError("Stage 1 reader returned no canonical core analysis")
        required = ("summary", "methodology", "findings", "conclusions")
        missing = [field for field in required if not str(core.get(field) or "").strip()]
        if missing:
            raise RuntimeError(
                "Stage 1 reader returned an incomplete canonical summary: " + ", ".join(missing)
            )
        placeholders = [
            field
            for field in required
            if _PLACEHOLDER_RE.search(str(core.get(field) or ""))
        ]
        if placeholders:
            raise RuntimeError(
                "Stage 1 reader returned placeholder content in: " + ", ".join(placeholders)
            )
        return dict(normalized)

    def _ensure_receipt(
        self,
        runtime: ProviderRuntime,
        *,
        prompt: str,
        input_payload: Mapping[str, Any],
        api_config: Mapping[str, Any],
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
                api_config=api_config,
                result=result,
                metadata={"execution_mode": "injected_reader"},
            )
        except ProviderBudgetExceeded:
            runtime.blocked_receipt(
                prompt=prompt,
                input_payload=input_payload,
                api_config=api_config,
                message="Stage 1 reader did not produce a provider receipt before its budget closed",
            )

    def _register_receipt_ledger(self) -> None:
        if not self.receipt_ledger.path.is_file():
            return
        self.registry.register_file(
            artifact_role="provider_receipts",
            artifact_type="provider_receipt_ledger",
            artifact_version="v1",
            path=str(self.receipt_ledger.path),
            producer="services.stage1_analysis_service.Stage1AnalysisService",
            artifact_id="provider_receipts",
            metadata={"receipt_count": len(self.receipt_ledger.list_receipts())},
        )

    def _index_existing(
        self,
        summaries: Sequence[Mapping[str, Any]],
        bundle: SourceBundle,
    ) -> dict[str, dict[str, Any]]:
        expected = {item.canonical_paper_key for item in bundle.paper_work_items}
        indexed: dict[str, dict[str, Any]] = {}
        for summary in summaries:
            if not isinstance(summary, Mapping):
                raise RuntimeError("existing Stage 1 summary is not an object")
            paper = summary.get("paper_info")
            if not isinstance(paper, Mapping):
                raise RuntimeError("existing Stage 1 summary has no paper_info")
            key = str(paper.get("canonical_paper_key") or "").strip()
            if key not in expected or key in indexed:
                raise RuntimeError(f"existing Stage 1 summary has unknown or duplicate identity: {key}")
            if str(summary.get("status") or "").strip().lower() != "success":
                continue
            # The bridge performs the strict canonical-schema validation after
            # this service returns.  Reject obvious non-canonical payloads here
            # so a partial resume cannot silently preserve a placeholder.
            self._canonical_substantive_summary({"status": "success", "content": summary.get("ai_summary")})
            indexed[key] = dict(summary)
        return indexed

    @staticmethod
    def _paper_key(item: PaperWorkItem) -> str:
        return str(item.canonical_paper_key or item.source_paper_id or "").strip()

    def _check_cancelled(self) -> None:
        if self.cancellation_checker is not None:
            self.cancellation_checker()

    @staticmethod
    def _prompt_template() -> str:
        path = Path(__file__).resolve().parents[1] / "prompts" / "optimized_prompt_analyze.txt"
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return "Analyze the following paper and return canonical JSON:\n\n{{PAPER_FULL_TEXT}}"

    @staticmethod
    def _schema_hash() -> str:
        payload = {"schema": "summary_v2_lite", "stage": "stage1_analyze"}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


__all__ = ["Stage1AnalysisResult", "Stage1AnalysisService"]

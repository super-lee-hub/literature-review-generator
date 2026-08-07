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
    _redact_mapping,
    compute_closure_epoch_id,
    hash_json,
    hash_text,
)
from runtime.provider_receipt_closure import ExpectedProviderCall, ProviderReceiptClosure
from runtime.stage_contracts import PaperWorkItem, SourceBundle
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    RegistryError,
    file_sha256,
)
from services.evidence_manifest import build_evidence_manifest_v1
from services.job_workspace import (
    JobWorkspace,
    atomic_write_json,
    publish_bytes_artifact,
    publish_json_artifact,
    utc_now_iso,
)
from services.settings import ApplicationSettings
from services.stage1_input_builder import Stage1InputBuilder
from services.stage1_input_completeness import build_completeness_metrics, has_blocking_stage1_reason
from services.stage1_reuse import (
    STAGE1_REUSE_POLICY,
    Stage1ReusableSummaryBindingV1,
    Stage1ReusableSummaryManifestV1,
    Stage1ReuseEligibilityV1,
    build_binding_hash,
    evaluate_stage1_reuse,
    verify_stage1_typed_manifest_authority,
)
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
    expected_call_graph_path: str = ""
    expected_call_graph_hash: str = ""
    closure_epoch_id: str = ""
    reuse_evidence_ids: tuple[str, ...] = ()
    expected_provider_transport_count: int = 0
    actual_provider_transport_count: int = 0


@dataclass(frozen=True)
class _PreparedStage1Item:
    item: PaperWorkItem
    previous: dict[str, Any] | None
    preprocess_metadata: dict[str, Any]
    built_input: Any
    primary_config: dict[str, Any]
    backup_config: dict[str, Any]
    reuse_eligibility: Stage1ReuseEligibilityV1 | None = None
    current_binding: Stage1ReusableSummaryBindingV1 = Stage1ReusableSummaryBindingV1()


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
        publication_context: Any | None = None,
        external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
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
        self.external_registry_resolver = external_registry_resolver
        from services.queue_service import LocalPublicationContext

        self.publication_context = (
            publication_context
            or getattr(artifact_registry, "publication_context", None)
            or LocalPublicationContext()
        )
        self.logger = logger or logging.getLogger("auto_generate.stage1")
        safe_attempt = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.attempt_id or "stage1")
        self.receipt_ledger_target_path = self.workspace.artifact_path(
            "stage1_provider_receipts.jsonl"
        )
        self.receipt_ledger = ProviderRuntimeLedger(
            self.workspace.artifact_path(
                f".publication-staging/provider-receipts/stage1/{safe_attempt}.jsonl"
            )
        )
        self.receipt_ledger_path = ""
        self.expected_calls: tuple[ExpectedProviderCall, ...] = ()
        self.expected_call_graph_hash = ""
        self.closure_epoch_id = ""
        self.expected_call_graph_path = ""
        self.receipt_closure_path = ""
        self.receipt_closure_hash = ""
        self.reuse_evidence_ids: list[str] = []
        self._generated_authorities: dict[
            str, tuple[ArtifactRecord, Stage1ReusableSummaryBindingV1, Mapping[str, Any]]
        ] = {}
        self._final_source_manifests: dict[str, ArtifactRecord] = {}

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

        # Preprocess and bind every work item before the first provider call.
        # The expected graph is therefore independent of whichever item or
        # retry happens to execute first.
        prepared = [
            self._prepare_item(item, existing.get(self._paper_key(item)))
            for item in bundle.paper_work_items
        ]
        self._predeclare_expected_calls(bundle, prepared)
        prepared = [
            replace(item, current_binding=self._bind_execution_provenance(item.current_binding))
            for item in prepared
        ]
        for item in prepared:
            self._check_cancelled()
            summary, receipt_ids = self._execute_prepared(item)
            summaries.append(summary)
            paper_key = self._paper_key(item.item)
            preprocess = summary.get("preprocess") if isinstance(summary, Mapping) else None
            preprocess = preprocess if isinstance(preprocess, Mapping) else item.preprocess_metadata
            source_items.append(
                {
                    "canonical_paper_key": paper_key,
                    "source_paper_id": item.item.source_paper_id,
                    "source_pdf": item.item.source_pdf,
                    "disposition": "reused" if item.previous is not None else "provider_generated",
                    "provider_receipt_ids": list(receipt_ids),
                    "reuse_evidence_id": str(
                        (summary.get("reuse_metadata") or {}).get("reuse_evidence_id") or ""
                    )
                    if isinstance(summary, Mapping)
                    else "",
                    "evidence_manifest_path": str(preprocess.get("evidence_manifest_path") or ""),
                    "evidence_manifest_hash": str(preprocess.get("evidence_manifest_hash") or ""),
                }
            )
            if item.previous is not None:
                reused_count += 1
            else:
                generated_count += 1

        if len(summaries) != len(bundle.paper_work_items):
            raise RuntimeError("Stage 1 did not produce one result for every source work item")

        self._register_receipt_ledger()
        # Finalize the provider authority before returning summaries.  This
        # lets generated summaries carry a typed reusable manifest that binds
        # the original closure/ledger; the bridge may call the finalizer again
        # after paper artifacts are persisted, but it is idempotent.
        closure_record = self.finalize_provider_receipt_closure()
        self._finalize_generated_source_manifests(closure_record)
        for summary in summaries:
            paper = summary.get("paper_info") if isinstance(summary, Mapping) else None
            paper_key = str(paper.get("canonical_paper_key") or "") if isinstance(paper, Mapping) else ""
            finalized = self._generated_authorities.get(paper_key)
            if finalized is None:
                continue
            _authority_record, finalized_binding, _paper_info = finalized
            reuse_metadata = summary.get("stage1_reuse")
            if isinstance(reuse_metadata, Mapping):
                summary["stage1_reuse"] = {
                    **dict(reuse_metadata),
                    "binding": finalized_binding.to_dict(),
                    "source_artifact_id": _authority_record.artifact_id,
                    "source_artifact_hash": _authority_record.content_hash,
                }
        # The provider path is optional evidence.  A zero-transport reuse run
        # must not inherit a historical ledger path or manufacture a target
        # that was never published.
        for summary in summaries:
            provider = summary.get("provider") if isinstance(summary, Mapping) else None
            if isinstance(provider, Mapping):
                summary["provider"] = {
                    **dict(provider),
                    "receipt_ledger_path": self.receipt_ledger_path,
                }
        current_epoch_receipts = tuple(
            receipt
            for receipt in self.receipt_ledger.list_receipts()
            if str(receipt.closure_epoch_id or "") == self.closure_epoch_id
        )
        actual_transport_count = len(current_epoch_receipts)
        return Stage1AnalysisResult(
            summaries=tuple(summaries),
            source_items=tuple(source_items),
            receipt_ids=tuple(receipt.receipt_id for receipt in current_epoch_receipts),
            receipt_ledger_path=self.receipt_ledger_path,
            reused_count=reused_count,
            generated_count=generated_count,
            expected_call_graph_path=self.expected_call_graph_path,
            expected_call_graph_hash=self.expected_call_graph_hash,
            closure_epoch_id=self.closure_epoch_id,
            reuse_evidence_ids=tuple(self.reuse_evidence_ids),
            expected_provider_transport_count=len(self.expected_calls),
            actual_provider_transport_count=actual_transport_count,
        )

    def prepare_empty_provider_receipt_closure(self, bundle: SourceBundle) -> None:
        """Persist an explicit zero-call graph for summary-source Stage 1 runs."""

        bundle.validate()
        if bundle.paper_work_items:
            raise ValueError("empty Stage 1 closure is only valid without source work items")
        self._predeclare_expected_calls(bundle, ())
        self._register_receipt_ledger()

    def _prepare_item(
        self,
        item: PaperWorkItem,
        previous: dict[str, Any] | None,
    ) -> _PreparedStage1Item:
        source_pdf = str(item.source_pdf or "").strip()
        if not source_pdf or not Path(source_pdf).is_file():
            raise RuntimeError(
                f"Stage 1 source PDF is missing for {self._paper_key(item)}: {source_pdf or '<empty>'}"
            )
        preprocess = self._preprocess(source_pdf)
        preprocess_metadata = self._preprocess_metadata(preprocess)
        evidence_manifest = build_evidence_manifest_v1(
            job_id=self.job_id,
            canonical_paper_key=item.canonical_paper_key,
            preprocess=preprocess_metadata,
        )
        existing_evidence = self.registry.get(
            f"evidence_manifest:{item.canonical_paper_key}"
        )
        if existing_evidence is not None and existing_evidence.status == "ready":
            try:
                existing_payload = json.loads(
                    Path(existing_evidence.path).read_text(encoding="utf-8")
                )
                if (
                    isinstance(existing_payload, Mapping)
                    and str(existing_payload.get("created_at") or "")
                    and hash_json(existing_payload.get("artifacts") or [])
                    == hash_json(
                        [item.to_dict() for item in evidence_manifest.artifacts]
                    )
                ):
                    evidence_manifest = replace(
                        evidence_manifest,
                        created_at=str(existing_payload["created_at"]),
                    )
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                pass
        evidence_manifest_path = self.workspace.artifact_path(
            "evidence_manifests/"
            f"{hashlib.sha256(item.canonical_paper_key.encode('utf-8')).hexdigest()[:24]}_v1.json"
        )
        evidence_record = publish_json_artifact(
            self.publication_context,
            self.registry,
            evidence_manifest_path,
            evidence_manifest.to_dict(),
            artifact_role="evidence_manifest",
            artifact_type="evidence_manifest",
            artifact_version="v1",
            producer="services.stage1_analysis_service.Stage1AnalysisService",
            artifact_id=f"evidence_manifest:{item.canonical_paper_key}",
        )
        preprocess_metadata["evidence_manifest_path"] = evidence_record.path
        preprocess_metadata["evidence_manifest_hash"] = evidence_record.content_hash
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
        current_binding = self._build_current_binding(
            item=item,
            preprocess_metadata=preprocess_metadata,
            built_input=built_input,
            primary_config=primary_config,
            stage1_input_settings=stage1_settings,
            evidence_record=evidence_record,
            visual_bundle=visual_bundle,
        )
        reuse_eligibility = (
            evaluate_stage1_reuse(
                previous,
                current_binding,
                registry=self.registry,
                external_registry_resolver=self.external_registry_resolver,
            )
            if previous is not None
            else None
        )
        return _PreparedStage1Item(
            item=item,
            previous=previous if reuse_eligibility is None or reuse_eligibility.reusable else None,
            preprocess_metadata=preprocess_metadata,
            built_input=built_input,
            primary_config=primary_config,
            backup_config=dict(self.settings.section("Backup_Reader_API")),
            reuse_eligibility=reuse_eligibility,
            current_binding=current_binding,
        )

    def _build_current_binding(
        self,
        *,
        item: PaperWorkItem,
        preprocess_metadata: Mapping[str, Any],
        built_input: Any,
        primary_config: Mapping[str, Any],
        stage1_input_settings: Mapping[str, Any],
        evidence_record: ArtifactRecord,
        visual_bundle: Mapping[str, Any],
    ) -> Stage1ReusableSummaryBindingV1:
        evidence_files = {}
        for field_name in (
            "markdown_path",
            "plain_text_path",
            "page_index_path",
            "chunks_path",
            "stage1_input_path",
            "stage1_input_manifest_path",
            "stage1_quality_report_path",
        ):
            path = str(preprocess_metadata.get(field_name) or "").strip()
            if path:
                evidence_files[field_name] = file_sha256(path) if Path(path).is_file() else ""
        semantic_source_hash = hash_json(
            {
                "stage1_input_text": hash_text(
                    str(preprocess_metadata.get("stage1_input_text") or "")
                ),
                "page_index": preprocess_metadata.get("page_index") or [],
                "page_count": int(preprocess_metadata.get("stage1_page_count") or 0),
            }
        )
        preprocess_hash = hash_json(
            {
                "policy": self._preprocess_policy_fingerprint(),
                "stage1_input_text_hash": hash_text(
                    str(preprocess_metadata.get("stage1_input_text") or "")
                ),
                "selected_text_source": str(preprocess_metadata.get("selected_text_source") or ""),
                "quality_level": str(preprocess_metadata.get("stage1_quality_level") or ""),
                "quality_reasons": list(preprocess_metadata.get("stage1_quality_reasons") or []),
                "page_count": int(preprocess_metadata.get("stage1_page_count") or 0),
                "chunk_count": int(preprocess_metadata.get("chunk_count") or 0),
                "evidence_files": {
                    key: value
                    for key, value in evidence_files.items()
                    if key in {"markdown_path", "plain_text_path", "page_index_path", "chunks_path"}
                },
            }
        )
        source_pdf_content_sha256 = file_sha256(item.source_pdf)
        stage1_extracted_text_hash = hash_text(
            str(preprocess_metadata.get("stage1_input_text") or "")
        )
        prompt_template_hash = hash_text(self._prompt_template())
        visual_input_manifest_hash = hash_json(dict(visual_bundle or {}))
        input_builder_policy_hash = hash_json(
            {
                "builder_version": "Stage1InputBuilder:v1",
                "stage1_input_policy": _redact_mapping(stage1_input_settings),
                "input_mode": str(built_input.input_mode or ""),
                "selected_visual_refs": list(built_input.selected_visual_refs or []),
                "visual_selection_policy_snapshot": dict(
                    built_input.visual_selection_policy_snapshot or {}
                ),
                "multimodal_capability": dict(built_input.multimodal_capability or {}),
                "pdf_attachment_status": str(built_input.pdf_attachment_status or ""),
            }
        )
        runtime = self.registry.get("runtime_job_spec")
        return Stage1ReusableSummaryBindingV1(
            canonical_paper_key=str(item.canonical_paper_key or ""),
            source_paper_id=str(item.source_paper_id or ""),
            source_mode=str(item.source_mode or ""),
            source_pdf=str(item.source_pdf or ""),
            # Legacy names remain populated for older readers, but both now
            # carry the real PDF byte identity.  Semantic/preprocess identity
            # lives only in the explicit Stage 1 fields below.
            source_pdf_hash=source_pdf_content_sha256,
            source_pdf_fingerprint=source_pdf_content_sha256,
            source_pdf_content_sha256=source_pdf_content_sha256,
            stage1_extracted_text_hash=stage1_extracted_text_hash,
            stage1_semantic_input_hash=semantic_source_hash,
            preprocess_contract_hash=preprocess_hash,
            prompt_template_hash=prompt_template_hash,
            input_builder_policy_hash=input_builder_policy_hash,
            summary_schema_hash=self._schema_hash(),
            visual_input_manifest_hash=visual_input_manifest_hash,
            original_source_location=str(item.source_pdf or ""),
            current_source_location=str(item.source_pdf or ""),
            preprocess_hash=preprocess_hash,
            stage1_input_hash=hash_json(
                {
                    "source_text_hash": hash_text(
                        str(preprocess_metadata.get("stage1_input_text") or "")
                    ),
                    "input_mode": str(built_input.input_mode or ""),
                    "selected_visual_refs": list(built_input.selected_visual_refs or []),
                    "visual_selection_policy_snapshot": dict(
                        built_input.visual_selection_policy_snapshot or {}
                    ),
                    "multimodal_capability": dict(built_input.multimodal_capability or {}),
                    "pdf_attachment_status": str(built_input.pdf_attachment_status or ""),
                }
            ),
            prompt_hash=hash_json(
                {
                    "prompt_template_hash": prompt_template_hash,
                    "source_text_hash": hash_text(
                        str(preprocess_metadata.get("stage1_input_text") or "")
                    ),
                    "visual_provenance_hash": hash_json(dict(visual_bundle or {})),
                }
            ),
            builder_version="Stage1InputBuilder:v1",
            provider=str(
                primary_config.get("provider")
                or primary_config.get("provider_name")
                or primary_config.get("name")
                or "Primary_Reader_API"
            ),
            model=str(primary_config.get("model") or ""),
            endpoint_type=str(primary_config.get("endpoint_type") or "chat_completions"),
            provider_config_hash=hash_json(_redact_mapping(primary_config)),
            schema_hash=self._schema_hash(),
            visual_provenance_hash=hash_json(dict(visual_bundle or {})),
            source_kind="stage1_provider_generated",
            evidence_manifest_id=evidence_record.artifact_id,
            evidence_manifest_hash=evidence_record.content_hash,
            current_evidence_manifest_id=evidence_record.artifact_id,
            current_evidence_manifest_hash=evidence_record.content_hash,
            runtime_spec_id=runtime.artifact_id if runtime is not None else "",
            runtime_spec_hash=runtime.content_hash if runtime is not None else "",
            current_runtime_spec_id=runtime.artifact_id if runtime is not None else "",
            current_runtime_spec_hash=runtime.content_hash if runtime is not None else "",
            extra={
                "evidence_file_hashes": evidence_files,
                "source_pdf_file_hash": source_pdf_content_sha256,
                "source_kind": "stage1_provider_generated",
                "provider_transport_count": 1,
            },
        )

    def _preprocess_policy_fingerprint(self) -> Mapping[str, Any]:
        """Return the semantic preprocessing policy without machine-local paths.

        Cache/output locations and timeout knobs affect execution logistics but
        do not define the extracted Stage 1 evidence contract.  Excluding them
        keeps reuse portable across workspaces while still binding all policy
        settings that can change extraction semantics.
        """

        section = self.config.get("Preprocess")
        raw = dict(section) if isinstance(section, Mapping) else {}

        def keep(key: str) -> bool:
            lowered = key.lower()
            return not any(token in lowered for token in ("path", "dir", "timeout", "cache"))

        return _redact_mapping({key: value for key, value in raw.items() if keep(str(key))})

    def _bind_execution_provenance(
        self,
        binding: Stage1ReusableSummaryBindingV1,
    ) -> Stage1ReusableSummaryBindingV1:
        runtime = self.registry.get("runtime_job_spec")
        graph = self.registry.get("stage1:provider_expected_call_graph")
        return replace(
            binding,
            runtime_spec_id=runtime.artifact_id if runtime is not None else binding.runtime_spec_id,
            runtime_spec_hash=runtime.content_hash if runtime is not None else binding.runtime_spec_hash,
            current_runtime_spec_id=runtime.artifact_id if runtime is not None else binding.current_runtime_spec_id,
            current_runtime_spec_hash=runtime.content_hash if runtime is not None else binding.current_runtime_spec_hash,
            expected_call_graph_id=graph.artifact_id if graph is not None else "",
            expected_call_graph_hash=graph.content_hash if graph is not None else self.expected_call_graph_hash,
        )

    def _publish_generated_source_artifact(
        self,
        prepared: _PreparedStage1Item,
        *,
        paper_info: Mapping[str, Any],
        ai_summary: Mapping[str, Any],
    ) -> tuple[ArtifactRecord, Stage1ReusableSummaryBindingV1]:
        summary_payload_hash = hash_json(ai_summary)
        provisional = replace(
            prepared.current_binding,
            normalized_summary_payload_hash=summary_payload_hash,
            summary_payload_hash=summary_payload_hash,
        )
        payload = {
            "artifact_type": "summary_file",
            "artifact_version": "v1",
            "source_kind": "stage1_provider_generated",
            "job_id": self.job_id,
            "status": "success",
            "paper_info": dict(paper_info),
            "ai_summary": dict(ai_summary),
            "summary_payload_hash": summary_payload_hash,
            "normalized_summary_payload_hash": summary_payload_hash,
            "binding": provisional.to_dict(),
        }
        # A registered summary_file is a canonical summary collection even
        # when it is used as the one-paper authority for later reuse.  Keep
        # the provenance envelope on the summary item while preserving the
        # array contract enforced by runtime.reconcile.
        source_payload = [payload]
        digest = hash_json(source_payload)
        path = self.workspace.artifact_path(f"stage1/reuse_sources/generated_{digest[:24]}.json")
        dependencies = []
        for artifact_id in (
            "source_bundle",
            "runtime_job_spec",
            f"evidence_manifest:{prepared.item.canonical_paper_key}",
        ):
            record = self.registry.get(artifact_id)
            if record is not None and record.status == "ready":
                dependencies.append(ArtifactDependencyRefV2.from_record(record))
        record = publish_json_artifact(
            self.publication_context,
            self.registry,
            path,
            source_payload,
            artifact_role="summary_source",
            artifact_type="summary_file",
            artifact_version="v1",
            producer="services.stage1_analysis_service.Stage1AnalysisService",
            artifact_id=f"stage1:summary_source:{digest[:24]}",
            depends_on=dependencies,
            metadata={
                "immutable": True,
                "summary_payload_hash": summary_payload_hash,
                "source_paper_key": prepared.item.canonical_paper_key,
                "reuse_policy": STAGE1_REUSE_POLICY,
            },
        )
        # The reusable manifest is published only after the provider closure
        # and receipt ledger exist.  Publishing a provider-generated manifest
        # before that point would create an apparently typed but incomplete
        # authority artifact.
        generated_binding = replace(
            provisional,
            registered_source_artifact_id=record.artifact_id,
            registered_source_artifact_hash=record.content_hash,
            registered_source_artifact_path=record.path,
            registry_file_hash=file_sha256(record.path),
            source_authority_job_id=self.job_id,
            source_authority_artifact_id=record.artifact_id,
            source_authority_artifact_hash=record.content_hash,
            source_authority_artifact_path=record.path,
            source_authority_registry_id=f"artifact-registry:{self.job_id}",
            source_authority_registry_revision=str(self.registry.revision),
            source_authority_registry_path=str(self.registry.registry_path),
            source_summary_manifest_id="",
            source_summary_manifest_hash="",
        )
        self._generated_authorities[prepared.item.canonical_paper_key] = (
            record,
            generated_binding,
            dict(paper_info),
        )
        return record, generated_binding

    def _finalize_generated_source_manifests(
        self,
        closure_record: ArtifactRecord,
    ) -> None:
        """Publish provider-bound reusable manifests after closure finalization."""

        ledger_record = self.registry.get("stage1_provider_receipts")
        source_bundle_record = self.registry.get("source_bundle")
        runtime_record = self.registry.get("runtime_job_spec")
        for paper_key, (source_record, binding, paper_info) in list(
            self._generated_authorities.items()
        ):
            if paper_key in self._final_source_manifests:
                continue
            closure_payload = replace(
                binding,
                source_provider_receipt_closure_id=closure_record.artifact_id,
                source_provider_receipt_closure_hash=closure_record.content_hash,
                source_authority_closure_id=closure_record.artifact_id,
                source_authority_closure_hash=closure_record.content_hash,
                source_provider_receipt_ledger_id=(
                    ledger_record.artifact_id if ledger_record is not None else ""
                ),
                source_provider_receipt_ledger_hash=(
                    ledger_record.content_hash if ledger_record is not None else ""
                ),
            )
            binding_hash = build_binding_hash(closure_payload.to_dict())
            source_payload = json.loads(
                Path(source_record.path).read_text(encoding="utf-8")
            )[0].get("ai_summary", {})
            manifest = Stage1ReusableSummaryManifestV1(
                job_id=self.job_id,
                stage_name="stage1_analyze",
                canonical_paper_key=paper_key,
                source_paper_id=str(closure_payload.source_paper_id or ""),
                source_summary_artifact_id=source_record.artifact_id,
                source_summary_artifact_hash=source_record.content_hash,
                source_summary_artifact_path=source_record.path,
                source_summary_artifact_version=source_record.artifact_version,
                summary_payload_hash=closure_payload.summary_payload_hash,
                normalized_summary_payload_hash=closure_payload.normalized_summary_payload_hash,
                binding_hash=binding_hash,
                source_pdf_content_sha256=closure_payload.source_pdf_content_sha256,
                stage1_extracted_text_hash=closure_payload.stage1_extracted_text_hash,
                stage1_semantic_input_hash=closure_payload.stage1_semantic_input_hash,
                preprocess_contract_hash=closure_payload.preprocess_contract_hash,
                prompt_template_hash=closure_payload.prompt_template_hash,
                input_builder_policy_hash=closure_payload.input_builder_policy_hash,
                summary_schema_hash=closure_payload.summary_schema_hash,
                visual_input_manifest_hash=closure_payload.visual_input_manifest_hash,
                provider=closure_payload.provider,
                model=closure_payload.model,
                endpoint_type=closure_payload.endpoint_type,
                provider_config_hash=closure_payload.provider_config_hash,
                summary_schema_version=str(
                    source_payload.get("schema_version")
                    if isinstance(source_payload, Mapping)
                    else ""
                ),
                provider_receipt_closure_id=closure_record.artifact_id,
                provider_receipt_closure_hash=closure_record.content_hash,
                provider_receipt_closure_path=closure_record.path,
                provider_receipt_ledger_id=(
                    ledger_record.artifact_id if ledger_record is not None else ""
                ),
                provider_receipt_ledger_hash=(
                    ledger_record.content_hash if ledger_record is not None else ""
                ),
                provider_receipt_ledger_path=(
                    ledger_record.path if ledger_record is not None else ""
                ),
                source_registry_identity=closure_payload.source_authority_registry_id,
                source_registry_revision=closure_payload.source_authority_registry_revision,
                source_kind="stage1_provider_generated",
                binding=closure_payload.to_dict(),
                paper_info=dict(paper_info),
                summary_payload=dict(source_payload),
                runtime_spec_id=runtime_record.artifact_id if runtime_record else "",
                runtime_spec_hash=runtime_record.content_hash if runtime_record else "",
                evidence_manifest_id=closure_payload.evidence_manifest_id,
                evidence_manifest_hash=closure_payload.evidence_manifest_hash,
                source_bundle_id=source_bundle_record.artifact_id if source_bundle_record else "",
                source_bundle_hash=source_bundle_record.content_hash if source_bundle_record else "",
                created_at=utc_now_iso(),
            )
            manifest_base = manifest.to_dict()
            manifest_base["manifest_content_hash"] = ""
            manifest_content_hash = hash_json(manifest_base)
            manifest_payload = {**manifest.to_dict(), "manifest_content_hash": manifest_content_hash}
            manifest_digest = hash_json(manifest_payload)
            dependencies: list[ArtifactDependencyRefV2] = []
            seen_ids: set[str] = set()
            for dependency_record in (
                source_record,
                source_bundle_record,
                runtime_record,
                self.registry.get(closure_record.artifact_id),
                ledger_record,
                self.registry.get(closure_payload.evidence_manifest_id),
            ):
                if dependency_record is None or dependency_record.status != "ready":
                    continue
                if dependency_record.artifact_id in seen_ids:
                    continue
                seen_ids.add(dependency_record.artifact_id)
                dependencies.append(ArtifactDependencyRefV2.from_record(dependency_record))
            manifest_record = publish_json_artifact(
                self.publication_context,
                self.registry,
                self.workspace.artifact_path(
                    f"stage1/reuse_sources/final_manifest_{manifest_digest[:24]}.json"
                ),
                manifest_payload,
                artifact_role="stage1_summary_manifest",
                artifact_type="stage1_reusable_summary_manifest",
                artifact_version="v1",
                producer="services.stage1_analysis_service.Stage1AnalysisService",
                artifact_id=f"stage1:summary_manifest:final:{manifest_digest[:24]}",
                depends_on=dependencies,
                metadata={
                    "immutable": True,
                    "authority": True,
                    "source_summary_artifact_id": source_record.artifact_id,
                    "source_summary_artifact_hash": source_record.content_hash,
                    "provider_receipt_closure_id": closure_record.artifact_id,
                    "provider_receipt_ledger_id": ledger_record.artifact_id if ledger_record else "",
                },
            )
            finalized_binding = replace(
                closure_payload,
                source_summary_manifest_id=manifest_record.artifact_id,
                source_summary_manifest_hash=manifest_record.content_hash,
            )
            self._generated_authorities[paper_key] = (
                source_record,
                finalized_binding,
                paper_info,
            )
            self._final_source_manifests[paper_key] = manifest_record

    def _ensure_durable_input_records(self, bundle: SourceBundle) -> tuple[str, str]:
        source_record = self.registry.get("source_bundle")
        if source_record is None:
            source_path = self.workspace.artifact_path("source_bundle_v1.json")
            source_record = publish_json_artifact(
                self.publication_context,
                self.registry,
                source_path,
                bundle.to_dict(),
                artifact_role="source_bundle",
                artifact_type="source_bundle",
                artifact_version="v1",
                producer="services.stage1_analysis_service.Stage1AnalysisService",
                artifact_id="source_bundle",
            )
        runtime_record = self.registry.get("runtime_job_spec")
        if runtime_record is None:
            spec_path = self.workspace.artifact_path("stage1_execution_spec_v1.json")
            runtime_record = publish_json_artifact(
                self.publication_context,
                self.registry,
                spec_path,
                {
                    "artifact_type": "runtime_job_spec",
                    "artifact_version": "v1",
                    "job_id": self.job_id,
                    "stage_name": "stage1_analyze",
                    "attempt_id": self.attempt_id,
                },
                artifact_role="runtime_spec",
                artifact_type="runtime_job_spec",
                artifact_version="v1",
                producer="services.stage1_analysis_service.Stage1AnalysisService",
                artifact_id="runtime_job_spec",
            )
        return source_record.content_hash, runtime_record.content_hash

    def _predeclare_expected_calls(
        self,
        bundle: SourceBundle,
        prepared: Sequence[_PreparedStage1Item],
    ) -> None:
        source_bundle_hash, runtime_spec_hash = self._ensure_durable_input_records(bundle)
        # Exact summary reuse is evidence, not provider work.  The expected
        # graph contains only items that can genuinely produce a transport
        # receipt in this epoch.
        graph_seed = [
            {
                "call_id": f"stage1:{self._paper_key(item.item)}",
                "job_id": self.job_id,
                "attempt_id": self.attempt_id,
                "stage_name": "stage1_analyze",
                "node_id": self._paper_key(item.item),
                "logical_attempt_identity": self.attempt_id,
                "prompt_hash": hash_text(item.built_input.prompt_text),
                "input_hash": hash_json(item.built_input.to_metadata_dict()),
                # ProviderRuntime hashes the redacted transport config.  The
                # durable expected graph must use the same hash domain so a
                # successful receipt cannot become stale merely because the
                # graph was declared before the provider call.
                "config_hash": hash_json(_redact_mapping(item.primary_config)),
                "schema_hash": self._schema_hash(),
                "artifact_path": self._paper_artifact_path(item.item),
                "max_attempts": max(1, self.settings.runtime.node_retry_limit + 1),
                "usage_required": False,
            }
            for item in prepared
            if item.previous is None
        ]
        graph_hash = hash_json({
            "job_id": self.job_id,
            "stage_name": "stage1_analyze",
            "attempt_id": self.attempt_id,
            "source_bundle_hash": source_bundle_hash,
            "runtime_spec_hash": runtime_spec_hash,
            "expected_calls": graph_seed,
        })
        config_hash = hash_json({
            "primary_reader": [item.primary_config for item in prepared],
            "stage": "stage1_analyze",
        })
        epoch = compute_closure_epoch_id(
            job_id=self.job_id,
            stage_name="stage1_analyze",
            logical_attempt_identity=self.attempt_id,
            expected_call_graph_hash=graph_hash,
            current_input_artifact_hashes={
                "source_bundle": source_bundle_hash,
                "runtime_spec": runtime_spec_hash,
            },
            provider_config_hash=config_hash,
            schema_version=self._schema_hash(),
        )
        self.expected_call_graph_hash = graph_hash
        self.closure_epoch_id = epoch
        self.expected_calls = tuple(
            ExpectedProviderCall(
                **item,
                closure_epoch_id=epoch,
                expected_call_graph_hash=graph_hash,
            )
            for item in graph_seed
        )
        self.expected_call_graph_path = self.workspace.artifact_path(
            "stage1/provider_expected_calls.json"
        )
        graph_record = publish_json_artifact(
            self.publication_context,
            self.registry,
            self.expected_call_graph_path,
            {
                "artifact_type": "provider_expected_call_graph",
                "artifact_version": "v1",
                "job_id": self.job_id,
                "stage_name": "stage1_analyze",
                "attempt_id": self.attempt_id,
                "closure_epoch_id": epoch,
                "expected_call_graph_hash": graph_hash,
                "source_bundle_hash": source_bundle_hash,
                "runtime_spec_hash": runtime_spec_hash,
                "expected_calls": [asdict(item) for item in self.expected_calls],
            },
            artifact_role="provider_expected_call_graph",
            artifact_type="provider_expected_call_graph",
            artifact_version="v1",
            producer="services.stage1_analysis_service.Stage1AnalysisService",
            artifact_id="stage1:provider_expected_call_graph",
            metadata={
                "closure_epoch_id": epoch,
                "expected_call_graph_hash": graph_hash,
                "source_bundle_hash": source_bundle_hash,
                "runtime_spec_hash": runtime_spec_hash,
                "expected_call_count": len(self.expected_calls),
                "reuse_excluded_from_expected_calls": True,
            },
        )
        self.expected_call_graph_path = graph_record.path

    def _execute_prepared(
        self,
        prepared: _PreparedStage1Item,
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        item = prepared.item
        runtime = ProviderRuntime(
            budget=ProviderBudgetV1(
                max_calls=max(2, self.settings.runtime.node_retry_limit + 2),
                max_retries_per_call=self.settings.runtime.node_retry_limit,
            ),
            ledger=self.receipt_ledger,
            job_id=self.job_id,
            attempt_id=self.attempt_id,
            stage_name="stage1_analyze",
            route="Stage1Reuse" if prepared.previous is not None else "Primary_Reader_API",
            node_id=self._paper_key(item),
            call_id=f"stage1:{self._paper_key(item)}",
            endpoint_type=str(prepared.primary_config.get("endpoint_type") or "chat_completions"),
            schema_hash=self._schema_hash(),
            closure_epoch_id=self.closure_epoch_id,
            logical_attempt_identity=self.attempt_id,
        )
        if prepared.previous is not None:
            summary = dict(prepared.previous)
            reuse_record = self._persist_reuse_evidence(prepared)
            self.reuse_evidence_ids.append(reuse_record.artifact_id)
            reuse_payload = json.loads(Path(reuse_record.path).read_text(encoding="utf-8"))
            prior_reuse_metadata = summary.get("stage1_reuse")
            prior_binding = Stage1ReusableSummaryBindingV1.from_mapping(
                prior_reuse_metadata.get("binding")
                if isinstance(prior_reuse_metadata, Mapping)
                else None
            )
            registered_id = str(
                reuse_payload.get("registered_source_artifact_id") or ""
            )
            registered_hash = str(
                reuse_payload.get("registered_source_artifact_hash") or ""
            )
            registered_path = str(
                reuse_payload.get("registered_source_artifact_path") or ""
            )
            authority_id = str(
                reuse_payload.get("source_authority_artifact_id")
                or registered_id
                or ""
            )
            authority_hash = str(
                reuse_payload.get("source_authority_artifact_hash")
                or registered_hash
                or ""
            )
            authority_path = str(
                reuse_payload.get("source_authority_artifact_path")
                or registered_path
                or ""
            )
            original_source_location = str(
                prior_binding.original_source_location
                or prior_binding.current_source_location
                or prior_binding.source_pdf
                or ""
            )
            current_source_location = str(
                prepared.current_binding.current_source_location
                or prepared.current_binding.source_pdf
                or ""
            )
            try:
                normalized_original_location = str(
                    Path(original_source_location).expanduser().resolve()
                ).casefold()
                normalized_current_location = str(
                    Path(current_source_location).expanduser().resolve()
                ).casefold()
            except (OSError, RuntimeError, ValueError):
                normalized_original_location = original_source_location.casefold()
                normalized_current_location = current_source_location.casefold()
            reuse_binding = replace(
                prepared.current_binding,
                original_source_location=original_source_location,
                current_source_location=current_source_location,
                location_changed=(
                    bool(normalized_original_location)
                    and bool(normalized_current_location)
                    and normalized_original_location != normalized_current_location
                ),
                summary_payload_hash=str(reuse_payload.get("summary_payload_hash") or ""),
                registered_source_artifact_id=registered_id,
                registered_source_artifact_hash=registered_hash,
                registered_source_artifact_path=registered_path,
                registry_file_hash=str(reuse_payload.get("registry_file_hash") or ""),
                source_summary_manifest_id=str(
                    reuse_payload.get("source_summary_manifest_id") or ""
                ),
                source_summary_manifest_hash=str(
                    reuse_payload.get("source_summary_manifest_hash") or ""
                ),
                source_provider_receipt_closure_id=str(
                    reuse_payload.get("source_provider_receipt_closure_id") or ""
                ),
                source_provider_receipt_closure_hash=str(
                    reuse_payload.get("source_provider_receipt_closure_hash") or ""
                ),
                source_provider_receipt_ledger_id=str(
                    reuse_payload.get("source_provider_receipt_ledger_id") or ""
                ),
                source_provider_receipt_ledger_hash=str(
                    reuse_payload.get("source_provider_receipt_ledger_hash") or ""
                ),
                source_authority_job_id=str(
                    reuse_payload.get("source_authority_job_id") or ""
                ),
                source_authority_artifact_id=authority_id,
                source_authority_artifact_hash=authority_hash,
                source_authority_artifact_path=authority_path,
                source_authority_registry_id=str(
                    reuse_payload.get("source_authority_registry_id") or ""
                ),
                source_authority_registry_revision=str(
                    reuse_payload.get("source_authority_registry_revision") or ""
                ),
                source_authority_closure_id=str(
                    reuse_payload.get("source_provider_receipt_closure_id") or ""
                ),
                source_authority_closure_hash=str(
                    reuse_payload.get("source_provider_receipt_closure_hash") or ""
                ),
                current_snapshot_artifact_id=str(
                    reuse_payload.get("current_snapshot_artifact_id") or ""
                ),
                current_snapshot_artifact_hash=str(
                    reuse_payload.get("current_snapshot_artifact_hash") or ""
                ),
                current_snapshot_artifact_path=str(
                    reuse_payload.get("current_snapshot_artifact_path") or ""
                ),
                source_authority_registry_path=str(
                    reuse_payload.get("source_authority_registry_path") or ""
                ),
                extra={
                    **dict(prepared.current_binding.extra),
                },
            )
            summary["provider"] = {
                **dict(summary.get("provider") or {}),
                "route": "Stage1Reuse",
                "receipt_ids": [],
                "receipt_ledger_path": self.receipt_ledger_path,
                "transport_count": 0,
                "reuse_evidence_id": reuse_record.artifact_id,
            }
            summary["reuse_metadata"] = {
                "reused": True,
                "reuse_evidence_id": reuse_record.artifact_id,
                "reason": "exact_summary_reuse",
            }
            summary["stage1_reuse"] = {
                "decision": "exact_summary_reuse",
                "reason": "registered_prior_binding_matches_current_source",
                "policy": STAGE1_REUSE_POLICY,
                "source_authority_kind": str(
                    reuse_payload.get("source_authority_kind") or ""
                ),
                "binding": reuse_binding.to_dict(),
            }
            return summary, ()

        provider_result = self._call_reader(
            item=item,
            built_input=prepared.built_input,
            primary_config=prepared.primary_config,
            backup_config=prepared.backup_config,
            runtime=runtime,
        )
        self._ensure_receipt(
            runtime,
            prompt=prepared.built_input.prompt_text,
            input_payload=prepared.built_input.to_metadata_dict(),
            api_config=prepared.primary_config,
            result=provider_result,
        )
        ai_summary = self._canonical_substantive_summary(provider_result)
        source_record, generated_binding = self._publish_generated_source_artifact(
            prepared,
            paper_info=item.paper_info,
            ai_summary=ai_summary,
        )
        eligibility = prepared.reuse_eligibility
        summary = {
            "status": "success",
            "paper_info": {
                **dict(item.paper_info),
                "canonical_paper_key": item.canonical_paper_key,
                "source_paper_id": item.source_paper_id,
                "source_pdf": str(item.source_pdf),
                "source_mode": item.source_mode,
            },
            "source_mode": item.source_mode,
            "text_length": int(prepared.preprocess_metadata.get("selected_text_length") or 0),
            "processing_time": "",
            "ai_summary": ai_summary,
            "preprocess": prepared.preprocess_metadata,
            "stage1_input": prepared.built_input.to_metadata_dict(),
            "provider": {
                "route": runtime.route,
                "model": str(prepared.primary_config.get("model") or ""),
                "receipt_ids": [receipt.receipt_id for receipt in runtime.receipts],
                "receipt_ledger_path": self.receipt_ledger_path,
            },
            "stage1_reuse": {
                "decision": (
                    eligibility.decision
                    if eligibility is not None
                    else "provider_generated"
                ),
                "reason": (
                    eligibility.reason
                    if eligibility is not None
                    else "no_prior_summary_supplied"
                ),
                "policy": STAGE1_REUSE_POLICY,
                "binding": generated_binding.to_dict(),
                "source_artifact_id": source_record.artifact_id,
                "source_artifact_hash": source_record.content_hash,
            },
        }
        return summary, tuple(receipt.receipt_id for receipt in runtime.receipts)

    def _resolve_source_record(
        self,
        *,
        job_id: str,
        artifact_id: str,
        registry_path: str = "",
    ) -> ArtifactRecord | None:
        if not artifact_id:
            return None
        target = self.registry
        if job_id and job_id != self.job_id:
            # A path carried by imported JSON is only a locator for an
            # already-authorized resolver.  Constructing a Registry directly
            # from that path would let a summary choose its own authority.
            target = (
                self.external_registry_resolver(job_id)
                if self.external_registry_resolver is not None
                else None
            )
            if target is None:
                return None
            target.reload()
        record = target.get(artifact_id)
        return record if record is not None and record.status == "ready" else None

    def _publish_portable_authority_record(
        self,
        *,
        source_path: str,
        expected_hash: str,
        portable_kind: str,
        source_authority_job_id: str,
        original_artifact_id: str,
        dependencies: Sequence[ArtifactDependencyRefV2] = (),
        typed_manifest_artifact_id: str = "",
        typed_manifest_artifact_hash: str = "",
    ) -> ArtifactRecord:
        """Copy already-verified authority bytes into the current Registry."""

        source = Path(source_path).expanduser().resolve()
        actual_hash = file_sha256(source)
        if not expected_hash or actual_hash != expected_hash:
            raise RuntimeError(
                f"typed Stage 1 {portable_kind} authority bytes changed before publication"
            )
        suffix = ".jsonl" if source.suffix.casefold() == ".jsonl" else ".json"
        manifest_scope = str(typed_manifest_artifact_hash or "")[:16]
        if not manifest_scope:
            raise RuntimeError(
                f"typed Stage 1 {portable_kind} authority has no manifest scope"
            )
        manifest_scoped = portable_kind in {"provider_closure", "provider_ledger"}
        artifact_id = (
            f"stage1:portable_{portable_kind}:{manifest_scope}:{actual_hash[:24]}"
            if manifest_scoped
            else f"stage1:portable_{portable_kind}:{actual_hash[:24]}"
        )
        portable_name = (
            f"{portable_kind}_{manifest_scope}_{actual_hash[:24]}{suffix}"
            if manifest_scoped
            else f"{portable_kind}_{actual_hash[:24]}{suffix}"
        )
        return publish_bytes_artifact(
            self.publication_context,
            self.registry,
            self.workspace.artifact_path(
                f"stage1/portable_authority/{portable_name}"
            ),
            source.read_bytes(),
            artifact_role="stage1_portable_authority",
            artifact_type=f"stage1_portable_{portable_kind}",
            artifact_version="v1",
            producer="services.stage1_analysis_service.Stage1AnalysisService",
            artifact_id=artifact_id,
            depends_on=dependencies,
            metadata={
                "immutable": True,
                "authority_kind": "typed_manifest",
                "stage_name": "stage1_analyze",
                "source_authority_job_id": source_authority_job_id,
                "original_artifact_id": original_artifact_id,
                "original_artifact_hash": actual_hash,
                "typed_manifest_artifact_id": typed_manifest_artifact_id,
                "typed_manifest_artifact_hash": typed_manifest_artifact_hash,
            },
        )

    def _persist_reuse_evidence(self, prepared: _PreparedStage1Item) -> ArtifactRecord:
        """Persist typed evidence for a zero-transport exact summary reuse."""

        previous = dict(prepared.previous or {})
        summary_payload = previous.get("ai_summary")
        if not isinstance(summary_payload, Mapping):
            raise RuntimeError("reused Stage 1 summary has no canonical summary payload")
        summary_payload_hash = hash_json(summary_payload)
        source_paper_id = str(prepared.item.source_paper_id or "").strip()
        prior_reuse_metadata = prepared.previous.get("stage1_reuse") if isinstance(prepared.previous, Mapping) else None
        prior_binding = Stage1ReusableSummaryBindingV1.from_mapping(
            prior_reuse_metadata.get("binding")
            if isinstance(prior_reuse_metadata, Mapping)
            else None
        )
        runtime_record = self.registry.get("runtime_job_spec")
        if runtime_record is None or runtime_record.status != "ready":
            raise RuntimeError("reused Stage 1 summary requires a registered runtime_job_spec")
        runtime_dependency = ArtifactDependencyRefV2.from_record(runtime_record)
        typed_authority, typed_authority_reason = verify_stage1_typed_manifest_authority(
            previous,
            prior_binding,
        )
        typed_authority_requested = (
            isinstance(prior_reuse_metadata, Mapping)
            and str(prior_reuse_metadata.get("authority_kind") or "").strip()
            == "typed_manifest"
        )
        if typed_authority_requested and typed_authority is None:
            raise RuntimeError(
                f"typed Stage 1 reuse authority is no longer verified: {typed_authority_reason}"
            )

        # Only the imported parent binding can select the authority.  A
        # current-run snapshot or a legacy registered_source field is derived
        # evidence and must never be promoted to authority.
        source_authority_job_id = str(prior_binding.source_authority_job_id or "").strip()
        source_authority_artifact_id = str(
            prior_binding.source_authority_artifact_id or ""
        ).strip()
        if not source_authority_job_id or not source_authority_artifact_id:
            raise RuntimeError(
                "reused Stage 1 summary requires an explicit parent source authority"
            )
        source_authority_artifact_hash = str(
            prior_binding.source_authority_artifact_hash or ""
        ).strip()
        if not source_authority_artifact_hash:
            raise RuntimeError("reused Stage 1 summary source authority hash is missing")
        source_authority_registry_path = ""
        source_authority_record: ArtifactRecord | None = None
        source_authority_manifest: ArtifactRecord | None = None
        source_closure: ArtifactRecord | None = None
        source_ledger: ArtifactRecord | None = None
        portable_source_record: ArtifactRecord | None = None
        portable_manifest_record: ArtifactRecord | None = None
        portable_closure_record: ArtifactRecord | None = None
        portable_ledger_record: ArtifactRecord | None = None
        authority_kind = "parent_registry"

        if typed_authority is not None:
            authority_kind = "typed_manifest"
            manifest = typed_authority.manifest
            if (
                manifest.job_id != source_authority_job_id
                or manifest.source_summary_artifact_id != source_authority_artifact_id
                or manifest.source_summary_artifact_hash != source_authority_artifact_hash
            ):
                raise RuntimeError("typed Stage 1 manifest authority identity changed")
            portable_source_record = self._publish_portable_authority_record(
                source_path=typed_authority.source_summary_path,
                expected_hash=manifest.source_summary_artifact_hash,
                portable_kind="summary_source",
                source_authority_job_id=manifest.job_id,
                original_artifact_id=manifest.source_summary_artifact_id,
                dependencies=(runtime_dependency,),
                typed_manifest_artifact_id=typed_authority.manifest_artifact_id,
                typed_manifest_artifact_hash=typed_authority.manifest_file_hash,
            )
            if typed_authority.provider_ledger_path:
                portable_ledger_record = self._publish_portable_authority_record(
                    source_path=typed_authority.provider_ledger_path,
                    expected_hash=manifest.provider_receipt_ledger_hash,
                    portable_kind="provider_ledger",
                    source_authority_job_id=manifest.job_id,
                    original_artifact_id=manifest.provider_receipt_ledger_id,
                    dependencies=(runtime_dependency,),
                    typed_manifest_artifact_id=typed_authority.manifest_artifact_id,
                    typed_manifest_artifact_hash=typed_authority.manifest_file_hash,
                )
            if typed_authority.provider_closure_path:
                closure_dependencies = [runtime_dependency]
                if portable_ledger_record is not None:
                    closure_dependencies.append(
                        ArtifactDependencyRefV2.from_record(portable_ledger_record)
                    )
                portable_closure_record = self._publish_portable_authority_record(
                    source_path=typed_authority.provider_closure_path,
                    expected_hash=manifest.provider_receipt_closure_hash,
                    portable_kind="provider_closure",
                    source_authority_job_id=manifest.job_id,
                    original_artifact_id=manifest.provider_receipt_closure_id,
                    dependencies=tuple(closure_dependencies),
                    typed_manifest_artifact_id=typed_authority.manifest_artifact_id,
                    typed_manifest_artifact_hash=typed_authority.manifest_file_hash,
                )
            manifest_dependencies = [
                runtime_dependency,
                ArtifactDependencyRefV2.from_record(portable_source_record),
            ]
            for portable_record in (
                portable_closure_record,
                portable_ledger_record,
            ):
                if portable_record is not None:
                    manifest_dependencies.append(
                        ArtifactDependencyRefV2.from_record(portable_record)
                    )
            portable_manifest_record = self._publish_portable_authority_record(
                source_path=typed_authority.manifest_path,
                expected_hash=typed_authority.manifest_file_hash,
                portable_kind="summary_manifest",
                source_authority_job_id=manifest.job_id,
                original_artifact_id=typed_authority.manifest_artifact_id,
                dependencies=tuple(manifest_dependencies),
                typed_manifest_artifact_id=typed_authority.manifest_artifact_id,
                typed_manifest_artifact_hash=typed_authority.manifest_file_hash,
            )
            source_authority_record = portable_source_record
            source_authority_manifest = portable_manifest_record
            source_closure = portable_closure_record
            source_ledger = portable_ledger_record
            source_authority_artifact_path = typed_authority.source_summary_path
            source_authority_file_hash = portable_source_record.content_hash
        else:
            source_authority_registry_path = str(
                prior_binding.source_authority_registry_path
                or (
                    self.registry.registry_path
                    if source_authority_job_id == self.job_id
                    else ""
                )
            )
            source_authority_record = self._resolve_source_record(
                job_id=source_authority_job_id,
                artifact_id=source_authority_artifact_id,
                registry_path=source_authority_registry_path,
            )
            if source_authority_record is None:
                raise RuntimeError(
                    "reused Stage 1 summary requires a registered source authority"
                )
            if source_authority_artifact_hash != source_authority_record.content_hash:
                raise RuntimeError("reused Stage 1 summary source authority hash changed")
            source_authority_artifact_path = source_authority_record.path
            if (
                source_authority_record.job_id == self.job_id
                and not source_authority_registry_path
            ):
                source_authority_registry_path = str(self.registry.registry_path)
            source_authority_file_hash = file_sha256(source_authority_record.path)
            if (
                not source_authority_file_hash
                or source_authority_file_hash != source_authority_record.content_hash
            ):
                raise RuntimeError(
                    "reused Stage 1 summary source authority bytes are not verified"
                )
            if (
                prior_binding.registry_file_hash
                and prior_binding.registry_file_hash != source_authority_file_hash
            ):
                raise RuntimeError(
                    "reused Stage 1 summary source authority file hash changed"
                )
            source_authority_manifest = self._resolve_source_record(
                job_id=source_authority_job_id,
                artifact_id=prior_binding.source_summary_manifest_id,
                registry_path=source_authority_registry_path,
            )
            if source_authority_manifest is None:
                raise RuntimeError(
                    "reused Stage 1 summary requires a registered source summary manifest"
                )
            if (
                source_authority_manifest.artifact_type
                != "stage1_reusable_summary_manifest"
                or source_authority_manifest.artifact_version != "v1"
            ):
                raise RuntimeError(
                    "reused Stage 1 summary source manifest has the wrong type"
                )
            if (
                prior_binding.source_summary_manifest_hash
                != source_authority_manifest.content_hash
            ):
                raise RuntimeError(
                    "reused Stage 1 summary source manifest hash changed"
                )
        provider_payload = previous.get("provider")
        source_receipt_ids = list(
            provider_payload.get("receipt_ids", [])
            if isinstance(provider_payload, Mapping)
            else []
        )

        # Snapshot the exact input bytes before the current run can rewrite a
        # mutable summary/paper projection.  The snapshot is itself a real
        # Registry file, so a logical payload hash or synthetic ID can never
        # satisfy reuse closure by itself.
        source_snapshot_payload = [previous]
        source_snapshot_digest = hash_json(source_snapshot_payload)
        source_snapshot = publish_json_artifact(
            self.publication_context,
            self.registry,
            self.workspace.artifact_path(
                f"stage1/reuse_sources/summary_{source_snapshot_digest[:24]}.json"
            ),
            source_snapshot_payload,
            artifact_role="summary_source",
            artifact_type="summary_file",
            artifact_version="v1",
            producer="services.stage1_analysis_service.Stage1AnalysisService",
            artifact_id=f"stage1:reuse_source_summary:{source_snapshot_digest[:24]}",
            depends_on=(ArtifactDependencyRefV2.from_record(runtime_record),),
            metadata={
                "immutable": True,
                "summary_payload_hash": summary_payload_hash,
                "source_paper_key": self._paper_key(prepared.item),
            },
        )
        ArtifactRegistry._verify_ready_artifact(source_snapshot)

        source_manifest = self.registry.get("summary_source_manifest")
        source_manifest_payload: Mapping[str, Any] | None = None
        if source_manifest is not None and source_manifest.status == "ready":
            try:
                raw_manifest = json.loads(Path(source_manifest.path).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("registered summary source manifest is unreadable") from exc
            if isinstance(raw_manifest, Mapping):
                source_manifest_payload = raw_manifest
        if source_manifest_payload is None:
            source_manifest_payload = {
                "artifact_type": "summary_source_manifest",
                "artifact_version": "v2",
                "created_at": utc_now_iso(),
                "project_name": self.job_id,
                "source_kind": "stage1_reuse_source_snapshot",
                "source_path": source_snapshot.path,
                "source_items": [
                    {
                        "canonical_paper_key": self._paper_key(prepared.item),
                        "source_paper_id": source_paper_id,
                        "source_path": source_snapshot.path,
                        "disposition": "reused",
                    }
                ],
                "rejected_candidates": [],
                "materialized_summary_file": source_snapshot.path,
                "summary_count": 1,
            }
        source_manifest_digest = hash_json(source_manifest_payload)
        source_manifest_snapshot = publish_json_artifact(
            self.publication_context,
            self.registry,
            self.workspace.artifact_path(
                f"stage1/reuse_sources/manifest_{source_manifest_digest[:24]}.json"
            ),
            source_manifest_payload,
            artifact_role="summary_source",
            artifact_type="summary_source_manifest",
            artifact_version="v2",
            producer="services.stage1_analysis_service.Stage1AnalysisService",
            artifact_id=f"stage1:reuse_source_manifest:{source_manifest_digest[:24]}",
            depends_on=(
                ArtifactDependencyRefV2.from_record(source_snapshot),
                ArtifactDependencyRefV2.from_record(runtime_record),
            ),
            metadata={"immutable": True, "source_snapshot": True},
        )

        evidence_manifest = self.registry.get(
            f"evidence_manifest:{self._paper_key(prepared.item)}"
        )
        if evidence_manifest is None or evidence_manifest.status != "ready":
            raise RuntimeError("reused Stage 1 summary requires a registered current evidence manifest")

        if typed_authority is None:
            source_ledger = self._resolve_source_record(
                job_id=source_authority_job_id,
                artifact_id=prior_binding.source_provider_receipt_ledger_id,
                registry_path=source_authority_registry_path,
            )
            source_closure = self._resolve_source_record(
                job_id=source_authority_job_id,
                artifact_id=prior_binding.source_provider_receipt_closure_id,
                registry_path=source_authority_registry_path,
            )
        source_kind = str(
            prior_binding.source_kind
            or (
                prior_binding.extra.get("source_kind")
                if isinstance(prior_binding.extra, Mapping)
                else ""
            )
            or ""
        ).strip()
        raw_transport_count = (
            prior_binding.extra.get("provider_transport_count")
            if isinstance(prior_binding.extra, Mapping)
            else None
        )
        if raw_transport_count in (None, "") and isinstance(provider_payload, Mapping):
            raw_transport_count = provider_payload.get("transport_count")
        try:
            source_transport_count = int(raw_transport_count or len(source_receipt_ids) or 0)
        except (TypeError, ValueError):
            source_transport_count = len(source_receipt_ids)
        provider_generated = source_kind in {
            "stage1_provider_generated",
            "provider_generated",
            "runtime_stage1",
        } or source_transport_count > 0
        if provider_generated and source_closure is None:
            raise RuntimeError("reused Stage 1 summary requires its original provider closure")
        if provider_generated and source_ledger is None:
            raise RuntimeError("reused Stage 1 summary requires its original provider ledger")
        expected_closure_type = (
            "stage1_portable_provider_closure"
            if typed_authority is not None
            else "provider_receipt_closure"
        )
        expected_ledger_type = (
            "stage1_portable_provider_ledger"
            if typed_authority is not None
            else "provider_receipt_ledger"
        )
        if source_closure is not None and (
            source_closure.artifact_type != expected_closure_type
            or source_closure.artifact_version != "v1"
            or file_sha256(source_closure.path) != source_closure.content_hash
            or prior_binding.source_provider_receipt_closure_hash
            != source_closure.content_hash
        ):
            raise RuntimeError(
                "reused Stage 1 summary original provider closure is not verified"
            )
        if source_ledger is not None and (
            source_ledger.artifact_type != expected_ledger_type
            or source_ledger.artifact_version != "v1"
            or file_sha256(source_ledger.path) != source_ledger.content_hash
            or prior_binding.source_provider_receipt_ledger_hash
            != source_ledger.content_hash
        ):
            raise RuntimeError(
                "reused Stage 1 summary original provider ledger is not verified"
            )
        if source_authority_record is None or source_authority_manifest is None:
            raise RuntimeError("reused Stage 1 summary authority persistence is incomplete")

        source_summary_manifest_id = source_authority_manifest.artifact_id
        source_summary_manifest_hash = source_authority_manifest.content_hash
        source_provider_closure_id = str(
            prior_binding.source_provider_receipt_closure_id or ""
        )
        source_provider_closure_hash = str(
            prior_binding.source_provider_receipt_closure_hash or ""
        )
        source_provider_ledger_id = str(
            prior_binding.source_provider_receipt_ledger_id or ""
        )
        source_provider_ledger_hash = str(
            prior_binding.source_provider_receipt_ledger_hash or ""
        )
        if typed_authority is None:
            source_provider_closure_id = source_closure.artifact_id if source_closure else ""
            source_provider_closure_hash = source_closure.content_hash if source_closure else ""
            source_provider_ledger_id = source_ledger.artifact_id if source_ledger else ""
            source_provider_ledger_hash = source_ledger.content_hash if source_ledger else ""

        evidence = {
            "artifact_type": "stage1_summary_reuse_record",
            "artifact_version": "v1",
            "job_id": self.job_id,
            "stage_name": "stage1_analyze",
            "attempt_id": self.attempt_id,
            "reused_summary_artifact_id": source_authority_artifact_id,
            "reused_summary_artifact_hash": source_authority_artifact_hash,
            "summary_payload_hash": summary_payload_hash,
            "normalized_summary_payload_hash": summary_payload_hash,
            # The current-run snapshot is a derived copy.  Keep the legacy
            # registered_source fields empty so it cannot be mistaken for the
            # parent authority.
            "registered_source_artifact_id": "",
            "registered_source_artifact_hash": "",
            "registered_source_artifact_path": "",
            "registry_file_hash": source_authority_file_hash,
            "current_snapshot_artifact_id": source_snapshot.artifact_id,
            "current_snapshot_artifact_hash": source_snapshot.content_hash,
            "current_snapshot_artifact_path": source_snapshot.path,
            "current_snapshot_derived_from_external_authority": bool(
                source_authority_job_id != self.job_id
                and authority_kind in {"parent_registry", "typed_manifest"}
            ),
            "source_kind": source_kind,
            "source_authority_kind": authority_kind,
            "source_authority_job_id": source_authority_job_id,
            "source_authority_artifact_id": source_authority_artifact_id,
            "source_authority_artifact_hash": source_authority_artifact_hash,
            "source_authority_artifact_path": source_authority_artifact_path,
            "source_authority_registry_id": prior_binding.source_authority_registry_id,
            "source_authority_registry_revision": prior_binding.source_authority_registry_revision,
            "source_authority_registry_path": source_authority_registry_path,
            "source_summary_manifest_id": source_summary_manifest_id,
            "source_summary_manifest_hash": source_summary_manifest_hash,
            "source_paper_artifact_id": "",
            "source_paper_artifact_hash": "",
            "source_provider_receipt_closure_id": source_provider_closure_id,
            "source_provider_receipt_closure_hash": source_provider_closure_hash,
            "source_provider_receipt_ledger_id": source_provider_ledger_id,
            "source_provider_receipt_ledger_hash": source_provider_ledger_hash,
            "typed_manifest_artifact_id": (
                typed_authority.manifest_artifact_id if typed_authority else ""
            ),
            "typed_manifest_artifact_hash": (
                typed_authority.manifest_file_hash if typed_authority else ""
            ),
            "typed_manifest_content_hash": (
                typed_authority.manifest.manifest_content_hash
                if typed_authority
                else ""
            ),
            "portable_source_artifact_id": (
                portable_source_record.artifact_id if portable_source_record else ""
            ),
            "portable_source_artifact_hash": (
                portable_source_record.content_hash if portable_source_record else ""
            ),
            "portable_source_summary_manifest_id": (
                portable_manifest_record.artifact_id if portable_manifest_record else ""
            ),
            "portable_source_summary_manifest_hash": (
                portable_manifest_record.content_hash if portable_manifest_record else ""
            ),
            "portable_source_provider_receipt_closure_id": (
                portable_closure_record.artifact_id if portable_closure_record else ""
            ),
            "portable_source_provider_receipt_closure_hash": (
                portable_closure_record.content_hash if portable_closure_record else ""
            ),
            "portable_source_provider_receipt_ledger_id": (
                portable_ledger_record.artifact_id if portable_ledger_record else ""
            ),
            "portable_source_provider_receipt_ledger_hash": (
                portable_ledger_record.content_hash if portable_ledger_record else ""
            ),
            "source_bundle_paper_identity": {
                "canonical_paper_key": self._paper_key(prepared.item),
                "source_paper_id": source_paper_id,
                "source_pdf": str(prepared.item.source_pdf),
            },
            "input_manifest_hashes": {
                "evidence_manifest": str(
                    prepared.preprocess_metadata.get("evidence_manifest_hash") or ""
                ),
                "stage1_input": hash_json(prepared.built_input.to_metadata_dict()),
            },
            "original_provider_receipt_ids": source_receipt_ids,
            "current_evidence_manifest_id": evidence_manifest.artifact_id,
            "current_evidence_manifest_hash": evidence_manifest.content_hash,
            "current_runtime_spec_id": runtime_record.artifact_id,
            "current_runtime_spec_hash": runtime_record.content_hash,
            "reuse_policy": "exact_summary_reuse_v1",
            "reuse_decision_reason": "exact_summary_reuse",
            "created_at": utc_now_iso(),
        }
        evidence["content_hash"] = hash_json(evidence)
        digest = str(evidence["content_hash"])[:24]
        path = self.workspace.artifact_path(f"stage1/reuse_records/{digest}.json")
        dependencies: list[ArtifactDependencyRefV2] = []
        for dependency_record in (
            source_snapshot,
            source_manifest_snapshot,
            runtime_record,
            evidence_manifest,
            source_closure,
            source_ledger,
            source_authority_record,
            source_authority_manifest,
        ):
            if dependency_record is None or dependency_record.status != "ready":
                continue
            dependency_kind = (
                "external_job"
                if dependency_record.job_id != self.job_id
                else "local_job"
            )
            dependencies.append(
                ArtifactDependencyRefV2.from_record(
                    dependency_record,
                    dependency_kind=dependency_kind,
                )
            )
        return publish_json_artifact(
            self.publication_context,
            self.registry,
            path,
            evidence,
            artifact_role="stage1_summary_reuse_record",
            artifact_type="stage1_summary_reuse_record",
            artifact_version="v1",
            producer="services.stage1_analysis_service.Stage1AnalysisService",
            artifact_id=f"stage1:summary_reuse:{digest}",
            depends_on=dependencies,
            external_registry_resolver=self.external_registry_resolver,
            metadata={
                "reuse_policy": "exact_summary_reuse_v1",
                "transport_count": 0,
                "reused_summary_artifact_hash": source_authority_artifact_hash,
                "summary_payload_hash": summary_payload_hash,
                "current_snapshot_artifact_id": source_snapshot.artifact_id,
                "source_authority_artifact_id": source_authority_artifact_id,
                "source_authority_kind": authority_kind,
            },
        )

    def _paper_artifact_id(self, item: PaperWorkItem) -> str:
        digest = hashlib.sha256(self._paper_key(item).encode("utf-8")).hexdigest()[:24]
        return f"paper:{digest}"

    def _paper_artifact_path(self, item: PaperWorkItem) -> str:
        return self.workspace.artifact_path(
            f"paper_artifacts/{self._paper_artifact_id(item).replace(':', '_')}.json"
        )

    def finalize_provider_receipt_closure(self) -> ArtifactRecord:
        """Bind receipts to a stable generated source or paper artifact.

        Stage1 is finalized once before the bridge persists paper projections
        and once after it.  The generated source artifact is already a
        durable, typed output at the first call, so it is the stable binding
        for provider closure.  Paper artifacts remain downstream projections
        validated by the stage terminal record.
        """

        from dataclasses import replace as dataclass_replace

        receipts = tuple(
            receipt
            for receipt in self.receipt_ledger.list_receipts()
            if str(receipt.closure_epoch_id or "") == self.closure_epoch_id
        )
        expected_call_ids = tuple(sorted(
            str(item.call_id)
            for item in self.expected_calls
            if str(item.call_id or "")
        ))
        by_call = {
            call_id: max(
                (receipt for receipt in receipts if receipt.call_id == call_id),
                key=lambda receipt: (receipt.attempts, receipt.sequence, receipt.finished_at),
                default=None,
            )
            for call_id in (item.call_id for item in self.expected_calls)
        }
        bound: list[ExpectedProviderCall] = []
        paper_ids: list[str] = []
        stable_source_ids: list[str] = []
        all_records = self.registry.list_records()
        for expected in self.expected_calls:
            receipt = by_call.get(expected.call_id)
            if receipt is None:
                bound.append(expected)
                continue

            tracked = self._generated_authorities.get(str(expected.node_id))
            source_record = tracked[0] if tracked is not None else None
            paper_record = next(
                (
                    record
                    for record in all_records
                    if record.artifact_type == "paper_artifact"
                    and (
                        record.artifact_id
                        == f"paper:{hashlib.sha256(str(expected.node_id).encode('utf-8')).hexdigest()[:24]}"
                        or Path(record.path).resolve() == Path(expected.artifact_path).resolve()
                    )
                ),
                None,
            )
            output_record = source_record or paper_record
            if source_record is not None:
                stable_source_ids.append(source_record.artifact_id)
            if paper_record is not None and source_record is None:
                paper_ids.append(paper_record.artifact_id)

            payload_hash = ""
            if output_record is not None:
                try:
                    envelope = json.loads(Path(output_record.path).read_text(encoding="utf-8"))
                    if isinstance(envelope, list) and len(envelope) == 1 and isinstance(envelope[0], Mapping):
                        payload_hash = hash_json(envelope[0].get("ai_summary"))
                    elif isinstance(envelope, Mapping):
                        payload_hash = hash_json(
                            envelope.get("analysis")
                            if envelope.get("analysis") is not None
                            else envelope.get("ai_summary")
                        )
                except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                    payload_hash = ""
            response_hash = str(receipt.response_hash or "")
            bound.append(
                dataclass_replace(
                    expected,
                    output_hash=response_hash,
                    provider_response_hash=response_hash,
                    normalized_output_hash=response_hash,
                    artifact_payload_hash=payload_hash,
                    artifact_content_hash=str(output_record.content_hash if output_record else ""),
                    registry_file_hash=(file_sha256(output_record.path) if output_record else ""),
                    registered_artifact_hash=str(output_record.content_hash if output_record else ""),
                    node_output_hash=str(output_record.content_hash if output_record else ""),
                    artifact_path=str(output_record.path if output_record else expected.artifact_path),
                )
            )
        closure = ProviderReceiptClosure.evaluate(bound, receipts)
        self.receipt_closure_path = self.workspace.artifact_path(
            "stage1/provider_receipt_closure.json"
        )
        reuse_records = [
            self.registry.get(artifact_id) for artifact_id in self.reuse_evidence_ids
        ]
        reuse_records = [
            record
            for record in reuse_records
            if record is not None and record.status == "ready"
        ]
        payload = {
            "artifact_type": "provider_receipt_closure",
            "artifact_version": "v1",
            "job_id": self.job_id,
            "stage_name": "stage1_analyze",
            "attempt_id": self.attempt_id,
            "logical_attempt_identity": self.attempt_id,
            "closure_epoch_id": self.closure_epoch_id,
            "expected_call_graph_hash": self.expected_call_graph_hash,
            "expected_calls": [asdict(item) for item in bound],
            "paper_artifact_ids": sorted(set(paper_ids)),
            "source_summary_artifact_ids": sorted(set(stable_source_ids)),
            "payload": closure.to_dict(),
            "reuse_evidence_ids": [record.artifact_id for record in reuse_records],
            "reuse_evidence_count": len(reuse_records),
            "expected_provider_transport_count": len(self.expected_calls),
            "actual_provider_transport_count": len(receipts),
        }
        dependency_ids = (
            "source_bundle",
            "runtime_job_spec",
            "stage1:provider_expected_call_graph",
            "summary_source_manifest",
            *sorted(set(paper_ids)),
            *sorted(set(stable_source_ids)),
            *[record.artifact_id for record in reuse_records],
        )
        if self.expected_calls:
            dependency_ids = (*dependency_ids, "stage1_provider_receipts")
        dependency_records: list[ArtifactRecord] = []
        seen_dependency_ids: set[str] = set()
        for artifact_id in dependency_ids:
            if artifact_id in seen_dependency_ids:
                continue
            candidate = self.registry.get(artifact_id)
            if candidate is not None and candidate.status == "ready":
                dependency_records.append(candidate)
                seen_dependency_ids.add(artifact_id)
        for candidate in all_records:
            if (
                candidate.status == "ready"
                and candidate.artifact_type == "evidence_manifest"
                and candidate.artifact_id not in seen_dependency_ids
            ):
                dependency_records.append(candidate)
                seen_dependency_ids.add(candidate.artifact_id)
        current_expected_calls = [asdict(item) for item in bound]
        current_dependency_set = {
            (
                dependency.dependency_kind,
                dependency.job_id,
                dependency.artifact_id,
                dependency.artifact_type,
                dependency.path,
                dependency.content_hash,
            )
            for dependency in (
                ArtifactDependencyRefV2.from_record(item)
                for item in dependency_records
            )
        }
        existing = self.registry.get("stage1:provider_receipt_closure")
        if existing is not None and existing.status == "ready":
            try:
                existing_payload = json.loads(Path(existing.path).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                existing_payload = None
            closure_payload = (
                existing_payload.get("payload")
                if isinstance(existing_payload, Mapping)
                else None
            )
            issue_list_fields = (
                "missing_call_ids",
                "stale_call_ids",
                "failed_call_ids",
                "incomplete_call_ids",
                "unexpected_receipts",
                "out_of_scope_receipts",
                "out_of_epoch_receipts",
                "historical_receipts",
                "retry_exceeded_call_ids",
                "usage_incomplete_call_ids",
            )
            raw_expected_ids = (
                closure_payload.get("expected_call_ids")
                if isinstance(closure_payload, Mapping)
                else None
            )
            raw_observed_ids = (
                closure_payload.get("observed_call_ids")
                if isinstance(closure_payload, Mapping)
                else None
            )
            closure_lists_well_formed = (
                isinstance(raw_expected_ids, list)
                and isinstance(raw_observed_ids, list)
                and all(
                    isinstance(item, str) and bool(item)
                    for item in (*raw_expected_ids, *raw_observed_ids)
                )
                and isinstance(closure_payload, Mapping)
                and all(
                    isinstance(closure_payload.get(field), list)
                    for field in issue_list_fields
                )
                and isinstance(closure_payload.get("hash_mismatches"), Mapping)
                and all(
                    isinstance(values, list)
                    for values in closure_payload.get("hash_mismatches", {}).values()
                )
            )
            expected_ids = (
                tuple(raw_expected_ids)
                if isinstance(raw_expected_ids, list)
                else ()
            )
            observed_ids = (
                tuple(raw_observed_ids)
                if isinstance(raw_observed_ids, list)
                else ()
            )
            closure_issues_empty = (
                isinstance(closure_payload, Mapping)
                and closure_lists_well_formed
                and not any(
                    closure_payload.get(field)
                    for field in (*issue_list_fields, "hash_mismatches")
                )
            )
            expected_transport_count = (
                existing_payload.get("expected_provider_transport_count")
                if isinstance(existing_payload, Mapping)
                else None
            )
            actual_transport_count = (
                existing_payload.get("actual_provider_transport_count")
                if isinstance(existing_payload, Mapping)
                else None
            )
            counts_match = (
                isinstance(expected_transport_count, int)
                and not isinstance(expected_transport_count, bool)
                and isinstance(actual_transport_count, int)
                and not isinstance(actual_transport_count, bool)
                and expected_transport_count == len(self.expected_calls)
                and actual_transport_count == len(receipts)
            )
            root_expected_calls = (
                existing_payload.get("expected_calls")
                if isinstance(existing_payload, Mapping)
                else None
            )
            expected_calls_match = (
                isinstance(root_expected_calls, list)
                and hash_json(root_expected_calls) == hash_json(current_expected_calls)
            )
            try:
                existing_dependency_set = {
                    (
                        dependency.dependency_kind,
                        dependency.job_id,
                        dependency.artifact_id,
                        dependency.artifact_type,
                        dependency.path,
                        dependency.content_hash,
                    )
                    for dependency in existing.depends_on
                }
            except (AttributeError, TypeError, ValueError):
                existing_dependency_set = set()
            dependencies_match = existing_dependency_set == current_dependency_set
            file_hash_matches = False
            try:
                existing_artifact_valid = True
                ArtifactRegistry._verify_ready_artifact(existing)
                self.registry.verify_ready_dependencies(existing.depends_on)
                file_hash_matches = file_sha256(existing.path) == existing.content_hash
            except (OSError, RegistryError, TypeError, ValueError):
                existing_artifact_valid = False
            if (
                isinstance(existing_payload, Mapping)
                and str(existing_payload.get("closure_epoch_id") or "") == self.closure_epoch_id
                and str(existing_payload.get("expected_call_graph_hash") or "")
                == self.expected_call_graph_hash
                and file_hash_matches
                and existing_artifact_valid
                and isinstance(closure_payload, Mapping)
                and closure_payload.get("complete") is True
                and expected_ids == expected_call_ids
                and observed_ids == expected_call_ids
                and closure_issues_empty
                and counts_match
                and expected_calls_match
                and dependencies_match
            ):
                self.receipt_closure_path = existing.path
                self.receipt_closure_hash = existing.content_hash
                return existing
        record = publish_json_artifact(
            self.publication_context,
            self.registry,
            self.receipt_closure_path,
            payload,
            artifact_role="provider_receipt_closure",
            artifact_type="provider_receipt_closure",
            artifact_version="v1",
            producer="services.stage1_analysis_service.Stage1AnalysisService",
            artifact_id="stage1:provider_receipt_closure",
            depends_on=[ArtifactDependencyRefV2.from_record(item) for item in dependency_records],
            metadata={
                "closure_epoch_id": self.closure_epoch_id,
                "stage_name": "stage1_analyze",
                "expected_call_graph_hash": self.expected_call_graph_hash,
                "complete": closure.complete,
                "depends_on_expected_graph": "stage1:provider_expected_call_graph",
                "paper_artifact_ids": sorted(set(paper_ids)),
                "source_summary_artifact_ids": sorted(set(stable_source_ids)),
                "reuse_evidence_ids": [record.artifact_id for record in reuse_records],
            },
        )
        self.receipt_closure_path = record.path
        self.receipt_closure_hash = record.content_hash
        return record

    def _generate_one(self, item: PaperWorkItem) -> tuple[dict[str, Any], tuple[str, ...]]:
        source_pdf = str(item.source_pdf or "").strip()
        if not source_pdf or not Path(source_pdf).is_file():
            raise RuntimeError(
                f"Stage 1 source PDF is missing for {self._paper_key(item)}: {source_pdf or '<empty>'}"
            )

        preprocess = self._preprocess(source_pdf)
        preprocess_metadata = self._preprocess_metadata(preprocess)
        evidence_manifest = build_evidence_manifest_v1(
            job_id=self.job_id,
            canonical_paper_key=item.canonical_paper_key,
            preprocess=preprocess_metadata,
        )
        evidence_manifest_path = self.workspace.artifact_path(
            "evidence_manifests/"
            f"{hashlib.sha256(item.canonical_paper_key.encode('utf-8')).hexdigest()[:24]}_v1.json"
        )
        evidence_record = publish_json_artifact(
            self.publication_context,
            self.registry,
            evidence_manifest_path,
            evidence_manifest.to_dict(),
            artifact_role="evidence_manifest",
            artifact_type="evidence_manifest",
            artifact_version="v1",
            producer="services.stage1_analysis_service.Stage1AnalysisService",
            artifact_id=f"evidence_manifest:{item.canonical_paper_key}",
        )
        preprocess_metadata["evidence_manifest_path"] = evidence_record.path
        preprocess_metadata["evidence_manifest_hash"] = evidence_record.content_hash
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
            call_id=f"stage1:{self._paper_key(item)}",
            endpoint_type=str(primary_config.get("endpoint_type") or "chat_completions"),
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
                "receipt_ledger_path": self.receipt_ledger_path,
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
        current_receipts = tuple(
            receipt
            for receipt in self.receipt_ledger.list_receipts()
            if str(receipt.closure_epoch_id or "") == self.closure_epoch_id
        )
        # A zero-transport run has no provider receipt ledger.  Do not publish
        # an empty JSONL file that downstream validators could mistake for a
        # genuine transport receipt artifact.
        if not current_receipts:
            self.receipt_ledger_path = ""
            return
        payload = b"".join(
            (
                json.dumps(
                    receipt.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            for receipt in current_receipts
        )
        record = publish_bytes_artifact(
            self.publication_context,
            self.registry,
            self.receipt_ledger_target_path,
            payload,
            artifact_role="provider_receipts",
            artifact_type="provider_receipt_ledger",
            artifact_version="v1",
            producer="services.stage1_analysis_service.Stage1AnalysisService",
            artifact_id="stage1_provider_receipts",
            metadata={
                "receipt_count": len(current_receipts),
                "stage_name": "stage1_analyze",
                "closure_epoch_id": self.closure_epoch_id,
            },
        )
        self.receipt_ledger_path = record.path

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

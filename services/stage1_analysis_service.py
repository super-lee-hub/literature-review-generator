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
    canonical_provider_request_payload,
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
from services.prompt_registry import PromptRegistry
from services.stage1_visual_scan import (
    VisualScanBatch,
    VISUAL_OBSERVATIONS_VERSION,
    VISUAL_SCAN_PROMPT_ID,
    build_visual_scan_prompt,
    build_visual_scan_user_content,
    estimate_encoded_image_bytes,
    normalize_visual_byte_budgets,
    summarize_raw_reinspection_groups,
    validate_current_visual_observations_v2,
    select_final_visual_refs_after_scan,
)
from services.config_values import parse_strict_bool
from services.multimodal_capability import detect_multimodal_capability
from services.stage1_reuse import (
    STAGE1_REUSE_POLICY,
    Stage1ReusableSummaryBindingV1,
    Stage1ReusableSummaryManifestV1,
    Stage1VisualEvidenceQualificationV1,
    Stage1ReuseEligibilityV1,
    build_binding_hash,
    evaluate_stage1_reuse,
    verify_stage1_typed_manifest_authority,
)
from summary_schema import build_summary_schema_contract, is_canonical_ai_summary, normalize_ai_summary


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
    stage1_input_settings: dict[str, Any]
    visual_bundle: dict[str, Any]
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
        self.prompt_registry = PromptRegistry()
        self._stage1_user_prompt_identity = self.prompt_registry.identity("stage1.analysis.user.v3")
        self._stage1_system_prompt_identity = self.prompt_registry.identity("stage1.analysis.system.v3")
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
        self._expected_source_bundle_hash = ""
        self._expected_runtime_spec_hash = ""
        self.receipt_closure_path = ""
        self.receipt_closure_hash = ""
        self.reuse_evidence_ids: list[str] = []
        self._generated_authorities: dict[
            str, tuple[ArtifactRecord, Stage1ReusableSummaryBindingV1, Mapping[str, Any]]
        ] = {}
        self._final_source_manifests: dict[str, ArtifactRecord] = {}
        self._visual_observation_records: dict[str, list[ArtifactRecord]] = {}
        self._visual_coverage_records: dict[str, ArtifactRecord] = {}

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
        primary_config = dict(self.settings.section("Primary_Reader_API"))
        built_input = Stage1InputBuilder(logger=self.logger).build(
            prompt_template=self._prompt_template(),
            paper_text=preprocess.stage1_input_text,
            reader_api_config=primary_config,
            visual_bundle=visual_bundle,
            pdf_path=source_pdf,
            stage1_input_settings=stage1_settings,
            preprocess_metadata=preprocess_metadata,
            prompt_identity=self._stage1_user_prompt_identity.to_dict(),
            prompt_values={"SUMMARY_SCHEMA_CONTRACT": build_summary_schema_contract()},
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
            stage1_input_settings=stage1_settings,
            visual_bundle=dict(visual_bundle),
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
        prompt_authority = {
            "system": self._stage1_system_prompt_identity.to_dict(),
            "user": self._stage1_user_prompt_identity.to_dict(),
        }
        prompt_template_hash = hash_json(prompt_authority)
        visual_identity = self._build_visual_semantic_identity(
            visual_bundle=visual_bundle,
            selected_visual_refs=(built_input.all_visual_refs or built_input.selected_visual_refs),
            selection_policy_snapshot=built_input.visual_selection_policy_snapshot,
        )
        semantic_visual_refs = list(visual_identity.get("all_visuals") or visual_identity.get("selected_visuals") or [])
        semantic_visual_policy = dict(visual_identity.get("selection_policy") or {})
        visual_input_manifest_hash = hash_json(visual_identity)
        input_builder_policy_hash = hash_json(
            {
                "builder_version": "Stage1InputBuilder:v1",
                "stage1_input_policy": _redact_mapping(stage1_input_settings),
                "input_mode": str(built_input.input_mode or ""),
                "selected_visual_refs": semantic_visual_refs,
                "visual_selection_policy_snapshot": semantic_visual_policy,
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
            prompt_id=self._stage1_user_prompt_identity.prompt_id,
            prompt_version=self._stage1_user_prompt_identity.version,
            prompt_sha256=self._stage1_user_prompt_identity.sha256,
            prompt_template_hash=prompt_template_hash,
            input_builder_policy_hash=input_builder_policy_hash,
            summary_schema_hash=self._schema_hash(),
            visual_input_manifest_hash=visual_input_manifest_hash,
            visual_coverage_hash=hash_json(visual_identity.get("coverage_plan") or {}),
            visual_scan_schema_hash=self._visual_scan_schema_hash(),
            original_source_location=str(item.source_pdf or ""),
            current_source_location=str(item.source_pdf or ""),
            preprocess_hash=preprocess_hash,
            stage1_input_hash=hash_json(
                {
                    "source_text_hash": hash_text(
                        str(preprocess_metadata.get("stage1_input_text") or "")
                    ),
                    "input_mode": str(built_input.input_mode or ""),
                    "selected_visual_refs": semantic_visual_refs,
                    "visual_selection_policy_snapshot": semantic_visual_policy,
                    "multimodal_capability": dict(built_input.multimodal_capability or {}),
                    "pdf_attachment_status": str(built_input.pdf_attachment_status or ""),
                }
            ),
            prompt_hash=hash_json(
                {
                    "prompt_id": self._stage1_user_prompt_identity.prompt_id,
                    "prompt_version": self._stage1_user_prompt_identity.version,
                    "prompt_sha256": self._stage1_user_prompt_identity.sha256,
                    "prompt_template_hash": prompt_template_hash,
                    "source_text_hash": hash_text(
                        str(preprocess_metadata.get("stage1_input_text") or "")
                    ),
                    "visual_provenance_hash": visual_input_manifest_hash,
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
            visual_provenance_hash=visual_input_manifest_hash,
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
                "prompt_authority": prompt_authority,
                "visual_coverage_hash": hash_json(visual_identity.get("coverage_plan") or {}),
                "visual_scan_schema_hash": self._visual_scan_schema_hash(),
                "require_complete_visual_coverage": parse_strict_bool(
                    stage1_input_settings.get("require_complete_visual_coverage"),
                    field="Stage1_Input.require_complete_visual_coverage",
                    default=True,
                ),
            },
        )

    @staticmethod
    def _build_visual_semantic_identity(
        *,
        visual_bundle: Mapping[str, Any],
        selected_visual_refs: Sequence[Mapping[str, Any]],
        selection_policy_snapshot: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Project visual evidence into a path-independent reuse identity."""

        refs = [dict(item) for item in selected_visual_refs if isinstance(item, Mapping)]
        policy = dict(selection_policy_snapshot or {})
        raw_coverage = visual_bundle.get("coverage_report") or visual_bundle.get("visual_coverage") or {}
        coverage = dict(raw_coverage) if isinstance(raw_coverage, Mapping) else {}
        # Job ownership is Registry provenance, not visual input semantics.
        # Excluding it preserves exact reuse when identical PDF bytes move to
        # another workspace/job while the coverage artifact remains job-bound.
        coverage.pop("job_id", None)
        coverage.pop("selected_crops", None)
        coverage.pop("coverage_artifact_path", None)
        coverage.pop("coverage_artifact_hash", None)
        # These are achieved execution facts, not immutable input semantics.
        # Keeping them in a reuse hash makes a long-paper first run (planned)
        # differ from its own post-scan synthesis (achieved).
        for key in (
            "scan_batches", "visually_scanned_pages", "failed_pages",
            "coverage_status", "omissions", "observed_visual_ids",
            "sent_visual_ids", "observation_artifact_hashes",
        ):
            coverage.pop(key, None)
        coverage["page_status"] = [
            {
                "page_no": int(item.get("page_no") or 0),
                "status": str(item.get("status") or ""),
                "skipped_reason": str(item.get("skipped_reason") or ""),
            }
            for item in coverage.get("page_status") or []
            if isinstance(item, Mapping)
        ]
        if not refs and not policy and not coverage:
            return {}

        selected_visuals: list[dict[str, Any]] = []
        for selection_rank, visual in enumerate(refs, start=1):
            image_path = str(visual.get("image_path") or "").strip()
            image_content_sha256 = (
                file_sha256(image_path) if image_path and Path(image_path).is_file() else ""
            )
            bbox: list[float] = []
            raw_bbox = visual.get("bbox")
            if isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) == 4:
                try:
                    bbox = [round(float(value), 2) for value in raw_bbox]
                except (TypeError, ValueError):
                    bbox = []
            page_range = []
            raw_page_range = visual.get("page_range")
            if isinstance(raw_page_range, (list, tuple)):
                try:
                    page_range = [int(value) for value in raw_page_range]
                except (TypeError, ValueError):
                    page_range = []

            selected_visuals.append(
                {
                    "selection_rank": selection_rank,
                    "visual_id": str(visual.get("visual_id") or ""),
                    "page_no": int(visual.get("page_no") or 0),
                    "page_range": page_range,
                    "bbox": bbox,
                    "artifact_type": str(visual.get("artifact_type") or ""),
                    "source_type": str(visual.get("source_type") or ""),
                    "image_content_sha256": image_content_sha256,
                    "caption_excerpt_hash": hash_text(
                        " ".join(str(visual.get("caption_excerpt") or "").split())
                    ),
                    "nearby_text_excerpt_hash": hash_text(
                        " ".join(str(visual.get("nearby_text_excerpt") or "").split())
                    ),
                    "selection_reason_hash": hash_text(
                        " ".join(str(visual.get("selection_reason") or "").split())
                    ),
                    "selection_score": float(visual.get("selection_score") or 0.0),
                    "dedupe_group_id": str(visual.get("dedupe_group_id") or ""),
                }
            )

        return {
            "identity_version": "stage1_visual_semantic_identity/v1",
            "artifact_type": str(visual_bundle.get("artifact_type") or ""),
            "artifact_version": str(visual_bundle.get("artifact_version") or ""),
            "selection_policy": policy,
            "bundle_metadata": dict(visual_bundle.get("bundle_metadata") or {}),
            "coverage_plan": coverage,
            "all_visuals": selected_visuals,
            # Retain the old key for readers that only understand v1.
            "selected_visuals": selected_visuals,
        }

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
            # strategy_policy was accepted by older configs but has no
            # production parser owner; do not let it invalidate exact reuse.
            if lowered == "strategy_policy":
                return False
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
        for observation_record in self._visual_observation_records.get(
            self._paper_key(prepared.item), []
        ):
            dependencies.append(ArtifactDependencyRefV2.from_record(observation_record))
        coverage_record = self._visual_coverage_records.get(self._paper_key(prepared.item))
        if coverage_record is not None:
            dependencies.append(ArtifactDependencyRefV2.from_record(coverage_record))
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
                prompt_id=closure_payload.prompt_id,
                prompt_version=closure_payload.prompt_version,
                prompt_sha256=closure_payload.prompt_sha256,
                prompt_template_hash=closure_payload.prompt_template_hash,
                input_builder_policy_hash=closure_payload.input_builder_policy_hash,
                summary_schema_hash=closure_payload.summary_schema_hash,
                visual_input_manifest_hash=closure_payload.visual_input_manifest_hash,
                visual_coverage_hash=closure_payload.visual_coverage_hash,
                visual_scan_schema_hash=closure_payload.visual_scan_schema_hash,
                visual_evidence_qualification=closure_payload.visual_evidence_qualification,
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
                self._visual_coverage_records.get(paper_key),
                *self._visual_observation_records.get(paper_key, []),
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

    @staticmethod
    def _synthesis_call_id(paper_key: str) -> str:
        return f"stage1_synthesis:{paper_key}"

    @staticmethod
    def _visual_scan_call_id(paper_key: str, batch_index: int) -> str:
        return f"stage1_visual_scan:{paper_key}:{int(batch_index)}"

    def _visual_observation_path(self, paper_key: str, batch_index: int) -> str:
        digest = hashlib.sha256(str(paper_key).encode("utf-8")).hexdigest()[:24]
        return self.workspace.artifact_path(
            f"stage1_visuals/{digest}/observations_batch_{int(batch_index):04d}.json"
        )

    def _visual_scan_schema_hash(self) -> str:
        identity = self.prompt_registry.identity(VISUAL_SCAN_PROMPT_ID)
        return hash_json(
            {
                "artifact_type": "stage1_visual_observations",
                "artifact_version": VISUAL_OBSERVATIONS_VERSION,
                "prompt_id": identity.prompt_id,
                "prompt_version": identity.version,
                "prompt_sha256": identity.sha256,
            }
        )

    def _visual_scan_ocr_by_id(self, prepared: _PreparedStage1Item) -> dict[str, str]:
        page_index_path = str(prepared.preprocess_metadata.get("page_index_path") or "").strip()
        if not page_index_path or not Path(page_index_path).is_file():
            return {}
        try:
            payload = json.loads(Path(page_index_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, list):
            return {}
        return {
            f"page-{int(item.get('page_number') or item.get('page_no') or 0):03d}": str(item.get("text") or "")
            for item in payload
            if isinstance(item, Mapping) and int(item.get("page_number") or item.get("page_no") or 0) > 0
        }

    def _visual_scan_request(
        self,
        prepared: _PreparedStage1Item,
        batch: VisualScanBatch,
    ) -> tuple[str, str]:
        return build_visual_scan_prompt(
            batch,
            ocr_by_visual_id=self._visual_scan_ocr_by_id(prepared),
        )

    def _visual_scan_input_payload(
        self,
        prepared: _PreparedStage1Item,
        batch: VisualScanBatch,
    ) -> dict[str, Any]:
        ocr_by_id = self._visual_scan_ocr_by_id(prepared)
        identity = self.prompt_registry.identity(VISUAL_SCAN_PROMPT_ID)
        return {
            "artifact_type": "stage1_visual_scan_input",
            "artifact_version": VISUAL_OBSERVATIONS_VERSION,
            "batch_index": int(batch.batch_index),
            "visual_refs": [
                {
                    "visual_id": str(item.get("visual_id") or ""),
                    "page_no": int(item.get("page_no") or 0),
                    "bbox": list(item.get("bbox") or []),
                    "artifact_type": str(item.get("artifact_type") or ""),
                    "image_sha256": str(
                        item.get("image_sha256")
                        or (
                            file_sha256(str(item.get("image_path") or ""))
                            if str(item.get("image_path") or "").strip()
                            else ""
                        )
                    ),
                    "ocr_excerpt_hash": hash_text(str(ocr_by_id.get(str(item.get("visual_id") or "")) or "")),
                }
                for item in batch.visual_refs
            ],
            "child_candidates": list(batch.to_dict().get("child_candidates") or []),
            "prompt_id": identity.prompt_id,
            "prompt_version": identity.version,
            "prompt_sha256": identity.sha256,
        }

    def _predeclare_expected_calls(
        self,
        bundle: SourceBundle,
        prepared: Sequence[_PreparedStage1Item],
    ) -> None:
        source_bundle_hash, runtime_spec_hash = self._ensure_durable_input_records(bundle)
        # Exact summary reuse is evidence, not provider work. The expected
        # graph contains only items that can genuinely produce a receipt.
        graph_seed: list[dict[str, Any]] = []
        for item in prepared:
            if item.previous is not None:
                continue
            paper_key = self._paper_key(item.item)
            vision_enabled = detect_multimodal_capability(item.primary_config).supports_image_input
            scan_call_planned = False
            candidate_batches = item.built_input.visual_scan_candidate_refs or []
            for batch_index, batch_refs in enumerate(item.built_input.visual_scan_batches or []):
                if not vision_enabled:
                    break
                batch = VisualScanBatch(
                    batch_index=batch_index,
                    visual_refs=tuple(dict(ref) for ref in batch_refs),
                    child_candidates=tuple(
                        dict(ref)
                        for ref in (
                            candidate_batches[batch_index]
                            if batch_index < len(candidate_batches)
                            else []
                        )
                        if isinstance(ref, Mapping)
                    ),
                )
                max_request, max_single = normalize_visual_byte_budgets(
                    max_request_image_bytes=item.stage1_input_settings.get("max_request_image_bytes"),
                    max_single_image_bytes=item.stage1_input_settings.get("max_single_image_bytes"),
                )
                _scan_content, transport_report = build_visual_scan_user_content(
                    batch,
                    return_report=True,
                    max_single_image_bytes=max_single,
                    max_request_image_bytes=max_request,
                )
                sent_refs = tuple(
                    dict(ref)
                    for ref in transport_report.get("sent_visual_refs", [])
                    if isinstance(ref, Mapping)
                )
                # A batch with no sendable image is an explicit omission, not
                # a provider call that can later be mistaken for coverage.
                if not sent_refs:
                    continue
                scan_call_planned = True
                effective_batch = VisualScanBatch(
                    batch_index=batch_index,
                    visual_refs=sent_refs,
                    child_candidates=batch.child_candidates,
                )
                prompt, system_prompt = self._visual_scan_request(item, effective_batch)
                effective_scan_content, _effective_report = build_visual_scan_user_content(
                    effective_batch,
                    return_report=True,
                    max_single_image_bytes=max_single,
                    max_request_image_bytes=max_request,
                )
                effective_config = self._effective_provider_config(item.primary_config)
                scan_identity = self.prompt_registry.identity(VISUAL_SCAN_PROMPT_ID)
                graph_seed.append(
                    {
                        "call_id": self._visual_scan_call_id(paper_key, batch_index),
                        "job_id": self.job_id,
                        "attempt_id": self.attempt_id,
                        "stage_name": "stage1_analyze",
                        "node_id": f"{paper_key}:visual_scan:{batch_index}",
                        "logical_attempt_identity": self.attempt_id,
                        "prompt_hash": hash_text(prompt),
                        "prompt_id": scan_identity.prompt_id,
                        "prompt_version": scan_identity.version,
                        "prompt_sha256": scan_identity.sha256,
                        "input_hash": self._request_hash(prompt, system_prompt, effective_scan_content, effective_config, response_format="json"),
                        "config_hash": hash_json(_redact_mapping(effective_config)),
                        "schema_hash": self._visual_scan_schema_hash(),
                        "artifact_path": self._visual_observation_path(paper_key, batch_index),
                        "max_attempts": max(1, self.settings.runtime.node_retry_limit + 1),
                        "usage_required": False,
                    }
                )
            synthesis_content = item.built_input.user_message_content
            primary_config = self._effective_provider_config(item.primary_config)
            backup_config = self._effective_provider_config(item.backup_config)
            primary_hash = (
                ""
                if scan_call_planned
                else self._request_hash(
                    item.built_input.prompt_text,
                    self.prompt_registry.read("stage1.analysis.system.v3"),
                    synthesis_content,
                    primary_config,
                    response_format="json",
                )
            )
            backup_content = self._text_only_content(synthesis_content)
            backup_hash = self._request_hash(
                item.built_input.prompt_text,
                self.prompt_registry.read("stage1.analysis.system.v3"),
                backup_content,
                backup_config,
                response_format="json",
                default_max_tokens=8192,
            )
            variants = []
            if primary_hash:
                variants.append({
                    "input_hash": primary_hash,
                    "config_hash": hash_json(_redact_mapping(primary_config)),
                })
            variants.append({
                "input_hash": backup_hash,
                "config_hash": hash_json(_redact_mapping(backup_config)),
            })
            graph_seed.append(
                {
                    "call_id": self._synthesis_call_id(paper_key),
                    "job_id": self.job_id,
                    "attempt_id": self.attempt_id,
                    "stage_name": "stage1_analyze",
                    "node_id": paper_key,
                    "logical_attempt_identity": self.attempt_id,
                    "prompt_hash": hash_text(item.built_input.prompt_text),
                    "prompt_id": item.built_input.prompt_id,
                    "prompt_version": item.built_input.prompt_version,
                    "prompt_sha256": item.built_input.prompt_sha256,
                    "input_hash": primary_hash,
                    "config_hash": hash_json(_redact_mapping(primary_config)),
                    "schema_hash": self._schema_hash(),
                    "artifact_path": self._paper_artifact_path(item.item),
                    "max_attempts": max(1, self.settings.runtime.node_retry_limit + 1),
                    "usage_required": False,
                "request_variants": variants if primary_hash else ({
                    "input_hash": backup_hash,
                    "config_hash": hash_json(_redact_mapping(backup_config)),
                },),
                }
            )
        graph_hash = hash_json({
            "identity_version": "stage1_expected_call_graph/v2",
            "job_id": self.job_id,
            "stage_name": "stage1_analyze",
            "attempt_id": self.attempt_id,
            "source_bundle_hash": source_bundle_hash,
            "runtime_spec_hash": runtime_spec_hash,
            "call_shapes": [
                {
                    "call_id": item["call_id"],
                    "node_id": item["node_id"],
                    "prompt_id": item["prompt_id"],
                    "schema_hash": item["schema_hash"],
                    "artifact_path": item["artifact_path"],
                    "max_attempts": item["max_attempts"],
                }
                for item in graph_seed
            ],
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
        self._expected_source_bundle_hash = source_bundle_hash
        self._expected_runtime_spec_hash = runtime_spec_hash
        self.expected_calls = tuple(
            ExpectedProviderCall(
                **item,
                closure_epoch_id=epoch,
                expected_call_graph_hash=graph_hash,
            )
            for item in graph_seed
        )
        self.expected_call_graph_path = self.workspace.artifact_path("stage1/provider_expected_calls.json")
        self._publish_expected_call_graph()

    @staticmethod
    def _text_only_content(content: Any) -> Any:
        if not isinstance(content, list):
            return content
        text_items = [
            dict(item)
            for item in content
            if isinstance(item, Mapping)
            and str(item.get("type") or "").strip().lower() in {"text", "input_text"}
            and str(item.get("text") or "").strip()
        ]
        return text_items or None

    def _freeze_synthesis_visual_transport(
        self,
        prepared: _PreparedStage1Item,
    ) -> _PreparedStage1Item:
        """Freeze the final visual wire membership before expected-call binding.

        Selection and the HTTP transport are separate trust boundaries.  This
        preflight makes the final request identity depend on the bytes that
        will actually be sent and records any atomic-group fallback or
        non-representation before the provider call is admitted.
        """

        content = prepared.built_input.user_message_content
        if not isinstance(content, list):
            return prepared
        from ai_interface import freeze_local_visual_transport_content

        max_request, max_single = normalize_visual_byte_budgets(
            max_request_image_bytes=prepared.stage1_input_settings.get(
                "max_request_image_bytes"
            ),
            max_single_image_bytes=prepared.stage1_input_settings.get(
                "max_single_image_bytes"
            ),
        )
        frozen_content, report = freeze_local_visual_transport_content(
            content,
            max_single_image_bytes=max_single,
            max_request_image_bytes=max_request,
        )
        coverage = dict(prepared.built_input.visual_coverage or {})
        planned_units = [
            dict(item)
            for item in (coverage.get("raw_reinspection_units") or [])
            if isinstance(item, Mapping)
        ]
        group_reports = {
            str(item.get("group_id") or ""): item
            for item in (report.get("raw_reinspection_groups") or [])
            if isinstance(item, Mapping) and str(item.get("group_id") or "")
        }
        updated_units: list[dict[str, Any]] = []
        for unit in planned_units:
            unit_id = str(unit.get("unit_id") or "")
            group_report = group_reports.get(unit_id)
            if group_report is not None:
                resolution = str(group_report.get("resolution") or "")
                if resolution in {"page_snapshot_fallback", "not_represented"}:
                    unit["resolution"] = resolution
                    unit["selected_ids"] = [
                        str(value)
                        for value in (group_report.get("selected_ids") or [])
                        if str(value)
                    ]
                    if resolution == "page_snapshot_fallback":
                        unit["fallback_refs"] = list(unit["selected_ids"])
                    unit["fallback_reason"] = str(
                        group_report.get("fallback_reason") or ""
                    )
            updated_units.append(unit)
        if planned_units:
            coverage.update(
                summarize_raw_reinspection_groups(
                    prepared.built_input.selected_visual_refs or [],
                    sent_visual_ids=report.get("sent_visual_ids") or [],
                    planned_units=updated_units,
                )
            )
        coverage["transport_preflight"] = dict(report)
        coverage["transport_preflight_omissions"] = list(report.get("omissions") or [])
        rebuilt_input = replace(
            prepared.built_input,
            user_message_content=frozen_content,
            visual_coverage=coverage,
        )
        return replace(prepared, built_input=rebuilt_input)

    @staticmethod
    def _effective_provider_config(config: Mapping[str, Any]) -> dict[str, Any]:
        """Mirror the effective config fields added by ai_interface."""

        effective = dict(config)
        effective["api_key"] = str(config.get("api_key") or "")
        effective["model"] = str(config.get("model") or "")
        effective["api_base"] = str(
            config.get("api_base") or "https://api.openai.com/v1"
        )
        return effective

    @staticmethod
    def _request_parameters(
        api_config: Mapping[str, Any],
        *,
        default_max_tokens: int,
    ) -> tuple[int, float]:
        raw_max_tokens = api_config.get("max_output_tokens", default_max_tokens)
        raw_temperature = api_config.get("temperature", 0.3)
        try:
            max_tokens = int(raw_max_tokens)
            temperature = float(raw_temperature)
        except (TypeError, ValueError):
            # This mirrors get_summary_from_ai_detailed's defensive fallback.
            max_tokens = 3000
            temperature = 0.3
        return max_tokens, temperature

    @staticmethod
    def _request_hash(
        prompt: str,
        system_prompt: str,
        user_content: Any,
        api_config: Mapping[str, Any],
        *,
        response_format: str,
        default_max_tokens: int = 3000,
    ) -> str:
        max_tokens, temperature = Stage1AnalysisService._request_parameters(
            api_config,
            default_max_tokens=default_max_tokens if response_format == "json" else 4000,
        )
        return hash_json(canonical_provider_request_payload(
            prompt=prompt,
            system_prompt=system_prompt,
            user_content=user_content,
            response_format=response_format,
            max_output_tokens=max_tokens,
            temperature=temperature,
        ))

    @staticmethod
    def _transport_metadata_for_content(
        content: Any,
        *,
        stage1_input_settings: Mapping[str, Any],
        engine_type: str,
    ) -> dict[str, Any]:
        """Calculate the local-input transport facts for injected readers."""

        max_request, max_single = normalize_visual_byte_budgets(
            max_request_image_bytes=stage1_input_settings.get("max_request_image_bytes"),
            max_single_image_bytes=stage1_input_settings.get("max_single_image_bytes"),
        )
        planned_ids: list[str] = []
        sent_ids: list[str] = []
        omissions: list[dict[str, Any]] = []
        raw_group_reports: dict[str, dict[str, Any]] = {}
        encoded_bytes = 0
        has_file = False
        if engine_type != "backup" and isinstance(content, list):
            for raw in content:
                if not isinstance(raw, Mapping):
                    continue
                item_type = str(raw.get("type") or "").strip().lower()
                if item_type == "local_image_path":
                    visual_id = str(raw.get("visual_id") or "")
                    planned_for_item = [
                        str(value)
                        for value in (
                            raw.get("transport_planned_visual_ids")
                            or raw.get("ambiguous_candidate_ids")
                            or ([visual_id] if visual_id else [])
                        )
                        if str(value)
                    ]
                    for value in planned_for_item:
                        if value not in planned_ids:
                            planned_ids.append(value)
                    group_id = str(raw.get("raw_reinspection_group_id") or "").strip()
                    group: dict[str, Any] | None = None
                    if group_id:
                        group = raw_group_reports.setdefault(
                            group_id,
                            {
                                "group_id": group_id,
                                "page_no": int(raw.get("page_no") or 0),
                                "ambiguous_candidate_ids": list(planned_for_item),
                                "resolution": str(
                                    raw.get("raw_reinspection_resolution") or ""
                                ),
                                "selected_ids": [
                                    str(value)
                                    for value in (raw.get("raw_reinspection_selected_ids") or [])
                                    if str(value)
                                ],
                                "actual_sent_ids": [],
                                "fallback_reason": str(
                                    raw.get("raw_reinspection_fallback_reason") or ""
                                ),
                            },
                        )
                    path = str(raw.get("path") or "").strip()
                    try:
                        image_bytes = int(
                            raw.get("frozen_image_bytes")
                            or raw.get("image_bytes")
                            or Path(path).stat().st_size
                        )
                    except (OSError, TypeError, ValueError):
                        image_bytes = 0
                    reason = ""
                    frozen = bool(raw.get("transport_frozen")) and bool(
                        raw.get("frozen_image_data_url")
                    )
                    if not frozen and (not path or not Path(path).is_file()):
                        reason = "image_missing"
                    elif image_bytes <= 0:
                        reason = "image_empty"
                    elif image_bytes > max_single:
                        reason = "image_exceeds_single_byte_budget"
                    else:
                        encoded = estimate_encoded_image_bytes(image_bytes)
                        if encoded_bytes + encoded > max_request:
                            reason = "image_exceeds_request_byte_budget"
                        else:
                            encoded_bytes += encoded
                    if reason:
                        omissions.append(
                            {
                                "visual_id": visual_id,
                                "page_no": int(raw.get("page_no") or 0),
                                "reason": reason,
                                "scope": (
                                    "raw_reinspection"
                                    if group_id
                                    else str(
                                        raw.get("transport_omission_scope")
                                        or "final_transport"
                                    )
                                ),
                                "authority_blocking": False if group_id else True,
                                **(
                                    {
                                        "raw_reinspection_group_id": group_id,
                                        "raw_reinspection_resolution": "not_represented",
                                        "raw_reinspection_fallback_reason": reason,
                                    }
                                    if group_id
                                    else {}
                                ),
                            }
                        )
                    elif visual_id:
                        sent_ids.append(visual_id)
                        if group is not None:
                            actual = group["actual_sent_ids"]
                            if visual_id not in actual:
                                actual.append(visual_id)
                    continue
                if item_type in {"local_pdf_path", "input_file", "file"}:
                    has_file = True
        successful_mode = "multimodal" if sent_ids else "pdf_plus_text" if has_file else "text_only"
        return {
            "planned_visual_ids": planned_ids,
            "sent_visual_ids": sent_ids,
            "omissions": omissions,
            "raw_reinspection_groups": list(raw_group_reports.values()),
            "images_planned_count": len(planned_ids),
            "images_actually_sent_count": len(sent_ids),
            "estimated_encoded_image_bytes": encoded_bytes,
            "max_single_image_bytes": max_single,
            "max_request_image_bytes": max_request,
            "successful_input_mode": successful_mode,
        }

    def _publish_expected_call_graph(self) -> None:
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
                "closure_epoch_id": self.closure_epoch_id,
                "expected_call_graph_hash": self.expected_call_graph_hash,
                "source_bundle_hash": self._expected_source_bundle_hash,
                "runtime_spec_hash": self._expected_runtime_spec_hash,
                "expected_calls": [asdict(item) for item in self.expected_calls],
            },
            artifact_role="provider_expected_call_graph",
            artifact_type="provider_expected_call_graph",
            artifact_version="v1",
            producer="services.stage1_analysis_service.Stage1AnalysisService",
            artifact_id="stage1:provider_expected_call_graph",
            metadata={
                "closure_epoch_id": self.closure_epoch_id,
                "expected_call_graph_hash": self.expected_call_graph_hash,
                "source_bundle_hash": self._expected_source_bundle_hash,
                "runtime_spec_hash": self._expected_runtime_spec_hash,
                "expected_call_count": len(self.expected_calls),
                "reuse_excluded_from_expected_calls": True,
            },
        )
        self.expected_call_graph_path = graph_record.path

    def _refresh_synthesis_expected_call(self, prepared: _PreparedStage1Item) -> None:
        """Bind long-paper synthesis to the post-scan request identity."""

        paper_key = self._paper_key(prepared.item)
        prompt = prepared.built_input.prompt_text
        system_prompt = self.prompt_registry.read("stage1.analysis.system.v3")
        primary_config = self._effective_provider_config(prepared.primary_config)
        backup_config = self._effective_provider_config(prepared.backup_config)
        primary_hash = self._request_hash(
            prompt,
            system_prompt,
            prepared.built_input.user_message_content,
            primary_config,
            response_format="json",
        )
        backup_hash = self._request_hash(
            prompt,
            system_prompt,
            self._text_only_content(prepared.built_input.user_message_content),
            backup_config,
            response_format="json",
            default_max_tokens=8192,
        )
        primary_config_hash = hash_json(_redact_mapping(primary_config))
        backup_config_hash = hash_json(_redact_mapping(backup_config))
        self.expected_calls = tuple(
            replace(
                expected,
                input_hash=primary_hash if expected.call_id == self._synthesis_call_id(paper_key) else expected.input_hash,
                config_hash=primary_config_hash if expected.call_id == self._synthesis_call_id(paper_key) else expected.config_hash,
                prompt_hash=hash_text(prompt) if expected.call_id == self._synthesis_call_id(paper_key) else expected.prompt_hash,
                request_variants=(
                    {"input_hash": primary_hash, "config_hash": primary_config_hash},
                    {"input_hash": backup_hash, "config_hash": backup_config_hash},
                ) if expected.call_id == self._synthesis_call_id(paper_key) else expected.request_variants,
            )
            for expected in self.expected_calls
        )
        self._publish_expected_call_graph()

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
            call_id=self._synthesis_call_id(self._paper_key(item)),
            endpoint_type=str(prepared.primary_config.get("endpoint_type") or "chat_completions"),
            schema_hash=self._schema_hash(),
            prompt_id=prepared.built_input.prompt_id,
            prompt_version=prepared.built_input.prompt_version,
            prompt_sha256=prepared.built_input.prompt_sha256,
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
                visual_evidence_qualification=prior_binding.visual_evidence_qualification,
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

        prepared, visual_coverage, visual_observations = self._run_visual_scans(prepared)
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
            call_id=self._synthesis_call_id(self._paper_key(item)),
            endpoint_type=str(prepared.primary_config.get("endpoint_type") or "chat_completions"),
            schema_hash=self._schema_hash(),
            prompt_id=prepared.built_input.prompt_id,
            prompt_version=prepared.built_input.prompt_version,
            prompt_sha256=prepared.built_input.prompt_sha256,
            closure_epoch_id=self.closure_epoch_id,
            logical_attempt_identity=self.attempt_id,
        )
        if prepared.built_input.visual_scan_batches:
            max_request, max_single = normalize_visual_byte_budgets(
                max_request_image_bytes=prepared.stage1_input_settings.get(
                    "max_request_image_bytes"
                ),
                max_single_image_bytes=prepared.stage1_input_settings.get(
                    "max_single_image_bytes"
                ),
            )
            selection_result = select_final_visual_refs_after_scan(
                prepared.built_input.all_visual_refs
                or prepared.built_input.selected_visual_refs
                or [],
                visual_observations,
                max_refs=max(
                    0,
                    int(prepared.stage1_input_settings.get("final_image_refs_max", 8) or 8),
                ),
                max_request_image_bytes=max_request,
                max_single_image_bytes=max_single,
                return_plan=True,
            )
            if not isinstance(selection_result, tuple):
                raise RuntimeError("visual selector did not return its raw-reinspection plan")
            final_visual_refs, raw_reinspection_units = selection_result
            visual_coverage.update(
                summarize_raw_reinspection_groups(
                    final_visual_refs,
                    planned_units=raw_reinspection_units,
                )
            )
            rebuilt_input = Stage1InputBuilder(logger=self.logger).build(
                prompt_template=self._prompt_template(),
                paper_text=str(prepared.preprocess_metadata.get("stage1_input_text") or ""),
                reader_api_config=prepared.primary_config,
                visual_bundle=prepared.visual_bundle,
                pdf_path=str(item.source_pdf or ""),
                stage1_input_settings=prepared.stage1_input_settings,
                preprocess_metadata=prepared.preprocess_metadata,
                prompt_identity=self._stage1_user_prompt_identity.to_dict(),
                prompt_values={"SUMMARY_SCHEMA_CONTRACT": build_summary_schema_contract()},
                post_scan_visual_refs=final_visual_refs,
                visual_observations=visual_observations,
                visual_coverage=visual_coverage,
            )
            prepared = replace(prepared, built_input=rebuilt_input)

        prepared = self._freeze_synthesis_visual_transport(prepared)
        visual_coverage = {
            **dict(visual_coverage or {}),
            **dict(prepared.built_input.visual_coverage or {}),
        }
        self._refresh_synthesis_expected_call(prepared)
        # Refresh only the current graph artifact provenance.  The reusable
        # input binding remains the immutable pre-scan identity.
        prepared = replace(
            prepared,
            current_binding=self._bind_execution_provenance(prepared.current_binding),
        )

        provider_result = self._call_reader(
            item=item,
            built_input=prepared.built_input,
            primary_config=prepared.primary_config,
            backup_config=prepared.backup_config,
            runtime=runtime,
        )
        engine_type = str(provider_result.get("engine_type") or "primary").strip().lower()
        provider_route = "Backup_Reader_API" if engine_type == "backup" else "Primary_Reader_API"
        provider_config = (
            prepared.backup_config if engine_type == "backup" else prepared.primary_config
        )
        effective_provider_config = self._effective_provider_config(provider_config)
        request_content = (
            self._text_only_content(prepared.built_input.user_message_content)
            if engine_type == "backup"
            else prepared.built_input.user_message_content
        )
        transport_metadata = self._transport_metadata_for_content(
            request_content,
            stage1_input_settings=prepared.stage1_input_settings,
            engine_type=engine_type,
        )
        local_transport_metadata = dict(transport_metadata)
        preflight_report = visual_coverage.get("transport_preflight")
        if isinstance(preflight_report, Mapping):
            preflight_planned_ids = [
                str(value)
                for value in (preflight_report.get("planned_visual_ids") or [])
                if str(value)
            ]
            merged_planned_ids = list(
                dict.fromkeys(
                    [
                        *preflight_planned_ids,
                        *[
                            str(value)
                            for value in (transport_metadata.get("planned_visual_ids") or [])
                            if str(value)
                        ],
                    ]
                )
            )
            transport_metadata["planned_visual_ids"] = merged_planned_ids
            transport_metadata["images_planned_count"] = len(merged_planned_ids)
            transport_metadata["omissions"] = [
                *[
                    dict(item)
                    for item in (preflight_report.get("omissions") or [])
                    if isinstance(item, Mapping)
                ],
                *[
                    dict(item)
                    for item in (transport_metadata.get("omissions") or [])
                    if isinstance(item, Mapping)
                ],
            ]
        reported_transport = provider_result.get("transport_metadata")
        if isinstance(reported_transport, Mapping):
            transport_metadata.update(
                {
                    str(key): value
                    for key, value in reported_transport.items()
                    if value is not None
                }
            )
        # Wire membership is determined by the frozen request content, not by
        # an injected reader's optional report.  Keep the provider report as
        # supplemental metadata, but restore the locally verifiable planned,
        # sent, and omitted visual facts so expected calls, receipts, and the
        # final coverage reducer cannot disagree.
        local_planned_ids = [
            str(value)
            for value in (local_transport_metadata.get("planned_visual_ids") or [])
            if str(value)
        ]
        if isinstance(preflight_report, Mapping):
            local_planned_ids = list(
                dict.fromkeys(
                    [
                        *[
                            str(value)
                            for value in (preflight_report.get("planned_visual_ids") or [])
                            if str(value)
                        ],
                        *local_planned_ids,
                    ]
                )
            )
        transport_metadata["planned_visual_ids"] = local_planned_ids
        transport_metadata["sent_visual_ids"] = [
            str(value)
            for value in (local_transport_metadata.get("sent_visual_ids") or [])
            if str(value)
        ]
        transport_metadata["images_planned_count"] = len(local_planned_ids)
        transport_metadata["images_actually_sent_count"] = len(
            transport_metadata["sent_visual_ids"]
        )
        transport_metadata["omissions"] = [
            *[
                dict(item)
                for item in (
                    (preflight_report.get("omissions") or [])
                    if isinstance(preflight_report, Mapping)
                    else []
                )
                if isinstance(item, Mapping)
            ],
            *[
                dict(item)
                for item in (local_transport_metadata.get("omissions") or [])
                if isinstance(item, Mapping)
            ],
        ]
        transport_metadata["successful_input_mode"] = (
            "multimodal"
            if int(transport_metadata.get("images_actually_sent_count") or 0) > 0
            else str(transport_metadata.get("successful_input_mode") or "text_only")
        )
        transport_metadata.update(
            summarize_raw_reinspection_groups(
                prepared.built_input.selected_visual_refs or [],
                sent_visual_ids=transport_metadata.get("sent_visual_ids") or [],
                planned_units=visual_coverage.get("raw_reinspection_units"),
            )
        )
        provider_result = {
            **dict(provider_result),
            "transport_metadata": transport_metadata,
        }
        max_tokens, temperature = self._request_parameters(
            effective_provider_config,
            default_max_tokens=8192 if engine_type == "backup" else 3000,
        )
        request_payload = canonical_provider_request_payload(
            prompt=prepared.built_input.prompt_text,
            system_prompt=self.prompt_registry.read("stage1.analysis.system.v3"),
            user_content=request_content,
            response_format="json",
            max_output_tokens=max_tokens,
            temperature=temperature,
        )
        self._ensure_receipt(
            runtime,
            prompt=prepared.built_input.prompt_text,
            input_payload=request_payload,
            api_config=effective_provider_config,
            route=provider_route,
            result=provider_result,
        )
        ai_summary = self._canonical_substantive_summary(provider_result)
        coverage = dict(visual_coverage or prepared.built_input.visual_coverage or {})
        actual_visual_ids = [
            str(value)
            for value in (transport_metadata.get("sent_visual_ids") or [])
            if str(value)
        ]
        planned_visual_ids = [
            str(value)
            for value in (coverage.get("planned_visual_ids") or [])
            if str(value)
        ]
        coverage["final_engine_type"] = engine_type
        coverage["final_provider_route"] = provider_route
        coverage["final_successful_input_mode"] = str(
            transport_metadata.get("successful_input_mode") or "text_only"
        )
        coverage["direct_visual_ids"] = actual_visual_ids
        coverage["direct_visual_pages"] = sorted(
            {
                int(ref.get("page_no") or 0)
                for ref in (prepared.built_input.selected_visual_refs or [])
                if str(ref.get("visual_id") or "") in set(actual_visual_ids)
            }
        )
        coverage["images_planned_count"] = int(
            transport_metadata.get("images_planned_count")
            or len(prepared.built_input.selected_visual_refs or [])
        )
        coverage["images_actually_sent_count"] = int(
            transport_metadata.get("images_actually_sent_count") or 0
        )
        coverage["transport_omissions"] = list(transport_metadata.get("omissions") or [])
        coverage.update(
            summarize_raw_reinspection_groups(
                prepared.built_input.selected_visual_refs or [],
                sent_visual_ids=actual_visual_ids,
                planned_units=coverage.get("raw_reinspection_units"),
            )
        )
        required_page_ids = {
            str(value) for value in (coverage.get("required_page_ids") or []) if str(value)
        }
        if not prepared.built_input.visual_scan_batches:
            # Short papers use the final synthesis request as the only visual
            # transport.  That is a raw recheck, not a page scan.
            coverage["sent_visual_ids"] = sorted(set(actual_visual_ids))
            coverage.setdefault("observed_visual_ids", [])
            coverage["scan_coverage_status"] = "not_required"
        scan_status = str(coverage.get("scan_coverage_status") or "not_required")
        successful_mode = str(
            transport_metadata.get("successful_input_mode") or "text_only"
        )
        final_modality = successful_mode if successful_mode in {
            "multimodal", "text_only", "pdf_plus_text"
        } else "text_only"
        coverage["final_synthesis_modality"] = final_modality
        final_omissions = list(transport_metadata.get("omissions") or [])
        raw_units = [
            item
            for item in (coverage.get("raw_reinspection_units") or [])
            if isinstance(item, Mapping)
        ]
        required_raw_unit_count = len(raw_units)
        unresolved_raw_unit_ids = [
            str(item.get("unit_id") or "")
            for item in raw_units
            if item.get("closed") is not True and str(item.get("unit_id") or "")
        ]
        coverage["required_raw_reinspection_unit_count"] = required_raw_unit_count
        coverage["unresolved_raw_reinspection_unit_ids"] = unresolved_raw_unit_ids
        has_planned_visuals = bool(
            coverage.get("planned_visual_ids")
            or prepared.built_input.selected_visual_refs
        )
        if required_raw_unit_count == 0:
            # A backup/text-only route can still have had visual evidence
            # planned.  Preserve that it was not rechecked instead of
            # collapsing it into the genuinely no-visual ``not_required``
            # state.
            raw_recheck_status = (
                "not_run_fallback"
                if has_planned_visuals and final_modality != "multimodal"
                else "not_required"
            )
        elif not unresolved_raw_unit_ids and final_modality == "multimodal" and actual_visual_ids:
            raw_recheck_status = "complete"
        elif engine_type == "backup":
            raw_recheck_status = "not_run_fallback"
        else:
            raw_recheck_status = "partial" if actual_visual_ids else "not_run_fallback"
        coverage["final_raw_visual_recheck_status"] = raw_recheck_status
        raw_recheck_incomplete = bool(
            required_raw_unit_count and unresolved_raw_unit_ids
        )
        scan_incomplete = bool(
            scan_status in {"partial", "failed"}
            or (prepared.built_input.visual_scan_batches and required_page_ids - {
                str(value) for value in (coverage.get("observed_visual_ids") or [])
            })
            or coverage.get("failed_pages")
            or coverage.get("omissions")
        )
        direct_incomplete = bool(
            not prepared.built_input.visual_scan_batches
            and required_page_ids
            and final_modality != "multimodal"
            and engine_type != "backup"
            and final_omissions
        )
        require_complete_visual_coverage = parse_strict_bool(
            prepared.stage1_input_settings.get("require_complete_visual_coverage"),
            field="Stage1_Input.require_complete_visual_coverage",
            default=True,
        )
        if (
            scan_incomplete
            or direct_incomplete
            or (raw_recheck_incomplete and require_complete_visual_coverage)
        ):
            evidence_status = "incomplete"
        elif raw_recheck_incomplete:
            # The explicit relaxed policy applies only after page coverage is
            # complete.  Preserve the unresolved raw unit and omission facts,
            # but classify the final authority as verified degraded evidence.
            evidence_status = "degraded"
        elif not required_page_ids:
            evidence_status = "complete"
        elif final_modality == "multimodal" and not raw_recheck_incomplete and actual_visual_ids:
            evidence_status = "complete"
        else:
            # A successful text-only backup after a complete page scan is a
            # degraded final synthesis, not a failed page scan.
            evidence_status = "degraded"
        coverage["evidence_coverage_status"] = evidence_status
        # ``coverage_status`` is the Registry v1 scan-domain status.  Final
        # synthesis quality is a separate reducer fact and may legitimately be
        # ``degraded`` or ``incomplete`` without invalidating the Registry
        # artifact schema.
        coverage["coverage_status"] = (
            "complete" if scan_status == "not_required" else scan_status
        )
        if evidence_status != "complete":
            quality_audit = dict(ai_summary.get("quality_audit") or {})
            quality_audit["needs_manual_review"] = True
            flags = list(quality_audit.get("conflict_flags") or [])
            flag = (
                "visual_coverage_incomplete"
                if evidence_status == "incomplete"
                else "final_raw_visual_recheck_missing"
            )
            if flag not in flags:
                flags.append(flag)
            quality_audit["conflict_flags"] = flags
            ai_summary["quality_audit"] = quality_audit
        coverage = self._publish_final_visual_coverage(prepared, coverage)
        qualification = self._build_visual_evidence_qualification(prepared, coverage)
        prepared = replace(
            prepared,
            built_input=replace(prepared.built_input, visual_coverage=coverage),
            current_binding=replace(
                prepared.current_binding,
                visual_evidence_qualification=qualification,
            ),
        )
        fallback_reason = str(provider_result.get("fallback_reason") or "").strip()
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
                "route": provider_route,
                "model": str(provider_config.get("model") or ""),
                "receipt_ids": list(self._paper_receipt_ids(self._paper_key(item))),
                "receipt_ledger_path": self.receipt_ledger_path,
                "fallback_reason": fallback_reason,
                "planned_input_mode": prepared.built_input.input_mode,
                "input_mode": str(transport_metadata.get("successful_input_mode") or "text_only"),
                "successful_input_mode": str(transport_metadata.get("successful_input_mode") or "text_only"),
                "images_planned_count": int(transport_metadata.get("images_planned_count") or 0),
                "images_actually_sent_count": int(transport_metadata.get("images_actually_sent_count") or 0),
                "successful_engine": engine_type,
                "transport_omissions": list(transport_metadata.get("omissions") or []),
                "visual_coverage_status": str(coverage.get("coverage_status") or ""),
                "visual_scan_coverage_status": str(
                    coverage.get("scan_coverage_status") or ""
                ),
                "scan_coverage_status": str(coverage.get("scan_coverage_status") or ""),
                "final_synthesis_modality": str(
                    coverage.get("final_synthesis_modality") or ""
                ),
                "final_raw_visual_recheck_status": str(
                    coverage.get("final_raw_visual_recheck_status") or ""
                ),
                "evidence_coverage_status": str(
                    coverage.get("evidence_coverage_status") or ""
                ),
                "raw_reinspection_groups": list(
                    transport_metadata.get("raw_reinspection_groups") or []
                ),
                "ambiguous_candidate_ids": list(
                    transport_metadata.get("ambiguous_candidate_ids") or []
                ),
                "raw_reinspection_resolution": str(
                    transport_metadata.get("raw_reinspection_resolution") or ""
                ),
                "raw_reinspection_selected_ids": list(
                    transport_metadata.get("raw_reinspection_selected_ids") or []
                ),
                "raw_reinspection_fallback_reason": str(
                    transport_metadata.get("raw_reinspection_fallback_reason") or ""
                ),
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
        return summary, self._paper_receipt_ids(self._paper_key(item))

    def _paper_receipt_ids(self, paper_key: str) -> tuple[str, ...]:
        prefix = f"{paper_key}:visual_scan:"
        return tuple(
            receipt.receipt_id
            for receipt in self.receipt_ledger.list_receipts()
            if str(receipt.closure_epoch_id or "") == self.closure_epoch_id
            and (
                receipt.call_id == self._synthesis_call_id(paper_key)
                or str(receipt.call_id or "").startswith(f"stage1_visual_scan:{paper_key}:")
                or str(receipt.node_id or "").startswith(prefix)
            )
        )

    def _run_visual_scans(
        self,
        prepared: _PreparedStage1Item,
    ) -> tuple[_PreparedStage1Item, dict[str, Any], list[dict[str, Any]]]:
        """Scan every sendable page and return substantive observations."""

        paper_key = self._paper_key(prepared.item)
        coverage = dict(prepared.built_input.visual_coverage or {})
        batches = tuple(prepared.built_input.visual_scan_batches or ())
        candidate_batches = tuple(prepared.built_input.visual_scan_candidate_refs or ())
        scan_identity = self.prompt_registry.identity(VISUAL_SCAN_PROMPT_ID)
        coverage.update(
            {
                "visual_observation_artifact_version": VISUAL_OBSERVATIONS_VERSION,
                "visual_scan_prompt_id": scan_identity.prompt_id,
                "visual_scan_prompt_version": scan_identity.version,
                "visual_scan_prompt_sha256": scan_identity.sha256,
                "visual_scan_schema_hash": self._visual_scan_schema_hash(),
            }
        )
        capability = detect_multimodal_capability(prepared.primary_config)
        if not batches:
            self._visual_observation_records[paper_key] = []
            page_status = list(coverage.get("page_status") or [])
            total_pdf_pages = int(coverage.get("total_pdf_pages") or 0)
            nonblank_pages = int(coverage.get("nonblank_pages") or 0)
            rendered_pages = int(coverage.get("rendered_pages") or 0)
            skipped_pages = int(
                coverage.get("skipped_pages")
                or sum(
                    1
                    for item in page_status
                    if isinstance(item, Mapping)
                    and str(item.get("status") or "") == "skipped_blank"
                )
            )
            failed_page_count = int(
                coverage.get("failed_pages")
                or sum(
                    1
                    for item in page_status
                    if isinstance(item, Mapping)
                    and str(item.get("status") or "") in {
                        "render_failed", "scan_failed"
                    }
                )
            )
            raw_units = [
                dict(item)
                for item in (coverage.get("raw_reinspection_units") or [])
                if isinstance(item, Mapping)
            ]
            coverage.update(
                {
                    "total_pdf_pages": total_pdf_pages,
                    "nonblank_pages": nonblank_pages,
                    "rendered_pages": rendered_pages,
                    "visually_scanned_pages": int(
                        coverage.get("visually_scanned_pages") or 0
                    ),
                    "skipped_pages": skipped_pages,
                    "failed_pages": failed_page_count,
                    "page_status": page_status,
                    "scan_batches": list(coverage.get("scan_batches") or []),
                    "scan_coverage_status": "not_required",
                    "coverage_status": "not_required",
                    "final_synthesis_modality": str(
                        coverage.get("final_synthesis_modality") or "text_only"
                    ),
                    "final_raw_visual_recheck_status": str(
                        coverage.get("final_raw_visual_recheck_status")
                        or ("not_required" if not coverage.get("planned_visual_ids") else "not_run_fallback")
                    ),
                    "evidence_coverage_status": str(
                        coverage.get("evidence_coverage_status") or "incomplete"
                    ),
                    "raw_reinspection_units": raw_units,
                    "required_raw_reinspection_unit_count": len(raw_units),
                    "closed_raw_reinspection_unit_count": sum(
                        1 for item in raw_units if item.get("closed") is True
                    ),
                    "unresolved_raw_reinspection_unit_ids": [
                        str(item.get("unit_id") or "")
                        for item in raw_units
                        if item.get("closed") is not True
                        and str(item.get("unit_id") or "")
                    ],
                    "omissions": list(coverage.get("omissions") or []),
                    "observation_artifact_ids": [],
                    "observation_artifact_hashes": [],
                    "observation_artifact_paths": [],
                }
            )
            prepared = replace(
                prepared,
                built_input=replace(
                    prepared.built_input,
                    visual_coverage=coverage,
                ),
            )
            return prepared, coverage, []

        max_request, max_single = normalize_visual_byte_budgets(
            max_request_image_bytes=prepared.stage1_input_settings.get("max_request_image_bytes"),
            max_single_image_bytes=prepared.stage1_input_settings.get("max_single_image_bytes"),
        )

        observation_records: list[ArtifactRecord] = []
        observations: list[dict[str, Any]] = []
        scan_results: list[dict[str, Any]] = []
        sent_visual_ids: set[str] = set()
        observed_visual_ids: set[str] = set()
        failed_pages: set[int] = set()
        omissions: list[dict[str, Any]] = []
        planned_visual_ids = [
            str(value)
            for value in (coverage.get("planned_visual_ids") or [])
            if str(value)
        ]

        for batch_index, raw_refs in enumerate(batches):
            planned_batch = VisualScanBatch(
                batch_index=batch_index,
                visual_refs=tuple(dict(ref) for ref in raw_refs if isinstance(ref, Mapping)),
                child_candidates=tuple(
                    dict(ref)
                    for ref in (
                        candidate_batches[batch_index]
                        if batch_index < len(candidate_batches)
                        else []
                    )
                    if isinstance(ref, Mapping)
                ),
            )
            scan_content, report = build_visual_scan_user_content(
                planned_batch,
                return_report=True,
                max_single_image_bytes=max_single,
                max_request_image_bytes=max_request,
            )
            batch_omissions = [
                dict(item)
                for item in (report.get("omissions") or [])
                if isinstance(item, Mapping)
            ]
            omissions.extend(batch_omissions)
            sent_refs = tuple(
                dict(ref)
                for ref in (report.get("sent_visual_refs") or [])
                if isinstance(ref, Mapping)
            )
            sent_ids = [str(ref.get("visual_id") or "") for ref in sent_refs if str(ref.get("visual_id") or "")]
            sent_visual_ids.update(sent_ids)
            page_nos = sorted({int(ref.get("page_no") or 0) for ref in planned_batch.visual_refs})
            if not capability.supports_image_input:
                omissions.extend(
                    {
                        "visual_id": str(ref.get("visual_id") or ""),
                        "page_no": int(ref.get("page_no") or 0),
                        "reason": "provider_does_not_support_image_input",
                        "scope": "page_coverage",
                        "authority_blocking": True,
                    }
                    for ref in planned_batch.visual_refs
                )
                failed_pages.update(page_nos)
                scan_results.append(
                    {
                        "batch_index": batch_index,
                        "call_id": "",
                        "status": "skipped_no_multimodal_capability",
                        "planned_visual_ids": list(planned_batch.visual_ids),
                        "sent_visual_ids": [],
                        "page_nos": page_nos,
                        "omissions": batch_omissions,
                        "observation_artifact_id": "",
                        "observation_artifact_hash": "",
                    }
                )
                continue
            if not sent_refs:
                failed_pages.update(page_nos)
                scan_results.append(
                    {
                        "batch_index": batch_index,
                        "call_id": "",
                        "status": "skipped_no_sendable_images",
                        "planned_visual_ids": list(planned_batch.visual_ids),
                        "sent_visual_ids": [],
                        "page_nos": page_nos,
                        "omissions": batch_omissions,
                        "observation_artifact_id": "",
                        "observation_artifact_hash": "",
                    }
                )
                continue

            batch = VisualScanBatch(
                batch_index=batch_index,
                visual_refs=sent_refs,
                child_candidates=planned_batch.child_candidates,
            )
            scan_content, effective_report = build_visual_scan_user_content(
                batch,
                return_report=True,
                max_single_image_bytes=max_single,
                max_request_image_bytes=max_request,
            )
            prompt, system_prompt = self._visual_scan_request(prepared, batch)
            effective_config = self._effective_provider_config(prepared.primary_config)
            max_tokens, temperature = self._request_parameters(
                effective_config,
                default_max_tokens=3000,
            )
            input_payload = canonical_provider_request_payload(
                prompt=prompt,
                system_prompt=system_prompt,
                user_content=scan_content,
                response_format="json",
                max_output_tokens=max_tokens,
                temperature=temperature,
            )
            runtime = ProviderRuntime(
                budget=ProviderBudgetV1(
                    max_calls=max(1, self.settings.runtime.node_retry_limit + 1),
                    max_retries_per_call=self.settings.runtime.node_retry_limit,
                ),
                ledger=self.receipt_ledger,
                job_id=self.job_id,
                attempt_id=self.attempt_id,
                stage_name="stage1_analyze",
                route="Primary_Reader_API",
                node_id=f"{paper_key}:visual_scan:{batch_index}",
                call_id=self._visual_scan_call_id(paper_key, batch_index),
                endpoint_type=str(prepared.primary_config.get("endpoint_type") or "chat_completions"),
                schema_hash=self._visual_scan_schema_hash(),
                prompt_id=self.prompt_registry.identity(VISUAL_SCAN_PROMPT_ID).prompt_id,
                prompt_version=self.prompt_registry.identity(VISUAL_SCAN_PROMPT_ID).version,
                prompt_sha256=self.prompt_registry.identity(VISUAL_SCAN_PROMPT_ID).sha256,
                closure_epoch_id=self.closure_epoch_id,
                logical_attempt_identity=self.attempt_id,
            )
            result = self._call_visual_scan(
                prepared=prepared,
                batch=batch,
                prompt=prompt,
                system_prompt=system_prompt,
                user_content=scan_content,
                runtime=runtime,
            )
            self._ensure_receipt(
                runtime,
                prompt=prompt,
                input_payload=input_payload,
                api_config=effective_config,
                route="Primary_Reader_API",
                result=result,
            )
            payload, record, valid = self._publish_visual_observation(
                prepared=prepared,
                batch=batch,
                result=result,
            )
            if record is not None:
                observation_records.append(record)
            batch_observations = [
                dict(item)
                for item in (payload.get("observations") or [])
                if isinstance(item, Mapping)
            ] if valid else []
            observations.extend(batch_observations)
            observed_ids = {
                str(item.get("visual_id") or "")
                for item in batch_observations
                if str(item.get("visual_id") or "")
            }
            observed_visual_ids.update(observed_ids)
            observed_pages = {
                int(item.get("page_no") or 0)
                for item in batch_observations
                if int(item.get("page_no") or 0) > 0
            }
            if not valid:
                failed_pages.update(page_nos)
            else:
                failed_pages.update(set(page_nos) - observed_pages)
            scan_results.append(
                {
                    "batch_index": batch_index,
                    "call_id": runtime.call_id,
                    "status": "success" if valid else "scan_failed",
                    "planned_visual_ids": list(planned_batch.visual_ids),
                    "sent_visual_ids": list(batch.visual_ids),
                    "page_nos": page_nos,
                    "omissions": batch_omissions,
                    "observation_artifact_id": record.artifact_id if record is not None else "",
                    "observation_artifact_hash": record.content_hash if record is not None else "",
                    "error": str(payload.get("error") or "") if not valid else "",
                    "effective_transport": dict(effective_report),
                }
            )

        self._visual_observation_records[paper_key] = observation_records
        observed_pages = {
            int(ref.get("page_no") or 0)
            for ref in (prepared.built_input.all_visual_refs or prepared.built_input.selected_visual_refs or [])
            if str(ref.get("visual_id") or "") in observed_visual_ids
        }
        page_status = []
        for item in coverage.get("page_status") or []:
            if not isinstance(item, Mapping):
                continue
            page = int(item.get("page_no") or 0)
            status = str(item.get("status") or "")
            if status in {"rendered", "scanned"} and page in observed_pages:
                status = "scanned"
            elif status in {"rendered", "scanned"} and page in failed_pages:
                status = "scan_failed"
            page_status.append(
                {
                    "page_no": page,
                    "status": status,
                    "skipped_reason": str(item.get("skipped_reason") or ""),
                }
            )
        nonblank_pages = int(coverage.get("nonblank_pages") or 0)
        failed_count = sum(1 for item in page_status if item.get("status") == "scan_failed")
        scan_status = (
            "complete"
            if nonblank_pages == 0 or (
                len(observed_pages) >= nonblank_pages and failed_count == 0
            )
            else "failed"
            if failed_count and not observed_pages
            else "partial"
        )
        coverage.update(
            {
                "planned_visual_ids": planned_visual_ids,
                "sent_visual_ids": sorted(sent_visual_ids),
                "observed_visual_ids": sorted(observed_visual_ids),
                "visually_scanned_pages": len(observed_pages),
                "failed_pages": failed_count,
                "page_status": page_status,
                "scan_batches": scan_results,
                "observation_artifact_ids": [record.artifact_id for record in observation_records],
                "observation_artifact_hashes": [record.content_hash for record in observation_records],
                "observation_artifact_paths": [record.path for record in observation_records],
                "scan_coverage_status": scan_status,
                "coverage_status": scan_status,
                "final_synthesis_modality": str(
                    coverage.get("final_synthesis_modality") or "text_only"
                ),
                "final_raw_visual_recheck_status": str(
                    coverage.get("final_raw_visual_recheck_status") or "not_required"
                ),
                "evidence_coverage_status": str(
                    coverage.get("evidence_coverage_status") or "incomplete"
                ),
                "raw_reinspection_units": list(
                    coverage.get("raw_reinspection_units") or []
                ),
                "required_raw_reinspection_unit_count": int(
                    coverage.get("required_raw_reinspection_unit_count") or 0
                ),
                "closed_raw_reinspection_unit_count": int(
                    coverage.get("closed_raw_reinspection_unit_count") or 0
                ),
                "unresolved_raw_reinspection_unit_ids": list(
                    coverage.get("unresolved_raw_reinspection_unit_ids") or []
                ),
                "omissions": [
                    *omissions,
                    *[
                        {
                            "visual_id": f"page-{int(item.get('page_no') or 0):03d}",
                            "page_no": int(item.get("page_no") or 0),
                            "reason": str(item.get("skipped_reason") or item.get("status") or ""),
                            "scope": "page_coverage",
                            "authority_blocking": True,
                        }
                        for item in page_status
                        if item.get("status") in {"render_failed", "scan_failed"}
                    ],
                ],
            }
        )
        coverage_record = self._publish_visual_coverage(
            prepared=prepared,
            coverage=coverage,
            observation_records=observation_records,
        )
        prepared = replace(
            prepared,
            built_input=replace(
                prepared.built_input,
                visual_coverage=coverage,
            ),
        )
        return prepared, coverage, observations

    def _build_visual_evidence_qualification(
        self,
        prepared: _PreparedStage1Item,
        coverage: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Reduce scan, transport, and final-modality facts into one typed gate."""

        page_status = [item for item in coverage.get("page_status") or [] if isinstance(item, Mapping)]
        required_page_ids = tuple(
            str(item) for item in (coverage.get("required_page_ids") or []) if str(item)
        )
        required_page_set = set(required_page_ids)
        sent_page_ids = tuple(
            str(item)
            for item in (coverage.get("sent_visual_ids") or [])
            if str(item) in required_page_set
        )
        observed_page_ids = tuple(
            str(item)
            for item in (coverage.get("observed_visual_ids") or [])
            if str(item) in required_page_set
        )
        render_failed_page_ids = tuple(
            str(int(item.get("page_no") or 0))
            for item in page_status
            if str(item.get("status") or "") == "render_failed"
        )
        scan_failed_page_ids = tuple(
            str(int(item.get("page_no") or 0))
            for item in page_status
            if str(item.get("status") or "") == "scan_failed"
        )
        scan_identity = self.prompt_registry.identity(VISUAL_SCAN_PROMPT_ID)
        return Stage1VisualEvidenceQualificationV1(
            coverage_artifact_id=str(coverage.get("coverage_artifact_id") or ""),
            coverage_artifact_hash=str(coverage.get("coverage_artifact_hash") or ""),
            coverage_artifact_path=str(coverage.get("coverage_artifact_path") or ""),
            observation_artifact_ids=tuple(
                str(item) for item in (coverage.get("observation_artifact_ids") or []) if str(item)
            ),
            observation_artifact_hashes=tuple(
                str(item) for item in (coverage.get("observation_artifact_hashes") or []) if str(item)
            ),
            observation_artifact_paths=tuple(
                str(item) for item in (coverage.get("observation_artifact_paths") or []) if str(item)
            ),
            required_nonblank_page_count=int(
                coverage.get("required_nonblank_page_count")
                or coverage.get("nonblank_pages")
                or len(required_page_ids)
                or 0
            ),
            required_page_ids=required_page_ids,
            sent_page_ids=sent_page_ids,
            observed_page_ids=observed_page_ids,
            render_failed_page_ids=render_failed_page_ids,
            scan_failed_page_ids=scan_failed_page_ids,
            transport_omissions=tuple(
                dict(item)
                for item in (
                    list(coverage.get("omissions") or [])
                    + list(coverage.get("transport_omissions") or [])
                )
                if isinstance(item, Mapping)
            ),
            scan_coverage_status=str(coverage.get("scan_coverage_status") or "not_required"),
            final_synthesis_modality=str(
                coverage.get("final_synthesis_modality") or "text_only"
            ),
            final_raw_visual_recheck_status=str(
                coverage.get("final_raw_visual_recheck_status") or "not_required"
            ),
            evidence_coverage_status=str(
                coverage.get("evidence_coverage_status") or "incomplete"
            ),
            required_raw_reinspection_unit_count=int(
                coverage.get("required_raw_reinspection_unit_count") or 0
            ),
            closed_raw_reinspection_unit_count=int(
                coverage.get("closed_raw_reinspection_unit_count") or 0
            ),
            unresolved_raw_reinspection_unit_ids=tuple(
                str(item)
                for item in (coverage.get("unresolved_raw_reinspection_unit_ids") or [])
                if str(item)
            ),
            raw_reinspection_units=tuple(
                dict(item)
                for item in (coverage.get("raw_reinspection_units") or [])
                if isinstance(item, Mapping)
            ),
            require_complete_visual_coverage=parse_strict_bool(
                prepared.stage1_input_settings.get("require_complete_visual_coverage"),
                field="Stage1_Input.require_complete_visual_coverage",
                default=True,
            ),
            visual_observation_artifact_version=str(
                coverage.get("visual_observation_artifact_version")
                or VISUAL_OBSERVATIONS_VERSION
            ),
            visual_scan_prompt_id=str(
                coverage.get("visual_scan_prompt_id") or scan_identity.prompt_id
            ),
            visual_scan_prompt_version=str(
                coverage.get("visual_scan_prompt_version") or scan_identity.version
            ),
            visual_scan_prompt_sha256=str(
                coverage.get("visual_scan_prompt_sha256") or scan_identity.sha256
            ),
            visual_scan_schema_hash=str(
                coverage.get("visual_scan_schema_hash") or self._visual_scan_schema_hash()
            ),
        ).to_dict()

    def _publish_final_visual_coverage(
        self,
        prepared: _PreparedStage1Item,
        coverage: dict[str, Any],
    ) -> dict[str, Any]:
        """Publish the final reducer state after synthesis transport is known."""

        paper_key = self._paper_key(prepared.item)
        record = self._publish_visual_coverage(
            prepared=prepared,
            coverage=coverage,
            observation_records=self._visual_observation_records.get(paper_key, []),
        )
        coverage["coverage_artifact_id"] = record.artifact_id
        coverage["coverage_artifact_path"] = record.path
        coverage["coverage_artifact_hash"] = record.content_hash
        try:
            published_payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("final visual coverage readback failed") from exc
        if not isinstance(published_payload, Mapping):
            raise RuntimeError("final visual coverage readback is not an object")
        for field_name in (
            "coverage_status",
            "scan_coverage_status",
            "final_synthesis_modality",
            "final_raw_visual_recheck_status",
            "evidence_coverage_status",
            "raw_reinspection_units",
            "required_raw_reinspection_unit_count",
            "closed_raw_reinspection_unit_count",
            "unresolved_raw_reinspection_unit_ids",
            "omissions",
            "transport_omissions",
        ):
            if hash_json(published_payload.get(field_name)) != hash_json(coverage.get(field_name)):
                raise RuntimeError(
                    f"final visual coverage readback mismatch: {field_name}"
                )
        self._visual_coverage_records[paper_key] = record
        return coverage

    def _call_visual_scan(
        self,
        *,
        prepared: _PreparedStage1Item,
        batch: VisualScanBatch,
        prompt: str,
        system_prompt: str,
        user_content: Any,
        runtime: ProviderRuntime,
    ) -> Mapping[str, Any]:
        if self.reader is not None:
            value = self.reader(
                purpose="visual_scan",
                prompt_text=prompt,
                system_prompt=system_prompt,
                primary_api_config=dict(prepared.primary_config),
                backup_api_config=dict(prepared.backup_config),
                user_content=user_content,
                provider_runtime=runtime,
                paper_info=dict(prepared.item.paper_info),
                visual_scan_batch=batch.to_dict(),
            )
        else:
            from ai_interface import get_summary_from_ai_detailed

            value = get_summary_from_ai_detailed(
                prompt,
                cast(APIConfig, dict(prepared.primary_config)),
                cast(APIConfig, dict(prepared.backup_config)),
                engine_type="primary",
                logger=self.logger,
                config=self.config,
                user_content=user_content,
                retry_attempts=1,
                provider_runtime=runtime,
                system_prompt=system_prompt,
                normalize_summary=False,
                max_single_image_bytes=prepared.stage1_input_settings.get("max_single_image_bytes"),
                max_request_image_bytes=prepared.stage1_input_settings.get("max_request_image_bytes"),
            )
        if not isinstance(value, Mapping):
            return {
                "status": "failed",
                "error_kind": "invalid_response",
                "message": "visual scan returned a non-object",
            }
        return dict(value)

    def _publish_visual_observation(
        self,
        *,
        prepared: _PreparedStage1Item,
        batch: VisualScanBatch,
        result: Mapping[str, Any],
    ) -> tuple[dict[str, Any], ArtifactRecord | None, bool]:
        content = result.get("content")
        valid = False
        error = ""
        if str(result.get("status") or "").strip().lower() == "success" and isinstance(content, Mapping):
            try:
                content = validate_current_visual_observations_v2(
                    content,
                    allowed_visual_ids=batch.visual_ids,
                    expected_visual_refs=batch.visual_refs,
                    sent_visual_ids=batch.visual_ids,
                    candidate_refs=batch.child_candidates,
                )
                valid = True
            except ValueError as exc:
                error = str(exc)
        else:
            error = str(result.get("message") or result.get("error_kind") or "visual_scan_failed")
        payload = {
            "artifact_type": "stage1_visual_observations",
            "artifact_version": VISUAL_OBSERVATIONS_VERSION,
            "job_id": self.job_id,
            "paper_key": self._paper_key(prepared.item),
            "batch_index": int(batch.batch_index),
            "call_id": self._visual_scan_call_id(self._paper_key(prepared.item), batch.batch_index),
            "prompt_id": self.prompt_registry.identity(VISUAL_SCAN_PROMPT_ID).prompt_id,
            "prompt_version": self.prompt_registry.identity(VISUAL_SCAN_PROMPT_ID).version,
            "prompt_sha256": self.prompt_registry.identity(VISUAL_SCAN_PROMPT_ID).sha256,
            "visual_ids": list(batch.visual_ids),
            "child_candidate_ids": list(batch.child_candidate_ids),
            # Persist only the metadata needed to revalidate attribution at
            # the Registry boundary; never persist local image paths here.
            "child_candidate_refs": [
                {
                    "visual_id": str(ref.get("visual_id") or ""),
                    "page_no": int(ref.get("page_no") or 0),
                    "artifact_type": str(ref.get("artifact_type") or ""),
                    "bbox": list(ref.get("bbox") or []),
                }
                for ref in batch.child_candidates
                if isinstance(ref, Mapping) and str(ref.get("visual_id") or "")
            ],
            "schema_hash": self._visual_scan_schema_hash(),
            "status": "success" if valid else "failed",
            # Only persist observations after the complete v2 contract has
            # passed.  A failed provider response is diagnostic evidence, not
            # a partially trusted observation artifact.
            "observations": (
                list(content.get("observations") or [])
                if valid and isinstance(content, Mapping)
                else []
            ),
            "error": error,
        }
        manifest_record = self._record_for_path(prepared.built_input.visual_manifest_path)
        dependencies = [ArtifactDependencyRefV2.from_record(manifest_record)] if manifest_record is not None else []
        try:
            record = publish_json_artifact(
                self.publication_context,
                self.registry,
                self._visual_observation_path(self._paper_key(prepared.item), batch.batch_index),
                payload,
                artifact_role="stage1_visual_observations",
                artifact_type="stage1_visual_observations",
                artifact_version=VISUAL_OBSERVATIONS_VERSION,
                producer="services.stage1_analysis_service.Stage1AnalysisService",
                artifact_id=self._visual_scan_call_id(self._paper_key(prepared.item), batch.batch_index),
                depends_on=dependencies,
            )
        except (OSError, RegistryError, TypeError, ValueError) as exc:
            payload["status"] = "failed"
            payload["error"] = f"observation_publish_failed:{exc}"
            return payload, None, False
        return payload, record, valid

    def _publish_visual_coverage(
        self,
        *,
        prepared: _PreparedStage1Item,
        coverage: Mapping[str, Any],
        observation_records: Sequence[ArtifactRecord],
    ) -> ArtifactRecord:
        payload = {
            "artifact_type": "stage1_visual_coverage",
            "artifact_version": "v1",
            "job_id": self.job_id,
            "paper_key": self._paper_key(prepared.item),
            **dict(coverage),
        }
        # The artifact identity belongs to the Registry record being created.
        # Never embed a previous publication's identity in the new payload:
        # doing so makes the final reducer self-reference stale bytes and
        # weakens the typed reuse qualification.
        for key in (
            "coverage_artifact_id",
            "coverage_artifact_path",
            "coverage_artifact_hash",
        ):
            payload.pop(key, None)
        digest = hash_json(payload)
        manifest_record = self._record_for_path(prepared.built_input.visual_manifest_path)
        dependencies = [ArtifactDependencyRefV2.from_record(manifest_record)] if manifest_record is not None else []
        dependencies.extend(ArtifactDependencyRefV2.from_record(record) for record in observation_records)
        artifact_id = f"stage1_visual_coverage:final:{digest[:24]}"
        artifact_path = self.workspace.artifact_path(f"stage1_visuals/coverage_{digest[:24]}.json")
        # A previous run may have left this deterministic identity pointing at
        # bytes that were later tampered with or deleted.  Do not silently
        # reuse that Registry record; publish a fresh, traceable repair
        # identity so the new provider run can close its dependency graph.
        existing = self.registry.get(artifact_id)
        if existing is not None:
            try:
                existing_is_intact = (
                    existing.status == "ready"
                    and Path(existing.path).is_file()
                    and file_sha256(existing.path) == existing.content_hash
                )
            except (OSError, TypeError, ValueError):
                existing_is_intact = False
            if not existing_is_intact:
                try:
                    existing_file_hash = file_sha256(existing.path)
                except (OSError, TypeError, ValueError):
                    existing_file_hash = ""
                repair_suffix = hashlib.sha256(
                    f"{artifact_id}|{existing.content_hash}|{existing_file_hash}|"
                    f"{len(self.registry.list_records())}".encode("utf-8")
                ).hexdigest()[:12]
                artifact_id = f"{artifact_id}:repair:{repair_suffix}"
                artifact_path = self.workspace.artifact_path(
                    f"stage1_visuals/coverage_{digest[:24]}_repair_{repair_suffix}.json"
                )
        try:
            return publish_json_artifact(
                self.publication_context,
                self.registry,
                artifact_path,
                payload,
                artifact_role="stage1_visual_coverage",
                artifact_type="stage1_visual_coverage",
                artifact_version="v1",
                producer="services.stage1_analysis_service.Stage1AnalysisService",
                artifact_id=artifact_id,
                depends_on=dependencies,
            )
        except (OSError, RegistryError, TypeError, ValueError) as exc:
            raise RuntimeError("visual coverage publication failed") from exc

    def _record_for_path(self, path: str) -> ArtifactRecord | None:
        normalized = str(Path(path).expanduser().resolve()).casefold() if path else ""
        if not normalized:
            return None
        for record in self.registry.list_records():
            try:
                if str(Path(record.path).resolve()).casefold() == normalized and record.status == "ready":
                    return record
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _coverage_identity_hash(coverage: Mapping[str, Any]) -> str:
        normalized = dict(coverage)
        normalized.pop("coverage_artifact_id", None)
        normalized.pop("coverage_artifact_path", None)
        normalized.pop("coverage_artifact_hash", None)
        return hash_json(normalized)

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
            visual_record = (
                self.registry.get(expected.call_id)
                if str(expected.call_id).startswith("stage1_visual_scan:")
                else None
            )
            output_record = source_record or visual_record or paper_record
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
                        if envelope.get("artifact_type") == "stage1_visual_observations":
                            payload_hash = hash_json(
                                {
                                    "artifact_type": envelope.get("artifact_type"),
                                    "artifact_version": envelope.get("artifact_version"),
                                    "observations": envelope.get("observations") or [],
                                }
                            )
                        else:
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
        primary_config = dict(self.settings.section("Primary_Reader_API"))
        built_input = Stage1InputBuilder(logger=self.logger).build(
            prompt_template=self._prompt_template(),
            paper_text=preprocess.stage1_input_text,
            reader_api_config=primary_config,
            visual_bundle=visual_bundle,
            pdf_path=source_pdf,
            stage1_input_settings=stage1_settings,
            preprocess_metadata=preprocess_metadata,
            prompt_identity=self._stage1_user_prompt_identity.to_dict(),
            prompt_values={"SUMMARY_SCHEMA_CONTRACT": build_summary_schema_contract()},
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
            call_id=self._synthesis_call_id(self._paper_key(item)),
            endpoint_type=str(primary_config.get("endpoint_type") or "chat_completions"),
            schema_hash=self._schema_hash(),
            prompt_id=built_input.prompt_id,
            prompt_version=built_input.prompt_version,
            prompt_sha256=built_input.prompt_sha256,
        )
        provider_result = self._call_reader(
            item=item,
            built_input=built_input,
            primary_config=primary_config,
            backup_config=dict(self.settings.section("Backup_Reader_API")),
            runtime=runtime,
        )
        legacy_engine = str(provider_result.get("engine_type") or "primary").strip().lower()
        legacy_config = self._effective_provider_config(
            dict(self.settings.section("Backup_Reader_API"))
            if legacy_engine == "backup"
            else primary_config
        )
        legacy_content = (
            self._text_only_content(built_input.user_message_content)
            if legacy_engine == "backup"
            else built_input.user_message_content
        )
        legacy_max_tokens, legacy_temperature = self._request_parameters(
            legacy_config,
            default_max_tokens=8192 if legacy_engine == "backup" else 3000,
        )
        self._ensure_receipt(
            runtime,
            prompt=built_input.prompt_text,
            input_payload=canonical_provider_request_payload(
                prompt=built_input.prompt_text,
                system_prompt=self.prompt_registry.read("stage1.analysis.system.v3"),
                user_content=legacy_content,
                response_format="json",
                max_output_tokens=legacy_max_tokens,
                temperature=legacy_temperature,
            ),
            api_config=legacy_config,
            route="Backup_Reader_API" if legacy_engine == "backup" else "Primary_Reader_API",
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
            visual_settings=visual_settings,
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
                system_prompt=self.prompt_registry.read("stage1.analysis.system.v3"),
                primary_api_config=dict(primary_config),
                backup_api_config=dict(backup_config),
                user_content=built_input.user_message_content,
                provider_runtime=runtime,
                paper_info=dict(item.paper_info),
            )
        else:
            from ai_interface import get_summary_from_ai_with_fallback

            visual_coverage = built_input.visual_coverage or {}
            value = get_summary_from_ai_with_fallback(
                built_input.prompt_text,
                cast(APIConfig, dict(primary_config)),
                cast(APIConfig, dict(backup_config)),
                logger=self.logger,
                config=self.config,
                user_content=built_input.user_message_content,
                return_detailed=True,
                provider_runtime=runtime,
                system_prompt=self.prompt_registry.read("stage1.analysis.system.v3"),
                max_single_image_bytes=visual_coverage.get("max_single_image_bytes"),
                max_request_image_bytes=visual_coverage.get("max_request_image_bytes"),
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
        route: str,
        result: Mapping[str, Any],
    ) -> None:
        if runtime.receipts:
            return
        try:
            admission = runtime.admit(estimated_tokens=max(1, len(prompt) // 4))
            receipt_metadata: dict[str, Any] = {
                "execution_mode": "injected_reader",
            }
            transport_metadata = result.get("transport_metadata")
            if isinstance(transport_metadata, Mapping):
                receipt_metadata.update(
                    {str(key): value for key, value in transport_metadata.items()}
                )
            runtime.complete(
                admission=admission,
                prompt=prompt,
                input_payload=input_payload,
                api_config=api_config,
                result=result,
                route=route,
                metadata=receipt_metadata,
            )
        except ProviderBudgetExceeded:
            runtime.blocked_receipt(
                prompt=prompt,
                input_payload=input_payload,
                api_config=api_config,
                message="Stage 1 reader did not produce a provider receipt before its budget closed",
                route=route,
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
        return PromptRegistry().read("stage1.analysis.user.v3")

    @staticmethod
    def _schema_hash() -> str:
        payload = {
            "schema": "summary_v2_lite",
            "stage": "stage1_analyze",
            "contract": build_summary_schema_contract(),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


__all__ = ["Stage1AnalysisResult", "Stage1AnalysisService"]

"""Explicit validation-stage service and provider receipt boundary.

The runtime constructs this service from current durable records.  The old
validator module remains a compatibility pipeline for existing callers, but
it receives only the private adapter below; production orchestration does not
pass a generator-shaped object through its public boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from runtime.provider_receipt_closure import (
    ExpectedProviderCall,
    ProviderReceiptClosure,
    ReceiptClosureResult,
)
from runtime.provider_runtime import (
    ProviderBudgetV1,
    ProviderRuntime,
    ProviderRuntimeLedger,
    _redact_mapping,
    hash_json,
    hash_text,
)
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRecord, ArtifactRegistry
from services.job_workspace import JobWorkspace, atomic_write_json


@dataclass
class ValidationExecutionService:
    """Current validation execution contract.

    Every field is explicit so validation can be reconstructed from a job
    workspace without reviving the historical review-generator object.
    """

    job_id: str
    attempt_id: str
    workspace: JobWorkspace
    artifact_registry: ArtifactRegistry
    settings: Any
    summaries: list[dict[str, Any]]
    review_draft_record: ArtifactRecord | None
    citation_manifest_record: ArtifactRecord | None
    paper_artifact_records: Sequence[ArtifactRecord]
    visual_artifact_records: Sequence[ArtifactRecord]
    provider_factory: Callable[..., ProviderRuntime] | None
    cancellation_checker: Callable[[], None] | None
    logger: Any
    runtime_config: Mapping[str, Any] = field(default_factory=dict)
    validation_external_registry_resolver: Callable[[str], Any | None] | None = None
    _provider_receipt_ledger: ProviderRuntimeLedger | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _expected_provider_calls: dict[str, ExpectedProviderCall] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _provider_runtimes: dict[str, ProviderRuntime] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self.job_id = str(self.job_id or self.workspace.job_id)
        self.attempt_id = str(self.attempt_id or "validation")
        self.summaries = [dict(item) for item in self.summaries if isinstance(item, Mapping)]
        self.paper_artifact_records = tuple(self.paper_artifact_records or ())
        self.visual_artifact_records = tuple(self.visual_artifact_records or ())

    @property
    def project_name(self) -> str:
        return str(self.workspace.project_name)

    @property
    def review_draft_path(self) -> str:
        return str(
            self.review_draft_record.path
            if self.review_draft_record is not None
            else self.workspace.artifact_path("review_draft.json")
        )

    @property
    def citation_manifest_path(self) -> str:
        return str(
            self.citation_manifest_record.path
            if self.citation_manifest_record is not None
            else self.workspace.artifact_path("citation_manifest_v3.json")
        )

    @property
    def review_word_path(self) -> str:
        return self.workspace.artifact_path(f"{self.project_name}_literature_review.docx")

    def stage2_validation_enabled(self) -> bool:
        checker = getattr(self.settings, "review_validation_enabled", None)
        return bool(checker() if callable(checker) else getattr(getattr(self.settings, "validation", None), "review_enabled", True))

    @staticmethod
    def get_paper_key(paper: Mapping[str, Any]) -> str:
        return str(
            paper.get("canonical_paper_key")
            or paper.get("source_paper_id")
            or paper.get("title")
            or "unknown-paper"
        ).strip()

    @property
    def provider_receipt_ledger(self) -> ProviderRuntimeLedger:
        """Return the append-only validation receipt ledger for this attempt."""

        if self._provider_receipt_ledger is None:
            self._provider_receipt_ledger = ProviderRuntimeLedger(
                self.workspace.artifact_path("validation_provider_receipts.jsonl")
            )
        return self._provider_receipt_ledger

    def new_provider_runtime(
        self,
        *,
        stage_name: str,
        route: str,
        node_id: str,
        call_id: str,
        api_config: Mapping[str, Any],
        schema_hash: str | None = None,
    ) -> ProviderRuntime:
        """Register one expected validation provider call before transport."""

        if self.cancellation_checker is not None:
            self.cancellation_checker()
        config = dict(api_config or {})
        resolved_stage = str(stage_name or "stage4_validate").strip()
        resolved_route = str(route or "Validator_API").strip()
        resolved_node = str(node_id or "validation-node").strip()
        resolved_call = str(call_id or "").strip()
        if not resolved_call:
            raise ValueError("validation provider call_id is required")
        runtime_settings = getattr(self.settings, "runtime", None)
        try:
            retry_limit = max(0, int(getattr(runtime_settings, "validation_retry_limit", 1)))
        except (TypeError, ValueError):
            retry_limit = 1
        resolved_schema_hash = schema_hash or hashlib.sha256(
            json.dumps(
                {
                    "stage_name": resolved_stage,
                    "route": resolved_route,
                    "node_id": resolved_node,
                    "response_format": "json",
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        endpoint_type = str(config.get("endpoint_type") or "chat_completions")
        if self.provider_factory is not None:
            runtime = self.provider_factory(
                budget=ProviderBudgetV1(max_calls=1, max_retries_per_call=retry_limit),
                ledger=self.provider_receipt_ledger,
                job_id=self.job_id,
                attempt_id=self.attempt_id,
                stage_name=resolved_stage,
                route=resolved_route,
                node_id=resolved_node,
                call_id=resolved_call,
                endpoint_type=endpoint_type,
                schema_hash=resolved_schema_hash,
            )
        else:
            runtime = ProviderRuntime(
                budget=ProviderBudgetV1(max_calls=1, max_retries_per_call=retry_limit),
                ledger=self.provider_receipt_ledger,
                job_id=self.job_id,
                attempt_id=self.attempt_id,
                stage_name=resolved_stage,
                route=resolved_route,
                node_id=resolved_node,
                call_id=resolved_call,
                endpoint_type=endpoint_type,
                schema_hash=resolved_schema_hash,
            )
        if not isinstance(runtime, ProviderRuntime):
            raise TypeError("provider_factory must return ProviderRuntime")
        self._expected_provider_calls[resolved_call] = ExpectedProviderCall(
            call_id=resolved_call,
            job_id=self.job_id,
            attempt_id=self.attempt_id,
            stage_name=resolved_stage,
            node_id=resolved_node,
            max_attempts=max(1, retry_limit + 1),
            usage_required=endpoint_type not in {"internal", "fixture"},
        )
        self._provider_runtimes[resolved_call] = runtime
        return runtime

    def bind_provider_call(
        self,
        *,
        call_id: str,
        prompt: str,
        input_payload: Any,
        api_config: Mapping[str, Any],
        schema_hash: str,
    ) -> None:
        """Bind the exact pre-transport prompt/input/config/schema identity."""

        expected = self._expected_provider_calls.get(str(call_id))
        if expected is None:
            raise RuntimeError(f"provider call was not admitted before binding: {call_id}")
        self._expected_provider_calls[str(call_id)] = replace(
            expected,
            prompt_hash=hash_text(prompt),
            input_hash=hash_json(input_payload),
            config_hash=hash_json(_redact_mapping(dict(api_config))),
            schema_hash=str(schema_hash),
        )

    def bind_provider_output(self, *, call_id: str, content: Any) -> None:
        """Close normalized/registered/node output hashes after transport."""

        expected = self._expected_provider_calls.get(str(call_id))
        if expected is None:
            raise RuntimeError(f"provider output has no expected call: {call_id}")
        normalized_hash = hash_json(content)
        self._expected_provider_calls[str(call_id)] = replace(
            expected,
            output_hash=normalized_hash,
            normalized_output_hash=normalized_hash,
            registered_artifact_hash=normalized_hash,
            node_output_hash=normalized_hash,
        )

    def persist_summaries(self) -> bool:
        path = self.workspace.artifact_path(f"{self.project_name}_summaries.json")
        atomic_write_json(path, self.summaries)
        self.artifact_registry.register_file(
            artifact_role="summary",
            artifact_type="summary_file",
            artifact_version="v1",
            path=path,
            producer="validation.execution_service.ValidationExecutionService",
            artifact_id="summary_file",
        )
        return True

    def persist_paper_artifact(self, result: Mapping[str, Any]) -> bool:
        from services.paper_artifact import build_paper_artifact_v1

        paper = result.get("paper_info")
        if not isinstance(paper, Mapping):
            return False
        paper_key = self.get_paper_key(paper)
        digest = hashlib.sha256(paper_key.encode("utf-8")).hexdigest()[:24]
        path = self.workspace.artifact_path(f"paper_artifacts/paper_{digest}.json")
        artifact = build_paper_artifact_v1(
            job_id=self.job_id,
            paper=paper,
            result=result,
            paper_key=paper_key,
        )
        atomic_write_json(path, artifact.to_dict())
        self.artifact_registry.register_file(
            artifact_role="paper_summary",
            artifact_type="paper_artifact",
            artifact_version="v1",
            path=path,
            producer="validation.execution_service.ValidationExecutionService",
            artifact_id=f"paper:{digest}",
        )
        return True

    def persist_citation_manifest(
        self,
        *,
        review_draft_path: str,
        review_word_path: str,
        citation_ref_catalog: Mapping[str, Any] | None = None,
        citation_ref_catalog_path: str = "",
        citation_ref_catalog_hash: str = "",
    ) -> bool:
        from services.citation_manifest import build_citation_manifest_from_review_draft

        review_draft = json.loads(Path(review_draft_path).read_text(encoding="utf-8"))
        manifest = build_citation_manifest_from_review_draft(
            job_id=self.job_id,
            project_name=self.project_name,
            manifest_id="citation_manifest",
            review_draft_path=review_draft_path,
            review_word_path=review_word_path,
            review_draft=review_draft,
            paper_summaries=list(self.summaries),
            citation_ref_catalog=dict(citation_ref_catalog or {}) or None,
            citation_ref_catalog_path=citation_ref_catalog_path,
            citation_ref_catalog_hash=citation_ref_catalog_hash,
        )
        path = self.citation_manifest_path
        atomic_write_json(path, manifest.to_dict())
        self.artifact_registry.register_file(
            artifact_role="citation_manifest",
            artifact_type="citation_manifest",
            artifact_version="v3",
            path=path,
            producer="validation.execution_service.ValidationExecutionService",
            artifact_id="citation_manifest_v3",
            depends_on=[
                ArtifactDependencyRefV2(
                    dependency_kind="local_job",
                    job_id=self.job_id,
                    artifact_id="review_draft",
                    artifact_type="review_draft",
                    path=review_draft_path,
                    content_hash=self.review_draft_record.content_hash if self.review_draft_record else "",
                )
            ],
        )
        return True

    def rebuild_review_docx(self, review_draft: Mapping[str, Any], citation_manifest: Mapping[str, Any], output_path: str) -> None:
        from docx_writer import rebuild_review_docx_from_structured_artifacts

        rebuild_review_docx_from_structured_artifacts(
            _LegacyValidationHost(self),
            dict(review_draft),
            dict(citation_manifest),
            output_path,
        )

    def finalize_provider_receipts(self) -> dict[str, Any]:
        """Persist receipt ledger and evaluate against pre-transport expectations."""

        receipts = list(self.provider_receipt_ledger.list_receipts())
        expected_calls = list(self._expected_provider_calls.values())
        expected_ids = {item.call_id for item in expected_calls}
        closure: ReceiptClosureResult = ProviderReceiptClosure.evaluate(
            expected_calls,
            [receipt for receipt in receipts if receipt.call_id in expected_ids],
        )
        ledger_record = None
        ledger_path = self.provider_receipt_ledger.path
        if ledger_path.is_file():
            ledger_record = self.artifact_registry.register_file(
                artifact_role="provider_receipts",
                artifact_type="provider_receipt_ledger",
                artifact_version="v1",
                path=str(ledger_path),
                producer="validation.execution_service.ValidationExecutionService",
                artifact_id="validation_provider_receipts",
                metadata={"receipt_count": len(receipts)},
            )
        closure_path = self.workspace.artifact_path("validation_provider_receipt_closure.json")
        atomic_write_json(
            closure_path,
            {
                "job_id": self.job_id,
                "attempt_id": self.attempt_id,
                "payload": closure.to_dict(),
            },
        )
        dependencies: list[ArtifactDependencyRefV2] = []
        if ledger_record is not None and ledger_record.status == "ready":
            dependencies.append(ArtifactDependencyRefV2.from_record(ledger_record))
        closure_record = self.artifact_registry.register_file(
            artifact_role="provider_receipt_closure",
            artifact_type="provider_receipt_closure",
            artifact_version="v1",
            path=closure_path,
            producer="validation.execution_service.ValidationExecutionService",
            artifact_id="validation:provider_receipt_closure",
            depends_on=dependencies,
            metadata={"closure_hash": closure.closure_hash, "complete": closure.complete},
        )
        return {
            "ledger": ledger_record,
            "closure": closure,
            "closure_record": closure_record,
            "expected_call_ids": tuple(sorted(expected_ids)),
        }

    def run_review_validation(self) -> dict[str, Any]:
        """Run the current validation pipeline through the explicit service."""

        from validation.review_validation_pipeline import run_current_review_validation

        return run_current_review_validation(_LegacyValidationHost(self))


class _LegacyValidationHost:
    """Private compatibility surface for the still-large validation engine."""

    def __init__(self, service: ValidationExecutionService) -> None:
        self._service = service
        self.logger = service.logger
        self.config = service.runtime_config
        self.settings = service.settings
        self.project_name = service.project_name
        self.output_dir = service.workspace.base_output_dir
        self.job_workspace = service.workspace
        self.artifact_registry = service.artifact_registry
        self.summaries = service.summaries
        self.validation_attempt_id = service.attempt_id
        self.validation_external_registry_resolver = service.validation_external_registry_resolver

    def _review_draft_path(self) -> str:
        return self._service.review_draft_path

    def _citation_manifest_path(self) -> str:
        return self._service.citation_manifest_path

    def _get_review_word_file_path(self) -> str:
        return self._service.review_word_path

    def _stage2_validation_enabled(self) -> bool:
        return self._service.stage2_validation_enabled()

    def _persist_citation_manifest(self, *args: Any, **kwargs: Any) -> bool:
        return self._service.persist_citation_manifest(*args, **kwargs)

    def save_summaries(self) -> bool:
        return self._service.persist_summaries()

    def _persist_paper_artifact(self, result: Mapping[str, Any]) -> bool:
        return self._service.persist_paper_artifact(result)

    def get_paper_key(self, paper: Mapping[str, Any]) -> str:
        return self._service.get_paper_key(paper)

    def new_provider_runtime(self, **kwargs: Any) -> ProviderRuntime:
        return self._service.new_provider_runtime(**kwargs)

    def bind_provider_call(self, **kwargs: Any) -> None:
        self._service.bind_provider_call(**kwargs)

    def bind_provider_output(self, **kwargs: Any) -> None:
        self._service.bind_provider_output(**kwargs)

    def finalize_provider_receipts(self) -> dict[str, Any]:
        return self._service.finalize_provider_receipts()


__all__ = ["ValidationExecutionService"]

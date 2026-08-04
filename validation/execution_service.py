"""Explicit current validation-stage service and provider boundary.

The service is reconstructed from durable Registry records and owns the
validation sequence.  The historical ``validator`` module is deliberately
not imported here; it remains an external compatibility shim only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
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
    compute_closure_epoch_id,
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
    closure_epoch_id: str = field(default="", init=False)
    expected_call_graph_hash: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.job_id = str(self.job_id or self.workspace.job_id)
        self.attempt_id = str(self.attempt_id or "validation")
        self.summaries = [dict(item) for item in self.summaries if isinstance(item, Mapping)]
        self.paper_artifact_records = tuple(self.paper_artifact_records or ())
        self.visual_artifact_records = tuple(self.visual_artifact_records or ())
        input_hashes = {
            "review_draft": self.review_draft_record.content_hash if self.review_draft_record else "",
            "citation_manifest": self.citation_manifest_record.content_hash if self.citation_manifest_record else "",
            "papers": hash_json([record.content_hash for record in self.paper_artifact_records]),
            "visuals": hash_json([record.content_hash for record in self.visual_artifact_records]),
        }
        self.expected_call_graph_hash = hash_json(
            {
                "stage_name": "stage4_validate",
                "schema": "validation-v1",
                "call_id_pattern": "validation:{packet_stage}:{packet_hash}",
            }
        )
        self.closure_epoch_id = compute_closure_epoch_id(
            job_id=self.job_id,
            stage_name="stage4_validate",
            logical_attempt_identity=self.attempt_id,
            expected_call_graph_hash=self.expected_call_graph_hash,
            current_input_artifact_hashes=input_hashes,
            provider_config_hash=hash_json(_redact_mapping(dict(self.runtime_config or {}))),
            schema_version="validation-v1",
        )

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
            self._provider_receipt_ledger = ProviderRuntimeLedger.for_epoch(
                self.workspace.root_dir,
                stage_name="stage4_validate",
                closure_epoch_id=self.closure_epoch_id,
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
                closure_epoch_id=self.closure_epoch_id,
                logical_attempt_identity=self.attempt_id,
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
                closure_epoch_id=self.closure_epoch_id,
                logical_attempt_identity=self.attempt_id,
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
            closure_epoch_id=self.closure_epoch_id,
            logical_attempt_identity=self.attempt_id,
            expected_call_graph_hash=self.expected_call_graph_hash,
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
        safe_call_id = "".join(
            char if char.isalnum() or char in {"-", "_"} else "_"
            for char in str(call_id)
        )
        output_path = self.workspace.artifact_path(
            f"validation_provider_outputs/{safe_call_id}.json"
        )
        atomic_write_json(
            output_path,
            {
                "artifact_type": "validation_provider_output",
                "artifact_version": "v1",
                "job_id": self.job_id,
                "attempt_id": self.attempt_id,
                "stage_name": expected.stage_name,
                "call_id": str(call_id),
                "content_hash": normalized_hash,
                "payload": content,
            },
        )
        output_record = self.artifact_registry.register_file(
            artifact_role="validation_provider_output",
            artifact_type="validation_provider_output",
            artifact_version="v1",
            path=output_path,
            producer="validation.execution_service.ValidationExecutionService",
            artifact_id=f"validation-provider-output:{safe_call_id}",
        )
        self._expected_provider_calls[str(call_id)] = replace(
            expected,
            provider_response_hash=normalized_hash,
            output_hash=normalized_hash,
            normalized_output_hash=normalized_hash,
            artifact_payload_hash=normalized_hash,
            artifact_content_hash=normalized_hash,
            registry_file_hash=output_record.content_hash,
            artifact_path=output_record.path,
            registered_artifact_hash=normalized_hash,
            node_output_hash=normalized_hash,
        )

    def persist_summaries(self) -> bool:
        summary_set_hash = hash_json(self.summaries)
        path = self.workspace.artifact_path(
            f"stage1/inputs/stage1_summaries_{summary_set_hash[:24]}.json"
        )
        if Path(path).is_file():
            try:
                existing = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"current Stage 1 summary artifact is unreadable: {path}") from exc
            if hash_json(existing) != summary_set_hash:
                raise RuntimeError(f"content-addressed Stage 1 summary artifact has drifted: {path}")
        else:
            atomic_write_json(path, self.summaries)
        immutable_id = f"summary_file:{summary_set_hash}"
        immutable_record = self.artifact_registry.register_file(
            artifact_role="summary",
            artifact_type="summary_file",
            artifact_version="v1",
            path=path,
            producer="validation.execution_service.ValidationExecutionService",
            artifact_id=immutable_id,
            metadata={
                "immutable": True,
                "summary_set_hash": summary_set_hash,
                "versioned_artifact_id": immutable_id,
            },
        )
        self.artifact_registry.register_file(
            artifact_role="summary",
            artifact_type="summary_file",
            artifact_version="v1",
            path=path,
            producer="validation.execution_service.ValidationExecutionService",
            artifact_id="summary_file",
            depends_on=[ArtifactDependencyRefV2.from_record(immutable_record)],
            metadata={
                "pointer_role": "current",
                "current_version_artifact_id": immutable_id,
                "summary_set_hash": summary_set_hash,
            },
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
            self,
            dict(review_draft),
            dict(citation_manifest),
            output_path,
        )

    def revalidate_review_artifacts(
        self,
        *,
        review_draft_record: ArtifactRecord,
        citation_manifest_record: ArtifactRecord,
        output_dir: str,
        result_artifact_id: str,
        paper_artifact_records: Sequence[ArtifactRecord] | None = None,
    ) -> dict[str, Any]:
        """Run the current validator against explicit repaired artifacts.

        The repaired records remain quarantined.  The revalidation result is
        therefore also quarantined even when its semantic disposition is clean;
        promotion must consume this exact result and decide whether to move a
        current pointer.  No legacy validator or in-memory-only success flag is
        involved.
        """

        import json

        def load(record: ArtifactRecord) -> dict[str, Any]:
            payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError(f"revalidation input is not a JSON object: {record.artifact_id}")
            return dict(payload)

        from validation.current_validation import run_current_validation

        paper_payloads: list[dict[str, Any]] = []
        for record in paper_artifact_records or self.paper_artifact_records:
            if record.status != "ready":
                continue
            payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
            if isinstance(payload, Mapping):
                paper_payloads.append(dict(payload))
        result: dict[str, Any] | None = None
        try:
            result = run_current_validation(
                self,
                review_draft_override=load(review_draft_record),
                citation_manifest_override=load(citation_manifest_record),
                paper_artifacts_override=paper_payloads,
                review_draft_record_override=review_draft_record,
                citation_manifest_record_override=citation_manifest_record,
                output_dir=output_dir,
                result_artifact_id=result_artifact_id,
                result_artifact_type="validation_run_result_repaired",
                result_artifact_role="validation_run_result_repaired",
            )
        finally:
            closure = self.finalize_provider_receipts(
                artifact_id=f"validation:provider_receipt_closure:repaired:{result_artifact_id}",
                ledger_artifact_id=f"validation_provider_receipts:repaired:{result_artifact_id}",
                closure_path=str(Path(output_dir) / "provider_receipt_closure.json"),
            )
        if result is None:
            raise RuntimeError("current repair revalidation did not produce a result")
        result["provider_receipt_closure"] = closure["closure"].to_dict()
        result["provider_receipt_closure_record_id"] = closure["closure_record"].artifact_id
        return result

    def finalize_provider_receipts(
        self,
        *,
        artifact_id: str = "validation:provider_receipt_closure",
        ledger_artifact_id: str = "validation_provider_receipts",
        closure_path: str | None = None,
    ) -> dict[str, Any]:
        """Persist receipt ledger and evaluate against pre-transport expectations."""

        receipts = list(self.provider_receipt_ledger.list_receipts())
        expected_calls = list(self._expected_provider_calls.values())
        closure: ReceiptClosureResult = ProviderReceiptClosure.evaluate(
            expected_calls,
            receipts,
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
                artifact_id=ledger_artifact_id,
                metadata={"receipt_count": len(receipts)},
            )
        closure_path = closure_path or self.workspace.artifact_path(
            "validation_provider_receipt_closure.json"
        )
        resolved_epoch = closure.closure_epoch_id or self.closure_epoch_id
        closure_payload = {
            **closure.to_dict(),
            "job_id": self.job_id,
            "stage_name": "stage4_validate",
            "attempt_id": self.attempt_id,
            "logical_attempt_identity": self.attempt_id,
            "closure_epoch_id": resolved_epoch,
            "expected_call_graph_hash": self.expected_call_graph_hash,
            "expected_calls": [asdict(expected) for expected in expected_calls],
        }
        closure_artifact_id = f"provider-receipt-closure:stage4_validate:{resolved_epoch}"
        atomic_write_json(
            closure_path,
            {
                "artifact_type": "provider_receipt_closure",
                "artifact_version": "v1",
                "job_id": self.job_id,
                "stage_name": "stage4_validate",
                "attempt_id": self.attempt_id,
                "closure_epoch_id": resolved_epoch,
                "expected_call_graph_hash": self.expected_call_graph_hash,
                "payload": closure_payload,
            },
        )
        dependencies: list[ArtifactDependencyRefV2] = []
        dependency_ids: set[str] = set()

        def add_dependency(record: ArtifactRecord | None) -> None:
            if record is None or record.status != "ready" or record.artifact_id in dependency_ids:
                return
            dependency_ids.add(record.artifact_id)
            dependencies.append(ArtifactDependencyRefV2.from_record(record))

        add_dependency(ledger_record)
        for record in (
            self.review_draft_record,
            self.citation_manifest_record,
            *self.paper_artifact_records,
            *self.visual_artifact_records,
        ):
            add_dependency(record)
        for expected in expected_calls:
            expected_path = str(expected.artifact_path or "").strip()
            if not expected_path:
                continue
            expected_resolved = Path(expected_path).resolve()
            add_dependency(
                next(
                    (
                        record
                        for record in self.artifact_registry.list_records()
                        if record.status == "ready"
                        and Path(record.path).resolve() == expected_resolved
                    ),
                    None,
                )
            )
        closure_record = self.artifact_registry.register_file(
            artifact_role="provider_receipt_closure",
            artifact_type="provider_receipt_closure",
            artifact_version="v1",
            path=closure_path,
            producer="validation.execution_service.ValidationExecutionService",
            artifact_id=closure_artifact_id,
            depends_on=dependencies,
            metadata={
                "closure_hash": closure.closure_hash,
                "complete": closure.complete,
                "job_id": self.job_id,
                "stage_name": "stage4_validate",
                "attempt_id": self.attempt_id,
                "expected_call_graph_hash": self.expected_call_graph_hash,
                "closure_epoch_id": resolved_epoch,
            },
        )
        return {
            "ledger": ledger_record,
            "closure": closure,
            "closure_record": closure_record,
            "expected_call_ids": tuple(sorted(expected.call_id for expected in expected_calls)),
        }

    def run_review_validation(self) -> dict[str, Any]:
        """Run the current validation sequence from durable records."""

        from validation.current_validation import run_current_validation

        return run_current_validation(self)


__all__ = ["ValidationExecutionService"]

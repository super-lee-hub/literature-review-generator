"""Current validation execution service owned by the runtime boundary.

The validator operates on this typed service contract.  It is deliberately
workspace-oriented: the service owns the current settings, Registry, summary
source, and canonical artifact paths instead of exposing a generator-shaped
compatibility object.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
from typing import Any, Callable, Dict, List, Mapping

from runtime.provider_receipt_closure import (
    ExpectedProviderCall,
    ProviderReceiptClosure,
    ReceiptClosureResult,
)
from runtime.provider_runtime import (
    ProviderBudgetV1,
    ProviderRuntime,
    ProviderRuntimeLedger,
)
from services.artifact_registry import ArtifactDependencyRefV2
from services.job_workspace import atomic_write_json


@dataclass
class ValidationExecutionService:
    stage_host: Any
    validation_attempt_id: str = ""
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

    @property
    def logger(self) -> Any:
        return self.stage_host.logger

    @property
    def config(self) -> Any:
        return self.stage_host.config

    @property
    def artifact_registry(self) -> Any:
        return self.stage_host.artifact_registry

    @property
    def job_workspace(self) -> Any:
        return self.stage_host.job_workspace

    @property
    def summaries(self) -> List[Dict[str, Any]]:
        return self.stage_host.summaries

    @summaries.setter
    def summaries(self, value: List[Dict[str, Any]]) -> None:
        self.stage_host.summaries = value

    def _stage2_validation_enabled(self) -> bool:
        return bool(self.stage_host._stage2_validation_enabled())

    def _review_draft_path(self) -> str:
        return str(self.stage_host._review_draft_path())

    def _citation_manifest_path(self) -> str:
        return str(self.stage_host._citation_manifest_path())

    def _get_review_word_file_path(self) -> str:
        return str(self.stage_host._get_review_word_file_path())

    def _persist_citation_manifest(self, *args: Any, **kwargs: Any) -> bool:
        return bool(self.stage_host._persist_citation_manifest(*args, **kwargs))

    def save_summaries(self) -> bool:
        return bool(self.stage_host.save_summaries())

    def _persist_paper_artifact(self, result: Dict[str, Any]) -> bool:
        return bool(self.stage_host._persist_paper_artifact(result))

    def get_paper_key(self, paper: Dict[str, Any]) -> str:
        return str(self.stage_host.get_paper_key(paper))

    @property
    def provider_receipt_ledger(self) -> ProviderRuntimeLedger:
        """Return the validation-stage append-only provider receipt ledger."""

        if self._provider_receipt_ledger is None:
            workspace = self.job_workspace
            if workspace is None:
                raise RuntimeError("validation provider receipts require a bound job workspace")
            self._provider_receipt_ledger = ProviderRuntimeLedger(
                workspace.artifact_path("validation_provider_receipts.jsonl")
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
        """Bind one validation provider node to the current job and attempt.

        Validation model calls must enter through this factory.  The factory
        records the expected call identity before transport admission, so a
        missing or failed provider receipt cannot be mistaken for a completed
        validation pass.
        """

        workspace = self.job_workspace
        if workspace is None:
            raise RuntimeError("validation provider runtime requires a bound job workspace")
        settings = getattr(self.stage_host, "settings", None)
        runtime_settings = getattr(settings, "runtime", None)
        try:
            retry_limit = max(0, int(getattr(runtime_settings, "validation_retry_limit", 1)))
        except (TypeError, ValueError):
            retry_limit = 1
        config = dict(api_config or {})
        resolved_stage = str(stage_name or "stage4_validate").strip()
        resolved_route = str(route or "Validator_API").strip()
        resolved_node = str(node_id or "validation-node").strip()
        resolved_call = str(call_id or "").strip()
        if not resolved_call:
            raise ValueError("validation provider call_id is required")
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
        runtime = ProviderRuntime(
            budget=ProviderBudgetV1(
                max_calls=1,
                max_retries_per_call=retry_limit,
            ),
            ledger=self.provider_receipt_ledger,
            job_id=workspace.job_id,
            attempt_id=str(self.validation_attempt_id or "validation"),
            stage_name=resolved_stage,
            route=resolved_route,
            node_id=resolved_node,
            call_id=resolved_call,
            endpoint_type=endpoint_type,
            schema_hash=resolved_schema_hash,
        )
        self._expected_provider_calls[resolved_call] = ExpectedProviderCall(
            call_id=resolved_call,
            job_id=workspace.job_id,
            attempt_id=str(self.validation_attempt_id or "validation"),
            stage_name=resolved_stage,
            node_id=resolved_node,
            max_attempts=max(1, retry_limit + 1),
            usage_required=endpoint_type not in {"internal", "fixture"},
        )
        self._provider_runtimes[resolved_call] = runtime
        return runtime

    def finalize_provider_receipts(self) -> dict[str, Any]:
        """Persist the validation receipt ledger and its current closure."""

        workspace = self.job_workspace
        registry = self.artifact_registry
        if workspace is None or registry is None:
            raise RuntimeError("validation provider closure requires a bound workspace and Registry")

        ledger = self._provider_receipt_ledger
        receipts = list(ledger.list_receipts()) if ledger is not None else []
        expected_calls: list[ExpectedProviderCall] = []
        for call_id, expected in self._expected_provider_calls.items():
            candidates = [receipt for receipt in receipts if receipt.call_id == call_id]
            if candidates:
                current = max(candidates, key=lambda item: (item.attempts, item.sequence, item.finished_at))
                expected = replace(
                    expected,
                    prompt_hash=current.prompt_hash,
                    input_hash=current.input_hash,
                    config_hash=current.config_hash,
                    schema_hash=current.schema_hash,
                    output_hash=current.response_hash or "",
                )
            expected_calls.append(expected)
        expected_ids = {item.call_id for item in expected_calls}
        closure: ReceiptClosureResult = ProviderReceiptClosure.evaluate(
            expected_calls,
            [receipt for receipt in receipts if receipt.call_id in expected_ids],
        )

        ledger_record = None
        if ledger is not None and ledger.path.is_file():
            ledger_record = registry.register_file(
                artifact_role="provider_receipts",
                artifact_type="provider_receipt_ledger",
                artifact_version="v1",
                path=str(ledger.path),
                producer="validation.execution_service.ValidationExecutionService",
                artifact_id="validation_provider_receipts",
                metadata={"receipt_count": len(receipts)},
            )

        closure_path = workspace.artifact_path("validation_provider_receipt_closure.json")
        atomic_write_json(
            closure_path,
            {
                "job_id": workspace.job_id,
                "attempt_id": str(self.validation_attempt_id or "validation"),
                "payload": closure.to_dict(),
            },
        )
        dependencies: list[ArtifactDependencyRefV2] = []
        if ledger_record is not None and ledger_record.status == "ready":
            dependencies.append(ArtifactDependencyRefV2.from_record(ledger_record))
        closure_record = registry.register_file(
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


__all__ = ["ValidationExecutionService"]

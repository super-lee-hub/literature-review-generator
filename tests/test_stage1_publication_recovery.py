from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Mapping

import pytest

import runtime.stage1_publication_recovery as recovery
import runtime.provider_runtime as provider_runtime
from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderBudgetController,
    ProviderCallReceiptV1,
    RuntimeControlContextV1,
    RuntimeControlIdentityV1,
    hash_json,
)
from runtime.provider_receipt_closure import ExpectedProviderCall, ProviderReceiptClosure
from runtime.reconcile import ProvenStageRecovery, RuntimeReconciler
from runtime.stage_terminal import StageTerminalStore, TerminalStageRecordV1
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    file_sha256,
)
from services.job_workspace import JobWorkspace, utc_now_iso
from summary_schema import normalize_ai_summary
from runtime.stage1_publication_recovery import (
    Stage1PublicationRecoveryBlocked,
    try_recover_registered_stage1_publication,
)


class _MemoryRegistry:
    def __init__(self, root: Path, job_id: str) -> None:
        self.root = root
        self.job_id = job_id
        self.records: dict[str, ArtifactRecord] = {}

    def get(self, artifact_id: str) -> ArtifactRecord | None:
        return self.records.get(artifact_id)

    def list_records(self) -> list[ArtifactRecord]:
        return list(self.records.values())

    def add_json(
        self,
        artifact_id: str,
        artifact_type: str,
        artifact_version: str,
        payload: Any,
        *,
        dependencies: list[ArtifactDependencyRefV2] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactRecord:
        safe_id = artifact_id.replace(":", "_")
        path = self.root / f"{safe_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        record = ArtifactRecord(
            artifact_id=artifact_id,
            artifact_role=artifact_type,
            artifact_type=artifact_type,
            artifact_version=artifact_version,
            path=str(path),
            producer="tests.stage1_publication_recovery",
            job_id=self.job_id,
            status="ready",
            content_hash=file_sha256(path),
            depends_on=list(dependencies or []),
            metadata=dict(metadata or {}),
        )
        self.records[artifact_id] = record
        return record

    def add_jsonl(
        self,
        artifact_id: str,
        artifact_type: str,
        artifact_version: str,
        rows: list[Mapping[str, Any]],
    ) -> ArtifactRecord:
        path = self.root / f"{artifact_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(dict(row), sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        record = ArtifactRecord(
            artifact_id=artifact_id,
            artifact_role=artifact_type,
            artifact_type=artifact_type,
            artifact_version=artifact_version,
            path=str(path),
            producer="tests.stage1_publication_recovery",
            job_id=self.job_id,
            status="ready",
            content_hash=file_sha256(path),
        )
        self.records[artifact_id] = record
        return record

    def verify_ready_artifact_closure(
        self,
        root: ArtifactRecord | ArtifactDependencyRefV2,
        *,
        external_registry_resolver: Any = None,
    ) -> ArtifactRecord:
        del external_registry_resolver
        record = self.get(root.artifact_id)
        if record is None or record.status != "ready" or record.job_id != self.job_id:
            raise ValueError("missing or foreign READY record")
        self._verify(record, set())
        return record

    def _verify(self, record: ArtifactRecord, seen: set[str]) -> None:
        if record.artifact_id in seen:
            return
        seen.add(record.artifact_id)
        path = Path(record.path)
        if not path.is_file() or file_sha256(path) != record.content_hash:
            raise ValueError(f"artifact bytes changed: {record.artifact_id}")
        for dependency in record.depends_on:
            if dependency.dependency_kind != "local_job" or dependency.job_id != self.job_id:
                raise ValueError("foreign dependency")
            child = self.get(dependency.artifact_id)
            if child is None or ArtifactDependencyRefV2.from_record(child) != dependency:
                raise ValueError("dependency reference mismatch")
            self._verify(child, seen)


class _TestWorkspace:
    def __init__(self, root: Path, job_id: str) -> None:
        self.job_id = job_id
        self.root_dir = str(root / "workspace")
        Path(self.root_dir).mkdir(parents=True, exist_ok=True)

    def artifact_path(self, filename: str) -> str:
        return str(Path(self.root_dir) / "artifacts" / filename)

    def log_path(self, filename: str) -> str:
        path = Path(self.root_dir) / "logs" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)


def _fake_reconciler(monkeypatch: pytest.MonkeyPatch) -> None:
    class ReadOnlyReconciler:
        def __init__(self, workspace: Any, registry: _MemoryRegistry, **kwargs: Any) -> None:
            del workspace, kwargs
            self.registry = registry

        def validate_dependency_ref(self, ref: ArtifactDependencyRefV2) -> ArtifactRecord:
            return self.registry.verify_ready_artifact_closure(ref)

    monkeypatch.setattr(recovery, "RuntimeReconciler", ReadOnlyReconciler)


def _fixture(tmp_path: Path) -> tuple[_MemoryRegistry, Any, str, str, list[str]]:
    job_id = "stage1-recovery-job"
    attempt_id = "attempt-original"
    epoch_id = "epoch-original"
    graph_hash = hashlib.sha256(b"expected-stage1-call-graph").hexdigest()
    config_hash = hashlib.sha256(b"reader-config").hexdigest()
    registry = _MemoryRegistry(tmp_path, job_id)
    workspace = _TestWorkspace(tmp_path, job_id)
    keys = [f"paper-{index}" for index in range(1, 5)]

    source_bundle = registry.add_json(
        "source_bundle",
        "source_bundle",
        "v1",
        {
            "artifact_type": "source_bundle",
            "artifact_version": "v1",
            "job_id": job_id,
            "paper_work_items": [
                {"canonical_paper_key": key, "source_paper_id": key}
                for key in keys
            ],
        },
    )
    runtime_spec = registry.add_json(
        "runtime_job_spec",
        "runtime_job_spec",
        "v1",
        {"artifact_type": "runtime_job_spec", "artifact_version": "v1", "job_id": job_id},
    )
    source_scope = registry.add_json(
        "stage1_source_scope",
        "stage1_source_scope",
        "v1",
        {"artifact_type": "stage1_source_scope", "artifact_version": "v1", "job_id": job_id},
        dependencies=[
            ArtifactDependencyRefV2.from_record(source_bundle),
            ArtifactDependencyRefV2.from_record(runtime_spec),
        ],
    )
    calls = [
        {
            "call_id": f"stage1_synthesis:{key}",
            "job_id": job_id,
            "stage_name": "stage1_analyze",
            "node_id": key,
            "attempt_id": attempt_id,
            "logical_attempt_identity": attempt_id,
            "closure_epoch_id": epoch_id,
            "expected_call_graph_hash": graph_hash,
            "input_hash": hashlib.sha256((key + "-input").encode()).hexdigest(),
            "config_hash": config_hash,
            "schema_hash": hashlib.sha256(b"summary-schema").hexdigest(),
        }
        for key in keys
    ]
    calls.extend(
        [
            {
                **calls[0],
                "call_id": "stage1_visual_scan:paper-1:0",
                "node_id": "paper-1:visual_scan:0",
                "artifact_path": str(tmp_path / "visual-scan-paper-1.json"),
            },
            {
                **calls[0],
                "call_id": "stage1_visual_extract:paper-1:1",
                "node_id": "paper-1:visual_extract:1",
                "artifact_path": str(tmp_path / "visual-extract-paper-1.json"),
            },
            {
                **calls[1],
                "call_id": "stage1_mineru_parse:paper-2",
                "node_id": "paper-2:mineru_parse",
                "artifact_path": str(tmp_path / "mineru-paper-2.json"),
            },
        ]
    )
    attempts_by_call = {
        str(call["call_id"]): (2 if str(call["call_id"]).startswith("stage1_visual_scan:") else 3
                               if str(call["call_id"]).startswith("stage1_visual_extract:") else 4
                               if str(call["call_id"]).startswith("stage1_mineru_parse:") else 1)
        for call in calls
    }
    graph = registry.add_json(
        "stage1:provider_expected_call_graph",
        "provider_expected_call_graph",
        "v1",
        {
            "artifact_type": "provider_expected_call_graph",
            "artifact_version": "v1",
            "job_id": job_id,
            "stage_name": "stage1_analyze",
            "attempt_id": attempt_id,
            "logical_attempt_identity": attempt_id,
            "closure_epoch_id": epoch_id,
            "expected_call_graph_hash": graph_hash,
            "source_bundle_hash": source_bundle.content_hash,
            "runtime_spec_hash": runtime_spec.content_hash,
            "expected_calls": calls,
        },
        dependencies=[ArtifactDependencyRefV2.from_record(source_scope)],
        metadata={"expected_call_graph_hash": graph_hash},
    )
    receipt_id_by_call = {str(call["call_id"]): f"receipt-{index}" for index, call in enumerate(calls, start=1)}
    ledger_rows = []
    receipt_ids_by_paper = {key: [] for key in keys}
    for index, call in enumerate(calls, start=1):
        call_id = str(call["call_id"])
        if call_id.startswith("stage1_synthesis:"):
            paper_key = call_id.removeprefix("stage1_synthesis:")
            include_in_summary = True
        elif call_id.startswith(("stage1_visual_scan:", "stage1_visual_extract:")):
            remainder = call_id.split(":", 1)[1]
            paper_key = remainder.rpartition(":")[0]
            include_in_summary = True
        else:
            paper_key = call_id.removeprefix("stage1_mineru_parse:")
            include_in_summary = False
        receipt_id = receipt_id_by_call[call_id]
        receipt = ProviderCallReceiptV1(
            artifact_type="provider_call_receipt",
            artifact_version="v2",
            receipt_id=receipt_id,
            sequence=index,
            job_id=job_id,
            attempt_id=attempt_id,
            stage_name="stage1_analyze",
            route="Primary Reader",
            provider="local-fixture",
            model="fixture-model",
            endpoint="http://fixture.invalid",
            prompt_hash=hashlib.sha256((call_id + "-prompt").encode()).hexdigest(),
            input_hash=str(call["input_hash"]),
            config_hash=str(call["config_hash"]),
            schema_hash=str(call["schema_hash"]),
            status="success",
            error_kind=None,
            http_status=200,
            provider_code=None,
            attempts=attempts_by_call[call_id],
            retry_after_seconds=None,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            response_hash=hashlib.sha256((call_id + "-response").encode()).hexdigest(),
            started_at=f"2026-01-01T00:00:{index:02d}Z",
            finished_at=f"2026-01-01T00:01:{index:02d}Z",
            budget={},
            metadata={
                "transport_config": {
                    "provider_family": "openai_compatible",
                    "endpoint_type": "chat_completions",
                    "api_base": "http://fixture.invalid",
                    "model": "fixture-model",
                }
            },
            node_id=str(call["node_id"]),
            call_id=call_id,
            closure_epoch_id=epoch_id,
            logical_attempt_identity=attempt_id,
            endpoint_type="chat_completions",
            usage_status="reported",
            test_only=False,
        )
        ledger_rows.append(receipt.to_dict())
        if include_in_summary:
            receipt_ids_by_paper[paper_key].append(receipt_id)
    ledger = registry.add_jsonl("stage1_provider_receipts", "provider_receipt_ledger", "v1", ledger_rows)
    summaries = [
        {
            "status": "success",
            "paper_info": {"canonical_paper_key": key, "source_paper_id": key},
            "provider": {"receipt_ids": receipt_ids_by_paper[key]},
        }
        for key in keys
    ]
    versioned_summary = registry.add_json(
        "summary_file:immutable-version",
        "summary_file",
        "v1",
        summaries,
        dependencies=[
            ArtifactDependencyRefV2.from_record(source_bundle),
            ArtifactDependencyRefV2.from_record(ledger),
        ],
        metadata={"immutable": True},
    )
    summary_file = replace(
        versioned_summary,
        artifact_id="summary_file",
        metadata={
            "pointer_role": "current",
            "current_version_artifact_id": versioned_summary.artifact_id,
            "summary_set_hash": "set-hash",
        },
    )
    registry.records[summary_file.artifact_id] = summary_file
    manifest = registry.add_json(
        "summary_source_manifest",
        "summary_source_manifest",
        "v2",
        {
            "artifact_type": "summary_source_manifest",
            "artifact_version": "v2",
            "source_kind": "provider_generated",
            "source_path": source_bundle.path,
            "materialized_summary_file": summary_file.path,
            "summary_count": len(keys),
            "source_items": [
                {
                    "canonical_paper_key": key,
                    "source_paper_id": key,
                    "provider_receipt_ids": receipt_ids_by_paper[key],
                    "disposition": "provider_generated",
                }
                for key in keys
            ],
        },
        dependencies=[
            ArtifactDependencyRefV2.from_record(source_bundle),
            ArtifactDependencyRefV2.from_record(ledger),
        ],
    )
    registry.add_json(
        "stage1_progress_snapshot",
        "stage1_progress_snapshot",
        "v1",
        {"artifact_type": "stage1_progress_snapshot", "artifact_version": "v1", "job_id": job_id},
        dependencies=[ArtifactDependencyRefV2.from_record(summary_file)],
    )
    papers = []
    for key in keys:
        artifact_id = f"paper:{hashlib.sha256(key.encode()).hexdigest()[:24]}"
        papers.append(
            registry.add_json(
                artifact_id,
                "paper_artifact",
                "v1",
                {
                    "artifact_type": "paper_artifact",
                    "artifact_version": "v1",
                    "job_id": job_id,
                    "paper_identity": {"canonical_paper_key": key},
                },
            )
        )
    call_ids = [str(call["call_id"]) for call in calls]
    closure_payload = {
        "closure_epoch_id": epoch_id,
        "complete": True,
        "expected_call_ids": call_ids,
        "observed_call_ids": call_ids,
        "missing_call_ids": [],
        "stale_call_ids": [],
        "failed_call_ids": [],
        "incomplete_call_ids": [],
        "hash_mismatches": {},
        "unexpected_receipts": [],
        "retry_exceeded_call_ids": [],
        "usage_incomplete_call_ids": [],
        "verified_reuse_call_ids": [],
    }
    closure_document = {
        "artifact_type": "provider_receipt_closure",
        "artifact_version": "v1",
        "job_id": job_id,
        "stage_name": "stage1_analyze",
        "attempt_id": attempt_id,
        "logical_attempt_identity": attempt_id,
        "closure_epoch_id": epoch_id,
        "expected_call_graph_hash": graph_hash,
        "expected_calls": calls,
        "payload": closure_payload,
    }
    registry.add_json(
        "stage1:provider_receipt_closure",
        "provider_receipt_closure",
        "v1",
        closure_document,
        dependencies=[
            ArtifactDependencyRefV2.from_record(source_bundle),
            ArtifactDependencyRefV2.from_record(runtime_spec),
            ArtifactDependencyRefV2.from_record(graph),
            ArtifactDependencyRefV2.from_record(manifest),
            ArtifactDependencyRefV2.from_record(ledger),
            *(ArtifactDependencyRefV2.from_record(paper) for paper in papers),
        ],
        metadata={
            "stage_name": "stage1_analyze",
            "closure_epoch_id": epoch_id,
            "expected_call_graph_hash": graph_hash,
        },
    )
    return registry, workspace, source_bundle.content_hash, runtime_spec.content_hash, keys


def _install_proof_stubs(monkeypatch: pytest.MonkeyPatch, *, issues: list[str] | None = None) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class ReadOnlyReconciler:
        def __init__(self, workspace: Any, registry: _MemoryRegistry, **kwargs: Any) -> None:
            del workspace, kwargs
            self.registry = registry

        def validate_dependency_ref(self, ref: ArtifactDependencyRefV2) -> ArtifactRecord:
            return self.registry.verify_ready_artifact_closure(ref)

    def closure_proof(
        stage: str,
        record: ArtifactRecord,
        terminal_record: ArtifactRecord,
        terminal_payload: Mapping[str, Any],
        registry: Any,
        *,
        current_set: Any = None,
        external_registry_resolver: Any = None,
    ) -> tuple[dict[str, Any], list[str]]:
        del current_set, external_registry_resolver
        closure_document = json.loads(Path(record.path).read_text(encoding="utf-8"))
        closure_payload = closure_document.get("payload") or {}
        declared_ids = {
            str(item.get("call_id") or "")
            for item in closure_document.get("expected_calls") or ()
            if isinstance(item, Mapping)
        }
        closure_issues = list(issues or [])
        if (
            set(closure_payload.get("expected_call_ids") or ()) != declared_ids
            or set(closure_payload.get("observed_call_ids") or ()) != declared_ids
        ):
            closure_issues.append("provider_closure_receipt_set_mismatch:analyze")
        calls.append({
            "stage": stage,
            "closure_id": record.artifact_id,
            "terminal_probe_id": terminal_record.artifact_id,
            "output_ids": [item.get("artifact_id") for item in terminal_payload.get("output_artifact_refs") or ()],
            "registry_job": registry.job_id,
        })
        return {"complete": not closure_issues}, closure_issues

    monkeypatch.setattr(recovery, "RuntimeReconciler", ReadOnlyReconciler)
    monkeypatch.setattr(recovery, "_provider_closure_entry", closure_proof)
    return calls


def _register_json(
    registry: ArtifactRegistry,
    workspace: JobWorkspace,
    *,
    artifact_id: str,
    artifact_type: str,
    artifact_version: str,
    relative_path: str,
    payload: Mapping[str, Any] | list[Any],
    dependencies: list[ArtifactDependencyRefV2] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ArtifactRecord:
    path = Path(workspace.artifact_path(relative_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return registry.register_file(
        artifact_id=artifact_id,
        artifact_role=artifact_type,
        artifact_type=artifact_type,
        artifact_version=artifact_version,
        path=path,
        producer="tests.stage1_publication_recovery.actual_registry",
        depends_on=dependencies or [],
        metadata=metadata or {},
    )


def _actual_registry_fixture(tmp_path: Path) -> tuple[ArtifactRegistry, JobWorkspace, ArtifactRecord, ArtifactRecord]:
    job_id = "stage1-actual-registry-job"
    workspace = JobWorkspace.create(str(tmp_path / "output"), "Stage1Recovery", job_id)
    registry = ArtifactRegistry(workspace.paths.registry_path, job_id)
    paper_key = "paper-actual"
    source_pdf = tmp_path / "paper-actual.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\nprovider-free fixture\n")
    source_pdf_hash = file_sha256(source_pdf)

    summary = normalize_ai_summary(
        {
            "routing": {
                "paper_type": "empirical",
                "classification_status": "resolved",
                "route_confidence": "high",
                "classification_rationale": "Controlled study with a defined comparison.",
            },
            "paper_metadata": {"title": "Actual Registry fixture", "authors": ["Example"], "year": "2025"},
            "core_analysis": {
                "summary": "The study tests a treatment against a control.",
                "key_points": ["The treatment changes the measured outcome."],
                "methodology": "A controlled experiment with 120 observations.",
                "findings": "The treatment improves the measured outcome.",
                "conclusions": "The evidence supports the tested mechanism.",
                "relevance": "The result informs treatment design.",
                "limitations": "The result is bounded by the tested context.",
            },
            "specialized_details": {
                "empirical": {
                    "research_questions_or_hypotheses": ["Does the treatment change the outcome?"],
                    "data_source_and_size": "Controlled experiment, N=120.",
                    "analysis_technique": "Group comparison.",
                    "core_variables": {"independent": ["treatment"], "dependent": ["outcome"]},
                    "sample_characteristics_or_context": "Controlled context.",
                }
            },
        }
    )
    source_bundle = _register_json(
        registry,
        workspace,
        artifact_id="source_bundle",
        artifact_type="source_bundle",
        artifact_version="v1",
        relative_path="source_bundle_v1.json",
        payload={
            "source_mode": "direct",
            "project_name": "Stage1Recovery",
            "paper_work_items": [
                {
                    "canonical_paper_key": paper_key,
                    "source_paper_id": paper_key,
                    "source_pdf": str(source_pdf.resolve()),
                    "source_mode": "direct",
                }
            ],
            "source_snapshot": {"canonical_ready": True, "summary_only": False},
        },
    )
    runtime_spec = _register_json(
        registry,
        workspace,
        artifact_id="runtime_job_spec",
        artifact_type="runtime_job_spec",
        artifact_version="v1",
        relative_path="runtime_job_spec_v1.json",
        payload={
            "artifact_type": "runtime_job_spec",
            "artifact_version": "v1",
            "job_id": job_id,
            "action": "analyze",
        },
    )
    scope = _register_json(
        registry,
        workspace,
        artifact_id="stage1_source_scope",
        artifact_type="stage1_source_scope",
        artifact_version="v1",
        relative_path="stage1/source_scope_v1.json",
        payload={
            "schema_version": "stage1-source-scope/v1",
            "job_id": job_id,
            "source_bundle_hash": source_bundle.content_hash,
            "runtime_spec_hash": runtime_spec.content_hash,
            "papers": [
                {
                    "paper_key": paper_key,
                    "source_pdf": str(source_pdf.resolve()),
                    "source_pdf_sha256": source_pdf_hash,
                    "size_bytes": source_pdf.stat().st_size,
                    "page_count": 1,
                }
            ],
            "paper_count": 1,
            "page_count": 1,
            "reuse_status": "not_requested",
            "model_request_bounds": {},
        },
        dependencies=[
            ArtifactDependencyRefV2.from_record(source_bundle),
            ArtifactDependencyRefV2.from_record(runtime_spec),
        ],
    )

    attempt_id = "attempt-actual"
    epoch_id = hashlib.sha256(b"stage1-actual-epoch").hexdigest()
    graph_hash = hashlib.sha256(b"stage1-actual-call-graph").hexdigest()
    prompt_hash = hashlib.sha256(b"actual-prompt").hexdigest()
    input_hash = hashlib.sha256(b"actual-input").hexdigest()
    config_hash = hashlib.sha256(b"actual-config").hexdigest()
    schema_hash = hashlib.sha256(b"actual-schema").hexdigest()
    call_id = f"stage1_synthesis:{paper_key}"
    receipt_id = "provider-receipt-actual-registry"
    response_hash = hash_json(summary)
    paper_path = str(Path(workspace.artifact_path("paper_artifacts/paper-actual.json")))
    graph_contract = ExpectedProviderCall(
        call_id=call_id,
        job_id=job_id,
        attempt_id=attempt_id,
        stage_name="stage1_analyze",
        node_id=paper_key,
        closure_epoch_id=epoch_id,
        logical_attempt_identity=attempt_id,
        expected_call_graph_hash=graph_hash,
        prompt_hash=prompt_hash,
        input_hash=input_hash,
        config_hash=config_hash,
        schema_hash=schema_hash,
        artifact_path=paper_path,
        max_attempts=1,
        usage_required=False,
        provider="fixture-provider",
        model="fixture-model",
        endpoint="https://fixture.invalid/v1",
        endpoint_type="chat_completions",
        request_variants=({"input_hash": input_hash, "config_hash": config_hash},),
    )
    graph = _register_json(
        registry,
        workspace,
        artifact_id="stage1:provider_expected_call_graph",
        artifact_type="provider_expected_call_graph",
        artifact_version="v1",
        relative_path="stage1/provider_expected_call_graph.json",
        payload={
            "artifact_type": "provider_expected_call_graph",
            "artifact_version": "v1",
            "job_id": job_id,
            "stage_name": "stage1_analyze",
            "attempt_id": attempt_id,
            "logical_attempt_identity": attempt_id,
            "closure_epoch_id": epoch_id,
            "expected_call_graph_hash": graph_hash,
            "source_bundle_hash": source_bundle.content_hash,
            "runtime_spec_hash": runtime_spec.content_hash,
            "expected_calls": [asdict(graph_contract)],
        },
        dependencies=[ArtifactDependencyRefV2.from_record(scope)],
        metadata={"expected_call_graph_hash": graph_hash, "closure_epoch_id": epoch_id},
    )

    paper_payload = {
        "artifact_type": "paper_artifact",
        "artifact_version": "v1",
        "created_from_job_id": job_id,
        "created_at": utc_now_iso(),
        "paper_identity": {
            "canonical_paper_key": paper_key,
            "source_paper_id": paper_key,
            "paper_key_aliases": [paper_key],
        },
        "source": {
            "source_mode": "direct",
            "source_pdf": str(source_pdf.resolve()),
            "source_pdf_fingerprint": source_pdf_hash,
            "metadata_confidence": "high",
            "metadata_source_priority_snapshot": [],
        },
        "paper_info": {
            "canonical_paper_key": paper_key,
            "source_paper_id": paper_key,
            "source_pdf": str(source_pdf.resolve()),
            "source_mode": "direct",
        },
        "analysis": {
            "status": "success",
            "processing_time": "",
            "text_length": 0,
            "preprocess": {},
            "ai_summary": summary,
        },
        "stage1_inputs": {"input_mode": "text_only"},
    }
    paper_record = _register_json(
        registry,
        workspace,
        artifact_id=f"paper:{hashlib.sha256(paper_key.encode()).hexdigest()[:24]}",
        artifact_type="paper_artifact",
        artifact_version="v1",
        relative_path="paper_artifacts/paper-actual.json",
        payload=paper_payload,
        dependencies=[ArtifactDependencyRefV2.from_record(source_bundle)],
    )

    receipt = ProviderCallReceiptV1(
        artifact_type="provider_call_receipt",
        artifact_version="v2",
        receipt_id=receipt_id,
        sequence=1,
        job_id=job_id,
        attempt_id=attempt_id,
        stage_name="stage1_analyze",
        route="Primary Reader",
        provider="fixture-provider",
        model="fixture-model",
        endpoint="https://fixture.invalid/v1",
        prompt_hash=prompt_hash,
        input_hash=input_hash,
        config_hash=config_hash,
        schema_hash=schema_hash,
        status="success",
        error_kind=None,
        http_status=200,
        provider_code=None,
        attempts=1,
        retry_after_seconds=None,
        input_tokens=10,
        output_tokens=10,
        total_tokens=20,
        response_hash=response_hash,
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:00:01Z",
        budget={},
        metadata={
            "transport_config": {
                "provider_family": "openai_compatible",
                "endpoint_type": "chat_completions",
                "api_base": "https://fixture.invalid/v1",
                "model": "fixture-model",
            },
            "registry_file_hash": paper_record.content_hash,
            "registry_file_path": paper_record.path,
            "replay_output_hash": response_hash,
        },
        node_id=paper_key,
        call_id=call_id,
        closure_epoch_id=epoch_id,
        logical_attempt_identity=attempt_id,
        endpoint_type="chat_completions",
        usage_status="reported",
        test_only=False,
    )
    ledger_path = Path(workspace.artifact_path("stage1_provider_receipts.jsonl"))
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(receipt.to_dict(), sort_keys=True) + "\n", encoding="utf-8")
    ledger = registry.register_file(
        artifact_id="stage1_provider_receipts",
        artifact_role="provider_receipts",
        artifact_type="provider_receipt_ledger",
        artifact_version="v1",
        path=ledger_path,
        producer="tests.stage1_publication_recovery.actual_registry",
        metadata={"receipt_count": 1, "stage_name": "stage1_analyze", "closure_epoch_id": epoch_id},
    )

    analysis_payload_hash = hash_json(paper_payload["analysis"])
    registry_file_hash = file_sha256(paper_record.path)
    bound_contract = replace(
        graph_contract,
        output_hash=response_hash,
        provider_response_hash=response_hash,
        normalized_output_hash=response_hash,
        artifact_payload_hash=analysis_payload_hash,
        artifact_content_hash=paper_record.content_hash,
        registry_file_hash=registry_file_hash,
        artifact_path=paper_record.path,
        registered_artifact_hash=paper_record.content_hash,
        replay_output_hash=response_hash,
        node_output_hash=paper_record.content_hash,
    )
    closure_result = ProviderReceiptClosure.evaluate([bound_contract], [receipt])
    assert closure_result.complete, closure_result.to_dict()

    summary_row = {
        "status": "success",
        "paper_info": paper_payload["paper_info"],
        "ai_summary": summary,
        "provider": {"receipt_ids": [receipt_id]},
    }
    summary_path = Path(workspace.artifact_path("summary_file.json"))
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps([summary_row], ensure_ascii=False, sort_keys=True), encoding="utf-8")
    summary_dependencies = [
        ArtifactDependencyRefV2.from_record(source_bundle),
        ArtifactDependencyRefV2.from_record(ledger),
    ]
    versioned_summary = registry.register_file(
        artifact_id="summary_file:immutable-version",
        artifact_role="summary_file",
        artifact_type="summary_file",
        artifact_version="v1",
        path=summary_path,
        producer="tests.stage1_publication_recovery.actual_registry",
        depends_on=summary_dependencies,
        metadata={"immutable": True},
    )
    summary_file = registry.register_file(
        artifact_id="summary_file",
        artifact_role="summary_file",
        artifact_type="summary_file",
        artifact_version="v1",
        path=summary_path,
        producer="tests.stage1_publication_recovery.actual_registry",
        depends_on=summary_dependencies,
        metadata={
            "pointer_role": "current",
            "current_version_artifact_id": versioned_summary.artifact_id,
            "summary_set_hash": hash_json([summary_row]),
        },
    )
    manifest = _register_json(
        registry,
        workspace,
        artifact_id="summary_source_manifest",
        artifact_type="summary_source_manifest",
        artifact_version="v2",
        relative_path="summary_source_manifest_v2.json",
        payload={
            "artifact_type": "summary_source_manifest",
            "artifact_version": "v2",
            "created_at": utc_now_iso(),
            "project_name": "Stage1Recovery",
            "source_kind": "provider_generated",
            "source_path": source_bundle.path,
            "source_items": [
                {
                    "canonical_paper_key": paper_key,
                    "source_paper_id": paper_key,
                    "source_pdf": str(source_pdf.resolve()),
                    "disposition": "provider_generated",
                    "provider_receipt_ids": [receipt_id],
                }
            ],
            "rejected_candidates": [],
            "materialized_summary_file": summary_file.path,
            "summary_count": 1,
        },
        dependencies=summary_dependencies,
    )
    _register_json(
        registry,
        workspace,
        artifact_id="stage1_progress_snapshot",
        artifact_type="stage1_progress_snapshot",
        artifact_version="v1",
        relative_path="stage1/progress_snapshot.json",
        payload={"artifact_type": "stage1_progress_snapshot", "artifact_version": "v1", "job_id": job_id},
        dependencies=[ArtifactDependencyRefV2.from_record(summary_file)],
    )
    _register_json(
        registry,
        workspace,
        artifact_id="stage1:provider_receipt_closure",
        artifact_type="provider_receipt_closure",
        artifact_version="v1",
        relative_path="stage1/provider_receipt_closure.json",
        payload={
            "artifact_type": "provider_receipt_closure",
            "artifact_version": "v1",
            "job_id": job_id,
            "stage_name": "stage1_analyze",
            "attempt_id": attempt_id,
            "logical_attempt_identity": attempt_id,
            "closure_epoch_id": epoch_id,
            "expected_call_graph_hash": graph_hash,
            "expected_calls": [asdict(bound_contract)],
            "paper_artifact_ids": [paper_record.artifact_id],
            "source_summary_artifact_ids": [],
            "payload": closure_result.to_dict(),
            "reuse_evidence_ids": [],
            "reuse_evidence_count": 0,
            "expected_provider_transport_count": 1,
            "actual_provider_transport_count": 1,
            "mineru_receipt_artifact_ids": [],
        },
        dependencies=[
            ArtifactDependencyRefV2.from_record(source_bundle),
            ArtifactDependencyRefV2.from_record(runtime_spec),
            ArtifactDependencyRefV2.from_record(graph),
            ArtifactDependencyRefV2.from_record(manifest),
            ArtifactDependencyRefV2.from_record(ledger),
            ArtifactDependencyRefV2.from_record(paper_record),
        ],
        metadata={
            "stage_name": "stage1_analyze",
            "closure_epoch_id": epoch_id,
            "expected_call_graph_hash": graph_hash,
        },
    )
    return registry, workspace, source_bundle, runtime_spec


def test_recovers_and_reconciles_provider_free_fixture_with_real_registry_and_closure(
    tmp_path: Path,
) -> None:
    registry, workspace, source_bundle, runtime_spec = _actual_registry_fixture(tmp_path)
    initial_proposal = try_recover_registered_stage1_publication(
        workspace,
        registry,
        expected_source_bundle_hash=source_bundle.content_hash,
        expected_runtime_spec_hash=runtime_spec.content_hash,
    )
    assert isinstance(initial_proposal, ProvenStageRecovery)
    historical_success = TerminalStageRecordV1.create(
        job_id=registry.job_id,
        attempt_id="attempt-historical-success",
        stage_name="analyze",
        status="succeeded",
        producer="tests.stage1_publication_recovery.actual_registry",
        output_artifact_refs=initial_proposal.output_artifact_refs,
        model_call_count=1,
        terminal_reason="previous source binding before canonical pointer advanced",
    )
    StageTerminalStore(workspace, registry).persist(historical_success)

    old_summary = registry.get("summary_file")
    old_manifest = registry.get("summary_source_manifest")
    old_progress = registry.get("stage1_progress_snapshot")
    old_closure = registry.get("stage1:provider_receipt_closure")
    source_ledger = registry.get("stage1_provider_receipts")
    assert old_summary is not None and old_manifest is not None
    assert old_progress is not None and old_closure is not None and source_ledger is not None
    rows = json.loads(Path(old_summary.path).read_text(encoding="utf-8"))
    revised_summary_path = Path(workspace.artifact_path("summary_file_revision_2.json"))
    revised_summary_path.parent.mkdir(parents=True, exist_ok=True)
    revised_summary_path.write_text(
        json.dumps(rows, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary_dependencies = [
        ref
        for ref in old_summary.depends_on
    ]
    revised_version = registry.register_file(
        artifact_id="summary_file:immutable-version:revision-2",
        artifact_role="summary_file",
        artifact_type="summary_file",
        artifact_version="v1",
        path=revised_summary_path,
        producer="tests.stage1_publication_recovery.actual_registry",
        depends_on=summary_dependencies,
        metadata={"immutable": True},
    )
    revised_summary = registry.register_file(
        artifact_id="summary_file",
        artifact_role="summary_file",
        artifact_type="summary_file",
        artifact_version="v1",
        path=revised_summary_path,
        producer="tests.stage1_publication_recovery.actual_registry",
        depends_on=summary_dependencies,
        metadata={
            "pointer_role": "current",
            "current_version_artifact_id": revised_version.artifact_id,
            "summary_set_hash": hash_json(rows),
        },
    )
    manifest_payload = json.loads(Path(old_manifest.path).read_text(encoding="utf-8"))
    manifest_payload["materialized_summary_file"] = str(revised_summary_path.resolve())
    Path(old_manifest.path).write_text(
        json.dumps(manifest_payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    revised_manifest = registry.register_file(
        artifact_id=old_manifest.artifact_id,
        artifact_role=old_manifest.artifact_role,
        artifact_type=old_manifest.artifact_type,
        artifact_version=old_manifest.artifact_version,
        path=old_manifest.path,
        producer=old_manifest.producer,
        depends_on=old_manifest.depends_on,
        metadata=old_manifest.metadata,
    )
    registry.register_file(
        artifact_id=old_progress.artifact_id,
        artifact_role=old_progress.artifact_role,
        artifact_type=old_progress.artifact_type,
        artifact_version=old_progress.artifact_version,
        path=old_progress.path,
        producer=old_progress.producer,
        depends_on=[ArtifactDependencyRefV2.from_record(revised_summary)],
        metadata=old_progress.metadata,
    )
    closure_dependencies = [
        ArtifactDependencyRefV2.from_record(revised_manifest)
        if ref.artifact_id == revised_manifest.artifact_id
        else ref
        for ref in old_closure.depends_on
    ]
    registry.register_file(
        artifact_id=old_closure.artifact_id,
        artifact_role=old_closure.artifact_role,
        artifact_type=old_closure.artifact_type,
        artifact_version=old_closure.artifact_version,
        path=old_closure.path,
        producer=old_closure.producer,
        depends_on=closure_dependencies,
        metadata=old_closure.metadata,
    )
    assert RuntimeReconciler(workspace, registry).load_completed_stage_result("analyze") is None

    proposal = try_recover_registered_stage1_publication(
        workspace,
        registry,
        expected_source_bundle_hash=source_bundle.content_hash,
        expected_runtime_spec_hash=runtime_spec.content_hash,
    )

    assert isinstance(proposal, ProvenStageRecovery)
    assert proposal.model_call_count == 1
    reconciler = RuntimeReconciler(workspace, registry)
    result = reconciler.reconcile(stage_recoveries=(proposal,))
    assert "analyze" in result.completed_stages
    rehydrated = reconciler.load_completed_stage_result("analyze")
    assert rehydrated is not None
    terminals = [terminal for terminal, _path in reconciler.stage_store.load_records()]
    analyze_terminal = next(terminal for terminal in terminals if terminal.stage_name == "analyze")
    assert analyze_terminal.model_call_count == 1


def test_recovers_exact_closed_stage1_publication_without_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, workspace, source_hash, spec_hash, keys = _fixture(tmp_path)
    proof_calls = _install_proof_stubs(monkeypatch)

    proposal = try_recover_registered_stage1_publication(
        workspace,
        registry,  # type: ignore[arg-type]
        expected_source_bundle_hash=source_hash,
        expected_runtime_spec_hash=spec_hash,
    )

    assert isinstance(proposal, ProvenStageRecovery)
    assert proposal.stage_name == "analyze"
    assert proposal.attempt_id == "attempt-original"
    assert proposal.model_call_count == 13
    summary_record = registry.get("summary_file")
    assert summary_record is not None
    saved_summaries = json.loads(Path(summary_record.path).read_text(encoding="utf-8"))
    summary_by_key = {
        str(item["paper_info"]["canonical_paper_key"]): item
        for item in saved_summaries
    }
    assert summary_by_key["paper-1"]["provider"]["receipt_ids"] == [
        "receipt-1",
        "receipt-5",
        "receipt-6",
    ]
    assert summary_by_key["paper-2"]["provider"]["receipt_ids"] == ["receipt-2"]
    output_ids = [ref.artifact_id for ref in proposal.output_artifact_refs]
    assert output_ids[:3] == ["summary_file", "stage1_progress_snapshot", "summary_source_manifest"]
    assert output_ids[3:7] == [
        f"paper:{hashlib.sha256(key.encode()).hexdigest()[:24]}" for key in keys
    ]
    assert output_ids[-3:] == [
        "stage1:provider_expected_call_graph",
        "stage1:provider_receipt_closure",
        "stage1_provider_receipts",
    ]
    assert proof_calls == [{
        "stage": "analyze",
        "closure_id": "stage1:provider_receipt_closure",
        "terminal_probe_id": "stage1-publication-recovery-probe",
        "output_ids": output_ids,
        "registry_job": "stage1-recovery-job",
    }]


def test_returns_none_when_no_stage1_provider_publication_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _MemoryRegistry(tmp_path, "stage1-recovery-job")
    workspace = _TestWorkspace(tmp_path, registry.job_id)
    calls = _install_proof_stubs(monkeypatch)
    monkeypatch.setattr(recovery, "_pretransport_no_call_proven", lambda *_args: True)

    assert try_recover_registered_stage1_publication(
        workspace,
        registry,  # type: ignore[arg-type]
        expected_source_bundle_hash="",
        expected_runtime_spec_hash="",
    ) is None
    assert calls == []


def _bind_zero_budget_controller(
    workspace: _TestWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> ProviderBudgetController:
    run_id = f"run:{workspace.job_id}"
    state_path = workspace.log_path("acceptance_budget_state_v1.json")
    controller = ProviderBudgetController(
        ProviderAggregateBudgetV2(
            max_provider_calls_total=20,
            max_output_tokens_total=100_000,
            max_retry_attempts_total=0,
            max_wall_seconds=300.0,
        )
    )
    controller.bind_state_path(state_path, acceptance_run_id=run_id, state_started=False)
    identity = RuntimeControlIdentityV1(
        job_id=workspace.job_id,
        workspace_path=workspace.root_dir,
        acceptance_run_id=run_id,
        provider_budget_started=True,
    )
    monkeypatch.setattr(
        provider_runtime,
        "current_runtime_control_context",
        lambda: RuntimeControlContextV1(identity, None, None),
    )
    monkeypatch.setattr(provider_runtime, "current_acceptance_execution_context", lambda: None)
    monkeypatch.setattr(provider_runtime, "acceptance_execution_context_from_environment", lambda: None)
    return controller


def test_pretransport_fallback_requires_job_bound_zero_budget_and_no_started_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _TestWorkspace(tmp_path, "stage1-recovery-job")
    controller = _bind_zero_budget_controller(workspace, monkeypatch)

    assert recovery._pretransport_no_call_proven(workspace, controller) is True

    reservation = controller.admit()
    controller.mark_transport_started(reservation)
    assert recovery._pretransport_no_call_proven(workspace, controller) is False


def test_partial_pretransport_graph_can_fall_through_only_with_zero_call_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, workspace, source_hash, spec_hash, _keys = _fixture(tmp_path)
    registry.records.pop("stage1:provider_receipt_closure")
    registry.records.pop("stage1_provider_receipts")
    monkeypatch.setattr(recovery, "_pretransport_no_call_proven", lambda *_args: True)

    assert try_recover_registered_stage1_publication(
        workspace,
        registry,  # type: ignore[arg-type]
        expected_source_bundle_hash=source_hash,
        expected_runtime_spec_hash=spec_hash,
    ) is None


def test_partial_stage1_with_unknown_or_started_transport_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, workspace, source_hash, spec_hash, _keys = _fixture(tmp_path)
    registry.records.pop("stage1:provider_receipt_closure")
    registry.records.pop("stage1_provider_receipts")
    controller = _bind_zero_budget_controller(workspace, monkeypatch)
    reservation = controller.admit()
    controller.mark_transport_started(reservation)

    with pytest.raises(Stage1PublicationRecoveryBlocked, match="prior transport cannot be ruled out"):
        try_recover_registered_stage1_publication(
            workspace,
            registry,  # type: ignore[arg-type]
            expected_source_bundle_hash=source_hash,
            expected_runtime_spec_hash=spec_hash,
            provider_budget_controller=controller,
        )


def _zero_call_typed_fixture(
    tmp_path: Path,
    *,
    nonempty_source: bool = False,
) -> tuple[_MemoryRegistry, _TestWorkspace, str, str]:
    job_id = "stage1-empty-import-job"
    registry = _MemoryRegistry(tmp_path, job_id)
    workspace = _TestWorkspace(tmp_path, job_id)
    reuse_manifest_path = tmp_path / "typed-reuse-manifest.json"
    reuse_manifest_path.write_text(
        json.dumps(
            {
                "artifact_type": "stage1_reusable_summary_manifest",
                "artifact_version": "v2",
                "canonical_paper_key": "paper-imported",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    work_items = (
        [{"canonical_paper_key": "paper-unrelated", "source_paper_id": "paper-unrelated"}]
        if nonempty_source
        else []
    )
    source_snapshot = {
        "summary_only": not nonempty_source,
        "reuse_only_stage1": not nonempty_source,
    }
    source_bundle = registry.add_json(
        "source_bundle",
        "source_bundle",
        "v1",
        {
            "artifact_type": "source_bundle",
            "artifact_version": "v1",
            "job_id": job_id,
            "paper_work_items": work_items,
            "source_snapshot": source_snapshot,
        },
    )
    runtime_spec = registry.add_json(
        "runtime_job_spec",
        "runtime_job_spec",
        "v1",
        {
            "artifact_type": "runtime_job_spec",
            "artifact_version": "v1",
            "job_id": job_id,
            "reuse_summary_files": [] if nonempty_source else [str(reuse_manifest_path)],
        },
    )
    source_scope = None
    graph_dependencies: list[ArtifactDependencyRefV2] = []
    if nonempty_source:
        source_scope = registry.add_json(
            "stage1_source_scope",
            "stage1_source_scope",
            "v1",
            {"artifact_type": "stage1_source_scope", "artifact_version": "v1", "job_id": job_id},
            dependencies=[
                ArtifactDependencyRefV2.from_record(source_bundle),
                ArtifactDependencyRefV2.from_record(runtime_spec),
            ],
        )
        graph_dependencies.append(ArtifactDependencyRefV2.from_record(source_scope))
    graph_hash = hashlib.sha256(b"empty-source-graph").hexdigest()
    epoch_id = hashlib.sha256(b"empty-source-epoch").hexdigest()
    graph = registry.add_json(
        "stage1:provider_expected_call_graph",
        "provider_expected_call_graph",
        "v1",
        {
            "artifact_type": "provider_expected_call_graph",
            "artifact_version": "v1",
            "job_id": job_id,
            "stage_name": "stage1_analyze",
            "attempt_id": "attempt-empty",
            "logical_attempt_identity": "attempt-empty",
            "closure_epoch_id": epoch_id,
            "expected_call_graph_hash": graph_hash,
            "source_bundle_hash": source_bundle.content_hash,
            "runtime_spec_hash": runtime_spec.content_hash,
            "expected_calls": [],
        },
        dependencies=graph_dependencies,
        metadata={"expected_call_graph_hash": graph_hash},
    )
    registry.add_json(
        "stage1:provider_receipt_closure",
        "provider_receipt_closure",
        "v1",
        {
            "artifact_type": "provider_receipt_closure",
            "artifact_version": "v1",
            "job_id": job_id,
            "stage_name": "stage1_analyze",
            "attempt_id": "attempt-empty",
            "logical_attempt_identity": "attempt-empty",
            "closure_epoch_id": epoch_id,
            "expected_call_graph_hash": graph_hash,
            "expected_calls": [],
            "payload": {
                "complete": True,
                "expected_call_ids": [],
                "observed_call_ids": [],
                "missing_call_ids": [],
                "stale_call_ids": [],
                "failed_call_ids": [],
                "incomplete_call_ids": [],
                "unexpected_receipts": [],
                "out_of_scope_receipts": [],
                "out_of_epoch_receipts": [],
                "historical_receipts": [],
                "retry_exceeded_call_ids": [],
                "usage_incomplete_call_ids": [],
                "hash_mismatches": {},
                "verified_reuse_call_ids": [],
            },
        },
        dependencies=[
            ArtifactDependencyRefV2.from_record(source_bundle),
            ArtifactDependencyRefV2.from_record(runtime_spec),
            ArtifactDependencyRefV2.from_record(graph),
        ],
        metadata={
            "closure_epoch_id": epoch_id,
            "expected_call_graph_hash": graph_hash,
        },
    )
    return registry, workspace, source_bundle.content_hash, runtime_spec.content_hash


def test_zero_call_typed_empty_source_reaches_existing_import_verifier(
    tmp_path: Path,
) -> None:
    registry, workspace, source_hash, spec_hash = _zero_call_typed_fixture(tmp_path)

    assert try_recover_registered_stage1_publication(
        workspace,
        registry,  # type: ignore[arg-type]
        expected_source_bundle_hash=source_hash,
        expected_runtime_spec_hash=spec_hash,
    ) is None


def test_zero_call_nonempty_pdf_source_is_not_accepted_as_typed_import(
    tmp_path: Path,
) -> None:
    registry, workspace, source_hash, spec_hash = _zero_call_typed_fixture(
        tmp_path,
        nonempty_source=True,
    )

    with pytest.raises(Stage1PublicationRecoveryBlocked, match="zero-call Stage 1 graph"):
        try_recover_registered_stage1_publication(
            workspace,
            registry,  # type: ignore[arg-type]
            expected_source_bundle_hash=source_hash,
            expected_runtime_spec_hash=spec_hash,
        )


def test_all_verified_reuse_zero_call_closure_uses_existing_reuse_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_stage1_closure_adversarial import _run_stage1, _stage1_papers
    from runtime.runner import AgentRuntimeRunner

    papers = _stage1_papers(tmp_path / "seed-source", prefix="reuse")
    seed_result, _seed_workspace, _seed_registry = _run_stage1(
        tmp_path,
        monkeypatch,
        job_id="stage1-reuse-seed-for-recovery",
        papers=papers,
    )
    seed_inputs = Path(seed_result.workspace_path) / "artifacts" / "stage1" / "inputs"
    seed_summary = next(seed_inputs.glob("stage1_summaries_*.json"))
    _all_result, _all_workspace, all_registry = _run_stage1(
        tmp_path,
        monkeypatch,
        job_id="stage1-all-reuse-for-recovery",
        papers=papers,
        reuse_summary_files=(str(seed_summary),),
        reuse_stage1=True,
    )
    source_bundle = all_registry.get("source_bundle")
    runtime_spec = all_registry.get("runtime_job_spec")
    graph = all_registry.get("stage1:provider_expected_call_graph")
    closure = all_registry.get("stage1:provider_receipt_closure")
    assert source_bundle is not None and runtime_spec is not None
    assert graph is not None and closure is not None
    assert all_registry.get("stage1_provider_receipts") is None
    graph_document = json.loads(Path(graph.path).read_text(encoding="utf-8"))
    external_registry_resolver = AgentRuntimeRunner._external_registry_resolver(_all_workspace)

    proposal = recovery._all_reused_zero_call_recovery(
        workspace=_all_workspace,
        registry=all_registry,
        source_bundle=source_bundle,
        runtime_spec=runtime_spec,
        graph=graph,
        graph_document=graph_document,
        source_document=recovery._payload(
            json.loads(Path(source_bundle.path).read_text(encoding="utf-8"))
        ),
        closure_record=closure,
        ledger_record=None,
        expected_source_bundle_hash=source_bundle.content_hash,
        expected_runtime_spec_hash=runtime_spec.content_hash,
        external_registry_resolver=external_registry_resolver,
        schema_validators=None,
    )
    assert isinstance(proposal, ProvenStageRecovery)
    assert proposal.model_call_count == 0


def test_failed_analyze_terminal_with_pretransport_proof_leaves_ordinary_retry_in_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _MemoryRegistry(tmp_path, "stage1-recovery-job")
    workspace = _TestWorkspace(tmp_path, registry.job_id)
    terminal = TerminalStageRecordV1.create(
        job_id=registry.job_id,
        attempt_id="attempt-failed",
        stage_name="analyze",
        status="failed",
        producer="tests.stage1_publication_recovery",
        output_artifact_refs=(),
        model_call_count=0,
        terminal_reason="pretransport rejection",
    )
    registry.add_json(
        "terminal:analyze",
        "runtime_stage_terminal",
        "v1",
        terminal.to_dict(),
        metadata={"stage_name": "analyze"},
    )
    monkeypatch.setattr(recovery, "_pretransport_no_call_proven", lambda *_args: True)

    assert try_recover_registered_stage1_publication(
        workspace,
        registry,  # type: ignore[arg-type]
        expected_source_bundle_hash="",
        expected_runtime_spec_hash="",
    ) is None


@pytest.mark.parametrize("field", ["source", "spec"])
def test_blocks_if_current_source_or_runtime_spec_pointer_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    registry, workspace, source_hash, spec_hash, _keys = _fixture(tmp_path)
    _install_proof_stubs(monkeypatch)

    with pytest.raises(Stage1PublicationRecoveryBlocked, match="differs"):
        try_recover_registered_stage1_publication(
            workspace,
            registry,  # type: ignore[arg-type]
            expected_source_bundle_hash=("wrong" if field == "source" else source_hash),
            expected_runtime_spec_hash=("wrong" if field == "spec" else spec_hash),
        )


def test_blocks_if_summary_pointer_is_not_current(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry, workspace, source_hash, spec_hash, _keys = _fixture(tmp_path)
    _install_proof_stubs(monkeypatch)
    current = registry.get("summary_file")
    assert current is not None
    registry.records["summary_file"] = replace(
        current,
        metadata={**current.metadata, "pointer_role": "previous"},
    )

    with pytest.raises(Stage1PublicationRecoveryBlocked, match="not the current"):
        try_recover_registered_stage1_publication(
            workspace,
            registry,  # type: ignore[arg-type]
            expected_source_bundle_hash=source_hash,
            expected_runtime_spec_hash=spec_hash,
        )


def test_blocks_if_receipt_closure_call_sets_differ(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry, workspace, source_hash, spec_hash, _keys = _fixture(tmp_path)
    _install_proof_stubs(monkeypatch)
    closure = registry.get("stage1:provider_receipt_closure")
    assert closure is not None
    document = json.loads(Path(closure.path).read_text(encoding="utf-8"))
    document["payload"]["observed_call_ids"] = document["payload"]["observed_call_ids"][:-1]
    Path(closure.path).write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    registry.records[closure.artifact_id] = replace(
        closure,
        content_hash=file_sha256(closure.path),
    )

    with pytest.raises(Stage1PublicationRecoveryBlocked, match="closure proof rejected"):
        try_recover_registered_stage1_publication(
            workspace,
            registry,  # type: ignore[arg-type]
            expected_source_bundle_hash=source_hash,
            expected_runtime_spec_hash=spec_hash,
        )


def test_visual_provider_call_cannot_be_reassigned_to_another_paper_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, workspace, source_hash, spec_hash, _keys = _fixture(tmp_path)
    _install_proof_stubs(monkeypatch)
    graph = registry.get("stage1:provider_expected_call_graph")
    closure = registry.get("stage1:provider_receipt_closure")
    assert graph is not None and closure is not None
    graph_document = json.loads(Path(graph.path).read_text(encoding="utf-8"))
    visual_call = next(
        call for call in graph_document["expected_calls"]
        if str(call["call_id"]).startswith("stage1_visual_scan:")
    )
    visual_call["node_id"] = "paper-2:visual_scan:0"
    Path(graph.path).write_text(json.dumps(graph_document, sort_keys=True), encoding="utf-8")
    updated_graph = replace(graph, content_hash=file_sha256(graph.path))
    registry.records[graph.artifact_id] = updated_graph
    registry.records[closure.artifact_id] = replace(
        closure,
        depends_on=[
            ArtifactDependencyRefV2.from_record(updated_graph)
            if ref.artifact_id == graph.artifact_id else ref
            for ref in closure.depends_on
        ],
    )

    with pytest.raises(Stage1PublicationRecoveryBlocked, match="call/node ownership mismatch"):
        try_recover_registered_stage1_publication(
            workspace,
            registry,  # type: ignore[arg-type]
            expected_source_bundle_hash=source_hash,
            expected_runtime_spec_hash=spec_hash,
        )


def test_blocks_if_source_manifest_paper_coverage_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, workspace, source_hash, spec_hash, _keys = _fixture(tmp_path)
    _install_proof_stubs(monkeypatch)
    manifest = registry.get("summary_source_manifest")
    closure = registry.get("stage1:provider_receipt_closure")
    assert manifest is not None and closure is not None
    document = json.loads(Path(manifest.path).read_text(encoding="utf-8"))
    document["source_items"] = document["source_items"][:-1]
    Path(manifest.path).write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    updated_manifest = replace(manifest, content_hash=file_sha256(manifest.path))
    registry.records[manifest.artifact_id] = updated_manifest
    registry.records[closure.artifact_id] = replace(
        closure,
        depends_on=[
            ArtifactDependencyRefV2.from_record(updated_manifest)
            if ref.artifact_id == manifest.artifact_id else ref
            for ref in closure.depends_on
        ],
    )

    with pytest.raises(Stage1PublicationRecoveryBlocked, match="does not cover"):
        try_recover_registered_stage1_publication(
            workspace,
            registry,  # type: ignore[arg-type]
            expected_source_bundle_hash=source_hash,
            expected_runtime_spec_hash=spec_hash,
        )


@pytest.mark.parametrize(
    "issue",
    [
        "provider_closure_receipt_set_mismatch:analyze",
        "provider_closure_expected_call_identity_mismatch:analyze:call:config_hash",
    ],
)
def test_blocks_when_provider_closure_reader_rejects_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, issue: str,
) -> None:
    registry, workspace, source_hash, spec_hash, _keys = _fixture(tmp_path)
    _install_proof_stubs(monkeypatch, issues=[issue])

    with pytest.raises(Stage1PublicationRecoveryBlocked, match="closure proof rejected recovery"):
        try_recover_registered_stage1_publication(
            workspace,
            registry,  # type: ignore[arg-type]
            expected_source_bundle_hash=source_hash,
            expected_runtime_spec_hash=spec_hash,
        )


def test_blocks_tampered_current_summary_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry, workspace, source_hash, spec_hash, _keys = _fixture(tmp_path)
    _install_proof_stubs(monkeypatch)
    summary = registry.get("summary_file")
    assert summary is not None
    Path(summary.path).write_text("[]", encoding="utf-8")

    with pytest.raises(Stage1PublicationRecoveryBlocked, match="artifact closure is untrusted"):
        try_recover_registered_stage1_publication(
            workspace,
            registry,  # type: ignore[arg-type]
            expected_source_bundle_hash=source_hash,
            expected_runtime_spec_hash=spec_hash,
        )





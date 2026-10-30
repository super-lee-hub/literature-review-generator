"""Provider-free relation-closure batching and integrity regressions."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

import pytest

from outline.v3_artifacts import RelationAdjudicationResult
from runtime.provider_receipt_closure import (
    ExpectedProviderCall,
    ProviderReceiptClosure,
)
from runtime.provider_runtime import ProviderCallReceiptV1, hash_json
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    RegistryError,
)
from validation import closure as closure_module

_UNTRUSTED = "provider_closure_expected_relation_artifact_dependency_untrusted:outline:"


@dataclass
class RelationCase:
    registry: ArtifactRegistry
    leaf: ArtifactRecord
    roots: list[ArtifactRecord]
    calls: list[dict[str, Any]]
    closure: ArtifactRecord
    terminal: ArtifactRecord
    terminal_payload: dict[str, Any]

    def invoke(self) -> tuple[dict[str, Any], list[str]]:
        return closure_module._provider_closure_entry(
            "outline", self.closure, self.terminal, self.terminal_payload, self.registry,
        )


def _relation_case(tmp_path: Path, *, root_count: int = 2) -> RelationCase:
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "relation-batch")

    def register(
        artifact_id: str, artifact_type: str, payload: Any,
        *, version: str = "v1", dependencies: tuple[ArtifactRecord, ...] = (),
    ) -> ArtifactRecord:
        path = tmp_path / f"{artifact_id.replace(':', '_')}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return registry.register_file(
            artifact_id=artifact_id, artifact_role="test", artifact_type=artifact_type,
            artifact_version=version, path=path, producer="relation-closure-test",
            depends_on=[ArtifactDependencyRefV2.from_record(item) for item in dependencies],
        )

    leaf = register("shared-source", "test_source", {"source": "shared evidence"})
    roots: list[ArtifactRecord] = []
    calls: list[dict[str, Any]] = []
    receipts: list[ProviderCallReceiptV1] = []
    for index in range(root_count):
        artifact = RelationAdjudicationResult(
            job_id=registry.job_id,
            payload={"confirmed_relation_ids": [], "rejected_relations": []},
        )
        root = register(
            f"relation-{index}", "relation_adjudication_result", artifact.to_dict(),
            version="v3", dependencies=(leaf,),
        )
        roots.append(root)
        call = ExpectedProviderCall(
            call_id=f"outline:relation:{index}", job_id=registry.job_id,
            attempt_id="attempt", stage_name="outline_v3",
            node_id=f"relation_adjudication:{index}", closure_epoch_id="epoch",
            logical_attempt_identity="attempt", expected_call_graph_hash=hash_json("graph"),
            prompt_hash=hash_json("prompt"), input_hash=hash_json("input"),
            config_hash=hash_json("config"), schema_hash=hash_json("schema"),
            provider_response_hash=hash_json(artifact.payload),
            normalized_output_hash=hash_json(artifact.payload),
            artifact_payload_hash=hash_json(artifact.payload),
            artifact_content_hash=artifact.content_hash, node_output_hash=artifact.content_hash,
            registry_file_hash=root.content_hash, artifact_path=root.path,
        )
        calls.append(asdict(call))
        receipt_fields = {item.name for item in fields(ProviderCallReceiptV1)}
        receipts.append(ProviderCallReceiptV1.from_dict({
            **{key: value for key, value in asdict(call).items() if key in receipt_fields},
            "artifact_type": "provider_call_receipt", "artifact_version": "v2",
            "receipt_id": f"receipt-{index}", "sequence": index + 1,
            "route": "fixture", "provider": "fixture", "model": "fixture",
            "endpoint": "local-fixture", "endpoint_type": "fixture", "status": "success",
            "response_hash": call.provider_response_hash, "attempts": 1,
            "budget": {},
            "started_at": "2026-10-24T00:00:00Z", "finished_at": "2026-10-24T00:00:01Z",
            "metadata": {"registry_file_hash": root.content_hash}, "test_only": False,
        }))
    proof = ProviderReceiptClosure.evaluate(calls, receipts)
    assert proof.complete, proof
    ledger_path = tmp_path / "ledger.jsonl"
    ledger_path.write_text("\n".join(json.dumps(asdict(row)) for row in receipts), encoding="utf-8")
    ledger = registry.register_file(
        artifact_id="ledger", artifact_role="test", artifact_type="provider_receipt_ledger",
        artifact_version="v1", path=ledger_path, producer="relation-closure-test",
    )
    closure = register(
        "outline-v3:provider_receipt_closure", "provider_receipt_closure",
        {
            "artifact_type": "provider_receipt_closure", "artifact_version": "v1",
            "job_id": registry.job_id,
            "payload": {
                **proof.to_dict(), "job_id": registry.job_id, "stage_name": "stage2_outline",
                "attempt_id": "attempt", "logical_attempt_identity": "attempt",
                "expected_call_graph_hash": hash_json("graph"), "expected_calls": calls,
            },
        },
        dependencies=(*roots, ledger),
    )
    terminal_payload = {
        "stage_name": "stage2_outline", "status": "succeeded", "model_call_count": root_count,
        "output_artifact_refs": [ArtifactDependencyRefV2.from_record(closure).to_dict()],
    }
    terminal = register("terminal", "runtime_stage_terminal", terminal_payload, dependencies=(closure,))
    spec_path = tmp_path / "artifacts" / "runtime_job_spec_v1.json"
    spec_path.parent.mkdir()
    spec_path.write_text(json.dumps({"action": "generate_outline"}), encoding="utf-8")
    return RelationCase(registry, leaf, roots, calls, closure, terminal, terminal_payload)


def test_provider_pass_batches_shared_roots_after_independent_expected_file_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _relation_case(tmp_path)
    checks: Counter[str] = Counter()
    expected_hashes: list[str] = []
    batches: list[tuple[str, ...]] = []
    original_verify = ArtifactRegistry._verify_ready_artifact
    original_hash = closure_module.file_sha256
    original_batch = case.registry.verify_ready_artifact_closures

    def counted(cls: Any, record: ArtifactRecord) -> None:
        checks[record.artifact_id] += 1
        original_verify(record)

    def hashed(path: str | Path) -> str:
        expected_hashes.append(str(path))
        return original_hash(path)

    def batched(roots: Any, **kwargs: Any) -> list[ArtifactRecord]:
        rows = tuple(roots)
        batches.append(tuple(row.artifact_id for row in rows))
        assert expected_hashes == [row.path for row in case.roots]
        return original_batch(rows, **kwargs)

    monkeypatch.setattr(ArtifactRegistry, "_verify_ready_artifact", classmethod(counted))
    monkeypatch.setattr(closure_module, "file_sha256", hashed)
    monkeypatch.setattr(case.registry, "verify_ready_artifact_closures", batched)
    entry, issues = case.invoke()

    assert entry["complete"] is True
    assert issues == []
    assert batches == [tuple(row.artifact_id for row in case.roots)]
    assert checks[case.leaf.artifact_id] == 2  # One walk plus the union's late byte fence.


@pytest.mark.parametrize("conflict", ["alias", "hash"])
def test_relation_binding_conflicts_remain_rejected(tmp_path: Path, conflict: str) -> None:
    case = _relation_case(tmp_path)
    root = case.roots[0]
    if conflict == "alias":
        conflicting = replace(ArtifactDependencyRefV2.from_record(root), artifact_id="other-alias")
        case.closure = replace(case.closure, depends_on=[*case.closure.depends_on, conflicting])
        marker = "provider_closure_expected_relation_artifact_dependency_missing:outline:"
    else:
        payload = json.loads(Path(case.closure.path).read_text(encoding="utf-8"))
        payload["payload"]["expected_calls"][0]["registry_file_hash"] = "0" * 64
        Path(case.closure.path).write_text(json.dumps(payload), encoding="utf-8")
        marker = "provider_closure_expected_relation_artifact_dependency_hash_mismatch:outline:"
    _entry, issues = case.invoke()
    assert marker + case.calls[0]["call_id"] in issues


def test_failed_batch_retains_only_affected_call_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _relation_case(tmp_path)
    registry_path = Path(case.registry.registry_path)
    document = json.loads(registry_path.read_text(encoding="utf-8"))
    bad = next(row for row in document["artifacts"] if row["artifact_id"] == case.roots[1].artifact_id)
    bad["depends_on"].append({
        **ArtifactDependencyRefV2.from_record(case.leaf).to_dict(), "artifact_id": "missing-source",
    })
    registry_path.write_text(json.dumps(document), encoding="utf-8")
    case.registry = ArtifactRegistry(registry_path, case.registry.job_id)
    original_single = case.registry.verify_ready_artifact_closure
    singleton_ids: list[str] = []

    def singleton(root: ArtifactRecord, **kwargs: Any) -> ArtifactRecord:
        singleton_ids.append(root.artifact_id)
        return original_single(root, **kwargs)

    monkeypatch.setattr(case.registry, "verify_ready_artifact_closure", singleton)
    _entry, issues = case.invoke()
    errors = [item for item in issues if item.startswith(_UNTRUSTED)]
    assert singleton_ids == [row.artifact_id for row in case.roots]
    assert len(errors) == 1
    assert errors[0].startswith(_UNTRUSTED + case.calls[1]["call_id"] + ":")
    assert "missing-source" in errors[0]


def test_union_failure_cannot_disappear_when_singleton_retries_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _relation_case(tmp_path)
    original_batch = case.registry.verify_ready_artifact_closures

    def failed_union(roots: Any, **kwargs: Any) -> list[ArtifactRecord]:
        rows = tuple(roots)
        if len(rows) > 1:
            raise RegistryError("union snapshot drift")
        return original_batch(rows, **kwargs)

    monkeypatch.setattr(case.registry, "verify_ready_artifact_closures", failed_union)
    _entry, issues = case.invoke()
    assert issues == [
        (
            "provider_closure_expected_relation_artifact_dependency_batch_untrusted:"
            "outline:union snapshot drift"
        )
    ]
    monkeypatch.setattr(case.registry, "verify_ready_artifact_closures", original_batch)
    assert case.invoke()[1] == []


@pytest.mark.parametrize("target", ["shared_source", "expected_output"])
def test_fresh_invocation_detects_byte_tamper(tmp_path: Path, target: str) -> None:
    case = _relation_case(tmp_path)
    assert case.invoke()[1] == []
    record = case.leaf if target == "shared_source" else case.roots[0]
    Path(record.path).write_text("{}", encoding="utf-8")
    _entry, issues = case.invoke()
    assert any(item.startswith(_UNTRUSTED) for item in issues)
    if target == "expected_output":
        assert "provider_closure_expected_file_hash_mismatch:outline:" + case.calls[0]["call_id"] in issues


def test_source_tamper_after_expected_hash_checks_is_not_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _relation_case(tmp_path)
    original_hash = closure_module.file_sha256

    def hashed(path: str | Path) -> str:
        digest = original_hash(path)
        if str(path) == case.roots[-1].path:
            Path(case.leaf.path).write_text("{}", encoding="utf-8")
        return digest

    monkeypatch.setattr(closure_module, "file_sha256", hashed)
    _entry, issues = case.invoke()
    assert len([item for item in issues if item.startswith(_UNTRUSTED)]) == len(case.calls)


@pytest.mark.parametrize("drift", ["revision", "same_revision_dependencies"])
def test_outer_registry_fence_rejects_drift_before_relation_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str,
) -> None:
    case = _relation_case(tmp_path)
    original_hash = closure_module.file_sha256

    def hashed(path: str | Path) -> str:
        digest = original_hash(path)
        if str(path) == case.roots[-1].path:
            registry_path = Path(case.registry.registry_path)
            document = json.loads(registry_path.read_text(encoding="utf-8"))
            if drift == "revision":
                document["revision"] += 1
            else:
                row = next(item for item in document["artifacts"] if item["artifact_id"] == case.roots[0].artifact_id)
                row["depends_on"].append({
                    **ArtifactDependencyRefV2.from_record(case.leaf).to_dict(), "artifact_id": "missing-source",
                })
            registry_path.write_text(json.dumps(document), encoding="utf-8")
        return digest

    monkeypatch.setattr(closure_module, "file_sha256", hashed)
    closure_map = closure_module.resolve_current_stage_closure_map(case.registry)
    assert "current_stage_closure_registry_snapshot_changed" in closure_map.blocking_issues
    if drift == "same_revision_dependencies":
        assert any(item.startswith(_UNTRUSTED) for item in closure_map.blocking_issues)

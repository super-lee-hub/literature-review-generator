from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from runtime.provider_receipt_closure import (
    ExpectedProviderCall,
    ProviderReceiptClosure,
)
from runtime.provider_runtime import (
    ProviderCallReceiptV1,
    ProviderRuntimeLedger,
    hash_json,
)
from services import stage1_reuse
from services.artifact_registry import ArtifactRegistry, RegistryError, file_sha256
from services.stage1_reuse import (
    Stage1ReusableSummaryBindingV1,
    Stage1ReusableSummaryManifestV1,
    build_binding_hash,
    verified_typed_reuse_registry_paths,
    verify_stage1_typed_manifest_authority,
)
from tests import test_run_first_typed_registry_bootstrap as bootstrap_fixtures

typed_parent_child = bootstrap_fixtures.typed_parent_child


@pytest.fixture()
def shared_provider_case(tmp_path: Path) -> Any:
    owner = ArtifactRegistry(tmp_path / "owner.json", "proof-owner")
    child = ArtifactRegistry(tmp_path / "child.json", "proof-child")
    output_paths: list[Path] = []
    expected_calls: list[dict[str, Any]] = []
    ledger_path = tmp_path / "receipts.jsonl"
    ledger = ProviderRuntimeLedger(ledger_path)
    for index in range(3):
        payload = {"summary": f"Source-supported finding {index}."}
        output = tmp_path / f"output-{index}.json"
        output.write_text(json.dumps({"analysis": payload, "content_hash": hash_json(payload)}))
        output_paths.append(output)
        call = ExpectedProviderCall(
            call_id=f"call-{index}", job_id=owner.job_id, attempt_id="attempt-1",
            stage_name="stage1_analyze", node_id=f"node-{index}",
            prompt_hash=hash_json("prompt"), input_hash=hash_json("input"),
            config_hash=hash_json("config"), schema_hash=hash_json("schema"),
            closure_epoch_id="epoch-1", normalized_output_hash=hash_json(payload),
            provider_response_hash=hash_json(payload), artifact_payload_hash=hash_json(payload),
            artifact_content_hash=hash_json(payload), node_output_hash=hash_json(payload),
            registry_file_hash=file_sha256(output), artifact_path=str(output),
        )
        expected_calls.append(asdict(call))
        receipt = ProviderCallReceiptV1.from_dict({
            "artifact_type": "provider_call_receipt", "artifact_version": "v2",
            "receipt_id": f"receipt-{index}", "sequence": index + 1,
            "job_id": owner.job_id, "attempt_id": "attempt-1",
            "stage_name": "stage1_analyze", "node_id": call.node_id, "call_id": call.call_id,
            "closure_epoch_id": "epoch-1", "route": "reader", "provider": "fixture",
            "model": "fixture", "endpoint": "127.0.0.1:1", "status": "success",
            "budget": {}, "prompt_hash": call.prompt_hash, "input_hash": call.input_hash,
            "config_hash": call.config_hash, "schema_hash": call.schema_hash,
            "http_status": 200, "attempts": 1, "response_hash": hash_json(payload),
            "started_at": "2026-10-07T00:00:00Z", "finished_at": "2026-10-07T00:00:01Z",
            "metadata": {"registry_file_path": str(output), "registry_file_hash": file_sha256(output)},
            "test_only": True,
        })
        ledger.append(receipt)
    proof = ProviderReceiptClosure.evaluate(expected_calls, ledger.list_receipts())
    assert proof.complete, proof
    closure_path = tmp_path / "closure.json"
    closure_path.write_text(json.dumps({
        "artifact_type": "provider_receipt_closure", "artifact_version": "v1",
        "job_id": owner.job_id, "stage_name": "stage1_analyze",
        "expected_calls": expected_calls, "payload": proof.to_dict(),
    }))
    rows: list[dict[str, Any]] = []
    for index in range(3):
        info = {"canonical_paper_key": f"paper-{index}"}
        summary = {"summary": f"Finding for paper {index}."}
        row = {"paper_info": info, "ai_summary": summary, "summary_payload_hash": hash_json(summary)}
        source_path = tmp_path / f"summary-{index}.json"
        source_path.write_text(json.dumps([row]))
        source = owner.register_file(
            artifact_id=f"summary-{index}", artifact_type="summary_file", artifact_version="v1",
            artifact_role="source", path=source_path, producer="test-fixture",
        )
        binding = Stage1ReusableSummaryBindingV1(
            canonical_paper_key=info["canonical_paper_key"], source_kind="provider_generated",
            source_authority_job_id=owner.job_id,
            source_authority_registry_id=f"artifact-registry:{owner.job_id}",
            source_authority_registry_path=str(owner.registry_path), source_authority_registry_revision="1",
            source_authority_artifact_id=source.artifact_id,
            source_authority_artifact_hash=source.content_hash,
            source_authority_artifact_path=source.path,
            summary_payload_hash=hash_json(summary), normalized_summary_payload_hash=hash_json(summary),
            source_provider_receipt_closure_id="closure", source_provider_receipt_closure_hash=file_sha256(closure_path),
            source_provider_receipt_ledger_id="ledger", source_provider_receipt_ledger_hash=file_sha256(ledger_path),
        )
        manifest = Stage1ReusableSummaryManifestV1(
            job_id=owner.job_id, canonical_paper_key=info["canonical_paper_key"], source_kind="provider_generated",
            source_summary_artifact_id=source.artifact_id, source_summary_artifact_hash=source.content_hash,
            source_summary_artifact_path=source.path, source_registry_identity=binding.source_authority_registry_id,
            source_registry_revision="1", summary_payload_hash=hash_json(summary),
            normalized_summary_payload_hash=hash_json(summary), binding=binding.to_dict(),
            binding_hash=build_binding_hash(binding.to_dict()), paper_info=info, summary_payload=summary,
            provider_receipt_closure_id="closure", provider_receipt_closure_hash=file_sha256(closure_path),
            provider_receipt_closure_path=str(closure_path), provider_receipt_ledger_id="ledger",
            provider_receipt_ledger_hash=file_sha256(ledger_path), provider_receipt_ledger_path=str(ledger_path),
        ).to_dict()
        manifest["manifest_content_hash"] = hash_json({**manifest, "manifest_content_hash": ""})
        manifest_path = tmp_path / f"manifest-{index}.json"
        manifest_path.write_text(json.dumps(manifest))
        row["stage1_reuse"] = {
            "authority_kind": "typed_manifest", "typed_manifest_path": str(manifest_path),
            "typed_manifest_artifact_id": f"manifest-{index}",
            "typed_manifest_artifact_hash": file_sha256(manifest_path), "binding": binding.to_dict(),
        }
        rows.append(row)
    child_summary = tmp_path / "current-summaries.json"
    child_summary.write_text(json.dumps(rows))
    child.register_file(
        artifact_id="summary_file", artifact_type="summary_file", artifact_version="v1",
        artifact_role="source", path=child_summary, producer="test-fixture",
    )
    return SimpleNamespace(
        parent_registry=owner, child_registry=child, rows=rows,
        output_paths=output_paths, closure_path=closure_path, ledger_path=ledger_path,
        expected_calls=expected_calls,
    )


def test_bootstrap_recomputes_identical_shared_provider_proof_once(
    shared_provider_case: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = shared_provider_case
    original = ProviderReceiptClosure.evaluate
    evaluations: list[tuple[Any, ...]] = []

    def tracked(cls: type, *args: Any, **kwargs: Any) -> Any:
        evaluations.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(ProviderReceiptClosure, "evaluate", classmethod(tracked))
    assert verified_typed_reuse_registry_paths(case.child_registry) == (
        str(Path(case.parent_registry.registry_path).resolve()),
    )
    assert len(evaluations) == 1


def test_runtime_parent_child_reuses_shared_provider_proof_once(
    typed_parent_child: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = typed_parent_child
    original = ProviderReceiptClosure.evaluate
    evaluations: list[tuple[Any, ...]] = []

    def tracked(cls: type, *args: Any, **kwargs: Any) -> Any:
        evaluations.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(ProviderReceiptClosure, "evaluate", classmethod(tracked))
    assert verified_typed_reuse_registry_paths(case.child_registry) == (
        str(Path(case.parent_registry.registry_path).resolve()),
    )
    assert len(evaluations) == 1


@pytest.mark.parametrize("target", ("closure", "ledger", "output"))
def test_bootstrap_rechecks_shared_proof_files_at_scope_exit(
    shared_provider_case: Any, monkeypatch: pytest.MonkeyPatch, target: str,
) -> None:
    case = shared_provider_case
    original = stage1_reuse.verify_stage1_typed_manifest_authority
    verified_papers: list[str] = []

    def mutate_after_last_paper(row: Any, binding: Any, **kwargs: Any) -> Any:
        result = original(row, binding, **kwargs)
        assert result[0] is not None, result[1]
        verified_papers.append(binding.canonical_paper_key)
        if len(verified_papers) == 3:
            path = {
                "closure": case.closure_path, "ledger": case.ledger_path,
                "output": case.output_paths[0],
            }[target]
            with path.open("a") as handle:
                handle.write("\nlate-byte-drift\n")
        return result

    monkeypatch.setattr(stage1_reuse, "verify_stage1_typed_manifest_authority", mutate_after_last_paper)
    with pytest.raises(RegistryError, match="changed during verification"):
        verified_typed_reuse_registry_paths(case.child_registry)
    assert len(verified_papers) == 3


@pytest.mark.parametrize("target", ("source", "manifest"))
def test_shared_proof_cannot_hide_individual_paper_tampering(
    shared_provider_case: Any, target: str,
) -> None:
    case = shared_provider_case
    row = case.rows[1]
    path = (
        Path(row["stage1_reuse"]["binding"]["source_authority_artifact_path"])
        if target == "source" else Path(row["stage1_reuse"]["typed_manifest_path"])
    )
    with path.open("a") as handle:
        handle.write("\nindividual-byte-drift\n")
    with pytest.raises(RegistryError, match="hash"):
        verified_typed_reuse_registry_paths(case.child_registry)


def test_standalone_verification_still_recomputes_and_rejects_changed_output(
    shared_provider_case: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = shared_provider_case
    original = ProviderReceiptClosure.evaluate
    evaluations: list[tuple[Any, ...]] = []

    def tracked(cls: type, *args: Any, **kwargs: Any) -> Any:
        evaluations.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(ProviderReceiptClosure, "evaluate", classmethod(tracked))
    for row in case.rows:
        binding = Stage1ReusableSummaryBindingV1.from_mapping(row["stage1_reuse"]["binding"])
        authority, reason = verify_stage1_typed_manifest_authority(row, binding)
        assert authority is not None, reason
    assert len(evaluations) == 3
    case.output_paths[0].write_text("changed output")
    row = case.rows[0]
    binding = Stage1ReusableSummaryBindingV1.from_mapping(row["stage1_reuse"]["binding"])
    authority, reason = verify_stage1_typed_manifest_authority(row, binding)
    assert authority is None
    assert reason == "typed_manifest_provider_closure_recompute_mismatch"


def test_scope_cannot_be_reused_after_its_final_byte_check(shared_provider_case: Any) -> None:
    case = shared_provider_case
    row = case.rows[0]
    binding = Stage1ReusableSummaryBindingV1.from_mapping(row["stage1_reuse"]["binding"])
    with stage1_reuse._TypedManifestVerificationScope() as scope:
        authority, reason = verify_stage1_typed_manifest_authority(
            row, binding, _verification_scope=scope,
        )
        assert authority is not None, reason
    authority, reason = verify_stage1_typed_manifest_authority(
        row, binding, _verification_scope=scope,
    )
    assert authority is None
    assert "scope is not active" in reason
    with pytest.raises(RuntimeError, match="cannot be reused"), scope:
        pass


@pytest.mark.parametrize(
    "identity", (
        "closure_hash", "ledger_hash",
        "job_id", "stage_name", "expected_calls", "declared_closure",
    ),
)
def test_distinct_shared_proof_identity_cannot_inherit_a_prior_success(
    shared_provider_case: Any, monkeypatch: pytest.MonkeyPatch, identity: str,
) -> None:
    case = shared_provider_case
    closure = json.loads(case.closure_path.read_text())
    kwargs = {
        "closure_path": case.closure_path, "closure_hash": file_sha256(case.closure_path),
        "ledger_path": case.ledger_path, "ledger_hash": file_sha256(case.ledger_path),
        "job_id": case.parent_registry.job_id, "stage_name": "stage1_analyze",
        "expected_calls": closure["expected_calls"], "declared_closure": closure["payload"],
    }
    original = ProviderReceiptClosure.evaluate
    evaluations: list[tuple[Any, ...]] = []

    def tracked(cls: type, *args: Any, **extra: Any) -> Any:
        evaluations.append(args)
        return original(*args, **extra)

    monkeypatch.setattr(ProviderReceiptClosure, "evaluate", classmethod(tracked))
    with stage1_reuse._TypedManifestVerificationScope() as scope:
        assert scope.verify_provider_closure(**kwargs)
        altered = dict(kwargs)
        if identity.endswith("_hash"):
            altered[identity] = "f" * 64
        elif identity in {"job_id", "stage_name"}:
            altered[identity] = "other-identity"
        elif identity == "expected_calls":
            altered[identity] = json.loads(json.dumps(kwargs[identity]))
            altered[identity][0]["prompt_hash"] = "f" * 64
        else:
            altered[identity] = {**kwargs[identity], "closure_hash": "f" * 64}
        if identity.endswith("_hash"):
            with pytest.raises(RegistryError, match="proof bytes changed"):
                scope.verify_provider_closure(**altered)
            assert len(evaluations) == 1
        else:
            verified = scope.verify_provider_closure(**altered)
            assert verified is (identity not in {"expected_calls", "declared_closure"})
            assert len(evaluations) == 2


@pytest.mark.parametrize("copy_field", ("closure_path", "ledger_path"))
@pytest.mark.parametrize("tamper_before_recheck", (False, True))
def test_portable_proof_copy_shares_computation_but_requires_exact_bytes(
    shared_provider_case: Any, monkeypatch: pytest.MonkeyPatch,
    copy_field: str, tamper_before_recheck: bool,
) -> None:
    case = shared_provider_case
    closure = json.loads(case.closure_path.read_text())
    kwargs = {
        "closure_path": case.closure_path, "closure_hash": file_sha256(case.closure_path),
        "ledger_path": case.ledger_path, "ledger_hash": file_sha256(case.ledger_path),
        "job_id": case.parent_registry.job_id, "stage_name": "stage1_analyze",
        "expected_calls": closure["expected_calls"], "declared_closure": closure["payload"],
    }
    original = ProviderReceiptClosure.evaluate
    evaluations: list[tuple[Any, ...]] = []

    def tracked(cls: type, *args: Any, **extra: Any) -> Any:
        evaluations.append(args)
        return original(*args, **extra)

    monkeypatch.setattr(ProviderReceiptClosure, "evaluate", classmethod(tracked))
    copy_path = kwargs[copy_field].with_name("copy-" + kwargs[copy_field].name)
    copy_path.write_bytes(kwargs[copy_field].read_bytes())
    with stage1_reuse._TypedManifestVerificationScope() as scope:
        assert scope.verify_provider_closure(**kwargs)
        copied = {**kwargs, copy_field: copy_path}
        if tamper_before_recheck:
            with copy_path.open("ab") as handle:
                handle.write(b"\nchanged-copy\n")
            with pytest.raises(RegistryError, match="proof bytes changed"):
                scope.verify_provider_closure(**copied)
        else:
            assert scope.verify_provider_closure(**copied)
        assert len(evaluations) == 1


@pytest.mark.parametrize("copy_field", ("closure_path", "ledger_path"))
def test_scope_rechecks_portable_copy_after_shared_proof_hit(
    shared_provider_case: Any, copy_field: str,
) -> None:
    case = shared_provider_case
    closure = json.loads(case.closure_path.read_text())
    kwargs = {
        "closure_path": case.closure_path, "closure_hash": file_sha256(case.closure_path),
        "ledger_path": case.ledger_path, "ledger_hash": file_sha256(case.ledger_path),
        "job_id": case.parent_registry.job_id, "stage_name": "stage1_analyze",
        "expected_calls": closure["expected_calls"], "declared_closure": closure["payload"],
    }
    copy_path = kwargs[copy_field].with_name("copy-" + kwargs[copy_field].name)
    copy_path.write_bytes(kwargs[copy_field].read_bytes())
    with pytest.raises(RegistryError, match="changed during verification"):
        with stage1_reuse._TypedManifestVerificationScope() as scope:
            assert scope.verify_provider_closure(**kwargs)
            assert scope.verify_provider_closure(**{**kwargs, copy_field: copy_path})
            with copy_path.open("ab") as handle:
                handle.write(b"\nlate-copy-byte-drift\n")


def test_analyze_closure_reader_reports_late_authority_drift_as_blocked(
    shared_provider_case: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from validation import closure as closure_module

    case = shared_provider_case
    closure = json.loads(case.closure_path.read_text())

    def verified_then_drift(*args: Any, _verification_scope: Any, **kwargs: Any) -> Any:
        assert _verification_scope.verify_provider_closure(
            closure_path=case.closure_path, closure_hash=file_sha256(case.closure_path),
            ledger_path=case.ledger_path, ledger_hash=file_sha256(case.ledger_path),
            job_id=case.parent_registry.job_id, stage_name="stage1_analyze",
            expected_calls=closure["expected_calls"], declared_closure=closure["payload"],
        )
        case.output_paths[0].write_text("changed after the closure calculation")
        return {"artifact_id": "retained-proof", "status": "complete", "complete": True}, []

    monkeypatch.setattr(closure_module, "_provider_closure_entry_impl", verified_then_drift)

    entry, issues = closure_module._provider_closure_entry(
        "analyze", None, None, {}, case.child_registry,
    )

    assert entry == {"artifact_id": "retained-proof", "status": "blocked", "complete": False}
    assert any(issue.startswith("provider_closure_reuse_authority_changed:analyze:") for issue in issues)


def test_analyze_closure_reader_preserves_impl_registry_errors(
    shared_provider_case: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from validation import closure as closure_module

    def failed_impl(*args: Any, **kwargs: Any) -> Any:
        raise RegistryError("implementation failed before returning a descriptor")

    monkeypatch.setattr(closure_module, "_provider_closure_entry_impl", failed_impl)
    with pytest.raises(RegistryError, match="implementation failed"):
        closure_module._provider_closure_entry(
            "analyze", None, None, {}, shared_provider_case.child_registry,
        )


def test_scope_rechecks_output_path_from_selected_receipt_metadata(
    shared_provider_case: Any,
) -> None:
    case = shared_provider_case
    closure = json.loads(case.closure_path.read_text())
    for call in closure["expected_calls"]:
        call["artifact_path"] = ""
    with (
        pytest.raises(RegistryError, match="changed during verification"),
        stage1_reuse._TypedManifestVerificationScope() as scope,
    ):
        assert scope.verify_provider_closure(
            closure_path=case.closure_path, closure_hash=file_sha256(case.closure_path),
            ledger_path=case.ledger_path, ledger_hash=file_sha256(case.ledger_path),
            job_id=case.parent_registry.job_id, stage_name="stage1_analyze",
            expected_calls=closure["expected_calls"], declared_closure=closure["payload"],
        )
        case.output_paths[-1].write_text("late metadata-bound output mutation")


@pytest.mark.parametrize("target", ("manifest", "source"))
def test_scope_rechecks_selected_manifest_and_source_at_scope_exit(
    shared_provider_case: Any,
    target: str,
) -> None:
    case = shared_provider_case
    row = case.rows[-1]
    binding = Stage1ReusableSummaryBindingV1.from_mapping(
        row["stage1_reuse"]["binding"]
    )
    path = (
        Path(row["stage1_reuse"]["typed_manifest_path"])
        if target == "manifest"
        else Path(binding.source_authority_artifact_path)
    )

    with pytest.raises(RegistryError, match="changed during verification"):
        with stage1_reuse._TypedManifestVerificationScope() as scope:
            authority, reason = verify_stage1_typed_manifest_authority(
                row, binding, _verification_scope=scope,
            )
            assert authority is not None, reason
            with path.open("ab") as handle:
                handle.write(b"\nlate-authority-byte-drift\n")


def test_fresh_operation_scope_recomputes_identical_provider_proof(
    shared_provider_case: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = shared_provider_case
    original = ProviderReceiptClosure.evaluate
    evaluations: list[tuple[Any, ...]] = []

    def tracked(cls: type, *args: Any, **kwargs: Any) -> Any:
        evaluations.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(ProviderReceiptClosure, "evaluate", classmethod(tracked))
    for _operation in range(2):
        with stage1_reuse._TypedManifestVerificationScope() as scope:
            for row in case.rows:
                binding = Stage1ReusableSummaryBindingV1.from_mapping(
                    row["stage1_reuse"]["binding"]
                )
                authority, reason = verify_stage1_typed_manifest_authority(
                    row, binding, _verification_scope=scope,
                )
                assert authority is not None, reason
    assert len(evaluations) == 2

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from outline.v3_models import PaperContentLayers
from runtime import control_plane as control_plane_module
from runtime.provider_receipt_closure import ProviderReceiptClosure
from runtime.provider_runtime import ProviderRuntimeLedger, hash_json
from services import stage1_reuse
from services.artifact_registry import RegistryError, file_sha256
from services.stage1_reuse import build_binding_hash
from tests.test_current_stage1_generation import _canonical_summary

pytest_plugins = ("tests.test_run_first_typed_shared_receipt_verification",)


def _summary_path(case: Any) -> Path:
    record = case.child_registry.get("summary_file")
    assert record is not None
    return Path(record.path)


def _canonicalize_case(case: Any) -> None:
    """Bind portable V1 manifests to canonical summary_v2_lite source bytes."""

    source_records: list[tuple[Any, dict[str, Any], Any, str]] = []
    for index, row in enumerate(case.rows):
        paper_key = str(row["paper_info"]["canonical_paper_key"])
        title = f"Evidence-bound paper {index}"
        summary = deepcopy(_canonical_summary())
        summary["paper_metadata"]["title"] = title
        summary["paper_metadata"]["doi"] = f"10.1000/portable-{index}"
        finding = f"The treatment improved the outcome for paper {index}."
        summary["core_analysis"]["summary"] = f"{title} reports a source-grounded result."
        summary["core_analysis"]["key_points"] = [finding]
        summary["core_analysis"]["findings"] = finding

        paper_info = dict(row["paper_info"])
        paper_info.update(
            {
                "source_paper_id": paper_key,
                "title": title,
                "authors": ["Example Author"],
                "year": 2025,
            }
        )
        summary_hash = hash_json(summary)
        row["paper_info"] = paper_info
        row["ai_summary"] = summary
        row["summary_payload_hash"] = summary_hash

        binding = row["stage1_reuse"]["binding"]
        source_path = Path(binding["source_authority_artifact_path"])
        source_rows = json.loads(source_path.read_text(encoding="utf-8"))
        source_rows[0]["paper_info"] = paper_info
        source_rows[0]["ai_summary"] = summary
        source_rows[0]["summary_payload_hash"] = summary_hash
        source_path.write_text(json.dumps(source_rows), encoding="utf-8")

        old_record = case.parent_registry.get(f"summary-{index}")
        assert old_record is not None
        updated_record = case.parent_registry.register_file(
            artifact_role=old_record.artifact_role,
            artifact_type=old_record.artifact_type,
            artifact_version=old_record.artifact_version,
            path=source_path,
            producer=old_record.producer,
            artifact_id=old_record.artifact_id,
            expected_revision=case.parent_registry.revision,
            metadata=old_record.metadata,
        )
        source_records.append(
            (row, summary, updated_record, str(case.parent_registry.revision))
        )

    for row, summary, source_record, source_registry_revision in source_records:
        summary_hash = hash_json(summary)
        binding = dict(row["stage1_reuse"]["binding"])
        binding.update(
            {
                "source_authority_artifact_hash": source_record.content_hash,
                "source_authority_registry_revision": source_registry_revision,
                "summary_payload_hash": summary_hash,
                "normalized_summary_payload_hash": summary_hash,
            }
        )
        manifest_path = Path(row["stage1_reuse"]["typed_manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            {
                "source_summary_artifact_hash": source_record.content_hash,
                "source_registry_revision": source_registry_revision,
                "summary_payload_hash": summary_hash,
                "normalized_summary_payload_hash": summary_hash,
                "binding": binding,
                "binding_hash": build_binding_hash(binding),
                "paper_info": dict(row["paper_info"]),
                "summary_payload": summary,
                "manifest_content_hash": "",
            }
        )
        manifest["manifest_content_hash"] = hash_json(
            {**manifest, "manifest_content_hash": ""}
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        row["stage1_reuse"]["binding"] = binding
        row["stage1_reuse"]["typed_manifest_artifact_hash"] = file_sha256(
            manifest_path
        )

    child_summary_path = _summary_path(case)
    child_summary_path.write_text(json.dumps(case.rows), encoding="utf-8")
    child_record = case.child_registry.get("summary_file")
    assert child_record is not None
    case.child_registry.register_file(
        artifact_role=child_record.artifact_role,
        artifact_type=child_record.artifact_type,
        artifact_version=child_record.artifact_version,
        path=child_summary_path,
        producer=child_record.producer,
        artifact_id=child_record.artifact_id,
        expected_revision=case.child_registry.revision,
        metadata=child_record.metadata,
    )


def _wrap_real_authority_verifier(
    monkeypatch: pytest.MonkeyPatch,
    *,
    after_success: Callable[[int, Any, Any], None] | None = None,
) -> list[Any]:
    original = stage1_reuse.verify_stage1_typed_manifest_authority
    verified: list[Any] = []

    def tracked(row: Any, binding: Any, **kwargs: Any) -> Any:
        result = original(row, binding, **kwargs)
        assert result[0] is not None, result[1]
        verified.append(row)
        if after_success is not None:
            after_success(len(verified), row, binding)
        return result

    # ReviewControlPlane imports the verifier directly, so patch that public
    # call site while delegating every call to the real services verifier.
    monkeypatch.setattr(
        control_plane_module,
        "verify_stage1_typed_manifest_authority",
        tracked,
    )
    return verified


def _wrap_real_closure_evaluator(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[Any, ...]]:
    original = ProviderReceiptClosure.evaluate
    evaluations: list[tuple[Any, ...]] = []

    def tracked(cls: type, *args: Any, **kwargs: Any) -> Any:
        evaluations.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        ProviderReceiptClosure,
        "evaluate",
        classmethod(tracked),
    )
    return evaluations


def _plan(case: Any, *, repo_root: Path, summary_files: list[Path] | None = None) -> dict[str, Any]:
    # With no repo-local config.ini, chunk_plan keeps route planning unavailable
    # and returns its provider-free plan without constructing a provider.
    return control_plane_module.ReviewControlPlane(repo_root=repo_root).chunk_plan(
        summary_files if summary_files is not None else [_summary_path(case)],
        job_id="chunk-plan-shared-authority-test",
    )


def _make_third_manifest_use_distinct_proof(case: Any, tmp_path: Path) -> None:
    row = case.rows[2]
    binding = dict(row["stage1_reuse"]["binding"])
    expected_call = case.expected_calls[2]
    receipt = next(
        item
        for item in ProviderRuntimeLedger(str(case.ledger_path)).list_receipts()
        if item.call_id == expected_call["call_id"]
    )

    distinct_ledger_path = tmp_path / "distinct-receipts.jsonl"
    distinct_ledger = ProviderRuntimeLedger(str(distinct_ledger_path))
    distinct_ledger.append(receipt)

    proof = ProviderReceiptClosure.evaluate([expected_call], [receipt])
    assert proof.complete, proof
    distinct_closure_path = tmp_path / "distinct-closure.json"
    distinct_closure_path.write_text(
        json.dumps(
            {
                "artifact_type": "provider_receipt_closure",
                "artifact_version": "v1",
                "job_id": case.parent_registry.job_id,
                "stage_name": "stage1_analyze",
                "expected_calls": [expected_call],
                "payload": proof.to_dict(),
            }
        ),
        encoding="utf-8",
    )

    closure_hash = file_sha256(distinct_closure_path)
    ledger_hash = file_sha256(distinct_ledger_path)
    binding.update(
        {
            "source_provider_receipt_closure_id": "closure-distinct",
            "source_provider_receipt_closure_hash": closure_hash,
            "source_provider_receipt_ledger_id": "ledger-distinct",
            "source_provider_receipt_ledger_hash": ledger_hash,
        }
    )

    manifest_path = Path(row["stage1_reuse"]["typed_manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "binding": binding,
            "binding_hash": build_binding_hash(binding),
            "provider_receipt_closure_id": "closure-distinct",
            "provider_receipt_closure_hash": closure_hash,
            "provider_receipt_closure_path": str(distinct_closure_path),
            "provider_receipt_ledger_id": "ledger-distinct",
            "provider_receipt_ledger_hash": ledger_hash,
            "provider_receipt_ledger_path": str(distinct_ledger_path),
            "manifest_content_hash": "",
        }
    )
    manifest["manifest_content_hash"] = hash_json(
        {**manifest, "manifest_content_hash": ""}
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    row["stage1_reuse"]["binding"] = binding
    row["stage1_reuse"]["typed_manifest_artifact_hash"] = file_sha256(manifest_path)
    _summary_path(case).write_text(json.dumps(case.rows), encoding="utf-8")


@pytest.mark.parametrize("portable_files", [False, True])
def test_public_chunk_plan_evaluates_one_identical_shared_receipt_proof_once(
    shared_provider_case: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    portable_files: bool,
) -> None:
    case = shared_provider_case
    _canonicalize_case(case)
    verified = _wrap_real_authority_verifier(monkeypatch)
    evaluations = _wrap_real_closure_evaluator(monkeypatch)

    inputs = [Path(row["stage1_reuse"]["typed_manifest_path"]) for row in case.rows] if portable_files else None
    result = _plan(case, repo_root=tmp_path, summary_files=inputs)

    assert len(verified) == 3
    assert len(evaluations) == 1
    assert result["provider_posts_emitted"] == 0
    assert result["read_only"] is True


def test_public_chunk_plan_recomputes_distinct_receipt_proofs_independently(
    shared_provider_case: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = shared_provider_case
    _canonicalize_case(case)
    _make_third_manifest_use_distinct_proof(case, tmp_path)
    verified = _wrap_real_authority_verifier(monkeypatch)
    evaluations = _wrap_real_closure_evaluator(monkeypatch)

    result = _plan(case, repo_root=tmp_path)

    assert len(verified) == 3
    assert len(evaluations) == 2
    assert result["provider_posts_emitted"] == 0
    assert result["read_only"] is True


@pytest.mark.parametrize(
    ("target", "authority_path"),
    [
        ("manifest", lambda case: Path(case.rows[0]["stage1_reuse"]["typed_manifest_path"])),
        (
            "source_summary",
            lambda case: Path(
                case.rows[0]["stage1_reuse"]["binding"]["source_authority_artifact_path"]
            ),
        ),
        ("provider_output", lambda case: case.output_paths[0]),
        ("closure", lambda case: case.closure_path),
        ("ledger", lambda case: case.ledger_path),
    ],
)
def test_public_chunk_plan_rejects_shared_authority_bytes_changed_after_last_verification(
    shared_provider_case: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target: str,
    authority_path: Callable[[Any], Path],
) -> None:
    case = shared_provider_case
    _canonicalize_case(case)
    path = authority_path(case)

    def mutate_after_last(
        verified_count: int,
        _row: Any,
        _binding: Any,
    ) -> None:
        if verified_count == 3:
            with path.open("ab") as handle:
                handle.write(f"\nlate {target} byte drift\n".encode())

    verified = _wrap_real_authority_verifier(
        monkeypatch,
        after_success=mutate_after_last,
    )

    with pytest.raises(control_plane_module.ControlPlaneError, match="changed during verification") as caught:
        _plan(case, repo_root=tmp_path)

    assert isinstance(caught.value.__cause__, RegistryError)
    assert len(verified) == 3



def test_chunk_plan_hash_work_does_not_scale_with_input_locator_count(
    shared_provider_case: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    case = shared_provider_case
    _canonicalize_case(case)
    original = PaperContentLayers.content_hash.fget
    assert original is not None
    observed: list[str] = []

    def tracked(model: PaperContentLayers) -> str:
        value = original(model)
        observed.append(value)
        return value

    monkeypatch.setattr(PaperContentLayers, "content_hash", property(tracked))
    single = _plan(case, repo_root=tmp_path)
    single_count = len(observed)
    observed.clear()
    inputs = [Path(row["stage1_reuse"]["typed_manifest_path"]) for row in case.rows]
    multiple = _plan(case, repo_root=tmp_path, summary_files=inputs)
    multiple_count = len(observed)

    assert single_count > 0 and multiple_count == single_count
    for result, expected_paths in ((single, [_summary_path(case)]), (multiple, inputs)):
        inventory = result["semantic_chunk_plan"]["reuse_inventory"]
        assert [row["source_path"] for row in inventory] == [str(path.resolve()) for path in expected_paths]
        assert all(row["content_hash"] == result["content_layers"]["content_hash"] for row in inventory)
